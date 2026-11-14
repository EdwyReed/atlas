"""Real local BGE -> canonical Qdrant -> authenticated HTTP/MCP/restart.

Owned populated synthetic corpus, real CPU encoder, no native server or downloads.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--model-cache',type=Path,required=True)
    p.add_argument('--docker-image')
    p.add_argument('--extra-heads',type=int,default=0)
    p.add_argument('--reuse-owned-state',action='store_true',help='reuse exact owned seed after interrupted acceptance, no reimport')
    a=p.parse_args();out=a.out.absolute();out.mkdir(parents=True,exist_ok=a.reuse_owned_state,mode=0o700)
    root=out/'state';root.mkdir(exist_ok=a.reuse_owned_state)
    if not a.reuse_owned_state:shutil.copytree(a.model_cache,root/'cache'/'fastembed',symlinks=False)
    assert not (a.reuse_owned_state and a.docker_image),'reuse applies only to the owned host root'
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    token=secrets.token_urlsafe(32);headers={'Authorization':'Bearer '+token}
    clients=out/'clients.json';clients.write_text(json.dumps([{'token':token,'principal':'fixture','source':'semantic'}]));clients.chmod(0o600)
    config=out/'atlas.json';config.write_text(json.dumps({'root':str(root),'clients_file':str(clients),'port':port}))
    endpoint=f'http://127.0.0.1:{port}'
    container=None
    if a.docker_image:
        import hashlib
        import atlas_engine
        sys.path.insert(0,str(Path(__file__).absolute().parent))
        from container_lifecycle import Container
        modules={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(atlas_engine.__file__).parent.glob('*.py')}
        container=Container(a.docker_image,out,port,token,modules)
        container.configure({'root':'/var/lib/atlas','clients_file':'/run/atlas/clients.json',
            'host':'0.0.0.0','port':12088,'container_endpoint':endpoint})
        code='import os,shutil;shutil.copytree("/input", "/var/lib/atlas/cache/fastembed",symlinks=False);[(os.chown(p,10001,10001)) for r,ds,fs in os.walk("/var/lib/atlas") for p in [r]+[os.path.join(r,f) for f in fs]]'
        container.run('run','--rm','--network','none','--user','0:0','-v',container.state+':/var/lib/atlas',
            '-v',str(root/'cache'/'fastembed')+':/input:ro','--entrypoint','python',a.docker_image,'-c',code)
    env={**os.environ,'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','CUDA_VISIBLE_DEVICES':''}
    client=httpx.Client(headers=headers,timeout=600,trust_env=False)
    def op(name,args):
        r=client.post(endpoint+'/v1/ops/'+name,json=args)
        r.raise_for_status();j=r.json();assert j['ok'],j;return j['result']
    async def mcp_search(args):
        async with streamable_http_client(endpoint+'/mcp/',http_client=httpx.AsyncClient(headers=headers,trust_env=False,timeout=90)) as (rd,wr,_):
            async with ClientSession(rd,wr) as session:
                await session.initialize();r=await session.call_tool('search',args)
                j=r.structuredContent or json.loads(r.content[0].text);assert j['ok'],j;return j['result']
    log=(out/'daemon.log').open('ab')
    proc=None
    def start():
        nonlocal proc
        proc=container.start() if container else subprocess.Popen([sys.executable,'-I','-B','-m','atlas_engine.server','--config',str(config)],stdout=log,stderr=log,env=env)
        for _ in range(300):
            assert proc.poll() is None,'daemon exited'
            try:
                if op('status',{}):return
            except httpx.TransportError:pass
            time.sleep(.1)
        raise AssertionError('not ready')
    def stop():
        if proc and proc.poll() is None:proc.terminate();proc.wait(40)
    try:
        start()
        bodies=['A fluffy kitten is sleeping on a warm cushion.','A fast automobile races along the highway.','Fresh bread is baking in the kitchen.']
        for i,body in enumerate(bodies):
            if a.reuse_owned_state:break
            op('corpus.put',{'request_id':'put-'+str(i),'collection':'tags','identity':{'tag':str(i)},'expected_revision':0,'body':{'description':body}})
        for i in range(a.extra_heads):
            if a.reuse_owned_state:break
            op('corpus.put',{'request_id':'extra-'+str(i),'collection':'tags','identity':{'tag':'extra-'+str(i)},'expected_revision':0,
                            'body':{'description':'Volcanic geological sediment layers and tectonic rock formations.'}})
        count=3+a.extra_heads
        query={'query':'a cat resting on a pillow','mode':'semantic','limit':3}
        begin=time.monotonic()
        first=op('search',query);dense=first['canonical_semantic']
        cold_seconds=time.monotonic()-begin
        assert dense['indexed']==count and dense['complete'] and len(dense['model_files_sha256'])==64,dense
        assert dense['items'][0]['record']['identity']['tag']=='0',dense
        assert asyncio.run(mcp_search(query))==first
        snapshot=dense['snapshot_sha256'];stop()
        # Derived cache corruption must rebuild, never poison authoritative
        # records or silently change ranking. Works on the owned volume too.
        if container:
            code='from pathlib import Path;p=next(Path("/var/lib/atlas/cache/canonical-vectors").glob("*/*"));p.write_bytes(b"corrupt disposable cache")'
            container.run('run','--rm','--network','none','-v',container.state+':/var/lib/atlas',
                          '--entrypoint','python',a.docker_image,'-I','-c',code)
        else:
            next((root/'cache/canonical-vectors').glob('*/*')).write_bytes(b'corrupt disposable cache')
        start()
        begin=time.monotonic();again=op('search',query);warm_seconds=time.monotonic()-begin
        assert again['canonical_semantic']['snapshot_sha256']==snapshot
        assert again['semantic']==first['semantic']
        # Delete all derived vectors and alter an inert local model-snapshot
        # notice: the file fingerprint must select a NEW cache namespace even
        # when the effective encoder and authoritative corpus are unchanged.
        stop()
        code='''from pathlib import Path
import shutil
r=Path(ROOT)
shutil.rmtree(r/'cache/canonical-vectors')
p=next((r/'cache/fastembed/models--qdrant--bge-small-en-v1.5-onnx-q/snapshots').iterdir())
(p/'atlas-cache-namespace.txt').write_text('owned acceptance namespace witness')
'''
        if container:
            container.run('run','--rm','--network','none','-v',container.state+':/var/lib/atlas',
                '--entrypoint','python',a.docker_image,'-I','-c',code.replace('ROOT',repr('/var/lib/atlas')))
        else:
            exec(code.replace('ROOT',repr(str(root))))
        start();namespaced=op('search',query)
        assert namespaced['semantic']==first['semantic']
        assert namespaced['canonical_semantic']['snapshot_sha256']==snapshot
        assert namespaced['canonical_semantic']['model_files_sha256']!=dense['model_files_sha256']
        # Read-only/broken cache cannot make derived search unavailable.
        stop()
        code='from pathlib import Path;import shutil;p=Path(ROOT)/"cache/canonical-vectors";shutil.rmtree(p);p.write_text("unwritable cache witness")'
        if container:
            container.run('run','--rm','--network','none','-v',container.state+':/var/lib/atlas',
                '--entrypoint','python',a.docker_image,'-I','-c',code.replace('ROOT',repr('/var/lib/atlas')))
        else:
            exec(code.replace('ROOT',repr(str(root))))
        start();assert op('search',query)['semantic']==first['semantic']
        op('corpus.put',{'request_id':'revise','collection':'tags','identity':{'tag':'0'},'expected_revision':1,'body':{'description':'A submarine travels deep underwater.'}})
        updated=op('search',query)['canonical_semantic']
        assert updated['snapshot_sha256']!=snapshot
        assert all(x['record']['revision']==2 for x in updated['items'] if x['record']['identity']['tag']=='0')
        literal=op('search',{'query':'submarine','mode':'literal','limit':3})
        assert literal['corpus']['items'][0]['body']['description'].startswith('A submarine')
        hybrid=op('search',{'query':'automobile','mode':'hybrid','limit':3})
        assert hybrid['corpus']['items'] and hybrid['canonical_semantic']['indexed']==count
        if container:container.verify()
        result={'status':'pass','encoder':'real local BGE CPU','synthetic_corpus':True,'indexed':count,
                'cold_seconds':cold_seconds,'restart_seconds':warm_seconds,
                'checks':['complete canonical semantic ranking','HTTP/SDK MCP equality','corrupt-cache rebuild with exact scores and snapshot','cache deletion and model fingerprint namespace invalidation','unwritable derived cache does not disable search','revision invalidation/no stale records','literal and hybrid remain separate'], 'first':first,'updated':updated}
        if container:result['docker_image_id']=json.loads((out/'container.json').read_text())['image_id']
        (out/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps({k:v for k,v in result.items() if k not in ('first','updated')}))
    finally:
        stop();log.close();client.close()
        if container:container.cleanup()
        else:(out/'cleanup.json').write_text(json.dumps({'owned_running':bool(proc and proc.poll() is None)}))


if __name__=='__main__':main()
