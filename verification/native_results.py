"""Narrow installed regression: bounded DTO snapshots and unchanged image artifacts.

Synthetic DTO/PNG fixtures, not native processor or image-model acceptance.
Run the selected installed Python with -I and a fresh --out directory.
"""
import argparse
import base64
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from PIL import Image
from atlas_engine.engine import Engine
from atlas_engine.native_results import snapshot
from atlas_engine.repository import Repository
from atlas_engine.artifacts import Artifacts
from atlas_engine.contracts import Fault

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--out', type=Path, required=True)
a = p.parse_args()
a.out.mkdir(parents=True, exist_ok=False)
checks = []
session = {'results': {
    'i': {'type': 'integer_output', 'value': 17, 'private': '/private/not-a-result'},
    'b': {'type': 'boolean_output', 'value': True},
    'wrong': {'type': 'integer_output', 'value': True},
    'huge': {'type': 'integer_output', 'value': 2**200},
    'opaque': {'type': 'latents_output', 'latents': {'path': '/private/tensor', 'blob': 'x'*100000}},
    'string': {'type': 'string_output', 'value': '/private/not-exposed'},
    'foreign': {'type': 'integer_output', 'value': 99}}}
s = snapshot(session, {'i', 'b', 'wrong', 'huge', 'opaque', 'string'})
entries = {e['node_id']: e for e in s['entries']}
assert entries['i']['value'] == 17 and entries['b']['value'] is True
assert entries['wrong']['status'] == 'invalid_value'
assert entries['huge']['status'] == 'value_out_of_range'
assert entries['opaque']['status'] == entries['string']['status'] == 'unsupported_type'
assert s['unscoped'] == s['omitted'] == 1 and not s['truncated']
assert '/private' not in json.dumps(s) and 'blob' not in json.dumps(s)
checks.append('exact primitive DTOs; bool not integer; range; explicit unsupported; no opaque fields or foreign IDs')
large = {'results': {str(i): {'type':'integer_output','value':i} for i in range(1000)}}
bounded = snapshot(large, set(large['results']))
assert len(bounded['entries']) == 64 and bounded['omitted'] == 936 and bounded['truncated']
assert len(json.dumps(bounded).encode()) < 20000
assert snapshot({'results': {'x'*257: {'type':'integer_output','value':1}}}, {'x'*257})['unscoped'] == 1
checks.append('64 entries; honest omitted/truncated counts; bounded IDs')

for raw, status in [(17, 'available'), (True, 'unsupported_value_type'),
                    ('private-string', 'unsupported_value_type'), (2**100, 'value_out_of_range'),
                    ({'private': 'blob'}, 'unsupported_value_type')]:
    named = {'prepared_source_mapping': {'prepared-call': 'call'}, 'results': {
        'prepared-call': {'type': 'workflow_return_output', 'values': {'result': raw, 'other': 'private-string'}},
        'foreign': {'type': 'workflow_return_output', 'values': {'result': 99}}}}
    scoped_return = snapshot(named, {'call'}, {'call': 'result'})
    entry = scoped_return['entries'][0]
    assert entry['status'] == status and entry['return_key'] == 'result'
    assert scoped_return['unscoped'] == 1 and 'private' not in json.dumps(scoped_return)
    if status == 'available': assert type(entry['value']) is int and entry['value'] == 17
    else: assert 'value' not in entry
    assert snapshot(named, {'call'})['entries'][0]['status'] == 'unsupported_type'
assert snapshot({'results': {'call': {'type': 'workflow_return_output', 'values': {}}}},
                {'call'}, {'call': 'result'})['entries'][0]['status'] == 'return_missing'
checks.append('selected named workflow return only; exact prepared mapping; integer strict/range; missing/unsupported explicit; no unselected/private/child copying')

item = {'item_id':1,'status':'completed','session_id':'synthetic-session','session': {
    'graph': {'id':'synthetic-graph','nodes': {
        'final': {'is_intermediate':False}, 'input': {'is_intermediate':True},
        'mask': {'is_intermediate':True}, 'unspecified': {}}},
    'prepared_source_mapping': {'prepared':'final'},
    'results': {'prepared': {'type':'image_output','image':{'image_name':'final.png'}},
                'input': {'image':{'image_name':'input.png'}},
                'mask': {'image':{'image_name':'mask.png'}},
                'unspecified': {'image':{'image_name':'intermediate.png'}},
                'i': {'type':'integer_output','value':17}}}}
legacy = Engine.compact(deepcopy(item))
assert 'native_results' not in legacy and legacy['outputs'] == ['final.png']
scoped = Engine.compact(deepcopy(item), {'final','input','mask','unspecified','i'})
assert scoped['outputs'] == legacy['outputs']
assert Engine.outputs({'1': scoped}) == ['final.png']
checks.append('final image names unchanged; input/mask/intermediate excluded; typed result not an image')

buffer = io.BytesIO()
Image.new('RGB', (2,3), (17,31,47)).save(buffer, format='PNG')
raw = buffer.getvalue()
requests = []
def serve(request):
    requests.append(str(request.url))
    assert request.url.path == '/api/v1/images/i/final.png/full'
    return httpx.Response(200, content=raw)
repo = Repository(a.out/'state')
job, _ = repo.reserve('synthetic', 'regression', 'image', 'generation', {}, {'outputs':scoped['outputs'], 'native_items':{'1':scoped}})
with httpx.Client(base_url='http://fixture', transport=httpx.MockTransport(serve)) as client:
    artifacts = Artifacts(repo, SimpleNamespace(http=client))
    result = artifacts.fetch(job['id'], 'final.png')
    assert result['sha256'] == hashlib.sha256(raw).hexdigest()
    assert (result['width'],result['height']) == (2,3)
    try:
        artifacts.fetch(job['id'], 'input.png')
        raise AssertionError('intermediate artifact admitted')
    except Fault as e:
        assert e.code == 'ARTIFACT_NOT_LINKED'
    restarted = Artifacts(Repository(a.out/'state'), SimpleNamespace(http=client))
    assert restarted.fetch(job['id'], 'final.png') == result and len(requests) == 1
    assert base64.b64decode(restarted.read(result['artifact_id'], 0, 100000)['base64']) == raw
checks.append('artifact bytes/hash/dimensions; intermediate refused; restart fetch cached; read bytes identical')
(a.out/'result.json').write_text(json.dumps({'checks':checks,'snapshot':s,'bounded_bytes':len(json.dumps(bounded).encode()),'artifact':result},indent=2))
print(json.dumps({'checks':checks},indent=2))
