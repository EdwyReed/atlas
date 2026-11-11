"""Assemble an isolated, literal-only destination from pinned OFFLINE inputs.

No capture, approval, service control, network, or production activation. A fence
pins bytes, not the historical accuracy or authorization of a supplied capture.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .full_migration import FrozenSource, ingest_frozen
from .knowledge import Knowledge
from .materialize import materialize
from .reference_mirror import build_index
from .repository import Repository, digest, dump

VERSION = 'atlas-final-delta-v1'


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def code_pin():
    # Pin the whole implementation, including conversion and ACL consumers.
    return digest({p.name: sha256(p) for p in sorted(Path(__file__).parent.glob('*.py'))})


def save(path, value):
    path = Path(path)
    pending = path.with_suffix('.pending')
    with pending.open('w') as stream:
        os.chmod(pending, 0o600)
        stream.write(json.dumps(value, ensure_ascii=False, indent=2))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def pinned_file(item):
    if set(item) != {'path', 'sha256'} or not re.fullmatch('[0-9a-f]{64}', item['sha256']):
        raise ValueError('explicit path and lowercase SHA-256 required')
    path = Path(item['path'])
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError('absolute regular non-symlink input required')
    if sha256(path) != item['sha256']:
        raise ValueError('source content fence drift: ' + str(path))
    return path.resolve()


def load_fence(path, expected):
    path = pinned_file({'path': str(Path(path).absolute()), 'sha256': expected})
    fence = json.loads(path.read_text())
    if set(fence) != {'format', 'scope', 'source_consistency', 'export', 'deltas', 'capture_receipts'}:
        raise ValueError('complete explicit fence contract required')
    if fence['format'] != VERSION or fence['scope'] != 'offline-isolated-only':
        raise ValueError('only offline isolated assembly is supported')
    if not isinstance(fence['source_consistency'], str) or not fence['source_consistency']:
        raise ValueError('preserve source consistency qualification explicitly')
    export = pinned_file(fence['export'])
    # A main-file hash must not ignore committed SQLite WAL state.
    for suffix in ('-wal', '-journal'):
        sidecar = Path(str(export) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError('export is not sealed: nonempty SQLite sidecar')
    with_source = FrozenSource(export)
    with_source.close()  # validates every collection before any destination opens
    paths = [path, export]
    manifests = []
    if not isinstance(fence['deltas'], list) or not fence['deltas']:
        raise ValueError('ordered delta inputs required')
    for entry in fence['deltas']:
        if set(entry) != {'manifest', 'archive'}:
            raise ValueError('delta requires pinned manifest and archive')
        manifest_path = pinned_file(entry['manifest'])
        archive = Path(entry['archive'])
        if not archive.is_absolute() or archive.is_symlink() or not archive.is_dir():
            raise ValueError('absolute non-symlink archive required')
        manifest = json.loads(manifest_path.read_text())
        # Knowledge.ingest validates every content-addressed object and record.
        manifests.append((manifest, archive))
        paths.extend([manifest_path, archive.resolve()])
    for capture in fence['capture_receipts']:
        paths.append(pinned_file(capture))
    return fence, export, manifests, paths


def verify_knowledge(knowledge, manifests):
    expected = {}
    heads = {}
    for manifest, _ in manifests:
        for record in manifest['records']:
            key = (record['entity_id'], record['revision'])
            if key in expected and expected[key] != record:
                raise ValueError('conflicting input revision')
            expected[key] = record
            if record['revision'] > heads.get(record['entity_id'], {}).get('revision', 0):
                heads[record['entity_id']] = record
    with knowledge.repo.connect() as c:
        rows = c.execute('select entity_id,revision,hash,record from knowledge_revisions').fetchall()
        actual = {(r['entity_id'], r['revision']): json.loads(r['record']) for r in rows}
        if actual != expected or any(r['hash'] != digest(actual[r['entity_id'], r['revision']]) for r in rows):
            raise ValueError('destination revision drift')
        actual_heads = {r['entity_id']: (r['revision'], r['hash']) for r in c.execute('select * from knowledge_heads')}
        if actual_heads != {k: (v['revision'], digest(v)) for k, v in heads.items()}:
            raise ValueError('destination head drift')
    return {'revisions': len(expected), 'heads': len(heads), 'ordered_records_sha256': digest([expected[k] for k in sorted(expected)])}


def run(fence_path, fence_sha256, destination, after_stage=None):
    destination = Path(destination).absolute()
    if destination.is_symlink():
        raise ValueError('destination symlink refused')
    destination = destination.resolve()
    fence, export, manifests, paths = load_fence(fence_path, fence_sha256)
    for source in paths:
        if destination == source or destination in source.parents or source in destination.parents:
            raise ValueError('destination overlaps source')
    # Whole-chain preflight catches conflict/stale/ACL/lineage before destination.
    # TMPDIR is the configured private scratch, never a live registry.
    with tempfile.TemporaryDirectory(prefix='atlas-final-delta-') as temporary:
        check = Knowledge(Repository(Path(temporary)))
        for manifest, archive in manifests:
            check.ingest(manifest, archive)
        expected = verify_knowledge(check, manifests)
    binding = {'version': VERSION, 'fence_sha256': fence_sha256, 'code_sha256': code_pin(), 'destination': str(destination)}
    if not destination.exists():
        destination.mkdir(mode=0o700, parents=True)
    marker = destination / '.final-delta-owner.json'
    # Refuse ANY pre-existing non-owned root (including canary and exports).
    if not marker.exists():
        if list(destination.iterdir()):
            raise ValueError('destination must be new or owned by this exact fence')
        save(marker, binding)
    if json.loads(marker.read_text()) != binding:
        raise ValueError('resume fence/code/destination mismatch')
    # An ownership marker is not permission to follow swapped links into a live
    # store. Refuse linked files before opening any mutable destination handle.
    for path in destination.rglob('*'):
        if path.is_symlink() or (path.is_file() and path.stat().st_nlink != 1):
            raise ValueError('linked destination entry refused')
    runtime = destination / 'runtime'
    runtime.mkdir(mode=0o700, exist_ok=True)
    with (destination / '.final-delta.lock').open('a') as lock, (runtime / 'service.lock').open('a') as service_lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Same lock used by the real server: never assemble into a running reader.
        fcntl.flock(service_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        registry = runtime / 'registry.sqlite3'
        if registry.exists():
            import sqlite3
            with sqlite3.connect('file:' + str(registry) + '?mode=ro', uri=True) as db:
                if db.execute('select count(*) from jobs').fetchone()[0]:
                    raise ValueError('activated runtime with jobs cannot be a migration destination')
        receipt = destination / 'receipt.json'
        # A failed retry must not leave a previous success looking current.
        save(receipt, {**binding, 'complete': False})
        def boundary(stage):
            if after_stage:
                after_stage(stage)
            load_fence(fence_path, fence_sha256)
            # Revalidate archive bytes as well as manifest/export pins.
            for manifest, archive in manifests:
                for name, info in manifest['sources'].items():
                    p = pinned_file({'path': str(archive / name), 'sha256': name})
                    if p.stat().st_size != info['bytes']:
                        raise ValueError('source object length drift')
        boundary('preflight')
        raw = ingest_frozen(export, destination / 'reference.sqlite3', destination / 'raw.json')
        boundary('raw')
        typed = materialize(destination / 'reference.sqlite3', destination / 'typed.json')
        boundary('typed')
        index = build_index(destination / 'reference.sqlite3', destination / 'index.json')
        knowledge = Knowledge(Repository(destination / 'runtime'))
        imports = [knowledge.ingest(manifest, archive) for manifest, archive in manifests]
        actual = verify_knowledge(knowledge, manifests)
        if actual != expected:
            raise ValueError('combined lineage readback differs')
        boundary('knowledge')
        result = {**binding, 'complete': True, 'source_consistency': fence['source_consistency'],
                  'source_capture_certified': False, 'production_ready': False,
                  'capture_receipts': fence['capture_receipts'], 'raw': raw, 'typed': typed,
                  'index': index, 'knowledge': actual, 'imports_this_run': imports,
                  'reference_mirror': str(destination / 'reference.sqlite3'),
                  'runtime_root': str(destination / 'runtime'), 'network_calls': 0,
                  'semantic_enabled': False, 'gpu_submissions': 0}
        save(receipt, result)
        return result


def main():
    import sys
    # Keep the original isolated assembler contract; additive mode is explicit.
    if len(sys.argv)>1 and sys.argv[1]=='integrate':
        from .sealed_delta import main as integrate_main
        del sys.argv[1]
        return integrate_main()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fence', required=True, type=Path)
    parser.add_argument('--fence-sha256', required=True)
    parser.add_argument('--destination', required=True, type=Path)
    args = parser.parse_args()
    print(dump(run(args.fence, args.fence_sha256, args.destination)))


if __name__ == '__main__':
    main()
