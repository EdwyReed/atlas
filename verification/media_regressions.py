"""Narrow installed media compatibility/guard regression. Mask is a labelled
subordinate contract stand-in, NOT mask model execution evidence.
"""
import argparse,base64,hashlib,io,json,threading
from pathlib import Path
from PIL import Image
from atlas_engine.engine import Engine

p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False)
e=Engine(a.out/'state',None,None,None,media_config={'mask':{'root':'unused'},'censor':{'python':'unused','model':'unused','upstream':'unused'}})
def ok(op,args):
    r=e.call(op,args);assert r['ok'],r;return r['result']
b=io.BytesIO();Image.new('RGB',(32,24),'blue').save(b,format='PNG');raw=b.getvalue();sha=hashlib.sha256(raw).hexdigest()
u=ok('media.upload.begin',{'request_id':'u','role':'image','size':len(raw),'sha256':sha})
ok('media.upload.chunk',{'upload_id':u['upload_id'],'offset':0,'base64':base64.b64encode(raw).decode()});ok('media.upload.finalize',{'upload_id':u['upload_id']})
seen=[]
def mask(args,principal):
    seen.append(args)
    if args['action']=='setup':return {'ok':True,'cache_id':'cache','image_path':args['image_path']}
    if args['action'] in ('health','release','reset'):return {'ok':True}
    folder=Path(args['output_dir']);rows=[]
    for key,name in [('mask_file','mask.png'),('overlay_file','overlay.png'),('logits_file','candidate-0.npy')]:
        (folder/name).write_bytes(raw if name.endswith('.png') else b'labelled stand-in, not numpy evidence')
        rows.append((key,name))
    result={'ok':True,'candidates':[dict(rows,logits_sha256=hashlib.sha256((folder/'candidate-0.npy').read_bytes()).hexdigest())]};path=folder/'subordinate.json';path.write_text(json.dumps(result))
    return result|{'manifest_path':str(path),'image_path':'private','output_dir':str(folder)}
e.media.mask_call=mask
assert ok('mask.setup',{'upload_id':u['upload_id']})=={'ok':True,'cache_id':'cache'}
segment={'request_id':'mask1','cache_id':'cache','points':[{'x':4.5,'y':7.5,'label':1}],'box':[1.0,2.0,20.0,21.0],'multimask':False,'annotate_prompts':False}
j=ok('mask.segment',segment);assert j['state']=='succeeded',j
assert seen[-1]['points']==segment['points'] and seen[-1]['box']==segment['box'] and not seen[-1]['multimask']
assert ok('mask.segment',segment)==j
prior={'job_id':j['id'],'candidate':0}
j2=ok('mask.segment',segment|{'request_id':'mask2','prior':prior});assert j2['state']=='succeeded'
assert seen[-1]['prior']['manifest_path']==j['media_manifest_path']
with e.media.lock:
    assert ok('mask.get',{'job_id':j['id']})==j
    assert e.call('mask.health',{})['error']['code']=='CAPACITY'
# No Engine start: admitted work stays queued. Recovery must fail it, not run it.
payload={'request_id':'queued','images':[{'upload_id':u['upload_id'],'name':'safe.png'}]}
c=ok('censor.process',payload);assert c['state']=='queued'
assert Path(c['media_inputs'][0]['path']).read_bytes()==raw
assert ok('censor.process',payload)==c
assert e.call('mask.health',{})['error']['code']=='CAPACITY'
e.media.recover();assert ok('censor.get',{'job_id':c['id']})['error']['code']=='INTERRUPTED'
assert ok('censor.process',payload)['state']=='failed'
e.media.config['max_storage_bytes']=1
assert e.call('censor.process',payload|{'request_id':'full'})['error']['code']=='RESOURCE_LIMIT'
# Restore guard is checked before optional-media dispatch, including worker startup.
e.restore_quarantined=True;e.start();assert not getattr(e,'media_thread',None)
assert e.call('censor.process',payload|{'request_id':'restored'})['error']['code']=='RESTORE_RECONCILIATION_REQUIRED'
e.close()
(a.out/'summary.json').write_text(json.dumps({'checks':['mask stand-in: setup/synchronous segment/points/box/options/artifacts/replay/job prior/read independent of slot','queued input byte snapshot; capacity; interrupted recovery without replay','storage admission cap; restored-root workers and dispatch suppressed'],'actual_mask_inference':False},indent=2))
print((a.out/'summary.json').read_text())
