"""Dual-stream frozen refresh, using the existing canonical corpus/receipts.

Community quota is independent of author overlap. Source aliases are evidence,
not synthetic numeric IDs. Corpus children are bounded *batches*, never a bound
on the complete author snapshot. Legacy jobs continue through onboarding.run.
"""
import time
from .contracts import Fault
from .repository import digest, dump
from .corpus import entity_id
from .corpus_contracts import CorpusImport
from . import civitai_sources as source
from .onboarding import numeric


def unique(records, fixture, known=None):
    groups, index = [], {}
    historical = {}
    for alias, identities in (known or {}).items():
        for identity in identities:
            historical.setdefault(identity, []).append(alias)
    for position, record in enumerate(records):
        keys = [dump(a) for a in source.aliases(record, fixture)]
        resolved = {identity for key in keys for identity in (known or {}).get(key, {})}
        if len(resolved) > 1:
            raise Fault('SOURCE_IDENTITY_CONFLICT', 'multiple existing canonical generations match source aliases')
        keys = list(dict.fromkeys(keys + [a for identity in resolved for a in historical[identity]]))
        matches = sorted({index[k] for k in keys if k in index})
        merged_keys = list(dict.fromkeys([a for i in matches for a in groups[i]['aliases']] + keys))
        ids = {__import__('json').loads(a).get('image_id') for a in merged_keys} - {None}
        if len(ids) > 1:
            raise Fault('SOURCE_IDENTITY_CONFLICT', 'one asset has conflicting source image IDs')
        observations = sorted([x for i in matches for x in groups[i]['observations']] + [(position, record)], key=lambda x: x[0])
        target = matches[0] if matches else len(groups)
        raw = next((r for _, r in observations if isinstance(r.get('meta'), dict)), observations[0][1])
        group = {'raw': raw, 'position': observations[0][0], 'aliases': merged_keys,
                 'observations': observations, 'group_index': target}
        if matches:
            groups[target] = group
            for i in matches[1:]:
                groups[i] = None
        else:
            groups.append(group)
        for key in merged_keys:
            index[key] = target
    return [g for g in groups if g is not None]


def existing_aliases(o):
    # Qdrant remains canonical; no new alias database. Include historical source
    # observations so a changed rendition URL does not orphan an existing ID.
    result, histories = {}, {}
    with o.e.corpus.lock:
        for revision in o.e.corpus.scan('generations', kind='revision'):
            identity = revision['identity']
            records = [revision['body'].get('source_record')]
            records += [x.get('raw') for x in revision['body'].get('source_records', [])]
            keys = [dump(identity)]
            keys += [dump(a) for a in revision['body'].get('source_aliases', [])]
            for record in records:
                if isinstance(record, dict):
                    keys.extend(dump(a) for a in source.aliases(record, o.fixture))
            histories.setdefault(dump(identity), {'identity': identity, 'keys': set()})['keys'].update(keys)
    for canonical, history in histories.items():
        ids = {__import__('json').loads(a).get('image_id') for a in history['keys']} - {None}
        if len(ids) > 1:
            raise Fault('SOURCE_IDENTITY_CONFLICT', 'historical generation has conflicting source image IDs')
        for key in sorted(history['keys']):
            result.setdefault(key, {})[canonical] = history['identity']
    return result


def source_groups(o, job, known, community=None):
    records = {'author': job.get('author_records', []),
               'community': job.get('community_records', []) if community is None else community}
    all_records = records['author'] + records['community'] + [x['raw'] for x in job.get('author_hydration', [])]
    groups = unique(all_records, o.fixture, known)
    # A bridge can be historical OR in the other stream/hydration. Count each
    # stream with exactly the equivalence used for canonical snapshot samples.
    equivalence = {alias: {group['aliases'][0]: {}}
                   for group in groups for alias in group['aliases']}
    streams = {stream: unique(rows, o.fixture, equivalence) for stream, rows in records.items()}
    return groups, streams


def snapshot(o, job):
    p, v = job['payload'], job['verified']
    known = job['canonical_aliases'] if 'canonical_aliases' in job else existing_aliases(o)
    groups, streams = source_groups(o, job, known)
    samples, lookup = [], {}
    for group in groups:
        matches = {}
        for alias in group['aliases']:
            matches.update(known.get(alias, {}))
        if len(matches) > 1:
            raise Fault('SOURCE_IDENTITY_CONFLICT', 'multiple existing canonical generations match source aliases')
        aliases = [__import__('json').loads(a) for a in group['aliases']]
        identity = next(iter(matches.values())) if matches else next((a for a in aliases if 'image_id' in a), aliases[0])
        sample = {'identity': identity, 'aliases': aliases, 'raw': group['raw'],
                  'source_records': [], 'memberships': [],
                  'expected_revision': job['initial_generation_heads'].get(entity_id('generations', identity), 0)}
        samples.append(sample)
        for alias in group['aliases']:
            lookup[alias] = sample
    order = {}
    for stream, records in streams.items():
        order[stream] = []
        for item in records:
            x = lookup[item['aliases'][0]]
            membership = {'model_id': p['model_id'], 'version_id': p['version_id'],
                          'stream': stream, 'source_position': item['position']}
            if stream == 'author':
                membership.update(source_index=item['raw'].get('index'), post_id=item['raw'].get('postId'))
            x['memberships'].append(membership)
            for _, record in item['observations']:
                x['source_records'].append({'stream': stream, 'raw': record})
            # Prefer available literal metadata, never reconstruct missing fields.
            if not isinstance(x['raw'].get('meta'), dict) and isinstance(item['raw'].get('meta'), dict):
                x['raw'] = item['raw']
            order[stream].append({'identity': x['identity'], 'source_position': item['position']})
    for observation in job.get('author_hydration', []):
        record = observation['raw']
        x = lookup[dump({'system': 'civitai', 'image_id': record['id']})]
        x['source_records'].append({'stream': 'author', 'role': 'rest_metadata', **observation})
        # REST supplies the downloadable rendition, even when literal meta is unknown.
        if isinstance(record.get('meta'), dict) or not isinstance(x['raw'].get('meta'), dict):
            x['raw'] = record
    for page in job.get('author_pages', []):
        if page.get('role') != 'image_order':
            continue
        for record in page['raw']['result']['data']['json']['items']:
            for key in source.aliases(record, o.fixture):
                x = lookup.get(dump(key))
                if x:
                    for observation in x['source_records']:
                        if observation['raw'] == record and observation['stream'] == 'author':
                            observation.update(role='rpc_order', source_url=page['url'])
                    break
    return samples, order


def progress(o, job):
    samples = job.get('sample', [])
    community = job.get('community_status', {'complete': False, 'reason': 'not_queried'})
    author = job.get('author_status', {'complete': False, 'reason': 'not_queried'})
    children = [o.e.corpus.progress(o.e.repo.get(i)) for i in job.get('corpus_job_ids', [])]
    return {'job_id': job['id'], 'state': job['state'], 'collector_semantics': source.SEMANTICS,
            'partial': job['state'] != 'succeeded' or not author['complete'] or not community['complete']
                       or any(x.get('file', {}).get('status') != 'saved' for x in samples),
            'requested': job['payload']['gallery_limit'], 'community': community, 'author': author,
            'refresh_of': job.get('refresh_of'),
            'recovery_of': job.get('recovery_of'),
            'recovery_provenance': job.get('recovery_provenance'),
            'available_examples': len(samples), 'saved_images': sum(x.get('file', {}).get('status') == 'saved' for x in samples),
            'source_order': job.get('source_order', {}),
            'sample': [{'identity': x['identity'], 'image_id': x['identity'].get('image_id'),
                        'memberships': x['memberships'], 'file': x.get('file')} for x in samples],
            'pages': job.get('pages', []), 'pagination_complete': job.get('pagination_complete', False),
            'stop_reason': community.get('reason'), 'corpus_job_ids': job.get('corpus_job_ids', []),
            'corpus_job_id': job.get('corpus_job_ids', [None])[0] if job.get('corpus_job_ids') else None,
            'corpus_progress': children, 'completed_stages': job.get('completed_stages', []),
            'error': job.get('onboarding_error'), 'retry_at': job.get('retry_at'),
            'visibility': 'saved files and completed corpus batches immediately readable; no atomic publication',
            'native_install': 'not performed; verified existing inventory only'}


def entries(o, job):
    p, v = job['payload'], job['verified']
    result = [{'collection': v['collection'], 'identity': o.identity(p),
               'expected_revision': v['expected_revision'],
               'body': {'source_model': v['model'], 'source_version': v['version'],
                        'selected_file': v['selected_file'], 'registered_reference': v['registered'],
                        'gallery': job['source_order']['community'], 'author_carousel': job['source_order']['author'],
                        'source_pages': job['pages'], 'author_pages': job.get('author_pages', []), 'author_status': job['author_status'],
                        'community_status': job['community_status'], 'collector_semantics': source.SEMANTICS,
                        'literal_triggers': v['version'].get('trainedWords'),
                        'numeric_scope': 'explicit source arrays only; gallery association is not evidence of resource usage'},
               'attribution': o.identity(p)}]
    for x in job['sample']:
        occurrences, scope = numeric(x['raw'])
        with o.e.corpus.lock:
            prior = o.e.corpus.head('generations', entity_id('generations', x['identity']))
        memberships = [a for a in (prior or {}).get('body', {}).get('memberships', [])
                       if (a.get('model_id'), a.get('version_id')) != (p['model_id'], p['version_id'])]
        result.append({'collection': 'generations', 'identity': x['identity'], 'expected_revision': x['expected_revision'],
                       'body': {'source_record': x['raw'], 'source_records': x['source_records'],
                                'source_aliases': x['aliases'], 'memberships': memberships + x['memberships'],
                                'file': x['file'], 'numeric_inputs': scope,
                                'resource_usage': {'status': 'source_fields_only', 'gallery_membership_implies_usage': False}},
                       'attribution': x['identity'], 'occurrences': occurrences})
        if job.get('recovery_of') and prior:
            # Keep literal metadata/aliases from other source memberships too.
            # The current source record is fresh; prior observations remain raw,
            # not reinterpreted as a new stream observation or numeric evidence.
            body = result[-1]['body']
            old = prior['body']
            for key in ('source_records', 'source_aliases'):
                body[key] = list(old.get(key, [])) + [a for a in body[key] if a not in old.get(key, [])]
            old_raw = old.get('source_record')
            if old_raw and old_raw != body['source_record']:
                record = {'role': 'prior_source_observation', 'raw': old_raw}
                if record not in body['source_records']:
                    body['source_records'].append(record)
    literal_tags = []
    for array in (v['model'].get('tags'), v['version'].get('trainedWords')):
        if isinstance(array, list):
            for tag in array:
                if isinstance(tag, str) and tag not in literal_tags:
                    literal_tags.append(tag)
    # Model vocabulary is separate from image occurrences. Batches remove the
    # old whole-import item bound; no literal tag is silently dropped.
    for tag in literal_tags:
        identity = {'tag': tag}
        if o.expected('tags', identity) == 0:
            result.append({'collection': 'tags', 'identity': identity, 'body': {}, 'expected_revision': 0})
    return result


def run(o, job):
    repo, p = o.e.repo, job['payload']
    if job['state'] in ('succeeded', 'failed'):
        return progress(o, job)
    blocked = max((j.get('retry_at', 0) or 0 for j in repo.iter_jobs('onboarding')), default=0)
    if blocked > time.time():
        raise Fault('SOURCE_BACKOFF', 'source Retry-After/backoff is active')
    def update(**changes):
        nonlocal job
        job = repo.update(job['id'], 'running', changes, event='onboarding.progress')[0]
        return job
    try:
        if 'verified' not in job:
            version = o.json('/api/v1/model-versions/' + str(p['version_id']))
            model = o.json('/api/v1/models/' + str(p['model_id']))
            collection, selected, registered = o.verify(p, version, model)
            update(verified={'collection': collection, 'version': version, 'model': model,
                             'selected_file': selected, 'registered': registered,
                             'expected_revision': job['initial_heads'][collection]},
                   community_records=[],
                   next_page=source.community_url(o.base, p['version_id'], p['gallery_limit']),
                   completed_stages=['verified_registered_file'])
        else:
            o.verify(p, job['verified']['version'], job['verified']['model'])
        if 'author_status' not in job:
            job = source.collect_author(o, job, update)
        if 'source_order' not in job and 'canonical_aliases' not in job:
            # Freeze identity evidence before quota decisions. Resume and snapshot
            # must not silently re-resolve aliases against later corpus history.
            captured = existing_aliases(o)
            changes: dict = {'canonical_aliases': captured}
            # A pre-fix interrupted collector may already have declared its raw
            # quota reached, but has not frozen a snapshot or performed effects.
            # Persist evidence and repaired stop status in the SAME checkpoint.
            count = len(source_groups(o, job, captured)[1]['community'])
            if job.get('pagination_complete') and count < p['gallery_limit']:
                nxt = job.get('next_page')
                cycle = nxt in [x['url'] for x in job['pages']] if nxt else False
                reason = 'pagination_cycle' if cycle else 'source_exhausted' if not nxt else None
                changes.update(pagination_complete=bool(reason), community_status={
                    'requested': p['gallery_limit'], 'observed': count, 'complete': False,
                    'exhausted': not nxt, 'reason': reason, 'shortfall_verified': not nxt})
            update(**changes)
        known = job.get('canonical_aliases', {})
        def community_count(records):
            return len(source_groups(o, job, known, records)[1]['community'])
        # No page/item ceiling. Each persisted page is a resumption checkpoint;
        # network/byte failures keep this job running, never declare exhaustion.
        while not job.get('pagination_complete'):
            url = job['next_page']
            page = o.json(url)
            items = page.get('items')
            if not isinstance(items, list):
                raise Fault('SOURCE_SHAPE', 'source items array required')
            candidates = job['community_records'] + items
            # Resolve the WHOLE page before cutting the quota. A late alias
            # bridge may collapse earlier selections; an earlier overflow item
            # on this same final page must still be available to fill the gap.
            selected = source_groups(o, job, known, candidates)[1]['community'][:p['gallery_limit']]
            records = [record for _, record in sorted(
                (observation for group in selected for observation in group['observations']),
                key=lambda observation: observation[0])]
            next_page = source.continuation(o, page, url, p['version_id'])
            reached = community_count(records) >= p['gallery_limit']
            cycle = next_page in [x['url'] for x in job['pages']] + [url] if next_page else False
            reason = 'community_quota' if reached else 'pagination_cycle' if cycle else 'source_exhausted' if not next_page else None
            update(community_records=records, pages=job['pages'] + [{'url': url, 'raw': page}], next_page=next_page,
                   pagination_complete=bool(reason), community_status={
                       'requested': p['gallery_limit'], 'observed': community_count(records),
                       'complete': reached, 'exhausted': not next_page, 'reason': reason,
                       'shortfall_verified': not reached and not next_page})
            o.hook('after_source_page', {'job_id': job['id']})
        if 'source_order' not in job:
            samples, order = snapshot(o, job)
            update(sample=samples, source_order=order)
        for index, x in enumerate(job['sample']):
            if 'file' not in x:
                ref = o.save_image(x['raw'], p['image_refresh'], job['id'], index)
                samples = list(job['sample'])
                samples[index] = {**x, 'file': ref}
                update(sample=samples)
            elif x['file']['status'] == 'saved':
                o.verify_file(x['file'])
        if 'corpus_payloads' not in job:
            with o.e.corpus.lock:
                frozen = entries(o, job)
                if job.get('recovery_of'):
                    from .onboarding_recovery import validate_entries
                    provenance = validate_entries(o, job, frozen)
                else:
                    provenance = None
            payloads = [CorpusImport.model_validate({'request_id': 'onboard-corpus:' + job['id'] + ':' + str(i // 80),
                                                    'entries': frozen[i:i+80]}).model_dump()
                        for i in range(0, len(frozen), 80)]
            update(corpus_payloads=payloads, recovery_provenance=provenance)
        ids = list(job.get('corpus_job_ids', []))
        for index, payload in enumerate(job['corpus_payloads']):
            if len(ids) <= index:
                with o.e.corpus.lock:
                    captured = {}
                    for entry in payload['entries']:
                        eid = entity_id(entry['collection'], entry['identity'])
                        head = o.e.corpus.head(entry['collection'], eid)
                        captured[eid] = head['revision_id'] if head else None
                    child, _ = repo.reserve(job['owner_principal'], job['source_client'], payload['request_id'], 'corpus',
                                            {'operation': 'corpus.import', **payload},
                                            {'corpus_operation': 'corpus.import', 'completed_stages': [], 'receipts': [], 'import_heads': captured})
                o.hook('after_corpus_reserved', {'job_id': child['id']})
                ids.append(child['id'])
                update(corpus_job_ids=ids, completed_stages=['verified_registered_file', 'source_snapshot', 'image_files'])
            child_progress = o.e.corpus.run(repo.get(ids[index]))
            if child_progress['state'] != 'succeeded':
                raise Fault('CORPUS_INCOMPLETE', 'corpus batch did not complete')
            o.hook('after_corpus_write', {'job_id': ids[index]})
        if job.get('recovery_of'):
            # Corpus may legitimately return a historical observation receipt
            # without moving a newer head. Do not report recovery success over
            # an external edit just because that read-only replay was safe.
            from .onboarding_recovery import validate_entries
            with o.e.corpus.lock:
                validate_entries(o, job, [entry for payload in job['corpus_payloads']
                                         for entry in payload['entries']], captured=False)
        update(completed_stages=['verified_registered_file', 'source_snapshot', 'image_files', 'corpus'])
        job = repo.update(job['id'], 'succeeded', {'onboarding_error': None, 'retry_at': 0}, event='onboarding.completed')[0]
    except Exception as exc:
        terminal = isinstance(exc, Fault) and exc.code in {
            'REVISION_CONFLICT', 'TOMBSTONED', 'REGISTERED_KEY_MISMATCH', 'MODEL_TYPE_MISMATCH',
            'REGISTERED_FILE_MISMATCH', 'SOURCE_ID_MISMATCH', 'SOURCE_FILE_MISMATCH',
            'HASH_ALGORITHM_UNKNOWN', 'FILE_HASH_MISMATCH', 'SOURCE_IDENTITY_CONFLICT', 'RECOVERY_EXTERNAL_EDIT'}
        job = repo.update(job['id'], 'failed' if terminal else 'running',
                          {'onboarding_error': {'code': getattr(exc, 'code', 'INTERRUPTED'), 'type': type(exc).__name__,
                                                'message': str(exc) if isinstance(exc, Fault) else 'interrupted; resume frozen import'},
                           'retry_at': getattr(exc, 'retry_at', 0)}, event='onboarding.interrupted')[0]
        if not isinstance(exc, Fault):
            raise
    return progress(o, job)
