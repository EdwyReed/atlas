"""Read-only bounded Qdrant capture and isolated full-fidelity overlay import.

Covers ALL model_asset cards and ALL anime_studio_generations rows, never claims
unchanged tags/relations or a globally atomic snapshot. No live mutation path.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import zlib
from urllib.parse import urlsplit

from .final_delta import save, sha256, code_pin
from .full_migration import Mirror
from .materialize import materialize
from .migrate import Source
from .reference_mirror import ReferenceMirror, build_index
from .repository import dump, digest

FORMAT = 'atlas-scoped-point-delta-v1'
SCOPES = {'prompt_atlas': {'must': [{'key': 'kind', 'match': {'value': 'model_asset'}}]},
          'anime_studio_generations': None}


def connect(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)


def baseline_state(path):
    """Bounded indexed reads; no rehash/reimport of the million-point archive."""
    points = {}
    with connect(path) as c:
        c.execute('BEGIN')
        metadata = {}
        for name in SCOPES:
            row = c.execute('select schema,expected,done,after_schema,after_count from collections where name=?', (name,)).fetchone()
            if not row or not row[2] or row[0] != row[3] or row[1] != row[4]:
                raise ValueError('baseline collection not sealed/complete')
            metadata[name] = list(row)
            sql = 'select id,raw,sha256 from points where collection=?'
            if name == 'prompt_atlas':
                sql += " and entity_type='resource'"
            for key, raw, expected in c.execute(sql, (name,)):
                value = json.loads(zlib.decompress(raw))
                if digest(value) != expected or dump(value['id']) != key:
                    raise ValueError('baseline point checksum/ID mismatch')
                if name == 'prompt_atlas' and value.get('payload', {}).get('kind') != 'model_asset':
                    raise ValueError('unexpected baseline resource scope')
                points[name, key] = value
    binding = {'path': str(Path(path).resolve()), 'collections': metadata,
               'scoped_sha256': digest([[n, k, digest(v)] for (n, k), v in sorted(points.items())])}
    return binding, points


def scan(source, name):
    offset = None
    seen_cursors = set()
    points = {}
    while True:
        body = {'limit': 128, 'with_payload': True, 'with_vector': True}
        if SCOPES[name] is not None:
            body['filter'] = SCOPES[name]
        if offset is not None:
            body['offset'] = offset
        page = source.call('POST', '/collections/' + name + '/points/scroll', body)
        for point in page['points']:
            # Qdrant's returned JSON, including every payload/vector field, is archival truth.
            key = dump(point['id'])
            if key in points:
                raise ValueError('duplicate point in scroll')
            if name == 'prompt_atlas' and point.get('payload', {}).get('kind') != 'model_asset':
                raise ValueError('out-of-scope point')
            points[key] = point
        offset = page.get('next_page_offset')
        if offset is None:
            return points
        if dump(offset) in seen_cursors:
            raise ValueError('scroll cursor loop')
        seen_cursors.add(dump(offset))


def capture(source, baseline, endpoint):
    import datetime
    binding, old = baseline_state(baseline)
    result = {'format': FORMAT, 'source': endpoint, 'baseline': binding,
              'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'scopes': {}, 'source_writes': False, 'production_ready': False,
              'source_consistency': 'two equal bounded online scrolls; NOT atomic; writers not fenced',
              'global_coverage_complete': False,
              'unverified_scopes': ['prompt_atlas:danbooru_tag', 'prompt_atlas:tag_relation',
                                    'external domain documents and session changes since their watermarks']}
    for name in SCOPES:
        before = source.schema(name)
        count_body = {'exact': True}
        if SCOPES[name] is not None:
            count_body['filter'] = SCOPES[name]
        count_before = source.call('POST', '/collections/' + name + '/points/count', count_body)['count']
        first = scan(source, name)
        second = scan(source, name)
        count_after = source.call('POST', '/collections/' + name + '/points/count', count_body)['count']
        after = source.schema(name)
        if len(first) != count_before or count_before != count_after:
            raise ValueError('scope count/scroll coverage drift')
        if first != second or before['config'] != after['config'] or before['points_count'] != after['points_count']:
            raise ValueError('source drift during bounded capture; use a NEW capture')
        if name == 'anime_studio_generations' and len(first) != after['points_count']:
            raise ValueError('generation coverage mismatch')
        result['scopes'][name] = {'filter': SCOPES[name], 'before': before, 'after': after,
                                  'points': [first[k] for k in sorted(first)], 'readbacks': len(second),
                                  'count_before': count_before, 'count_after': count_after}
    result['finished_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return result


def validate_capture(value, baseline):
    if value['format'] != FORMAT or set(value['scopes']) != set(SCOPES):
        raise ValueError('unsupported scope/format')
    if value['global_coverage_complete'] is not False or value['production_ready'] is not False:
        raise ValueError('bounded capture cannot certify production/global coverage')
    binding, old = baseline_state(baseline)
    if value['baseline'] != binding:
        raise ValueError('baseline lineage drift')
    new = {}
    for name, scope in value['scopes'].items():
        if scope['filter'] != SCOPES[name] or scope['before']['config'] != scope['after']['config']:
            raise ValueError('scope/schema drift')
        if scope['before']['points_count'] != scope['after']['points_count']:
            raise ValueError('count drift')
        if name == 'anime_studio_generations' and len(scope['points']) != scope['after']['points_count']:
            raise ValueError('incomplete generation scope')
        for p in scope['points']:
            key = (name, dump(p['id']))
            if key in new or (name == 'prompt_atlas' and p.get('payload', {}).get('kind') != 'model_asset'):
                raise ValueError('duplicate/out-of-scope point')
            new[key] = p
        if not len(scope['points']) == scope['readbacks'] == scope['count_before'] == scope['count_after']:
            raise ValueError('incomplete readback/scope count')
    return old, new


def import_capture(capture_path, expected_sha256, baseline, destination):
    capture_path = Path(capture_path).resolve()
    if sha256(capture_path) != expected_sha256:
        raise ValueError('capture hash drift')
    value = json.loads(capture_path.read_text())
    old, new = validate_capture(value, baseline)
    destination = Path(destination).absolute()
    if destination.is_symlink():
        raise ValueError('linked destination')
    destination = destination.resolve()
    for src in (capture_path, Path(baseline).resolve()):
        if src == destination or src in destination.parents or destination in src.parents:
            raise ValueError('destination overlaps source')
    binding = {'format': FORMAT, 'capture_sha256': expected_sha256, 'code_sha256': code_pin(),
               'baseline': value['baseline'], 'destination': str(destination)}
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = destination / 'owner.json'
    if not marker.exists():
        if list(destination.iterdir()):
            raise ValueError('destination must be new or owned by exact capture/code')
        save(marker, binding)
    if json.loads(marker.read_text()) != binding:
        raise ValueError('resume binding mismatch')
    for p in destination.rglob('*'):
        if p.is_symlink() or (p.is_file() and p.stat().st_nlink != 1):
            raise ValueError('linked destination entry')
    import fcntl
    with (destination / 'import.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        save(destination / 'receipt.json', {**binding, 'complete': False})
        changes = {'new': [], 'changed': [], 'unchanged': [], 'missing_held': [], 'acl_relaxation_held': []}
        for key, p in new.items():
            changes['new' if key not in old else 'unchanged' if p == old[key] else 'changed'].append(list(key))
        changes['missing_held'] = [list(key) for key in sorted(old.keys() - new.keys())]
        mirror_path = destination / 'overlay.sqlite3'
        mirror = Mirror(mirror_path)
        try:
            for name, scope in value['scopes'].items():
                params = scope['after']['config']['params']
                vectors = params['vectors']
                if 'size' in vectors:
                    vectors = {'unnamed': vectors}
                with mirror.db:
                    mirror.db.execute('insert or ignore into collections(name,schema,expected) values(?,?,?)',
                                      (name, dump(params), len(scope['points'])))
                mirror.ingest(name, vectors, scope['points'], None, True)
                with mirror.db:
                    mirror.db.execute('update collections set after_schema=?,after_count=? where name=?',
                                      (dump(params), len(scope['points']), name))
            raw = mirror.verify()
        finally:
            mirror.close()
        typed = materialize(mirror_path, destination / 'typed.json')
        index = build_index(mirror_path, destination / 'index.json')
        with sqlite3.connect(mirror_path) as c:
            c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        # Exact raw JSON and typed payload/vector values, not only IDs/counts.
        with connect(mirror_path) as c:
            actual = {}
            from .migrate import convert
            for name, key, raw_point, envelope, vectors in c.execute(
                    'select p.collection,p.id,p.raw,e.payload,q.vectors from points p '
                    'join entities e using(collection,id) join vector_quarantine q using(collection,id)'):
                point = json.loads(zlib.decompress(raw_point))
                params = value['scopes'][name]['after']['config']['params']['vectors']
                params = {'unnamed': params} if 'size' in params else params
                expected_envelope = convert(name, point, params)[1]['payload']
                if (json.loads(zlib.decompress(envelope)) != expected_envelope or
                        json.loads(zlib.decompress(vectors)) != point.get('vector')):
                    raise ValueError('exact typed provenance/payload/vector readback mismatch')
                actual[name, key] = point
        if actual != new or sha256(capture_path) != expected_sha256:
            raise ValueError('capture/destination readback drift')
        result = {**binding, 'complete': True, 'global_coverage_complete': False,
                  'production_ready': False, 'source_consistency': value['source_consistency'],
                  'changes': changes, 'counts': {k: len(v) for k, v in changes.items()},
                  'raw': raw, 'typed': typed, 'index': index,
                  'overlay_sha256': sha256(mirror_path), 'source_writes': False,
                  'semantic_enabled': False, 'network_calls_during_import': 0}
        save(destination / 'receipt.json', result)
        return result


class ScopedReferenceMirror:
    """Explicit offline overlay; existing base reader remains unchanged by default."""
    semantic_enabled = False

    def __init__(self, baseline, destination):
        root = Path(destination).resolve()
        self.receipt = json.loads((root / 'receipt.json').read_text())
        if not self.receipt.get('complete') or self.receipt['baseline'] != baseline_state(baseline)[0]:
            raise ValueError('overlay/base not verified')
        if sha256(root / 'overlay.sqlite3') != self.receipt['overlay_sha256']:
            raise ValueError('overlay content drift')
        self.base = ReferenceMirror(baseline)
        self.overlay = ReferenceMirror(root / 'overlay.sqlite3')
        self.covered = set(baseline_state(baseline)[1])
        with self.overlay.connect() as c:
            self.covered.update((n, k) for n, k in c.execute('select collection,id from points'))
        self.holds = {tuple(k) for k in self.receipt['changes']['missing_held']}

    @staticmethod
    def items(reader, sql, args=(), excluded=()):
        with reader.connect() as c:
            return [reader.unpack(row) for row in c.execute(sql, args)
                    if (row[0], row[1]) not in excluded]

    def resources(self):
        sql = "select collection,id,payload from entities where entity_type='resource' order by collection,id"
        return self.items(self.overlay, sql, excluded=self.holds)

    def get(self, collection, point_id):
        from .contracts import Fault
        key = (collection, dump(point_id))
        if key in self.holds:
            raise Fault('SOURCE_MISSING', 'point absent in newer source snapshot')
        return (self.overlay if key in self.covered else self.base).get(collection, point_id)

    def evidence(self, kind, key):
        # Exclude superseded base rows even if they used a different literal key.
        sql = ('select e.collection,e.id,e.payload from reference_keys k join entities e using(collection,id) '
               'where k.kind=? and k.key=? order by e.collection,e.id')
        return (self.items(self.overlay, sql, (kind, key), self.holds) +
                self.items(self.base, sql, (kind, key), self.covered))[:10]

    def query(self, vector, limit):
        return self.base.query(vector, limit)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='action', required=True)
    c = sub.add_parser('capture')
    c.add_argument('--source', default='http://127.0.0.1:6333')
    c.add_argument('--baseline', type=Path, required=True)
    c.add_argument('--out', type=Path, required=True)
    i = sub.add_parser('import')
    i.add_argument('--capture', type=Path, required=True)
    i.add_argument('--capture-sha256', required=True)
    i.add_argument('--baseline', type=Path, required=True)
    i.add_argument('--destination', type=Path, required=True)
    a = p.parse_args()
    os.umask(0o077)
    if a.action == 'capture':
        if a.out.exists():
            raise SystemExit('capture output must be NEW; preserve previous watermarks')
        u = urlsplit(a.source)
        if (u.scheme, u.hostname, u.port, u.path, u.query, u.fragment) != ('http', '127.0.0.1', 6333, '', '', '') or u.username or u.password:
            raise SystemExit('only explicit local legacy source allowed')
        source = Source(a.source)
        try:
            value = capture(source, a.baseline, a.source)
        finally:
            source.client.close()
        a.out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Exclusive create; never overwrite another capture after concurrent scan.
        with a.out.open('x') as f:
            f.write(json.dumps(value, ensure_ascii=False, indent=2))
        print(dump({'capture': str(a.out), 'sha256': sha256(a.out), 'counts': {k: len(v['points']) for k, v in value['scopes'].items()},
                    'global_coverage_complete': False}))
    else:
        result = import_capture(a.capture, a.capture_sha256, a.baseline, a.destination)
        print(dump({k: result[k] for k in ('complete', 'counts', 'global_coverage_complete', 'production_ready')}))


if __name__ == '__main__':
    main()
