"""Real registered native inventory + civitai.red gallery, isolated Atlas root.

No model installation/generation/native mutation. Credentials are referenced in
place from an explicit operator config, never copied or printed. Child enforces
GET-only HTTP at the client send boundary. Payloads/evidence remain PRIVATE.
"""
import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import httpx


def sha(raw): return hashlib.sha256(raw).hexdigest()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--operator-config',type=Path)
    p.add_argument('--keys',nargs=2)
    p.add_argument('--child',action='store_true')
    p.add_argument('--resume-owned',action='store_true',help='reuse this runner-owned root and exact immutable requests')
    a=p.parse_args();out=a.out.absolute()
    if a.child:
        send=httpx.Client.send
        def readonly(self,request,*args,**kwargs):
            if request.method!='GET':raise RuntimeError('read-only acceptance forbids upstream non-GET')
            return send(self,request,*args,**kwargs)
        httpx.Client.send=readonly
        from atlas_engine.server import main as server
        sys.argv=['atlas','--config',str(out/'atlas.json')];return server()
    from atlas_engine.config import Config
    from atlas_engine.upstream import Invoke
    from atlas_engine.onboarding import native_hash
    cfg=Config.load(a.operator_config);binding=cfg.connections[0]
    out.mkdir(parents=True,exist_ok=a.resume_owned,mode=0o700);os.umask(0o077)
    native=Invoke(binding.url,binding.token_file.read_text().strip() if binding.token_file else '')
    inventory=native.models();selected=[next(m for m in inventory if m['key']==key) for key in a.keys]
    assert {m['type'] for m in selected}=={'main','lora'}
    (out/'native-inventory.private.json').write_text(json.dumps(selected))
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    endpoint=f'http://127.0.0.1:{port}';token=secrets.token_urlsafe(32)
    (out/'clients.json').write_text(json.dumps([{'token':token,'principal':'acceptance','source':'real-public-gallery'}]))
    c=binding.model_dump(mode='json');c['enable_writes']=False
    (out/'atlas.json').write_text(json.dumps({'root':str(out/'state'),'clients_file':str(out/'clients.json'),
                                             'port':port,'connections':[c]}))
    payloads=[]
    with httpx.Client(timeout=60,trust_env=False,follow_redirects=False) as client:
        for m in selected:
            algorithm,value=native_hash(m)
            response=client.get('https://civitai.red/api/v1/model-versions/by-hash/'+value);response.raise_for_status();v=response.json()
            files=[f for f in v['files'] if f.get('hashes',{}).get(algorithm,'').lower()==value]
            assert len(files)==1
            f=files[0]
            payloads.append({'request_id':'real-'+m['type'],'registered_key':m['key'],'registered_path':m['path'],
                'model_id':v['modelId'],'version_id':v['id'],'file_id':f['id'],'file_name':f['name'],
                'hash_algorithm':algorithm,'file_hash':value,'gallery_limit':30,
                'connection_id':c['id'],'backend_generation':c['backend_generation']})
    (out/'payloads.private.json').write_text(json.dumps(payloads))
    client=httpx.Client(headers={'Authorization':'Bearer '+token},timeout=120,trust_env=False)
    responses=[];log=(out/'daemon.log').open('ab');proc=None
    def op(name,args):
        r=client.post(endpoint+'/v1/ops/'+name,json=args);r.raise_for_status();v=r.json()
        responses.append({'operation':name,'response':v});assert v['ok'],v;return v['result']
    def start():
        nonlocal proc
        proc=subprocess.Popen([sys.executable,'-I','-B',str(Path(__file__).absolute()),'--child','--out',str(out)],stdout=log,stderr=log)
        for _ in range(300):
            assert proc.poll() is None,'daemon exited'
            try:
                if op('status',{}):return
            except httpx.TransportError:pass
            time.sleep(.1)
        raise RuntimeError('not ready')
    def stop():
        if proc and proc.poll() is None:proc.terminate();proc.wait(40)
    async def mcp_get(jid):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        async with streamable_http_client(endpoint+'/mcp/',http_client=httpx.AsyncClient(headers=client.headers,timeout=120,trust_env=False)) as (rd,wr,_):
            async with ClientSession(rd,wr) as session:
                await session.initialize();r=await session.call_tool('models_onboard_get',{'job_id':jid})
                v=r.structuredContent or json.loads(r.content[0].text);assert v['ok'],v;return v['result']
    jobs=[]
    try:
        start()
        for payload in payloads:
            job=op('models.onboard',payload);deadline=time.monotonic()+1800
            while job['state'] not in ('succeeded','failed','interrupted') and time.monotonic()<deadline:
                time.sleep(2);job=op('models.onboard.get',{'job_id':job['job_id']})
                (out/('job-'+str(payload['version_id'])+'.private.json')).write_text(json.dumps(job))
            jobs.append(job)
            print(json.dumps({'version_id':payload['version_id'],'state':job['state'],'error':job.get('error'),'partial':job.get('partial'), 'author':job.get('author'), 'community':job.get('community')}),flush=True)
            assert job['state']=='succeeded',job.get('error')
            assert asyncio.run(mcp_get(job['job_id']))==job
            assert op('models.onboard',payload)==job
        stop();start()
        for payload,job in zip(payloads,jobs):
            assert op('models.onboard.get',{'job_id':job['job_id']})==job
            assert op('models.onboard',payload)==job
        # Whole acquired CAS is verified locally; public paging readback samples
        # every representation, not just the first model's first image.
        files=[]
        for path in (out/'state/corpus/files/sha256').glob('*/*'):
            data=path.read_bytes();assert sha(data)==path.name
            head=op('corpus.files.read',{'sha256':path.name,'offset':0,'size':32768})
            assert base64.b64decode(head['base64'])==data[:32768]
            files.append({'sha256':path.name,'size':len(data)})
        assert files
        (out/'result.json').write_text(json.dumps({'status':'partial' if any(j['partial'] for j in jobs) else 'pass','native':'real configured registered inventory; GET-only fence',
            'source':'real civitai.red and issued CDN URLs','versions':[x['version_id'] for x in payloads],
            'files':files,'coverage':[{k:j[k] for k in ('job_id','partial','author','community','saved_images')} for j in jobs],
            'checks':['both exact registered types','source-reported author/community coverage retained, no completion inferred','HTTP/MCP/replay/restart','CAS bytes and per-file public readback'],
            'limits':['no model installation or GPU','URL bytes are source-issued representations, not original-file guarantee']}))
    finally:
        stop();log.close();client.close();native.http.close()
        (out/('responses-'+str(time.time_ns())+'.private.json')).write_text(json.dumps(responses))
        (out/'cleanup.json').write_text(json.dumps({'owned_running':bool(proc and proc.poll() is None)}))


if __name__=='__main__':main()
