"""Original full-server IMAGE recipe acceptance; shared launcher owns cleanup."""
import asyncio
import base64
from copy import deepcopy
import hashlib
import io
import json
import sqlite3
import time
import httpx
from PIL import Image


def run(c):
    op,native,save,passed=(c[k] for k in ('op','native','save','passed'))
    out,ref,mode=(c[k] for k in ('out','ref','mode'))
    def mcp(name,args):return asyncio.run(c['mcp'](name.replace('.','_'),args))
    def fail(name,args,code):
        r=httpx.post(c['au']+'/v1/ops/'+name,json=args,headers=c['headers'],trust_env=False,timeout=45)
        r.raise_for_status();data=r.json()
        assert not data['ok'] and data['error']['code']==code,data
        c['responses'].append({'negative':name,'response':data})
    def read(artifact,via=op):
        data=bytearray()
        while len(data)<artifact['size']:
            r=via('artifact.read',{'artifact_id':artifact['artifact_id'],'offset':len(data),'size':32768})
            data.extend(base64.b64decode(r['base64'],validate=True))
        assert r['eof'] and hashlib.sha256(data).hexdigest()==artifact['sha256']
        return bytes(data)
    # Installed original API supplies actual invocation schemas, no guessed names.
    schema=httpx.get(c['nu']+'/openapi.json',trust_env=False,timeout=40).json()
    schemas=schema['components']['schemas']
    chosen={k:v for k,v in schemas.items() if any(x in k for x in ('ImageResizeInvocation','ImageField','WorkflowReturn','CallSavedWorkflow'))}
    assert any(v.get('properties',{}).get('type',{}).get('const')=='img_resize' for v in chosen.values()),chosen.keys()
    assert 'patch' in schema['paths']['/api/v1/workflows/i/{workflow_id}']
    save('native-invocation-schemas.json',chosen)
    # Owned tiny asymmetric RGB raster, source retained byte-for-byte.
    source=Image.new('RGB',(7,5))
    for y in range(source.height):
        for x in range(source.width):source.putpixel((x,y),((x*31+y*7)%256,(y*43+x*3)%256,(x*19+y*29)%256))
    b=io.BytesIO();source.save(b,format='PNG',compress_level=0);raw=b.getvalue()
    (out/'input.png').write_bytes(raw)
    u=op('upload.begin',{'request_id':'image-input','size':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'role':'image'})
    op('upload.chunk',{'upload_id':u['upload_id'],'offset':0,'base64':base64.b64encode(raw).decode()})
    op('upload.finalize',{'upload_id':u['upload_id']})
    imported=mcp('upload.import',{**ref,'request_id':'import-image','upload_id':u['upload_id'],'family':'pixel','board_id':None})
    assert imported['state']=='succeeded',imported
    input_name=imported['result']['image_name'];original=imported['result']['original']
    assert read(original,mcp)==raw
    save('import.json',imported)
    node,edge=c['node'],c['edge']
    resize=node('resize','img_resize',{'image':None,'width':11,'height':9,'resample_mode':'nearest'})
    resize['data']['version']='1.2.2'
    workflow={**c['workflow'],'name':'Owned nearest IMAGE resize',
        'exposedFields':[{'nodeId':'resize','fieldName':n} for n in ('image','width','height')],
        'nodes':[resize,node('rv','workflow_return_value',{'key':'resized','value':None}),node('ret','workflow_return',{'values':[]})],
        'edges':[edge('resize','image','rv','value'),edge('rv','value','ret','values')]}
    created=native('POST','/api/v1/workflows/',{'workflow':workflow});wid=created['workflow_id']
    assert native('GET','/api/v1/workflows/i/'+wid)['workflow']==created['workflow']
    wref={**ref,'workflow_id':wid};observed=op('workflows.schema',wref)
    assert observed['recipe']=='native-image-resize-v1' and observed['return_type']=='image' and mcp('workflows.schema',wref)==observed
    req={**wref,'request_id':'image-http','version_semantics':'latest_at_execution',
         'workflow_inputs':{'saved_workflow_input::resize::image':{**ref,'image_name':input_name}},'return_key':'resized'}
    count=c['posts']()
    fail('workflows.run',{**req,'backend_generation':'other'},'NATIVE_SCOPE_MISMATCH')
    bad=deepcopy(req);bad['workflow_inputs']['saved_workflow_input::resize::image']['backend_generation']='other'
    fail('workflows.run',bad,'NATIVE_SCOPE_MISMATCH')
    bad=deepcopy(req);bad['workflow_inputs']['saved_workflow_input::resize::image']={'image_name':input_name}
    fail('workflows.run',bad,'VALIDATION')
    bad=deepcopy(req);bad['workflow_inputs']['saved_workflow_input::resize::image']['image_name']='/private/input.png'
    fail('workflows.run',bad,'VALIDATION')
    fail('workflows.run',{**req,'workflow_inputs':{}},'WORKFLOW_INPUT_UNSUPPORTED')
    fail('workflows.run',{**req,'return_key':'unselected'},'WORKFLOW_OUTPUT_UNSUPPORTED')
    fail('workflows.run',{**req,'workflow_inputs':{**req['workflow_inputs'],'saved_workflow_input::resize::width':2049}},'WORKFLOW_INPUT_UNSUPPORTED')
    assert c['posts']()==count
    passed('running original img_resize1.2.2 schemas; scoped SDK MCP upload/import original retained; HTTP/MCP recipe schema; malformed/path/scope/key/bounds refused before enqueue')
    jobs=[];artifacts=[]
    def verify(job,dimensions,via=op):
        done,item=c['complete'](job)
        save('candidate-'+job['payload']['request_id']+'.json',{'job':done,'native':item})
        for _ in range(100):
            if done.get('outputs'):break
            time.sleep(.1);done=c['observe'](job['id'])
        assert done['state']=='succeeded' and len(done['outputs'])==1,done
        entries=done['native_items'][str(done['native_ids'][0])]['native_results']['entries']
        assert len(entries)==1 and entries[0]['status']=='available',entries
        entry=entries[0];name=done['outputs'][0]
        assert entry['image']=={**ref,'image_name':name} and name!=input_name
        lin=entry['lineage'];child=native('GET','/api/v1/queue/default/i/'+str(lin['child_item_id']))
        assert child['parent_item_id']==item['item_id'] and child['session_id']==lin['child_session_id']
        assert done['workflow_observation']['executed_revision'] is None and lin['return_key']=='resized'
        artifact=via('artifact.fetch',{'job_id':done['id'],'image_name':name})
        assert artifact['native_lineage']==lin and (artifact['width'],artifact['height'])==dimensions
        downloaded=read(artifact,via)
        remote=httpx.get(c['nu']+'/api/v1/images/i/'+name+'/full',headers=c['native_headers'],trust_env=False)
        remote.raise_for_status();assert downloaded==remote.content
        with Image.open(io.BytesIO(downloaded)) as result:
            expected=source.resize(dimensions,Image.Resampling.NEAREST)
            assert result.size==expected.size and result.mode==expected.mode and result.tobytes()==expected.tobytes()
            pixels=hashlib.sha256(result.tobytes()).hexdigest()
        fail('artifact.fetch',{'job_id':done['id'],'image_name':input_name},'ARTIFACT_NOT_LINKED')
        assert read(original)==raw
        save('image-'+job['payload']['request_id']+'.json',{'job':done,'native_root':item,'native_child':child,'artifact':artifact,'pixels_sha256':pixels})
        (out/(job['payload']['request_id']+'.png')).write_bytes(downloaded)
        jobs.append(done);artifacts.append(artifact)
        return done
    first=verify(op('workflows.run',req),(11,9))
    second_req={**req,'request_id':'image-mcp','workflow_inputs':{**req['workflow_inputs'],'saved_workflow_input::resize::width':13,'saved_workflow_input::resize::height':6}}
    verify(mcp('workflows.run',second_req),(13,6),mcp)
    passed('installed HTTP and real SDK MCP callable -> original native child nearest resize -> selected scoped IMAGE -> artifact fetch/read exact native bytes, dimensions and every deterministic pixel')
    count=c['posts']();c['stop']('atlas');c['stop']('native');c['start_native']();c['start_atlas']()
    assert op('workflows.run',req)['id']==first['id'] and mcp('workflows.run',second_req)['id']==jobs[1]['id'] and c['posts']()==count
    for a in artifacts:assert read(a)==(out/(jobs[artifacts.index(a)]['payload']['request_id']+'.png')).read_bytes()
    passed('both original native and Atlas restart; exact HTTP/MCP workflow replay/artifacts retained with no additional enqueue')
    mode['lost']=True
    lost_req={**req,'request_id':'image-lost-ack'}
    lost=mcp('workflows.run',lost_req)
    c['stop']('atlas');c['start_atlas']()
    for _ in range(100):
        lost=c['observe'](lost['id'])
        if lost.get('native_ids'):break
        time.sleep(.1)
    count=c['posts']();assert op('workflows.run',lost_req)['id']==lost['id'] and c['posts']()==count
    verify(lost,(11,9))
    mode.update(lost=True,crash_enqueue=True)
    crash_req={**req,'request_id':'image-crash-after-commit'}
    try:op('workflows.run',crash_req)
    except httpx.TransportError:pass
    else:raise AssertionError('SIGKILL did not drop Atlas response')
    c['start_atlas']()
    for _ in range(100):
        crash=op('workflows.run',crash_req)
        if crash.get('native_ids'):break
        op('observer.reconcile',{});time.sleep(.1)
    verify(crash,(11,9),mcp)
    passed('lost enqueue response and actual post-native-commit Atlas SIGKILL; restart exact-ID GET receipt recovery/ACK, no blind replay')
    # Mutate native library AFTER Atlas observes revision1, BEFORE enqueue.
    def edit():
        changed=deepcopy(created['workflow']);changed['nodes'][0]['data']['inputs']['width']['value']=10
        updated=native('PATCH','/api/v1/workflows/i/'+wid,{'workflow':changed,'expected_revision':1})
        assert updated['revision']==2
        assert native('GET','/api/v1/workflows/i/'+wid)['workflow']==updated['workflow']
        save('mutable-native-edit.json',updated)
    mode['enqueue_before_forward']=edit
    mutated=verify(op('workflows.run',{**req,'request_id':'image-latest'}),(10,9))
    assert mutated['workflow_observation']['observed_revision']==1 and op('workflows.schema',wref)['observed_revision']==2
    passed('real native library edit revision1->2 after Atlas preflight executes changed width10; observed revision1 retained and executed_revision remains unknown')
    mode['stale']=True
    assert c['observe'](first['id'])['outputs']==first['outputs'];mode['stale']=False
    mode['missing']=True
    assert c['observe'](first['id'])['outputs']==first['outputs'];mode['missing']=False
    c['stop']('atlas');c['stop']('native')
    with sqlite3.connect(c['root']/'databases/invokeai.db') as db:
        db.row_factory=sqlite3.Row
        rows=[dict(r) for r in db.execute('select item_id,parent_item_id,batch_id,status,user_id from session_queue')]
        models=db.execute('select count(*) from models').fetchone()[0]
        migrations=[dict(r) for r in db.execute('select * from migrations')]
        assert db.execute('pragma integrity_check').fetchone()[0]=='ok'
    roots=[r for r in rows if r['parent_item_id'] is None];children=[r for r in rows if r['parent_item_id'] is not None]
    assert len(roots)==c['posts']()==len(jobs)==5 and len(children)==5,rows
    assert all(sum(r['parent_item_id']==j['native_ids'][0] for r in children)==1 for j in jobs)
    assert all(r['status']=='completed' and r['user_id']==c['user']['user_id'] for r in rows)
    assert models==0 and sorted(r['version'] for r in migrations)==list(range(35))
    assert len(c['intent_checks'])==5
    save('database.json',{'queue_rows':rows,'root_count':len(roots),'child_count':len(children),'models':models,'migrations':migrations,'integrity_check':'ok'})
    c['start_atlas']()
    assert mcp('workflows.run',req)['outputs']==first['outputs']
    for j,a in zip(jobs,artifacts):
        assert op('generation.get',{'job_id':j['id']})['outputs']==j['outputs']
        assert mcp('artifact.fetch',{'job_id':j['id'],'image_name':a['image_name']})==a
        assert read(a,mcp)==(out/(j['payload']['request_id']+'.png')).read_bytes()
    assert read(original,mcp)==raw and c['posts']()==5
    passed('independent original DB: exactly5 roots/5 children/5 enqueues, one child per intent,5 durable pre-forward checks, migrations0-34, zero models; stale/missing history and native-offline cached artifacts/results retained')
    save('result.json',{'checks':c['checks'],'enqueue_posts':c['posts'](),'root_count':len(roots),'child_count':len(children),'job_ids':[j['id'] for j in jobs],'artifacts':artifacts})
