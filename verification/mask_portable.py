"""Installed real CPU mask acceptance. Only explicitly supplied private backend root.
Synthetic geometry verifies execution/contracts, not general segmentation quality.
"""
import argparse,asyncio,ast,base64,hashlib,io,json,math,os,secrets,socket,struct,subprocess,sys,time
from pathlib import Path
import httpx
from PIL import Image,ImageDraw
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import atlas_engine


def sha(raw):return hashlib.sha256(raw).hexdigest()

def logits(raw):
    assert raw[:8]==b'\x93NUMPY\x01\x00'
    n=struct.unpack('<H',raw[8:10])[0];h=ast.literal_eval(raw[10:10+n].decode())
    assert h=={'descr':'<f4','fortran_order':False,'shape':(1,256,256)},h
    values=struct.unpack('<65536f',raw[10+n:]);assert all(math.isfinite(x) for x in values)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);p.add_argument('--backend',type=Path,required=True)
    a=p.parse_args();out=a.out.absolute();out.mkdir(parents=True,exist_ok=False,mode=0o700);backend=a.backend.resolve()
    assert (backend/'backend-paths.json').is_file() and not (backend/'runtime/worker.sock').exists()
    token=secrets.token_urlsafe(36);(out/'clients.json').write_text(json.dumps([{'token':token,'principal':'mask-check','source':'synthetic'}]));(out/'clients.json').chmod(0o600)
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    endpoint=f'http://127.0.0.1:{port}';headers={'Authorization':'Bearer '+token}
    (out/'atlas.json').write_text(json.dumps({'root':'state','clients_file':'clients.json','port':port,'media_config_file':'media.json'}))
    (out/'media.json').write_text(json.dumps({'mask':{'root':os.path.relpath(backend,out)}}))
    home=out/'home';home.mkdir();env={k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME','VIRTUAL_ENV')};env.update(HOME=str(home),HF_HUB_OFFLINE='1')
    log=(out/'daemon.log').open('wb');proc=None;checks=[];timings={};worker_pids=[]
    def op(name,args=None):
        r=httpx.post(endpoint+'/v1/ops/'+name,json=args or {},headers=headers,trust_env=False,timeout=100);r.raise_for_status();return r.json()
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
        proc.terminate();assert proc.wait(timeout=15) in (0,-15);proc=None
        assert not (backend/'runtime/worker.sock').exists()
        for pid in worker_pids:assert not Path(f'/proc/{pid}').exists(),'owned worker not reaped'
    async def mcp(name,args):
        async with streamable_http_client(endpoint+'/mcp/',http_client=httpx.AsyncClient(headers=headers,trust_env=False)) as (r,w,_):
            async with ClientSession(r,w) as session:
                await session.initialize();v=await session.call_tool(name,args);assert not v.isError,v;return v.structuredContent['result']
    def upload(raw,rid,final=True):
        u=ok('media.upload.begin',{'request_id':rid,'role':'image','size':len(raw),'sha256':sha(raw)})
        for off in range(0,len(raw),32768):ok('media.upload.chunk',{'upload_id':u['upload_id'],'offset':off,'base64':base64.b64encode(raw[off:off+32768]).decode()})
        if final:ok('media.upload.finalize',{'upload_id':u['upload_id']})
        return u['upload_id']
    def artifacts(job):
        rows=ok('artifact.list',{'job_id':job['id']});assert len(rows)==3*len(job['result']['candidates'])+1
        folder=out/job['id'];folder.mkdir();data={}
        for row in rows:
            raw=bytearray()
            while len(raw)<row['size']:
                part=ok('artifact.read',{'artifact_id':row['artifact_id'],'offset':len(raw),'size':32768});chunk=base64.b64decode(part['base64']);assert chunk;raw.extend(chunk)
            raw=bytes(raw);assert len(raw)==row['size'] and sha(raw)==row['sha256'];data[row['image_name']]=raw;(folder/row['image_name']).write_bytes(raw)
            if row['image_name'].endswith('.npy'):logits(raw)
            elif row['image_name'].endswith('.png'):assert Image.open(io.BytesIO(raw)).size==(320,240) and row['mime']=='image/png'
        manifest=json.loads(data['manifest.json']);assert manifest['engine_job_id']==job['id'] and manifest['image_sha256']==digest
        assert manifest['mask_polarity']=='white=foreground; black=background'
        for c in manifest['candidates']:
            mask=Image.open(io.BytesIO(data[c['mask_file']]));values=list(mask.getdata());assert set(values)<= {0,255} and values.count(255)==c['foreground_pixels']
            assert sha(data[c['logits_file']])==c['logits_sha256']
        return data,rows
    try:
        proc=start();image=Image.new('RGB',(320,240),'#dbe9f2');d=ImageDraw.Draw(image);d.rectangle((70,50,235,195),fill='#cb3546',outline='#521725',width=5);d.ellipse((90,68,115,93),fill='#ffcf62')
        b=io.BytesIO();image.save(b,format='PNG');raw=b.getvalue();digest=sha(raw);(out/'input.png').write_bytes(raw);uid=upload(raw,'image')
        t=time.monotonic();setup=ok('mask.setup',{'upload_id':uid});timings['setup']=time.monotonic()-t
        assert (setup['width'],setup['height'])==(320,240) and setup['image_sha256']==digest and 'original stored raster' in setup['coordinates'] and not setup['embedding_reused']
        h=ok('mask.health');worker_pids.append(h['pid']);assert h['loaded'] and h['scope_cache_count']==1
        assert ok('mask.setup',{'upload_id':uid})['embedding_reused']
        payload={'request_id':'segment','cache_id':setup['cache_id'],'points':[{'x':160,'y':130,'label':1},{'x':20,'y':20,'label':0}],'box':[60,40,245,205],'multimask':True,'annotate_prompts':False}
        t=time.monotonic();job=ok('mask.segment',payload);timings['segment']=time.monotonic()-t;assert job['state']=='succeeded',job
        assert job['result']['cpu_only'] and len(job['result']['candidates'])==3
        data,rows=artifacts(job);selected=1
        mask=Image.open(io.BytesIO(data[f'candidate-{selected}.png']));assert mask.getpixel((160,130))==255 and mask.getpixel((20,20))==0
        prior={'job_id':job['id'],'candidate':selected};refine=payload|{'request_id':'refine','prior':prior,'multimask':False}
        refined=asyncio.run(mcp('mask_segment',refine));assert refined['state']=='succeeded',refined
        refined_data,_=artifacts(refined);assert refined['result']['prior_provenance']['logits_sha256']==sha(data[f'candidate-{selected}.npy'])
        assert Path(refined['media_manifest_path']).with_name('refinement-input.npy').read_bytes()==data[f'candidate-{selected}.npy']
        art=next(x for x in rows if x['image_name']==f'candidate-{selected}.png');part=asyncio.run(mcp('artifact_read',{'artifact_id':art['artifact_id'],'offset':0,'size':32768}));assert base64.b64decode(part['base64'])==data[art['image_name']]
        assert asyncio.run(mcp('mask_segment',payload))==job
        checks.append('real CPU HTTP original-raster setup/reuse + points/box 3 candidates; SDK MCP explicit job-prior refinement; all artifact bytes/hash/dimensions/float32 shape/binary polarity and selected positive/negative pixel')
        # Legacy prior uploads exercise real core identity/hash/shape guards, not mocks.
        subordinate=Path(job['media_manifest_path']);original=json.loads(subordinate.read_text());lr=data[f'candidate-{selected}.npy']
        for key,value in [('image_sha256','0'*64),('width',321),('weights_sha256','0'*64),('logits_hash',None),('shape',None)]:
            m=json.loads(json.dumps(original));blob=lr
            if key=='logits_hash':blob=lr[:-1]+bytes([lr[-1]^1])
            elif key=='shape':
                blob=lr.replace(b'(1, 256, 256)',b'(2, 128, 256)');m['candidates'][selected]['logits_sha256']=sha(blob)
            else:m[key]=value
            mu=upload(json.dumps(m).encode(),'manifest-'+key,False);lu=upload(blob,'logits-'+key,False)
            response=op('mask.segment',payload|{'request_id':'bad-'+key,'prior':{'manifest_upload_id':mu,'logits_upload_id':lu,'candidate':selected}})
            if response['ok']:
                bad=response['result'];assert bad['state']=='failed' and bad['error']['code'] in ('PROVENANCE','VALIDATION'),bad
            else:assert response['error']['code']=='PROVENANCE',response
        bad=ok('mask.segment',payload|{'request_id':'outside','points':[{'x':320,'y':1,'label':1}]});assert bad['state']=='failed' and bad['error']['code']=='VALIDATION'
        checks.append('actual prior image/dimensions/model/checksum/float32-shape refusal; original-raster out-of-bounds refusal')
        inventory={str(f.relative_to(out/'state/media')):sha(f.read_bytes()) for f in (out/'state/media').rglob('*') if f.is_file()}
        assert ok('mask.segment',payload)==job
        assert inventory=={str(f.relative_to(out/'state/media')):sha(f.read_bytes()) for f in (out/'state/media').rglob('*') if f.is_file()}
        stop();proc=start();assert ok('mask.segment',payload)==job and not (backend/'runtime/worker.sock').exists()
        assert ok('mask.get',{'job_id':refined['id']})==refined
        assert base64.b64decode(ok('artifact.read',{'artifact_id':art['artifact_id'],'offset':0,'size':32768})['base64'])==data[art['image_name']]
        assert not (backend/'runtime/worker.sock').exists()
        stale=ok('mask.segment',payload|{'request_id':'stale'});assert stale['state']=='failed' and stale['error']['code']=='STALE_CACHE',stale
        worker_pids.append(ok('mask.health')['pid']);new=ok('mask.setup',{'upload_id':uid});assert new['cache_id']!=setup['cache_id'] and not new['embedding_reused']
        after=ok('mask.segment',refine|{'request_id':'after-restart','cache_id':new['cache_id']});assert after['state']=='succeeded',after
        assert after['result']['prior_provenance']==refined['result']['prior_provenance'];artifacts(after)
        assert (out/'state/media/inputs'/digest).read_bytes()==raw and (out/'input.png').read_bytes()==raw
        stop();checks.append('completed replay no new worker/files; restart reaps worker and expires cache; setup-again + durable prior refinement; original bytes unchanged')
        summary={'checks':checks,'timings_seconds':timings,'installed':str(Path(atlas_engine.__file__).parent),'job':job,'refined':refined,'worker_pids':worker_pids,'owned_workers_after':0}
        (out/'summary.json').write_text(json.dumps(summary,indent=2));print(json.dumps({'checks':checks,'timings':timings,'selected':selected,'job':job['id']},indent=2))
    finally:
        if proc and proc.poll() is None:stop()
        log.close()

if __name__=='__main__':main()
