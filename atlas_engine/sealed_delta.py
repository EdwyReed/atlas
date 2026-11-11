"""Additive offline attachment integration. Byte seals are not writer-fence claims.

Publish immutable layers by atomic attachment rename, never rebuild the baseline.
A private small-ledger backup stages ordered domain imports; the parent survives.
Source tombstones suppress old identities without deleting historical bytes.
"""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import zlib

from .contracts import Fault
from .final_delta import code_pin, pinned_file, save, sha256
from .full_migration import Mirror
from .knowledge import Knowledge
from .materialize import materialize
from .reference_mirror import ReferenceMirror, build_index
from .repository import Repository, digest, dump
from .scoped_delta import ScopedReferenceMirror
from .stream_delta import StreamReferenceMirror
from .reference_reader import DiskKeys, iter_items, search_resources

FORMAT = 'atlas-sealed-delta-v1'
ATTACHMENT = 'atlas-sealed-attachment-v1'


def plain(path):
    p = Path(path).absolute()
    if any(q.is_symlink() for q in (p, *p.parents)):
        raise ValueError('linked path refused')
    return p


def sealed(item):
    p = plain(item['path'])
    p = pinned_file({'path': str(p), 'sha256': item['sha256']})
    for suffix in ('-wal', '-journal'):
        s = Path(str(p) + suffix)
        if s.exists() and s.stat().st_size:
            raise ValueError('nonempty SQLite sidecar: input not sealed')
    return p


def pin(path):
    return {'path': str(plain(path)), 'sha256': sha256(path)}


def verify_inputs(seal):
    for r in seal['capture_receipts']:
        sealed(r)
    for entry in seal['knowledge']:
        manifest = json.loads(sealed(entry['manifest']).read_text())
        archive = plain(entry['archive'])
        for h, info in manifest['sources'].items():
            if not re.fullmatch('[0-9a-f]{64}', h) or sealed({'path': str(archive / h), 'sha256': h}).stat().st_size != info['bytes']:
                raise ValueError('archive drift')


def load_attachment(path, expected=None, seen=None):
    path = plain(path)
    if expected is not None:
        sealed({'path': str(path), 'sha256': expected})
    seen = set() if seen is None else seen
    if path in seen or len(seen) >= 64:
        raise ValueError('attachment cycle/depth limit')
    seen.add(path)
    a = json.loads(path.read_text())
    if not a.get('complete'):
        raise ValueError('incomplete attachment')
    sealed({'path': a['knowledge_ledger'], 'sha256': a['knowledge_ledger_sha256']})
    if a['format'] == ATTACHMENT:
        load_attachment(a['parent']['path'], a['parent']['sha256'], seen)
        sealed(a['overlay'])
        verify_inputs(json.loads(sealed(a['seal']).read_text()))
        r = json.loads(sealed(a['receipt']).read_text())
        if not r['complete'] or r['parent'] != a['parent'] or r['seal'] != a['seal']:
            raise ValueError('attachment receipt mismatch')
    elif a['format'] == 'atlas-checkpoint-attachment-v1':
        if sha256(Path(a['reference_delta']) / 'receipt.json') != a['reference_delta_receipt_sha256']:
            raise ValueError('checkpoint reference receipt drift')
    else:
        raise ValueError('unknown attachment format')
    return a


def reference(path, expected=None):
    a = load_attachment(path, expected)
    if a['format'] == ATTACHMENT:
        return SealedReferenceMirror(a)
    return StreamReferenceMirror(a['baseline'], a['reference_delta'])


def layers(reader):
    if hasattr(reader, 'overlay'):
        yield reader.overlay, reader.holds
        yield from layers(reader.base)
    else:
        yield reader, set()


def raw_point(reader, collection, point_id):
    key = dump(point_id)
    for layer, _ in layers(reader):
        with contextlib.closing(layer.connect()) as c:
            row = c.execute('select raw,sha256 from points where collection=? and id=?', (collection, key)).fetchone()
        if row:
            p = json.loads(zlib.decompress(row[0]))
            if digest(p) != row[1]:
                raise ValueError('raw parent point corrupt')
            return p
    return None


def held(reader, key):
    if not hasattr(reader, 'overlay'):
        return False
    if key in reader.holds:
        return True
    if key in reader.covered:
        return False
    return held(reader.base, key)


class SealedReferenceMirror:
    semantic_enabled = False

    def __init__(self, attachment):
        a = attachment
        self.base = reference(a['parent']['path'], a['parent']['sha256'])
        self.overlay = ReferenceMirror(a['overlay']['path'])
        seal = json.loads(sealed(a['seal']).read_text())
        self.holds = {(op['collection'],dump(op['id'])) for op in seal['operations'] if op['action'] in ('tombstone','hold')}
        self.covered = DiskKeys(self.overlay.path, extra=self.holds)

    def get(self, collection, point_id):
        key = (collection, dump(point_id))
        if key in self.holds:
            raise Fault('SOURCE_UNRESOLVED', 'explicit sealed source tombstone/hold; historical bytes retained')
        return (self.overlay if key in self.covered else self.base).get(collection, point_id)

    def items(self, sql, args=(), limit=None):
        return list(iter_items(self, sql, args, limit))

    def search_resources(self, query, limit):
        return search_resources(self, query, limit)

    def resources(self):
        return self.items("select collection,id,payload from entities where entity_type='resource' order by collection,id")

    def evidence(self, kind, key):
        return self.items('select e.collection,e.id,e.payload from reference_keys k join entities e using(collection,id) where k.kind=? and k.key=? order by e.collection,e.id', (kind, key), 10)

    def query(self, vector, limit):
        return self.base.query(vector, limit)


def integrate(attachment, attachment_sha256, seal_path, seal_sha256, destination, after_stage=None):
    attachment = plain(attachment)
    seal_path = sealed({'path': str(plain(seal_path)), 'sha256': seal_sha256})
    a = load_attachment(attachment, attachment_sha256)
    seal = json.loads(seal_path.read_text())
    if set(seal) != {'format', 'parent', 'source_consistency', 'capture_receipts', 'operations', 'knowledge'} or seal['format'] != FORMAT:
        raise ValueError('explicit sealed delta contract required')
    parent = {'path': str(attachment), 'sha256': attachment_sha256}
    if seal['parent'] != parent or not isinstance(seal['source_consistency'], str) or not seal['source_consistency']:
        raise ValueError('parent lineage/source qualification required')
    if not isinstance(seal['operations'], list) or not isinstance(seal['knowledge'], list) or not seal['capture_receipts']:
        raise ValueError('explicit operations/imports and capture receipts required')
    inputs = [attachment, seal_path, plain(a['knowledge_ledger'])]
    for r in seal['capture_receipts']:
        inputs.append(sealed(r))
    imports = []
    for entry in seal['knowledge']:
        if set(entry) != {'manifest', 'archive'}:
            raise ValueError('manifest/archive required')
        mp = sealed(entry['manifest']); archive = plain(entry['archive'])
        if not archive.is_dir():
            raise ValueError('source archive missing')
        manifest = json.loads(mp.read_text())
        inputs.extend([mp, archive]); imports.append((manifest, archive))
    destination = plain(destination)
    for p in inputs:
        if p == destination or p in destination.parents or destination in p.parents:
            raise ValueError('destination overlaps an input')
    binding = {'format': ATTACHMENT, 'parent': parent, 'seal': pin(seal_path), 'code_sha256': code_pin(), 'destination': str(destination)}
    # Everything below remains private until attachment.json is atomically published.
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    for p in destination.rglob('*'):
        if p.is_symlink() or (p.is_file() and p.stat().st_nlink != 1):
            raise ValueError('linked destination refused')
    owner = destination / 'owner.json'
    if not owner.exists():
        if list(destination.iterdir()):
            raise ValueError('new or exact owned destination required')
        save(owner, binding)
    if json.loads(owner.read_text()) != binding:
        raise ValueError('resume binding mismatch')
    ledger = destination / 'ledger'
    ledger.mkdir(mode=0o700, exist_ok=True)
    with (destination / 'import.lock').open('a') as lock, (Path(a['knowledge_ledger']).parent / 'service.lock').open('a') as parent_lock, (ledger / 'service.lock').open('a') as service_lock:
        for handle in (lock, parent_lock, service_lock):
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        output = destination / 'attachment.json'
        if output.exists():
            result = load_attachment(output)
            if result['parent'] != parent or result['seal'] != pin(seal_path):
                raise ValueError('published attachment binding drift')
            # Replay must validate CAS, not just trust an old success file.
            for manifest, archive in imports:
                for h, info in manifest['sources'].items():
                    if not re.fullmatch('[0-9a-f]{64}', h) or sealed({'path': str(archive / h), 'sha256': h}).stat().st_size != info['bytes']:
                        raise ValueError('archive drift')
            return result
        reader = reference(attachment, attachment_sha256)
        schemas = {}
        bottom = list(layers(reader))[-1][0]
        with contextlib.closing(bottom.connect()) as db:
            schemas = {n: json.loads(s) for n, s in db.execute('select name,schema from collections')}
        seen = set(); holds = []; points = {}
        for op in seal['operations']:
            if set(op) != {'collection', 'id', 'before_sha256', 'action', 'point'} or op['action'] not in ('upsert', 'tombstone', 'hold'):
                raise ValueError('explicit delta operation required')
            n = op['collection']; key = (n, dump(op['id']))
            if key in seen or n not in schemas:
                raise ValueError('duplicate operation or unknown collection')
            seen.add(key)
            old = raw_point(reader, n, op['id'])
            if op['before_sha256'] != (digest(old) if old is not None else None):
                raise ValueError('point precondition/lineage mismatch')
            p = op['point']
            if op['action'] != 'upsert':
                if p is not None or old is None:
                    raise ValueError('tombstone/hold requires known source and null point')
                holds.append(list(key)); continue
            if not isinstance(p, dict) or p.get('id') != op['id'] or type(p.get('id')) is not type(op['id']):
                raise ValueError('point identity mismatch')
            points.setdefault(n, []).append(p)
        def boundary(stage):
            if after_stage:
                after_stage(stage)
            sealed({'path': str(seal_path), 'sha256': seal_sha256})
            load_attachment(attachment, attachment_sha256)
            verify_inputs(seal)
        boundary('preflight')
        target = ledger / 'registry.sqlite3'
        # Repeat backup after any interrupted attempt; never modify the parent.
        with contextlib.closing(sqlite3.connect(Path(a['knowledge_ledger']).as_uri() + '?mode=ro', uri=True)) as src, contextlib.closing(sqlite3.connect(target)) as dst:
            if src.execute('select count(*) from jobs').fetchone()[0]:
                raise ValueError('activated parent jobs refused')
            src.backup(dst)
        target.chmod(0o600)
        knowledge = Knowledge(Repository(ledger))
        imported = [knowledge.ingest(m, ar) for m, ar in imports]
        boundary('knowledge')
        mirror_path = destination / 'overlay.sqlite3'
        # Failed attempts replay exact immutable rows. No baseline materialization.
        mirror = Mirror(mirror_path)
        try:
            for n, ps in points.items():
                schema = schemas[n]; vectors = schema['vectors']
                if 'size' in vectors: vectors = {'unnamed': vectors}
                with mirror.db:
                    mirror.db.execute('insert or ignore into collections(name,schema,expected) values(?,?,?)', (n, dump(schema), len(ps)))
                mirror.ingest(n, vectors, ps, None, True)
                with mirror.db:
                    mirror.db.execute('update collections set after_schema=?,after_count=? where name=?', (dump(schema), len(ps), n))
            raw = mirror.verify()
        finally:
            mirror.close()
        materialize(mirror_path, destination / 'typed.json')
        build_index(mirror_path, destination / 'index.json')
        for dbpath in (target, mirror_path):
            with contextlib.closing(sqlite3.connect(dbpath)) as db:
                if db.execute('pragma integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('destination integrity')
                if db.execute('pragma wal_checkpoint(TRUNCATE)').fetchone()[0] != 0:
                    raise ValueError('destination WAL busy')
        boundary('verified')
        receipt = {**binding, 'complete': True, 'operations': len(seen), 'upserts': sum(map(len, points.values())), 'holds': len(holds), 'raw': raw, 'imports': imported, 'source_consistency': seal['source_consistency'], 'source_capture_certified': False, 'baseline_rebuilt': False, 'production_ready': False, 'live_cutover': False, 'source_writes': False, 'gpu_submissions': 0}
        save(destination / 'receipt.json', receipt)
        result = {**a, 'format': ATTACHMENT, 'complete': True, 'parent': parent, 'seal': pin(seal_path), 'receipt': pin(destination / 'receipt.json'), 'overlay': pin(mirror_path), 'holds': holds, 'knowledge_ledger': str(target), 'knowledge_ledger_sha256': sha256(target), 'source_consistency': seal['source_consistency'], 'production_ready': False, 'live_cutover': False}
        with contextlib.closing(sqlite3.connect(target)) as db:
            result['counts'] = {t: db.execute('select count(*) from ' + t).fetchone()[0] for t in ('knowledge_revisions','knowledge_heads','legacy_rows','jobs')}
        save(output, result)
        load_attachment(output)
        return result


def prepare_runtime(attachment_path, root):
    """Caller holds root/service.lock. Seed a separate mutable runtime once.

    Even read-only API serving writes health telemetry; never run Engine on the
    content-pinned attachment ledger. A new attachment needs a new runtime root.
    """
    attachment_path = plain(attachment_path)
    a = load_attachment(attachment_path)
    root = plain(root)
    source = plain(a['knowledge_ledger'])
    if root == source.parent or root in source.parents or source.parent in root.parents:
        raise ValueError('runtime must be separate from sealed ledger')
    for p in root.rglob('*'):
        if p.is_symlink() or (p.is_file() and p.stat().st_nlink != 1):
            raise ValueError('linked runtime refused')
    binding = {'format':'atlas-attachment-runtime-v1', 'attachment':pin(attachment_path), 'code_sha256':code_pin()}
    marker = root / 'attachment-owner.json'
    if marker.exists():
        if json.loads(marker.read_text()) != binding:
            raise ValueError('runtime attachment/code binding mismatch')
        return a
    if {p.name for p in root.iterdir()} - {'service.lock'}:
        raise ValueError('new runtime root required')
    from .staged_import import table_state
    with contextlib.closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as src, contextlib.closing(sqlite3.connect(root/'registry.sqlite3')) as dst:
        src.execute('BEGIN')
        src.backup(dst)
        if table_state(src) != table_state(dst):
            raise ValueError('runtime backup readback mismatch')
    (root/'registry.sqlite3').chmod(0o600)
    save(marker, binding)
    return a


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--attachment', required=True, type=Path)
    p.add_argument('--attachment-sha256', required=True)
    p.add_argument('--seal', required=True, type=Path)
    p.add_argument('--seal-sha256', required=True)
    p.add_argument('--destination', required=True, type=Path)
    a = p.parse_args(); os.umask(0o077)
    print(dump(integrate(a.attachment, a.attachment_sha256, a.seal, a.seal_sha256, a.destination)))


if __name__ == '__main__':
    main()
