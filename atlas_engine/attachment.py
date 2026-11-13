"""Concrete offline historical attachment adoption, never a semantic conversion.

The operational ledger is copied whole. The original mirror stays an explicitly
external, pinned, immutable-read dependency. No source Store/Qdrant is opened.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import zlib

from .contracts import Fault
from .repository import dump
from .state_backup import safe_path, sha, write_json, sync_tree, MARKER
from .legacy_bindings import sealed_ledger
from .staged_import import table_state

RECEIPT = 'legacy-attachment.json'
CONSISTENCY = 'two exhaustive equal online passes; NOT atomic; writers NOT fenced'


def sealed(path):
    p = sealed_ledger(path)
    for suffix in ('-shm', '-wal', '-journal'):
        side = safe_path(str(p) + suffix)
        if side.exists() and not side.is_file():
            raise ValueError('nonregular SQLite sidecar')
    # Linux qualification only, not a writer fence or an immutability guarantee.
    st = p.stat()
    lockfile = Path('/proc/locks')
    if lockfile.exists():
        key = f'{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}'
        if any(key in line.split() for line in lockfile.read_text().splitlines()):
            raise ValueError('source has an active file lock')
    return p


def immutable(path):
    return sqlite3.connect(sealed(path).as_uri() + '?mode=ro&immutable=1', uri=True)


def verify_files(mapping):
    pins = mapping['files']
    if not isinstance(pins, list) or not pins:
        raise ValueError('explicit nonempty file inventory required')
    seen = set()
    for pin in pins:
        if set(pin) != {'path', 'bytes', 'sha256'} or type(pin['bytes']) is not int or pin['bytes'] < 0:
            raise ValueError('exact file pin fields required')
        if not isinstance(pin['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', pin['sha256']):
            raise ValueError('invalid SHA256 token')
        p = safe_path(pin['path'])
        if str(p) != pin['path'] or str(p) in seen or not p.is_file() or p.stat().st_nlink != 1:
            raise ValueError('unique absolute regular unlinked files required')
        seen.add(str(p))
        if p.suffix in ('.sqlite3', '.sqlite'):
            sealed(p)
        if p.stat().st_size != pin['bytes'] or sha(p) != pin['sha256']:
            raise ValueError('attachment source byte pin mismatch')
    return {p['path']: p for p in pins}


def validate(mapping):
    if (not isinstance(mapping, dict) or set(mapping) != {'format', 'mapping', 'attachment', 'files', 'source_consistency'}
            or mapping['format'] != 'atlas-legacy-attachment-mapping-v1'
            or mapping['mapping'] != 'ledger-preserve-v1' or mapping['source_consistency'] != CONSISTENCY):
        raise ValueError('explicit ledger-preserve-v1 mapping and non-atomic source limitation required')
    pins = verify_files(mapping)
    if mapping['attachment'] not in pins:
        raise ValueError('attachment must be byte pinned')
    a = json.loads(Path(mapping['attachment']).read_text())
    if a.get('format') != 'atlas-checkpoint-attachment-v1' or a.get('complete') is not True:
        raise ValueError('unsupported/incomplete historical attachment')
    delta = Path(a['reference_delta'])
    required = [a['baseline'], a['knowledge_ledger'], str(delta/'receipt.json'),
                str(delta/'overlay.sqlite3'), str(delta/'observations.sqlite3')]
    if any(p not in pins for p in required):
        raise ValueError('all attachment dependencies must be pinned')
    if pins[a['knowledge_ledger']]['sha256'] != a['knowledge_ledger_sha256'] or pins[str(delta/'receipt.json')]['sha256'] != a['reference_delta_receipt_sha256']:
        raise ValueError('attachment dependency binding differs')
    d = json.loads((delta/'receipt.json').read_text())
    if (d.get('format') != 'atlas-stream-delta-v1' or d.get('complete') is not True
            or d.get('baseline') != a['baseline'] or d.get('source_consistency') != CONSISTENCY
            or d.get('overlay_sha256') != pins[str(delta/'overlay.sqlite3')]['sha256']
            or d.get('observation_sha256') != pins[str(delta/'observations.sqlite3')]['sha256']):
        raise ValueError('stream delta binding differs')
    # Bind every directly declared *_sha256 dependency, not only the ledger.
    for key, value in a.items():
        if key.endswith('_sha256') and key[:-7] in a:
            path = a[key[:-7]]
            if path not in pins or pins[path]['sha256'] != value:
                raise ValueError('unbound declared attachment dependency: ' + key)
    return a


def schema(c):
    return list(c.execute("select type,name,tbl_name,sql from sqlite_master where name not like 'sqlite_%' order by type,name"))


def import_attachment(mapping, destination, fault_hook=lambda stage: None):
    a = validate(mapping)
    dest = safe_path(destination)
    if dest.exists() or not dest.parent.is_dir():
        raise ValueError('destination must be NEW with existing parent')
    for pin in mapping['files']:
        p = Path(pin['path'])
        if dest == p or dest in p.parents or p.parent in dest.parents:
            raise ValueError('destination overlaps attachment source')
    ledger = Path(a['knowledge_ledger'])
    free = shutil.disk_usage(dest.parent).free
    if free < ledger.stat().st_size * 4 + 2*1024**3:
        raise ValueError('insufficient space for bounded ledger copy plus reserve')
    with closing(immutable(ledger)) as source:
        expected = table_state(source)
        source_schema = schema(source)
        if not {'engine_meta', 'knowledge_revisions', 'knowledge_heads', 'legacy_rows'} <= set(expected):
            raise ValueError('attachment ledger schema requires a separately reviewed mapping')
        if any(expected.get(t, {}).get('rows') != n for t, n in a.get('counts', {}).items()):
            raise ValueError('attachment ledger counts differ')
        if any(expected.get(t, {}).get('rows', 0) for t in ('jobs', 'observations', 'uploads', 'artifacts')):
            raise ValueError('active/byte-path operational state needs a separately reviewed mapping')
        dest.mkdir(mode=0o700)
        state = {'format': 'atlas-restored-state-v1', 'reconciled': False,
                 'storage_target_root': str(dest), 'storage_source_roots': [str(ledger.parent)],
                 'backup_manifest_sha256': hashlib.sha256(dump(mapping).encode()).hexdigest(),
                 'preimage_verified': False, 'migration_kind': 'legacy-attachment-partial-v1'}
        write_json(dest/MARKER, state)
        sync_tree(dest)
        fault_hook('quarantine')
        shutil.copyfile(ledger, dest/'registry.sqlite3')
        (dest/'registry.sqlite3').chmod(0o600)
        sync_tree(dest)
        fault_hook('ledger')
        with closing(immutable(dest/'registry.sqlite3')) as target:
            if schema(target) != source_schema or table_state(target) != expected or target.execute('pragma integrity_check').fetchall() != [('ok',)]:
                raise ValueError('whole ledger preservation failed')
        if sha(dest/'registry.sqlite3') != a['knowledge_ledger_sha256']:
            raise ValueError('ledger file bytes differ')
    # Explicit new empty canonical authority; no legacy point is relabelled.
    from .corpus import Corpus
    corpus = Corpus(dest/'corpus', None)
    corpus.close()
    (dest/'service.lock').touch(mode=0o600, exist_ok=False)
    write_json(dest/'connection-binding.json', None)
    verify_files(mapping)
    with closing(immutable(ledger)) as c:
        inert_sources = [dict(zip(('source', 'table_name', 'rows'), row)) for row in
                         c.execute('select source,table_name,count(*) from legacy_rows group by source,table_name order by source,table_name')]
    with closing(immutable(a['baseline'])) as c:
        mirror_collections = [dict(zip(('name', 'captured_points'), row)) for row in
                              c.execute('select collection,count(*) from points group by collection order by collection')]
    receipt = {'format': 'atlas-legacy-attachment-receipt-v1', 'target_root': str(dest),
               'mapping': mapping, 'mapping_sha256': state['backup_manifest_sha256'],
               'source_consistency': CONSISTENCY, 'source_atomic': False, 'writers_fenced': False,
               'ledger_tables': expected, 'ledger_schema': source_schema,
               'ledger_copy_sha256': sha(dest/'registry.sqlite3'),
               'inert_sources': inert_sources, 'baseline_collections': mirror_collections,
               'attachment_usage': 'external read-only dependency; original files NOT copied',
               'canonicalized_legacy_points': 0, 'portable_self_contained': False,
               'full_fidelity_migration_accepted': False,
               'referenced_blob_availability': 'unassessed; source anchors and inert blob rows preserved; no fetch or inferred missing count',
               'unknown_data': 'whole ledger tables and complete original attachment bytes preserved without semantic conversion'}
    write_json(dest/RECEIPT, receipt)
    sync_tree(dest)
    fault_hook('receipt')
    state['preimage_verified'] = True
    write_json(dest/'restore-state.new', state)
    (dest/'restore-state.new').replace(dest/MARKER)
    sync_tree(dest)
    return receipt


class SealedReference:
    """Use the existing typed literal view without sidecar writes."""
    @staticmethod
    def make(path):
        from .reference_mirror import ReferenceMirror
        class Reader(ReferenceMirror):
            sealed_attachment = True
            def connect(self):
                return immutable(self.path)
        return Reader(path)


class Attachment:
    semantic_enabled = False

    def __init__(self, root):
        self.root = Path(root)
        r = json.loads((self.root/RECEIPT).read_text())
        if r.get('format') != 'atlas-legacy-attachment-receipt-v1' or r.get('target_root') != str(self.root):
            raise ValueError('invalid or moved legacy attachment receipt')
        self.a = validate(r['mapping'])
        if hashlib.sha256(dump(r['mapping']).encode()).hexdigest() != r['mapping_sha256']:
            raise ValueError('attachment mapping digest differs')
        self.receipt = r
        self.base = SealedReference.make(self.a['baseline'])
        self.overlay = SealedReference.make(Path(self.a['reference_delta'])/'overlay.sqlite3')
        from .reference_reader import DiskKeys
        class Keys(DiskKeys):
            def connect(self):
                return immutable(self.path)
        self.holds = Keys(Path(self.a['reference_delta'])/'observations.sqlite3', 'holds', where="reason!='acl_relaxation_held'")
        self.covered = Keys(self.overlay.path, extra=self.holds)

    def get(self, collection, point_id):
        key = (collection, dump(point_id))
        if key in self.holds:
            raise Fault('SOURCE_MISSING', 'point absent in newer captured observation')
        return (self.overlay if key in self.covered else self.base).get(collection, point_id)

    def search_resources(self, query, limit):
        from .reference_reader import search_resources
        return search_resources(self, query, limit)

    def evidence(self, kind, key):
        from .reference_reader import iter_items, EVIDENCE_SQL
        return list(iter_items(self, EVIDENCE_SQL, (kind, key), 10))

    def query(self, vector, limit):
        return self.base.query(vector, limit)

    def raw(self, view, collection, key):
        path = self.a['baseline'] if view == 'baseline' else Path(self.a['reference_delta'])/'overlay.sqlite3'
        with closing(immutable(path)) as c:
            row = c.execute('select raw,sha256,entity_type,target,quarantine from points where collection=? and id=?', (collection, key)).fetchone()
        if row is None:
            raise Fault('NOT_FOUND', 'captured legacy point not found')
        raw = zlib.decompress(row[0])
        if hashlib.sha256(raw).hexdigest() != row[1] or dump(json.loads(raw)['id']) != key:
            raise Fault('ATTACHMENT_INTEGRITY', 'raw captured point differs')
        return {'point': json.loads(raw), 'sha256': row[1], 'collection': collection,
                'id_json': key, 'view': view, 'entity_type_hint': row[2],
                'target_hint': row[3], 'quarantine': json.loads(row[4]),
                'source_file': str(path), 'canonical': False, 'source_consistency': CONSISTENCY}

    def call(self, op, p):
        if op == 'attachment.status':
            return self.receipt
        if op == 'attachment.get':
            return self.raw(p['view'], p['collection'], p['id_json'])
        path = self.a['baseline'] if p['view'] == 'baseline' else Path(self.a['reference_delta'])/'overlay.sqlite3'
        with closing(immutable(path)) as c:
            keys = c.execute('select id from points where collection=? and id>? order by id limit ?',
                             (p['collection'], p['after'], p['limit']+1)).fetchall()
        more = len(keys) > p['limit']
        items = [self.raw(p['view'], p['collection'], k[0]) for k in keys[:p['limit']]]
        return {'items': items, 'has_more': more, 'next_after': items[-1]['id_json'] if more else None}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['import'])
    p.add_argument('--mapping', required=True, type=Path)
    p.add_argument('--destination', required=True, type=Path)
    a = p.parse_args()
    os.umask(0o077)
    result = import_attachment(json.loads(a.mapping.read_text()), a.destination)
    print(dump({'target_root': result['target_root'], 'ledger_copy_sha256': result['ledger_copy_sha256'],
                'portable_self_contained': False, 'full_fidelity_migration_accepted': False}))


if __name__ == '__main__':
    main()
