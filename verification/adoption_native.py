"""Disposable CPU protocol fixture; no native server/model/GPU execution claim."""
import argparse,json,sys,sqlite3
from pathlib import Path

def run(a):
    sys.path.insert(0,str(a.package.parent))
    import httpx
    from atlas_engine import adoption_native as n,adoption_control as c,state_backup as s,adoption as ad,upstream
    from atlas_engine.repository import digest,dump
    from atlas_engine.engine import Engine
    from atlas_engine.config import Config,bind_connections
    from atlas_engine.connections import install
    def audit(event,args):
        if event=='socket.connect':raise AssertionError('fixture outbound socket forbidden')
    sys.addaudithook(audit)
    root=a.fixture/'target';state=s.restore_state(root)
    binding=json.loads((root/'adoption-receipt.json').read_text())['plan']['native_binding']
    binding={k:v for k,v in binding.items() if k!='enable_writes'}
    schema={'fixture':True};items={};receipts={};calls=[];boards={}
    def response(request):
        p=request.url.path;method=request.method;calls.append([method,p]);body=json.loads(request.content) if request.content else {}
        result=None
        if p=='/api/v1/app/version':result={'version':'fixture'}
        elif p=='/api/v1/auth/status':result={'setup_required':False,'multiuser_enabled':False}
        elif p=='/openapi.json':result=schema
        elif p.endswith('/status'):result={'queue':{'queue_id':'default'}}
        elif p.endswith('/item_ids'):result={'item_ids':sorted(items),'total_count':len(items)}
        elif '/queue/default/i/' in p:result=items[int(p.rsplit('/',1)[1])]
        elif p.endswith('/enqueue_batch'):
            b=body['batch'];nid=1000+len(items);items[nid]={'item_id':nid,'user_id':'system','status':'pending','batch_id':b['batch_id'],'origin':b['origin'],'session':{'graph':b['graph'],'results':{}}}
            result={'queue_id':'default','batch':{'batch_id':b['batch_id']},'item_ids':[nid],'requested':1,'enqueued':1}
            receipts[b['idempotency_key']]={**result,'batch_id':b['batch_id']}
        elif p.endswith('/enqueue_batch/acknowledge'):result={}
        elif p.endswith('/enqueue_batch/receipt'):
            result=receipts.get(request.url.params['idempotency_key'])
            if result is None:return httpx.Response(404,json={'detail':'missing'})
        elif p=='/api/v1/boards/' and method=='POST':
            result={'board_id':'fixture-board','board_name':request.url.params['board_name']};boards[result['board_id']]=result
        elif p.startswith('/api/v1/boards/'):result=boards[p.rsplit('/',1)[1]]
        elif p=='/api/v2/models/':result={'models':[]}
        else:raise AssertionError((method,p))
        return httpx.Response(200,json=result)
    Original=upstream.Invoke
    def inv_factory(url,*args,**kw):
        inv=Original(url,*args,**kw);inv.http.close();inv.http=httpx.Client(base_url=url,transport=httpx.MockTransport(response),trust_env=False);return inv
    upstream.Invoke=inv_factory
    report={'target_root':str(root),'runtime':s.versions(),'capture_receipt_sha256':state['adoption_receipt_sha256'],
        'refresh':state.get('refresh'),'registry_logical_sha256':s.registry_digest(root),
        'admission':n.ADMISSION,'previous_admission':None,'held_rows':n.required_holds(root,state),
        'operator':'fixture','evidence':[ad.pin(a.fixture/'result.json')],
        'native':{'binding':binding,'version':{'version':'fixture'},'openapi_sha256':digest(schema)},
        'final_cut_review':c.bound_record(state['refresh'])['held_review'],
        'progress_assessment':'Original intent invariant equal; source terminal observation accepted without replay; original receipt retained.'}
    with ad.connection(root/'registry.sqlite3') as db:report['historical_native_ids']=[r[0] for r in db.execute("select id from jobs where kind in ('generation','native_admin') order by id")]
    rp=a.fixture/'native.json'
    if state.get('held_admission'):
        report=c.bound_record(state['held_admission']);n.validate(root,state,report)
    else:
        s.write_json(rp,report);n.activate(root,rp,s.sha(rp))
    config=Config(root=root,clients_file=a.fixture/'unused-clients.json',connections=[{**binding,'enable_writes':True}]);manifest=bind_connections(root,config.connections,True)
    def engine():
        e=Engine(root,a.fixture/'fixture-vocabulary.sqlite3',inv_factory(binding['url']),object(),True,{'accepted_modes':['native_graph']},allow_admin=True)
        install(e,config,manifest,{binding['id']:inv_factory(binding['url'])});return e
    e=engine();pins=n.row_pins(root,[r['id'] for r in report['held_rows']]);assert e.allow_submit and next(iter(e.connection_contexts.values())).native_admin.enabled
    def call(op,p):
        r=e.call(op,p,'fixture-new','fixture');assert r['ok'],r;return r['result']
    p={'request_id':'new-graph','connection_id':binding['id'],'backend_generation':binding['backend_generation'],'graph':{'id':'cpu-graph','nodes':{'integer':{'id':'integer','type':'integer','value':1}},'edges':[]}}
    new=call('generation.submitGraph',p);assert new['state']=='observing',new
    count=lambda:sum(m=='POST' and path.endswith('/enqueue_batch') for m,path in calls)
    assert count()==1;assert call('generation.submitGraph',p)['id']==new['id'] and count()==1
    view=next(iter(e.connection_contexts.values()));nid=new['native_ids'][0]
    items[nid]['status']='completed';view.reconcile();assert e.repo.get(new['id'])['state']=='succeeded'
    admin=call('boards.create',{'request_id':'new-board','name':'fixture only','connection_id':binding['id'],'backend_generation':binding['backend_generation']});assert admin['state']=='succeeded'
    # Historical read/exact retry/conflict/cancel all preserve prior rows.
    old=next(j for j in e.repo.iter_jobs('generation') if j['id'] in {r['id'] for r in pins})
    old_result=e.call('generation.submit',old['payload'],'fixture','old-native');assert old_result['ok'],old_result
    bad=e.call('generation.submit',{**old['payload'],'prompt':'changed'},'fixture','old-native');assert bad['error']['code']=='IDEMPOTENCY_CONFLICT',bad
    bad=e.call('generation.cancel',{'job_id':old['id']});assert bad['error']['code']=='HELD_NATIVE_EFFECT',bad
    # Legitimate queued and unknown NEW intents survive restart; no 404 enqueue.
    queued,_=e.repo.reserve('fixture-new','fixture','restart-queued','generation',p,{'native_scope':view.native_queue.scope(),'plan':{'mode':'native_graph','graph':p['graph']},'native_ids':[]})
    unknown,_=e.repo.reserve('fixture-new','fixture','restart-unknown','generation',{**p,'request_id':'restart-unknown'},{'native_scope':view.native_queue.scope(),'plan':{'mode':'native_graph','graph':p['graph']},'native_ids':[]})
    prepared=view.native_queue.prepare(unknown);e.repo.update(unknown['id'],'ack_unknown',{'prepared_submission':prepared})
    e.close();e=engine();e.start()
    import time
    until=time.monotonic()+10
    while time.monotonic()<until and e.repo.get(queued['id'])['state']=='queued':time.sleep(.05)
    assert e.repo.get(queued['id'])['state']=='observing'
    assert e.repo.get(unknown['id'])['state']=='ack_unknown'
    e.close();assert count()==2;assert n.row_pins(root,[r['id'] for r in pins])==pins
    s.write_json(a.fixture/'native-result.json',{'passed':True,'scope':'disposable MockTransport CPU protocol fixture; graph execution substituted; no real native/GPU/install effects',
        'new_enqueue_posts':count(),'new_admin_succeeded':True,'completed_progress':True,'restart_new_queued':True,'unknown404_not_success_or_reenqueue':True,'original_holds_exact':True,'calls':calls})
    print('PASS native configured fixture: positive new submission/admin, progress, restart queued/unknown404, exact historical pins')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--package',type=Path,required=True);p.add_argument('--fixture',type=Path,required=True);run(p.parse_args())
