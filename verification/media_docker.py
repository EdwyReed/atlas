"""Real CPU mask+censor in the delivered image; no host runtime/source mounts.

Safe synthetic geometry establishes execution and contracts, not detector quality.
"""
import argparse
import asyncio
import base64
import hashlib
import io
import json
from pathlib import Path
import secrets
import socket
import sys
import time
import httpx
from PIL import Image, ImageDraw
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import atlas_engine


def sha(raw):return hashlib.sha256(raw).hexdigest()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--image',required=True);p.add_argument('--weights',type=Path,required=True)
    p.add_argument('--portable',action='store_true',help='pack populated media root, restore to a different root/volume and explicitly provision from archived weights')
    p.add_argument('--production-envelope',action='store_true')
    p.add_argument('--old-helper',type=Path)
    a=p.parse_args();out=a.out.absolute();out.mkdir(parents=True,exist_ok=False,mode=0o700)
    sys.path.insert(0,str(Path(__file__).absolute().parent))
    if a.production_envelope:
        assert a.old_helper and not a.portable
        from media_production_envelope import Container
        Container.old_helper=a.old_helper
    else:
        from container_lifecycle import Container
    modules={p.name:sha(p.read_bytes()) for p in Path(atlas_engine.__file__).parent.glob('*.py')}
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    endpoint=f'http://127.0.0.1:{port}';token=secrets.token_urlsafe(32);headers={'Authorization':'Bearer '+token}
    c=Container(a.image,out,port,token,modules,memory='3g',cpus='2',pids='256')
    original=c;relocated=None
    responses=[]
    def op(name,args={}):
        r=httpx.post(endpoint+'/v1/ops/'+name,json=args,headers=headers,timeout=120,trust_env=False);r.raise_for_status()
        v=r.json();assert v['ok'],v;responses.append({'operation':name,'response':v});return v['result']
    async def mcp(name,args):
        async with streamable_http_client(endpoint+'/mcp/',http_client=httpx.AsyncClient(headers=headers,trust_env=False,timeout=120)) as (rd,wr,_):
            async with ClientSession(rd,wr) as session:
                await session.initialize();r=await session.call_tool(name,args);v=r.structuredContent or json.loads(r.content[0].text)
                assert v['ok'],v;return v['result']
    def ready():
        c.start()
        for _ in range(300):
            assert c.poll() is None,'container exited'
            try:
                if op('status'):return
            except httpx.TransportError:pass
            time.sleep(.1)
        raise AssertionError('not ready')
    def artifacts(job):
        rows=op('artifact.list',{'job_id':job['id']});assert rows
        data={}
        folder=out/job['id'];folder.mkdir(exist_ok=True)
        for row in rows:
            raw=bytearray()
            while len(raw)<row['size']:
                part=op('artifact.read',{'artifact_id':row['artifact_id'],'offset':len(raw),'size':32768})
                block=base64.b64decode(part['base64']);assert block;raw.extend(block)
            raw=bytes(raw);assert len(raw)==row['size'] and sha(raw)==row['sha256']
            data[row['image_name']]=raw;(folder/row['image_name']).write_bytes(raw)
        return data
    try:
        c.configure({'root':'/var/lib/atlas/state' if a.production_envelope else '/var/lib/atlas','clients_file':'/run/atlas/clients.json','host':'0.0.0.0','port':12088,
                     'container_endpoint':endpoint,'media_config_file':'/run/atlas/media.json'})
        if a.production_envelope:
            c.provision(a.weights)
        else:
            provision=c.run('run','--rm','--network','none','--user','0:0',
                '-v',c.state+':/var/lib/atlas','-v',c.secrets+':/run/atlas',
                '-v',str(a.weights.absolute())+':/inputs:ro','--entrypoint','python',a.image,'/opt/atlas-media/init.py')
            (out/'provision.json').write_text(provision.stdout)
        ready()
        image=Image.new('RGB',(320,240),'#dbe9f2');draw=ImageDraw.Draw(image)
        draw.rectangle((70,50,235,195),fill='#cb3546',outline='#521725',width=5);draw.ellipse((90,68,115,93),fill='#ffcf62')
        b=io.BytesIO();image.save(b,format='PNG');raw=b.getvalue();(out/'input.png').write_bytes(raw)
        uid=op('media.upload.begin',{'request_id':'image','size':len(raw),'sha256':sha(raw),'role':'image'})['upload_id']
        op('media.upload.chunk',{'upload_id':uid,'offset':0,'base64':base64.b64encode(raw).decode()})
        op('media.upload.finalize',{'upload_id':uid})
        setup=op('mask.setup',{'upload_id':uid});assert (setup['width'],setup['height'])==(320,240)
        payload={'request_id':'segment','cache_id':setup['cache_id'],'points':[{'x':160,'y':130,'label':1},{'x':20,'y':20,'label':0}],
                 'box':[60,40,245,205],'multimask':True,'annotate_prompts':False}
        mask=op('mask.segment',payload);assert mask['state']=='succeeded' and len(mask['result']['candidates'])==3,mask
        data=artifacts(mask)
        selected=Image.open(io.BytesIO(data['candidate-1.png']))
        assert selected.size==(320,240) and selected.getpixel((160,130))==255 and selected.getpixel((20,20))==0
        refinement=asyncio.run(mcp('mask_segment',{**payload,'request_id':'refine','multimask':False,'prior':{'job_id':mask['id'],'candidate':1}}))
        assert refinement['state']=='succeeded',refinement
        assert refinement['result']['prior_provenance']['logits_sha256']==sha(data['candidate-1.npy'])
        artifacts(refinement)
        request={'request_id':'censor','images':[{'upload_id':uid,'name':'safe.png'}]}
        censor=asyncio.run(mcp('censor_process',request));assert censor['state']=='queued',censor
        for _ in range(1000):
            censor=op('censor.get',{'job_id':censor['id']})
            if censor['state'] in ('succeeded','failed'):break
            time.sleep(.1)
        assert censor['state']=='succeeded',censor
        assert censor['result']['device']=='cpu' and censor['result']['inference_images']==1
        censored=artifacts(censor);assert censored['safe.png']==raw
        def client_history():
            from atlas_engine.client import call
            tokenfile=out/'client-token';tokenfile.write_text(token);tokenfile.chmod(0o600)
            config={'endpoint':endpoint,'token_file':str(tokenfile)}
            assert call('mask.get',{'job_id':mask['id']},config)['result']==mask
            assert call('censor.get',{'job_id':censor['id']},config)['result']==censor
            assert call('jobs.page',{},config)['ok']
            assert httpx.post(endpoint+'/v1/ops/status',json={},trust_env=False).status_code in (401,403)
            (out/'client-history.json').write_text(json.dumps({'passed':True,'authenticated_installed_client':True,'unauthenticated_refused':True}))
        client_history()
        c.verify();c.terminate();c.wait();ready()
        client_history()
        assert op('mask.segment',payload)==mask
        assert op('censor.process',request)==censor
        assert artifacts(mask)==data
        if a.portable:
            c.terminate();c.wait()
            relocation=out/'relocation';relocation.mkdir()
            relocated=Container(a.image,relocation,port,token,modules,memory='3g',cpus='2',pids='256')
            relocated.configure({'root':'/var/lib/atlas/relocated','clients_file':'/run/atlas/clients.json',
                'host':'0.0.0.0','port':12088,'container_endpoint':endpoint,'media_config_file':'/run/atlas/media.json'})
            code='''from pathlib import Path
import json
from atlas_engine.portable import pack,unpack,activate
from atlas_engine.state_backup import restore_state,registry_digest,versions
packed=pack('/var/lib/atlas',Path('/backup/portable.tar.gz'),media_weights='/var/lib/atlas/media-kit/weights')
print(json.dumps({'pack':packed}))
'''
            transfer=c.run('run','--rm','--network','none','--user','0:0',
                '-v',c.state+':/var/lib/atlas:ro','-v',relocated.state+':/backup',
                '--entrypoint','python',a.image,'-I','-c',code)
            (relocation/'transfer.json').write_text(transfer.stdout)
            code='''from pathlib import Path
import json,os
from atlas_engine.portable import activate,unpack
from atlas_engine.state_backup import restore_state,registry_digest,versions
r=Path('/var/lib/atlas/relocated');print(json.dumps({'unpack':unpack(Path('/var/lib/atlas/portable.tar.gz'),r)}));s=restore_state(r)
from atlas_engine.portable import pack
archive=Path('/var/lib/atlas/repacked.tar.gz');print(json.dumps({'repack':pack(r,archive)}))
second=Path('/var/lib/atlas/second');unpack(archive,second)
with (second/'portable-media/weights/ntd11.pt').open('r+b') as f:f.write(b'corrupt')
try:pack(second,Path('/var/lib/atlas/must-refuse.tar.gz'))
except ValueError:print(json.dumps({'changed_media_repack_refused':True}))
else:raise AssertionError('corrupt media silently repacked')
report={'target_root':str(r),'operator':'owned acceptance','registry_logical_sha256':registry_digest(r),
'backup_manifest_sha256':s['backup_manifest_sha256'],'runtime':versions(),'retain_historical_effects':True,
'assessment':'synthetic media; original effects retained and never replayed'}
p=Path('/var/lib/atlas/report.json');p.write_text(json.dumps(report));print(json.dumps(activate(r,p)))
for folder,ds,fs in os.walk('/var/lib/atlas'):
 os.chown(folder,10001,10001)
 for name in fs:os.chown(Path(folder)/name,10001,10001)
'''
            admitted=c.run('run','--rm','--network','none','--user','0:0',
                '-v',relocated.state+':/var/lib/atlas','--entrypoint','python',a.image,'-I','-c',code)
            (relocation/'activation.json').write_text(admitted.stdout)
            provision=c.run('run','--rm','--network','none','--user','0:0',
                '-v',relocated.state+':/var/lib/atlas','-v',relocated.secrets+':/run/atlas',
                '--entrypoint','python',a.image,'/opt/atlas-media/init.py','--restored','--state-root','/var/lib/atlas/relocated')
            (relocation/'provision.json').write_text(provision.stdout)
            # The original volume, weights and credentials are NOT attached to
            # the successor daemon; only the archive crossed the boundary.
            c=relocated;ready()
            assert op('mask.segment',payload)==mask
            assert op('censor.process',request)==censor
            assert artifacts(mask)==data
            # Fresh setup and actual inference after relocation, not just old
            # receipt reads. The original subordinate cache is not transferred.
            setup=op('mask.setup',{'upload_id':uid})
            newmask=op('mask.segment',{**payload,'request_id':'relocated-segment','cache_id':setup['cache_id']})
            assert newmask['state']=='succeeded'
            artifacts(newmask);c.verify(state_root='/var/lib/atlas/relocated');c.terminate();c.wait();ready()
            assert op('mask.get',{'job_id':newmask['id']})==newmask
        result={'status':'pass','image_id':json.loads((out/'container.json').read_text())['image_id'],
            'boundary':'nonroot read-only image; two owned volumes only; private kit copied once; no old source/env/weights mounts in daemon',
            'checks':['real MobileSAM CPU setup/3 candidates and selected foreground/background','MCP selected-logit mask refinement','MCP NTD11 CPU inference + unchanged safe original bytes','all mask/logit/overlay/manifest/artifact SHA256 readback','restart exact receipt/artifacts without inference replay'],
            'limits':['safe synthetic geometry only; not general segmentation/detection quality'],
            'portable_media':bool(a.portable)}
        if a.portable:result['checks'].append('populated archive with pinned weights -> independent relocated volume/root -> historical receipts and fresh real CPU inference -> restart')
        (out/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
    finally:
        (out/'responses.json').write_text(json.dumps(responses))
        if c.created and c.poll() is None:
            code='import json;from pathlib import Path;r=Path("/var/lib/atlas");print(json.dumps({str(p.relative_to(r)):p.read_text(errors="replace")[-12000:] for p in r.rglob("worker.log")}))'
            (out/'workers.json').write_text(c.exec(['python','-I','-c',code]).stdout)
        if relocated:relocated.cleanup()
        original.cleanup()


if __name__=='__main__':main()
