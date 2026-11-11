"""Payload-only canonical Qdrant corpus, with one local service owner.

Qdrant stores immutable revisions, explicit heads and historical evidence. SQLite
jobs store replayable intents and confirmed stage receipts, never select heads.
The local Qdrant lifetime lock excludes a second process; an RLock serializes
all readers/writers in the owner. This is not Qdrant distributed transactional CAS.
"""
import fcntl
import itertools
import json
from pathlib import Path
import threading
import time
import uuid
import weakref
from collections import Counter
from typing import Callable
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue, PointStruct
from .contracts import Fault
from .repository import digest, dump

COLLECTIONS = ('tags', 'loras', 'checkpoints', 'generations', 'tag_relations')
FORMAT = 'atlas-canonical-corpus-v1'


def point_id(value):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, FORMAT + ':' + dump(value)))


def entity_id(collection, identity):
    fields = {
        'tags': ('tag',), 'loras': ('system', 'model_id', 'version_id'),
        'checkpoints': ('system', 'model_id', 'version_id'),
        'generations': ('system', 'image_id'), 'tag_relations': ('endpoints',),
    }
    # Typed source asset/URL identities are not fabricated Civitai numeric IDs.
    generation_asset = collection == 'generations' and set(identity) in ({'system', 'asset_id'}, {'system', 'source_url'})
    if collection not in fields or (set(identity) != set(fields[collection]) and not generation_asset):
        raise Fault('CORPUS_IDENTITY', 'identity must have exact fields for ' + collection)
    if collection == 'tag_relations':
        endpoints = identity['endpoints']
        if not isinstance(endpoints, list) or len(endpoints) != 2:
            raise Fault('CORPUS_IDENTITY', 'relation needs two literal signed endpoints')
        for e in endpoints:
            if (not isinstance(e, dict) or set(e) != {'tag', 'polarity'}
                    or not isinstance(e['tag'], str) or e['polarity'] not in ('positive', 'negative')):
                raise Fault('CORPUS_IDENTITY', 'invalid signed endpoint')
        # Endpoint order is part of raw relation identity. The importer explicitly
        # emits JSON-sorted unordered pairs, without rewriting any literal tag.
    else:
        for key, value in identity.items():
            if key in ('model_id', 'version_id', 'image_id'):
                valid = type(value) in (str, int)
            else:
                valid = isinstance(value, str)
            if not valid:
                raise Fault('CORPUS_IDENTITY', 'identity values must be literal strings or source IDs')
    return point_id([collection, identity])


class Corpus:
    def __init__(self, root, repo):
        root = Path(root)
        if not root.is_absolute() or root.resolve() != root:
            raise ValueError('explicit absolute nonsymlink corpus root required')
        root.mkdir(parents=True, exist_ok=True)
        self.owner = (root / 'owner.lock').open('a')
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.owner.close()
            raise Fault('CORPUS_BUSY', 'another process owns this local Qdrant corpus')
        self.repo = repo
        self.root = root
        self.lock = threading.RLock()
        self.job_locks = weakref.WeakValueDictionary()
        self.fault_hook: Callable[[str, dict], None] | None = None  # never a transport field
        try:
            marker = root / 'corpus.json'
            if marker.exists():
                if json.loads(marker.read_text()) != {'format': FORMAT, 'backend': 'qdrant-local'}:
                    raise ValueError('unrecognized corpus ownership marker')
            else:
                if set(p.name for p in root.iterdir()) != {'owner.lock'}:
                    raise ValueError('refuse nonempty unowned corpus root')
                marker.write_text(dump({'format': FORMAT, 'backend': 'qdrant-local'}))
            self.client = QdrantClient(path=str(root / 'qdrant'), force_disable_check_same_thread=True)
            if {c.name for c in self.client.get_collections().collections} - set(COLLECTIONS):
                raise ValueError('unexpected collections in owned corpus')
            for name in COLLECTIONS:
                if not self.client.collection_exists(name):
                    self.client.create_collection(name, vectors_config={})
                if self.client.get_collection(name).config.params.vectors != {}:
                    raise ValueError('corpus requires payload-only collections')
        except BaseException:
            if hasattr(self, 'client'):
                self.client.close()
            self.owner.close()
            raise

    def close(self):
        with self.lock:
            self.client.close()
            self.owner.close()

    def hook(self, event, detail):
        if self.fault_hook:
            self.fault_hook(event, detail)

    def read_point(self, collection, pid):
        rows = self.client.retrieve(collection, [pid], with_payload=True, with_vectors=False)
        return rows[0].payload if rows else None

    def write_point(self, collection, pid, payload, immutable=False):
        old = self.read_point(collection, pid)
        if immutable and old is not None:
            if old != payload:
                raise Fault('CORPUS_INTEGRITY', 'immutable point differs')
            return
        self.client.upsert(collection, [PointStruct(id=pid, vector={}, payload=payload)], wait=True)
        self.hook('after_write', {'collection': collection, 'id': pid, 'kind': payload['kind']})
        if self.read_point(collection, pid) != payload:
            raise Fault('CORPUS_READBACK', 'Qdrant readback differs')

    def scan(self, collection, **where):
        filt = Filter(must=[FieldCondition(key=k, match=MatchValue(value=v)) for k, v in where.items()])
        offset = None
        items = []
        while True:
            rows, offset = self.client.scroll(collection, scroll_filter=filt, offset=offset,
                                               limit=128, with_payload=True, with_vectors=False)
            items.extend(r.payload for r in rows)
            if offset is None:
                return items

    def revisions(self, collection, eid):
        return sorted(self.scan(collection, kind='revision', entity_id=eid), key=lambda r: r['revision'])

    def head(self, collection, eid):
        # A persisted revision is the commit decision. Repair a head lost between
        # revision and head writes from Qdrant alone, never a SQLite-selected body.
        revisions = self.revisions(collection, eid)
        if not revisions:
            return None
        if len({r['revision'] for r in revisions}) != len(revisions):
            raise Fault('CORPUS_INTEGRITY', 'duplicate revision numbers')
        latest = revisions[-1]
        hp = {'format': FORMAT, 'kind': 'head', 'entity_id': eid,
              'revision': latest['revision'], 'revision_id': latest['revision_id']}
        hid = point_id(['head', collection, eid])
        if self.read_point(collection, hid) != hp:
            self.write_point(collection, hid, hp)
        return latest

    @staticmethod
    def receipt(revision):
        return {k: revision[k] for k in ('collection', 'entity_id', 'revision', 'revision_id', 'deleted')}

    def append(self, collection, identity, body, attribution, occurrences, editor, token,
               expected=None, action='put', importing=False, derived=None, restore_revision=None,
               allowed_heads=None):
        eid = entity_id(collection, identity)
        rid = point_id(['revision', collection, eid, token])
        current = self.head(collection, eid)
        existing = self.read_point(collection, rid)
        if existing:
            return self.receipt(existing)
        observation = digest({'collection': collection, 'identity': identity, 'body': body,
                              'attribution': attribution, 'occurrences': occurrences})
        if importing:
            for old in self.revisions(collection, eid):
                if not old['deleted'] and old['observation_id'] == observation:
                    return self.receipt(old)  # old replay cannot move a current head
        revision = current['revision'] if current else 0
        if allowed_heads is not None and (current['revision_id'] if current else None) not in allowed_heads:
            raise Fault('REVISION_CONFLICT', 'head changed since import intent was reserved')
        if expected is not None and expected != revision:
            raise Fault('REVISION_CONFLICT', 'current revision is ' + str(revision))
        if action in ('delete', 'restore') and not current:
            raise Fault('NOT_FOUND', 'corpus entity not found')
        if action == 'put' and current and current['deleted']:
            raise Fault('TOMBSTONED', 'explicit corpus.restore required')
        if action == 'restore':
            source = next((r for r in self.revisions(collection, eid)
                           if r['revision'] == restore_revision), None)
            if source is None or source['deleted']:
                raise Fault('RESTORE_REVISION', 'restore target must be a nondeleted revision')
            body, attribution, occurrences = source['body'], source['attribution'], source['occurrences']
            observation = source['observation_id']
        elif action == 'delete':
            assert current is not None  # existence checked before selecting the action
            body, attribution, occurrences = current['body'], current['attribution'], current['occurrences']
            observation = current['observation_id']
        if derived is None:
            derived = current['derived'] if current else {}
        record = {'format': FORMAT, 'kind': 'revision', 'collection': collection,
                  'entity_id': eid, 'identity': identity, 'revision_id': rid,
                  'revision': revision + 1, 'supersedes': current['revision_id'] if current else None,
                  'body': body, 'attribution': attribution, 'occurrences': occurrences,
                  'origin_attribution': current['origin_attribution'] if current else attribution,
                  'origin_editor': current['origin_editor'] if current else editor,
                  'editor': editor, 'action': action,
                  'deleted': action == 'delete' or (action == 'derive' and bool(current and current['deleted'])),
                  'observation_id': observation, 'derived': derived, 'at': time.time()}
        self.write_point(collection, rid, record, immutable=True)
        self.head(collection, eid)
        return self.receipt(record)

    def entry(self, entry, editor, token, importing, allowed_heads=None):
        return self.append(entry['collection'], entry['identity'], entry['body'],
                           entry['attribution'], entry['occurrences'], editor, token,
                           expected=entry['expected_revision'], importing=importing, allowed_heads=allowed_heads)

    def historical_relations(self, revision, editor):
        if revision['deleted'] or revision['occurrences'] is None:
            return
        # Membership/file/metadata refreshes are not additional numeric evidence.
        # Reuse the earliest literal occurrence observation for this canonical
        # generation, including pre-dual-stream evidence IDs already stored.
        revision = min((r for r in self.revisions('generations', revision['entity_id'])
                        if not r['deleted'] and r['occurrences'] == revision['occurrences']),
                       key=lambda r: r['revision'], default=revision)
        counts = Counter((polarity, tag) for polarity, tags in revision['occurrences'].items() for tag in tags)
        endpoints = sorted(counts, key=lambda item: dump({'tag': item[1], 'polarity': item[0]}))
        for a, b in itertools.combinations_with_replacement(endpoints, 2):
            n = counts[a] * counts[b] if a != b else counts[a] * (counts[a] - 1) // 2
            if not n:
                continue
            identity = {'endpoints': [{'tag': tag, 'polarity': polarity} for polarity, tag in (a, b)]}
            eid = entity_id('tag_relations', identity)
            evidence_id = point_id(['evidence', eid, revision['observation_id']])
            evidence = self.read_point('tag_relations', evidence_id)
            if evidence is None:
                evidence = {'format': FORMAT, 'kind': 'evidence', 'entity_id': eid,
                            'observation_id': revision['observation_id'],
                            'source_identity': revision['identity'], 'source_revision_id': revision['revision_id'],
                            'pair_occurrences': n, 'scope': 'all_historical_observations'}
                self.write_point('tag_relations', evidence_id, evidence, immutable=True)
            # Recompute from immutable Qdrant evidence, not a read/increment/write
            # counter. Lost ACK between evidence and head cannot multiply support.
            items = sorted(self.scan('tag_relations', kind='evidence', entity_id=eid),
                           key=lambda e: e['observation_id'])
            derived = {'scope': 'all_historical_observations', 'historical_support': len(items),
                       'historical_pair_occurrences': sum(e['pair_occurrences'] for e in items),
                       'evidence': items, 'current_only': False}
            head = self.head('tag_relations', eid)
            if head and head['derived'] == derived:
                continue
            self.append('tag_relations', identity, head['body'] if head else {},
                        head['attribution'] if head else {}, None, editor,
                        ['derived', eid, digest(derived)], derived=derived, action='derive')

    def progress(self, job):
        return {'job_id': job['id'], 'state': job['state'],
                'completed_stages': job.get('completed_stages', []),
                'total_stages': len(COLLECTIONS), 'stages': list(COLLECTIONS),
                'partial': job['state'] != 'succeeded',
                'visibility': 'confirmed stages readable immediately; no multi-collection atomicity',
                'receipts': job.get('receipts', []), 'error': job.get('corpus_error')}

    def run(self, job):
        # Same-job retries serialize separately. Collection stages release the
        # corpus lock so reads/progress and other writers can observe partial state.
        with self.lock:
            job_lock = self.job_locks.get(job['id'])
            if job_lock is None:
                job_lock = threading.Lock()
                self.job_locks[job['id']] = job_lock
        with job_lock:
            return self.run_locked_job(job)

    def run_locked_job(self, job):
        job = self.repo.get(job['id'])
        if job['state'] == 'succeeded':
            return self.progress(job)
        if job['state'] == 'failed':
            error = job['corpus_error']
            raise Fault(error['code'], error['message'])
        payload = job['payload']
        op = job['corpus_operation']
        entries = payload['entries'] if op == 'corpus.import' else [payload]
        editor = {'principal': job['owner_principal'], 'source_client': job['source_client']}
        done = list(job.get('completed_stages', []))
        receipts = list(job.get('receipts', []))
        try:
            for collection in COLLECTIONS:
                if collection in done:
                    continue
                with self.lock:
                    for index, entry in enumerate(entries):
                        if entry['collection'] != collection:
                            continue
                        token = [job['id'], index]
                        if op in ('corpus.delete', 'corpus.restore'):
                            receipt = self.append(collection, entry['identity'], {}, {}, None, editor, token,
                                                  expected=entry['expected_revision'], action=op.split('.')[1],
                                                  restore_revision=entry.get('restore_revision'))
                        else:
                            allowed = None
                            if op == 'corpus.import':
                                eid = entity_id(collection, entry['identity'])
                                captured = job.get('import_heads', {})
                                if eid not in captured:
                                    raise Fault('REVISION_CONFLICT', 'import intent has no captured head; use a new request')
                                # Only the reserved head or an earlier write of this
                                # exact intent may precede a pending entry. Persisted
                                # token replay/dedup in append remains a read-only receipt.
                                allowed = {captured[eid]}
                                allowed.update(point_id(['revision', collection, eid, [job['id'], prior]])
                                               for prior in range(index))
                            receipt = self.entry(entry, editor, token, op == 'corpus.import', allowed)
                        if receipt not in receipts:
                            receipts.append(receipt)
                    if collection == 'tags':
                        # An explicit occurrence list is a numeric input, not a parser
                        # of opaque source prompts. Never replace an existing raw tag.
                        for entry in entries:
                            if entry['collection'] != 'generations' or not entry.get('occurrences'):
                                continue
                            for tags in entry['occurrences'].values():
                                for tag in tags:
                                    identity = {'tag': tag}
                                    if self.head('tags', entity_id('tags', identity)) is None:
                                        self.append('tags', identity, {}, {}, None, editor,
                                                    ['literal-tag', tag], importing=True)
                    if collection == 'tag_relations':
                        # All revisions, including superseded/deleted sources.
                        for receipt in receipts:
                            if receipt['collection'] == 'generations':
                                for revision in self.revisions('generations', receipt['entity_id']):
                                    self.historical_relations(revision, editor)
                    self.hook('before_stage_receipt', {'collection': collection, 'job_id': job['id']})
                    done.append(collection)
                    job = self.repo.update(job['id'], 'running',
                                           {'completed_stages': done, 'receipts': receipts, 'corpus_error': None},
                                           event='corpus.stage_confirmed')[0]
                self.hook('after_stage', {'collection': collection, 'job_id': job['id']})
            job = self.repo.update(job['id'], 'succeeded', {'corpus_error': None},
                                   event='corpus.completed')[0]
        except Exception as exc:
            # A stage may have partial writes. No stage receipt is advanced until
            # every write/readback in it completed. Retry reconciles deterministic IDs.
            terminal = isinstance(exc, Fault) and exc.code in {
                'REVISION_CONFLICT', 'TOMBSTONED', 'RESTORE_REVISION', 'NOT_FOUND'}
            self.repo.update(job['id'], 'failed' if terminal else 'running',
                             {'corpus_error': {'type': type(exc).__name__, 'message': str(exc),
                                               'code': getattr(exc, 'code', 'INTERRUPTED')}},
                             event='corpus.interrupted')
            raise
        return self.progress(job)

    def call(self, op, p, principal, source):
        if op in ('corpus.import.get', 'corpus.import.resume'):
            with self.lock:
                job = self.repo.get(p['job_id'])
                if job['kind'] != 'corpus':
                    raise Fault('KIND_MISMATCH', 'not a corpus operation')
            return self.run(job) if op.endswith('.resume') else self.progress(job)
        if op in ('corpus.put', 'corpus.delete', 'corpus.restore', 'corpus.import'):
            with self.lock:
                entries = p['entries'] if op == 'corpus.import' else [p]
                for entry in entries:
                    entity_id(entry['collection'], entry['identity'])
                    if entry.get('occurrences') is not None and entry['collection'] != 'generations':
                        raise Fault('CORPUS_OCCURRENCES', 'signed occurrences belong to generations')
                # Include operation in the durable intent hash. Namespace remains
                # exactly (principal, request_id), shared with existing engine jobs.
                intent = {'operation': op, **p}
                captured = {}
                if op == 'corpus.import':
                    for entry in entries:
                        eid = entity_id(entry['collection'], entry['identity'])
                        head = self.head(entry['collection'], eid)
                        captured[eid] = head['revision_id'] if head else None
                job, _ = self.repo.reserve(principal, source, p['request_id'], 'corpus', intent,
                                           {'corpus_operation': op, 'completed_stages': [], 'receipts': [],
                                            'import_heads': captured})
            return self.run(job)
        with self.lock:
            collection = p['collection']
            if op == 'corpus.page':
                heads = []
                # Include revision-only entities after an interrupted head write.
                for eid in sorted({r['entity_id'] for r in self.scan(collection, kind='revision')}):
                    if eid <= p['after']:
                        continue
                    head = self.head(collection, eid)
                    assert head is not None  # entity was enumerated from revisions
                    if p['include_deleted'] or not head['deleted']:
                        heads.append(head)
                    if len(heads) > p['limit']:
                        break
                more = len(heads) > p['limit']
                items = heads[:p['limit']]
                return {'items': items, 'has_more': more,
                        'next_after': items[-1]['entity_id'] if more else None}
            eid = entity_id(collection, p['identity'])
            if op == 'corpus.history':
                items = [r for r in self.revisions(collection, eid) if r['revision'] > p['after_revision']]
                more = len(items) > p['limit']
                items = items[:p['limit']]
                return {'items': items, 'has_more': more,
                        'next_after_revision': items[-1]['revision'] if more else None}
            head = self.head(collection, eid)
            if head is None:
                raise Fault('NOT_FOUND', 'corpus entity not found')
            return head
