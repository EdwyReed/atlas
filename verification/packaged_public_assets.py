"""Installed-image public compatibility regression on owned disposable volumes.

No real state/config/key mounts, model work, native POST or production lifecycle.
The policy=true branch must start without an external public_assets_dir override.
"""
import argparse
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import re
import socket
import subprocess
import time
import uuid
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def run(*args, input=None, check=True):
    return subprocess.run(list(map(str, args)), input=input, text=True,
                          capture_output=True, check=check, timeout=120)


SEED = '''import gc,io,json,os,sqlite3
from pathlib import Path
from PIL import Image
from atlas_engine.engine import Engine
from atlas_engine.upstream import Invoke,Atlas,unconfigured
from atlas_engine import portable as pt,state_backup as sb,adoption_control as ctl
r=Path('/var/lib/atlas/seed')
e=Engine(r,None,unconfigured(Invoke,'OFFLINE'),unconfigured(Atlas,'OFFLINE'),corpus_root=r/'corpus')
for i in range(7):
 j,_=e.repo.reserve('fixture-owner','fixture-client',str(i),'generation',{'request_id':str(i)})
 e.repo.update(j['id'],'observing' if i%2 else 'ack_unknown',{})
b=io.BytesIO();Image.new('RGB',(8,8),'red').save(b,format='PNG')
e.artifacts.capture_upload(j['id'],'history.png',b.getvalue())
e.close();del e;gc.collect()
with sqlite3.connect(r/'registry.sqlite3') as db:
 db.execute('pragma wal_checkpoint(TRUNCATE)');db.execute('pragma journal_mode=DELETE')
(r/'service.lock').touch()
pt.pack(r,Path('/var/lib/atlas/capture.tar.gz'))
t=Path('/var/lib/atlas/state');pt.unpack(Path('/var/lib/atlas/capture.tar.gz'),t)
s=sb.restore_state(t);p=Path('/var/lib/atlas/activation.json')
sb.write_json(p,dict(target_root=str(t),operator='disposable regression',assessment='retain seven synthetic uncertain rows without replay',retain_historical_effects=True,runtime=sb.versions(),backup_manifest_sha256=s['backup_manifest_sha256'],registry_logical_sha256=sb.registry_digest(t)))
assert pt.activate(t,p)['held']==7
ctl.atomic(Path('/var/lib/atlas/holds.json'),pt.protected_rows(t))
for d,ds,fs in os.walk('/var/lib/atlas'):
 os.chown(d,10001,10001)
 for n in fs:os.chown(Path(d)/n,10001,10001)
'''

READBACK = '''import json,sqlite3
from pathlib import Path
from atlas_engine import portable as pt,adoption_control as ctl
r=Path('/var/lib/atlas/state');pins=pt.protected_rows(r)
assert pins==ctl.read(Path('/var/lib/atlas/holds.json')) and len(pins)==7
assert (r/'.portable-runtime-admitted.json').is_file()
with sqlite3.connect('file:'+str(r/'registry.sqlite3')+'?mode=ro',uri=True) as db:
 db.row_factory=sqlite3.Row
 rows=[dict(x) for x in db.execute('select * from jobs order by id')]
print(json.dumps(dict(held=pins,full_jobs=rows,witness=True)))
'''


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--image', required=True)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    a.out.mkdir(parents=True, mode=0o700, exist_ok=False)
    name = 'atlas-assets-check-' + uuid.uuid4().hex[:12]
    state, config = name+'-state', name+'-config'
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    token = 'synthetic-fixture-only-0123456789abcdef'
    settings = dict(root='/var/lib/atlas/state', clients_file='/run/atlas/clients.json',
                    host='0.0.0.0', port=12088, container_endpoint=url,
                    public_anonymous_compatibility=True, projection_dir=None,
                    policy_file='/run/atlas/policy.json',media_config_file='/run/atlas/media.json',
                    connections=[dict(id='fixture', backend_generation='fixture-v1',
                                      account_scope='fixture', url='http://127.0.0.1:1',
                                      token_file=None, enable_writes=True)])
    payload = {'atlas.json': json.dumps(settings),
               'clients.json': json.dumps([dict(token=token,principal='fixture-owner',source='fixture-client')]),
               'media.json':json.dumps(dict(mask=dict(root='/var/lib/atlas/synthetic-mask'),censor=dict(python='/synthetic/no-execution/python',model='/synthetic/no-execution/model',upstream='/synthetic/no-execution/upstream'))),
               'policy.json': json.dumps(dict(auto_fetch_artifacts=False,native_owners={},principals={'fixture-owner':dict(projects=[None],shared_projects=[],boards=['synthetic-board'],managed_boards=['synthetic-board'],native_admin=True,board_admin=True,board_publish=True,board_delete_images=True,model_admin=True,windows_lora_install=True,native_user_id='fixture-user',model_sources=[])},windows_lora_installer=dict(ssh_host='fixture.invalid',python='/synthetic/no-execution/python'),board_create_private_verified=True))}
    doc = dict(name=name,services={'atlas':dict(image=a.image,user='10001:10001',
        read_only=True,cap_drop=['ALL'],security_opt=['no-new-privileges:true'],
        cpus=2,mem_limit='2g',pids_limit=128,init=True,restart='no',stop_grace_period='30s',
        ports=[f'127.0.0.1:{port}:12088'],tmpfs=['/tmp:rw,noexec,nosuid,size=64m'],
        volumes=[state+':/var/lib/atlas',config+':/run/atlas:ro'])},
        volumes={state:dict(external=True),config:dict(external=True)},
        networks={'default':dict(driver='bridge')})
    compose = a.out/'compose.json'
    compose.write_text(json.dumps(doc,indent=2)+'\n')
    (a.out/'synthetic-config.json').write_text(json.dumps(settings,indent=2)+'\n')
    dc = ['docker','compose','-f',str(compose)]
    created = []
    def op(operation, args=None, headers=None, prefix='/v1/ops/'):
        response=httpx.post(url+prefix+operation,json=args or {},headers=headers,trust_env=False,timeout=20)
        response.raise_for_status();value=response.json();assert value['ok'],value
        return value['result']
    async def mcp_check(path, headers):
        async with httpx.AsyncClient(headers=headers,trust_env=False,timeout=20) as client:
            async with streamable_http_client(url+path,http_client=client) as (rd,wr,_):
                async with ClientSession(rd,wr) as session:
                    await session.initialize()
                    v=(await session.call_tool('jobs_list',{'limit':100})).structuredContent
                    assert v['ok'] and len(v['result'])==7,v
                    return True
    def ready():
        for _ in range(200):
            try:
                if op('status')['worker_alive']:return
            except (httpx.TransportError,KeyError):pass
            time.sleep(.1)
        raise AssertionError(run(*dc,'logs').stdout)
    def verify():
        ready()
        statuses=[]
        for headers in ({},{'Authorization':'Bearer unknown-fixture'}, {'Authorization':'Bearer '+token}):
            status=op('status',headers=headers)
            assert status['held_intents']['count']==7 and status['held_intents']['historical_immutable']
            assert len(op('jobs.list',{'limit':100},headers))==7
            assert len(op('jobs.list',{'limit':100},headers,prefix='/atlas/v1/ops/'))==7
            assert httpx.get(url+'/atlas/v1/schema',headers=headers,trust_env=False).status_code==200
            for route in ('/atlas','/atlas/'):
                r=httpx.get(url+route,headers=headers,trust_env=False)
                assert r.status_code==200 and r.content==(a.source/'atlas_engine/public_assets/index.html').read_bytes()
                assert r.headers['cache-control']=='no-cache'
            for path in (a.source/'atlas_engine/public_assets').iterdir():
                r=httpx.get(url+'/atlas/assets/'+path.name,headers=headers,trust_env=False)
                assert r.status_code==200 and r.content==path.read_bytes(),path
            statuses.append(dict(anonymous=not headers,status='pass'))
        redirect=httpx.get(url+'/',trust_env=False);assert redirect.status_code==307 and redirect.headers['location']=='/atlas/'
        html=(a.source/'atlas_engine/public_assets/index.html').read_text()
        for path,digest in re.findall(r'(?:src|href)="([^"?]+)\?version=([a-f0-9]+)"',html):
            assert sha(httpx.get(url+path,trust_env=False).content)==digest
        public=httpx.post(url+'/atlas/api/operation',json={'operation':'jobs.list','arguments':{'limit':100}},trust_env=False)
        assert public.status_code==200 and len(public.json())==7
        assert httpx.get(url+'/atlas/api/media/not-a-digest',trust_env=False).status_code==422
        jobs=op('jobs.list',{'limit':100});artifacts=[]
        for j in jobs:
            artifacts+=op('artifact.list',{'job_id':j['id']})
        assert len(artifacts)==1
        for art in artifacts:
            part=op('artifact.read',{'artifact_id':art['artifact_id'],'offset':0,'size':32768})
            assert sha(base64.b64decode(part['base64']))==art['sha256']
        assert asyncio.run(mcp_check('/mcp/',{'Authorization':'Bearer '+token}))
        assert asyncio.run(mcp_check('/atlas/mcp/',{}))
        cid=run(*dc,'ps','-q','atlas').stdout.strip()
        rb=json.loads(run('docker','exec',cid,'python','-I','-B','-c',READBACK).stdout)
        info=json.loads(run('docker','inspect',cid).stdout)[0];hc=info['HostConfig']
        assert info['Image']==a.image and info['Config']['User']=='10001:10001'
        assert hc['ReadonlyRootfs'] and hc['CapDrop']==['ALL'] and hc['Memory']==2147483648 and hc['NanoCpus']==2000000000 and hc['PidsLimit']==128
        assert any(x in ('no-new-privileges','no-new-privileges:true') for x in hc['SecurityOpt'])
        assert hc['Init'] and all(m['Name'] in (state,config) for m in info['Mounts'] if m['Type']=='volume')
        cli="import json,subprocess;from pathlib import Path;p=Path('/tmp/fixture-token');p.write_text('synthetic-fixture-only-0123456789abcdef');p.chmod(0o600);r=subprocess.run(['atlas-client','jobs.list','--arguments','{\"limit\":100}','--endpoint','http://127.0.0.1:12088','--token-file',str(p)],capture_output=True,text=True);p.unlink();assert r.returncode==0,(r.stdout,r.stderr);v=json.loads(r.stdout);assert v['ok'] and len(v['result'])==7;print('CLI_PASS')"
        result=run('docker','exec',cid,'python','-I','-B','-c',cli,check=False)
        assert result.returncode==0,result.stderr
        return dict(auth= statuses,readback=rb,artifact_count=len(artifacts),mcp=True,installed_cli=True,resource_security_envelope=True)
    auth_probe = '''import json
from starlette.testclient import TestClient
from atlas_engine.server import make_app
class Spy:
 def call(self,op,args,principal,source):return {'ok':True,'result':{'principal':principal,'source':source}}
clients=[{'token':'synthetic-fixture-only-0123456789abcdef','principal':'fixture-owner','source':'fixture-client'}]
for enabled in (False,True):
 client=TestClient(make_app(Spy(),clients,12088,public_anonymous=enabled),base_url='http://127.0.0.1:12088')
 for auth,expected in [({},'anonymous'),({'Authorization':'Bearer unknown'},'anonymous'),({'Authorization':'Bearer '+clients[0]['token']},'fixture-owner')]:
  r=client.post('/v1/ops/status',json={},headers=auth)
  if enabled or expected=='fixture-owner':
   assert r.status_code==200 and r.json()['result']['principal']==expected
   if enabled:
    p=client.post('/atlas/api/operation',json={'operation':'status'},headers=auth)
    assert p.status_code==200 and p.json()['principal']==expected
  else:assert r.status_code==401
 r=client.post('/v1/ops/status',json={},headers={'Host':'fixture.invalid','Origin':'https://fixture.invalid'})
 assert r.status_code==(200 if enabled else 403)
print(json.dumps({'strict_and_compatibility_auth':True,'known_identity_attributed':True,'unknown_anonymous_only_under_explicit_compatibility':True}))
'''
    auth=run('docker','run','--rm','--network','none','--read-only','--cap-drop','ALL',
             '--security-opt','no-new-privileges','--tmpfs','/var/lib/atlas:rw,noexec,nosuid,size=1m',
             '--entrypoint','python',a.image,'-I','-B','-c',auth_probe)
    (a.out/'auth-policy.json').write_text(auth.stdout)
    try:
        for v in (state,config):
            run('docker','volume','create',v);created.append(v)
        seed=run('docker','run','--rm','--network','none','--read-only','--user','0:0',
            '--cap-drop','ALL','--cap-add','CHOWN','--cap-add','DAC_OVERRIDE','--security-opt','no-new-privileges',
            '--tmpfs','/tmp:rw,noexec,nosuid,size=64m','-v',state+':/var/lib/atlas',
            '--entrypoint','python',a.image,'-I','-B','-c',SEED)
        (a.out/'seed.log').write_text(seed.stdout+seed.stderr)
        setup="import json,os,sys;from pathlib import Path;p=Path('/run/atlas');data=json.load(sys.stdin);[( (p/k).write_text(v),os.chmod(p/k,0o600),os.chown(p/k,10001,10001)) for k,v in data.items()];os.chmod(p,0o700);os.chown(p,10001,10001)"
        run('docker','run','--rm','-i','--network','none','--read-only','--user','0:0',
            '--cap-drop','ALL','--cap-add','CHOWN','--cap-add','DAC_OVERRIDE','--security-opt','no-new-privileges',
            '-v',config+':/run/atlas','--entrypoint','python',a.image,'-I','-B','-c',setup,input=json.dumps(payload))
        run(*dc,'up','-d','--pull','never')
        first=verify();run(*dc,'restart','atlas');second=verify()
        assert first['readback']==second['readback'],'held/full historical rows changed on restart'
        (a.out/'result.json').write_text(json.dumps(dict(passed=True,image=a.image,public_anonymous_compatibility=True,first=first,second=second,restart=True,no_native_or_model_effects=True),indent=2)+'\n')
        print('PUBLIC_ASSETS_INSTALLED_COMPOSE_PASS',a.image)
    finally:
        logs=run(*dc,'logs',check=False);(a.out/'container.log').write_text(logs.stdout+logs.stderr)
        run(*dc,'down',check=False)
        for v in created:run('docker','volume','rm',v)
        assert all(run('docker','volume','inspect',v,check=False).returncode for v in created)
        assert not run('docker','ps','-aq','--filter','label=com.docker.compose.project='+name).stdout.strip()
        (a.out/'cleanup.json').write_text(json.dumps(dict(removed=created,containers_absent=True))+'\n')


if __name__ == '__main__':
    main()
