"""Cold lineage refusals derived from saved original-full-server responses.

Not another execution acceptance: no native server, enqueue or model. Each case
starts from a deep copy without a previously verified cached result/artifact.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from atlas_engine.engine import Engine
from atlas_engine.workflow_images import hydrate
from atlas_engine.artifacts import Artifacts
from atlas_engine.repository import Repository
from atlas_engine.contracts import Fault

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--evidence', required=True, type=Path)
p.add_argument('--out', required=True, type=Path)
a = p.parse_args()
a.out.mkdir(parents=True, exist_ok=False)
files = sorted(a.evidence.glob('image-image-*.json'))
assert len(files) == 5, files
checks = []

def observe(j, r, c):
    # Replay the saved exact child DTO; mismatched requested/envelope IDs refuse.
    compact = Engine.compact(r, j['plan']['graph']['nodes'], j['plan']['workflow_returns'])
    return hydrate(SimpleNamespace(invoke=SimpleNamespace(item=lambda n: c)), j, r, compact)

for path in files:
    evidence = json.loads(path.read_text())
    job, root, child = (evidence[k] for k in ('job', 'native_root', 'native_child'))
    good = observe(job, root, child)
    assert good['outputs'] == job['outputs']
    lineage = good['native_results']['entries'][0]['lineage']
    assert lineage['input_source'] == next(v for v in job['payload']['workflow_inputs'].values() if isinstance(v, dict))
    checks.append({'baseline': path.name, 'input_source': lineage['input_source'], 'outputs': good['outputs']})

base = json.loads((a.evidence/'image-image-http.json').read_text())
j, r, c = (base[k] for k in ('job', 'native_root', 'native_child'))
call = next(iter(r['session']['results']))
rn, vn, tn = (c['session']['source_prepared_mapping'][k][0] for k in ('resize', 'rv', 'ret'))
h = ('session', 'workflow_call_history', 0)

def set_at(data, path, value):
    for key in path[:-1]:
        data = data[key]
    data[path[-1]] = value

cases = []
def case(name, target, path, value):
    cases.append((name, lambda j,r,c: set_at({'job':j,'root':r,'child':c}[target], path, value)))
for field in ('item_id','parent_item_id','root_item_id'):
    case('wrong child '+field, 'child', (field,), 9999)
for field in ('session_id','parent_session_id','workflow_call_id','user_id','queue_id'):
    case('wrong child '+field, 'child', (field,), 'other')
case('wrong root item', 'root', ('item_id',), 9999)
case('wrong root queue', 'root', ('queue_id',), 'other')
case('wrong root nested session', 'root', ('session','id'), 'other')
case('wrong root graph', 'root', ('session','graph','id'), 'other')
case('wrong root call workflow input', 'root', ('session','graph','nodes','call','workflow_inputs'), {})
case('wrong root prepared call input', 'root', ('session','execution_graph','nodes',call,'workflow_inputs'), {})
case('wrong child nested session', 'child', ('session','id'), 'other')
case('wrong nested parent call', 'child', ('session','workflow_call_parent','workflow_call_id'), 'other')
case('wrong nested parent session', 'child', ('session','workflow_call_parent','parent_session_id'), 'other')
case('missing child graph identity', 'child', ('session','graph','id'), '')
for field in ('parent_session_id','prepared_call_node_id','source_call_node_id','workflow_id','child_session_id'):
    case('wrong history '+field, 'root', h+(field,), 'other')
case('wrong history child sessions', 'root', h+('child_session_ids',), ['other'])
case('missing history', 'root', ('session','workflow_call_history'), [])
case('duplicate history', 'root', ('session','workflow_call_history'), r['session']['workflow_call_history']*2)
case('duplicate child IDs', 'root', h+('child_item_ids',), [c['item_id']]*2)
case('missing root results', 'root', ('session','results'), {})
case('missing child results', 'child', ('session','results'), {})
for node in (rn, vn, tn):
    remaining = deepcopy(c['session']['results']); remaining.pop(node)
    case('missing child result '+node, 'child', ('session','results'), remaining)
extra = deepcopy(c['session']['results']); extra['duplicate'] = extra[rn]
case('duplicate child result', 'child', ('session','results'), extra)
extra = deepcopy(r['session']['results']); extra['duplicate'] = extra[call]
case('duplicate root result', 'root', ('session','results'), extra)
case('duplicate root mapping', 'root', ('session','prepared_source_mapping','duplicate'), 'call')
case('wrong root prepared mapping', 'root', ('session','prepared_source_mapping',call), 'other')
case('wrong resize prepared mapping', 'child', ('session','prepared_source_mapping',rn), 'other')
case('wrong return prepared mapping', 'child', ('session','prepared_source_mapping',tn), 'resize')
case('wrong inverse mapping', 'child', ('session','source_prepared_mapping','resize'), [tn])
case('wrong prepared resize identity', 'child', ('session','execution_graph','nodes',rn,'id'), 'other')
case('wrong prepared resize input', 'child', ('session','execution_graph','nodes',rn,'image'), {'image_name':'other.png'})
case('wrong child source input', 'child', ('session','graph','nodes','resize','image'), {'image_name':'other.png'})
case('wrong submitted input scope', 'job', ('payload','workflow_inputs','saved_workflow_input::resize::image','connection_id'), 'other')
case('wrong resize width', 'child', ('session','results',rn,'width'), 12)
case('wrong resize height', 'child', ('session','results',rn,'height'), 10)
case('unsupported executed dimensions', 'child', ('session','graph','nodes','resize','width'), 2049)
case('wrong prepared dimensions', 'child', ('session','execution_graph','nodes',rn,'width'), 12)
case('other selected IMAGE return', 'root', ('session','results',call,'values','resized'), {'image_name':'other.png'})
case('wrong child selected return', 'child', ('session','results',tn,'values','resized'), {'image_name':'other.png'})
case('wrong named intermediate', 'child', ('session','results',vn,'value','value'), {'image_name':'other.png'})
case('wrong resize intermediate', 'child', ('session','results',rn,'image'), {'image_name':'other.png'})
input_image = c['session']['graph']['nodes']['resize']['image']
case('input passed as selected final', 'root', ('session','results',call,'values','resized'), input_image)
case('unselected IMAGE only', 'root', ('session','results',call,'values'), {'other': {'image_name':j['outputs'][0]}})
# Consistently substituted input across execution still differs from submitted intent.
def substitute(j,r,c):
    c['session']['graph']['nodes']['resize']['image'] = {'image_name':'other.png'}
    c['session']['execution_graph']['nodes'][rn]['image'] = {'image_name':'other.png'}
cases.append(('consistent executed source differs from submitted IMAGE', substitute))
repo = Repository(a.out/'state')
for index, (name, mutate) in enumerate(cases):
    job, root, child = (deepcopy(base[k]) for k in ('job','native_root','native_child'))
    mutate(job, root, child)
    observed = observe(job, root, child)
    assert observed['outputs'] == [], (name, observed)
    result = observed['native_results']
    assert result['image_status'] == 'image_lineage_unavailable', (name, result)
    assert all(e['status'] != 'available' and not any(k in e for k in ('image','lineage','value')) for e in result['entries']), (name, result)
    # Actual artifact boundary sees a cold ledger, never cached successful evidence.
    cold, _ = repo.reserve('lineage', 'saved-native-counterexample', str(index), 'generation', {},
                          {'outputs': [], 'plan': job['plan'], 'native_items': {str(root['item_id']): observed}})
    artifacts = Artifacts(repo, None)
    for image in (base['job']['outputs'][0], input_image['image_name'], 'other.png'):
        try:
            artifacts.fetch(cold['id'], image)
        except Fault as e:
            assert e.code == 'ARTIFACT_NOT_LINKED', (name, e.code)
        else:
            raise AssertionError(name+' exposed artifact')
    assert artifacts.list(cold['id']) == []
    checks.append({'negative':name, 'image_status':result['image_status'],
                   'entry_statuses':[e['status'] for e in result['entries']], 'artifact_error':'ARTIFACT_NOT_LINKED'})
receipt = {'scope':'deep-copy counterexamples from actual original full-server responses; cold hydration, no cached acceptance; not native execution',
           'source_files':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
           'baseline_count':len(files), 'negative_count':len(cases), 'checks':checks}
(a.out/'result.json').write_text(json.dumps(receipt,indent=2))
print(json.dumps({'baseline_count':len(files),'negative_count':len(cases),'result':str(a.out/'result.json')}))
