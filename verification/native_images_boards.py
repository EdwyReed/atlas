"""Image/board scenarios for native_fullserver.py --images-boards.

Synthetic RGB PNG pixels; original full native server, services and storage.
The shared runner owns private auth/bootstrap, processes, proxy and cleanup.
"""
import asyncio
import base64
import hashlib
import io
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageOps, PngImagePlugin


def run(c):
    op, native, save, passed = (c[k] for k in ('op','native','save','passed'))
    out, ref, mode, calls = (c[k] for k in ('out','ref','mode','calls'))
    stop, start_atlas, start_native = (c[k] for k in ('stop','start_atlas','start_native'))
    def mcp(name,args):return asyncio.run(c['mcp'](name.replace('.','_'),args))
    def count(path):return sum(x['method']=='POST' and urlsplit(x['path']).path==path for x in calls)
    def fail(name,args,code):
        r=httpx.post(c['au']+'/v1/ops/'+name,json=args,headers=c['headers'],trust_env=False,timeout=40)
        r.raise_for_status();j=r.json()
        assert not j['ok'] and j['error']['code']==code,j
        c['responses'].append({'negative':name,'response':j})
    def upload(key,via=op,*,family='pixel',role='image',fmt='PNG',image_mode='RGB'):
        b=io.BytesIO();im=Image.new(image_mode,(129,97) if family=='pixel' else (128,96),0 if image_mode=='L' else (17,80,160,91) if image_mode=='RGBA' else (17,80,160))
        im.putpixel((3,7),255 if image_mode=='L' else (255,0,0,187) if image_mode=='RGBA' else (255,0,0))
        if image_mode=='L':im.putpixel((91,23),73)
        info=PngImagePlugin.PngInfo();info.add_text('fixture','synthetic owned PNG; '+key)
        im.save(b,format=fmt,**({'pnginfo':info,'compress_level':0} if fmt=='PNG' else {}))
        raw=b.getvalue();(out/(key+'.'+fmt.lower())).write_bytes(raw)
        p={'request_id':'ingress-'+key,'project':'attribution-not-native-board','size':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'role':role}
        u=via('upload.begin',p);uid=u['upload_id']
        for offset in range(0,len(raw),32768):
            q={'upload_id':uid,'offset':offset,'base64':base64.b64encode(raw[offset:offset+32768]).decode()}
            via('upload.chunk',q);via('upload.chunk',q)
        assert via('upload.finalize',{'upload_id':uid})['state']=='ready'
        return {**ref,'request_id':'import-'+key,'upload_id':uid,'family':family,'board_id':None},raw
    def verify(job,raw,board,via=op):
        assert job['state']=='succeeded',job
        r=job['result'];name=r['image_name'];artifact=r['original']
        assert r['reference']=={**ref,'board_id':board}
        dto=via('images.get',{**ref,'image_name':name})['native']
        with Image.open(io.BytesIO(raw)) as source:dimensions=source.size
        assert dto['image_name']==name and dto['board_id']==board and (dto['width'],dto['height'])==dimensions
        data=bytearray();chunk=None
        while len(data)<artifact['size']:
            chunk=via('artifact.read',{'artifact_id':artifact['artifact_id'],'offset':len(data),'size':32768})
            data.extend(base64.b64decode(chunk['base64'],validate=True))
        assert bytes(data)==raw and chunk is not None and chunk['eof']
        assert artifact['sha256']==hashlib.sha256(raw).hexdigest() and artifact['representation']=='original-upload'
        with Image.open(io.BytesIO(raw)) as source:
            assert artifact['mime']==('image/png' if source.format=='PNG' else 'image/jpeg')
        assert via('artifact.list',{'job_id':job['id']})==[artifact]
        fetched=httpx.get(c['nu']+'/api/v1/images/i/'+name+'/full',headers=c['native_headers'],trust_env=False)
        fetched.raise_for_status()
        assert hashlib.sha256(fetched.content).hexdigest()==r['native_bytes_sha256']
        with Image.open(io.BytesIO(fetched.content)) as a,Image.open(io.BytesIO(raw)) as b:
            expected=ImageOps.invert(b) if r['role']=='mask' else b
            assert a.size==expected.size and a.mode==expected.mode and a.tobytes()==expected.tobytes()
            nr=r['native_representation']
            assert nr['pixels_sha256']==hashlib.sha256(expected.tobytes()).hexdigest()
            assert nr['original_sha256']==artifact['sha256'] and nr['upload_id']==r['upload_id'] and nr['import_job_id']==job['id']
            assert nr['download_sha256']==r['native_bytes_sha256'] and nr['download_size']==len(fetched.content)
            if r['role']=='mask':
                assert a.getpixel((3,7))==0 and a.getpixel((91,23))==182 and a.getpixel((0,0))==255
                assert nr['transform']=='white-edit-to-black-edit-invert-L-v1'
                assert nr['sha256']!=artifact['sha256'] and nr['mask_contract']=='black-edit-white-preserve'
            else:assert nr['transform']=='identity-v1' and nr['sha256']==artifact['sha256']
        return name

    # Discover actual routes of this running pinned server, not inferred CLI paths.
    schema=httpx.get(c['nu']+'/openapi.json',trust_env=False,timeout=40).json()
    selected={k:v for k,v in schema['paths'].items() if k in ('/api/v1/images/upload','/api/v1/images/i/{image_name}','/api/v1/boards/','/api/v1/boards/{board_id}/image_names','/api/v1/board_images/batch')}
    assert '/api/v1/images/upload' in selected
    save('native-openapi-selected.json',selected)
    for path in ('/v1/ops/upload.import','/mcp/'):
        for h,expected in [({},401),({'Authorization':'Bearer wrong'},401),({**c['headers'],'Host':'evil.test'},403),({**c['headers'],'Origin':'https://evil.test'},403)]:
            assert httpx.post(c['au']+path,json={},headers=h,trust_env=False).status_code==expected
    passed('running native OpenAPI inspected; shared HTTP/MCP bearer/Host/Origin refusal')

    existing=native('POST','/api/v1/boards/?board_name=Existing-owned-board')['board_id']
    bp={**ref,'request_id':'board-created','name':'Atlas owned synthetic images'}
    board_job=op('boards.create',bp);assert board_job['state']=='succeeded',board_job
    bid=board_job['result']['board']['board_id']
    assert mcp('boards.create',bp)['id']==board_job['id']
    fail('boards.create',{**bp,'name':'changed'},'IDEMPOTENCY_CONFLICT')
    p,raw=upload('http');p['board_id']=bid
    fail('upload.import',{k:v for k,v in p.items() if k!='connection_id'},'VALIDATION')
    fail('upload.import',{**p,'backend_generation':'wrong'},'NATIVE_SCOPE_MISMATCH')
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(op,'upload.import',p) for _ in range(2)]
        first,duplicate=[f.result() for f in futures]
    assert first['id']==duplicate['id'] and first['result']==duplicate['result']
    name=verify(first,raw,bid)
    fail('upload.import',{**p,'board_id':existing},'IDEMPOTENCY_CONFLICT')
    fail('upload.import',{**p,'request_id':'second-intent-same-upload'},'UPLOAD_STATE')
    p2,raw2=upload('mcp',mcp);p2['board_id']=existing
    second=mcp('upload.import',p2);other=verify(second,raw2,existing,mcp)
    p3,raw3=upload('uncategorized');third=op('upload.import',p3);uncat=verify(third,raw3,None)
    assert op('boards.images',{'board_id':bid})==[name]
    assert op('boards.images',{'board_id':existing})==[other]
    assert uncat in op('boards.images',{'board_id':'none'})
    passed('HTTP and SDK MCP chunked upload -> exact native import/board/readback -> original byte/hash/dimension artifact; explicit uncategorized route')

    restored=[]
    for family,role,fmt,image_mode,via in [('sd-1','image','JPEG','RGB',op),('anima','image','PNG','RGBA',mcp),('sd-1','mask','PNG','L',mcp),('anima','mask','PNG','L',op)]:
        req,original=upload(family+'-'+role,via,family=family,role=role,fmt=fmt,image_mode=image_mode)
        before=count('/api/v1/images/upload')
        if not restored:
            stop('atlas')
            config=json.loads(c['config'].read_text());config['connections'][0]['enable_writes']=False
            c['config'].write_text(json.dumps(config));start_atlas()
            fail('upload.import',req,'ADMIN_WRITES_DISABLED')
            assert op('upload.get',{'upload_id':req['upload_id']})['state']=='ready'
            assert count('/api/v1/images/upload')==before
            stop('atlas');config['connections'][0]['enable_writes']=True
            c['config'].write_text(json.dumps(config));start_atlas()
        fail('upload.import',{**req,'family':'pixel'},'MASK_FAMILY' if role=='mask' else 'PIXEL_FORMAT')
        fail('upload.import',{**req,'family':'unsupported'},'VALIDATION')
        fail('upload.import',{**req,'connection_id':'other'},'NATIVE_SCOPE_MISMATCH')
        fail('upload.import',{k:v for k,v in req.items() if k!='board_id'},'VALIDATION')
        fail('upload.import',{**req,'board_id':'absent-board'},'REMOTE_HTTP_404')
        assert count('/api/v1/images/upload')==before
        job=via('upload.import',req);verify(job,original,None,via)
        assert op('upload.get',{'upload_id':req['upload_id']})['state']=='imported'
        assert mcp('upload.import',req)['result']==job['result']
        fail('upload.import',{**req,'family':'anima' if family=='sd-1' else 'sd-1'},'IDEMPOTENCY_CONFLICT')
        fail('upload.import',{**req,'request_id':'new-'+req['request_id']},'UPLOAD_STATE')
        restored.append({'request':req,'job':job,'original_sha256':hashlib.sha256(original).hexdigest()})
        assert count('/api/v1/images/upload')==before+1
    save('family-mask.json',restored)
    passed('restored sd-1 JPEG/anima RGBA images and both asymmetric L masks through scoped HTTP/MCP; original/native hashes, explicit inversion, family/scope/board refusals and replay verified')

    move={**ref,'request_id':'move-one','action':'move','board_id':existing,'image_names':[name],'project':'unrelated-attribution'}
    moved=mcp('boards.images.update',move);assert moved['state']=='succeeded',moved
    assert op('images.get',{**ref,'image_name':name})['native']['board_id']==existing
    assert op('images.get',{**ref,'image_name':other})['native']==second['result']['native']
    assert set(op('boards.images',{'board_id':existing}))=={name,other}
    assert op('boards.images',{'board_id':bid})==[]
    passed('exact selected image moves to existing board; unrelated native image/board membership unchanged; attribution remains independent')

    # ACK saved, later read fails. Replay may only GET the exact acknowledged ID.
    pr,rr=upload('readback-failure',family='sd-1',role='mask',image_mode='L');pr['board_id']=bid
    mode['replace_full']=rr
    pending=op('upload.import',pr)
    assert pending['state']=='ack_unknown' and pending.get('native_receipt'),pending
    assert op('upload.get',{'upload_id':pr['upload_id']})['state']!='imported'
    before=count('/api/v1/images/upload')
    stop('atlas');start_atlas()
    recovered=mcp('upload.import',pr);verify(recovered,rr,bid)
    assert count('/api/v1/images/upload')==before
    passed('uninverted original mask substituted at download fails transformed-pixel verification; durable exact ACK survives restart; SDK MCP GET-only recovery without POST')

    # Native commits; reply lost before Atlas sees its generated ID. No inference.
    pl,rl=upload('lost-ack',family='anima',role='mask',image_mode='L');pl['board_id']=bid
    mode['drop_path']='/api/v1/images/upload'
    lost=mcp('upload.import',pl)
    assert lost['state']=='ack_unknown' and not lost.get('native_receipt'),lost
    before=count('/api/v1/images/upload')
    assert op('upload.import',pl)['id']==lost['id']
    fail('upload.import',{**pl,'request_id':'new-id-unknown-upload'},'UPLOAD_STATE')
    assert count('/api/v1/images/upload')==before
    # Process death in the same window exercises persisted submitting recovery.
    pc,rc=upload('crash-ack',family='sd-1',role='mask',image_mode='L');pc['board_id']=bid
    mode.update(drop_path='/api/v1/images/upload',crash_atlas=True)
    try:
        httpx.post(c['au']+'/v1/ops/upload.import',json=pc,headers=c['headers'],trust_env=False,timeout=45)
    except httpx.TransportError:pass
    assert c['processes']['atlas'].wait(timeout=15)==-9
    start_atlas()
    crashed=op('upload.import',pc)
    assert crashed['state']=='ack_unknown' and not crashed.get('native_receipt'),crashed
    assert op('upload.get',{'upload_id':pc['upload_id']})['state']=='ack_unknown'
    assert count('/api/v1/images/upload')==before+1
    lost_board={**ref,'request_id':'board-lost','name':'Unknown ACK board'}
    mode['drop_path']='/api/v1/boards/'
    lb=op('boards.create',lost_board);assert lb['state']=='ack_unknown' and not lb.get('native_receipt'),lb
    board_posts=count('/api/v1/boards/')
    stop('atlas');stop('native');start_native();start_atlas()
    for req,j in [(pl,lost),(pc,crashed)]:
        for via in (op,mcp):
            result=via('upload.import',req)
            assert result['id']==j['id'] and result['state']=='ack_unknown' and not result.get('native_receipt')
    assert mcp('boards.create',lost_board)['id']==lb['id']
    assert count('/api/v1/images/upload')==before+1 and count('/api/v1/boards/')==board_posts
    assert op('upload.import',p)['result']==first['result']
    assert mcp('upload.import',p2)['result']==second['result']
    assert mcp('boards.images.update',move)['result']==moved['result']
    for item in restored:assert mcp('upload.import',item['request'])['result']==item['job']['result']
    passed('lost upload/create ACK and real Atlas SIGKILL; both servers restart; unknown remains explicit and HTTP/MCP replay never POSTs or adopts byte/name matches')

    # Completed snapshots are historical; moving later does not rewrite old receipt.
    stop('native')
    assert op('upload.import',p)['result']==first['result']
    chunk=mcp('artifact.read',{'artifact_id':first['result']['original']['artifact_id'],'size':32768})
    assert base64.b64decode(chunk['base64'])==raw[:32768]
    assert op('admin.get',{'job_id':lost['id']})['state']=='ack_unknown'
    passed('native offline: completed historical receipts/replay and downloadable original artifact remain available')
    stop('atlas')
    with sqlite3.connect(out/'native/databases/invokeai.db') as db:
        db.row_factory=sqlite3.Row
        images=[dict(r) for r in db.execute('select image_name,width,height,user_id from images')]
        boards=[dict(r) for r in db.execute('select board_id,board_name from boards')]
        migrations=[dict(r) for r in db.execute('select * from migrations')]
        assert len(images)==count('/api/v1/images/upload')==10
        assert len(boards)==board_posts+1==3
        assert all((r['width'],r['height']) in ((129,97),(128,96)) and r['user_id']==c['user']['user_id'] for r in images)
        assert db.execute('select count(*) from session_queue').fetchone()[0]==0
        assert db.execute('select count(*) from models').fetchone()[0]==0
        assert db.execute('pragma integrity_check').fetchone()[0]=='ok'
        assert sorted(r['version'] for r in migrations)==list(range(35))
    save('database.json',{'images':images,'boards':boards,'migrations':migrations,'models':0,'queue_rows':0,'integrity_check':'ok'})
    passed('original migrations/storage integrity; exactly ten native images/three boards; zero queue entries/models')
    save('result.json',{'checks':c['checks'],'upload_posts':count('/api/v1/images/upload'),'board_posts':board_posts,
        'jobs':{'http':first['id'],'mcp':second['id'],'uncategorized':third['id'],'get_recovered':recovered['id'],'lost_ack':lost['id'],'crash_ack':crashed['id'],'lost_board':lb['id']},
        'limitations':['Synthetic pixels, original full native acceptance; no image generation','Unknown native upload/create ACK cannot be exactly reconciled; no retry POST','Original ingress bytes retained; native PNG bytes may differ','Receipt readback is a historical observation, not ongoing remote CAS']})
