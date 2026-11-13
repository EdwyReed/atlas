"""Installed narrow binding/index/collision regression; synthetic HTTP image bytes.
Original two-server execution is in multi_connection.py, not claimed here.
"""
import argparse, hashlib, io, json
from copy import deepcopy
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import httpx
from PIL import Image
from atlas_engine.config import Config, Connection, bind_connection, bind_connections
from atlas_engine.engine import Engine
from atlas_engine.connections import install, select, job_context
from atlas_engine.contracts import SCHEMAS, Fault
from atlas_engine.repository import dump
from atlas_engine.upstream import Invoke, Atlas, unconfigured

p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False)
root=a.out/'state';root.mkdir();checks=[]
ca=Connection(id='A',url='http://owned-a',backend_generation='native-A',account_scope='operator-A',enable_writes=True)
cb=Connection(id='B',url='http://owned-b',backend_generation='native-B',account_scope='operator-B',enable_writes=True)
bind_connection(root,ca);legacy=(root/'connection-binding.json').read_bytes()
e=Engine(root,None,unconfigured(Invoke,'INVOKE_UNCONFIGURED'),unconfigured(Atlas,'REFERENCE_UNCONFIGURED'),True,corpus_root=root/'corpus')
oldp=SCHEMAS['generation.submit'].model_validate({'model':'absent','request_id':'legacy'}).model_dump();oldp={k:v for k,v in oldp.items() if k not in ('connection_id','backend_generation')}
old,_=e.repo.reserve('p','fixture','legacy','generation',oldp)
e.repo.update(old['id'],'succeeded',{'outputs':['same.png']});e.repo.native({'item_id':1},old['id'])
record=e.repo.get(old['id']);events=e.repo.connect().execute('select * from events').fetchall()
corpus=e.call('corpus.put',{'request_id':'corpus','collection':'tags','identity':{'tag':'preserved'},'expected_revision':0,'body':{'literal':'preserved'}});assert corpus['ok'],corpus
history=e.call('corpus.history',{'collection':'tags','identity':{'tag':'preserved'}})
try:bind_connections(root,[ca,cb])
except ValueError:pass
else:raise AssertionError('legacy migration was implicit')
try:bind_connections(root,[ca],True)
except ValueError as error:assert 'active corpus owner' in str(error)
else:raise AssertionError('migration ignored corpus owner')
assert not (root/'connection-bindings.json').exists() and (root/'connection-binding.json').read_bytes()==legacy
e.close()
from unittest.mock import patch
original_replace=Path.replace
for boundary in ('fsync','replace'):
    target='atlas_engine.config.os.fsync' if boundary=='fsync' else 'pathlib.Path.replace'
    with patch(target,side_effect=OSError('injected before manifest publication')):
        try:bind_connections(root,[ca],True)
        except OSError:pass
        else:raise AssertionError('injected manifest failure ignored')
    assert not (root/'connection-bindings.json').exists() and (root/'connection-binding.json').read_bytes()==legacy
    assert not (root/'connection-bindings.new').exists()
manifest=bind_connections(root,[ca],True);assert manifest['legacy_binding']['account_scope']=='operator-A'
try:bind_connections(root,[ca,cb])
except ValueError:pass
else:raise AssertionError('addition was implicit')
manifest=bind_connections(root,[ca,cb],True);assert manifest['legacy_connection_id']=='A' and manifest['additions']==[['B']]
e=Engine(root,None,unconfigured(Invoke,'INVOKE_UNCONFIGURED'),unconfigured(Atlas,'REFERENCE_UNCONFIGURED'),True,corpus_root=root/'corpus')
assert (root/'connection-binding.json').read_bytes()==legacy and e.repo.get(old['id'])==record
assert e.call('corpus.history',{'collection':'tags','identity':{'tag':'preserved'}})==history
checks.append('explicit legacy/additive fsynced receipts; original binding/jobs/canonical history unchanged')
for key in ('id','url','backend_generation','account_scope','token_file'):
    bad=ca.model_copy(update={key:'changed' if key!='token_file' else Path('/owned/changed-token')})
    try:bind_connections(root,[bad,cb],True)
    except ValueError:pass
    else:raise AssertionError(key)
try:Config(root=root,clients_file=Path('clients'),connections=[ca,ca])
except ValueError:pass
else:raise AssertionError('duplicate ID')
checks.append('all immutable binding fields and duplicate ID rejected before clients exist')
clients={};requests=[];raws={}
for key,color in [('A',(17,2,3)),('B',(4,29,6))]:
    b=io.BytesIO();Image.new('RGB',(2,3),color).save(b,format='PNG');raws[key]=b.getvalue()
    def transport(request,key=key):
        requests.append((key,request.url.path))
        if request.url.path.endswith('/full'):return httpx.Response(200,content=raws[key])
        if request.url.path=='/api/v2/models/':return httpx.Response(200,json={'models':[{'key':key,'name':key,'type':'main'}]})
        raise AssertionError((key,request.url.path))
    inv=Invoke('http://owned-'+key.lower());inv.http=httpx.Client(base_url=inv.base,transport=httpx.MockTransport(transport));clients[key]=inv
config=Config(root=root,clients_file=Path('clients'),connections=[ca,cb],compatibility_routing=True,default_connection_id='B')
install(e,config,manifest,clients)
assert job_context(e,record).connection_id=='A'
replay=e.call('generation.submit',oldp,'p');assert replay['ok'] and replay['result']['id']==old['id'] and requests==[],replay
conflict=e.call('generation.submit',{**oldp,'connection_id':'B'},'p');assert not conflict['ok'] and conflict['error']['code']=='IDEMPOTENCY_CONFLICT' and requests==[],conflict
assert e.repo.observed_job(1,e.connection_contexts['A'].native_queue.scope())==old['id']
checks.append('default B does not rebind unscoped persisted A intent; exact legacy replay without I/O; changed-scope conflict; old observation preserved under original scope')
for key in ('A','B'):
    v=e.connection_contexts[key]
    assert v.repo is e.repo and v.corpus is e.corpus and v.projection is e.projection and v.stop is e.stop
    assert all(getattr(v,n).e is v for n in ('onboarding','workspace','native_admin','native_queue'))
    j,_=e.repo.reserve('p','fixture','image-'+key,'generation',{'connection_id':key,'backend_generation':'native-'+key},{'native_scope':v.native_queue.scope(),'outputs':['same.png']})
    art=e.call('artifact.fetch',{'job_id':j['id'],'image_name':'same.png'});assert art['ok'],art
    assert art['result']['sha256']==hashlib.sha256(raws[key]).hexdigest()
    if key=='A':first=(j,art)
assert [k for k,path in requests]==['A','B'],requests
clients['A'].http.close()
assert e.call('artifact.fetch',{'job_id':first[0]['id'],'image_name':'same.png'})==first[1]
checks.append('one repo/corpus/stop owner; each helper bound immutably; SAME image name on A/B yields different correct bytes; cached fetch works without A client')
# Reopen A for a concurrent request test of backend context isolation.
def inventory_call(i):
    key='A' if i%2==0 else 'B';view,args=select(e,'catalog.list',{'connection_id':key},'p');return view.connection_id,view.invoke.base
with ThreadPoolExecutor(8) as pool:
    results=list(pool.map(inventory_call,range(80)))
assert all(url=='http://owned-'+key.lower() for key,url in results)
checks.append('80 concurrent selections retain their own immutable client; no mutable current backend')
assert e.repo.get(old['id'])==record and e.call('corpus.history',{'collection':'tags','identity':{'tag':'preserved'}})==history
from atlas_engine.state_backup import INCLUDED
assert 'connection-bindings.json' in INCLUDED
for client in clients.values():client.http.close()
e.close()
# Logical token references survive config relocation, but a different logical
# reference remains a rebinding even when it happens to hold identical bytes.
portable=a.out/'portable';portable.mkdir();(portable/'state').mkdir()
configuration={'root':'state','clients_file':'clients.json','connections':[{**ca.model_dump(mode='json'),'token_file':'native.token'}]}
(portable/'atlas.json').write_text(json.dumps(configuration));(portable/'native.token').write_text('owned-fixture-secret');(portable/'native.token').chmod(0o600)
pc=Config.load(portable/'atlas.json');pm=bind_connections(pc.root,pc.connections)
assert pm['connections']['A']['token_file']=='native.token'
# Model the earlier unpublished absolute-reference candidate and explicitly
# adopt logical refs while the resolved original credential location is exact.
absolute=deepcopy(pm);absolute['connections']['A']['token_file']=str(pc.connections[0].token_file)
(pc.root/'connection-bindings.json').write_text(json.dumps(absolute))
try:bind_connections(pc.root,pc.connections)
except ValueError:pass
else:raise AssertionError('absolute candidate migrated implicitly')
pm=bind_connections(pc.root,pc.connections,True);assert len(pm['token_reference_migrations'])==1
stable=(pc.root/'connection-bindings.json').read_bytes()
with patch('pathlib.Path.replace',side_effect=OSError('injected additive publication failure')):
    try:bind_connections(pc.root,[*pc.connections,cb],True)
    except OSError:pass
    else:raise AssertionError('additive write failure ignored')
assert (pc.root/'connection-bindings.json').read_bytes()==stable
for malformed in ({'format':'future-v3','connections':{}},{'connections':{}},[]):
    (pc.root/'connection-bindings.json').write_text(json.dumps(malformed))
    try:bind_connections(pc.root,pc.connections,True)
    except ValueError as error:assert 'unsupported connection binding manifest format' in str(error)
    else:raise AssertionError('unknown manifest admitted')
(pc.root/'connection-bindings.json').write_bytes(stable)
checks.append('corpus-owner publication refusal; pre-publication fsync/replace and additive failure preserve prior manifest/legacy bytes; unknown manifest refuses cleanly; explicit absolute-candidate logical-reference migration')
(a.out/'result.json').write_text(json.dumps({'ok':True,'checks':checks,'requests':requests},indent=2));print(json.dumps({'ok':True,'checks':checks},indent=2))
