"""Offline same-format backups. Never instantiate Qdrant against a source root."""
import argparse
from collections import Counter
from contextlib import contextmanager, ExitStack, closing
import fcntl
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import pickle
import shutil
import sqlite3
import stat
import sys

from .corpus import COLLECTIONS, FORMAT
from .repository import dump

INCLUDED = {'registry.sqlite3', 'corpus', 'artifacts', 'media',
            'connection-binding.json', 'restore-state.json'}
MARKER = 'restore-state.json'
READS = {'capabilities', 'status', 'jobs.get', 'jobs.list', 'jobs.page', 'jobs.events',
         'generation.get', 'batch.get', 'dream.get', 'admin.get', 'artifact.read',
         'artifact.list', 'upload.get', 'mask.get', 'censor.get',
         'corpus.get', 'corpus.page', 'corpus.history', 'corpus.import.get',
         'generation.records.get', 'generation.records.page', 'loras.get', 'loras.page'}
EFFECTS = {'generation', 'batch', 'native_workspace', 'native_admin', 'mask', 'censor',
           'acquisition', 'acquisition_batch', 'onboarding'}
TERMINAL = {'succeeded', 'partially_succeeded', 'failed', 'canceled'}


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def safe_path(value):
    p = Path(value)
    if '..' in p.parts:
        raise ValueError('path traversal refused')
    p = p.absolute()
    for part in [*reversed(p.parents), p]:
        if part.is_symlink():
            raise ValueError('symlink refused: ' + str(part))
    return p


def files(root):
    """Strict tree inventory, including a collision check on portable spellings."""
    result = {}
    folded = set()
    for p in sorted(root.rglob('*')):
        relative = p.relative_to(root).as_posix()
        mode = p.lstat().st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)) or p.is_symlink():
            raise ValueError('nonregular backup entry: ' + relative)
        if relative.casefold() in folded:
            raise ValueError('case-fold path collision')
        folded.add(relative.casefold())
        if p.is_file():
            result[relative] = {'bytes': p.stat().st_size, 'sha256': sha(p)}
    return result


@contextmanager
def offline(root):
    root = safe_path(root)
    # Validate lock files before opening; never replace an inode held by an owner.
    with ExitStack() as stack:
        for p in (root / 'service.lock', root / 'corpus' / 'owner.lock'):
            safe_path(p)
            if not p.is_file():
                raise ValueError('existing owner lock required: ' + str(p))
            handle = stack.enter_context(p.open('r'))
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('active Atlas owner; stop it before backup/reconciliation')
        yield root


def ro(path):
    return sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)


def integrity(path):
    with closing(ro(path)) as c:
        if c.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('SQLite integrity failure: ' + str(path))


def registry_digest(root):
    """Bind the entire committed ledger, including WAL, not just its main file."""
    h = hashlib.sha256()
    with closing(ro(Path(root) / 'registry.sqlite3')) as c:
        c.execute('BEGIN')
        for line in c.iterdump():
            h.update(line.encode() + b'\n')
    return h.hexdigest()


class PointReader(pickle.Unpickler):
    # Qdrant local stores PointStruct pickles. Do not execute arbitrary globals
    # merely to inventory an operator-supplied backup.
    def find_class(self, module, name):
        if (module, name) == ('qdrant_client.http.models.models', 'PointStruct'):
            from qdrant_client.models import PointStruct
            return PointStruct
        if (module, name) == ('builtins', 'set'):
            return set
        raise ValueError('unsupported Qdrant pickle global')


def points(root):
    marker = json.loads((root / 'corpus' / 'corpus.json').read_text())
    if marker != {'format': FORMAT, 'backend': 'qdrant-local'}:
        raise ValueError('unsupported corpus format')
    base = root / 'corpus' / 'qdrant'
    meta = json.loads((base / 'meta.json').read_text())
    if set(meta['collections']) != set(COLLECTIONS):
        raise ValueError('exactly five canonical collections required')
    result = {}
    for name in COLLECTIONS:
        path = base / 'collection' / name / 'storage.sqlite'
        for suffix in ('-wal', '-journal'):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists() and sidecar.stat().st_size:
                raise ValueError('Qdrant is not a clean closed checkpoint')
        integrity(path)
        h = hashlib.sha256(); kinds = Counter(); deleted = 0; count = 0
        with closing(ro(path)) as c:
            for key, raw in c.execute('select id,point from points order by id'):
                h.update(dump([key, hashlib.sha256(raw).hexdigest()]).encode() + b'\n')
                point = PointReader(io.BytesIO(raw)).load()
                kinds[point.payload.get('kind', '<unknown>')] += 1
                deleted += int(bool(point.payload.get('deleted')))
                count += 1
        result[name] = {'points': count, 'kinds': dict(kinds),
                        'deleted_records': deleted, 'raw_sha256': h.hexdigest()}
    return result


def versions():
    return {'atlas': importlib.metadata.version('atlas-engine'),
            'qdrant-client': importlib.metadata.version('qdrant-client'),
            'python': list(sys.version_info[:2]), 'sqlite': sqlite3.sqlite_version,
            'source_modules': {p.name: sha(p) for p in sorted(Path(__file__).parent.glob('*.py'))}}


def new_target(source, destination):
    source = safe_path(source); destination = safe_path(destination)
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError('overlapping source/destination refused')
    if destination.exists():
        raise ValueError('destination must be NEW, no overwrite')
    if not destination.parent.is_dir():
        raise ValueError('destination parent must exist')
    return source, destination


def write_json(path, value):
    with path.open('x', encoding='utf-8') as f:
        f.write(dump(value) + '\n'); f.flush(); os.fsync(f.fileno())
    path.chmod(0o600)


def sync_tree(root):
    for p in root.rglob('*'):
        if p.is_file():
            with p.open('rb') as f: os.fsync(f.fileno())
    for p in [*(p for p in root.rglob('*') if p.is_dir()), root, root.parent]:
        fd = os.open(p, os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)


def backup(source, destination):
    source, destination = new_target(source, destination)
    with offline(source):
        # Inventory selected trees before touching destination. Excluded paths
        # (including secrets and external symlinks) are named, never traversed.
        selected = sorted(INCLUDED & {p.name for p in source.iterdir()})
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = source / ('registry.sqlite3' + suffix)
            safe_path(sidecar)
            if sidecar.exists() and not sidecar.is_file():
                raise ValueError('nonregular SQLite sidecar')
        source_files = {}
        for name in selected:
            p = source / name; safe_path(p)
            if p.is_dir():
                source_files.update({name + '/' + k: v for k, v in files(p).items()})
            elif p.is_file():
                if name != 'registry.sqlite3':
                    source_files[name] = {'bytes': p.stat().st_size, 'sha256': sha(p)}
            else: raise ValueError('nonregular state entry')
        inventory = points(source)
        integrity(source / 'registry.sqlite3')
        destination.mkdir(mode=0o700)
        payload = destination / 'state'; payload.mkdir(mode=0o700)
        for name in selected:
            src = source / name; dest = payload / name
            if name == 'registry.sqlite3':
                with closing(ro(src)) as c, closing(sqlite3.connect(dest)) as target:
                    c.backup(target)
                    target.execute('PRAGMA journal_mode=DELETE')
                integrity(dest)
            elif src.is_dir(): shutil.copytree(src, dest)
            else: shutil.copyfile(src, dest)
        if points(payload) != inventory:
            raise ValueError('point preimage differs')
        copied_files = files(payload)
        if {k: v for k, v in copied_files.items() if k != 'registry.sqlite3'} != source_files:
            raise ValueError('copied state file bytes differ from source')
        manifest = {'format': 'atlas-offline-backup-v1', 'versions': versions(),
                    'source_root': str(source), 'corpus_format': FORMAT,
                    'consistency': 'service.lock + corpus/owner.lock; stopped single owner',
                    'included': selected, 'excluded_root_entries': sorted(p.name for p in source.iterdir() if p.name not in INCLUDED),
                    'external_files_included': False, 'configuration_secrets_included': False,
                    'collections': inventory, 'files': copied_files}
        # Manifest is the completion boundary, written only after durable payload.
        sync_tree(payload)
        write_json(destination / 'manifest.json', manifest)
        sync_tree(destination)
        return manifest


def validate_backup(source):
    source = safe_path(source); files(source)
    manifest = json.loads((source / 'manifest.json').read_text())
    if manifest['format'] != 'atlas-offline-backup-v1' or manifest['versions'] != versions():
        raise ValueError('backup format/runtime version mismatch; use original pinned runtime')
    payload = source / 'state'
    if {p.name for p in payload.iterdir()} - INCLUDED:
        raise ValueError('unexpected state member')
    if manifest['files'] != files(payload):
        raise ValueError('backup file inventory/hash mismatch')
    integrity(payload / 'registry.sqlite3')
    if points(payload) != manifest['collections']:
        raise ValueError('backup point inventory differs')
    return manifest


def restore(source, destination):
    source, destination = new_target(source, destination)
    manifest = validate_backup(source)
    shutil.copytree(source / 'state', destination)
    destination.chmod(0o700)
    if files(destination) != manifest['files']:
        raise ValueError('restored preimage differs')
    # Preserve prior location maps across a backup of an already restored root.
    prior = destination / MARKER
    roots = [manifest['source_root']]
    previous = None
    if prior.exists():
        previous = json.loads(prior.read_text())
        roots += previous.get('storage_source_roots', [])
        prior.unlink()
    write_json(prior, {'format': 'atlas-restored-state-v1', 'reconciled': False,
                      'backup_manifest_sha256': sha(source / 'manifest.json'),
                      'storage_source_roots': sorted(set(roots)),
                      'storage_target_root': str(destination),
                      'previous_restore': previous,
                      'preimage_verified': True})
    (destination / 'service.lock').touch(mode=0o600, exist_ok=False)
    sync_tree(destination)
    return json.loads(prior.read_text())


def restore_state(root):
    path = Path(root) / MARKER
    if not path.exists(): return None
    state = json.loads(path.read_text())
    if state.get('format') != 'atlas-restored-state-v1' or state.get('storage_target_root') != str(Path(root)):
        raise ValueError('invalid or moved restore marker; restore through atlas-state')
    return state


def storage_path(root, value):
    """Resolve only Engine-owned storage; do not rewrite journal/source identities."""
    root = Path(root); path = Path(value)
    state = restore_state(root)
    if state:
        for old in state['storage_source_roots']:
            try: relative = path.relative_to(old)
            except ValueError: continue
            path = root / relative; break
    path = safe_path(path)
    if not any(path.is_relative_to(root / n) for n in ('artifacts', 'media')):
        raise ValueError('artifact/prior outside owned storage')
    return path


def reconcile(root, report_path):
    root = safe_path(root)
    with offline(root):
        state = restore_state(root)
        if not state or state['reconciled']: raise ValueError('quarantined restore required')
        report = json.loads(safe_path(report_path).read_text())
        if (report.get('registry_logical_sha256') != registry_digest(root)
                or report.get('backup_manifest_sha256') != state['backup_manifest_sha256']
                or report.get('target_root') != str(root)
                or report.get('external_effects_reconciled') is not True):
            raise ValueError('report must bind current registry and confirm reconciliation')
        for section in ('generation', 'workspace', 'media', 'acquisition', 'projection', 'uploads'):
            if not isinstance(report.get(section), str) or not report[section].strip():
                raise ValueError('explicit reconciliation assessment required: ' + section)
        with closing(ro(Path(root) / 'registry.sqlite3')) as c:
            for kind, status in c.execute('select kind,state from jobs'):
                if kind in EFFECTS and status not in TERMINAL:
                    raise ValueError('unresolved external job; reconcile before activation')
            if c.execute("select 1 from uploads where state in ('importing','ack_unknown') limit 1").fetchone():
                raise ValueError('unresolved native upload')
        state.update(reconciled=True, reconciliation_report=report)
        temp = Path(root) / 'restore-state.new'
        write_json(temp, state); temp.replace(Path(root) / MARKER); sync_tree(Path(root))
        return state


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('backup', 'restore', 'reconcile'))
    p.add_argument('source', type=Path); p.add_argument('destination', type=Path)
    a = p.parse_args()
    try:
        result = globals()[a.action](a.source, a.destination)
        print(dump(result))
    except (ValueError, OSError, sqlite3.Error) as e:
        p.exit(1, 'Atlas state operation refused: ' + str(e) + '\n')


if __name__ == '__main__': main()
