"""Explicit dev20 canonical capture into ONE new, quarantined standalone root.

Plan is private operator input, not an editable same-version backup manifest.
No service control, remote requests, Qdrant loading or admission is performed.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

from . import state_backup as sb
from .repository import dump

FORMAT = 'atlas-canonical-adoption-plan-v1'
KIND = 'canonical-dev20-adoption-v1'
PAYLOAD = {'corpus', 'artifacts', 'media', 'attachment-owner.json',
           'connection-binding.json', 'connection-bindings.json', 'legacy-attachment.json'}
LOCKS = {'service.lock', 'admin.lock', 'acquisition.lock'}
SIDECARS = {'registry.sqlite3-wal', 'registry.sqlite3-shm', 'registry.sqlite3-journal'}
SUPPORTED = {'atlas': '0.3.0.dev20', 'qdrant-client': '1.20.0'}


def runtime(python):
    # Absolute launcher identity retained; never resolve a venv Python symlink.
    python = Path(python).absolute()
    code = '''import sys,json,hashlib,sqlite3,importlib.metadata as m
from pathlib import Path
import atlas_engine
print(json.dumps({'atlas':m.version('atlas-engine'),'qdrant-client':m.version('qdrant-client'),
 'python':list(sys.version_info[:2]),'sqlite':sqlite3.sqlite_version,
 'source_modules':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(atlas_engine.__file__).parent.glob('*.py'))}}))'''
    return json.loads(subprocess.check_output([str(python), '-I', '-B', '-c', code], timeout=30))


def schema(db):
    return [list(r) for r in db.execute(
        "select type,name,tbl_name,sql from sqlite_master order by type,name")]


def ledger(db):
    """Every table, every column, including sqlite_sequence and unknown extensions."""
    result = {}
    for (name,) in db.execute("select name from sqlite_master where type='table' order by name"):
        q = '"' + name.replace('"', '""') + '"'
        def encode(row):
            return dump([{'blob_hex': v.hex()} if isinstance(v, bytes) else v for v in row])
        rows = sorted(encode(r) for r in db.execute('select * from ' + q))
        h = hashlib.sha256()
        for row in rows:
            h.update(row.encode() + b'\n')
        result[name] = {'rows': len(rows), 'sha256': h.hexdigest()}
    return {'schema': schema(db), 'tables': result}


def connection(path, closed=False):
    # Immutable only for proven closed SQLite without committed WAL/journal.
    suffixes = ('-wal', '-journal')
    clean = not any(Path(str(path)+s).exists() and Path(str(path)+s).stat().st_size for s in suffixes)
    return sqlite3.connect(path.as_uri() + ('?immutable=1' if closed and clean else '?mode=ro'), uri=True)


def raw_collections(root):
    corpus = root / 'corpus'
    if json.loads((corpus/'corpus.json').read_text()) != {'format': sb.FORMAT, 'backend': 'qdrant-local'}:
        raise ValueError('canonical corpus format required')
    meta = json.loads((corpus/'qdrant/meta.json').read_text())
    if set(meta['collections']) != set(sb.COLLECTIONS):
        raise ValueError('exact canonical collection set required')
    result = {}
    for name in sb.COLLECTIONS:
        p = corpus/'qdrant/collection'/name/'storage.sqlite'
        for suffix in ('-wal', '-journal'):
            s = Path(str(p)+suffix)
            if s.exists() and s.stat().st_size:
                raise ValueError('Qdrant must be closed and checkpointed')
        with closing(connection(p, closed=True)) as db:
            if db.execute('pragma integrity_check').fetchall() != [('ok',)]:
                raise ValueError('Qdrant SQLite integrity failure')
            result[name] = ledger(db)
    return result


def pin(path):
    p = sb.safe_path(path)
    if not p.is_file():
        raise ValueError('external dependency must be a regular file')
    return {'path': str(p), 'bytes': p.stat().st_size, 'sha256': sb.sha(p)}


def dependency_pins(attachment, explicit):
    """Retain existing references, not a second copy of the historical mirror.

    Follow only attachment lineage and its declared baseline/delta/overlay/ledger
    dependencies. Configs are hashed as opaque bytes, never parsed or published.
    """
    paths = set(map(lambda p: str(sb.safe_path(p)), explicit))
    seen = set()
    expected = {}
    def declared(info):
        p = sb.safe_path(info['path'])
        paths.add(str(p))
        if str(p) in expected and expected[str(p)] != info['sha256']:
            raise ValueError('conflicting attachment dependency hash')
        expected[str(p)] = info['sha256']
        return p
    def walk(path):
        path = sb.safe_path(path)
        if str(path) in seen:
            return
        if len(seen) >= 64:
            raise ValueError('attachment lineage depth exceeded')
        seen.add(str(path)); paths.add(str(path))
        a = json.loads(path.read_text())
        if not a.get('complete'):
            raise ValueError('incomplete external attachment')
        for key in ('baseline', 'knowledge_ledger', 'reference_delta'):
            if a.get(key):
                p = sb.safe_path(a[key])
                if p.is_dir():
                    for item in p.rglob('*'):
                        item = sb.safe_path(item)
                        if not item.is_dir():
                            paths.add(str(item))
                else:
                    paths.add(str(p))
        for key, value in a.items():
            if key.endswith('_sha256') and key[:-7] in a:
                declared({'path': a[key[:-7]], 'sha256': value})
        for value in a.get('readbacks', []):
            paths.add(str(sb.safe_path(value)))
        if a.get('reference_delta'):
            delta = sb.safe_path(a['reference_delta'])
            r = json.loads((delta/'receipt.json').read_text())
            for key, name in (('overlay_sha256', 'overlay.sqlite3'),
                              ('observation_sha256', 'observations.sqlite3')):
                declared({'path': str(delta/name), 'sha256': r[key]})
        for key in ('overlay', 'seal', 'receipt'):
            if a.get(key):
                declared(a[key])
        if a.get('seal'):
            seal = json.loads(sb.safe_path(a['seal']['path']).read_text())
            for info in seal['capture_receipts']:
                declared(info)
            for entry in seal['knowledge']:
                manifest_path = declared(entry['manifest'])
                manifest = json.loads(manifest_path.read_text())
                for h, info in manifest['sources'].items():
                    if len(h) != 64 or any(c not in '0123456789abcdef' for c in h):
                        raise ValueError('invalid source archive digest')
                    p = declared({'path': str(sb.safe_path(entry['archive'])/h), 'sha256': h})
                    if p.stat().st_size != info['bytes']:
                        raise ValueError('source archive size mismatch')
        if a.get('parent'):
            parent = a['parent']
            declared(parent)
            walk(parent['path'])
    if attachment:
        walk(attachment)
    pins = [pin(p) for p in sorted(paths)]
    if any(p['sha256'] != expected[p['path']] for p in pins if p['path'] in expected):
        raise ValueError('attachment dependency hash mismatch')
    return pins


def check_pins(pins):
    if not isinstance(pins, list) or not pins:
        raise ValueError('explicit dependency pins required')
    for p in pins:
        if pin(p['path']) != p:
            raise ValueError('external dependency drift')


def plan(source, source_python, output, external, attachment=None, native_binding=None):
    source = sb.safe_path(source)
    versions = runtime(source_python)
    if any(versions[k] != v for k, v in SUPPORTED.items()):
        raise ValueError('unsupported source runtime; expected dev20 / Qdrant 1.20.0')
    with closing(connection(source/'registry.sqlite3')) as db:
        source_schema = schema(db)
    owner = source/'attachment-owner.json'
    if owner.exists():
        marker = json.loads(owner.read_text())
        if not attachment or marker['attachment'] != {'path': str(sb.safe_path(attachment)), 'sha256': sb.sha(Path(attachment))}:
            raise ValueError('source attachment owner must be explicitly bound')
    if native_binding:
        from .config import Connection
        native_binding = Connection.model_validate(native_binding).model_dump(mode='json')
        if native_binding['enable_writes']:
            raise ValueError('adoption binding cannot enable native writes')
    result = {'format': FORMAT, 'source_root': str(source),
              'source_python': str(Path(source_python).absolute()), 'source_versions': versions,
              'target_versions': sb.versions(), 'source_schema': source_schema,
              'owner': pin(owner) if owner.exists() else None,
              'attachment': str(sb.safe_path(attachment)) if attachment else None,
              'native_binding': native_binding,
              'external_dependencies': dependency_pins(attachment, external),
              'admission': 'quarantine; no replay, cancellation, workers or native effects'}
    sb.write_json(Path(output), result)
    return result


def validate_plan(path):
    p = json.loads(sb.safe_path(path).read_text())
    if p['format'] != FORMAT or p['target_versions'] != sb.versions():
        raise ValueError('adoption plan target runtime/source mismatch')
    if any(p['source_versions'].get(k) != v for k, v in SUPPORTED.items()):
        raise ValueError('unsupported source runtime')
    if runtime(p['source_python']) != p['source_versions']:
        raise ValueError('source runtime drift')
    check_pins(p['external_dependencies'])
    return p


def source_inventory(root):
    inventory = sb.files(root)
    # Reading SQLite may create SHM or an empty WAL. Only those incidental
    # files are exempt; never exempt a nonempty WAL or a rollback journal.
    return {k:v for k,v in inventory.items()
            if k != 'registry.sqlite3-shm' and not (k == 'registry.sqlite3-wal' and v['bytes'] == 0)}


def capture(plan_path, destination, reserve_bytes=2*1024**3, after_copy=None, expected_plan_sha256=None):
    if expected_plan_sha256 is not None and sb.sha(sb.safe_path(plan_path)) != expected_plan_sha256:
        raise ValueError('operator-approved plan checksum mismatch')
    p = validate_plan(plan_path)
    source, destination = sb.new_target(p['source_root'], destination)
    with sb.offline(source):
        before = source_inventory(source)  # Before first source SQLite read.
        entries = {v.name for v in source.iterdir()}
        if entries - PAYLOAD - LOCKS - SIDECARS - {'registry.sqlite3', sb.MARKER}:
            raise ValueError('unreviewed root entry; capture refused')
        if p['owner'] and pin(source/'attachment-owner.json') != p['owner']:
            raise ValueError('attachment owner drift')
        for rel in before:
            if (source/rel).stat().st_nlink != 1:
                raise ValueError('hardlinked mutable source refused')
        collections = raw_collections(source)
        # One copy, measured at the fence; backup registry can include WAL pages.
        size = sum(v['bytes'] for k,v in before.items() if k not in LOCKS)
        free = shutil.disk_usage(destination.parent).free
        if free < size + reserve_bytes + 64*1024**2:
            raise ValueError('insufficient space for one capture plus reserve')
        with closing(connection(source/'registry.sqlite3', closed=True)) as db:
            db.execute('BEGIN')
            if schema(db) != p['source_schema']:
                raise ValueError('source schema drift; prepare a new reviewed plan')
            if db.execute('pragma integrity_check').fetchall() != [('ok',)]:
                raise ValueError('registry integrity failure')
            expected = ledger(db)
            previous = json.loads((source/sb.MARKER).read_text()) if (source/sb.MARKER).exists() else None
            state = {'format': 'atlas-restored-state-v1', 'reconciled': False,
                     'migration_kind': KIND, 'preimage_verified': False,
                     'backup_manifest_sha256': sb.sha(Path(plan_path)),
                     'storage_source_roots': sorted(set([str(source)] + (previous or {}).get('storage_source_roots', []))),
                     'storage_target_root': str(destination), 'previous_restore': previous,
                     'adoption_plan_sha256': sb.sha(Path(plan_path))}
            destination.mkdir(mode=0o700)
            sb.write_json(destination/sb.MARKER, state); sb.sync_tree(destination)
            for name in sorted(entries & PAYLOAD):
                src = source/name; dest = destination/name
                if src.is_dir():
                    shutil.copytree(src, dest)
                else:
                    shutil.copyfile(src, dest)
                if after_copy:
                    after_copy(name)
            with closing(sqlite3.connect(destination/'registry.sqlite3')) as target:
                db.backup(target)
                target.execute('pragma journal_mode=DELETE')
                if ledger(target) != expected or target.execute('pragma integrity_check').fetchall() != [('ok',)]:
                    raise ValueError('registry preimage mismatch')
            if after_copy:
                after_copy('registry.sqlite3')
        actual = sb.files(destination)
        expected_files = {k:v for k,v in before.items() if k.split('/')[0] in PAYLOAD}
        if {k:v for k,v in actual.items() if k not in (sb.MARKER, 'registry.sqlite3')} != expected_files:
            raise ValueError('captured file preimage differs')
        if raw_collections(destination) != collections:
            raise ValueError('raw collection preimage differs')
        if source_inventory(source) != before:
            raise ValueError('source changed across capture')
        check_pins(p['external_dependencies'])
        # Persist the private receipt BEFORE publishing the completed marker.
        receipt = {'format': 'atlas-canonical-adoption-receipt-v1', 'plan': p,
                   'plan_sha256': sb.sha(Path(plan_path)), 'registry': expected,
                   'collections': collections, 'source_inventory': before,
                   'preimage_files': actual, 'source_unchanged': True,
                   'capacity': {'free_before': free, 'copy_budget': size, 'reserve': reserve_bytes},
                   'schema_migration': 'none during capture; additive scoped_observations on first standalone open',
                   'production_acceptance': False}
        sb.write_json(destination/'adoption-receipt.json', receipt)
        (destination/'service.lock').touch(mode=0o600)
        sb.sync_tree(destination)
        state['preimage_verified'] = True
        state['adoption_receipt_sha256'] = sb.sha(destination/'adoption-receipt.json')
        temp = destination/'restore-state.new'
        sb.write_json(temp, state); temp.replace(destination/sb.MARKER)
        sb.sync_tree(destination)
        return receipt


def verify(root):
    root = sb.safe_path(root)
    state = sb.restore_state(root)
    if not state or state.get('migration_kind') != KIND:
        raise ValueError('canonical adoption root required')
    if sb.sha(root/'adoption-receipt.json') != state['adoption_receipt_sha256']:
        raise ValueError('adoption receipt drift')
    receipt = json.loads((root/'adoption-receipt.json').read_text())
    with closing(connection(root/'registry.sqlite3', closed=True)) as db:
        if ledger(db) != receipt['registry']:
            raise ValueError('registry no longer at captured preimage')
    expected = {k:v for k,v in receipt['preimage_files'].items() if k != sb.MARKER}
    actual = {k:v for k,v in sb.files(root).items() if k not in (sb.MARKER, 'service.lock', 'adoption-receipt.json')}
    if actual != expected or raw_collections(root) != receipt['collections']:
        raise ValueError('adoption preimage drift')
    check_pins(receipt['plan']['external_dependencies'])
    return {'verified': True, 'quarantined': not state['reconciled'], 'tables': receipt['registry']['tables']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    prep = sub.add_parser('plan')
    prep.add_argument('source', type=Path); prep.add_argument('output', type=Path)
    prep.add_argument('--source-python', type=Path, required=True)
    prep.add_argument('--external', type=Path, action='append', required=True)
    prep.add_argument('--attachment', type=Path)
    prep.add_argument('--native-binding', type=Path)
    cap = sub.add_parser('capture'); cap.add_argument('plan', type=Path); cap.add_argument('destination', type=Path)
    cap.add_argument('--plan-sha256', required=True)
    ver = sub.add_parser('verify'); ver.add_argument('root', type=Path)
    a = parser.parse_args(); os.umask(0o077)
    try:
        if a.action == 'plan':
            plan(a.source, a.source_python, a.output, a.external, a.attachment,
                 json.loads(a.native_binding.read_text()) if a.native_binding else None)
            print(dump({'prepared': True, 'capture_performed': False, 'plan_sha256': sb.sha(a.output)}))
        elif a.action == 'capture':
            capture(a.plan, a.destination, expected_plan_sha256=a.plan_sha256)
            print(dump({'captured': True, 'quarantined': True, 'production_acceptance': False}))
        else:
            print(dump(verify(a.root)))
    except (ValueError, OSError, KeyError, sqlite3.Error, subprocess.SubprocessError) as e:
        parser.exit(1, 'Atlas adoption refused: ' + str(e) + '\n')


if __name__ == '__main__':
    main()
