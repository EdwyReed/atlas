"""Installed HTTP/SDK MCP -> TWO original authenticated no-model Invoke servers.

Reuses native_fullserver's original launcher/config/environment primitives.
Private owned roots only. No native services/tables replaced or model downloads.
"""
import argparse, asyncio, base64, hashlib, io, json, os, secrets, signal, sqlite3
import subprocess, sys, threading, time, zipfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import httpx
from PIL import Image
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import atlas_engine
sys.path.insert(0,str(Path(__file__).absolute().parent))
from native_fullserver import free_port, native_configuration, native_environment, native_command


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--native-python',type=Path,required=True)
    parser.add_argument('--wheel',type=Path,required=True)
    a=parser.parse_args();out=a.out.absolute();out.mkdir(parents=True,exist_ok=False,mode=0o700);os.umask(0o077)
    def save(name,data): (out/name).write_text(json.dumps(data,indent=2))
    source=Path(__file__).absolute().parents[1]/'atlas_engine'
    installed=Path(atlas_engine.__file__).parent
    def inventory(root):return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*.py')}
    expected=inventory(source);assert installed!=source and inventory(installed)==expected
    with zipfile.ZipFile(a.wheel) as z:
        payload={n.removeprefix('atlas_engine/'):hashlib.sha256(z.read(n)).hexdigest() for n in z.namelist() if n.startswith('atlas_engine/') and not n.endswith('/')}
    assert payload==expected
    save('module-equality.json',{'modules':expected,'installed':str(installed),'wheel_sha256':hashlib.sha256(a.wheel.read_bytes()).hexdigest()})
    def runtime():
        code="import hashlib,json,invokeai;from pathlib import Path;r=Path(next(iter(invokeai.__path__)));p=list(r.rglob('*.py'))+[Path(__import__('sys').executable).with_name('invokeai-web')];print(json.dumps({str(x):[hashlib.sha256(x.read_bytes()).hexdigest(),x.stat().st_ino,str(x.resolve())] for x in p}))"
        return json.loads(subprocess.check_output([str(a.native_python),'-I','-B','-c',code],text=True))
    before=runtime();save('native-runtime-before.json',before)
    home=out/'home';home.mkdir();env=native_environment(a.native_python,home,out)
    ports={k:free_port() for k in ('A','B','atlas')};urls={k:f'http://127.0.0.1:{v}' for k,v in ports.items()}
    processes={};all_processes=[];logs=[];proxies=[];calls=[];intents=[];responses=[];checks=[];fault={'drop':None}
    config=out/'atlas.json';token=secrets.token_urlsafe(36);headers={'Authorization':'Bearer '+token};native_headers={};roots={}
    (out/'clients.json').write_text(json.dumps([{'token':token,'principal':'shared-acceptance','source':'dual-fullserver'}]))
    (out/'policy.json').write_text(json.dumps({'accepted_modes':['native_graph','saved_workflow']}))
    def passed(text):checks.append(text);save('checks.json',checks);print('PASS',text,flush=True)
    def launch(name,argv,url):
        log=(out/(name+'.log')).open('a');logs.append(log)
        p=subprocess.Popen(argv,env=env,cwd=out,stdout=log,stderr=log);processes[name]=p;all_processes.append((name,p))
        for _ in range(1200):
            assert p.poll() is None,(name,p.returncode)
            try:
                if httpx.get(url,trust_env=False,timeout=1).status_code<500:return
            except httpx.TransportError:pass
            time.sleep(.1)
        raise AssertionError(name+' startup timeout')
    def stop(name):
        p=processes.get(name)
        if p and p.poll() is None:
            p.send_signal(signal.SIGINT if name in ('A','B') else signal.SIGTERM)
            try:p.wait(timeout=50)
            except subprocess.TimeoutExpired:p.kill();p.wait();raise AssertionError(name+' shutdown timeout')
    def start_native(key):launch(key,native_command(a.native_python,roots[key]),urls[key]+'/api/v1/auth/status')
    def start_atlas():launch('atlas',[sys.executable,'-I','-B','-m','atlas_engine.server','--config',str(config)],urls['atlas']+'/health')
    def native(key,method,path,body=None):
        r=httpx.request(method,urls[key]+path,json=body,headers=native_headers.get(key,{}),trust_env=False,timeout=45)
        assert r.is_success,(key,r.status_code,r.text);return r.json()
    def op(name,args,code=None):
        r=httpx.post(urls['atlas']+'/v1/ops/'+name,json=args,headers=headers,trust_env=False,timeout=60);r.raise_for_status();v=r.json()
        responses.append({'op':name,'args':args,'response':v})
        if code:assert not v['ok'] and v['error']['code']==code,v;return v
        assert v['ok'],v;return v['result']
    async def sdk(name,args):
        async with httpx.AsyncClient(headers=headers,trust_env=False) as client:
            async with streamable_http_client(urls['atlas']+'/mcp/',http_client=client) as streams:
                async with ClientSession(streams[0],streams[1]) as s:
                    await s.initialize();v=(await s.call_tool(name.replace('.','_'),args)).structuredContent
                    responses.append({'mcp':name,'args':args,'response':v});assert v['ok'],v;return v['result']
    def mcp(name,args):return asyncio.run(sdk(name,args))
    def proxy_for(key):
        class Proxy(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def forward(self):
                body=self.rfile.read(int(self.headers.get('Content-Length',0)));ct=self.headers.get('Content-Type','')
                record={'connection_id':key,'method':self.command,'path':self.path};calls.append(record)
                mutation=(self.command in ('POST','PUT','PATCH','DELETE') and self.path.startswith('/api/')
                          and not self.path.endswith(('/enqueue_batch/acknowledge','/items_by_ids')))
                if mutation:
                    with sqlite3.connect(out/'state/registry.sqlite3') as db:
                        rows=db.execute("select id,kind,data from jobs where state='submitting'").fetchall()
                        candidates=[]
                        for jid,kind,raw in rows:
                            j=json.loads(raw)
                            if j.get('payload',{}).get('connection_id')==key:
                                if self.path.endswith('/enqueue_batch') and j.get('prepared_submission')==json.loads(body):candidates.append((jid,j))
                                elif not self.path.endswith('/enqueue_batch'):candidates.append((jid,j))
                        assert len(candidates)==1,(key,self.path,rows)
                        jid,j=candidates[0]
                        assert j['payload']['backend_generation']=='native-'+key
                        intents.append({'connection_id':key,'job_id':jid,'path':self.path,'committed_before_forward':True})
                h={'Content-Type':ct} if ct else {}
                if self.headers.get('Authorization'):h['Authorization']=self.headers['Authorization']
                try:r=httpx.request(self.command,urls[key]+self.path,content=body or None,headers=h,trust_env=False,timeout=45)
                except httpx.TransportError:r=httpx.Response(503,json={'detail':'owned backend offline'})
                record['status']=r.status_code
                if self.path.endswith('/enqueue_batch') and fault['drop']==key:
                    assert r.is_success,r.text;fault['drop']=None
                    processes['atlas'].kill();processes['atlas'].wait(timeout=10)
                    self.close_connection=True;return
                try:
                    self.send_response(r.status_code);self.send_header('Content-Type',r.headers.get('Content-Type','application/octet-stream'));self.send_header('Content-Length',str(len(r.content)));self.end_headers();self.wfile.write(r.content)
                except (BrokenPipeError,ConnectionResetError):pass
            do_GET=do_POST=do_PUT=do_PATCH=do_DELETE=forward
        proxy=ThreadingHTTPServer(('127.0.0.1',0),Proxy);thread=threading.Thread(target=proxy.serve_forever,daemon=True);thread.start();proxies.append((proxy,thread));return proxy
    refs={key:{'connection_id':key,'backend_generation':'native-'+key} for key in ('A','B')}
    def graph(key,rid,value):return {**refs[key],'request_id':rid,'graph':{'id':'collision-graph','nodes':{'n':{'id':'n','type':'integer','value':value,'is_intermediate':True,'use_cache':False}},'edges':[]},'runs':1}
    def complete(job,value):
        key=job['payload']['connection_id']
        for _ in range(180):
            op('observer.reconcile',refs[key]);j=op('generation.get',{'job_id':job['id']})
            if j['state']=='succeeded':break
            time.sleep(.2)
        assert j['state']=='succeeded',j
        entries=j['native_items'][str(j['native_ids'][0])]['native_results']['entries']
        assert len(entries)==1 and entries[0]['value']==value and type(entries[0]['value']) is int,j
        assert j['native_scope']=={**refs[key],'queue_id':'default'}
        return j
    def enqueues(key):return sum(c['connection_id']==key and c['method']=='POST' and c['path'].endswith('/enqueue_batch') for c in calls)
    def node(nid,kind,inputs):return {'id':nid,'type':'invocation','position':{'x':0,'y':0},'data':{'id':nid,'type':kind,'version':'1.0.0','nodePack':'invokeai','isIntermediate':True,'useCache':False,'inputs':{k:{'value':v} for k,v in inputs.items()}}}
    def edge(src,sh,dst,dh):return {'id':src+'-'+dst,'type':'default','source':src,'sourceHandle':sh,'target':dst,'targetHandle':dh}
    try:
        connections=[];auth={}
        for key in ('A','B'):
            root=out/('native-'+key);root.mkdir();roots[key]=root;(root/'invokeai.yaml').write_text(native_configuration(ports[key]));start_native(key)
            assert native(key,'GET','/api/v1/auth/status')['setup_required']
            credentials={'email':'atlas-'+key.lower()+'@example.test','password':secrets.token_urlsafe(32)+'aA7!'}
            setup=native(key,'POST','/api/v1/auth/setup',{**credentials,'display_name':'Owned '+key});assert setup['success']
            login=native(key,'POST','/api/v1/auth/login',{**credentials,'remember_me':False});native_headers[key]={'Authorization':'Bearer '+login['token']}
            token_file=out/('native-token-'+key);token_file.write_text(login['token']);token_file.chmod(0o600)
            me=native(key,'GET','/api/v1/auth/me');auth[key]=me['user_id']
            assert httpx.get(urls[key]+'/api/v1/queue/default/status',headers=native_headers.get('B' if key=='A' else 'A',{'Authorization':'Bearer wrong'}),trust_env=False).status_code in (401,403)
            schema=httpx.get(urls[key]+'/openapi.json',trust_env=False,timeout=45).json();save('openapi-'+key+'.json',schema)
            proxy=proxy_for(key)
            connections.append({'id':key,'backend_generation':'native-'+key,'account_scope':me['user_id'],'url':f'http://127.0.0.1:{proxy.server_port}','token_file':token_file.name,'enable_writes':True})
        assert auth['A']!=auth['B'];save('native-users.json',auth)
        settings={'root':'state','clients_file':'clients.json','policy_file':'policy.json','port':ports['atlas'],'connections':connections}
        config.write_text(json.dumps(settings));save('commands.json',{'native':{k:native_command(a.native_python,r) for k,r in roots.items()},'atlas':[sys.executable,'-I','-B','-m','atlas_engine.server','--config',str(config)],'env':env})
        start_atlas();passed('two original launcher/auth/fresh roots; separate users/tokens; one installed Atlas owner')
        cap=mcp('capabilities',{});assert len(cap['connections'])==2 and 'token_file' not in json.dumps(cap)
        op('queue.status',{},'CONNECTION_REQUIRED');op('queue.status',{'connection_id':'missing'},'CONNECTION_UNKNOWN');op('queue.status',{**refs['A'],'backend_generation':'wrong'},'NATIVE_SCOPE_MISMATCH')
        op('generation.submitGraph',{'request_id':'no-scope','graph':{}},'VALIDATION')
        op('models.acquire.enqueue',{'request_id':'unsupported','urls':['https://civitai.red/models/1']},'MULTI_CONNECTION_UNSUPPORTED')
        ga,gb=graph('A','concurrent-A',17),graph('B','concurrent-B',29)
        with ThreadPoolExecutor(2) as pool:
            fa=pool.submit(op,'generation.submitGraph',ga);fb=pool.submit(mcp,'generation.submitGraph',gb);ja,jb=fa.result(),fb.result()
        ja,jb=complete(ja,17),complete(jb,29)
        assert ja['native_ids']==jb['native_ids']==[1] and ja['id']!=jb['id']
        op('generation.submitGraph',{**ga,**refs['B']},'IDEMPOTENCY_CONFLICT')
        assert enqueues('A')==enqueues('B')==1
        save('colliding-jobs.json',[ja,jb]);passed('simultaneous HTTP/SDK MCP integer17/integer29; native item ID1 collides; correct results/scoped observations; shared request conflict before POST')
        # Actual upload/admin and saved-library routing, not just queue routing.
        imports={};workflow_requests={};workflow_jobs={};image_jobs={};project_requests={}
        for key,color,value in [('A',(17,3,5),41),('B',(2,29,7),53)]:
            board=op('boards.create',{**refs[key],'request_id':'board-'+key,'name':'same-board-name'})
            assert board['state']=='succeeded',board
            bid=board['result']['board']['board_id'];assert mcp('boards.get',{**refs[key],'board_id':bid})['board']['board_name']=='same-board-name'
            image=Image.new('RGB',(3,2),color);buf=io.BytesIO();image.save(buf,format='PNG');raw=buf.getvalue()
            u=op('upload.begin',{'request_id':'upload-'+key,'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
            op('upload.chunk',{'upload_id':u['upload_id'],'offset':0,'base64':base64.b64encode(raw).decode()});op('upload.finalize',{'upload_id':u['upload_id']})
            imported=mcp('upload.import',{**refs[key],'request_id':'import-'+key,'upload_id':u['upload_id'],'family':'pixel','board_id':bid});assert imported['state']=='succeeded',imported
            imports[key]=imported;name=imported['result']['image_name'];assert op('images.get',{**refs[key],'image_name':name})['native']['board_id']==bid
            assert base64.b64decode(op('artifact.read',{'artifact_id':imported['result']['original']['artifact_id']})['base64'])==raw
            other='B' if key=='A' else 'A';op('images.get',{**refs[other],'image_name':name},'REMOTE_HTTP_404')
            image_req=graph(key,'resize-'+key,0)
            image_req['graph']={'id':'same-image-graph','nodes':{'resize':{'id':'resize','type':'img_resize','image':{'image_name':name},'width':5,'height':4,'resample_mode':'nearest','is_intermediate':False,'use_cache':False}},'edges':[]}
            image_job=op('generation.submitGraph',image_req)
            for _ in range(100):
                op('observer.reconcile',refs[key]);image_job=op('generation.get',{'job_id':image_job['id']})
                if image_job['state']=='succeeded' and image_job.get('outputs'):break
                time.sleep(.2)
            assert image_job['state']=='succeeded' and len(image_job['outputs'])==1,image_job
            image_jobs[key]=image_job
            # Same literal project ID exists in BOTH original native DBs.
            content={'documentSchemaVersion':4,'id':'same-project','name':'Owned '+key,'opaque':key}
            workspace={'documentSchemaVersion':4,'settingsLayering':1,'widgetInstances':{'g':{'typeId':'generate','state':{'values':{'positivePrompt':'before-'+key,'untouched':key}}}},'opaque':key}
            created_project=native(key,'POST','/api/v1/projects/',{'name':'Owned '+key,'project_id':'same-project','data':content,'workspace':workspace,'minimum_document_schema_version':4,'max_document_schema_version':4,'minimum_canvas_schema_version':2,'max_canvas_schema_version':2})
            pr={**refs[key],'project_id':'same-project'};current=mcp('projects.get',pr)
            assert current['native']['data']==content
            pp={**pr,'request_id':'prompt-'+key,'widget_instance_id':'g','prompt':'after-'+key,'expected_workspace_revision':current['workspace_revision']}
            changed=op('projects.savePrompt',pp);assert changed['state']=='succeeded',changed
            direct=native(key,'GET','/api/v1/projects/same-project?max_document_schema_version=4&max_canvas_schema_version=2')
            assert direct['data']==content and direct['workspace']['data']=={**workspace,'widgetInstances':{'g':{'typeId':'generate','state':{'values':{'positivePrompt':'after-'+key,'untouched':key}}}}}
            project_requests[key]=(pp,changed['id'])
            workflow={'name':'same-library-name','author':'owned','description':'','version':'1.0.0','contact':'','tags':'','notes':'','meta':{'version':'1.0.0','category':'user'},'exposedFields':[{'nodeId':'n','fieldName':'value'}],'form':None,'nodes':[node('n','integer',{'value':0}),node('rv','workflow_return_value',{'key':'result','value':None}),node('ret','workflow_return',{'values':[]})],'edges':[edge('n','value','rv','value'),edge('rv','value','ret','values')]}
            created=native(key,'POST','/api/v1/workflows/',{'workflow':workflow});wid=created['workflow_id']
            wr={**refs[key],'workflow_id':wid};assert op('workflows.schema',wr)['workflow_id']==wid
            op('workflows.schema',{**wr,**refs[other]},'REMOTE_HTTP_404')
            req={**wr,'request_id':'workflow-'+key,'version_semantics':'latest_at_execution','workflow_inputs':{'saved_workflow_input::n::value':value},'return_key':'result'}
            workflow_requests[key]=req;workflow_jobs[key]=complete(mcp('workflows.run',req),value)
        save('imports.json',imports);save('workflow-jobs.json',workflow_jobs);save('image-jobs.json',image_jobs)
        passed('both backends: board create/read, SDK upload/import/exact original artifacts, scoped image/library lookup and native saved integer child; cross-backend refs refused')
        count={k:enqueues(k) for k in ('A','B')}
        stop('atlas');stop('A');stop('B');start_native('A');start_native('B');start_atlas()
        for key,req in workflow_requests.items():assert op('workflows.run',req)['id']==workflow_jobs[key]['id']
        for key,(req,jid) in project_requests.items():assert mcp('projects.savePrompt',req)['id']==jid
        assert mcp('generation.submitGraph',ga)['id']==ja['id'] and op('generation.submitGraph',gb)['id']==jb['id']
        assert count=={k:enqueues(k) for k in ('A','B')};passed('both original servers and Atlas restart; persisted scoped HTTP/MCP replay no extra POST')
        fault['drop']='A';lostreq=graph('A','lost-A',67)
        try:op('generation.submitGraph',lostreq)
        except httpx.TransportError:pass
        processes['atlas'].wait(timeout=10)
        assert processes['atlas'].poll() is not None and fault['drop'] is None
        with sqlite3.connect(out/'state/registry.sqlite3') as db:
            row=db.execute("select id,state,data from jobs where request_id='lost-A'").fetchone();assert row and row[1]=='submitting';lost={'id':row[0],**json.loads(row[2])}
        start_atlas();lost=complete(lost,67);assert op('generation.submitGraph',lostreq)['id']==lost['id']
        assert enqueues('A')==4 and enqueues('B')==3;save('lost-job.json',lost)
        passed('A enqueue committed, ACK lost and Atlas SIGKILL; original A receipt GET recovery after restart; no B lookup/mutation and no enqueue replay')
        # Change compatibility default only for NEW commands; historical replay
        # and job controls stay pinned, while A outage must never fallback to B.
        stop('atlas');settings.update(compatibility_routing=True,default_connection_id='B');config.write_text(json.dumps(settings));start_atlas()
        fetched={}
        for key,color in [('A',(17,3,5)),('B',(2,29,7))]:
            job=image_jobs[key];artifact=mcp('artifact.fetch',{'job_id':job['id'],'image_name':job['outputs'][0]})
            raw=base64.b64decode(op('artifact.read',{'artifact_id':artifact['artifact_id']})['base64'])
            with Image.open(io.BytesIO(raw)) as im:assert im.size==(5,4) and im.tobytes()==Image.new('RGB',(5,4),color).tobytes()
            fetched[key]=artifact
        save('remote-artifacts.json',fetched)
        passed('colliding project IDs patch only own scoped workspace; generated IMAGE remote fetch after default changes uses persisted job backend, exact pixels')
        stop('A')
        bcount=enqueues('B');op('queue.status',refs['A'],'REMOTE_HTTP_503');assert op('queue.status',{})==op('queue.status',refs['B'])
        assert op('generation.submitGraph',ga)['id']==ja['id']
        assert op('generation.get',{'job_id':lost['id']})['state']=='succeeded'
        for key in ('A','B'):
            artifact=imports[key]['result']['original'];assert mcp('artifact.read',{'artifact_id':artifact['artifact_id']})['sha256']==artifact['sha256']
            assert mcp('artifact.fetch',{'job_id':image_jobs[key]['id'],'image_name':image_jobs[key]['outputs'][0]})==fetched[key]
        assert enqueues('B')==bcount;passed('A offline with compatibility default B: explicit A read fails, completed A replay/results/artifacts stay local; no cross-backend write')
        stop('atlas')
        # Startup immutable binding and duplicate-ID refusals occur without native I/O.
        for label,change in [('url',{'url':urls['B']}),('account',{'account_scope':'different'}),('generation',{'backend_generation':'different'}),('token-ref',{'token_file':'other-file'})]:
            bad=json.loads(json.dumps(settings));bad['connections'][0].update(change);config.write_text(json.dumps(bad));n=len(calls)
            r=subprocess.run([sys.executable,'-I','-B','-m','atlas_engine.server','--config',str(config)],env=env,cwd=out,capture_output=True,text=True,timeout=30)
            assert r.returncode and 'binding changed' in r.stderr,(label,r.stderr);assert len(calls)==n
        bad=json.loads(json.dumps(settings));bad['connections'].append(bad['connections'][0]);config.write_text(json.dumps(bad))
        r=subprocess.run([sys.executable,'-I','-B','-m','atlas_engine.server','--config',str(config)],env=env,cwd=out,capture_output=True,text=True,timeout=30);assert r.returncode and 'duplicate connection ID' in r.stderr
        config.write_text(json.dumps(settings));passed('URL/account/backend/token-file-ref rebinding and duplicate IDs refuse startup before requests')
        stop('B');dbs={}
        for key in ('A','B'):
            with sqlite3.connect(roots[key]/'databases/invokeai.db') as db:
                assert db.execute('pragma integrity_check').fetchone()[0]=='ok'
                rows=db.execute('select item_id,parent_item_id,status from session_queue order by item_id').fetchall()
                models=db.execute('select count(*) from models').fetchone()[0]
                assert models==0 and all(r[2]=='completed' for r in rows),(key,rows,models)
                dbs[key]={'items':rows,'models':models};assert len(rows)==(5 if key=='A' else 4)
        save('native-database-counts.json',dbs)
        with sqlite3.connect(out/'state/registry.sqlite3') as db:
            observations=db.execute('select scope,native_id,job_id from scoped_observations order by scope,native_id').fetchall()
        assert len([r for r in observations if r[1]==1])==2;save('scoped-observations.json',observations)
        assert len(intents)==13,(len(intents),intents)
        passed('independent native DBs: A4 roots+1 child, B3 roots+1 child; zero models; all complete; colliding observations coexist; thirteen durable pre-forward scoped intents')
        save('result.json',{'ok':True,'checks':checks,'enqueue_posts':{k:enqueues(k) for k in ('A','B')},'native_counts':dbs})
    finally:
        for name in ('atlas','A','B'):stop(name)
        for proxy,thread in proxies:proxy.shutdown();proxy.server_close();thread.join(timeout=10)
        after=runtime();save('native-runtime-after.json',after);assert before==after
        save('proxy-calls.json',calls);save('intent-checks.json',intents);save('responses.json',responses)
        save('cleanup.json',{'owned_running':[name for name,p in all_processes if p.poll() is None],'proxy_running':any(t.is_alive() for _,t in proxies),'native_runtime_unchanged':before==after})
        for log in logs:log.close()

if __name__=='__main__':main()
