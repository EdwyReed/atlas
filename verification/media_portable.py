"""Installed public CPU censor acceptance. Explicit external read-only model binding.
Safe synthetic input proves execution, not detector quality. Slow stand-in is separate.
"""
import argparse,asyncio,base64,hashlib,io,json,os,secrets,socket,subprocess,sys,time
from pathlib import Path
import httpx
from PIL import Image,ImageDraw
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import atlas_engine


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--python',required=True)
    p.add_argument('--upstream',required=True)
    p.add_argument('--model',required=True)
    a=p.parse_args();out=a.out.absolute();out.mkdir(parents=True,exist_ok=False,mode=0o700)
    token=secrets.token_urlsafe(36)
    (out/'clients.json').write_text(json.dumps([{'token':token,'principal':'media-check','source':'synthetic'}]));(out/'clients.json').chmod(0o600)
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    endpoint=f'http://127.0.0.1:{port}';headers={'Authorization':'Bearer '+token}
    config={'root':'state','clients_file':'clients.json','port':port,'media_config_file':'media.json'}
    (out/'atlas.json').write_text(json.dumps(config))
    binding={'python':a.python,'upstream':a.upstream,'model':a.model,'timeout_seconds':90}
    hashes={k:hashlib.sha256(Path(v).read_bytes()).hexdigest() for k,v in [('model',a.model),('renderer',str(Path(a.upstream)/'mosaic.py'))]}
    (out/'media.json').write_text(json.dumps({'censor':binding}))
    env={k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME','VIRTUAL_ENV')}
    home=out/'home';home.mkdir();env.update(HOME=str(home),HF_HUB_OFFLINE='1')
    log=(out/'daemon.log').open('wb');proc=None;checks=[];timings={}
    def op(name,args=None):
        r=httpx.post(endpoint+'/v1/ops/'+name,json=args or {},headers=headers,trust_env=False,timeout=10);r.raise_for_status();return r.json()
    def ok(name,args=None):
        r=op(name,args);assert r['ok'],r;return r['result']
    def start():
        child=subprocess.Popen([sys.executable,'-I','-B','-m','atlas_engine.server','--config',str(out/'atlas.json')],cwd=out,env=env,stdout=log,stderr=log)
        for _ in range(150):
            assert child.poll() is None,'daemon exited'
            try:
                if ok('status')['worker_alive']:return child
            except httpx.HTTPError:pass
            time.sleep(.1)
        raise AssertionError('startup timeout')
    def stop():
        nonlocal proc
        t=time.monotonic();proc.terminate();assert proc.wait(timeout=15) in (0,-15);proc=None;return time.monotonic()-t
    def poll(jid,terminal=True):
        for _ in range(900):
            j=ok('censor.get',{'job_id':jid})
            if (j['state'] in ('succeeded','partially_succeeded','failed')) if terminal else j['state']=='running':return j
            time.sleep(.1)
        raise AssertionError('poll timeout')
    async def mcp(name,args):
        async with streamable_http_client(endpoint+'/mcp/',http_client=httpx.AsyncClient(headers=headers,trust_env=False)) as (read,write,_):
            async with ClientSession(read,write) as session:
                await session.initialize();r=await session.call_tool(name,args);assert not r.isError,r;return r.structuredContent
    try:
        proc=start()
        image=Image.new('RGB',(320,240),'#a7d7ef');d=ImageDraw.Draw(image);d.rectangle((30,80,180,190),fill='#327946');d.ellipse((210,25,270,85),fill='#ffe87a')
        b=io.BytesIO();image.save(b,format='PNG');raw=b.getvalue();digest=hashlib.sha256(raw).hexdigest();(out/'input.png').write_bytes(raw)
        u=ok('media.upload.begin',{'request_id':'image','size':len(raw),'sha256':digest,'role':'image'})
        ok('media.upload.chunk',{'upload_id':u['upload_id'],'offset':0,'base64':base64.b64encode(raw).decode()})
        assert ok('media.upload.finalize',{'upload_id':u['upload_id']})['sha256']==digest
        payload={'request_id':'real-cpu','images':[{'upload_id':u['upload_id'],'name':'safe.png'}]}
        t=time.monotonic();receipt=ok('censor.process',payload);timings['actual_submit']=time.monotonic()-t
        assert receipt['state']=='queued' and receipt['media_inputs'][0]['sha256']==digest
        running=poll(receipt['id'],False);assert running['state']=='running'
        result=poll(receipt['id']);assert result['state']=='succeeded',result
        assert result['result']['device']=='cpu' and result['result']['inference_images']==1
        row=result['result']['items'][0];assert (row['width'],row['height'])==(320,240) and row['input_sha256']==digest
        arts=ok('artifact.list',{'job_id':receipt['id']});assert len(arts)==2,arts
        art=next(x for x in arts if x['image_name']=='safe.png')
        part=ok('artifact.read',{'artifact_id':art['artifact_id'],'offset':0,'size':32768})
        data=base64.b64decode(part['base64']);assert hashlib.sha256(data).hexdigest()==art['sha256']==row['output_sha256']
        assert Image.open(io.BytesIO(data)).size==(320,240);(out/'output.png').write_bytes(data)
        assert row['status']=='undetected' and data==raw
        assert result['result']['model_sha256']==hashes['model'] and result['result']['renderer_sha256']==hashes['renderer']
        replay=asyncio.run(mcp('censor_process',payload));assert replay['result']==result
        read=asyncio.run(mcp('artifact_read',{'artifact_id':art['artifact_id'],'offset':0,'size':32768}));assert read['result']==part
        assert op('censor.process',payload|{'padding':11})['error']['code']=='IDEMPOTENCY_CONFLICT'
        mcp_receipt=asyncio.run(mcp('censor_process',payload|{'request_id':'real-mcp'}))['result']
        assert mcp_receipt['state']=='queued'
        assert poll(mcp_receipt['id'])['state']=='succeeded'
        checks.append('real NTD11 CPU HTTP upload/digest/queued/running/success/artifact hash+dimensions; MCP new admission+replay+artifact; model/renderer provenance; conflict')
        stop();proc=start();assert ok('censor.process',payload)==result
        assert ok('artifact.read',{'artifact_id':art['artifact_id'],'offset':0,'size':32768})==part
        checks.append('completed replay and artifacts preserved across restart; no rerun')
        stop()
        # Controlled slow child, NOT model evidence. It never produces fake success.
        wrapper=out/'slow-python';wrapper.write_text('#!'+sys.executable+'\nimport os,sys,json,time,ctypes,signal\nfrom pathlib import Path\ns=json.loads(Path(sys.argv[-1]).read_text())\nctypes.CDLL(None).prctl(1,signal.SIGKILL,0,0,0)\nif os.getppid()!=s["parent_pid"]:sys.exit(1)\nPath(s["result_file"]).with_name("slow.pid").write_text(str(os.getpid()))\ntime.sleep(30)\n');wrapper.chmod(0o700)
        binding.update(python='slow-python',timeout_seconds=3)
        (out/'media.json').write_text(json.dumps({'censor':binding}))
        proc=start();slow=payload|{'request_id':'slow'}
        t=time.monotonic();j=ok('censor.process',slow);timings['slow_submit']=time.monotonic()-t;assert timings['slow_submit']<1
        poll(j['id'],False)
        for name,args in [('censor.get',{'job_id':j['id']}),('status',{}),('jobs.page',{}),('jobs.events',{'job_id':j['id'],'timeout_seconds':0}),('corpus.page',{'collection':'tags'})]:
            t=time.monotonic();ok(name,args);timings['slow_'+name]=time.monotonic()-t;assert timings['slow_'+name]<1
        assert ok('censor.process',slow)['id']==j['id']
        assert op('censor.process',slow|{'request_id':'busy'})['error']['code']=='CAPACITY'
        assert op('mask.get',{'job_id':j['id']})['error']['code']=='MODULE_DISABLED'
        failed=poll(j['id']);assert failed['error']['code']=='TIMEOUT',failed
        checks.append('slow stand-in: prompt durable ACK; independent polls/core reads; replay; bounded one-slot capacity; timeout')
        j=ok('censor.process',slow|{'request_id':'stop'});poll(j['id'],False)
        timings['stop']=stop();assert timings['stop']<10
        proc=start();assert ok('censor.get',{'job_id':j['id']})['error']['code']=='INTERRUPTED'
        assert ok('censor.process',slow|{'request_id':'stop'})['state']=='failed'
        checks.append('bounded graceful child stop/reap and interrupted no-replay after restart')
        j=ok('censor.process',slow|{'request_id':'crash'});poll(j['id'],False)
        pidfile=Path(j['media_folder'])/'slow.pid'
        for _ in range(100):
            if pidfile.exists():break
            time.sleep(.02)
        childpid=int(pidfile.read_text());proc.kill();proc.wait(timeout=10);proc=None
        proc=start();assert ok('censor.get',{'job_id':j['id']})['error']['code']=='INTERRUPTED'
        assert ok('censor.process',slow|{'request_id':'crash'})['state']=='failed'
        stat=Path(f'/proc/{childpid}/stat');assert not stat.exists() or stat.read_text().split()[2]=='Z'
        checks.append('abrupt owner death kills subordinate; restart quarantines interrupted intent without replay')
        assert hashes=={k:hashlib.sha256(Path(v).read_bytes()).hexdigest() for k,v in [('model',a.model),('renderer',str(Path(a.upstream)/'mosaic.py'))]}
        summary={'checks':checks,'timings_seconds':timings,'binding_hashes':hashes,'installed':str(Path(atlas_engine.__file__).parent),'real_result':result}
        (out/'summary.json').write_text(json.dumps(summary,indent=2));print(json.dumps({'checks':checks,'timings':timings},indent=2))
    finally:
        if proc and proc.poll() is None:stop()
        log.close()

if __name__=='__main__':main()
