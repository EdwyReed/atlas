"""Full frozen export -> isolated Qdrant typed quarantine, no live source I/O.
Uses the same convert/schema as runtime and sample migration. Unknown spaces are
never activated. The only accepted HTTP destination is the disposable port 16333.
"""
import argparse, collections, hashlib, json, math, os, time
from pathlib import Path
import httpx
from .full_migration import FrozenSource
from .migrate import convert
from .repository import dump
from .typed import validate

URL = 'http://127.0.0.1:16333'
WRITER_VERSION = 'atlas-frozen-quarantine-v2-sibling-vectors'

def save(path, value):
    path = Path(path)
    tmp = path.with_suffix('.pending')
    tmp.write_text(json.dumps(value, indent=2)); tmp.chmod(0o600)
    os.replace(tmp, path)

class Destination:
    def __init__(self):
        self.h = httpx.Client(base_url=URL, timeout=180, trust_env=False, follow_redirects=False)
    def call(self, method, path, body=None):
        r = self.h.request(method, path, json=body)
        r.raise_for_status()
        return r.json()['result']
    def ensure(self, name, schema):
        if not name.startswith('atlas_v1_quarantine_'):
            raise ValueError('only isolated quarantine namespace is allowed')
        r = self.h.get('/collections/' + name)
        if r.status_code == 404:
            self.call('PUT', '/collections/' + name, {'vectors': schema, 'on_disk_payload': True,
                'hnsw_config': {'m': 0}, 'optimizers_config': {'indexing_threshold': 0}})
        else:
            r.raise_for_status()
            actual = r.json()['result']['config']['params']['vectors']
            if actual != schema:
                raise ValueError('existing destination vector schema differs')
    def write_verify(self, name, points, schema):
        self.call('PUT', '/collections/' + name + '/points?wait=true', {'points': points})
        rows = self.call('POST', '/collections/' + name + '/points', {
            'ids': [p['id'] for p in points], 'with_payload': True, 'with_vector': True})
        expected = {dump(p['id']): p for p in points}
        if len(rows) != len(expected):
            raise AssertionError('readback ID coverage')
        for r in rows:
            p = expected.pop(dump(r['id']))
            if r['payload'] != p['payload']:
                raise AssertionError('typed payload/ACL/ID mismatch')
            if set(r.get('vector') or {}) != set(p['vector']):
                raise AssertionError('named vector mismatch')
            for key, vector in p['vector'].items():
                actual = r['vector'][key]
                if len(vector) != len(actual):
                    raise AssertionError('vector dimensions')
                if schema[key]['distance'] == 'Cosine':
                    dot = sum(a*b for a,b in zip(vector, actual))
                    norm = math.sqrt(sum(a*a for a in vector)*sum(a*a for a in actual))
                    if not norm or abs(dot/norm-1) > 1e-5:
                        raise AssertionError('cosine direction changed')
                elif any(abs(a-b) > 1e-5 for a,b in zip(vector, actual)):
                    raise AssertionError('vector changed')
        if expected:
            raise AssertionError('missing readbacks')

def run(source_path, manifest_path, batch_size=512, stop_after_batches=None):
    source_path = Path(source_path).resolve()
    st = source_path.stat()
    # Sealed export identity, checked on every resume; no live scan or embedding.
    identity = {'path': str(source_path), 'bytes': st.st_size, 'mtime_ns': st.st_mtime_ns}
    state_path = Path(manifest_path)
    state = json.loads(state_path.read_text()) if state_path.exists() else {
        'writer_version': WRITER_VERSION,
        'source_identity': identity, 'destination': URL, 'collections': {}, 'targets': {},
        'complete': False, 'source_consistency': 'frozen local export; upstream was online, NOT atomic',
        'live_writes': False, 'llm_calls': 0, 'semantic_queries_enabled': False,
        'vectors': 'all unknown spaces quarantined; invalid original vectors retained in payload',
        'verified_points': 0, 'elapsed_seconds': 0}
    if state['source_identity'] != identity or state['destination'] != URL:
        raise ValueError('resume source/destination identity differs')
    if state.get('writer_version') != WRITER_VERSION:
        raise ValueError('resume writer version differs; retain old receipt, use a new manifest/destination for this contract')
    source = FrozenSource(source_path); dest = Destination(); ensured = set()
    started = time.monotonic(); batches = 0
    try:
        for name in source.collections():
            before = source.schema(name)
            schema = before['config']['params']['vectors']
            schema = {'unnamed': schema} if 'size' in schema else schema
            progress = state['collections'].setdefault(name, {'last_id': '', 'verified': 0,
                'expected': before['points_count'], 'schema': schema, 'done': False})
            if progress['expected'] != before['points_count'] or progress['schema'] != schema:
                raise ValueError('source schema/count changed')
            while not progress['done']:
                rows = source.db.execute('select id,raw from points where collection=? and id>? order by id limit ?',
                    (name, progress['last_id'], batch_size)).fetchall()
                if not rows:
                    if progress['verified'] != progress['expected']:
                        raise AssertionError('incomplete source traversal')
                    progress['done'] = True; save(state_path, state); break
                import zlib
                grouped = collections.defaultdict(list); specs = {}; batch_hash = hashlib.sha256()
                for key, raw in rows:
                    point = json.loads(zlib.decompress(raw))
                    target, converted, bad = convert(name, point, schema)
                    validate(converted['payload'])
                    if bad:
                        # A bad sibling must not erase viable named vectors. Keep mixed
                        # records in a distinct quarantine with the original vector schema.
                        target += '_mixed' if converted['vector'] else '_invalid'
                        converted['payload']['quarantined_original_vectors'] = point.get('vector')
                        specs[target] = schema if converted['vector'] else {}
                    else:
                        specs[target] = schema
                    grouped[target].append(converted)
                    batch_hash.update(dump([name, point]).encode() + b'\n')
                for target, points in grouped.items():
                    if target not in ensured:
                        dest.ensure(target, specs[target]); ensured.add(target)
                    dest.write_verify(target, points, specs[target])
                # Only commit cursor after every target write + exact readback. Replay is upsert-idempotent.
                for target, points in grouped.items():
                    meta = state['targets'].setdefault(target, {'expected': 0, 'schema': specs[target]})
                    meta['expected'] += len(points)
                progress['last_id'] = rows[-1][0]; progress['verified'] += len(rows)
                state['verified_points'] += len(rows)
                progress['receipt_chain_sha256'] = hashlib.sha256((progress.get('receipt_chain_sha256','') + batch_hash.hexdigest()).encode()).hexdigest()
                save(state_path, state); batches += 1
                if batches % 100 == 0:
                    print(dump({'verified': state['verified_points'], 'seconds_this_run': time.monotonic()-started}), flush=True)
                    if __import__('shutil').disk_usage(source_path.parent).free < 2*1024**3:
                        raise RuntimeError('stop before exhausting disk; resumable destination retained')
                if stop_after_batches and batches >= stop_after_batches:
                    return state
        for target, meta in state['targets'].items():
            count = dest.call('POST', '/collections/' + target + '/points/count', {'exact': True})['count']
            meta['actual_count'] = count
            if count != meta['expected']:
                raise AssertionError('target count mismatch')
        state['complete'] = all(p['done'] for p in state['collections'].values())
        state['elapsed_seconds'] += time.monotonic()-started
        save(state_path, state); print(dump(state), flush=True); return state
    finally:
        dest.h.close(); source.close()

def main():
    p = argparse.ArgumentParser(); p.add_argument('--source', required=True); p.add_argument('--manifest', required=True)
    p.add_argument('--stop-after-batches', type=int); a = p.parse_args()
    run(a.source, a.manifest, stop_after_batches=a.stop_after_batches)
if __name__ == '__main__': main()
