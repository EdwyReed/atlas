"""Unified domain owner. Adapters never create a backend or independent journal."""
import threading,time,uuid
from urllib.parse import quote,urlencode
from pydantic import ValidationError
from .contracts import SCHEMAS,Fault
from .repository import Repository,digest
from .creative import Vocabulary,CreativeSearch
from .graphs import _find,_model_ref,_build_graph

TERMINAL={'succeeded','partially_succeeded','failed','canceled'}
class Engine:
    def __init__(self,root,vocabulary,invoke,atlas,allow_submit=False,policy=None,native_events=False,projection=None,allow_admin=False,corpus_root=None,civitai_url='https://civitai.red',media_config=None):
        self.projection=projection
        self.repo=Repository(root);self.vocab=Vocabulary(vocabulary);self.creative=CreativeSearch(self.vocab)
        from .knowledge import Knowledge
        self.knowledge=Knowledge(self.repo)
        from .curated import Curated
        self.curated=Curated(self.knowledge)
        from .records import Records
        self.records=Records(self.repo)
        from .corpus import Corpus
        self.corpus=Corpus(corpus_root,self.repo) if corpus_root is not None else None
        self.invoke=invoke;self.atlas=atlas;self.allow_submit=allow_submit
        from .onboarding import Onboarding
        self.onboarding=Onboarding(self,civitai_url)
        from .native_admin import NativeAdmin
        self.native_admin=NativeAdmin(self,policy,allow_admin)
        from .workspace import Workspace
        self.workspace=Workspace(self)
        from .native_queue import NativeQueue
        self.native_queue=NativeQueue(self)
        from .acquisition import Acquisition
        self.acquisition=Acquisition(self)
        from .artifacts import Artifacts
        self.artifacts=Artifacts(self.repo,invoke)
        from .uploads import Uploads
        self.uploads=Uploads(self.repo,invoke)
        from .media import Media
        self.media=Media(self,media_config)
        self.accepted_modes=set((policy or {}).get('accepted_modes',['txt2img']))
        self.auto_fetch_artifacts=(policy or {}).get('auto_fetch_artifacts',False)
        # Historical native-user mapping is attribution only; no grants consulted.
        self.native_origins=(policy or {}).get('native_owners',{})
        from .text_embedding import TextEncoder
        self.text_encoder=TextEncoder(self.repo.root/'cache'/'fastembed')
        self.stop=threading.Event();self.wake=threading.Event();self.native_dirty=threading.Event();self.thread=None;self.observer_lock=threading.Lock()
        from .events import NativeEvents
        self.native_events=NativeEvents(invoke,self.repo,self.native_dirty,self.wake) if native_events else None
    def start(self):
        if any(w and w.is_alive() for w in (self.thread,getattr(self,'cpu_thread',None),getattr(self,'delivery_thread',None),getattr(self,'acquisition_thread',None))):raise Fault('ALREADY_RUNNING','engine worker already running')
        self.stop.clear();self.projection_cursor=''
        self.workspace.recover()
        self.acquisition.recover()
        self.media.recover()

        # Recovery walks indexed active states, never a bounded historical tail.
        for j in self.repo.iter_jobs('dream',{'running'}):
            self.repo.update(j['id'],'queued',expected={'running'},event='dream.restart_replay')
        for j in self.repo.iter_jobs('generation',{'submitting'}):
            self.repo.update(j['id'],'ack_unknown',expected={'submitting'},event='submission.restart_ambiguous')
        for j in self.repo.iter_jobs('native_admin',{'queued'}):
            self.repo.update(j['id'],'failed',{'error':{'code':'ADMIN_NOT_SENT','message':'process stopped before native I/O; no automatic administration replay'}},expected={'queued'},event='admin.restart_not_sent')
        for j in self.repo.iter_jobs('native_admin',{'submitting'}):
            self.repo.update(j['id'],'ack_unknown',expected={'submitting'},event='admin.restart_ambiguous_no_replay')
        self.cpu_thread=threading.Thread(target=self.creative_loop,name='atlas-creative',daemon=True)
        self.thread=threading.Thread(target=self.loop,name='atlas-observer',daemon=True)
        self.delivery_thread=threading.Thread(target=self.delivery_loop,name='atlas-delivery',daemon=True)
        self.acquisition_thread=threading.Thread(target=self.acquisition.loop,name='atlas-acquisition',daemon=True)
        self.thread.start();self.cpu_thread.start();self.delivery_thread.start();self.acquisition_thread.start()
        if self.native_events:self.native_events.start()
    def close(self):
        self.stop.set();self.wake.set()
        if self.native_events:self.native_events.close()
        for worker in (self.thread,getattr(self,'cpu_thread',None),getattr(self,'delivery_thread',None),getattr(self,'acquisition_thread',None)):
            if worker:worker.join(timeout=35)
            if worker and worker.is_alive():raise Fault('SHUTDOWN_TIMEOUT','worker did not stop; resources remain open')
        if self.projection:self.projection.close()
        if self.corpus:self.corpus.close()

    def creative_loop(self):
        while not self.stop.is_set():
            try:
                jobs=sorted(self.repo.page('dream',{'queued'},limit=16),key=lambda j:(j['created'],j['id']))
                for j in jobs:
                    if self.stop.is_set():break
                    _,claimed=self.repo.update(j['id'],'running',expected={'queued'},event='dream.started')
                    if not claimed:continue
                    try:
                        creative,context=self.creative_context(j['payload'],j['owner_principal'])
                        pinned=j.get('reference_context')
                        if pinned and pinned['snapshot_sha256']!=context['snapshot_sha256']:raise Fault('REFERENCE_CHANGED','reference heads changed since dream intent; use a new request_id')
                        r=creative.dream(j['payload'],lambda:self.stop.is_set() or self.repo.get(j['id'])['state']=='canceled')
                        self.curated.unchanged(context)
                        if context['heads']:r['reference_context']=context
                        self.repo.update(j['id'],'succeeded',{'result':r},expected={'running'},event='dream.finished')
                    except Fault as e:
                        if self.stop.is_set():self.repo.update(j['id'],'queued',expected={'running'},event='dream.shutdown_replay')
                        else:self.repo.update(j['id'],'canceled' if e.code=='CANCELED' else 'failed',{'error':{'code':e.code,'message':str(e)}},expected={'running'})
                    except Exception as e:self.repo.update(j['id'],'failed',{'error':{'code':'INTERNAL','message':type(e).__name__}},expected={'running'})
            except Exception as e:self.repo.health('creative_worker',{'ok':False,'error':type(e).__name__,'at':time.time()})
            self.stop.wait(.1)
    def loop(self):
        next_scan=0
        while not self.stop.is_set():
            try:
                if time.monotonic()>=next_scan or self.native_dirty.is_set():
                    self.native_dirty.clear();started=time.monotonic();self.reconcile();next_scan=time.monotonic()+10
                    self.repo.health('poll_latency',{'seconds':time.monotonic()-started,'at':time.time()})
                # Native submission/projection I/O runs in its own bounded worker.
            except Exception as e:self.repo.health('worker',{'ok':False,'error':type(e).__name__,'at':time.time()})
            self.wake.wait(.25);self.wake.clear()
    def delivery_loop(self):
        cursor='';batch_cursor=''
        while not self.stop.is_set():
            try:
                if self.allow_submit:
                    groups=self.repo.page('batch',{'queued','observing'},after=batch_cursor,limit=4)
                    batch_cursor=groups[-1]['id'] if groups else ''
                    for group in groups:
                        if self.stop.is_set():break
                        self.repo.materialize_batch(group['id'])
                        self.get_batch(group['id'])
                    rows=self.repo.page('generation',{'queued'},after=cursor,limit=4)
                    cursor=rows[-1]['id'] if rows else ''
                    for j in rows:
                        if self.stop.is_set():break
                        if not j.get('plan'):continue
                        try:self.execute_submission(j)
                        except Fault as e:self.repo.update(j['id'],'failed',{'error':{'code':e.code,'message':str(e)}},expected={'queued'},event='submission.policy_rejected')
                if self.auto_fetch_artifacts:self.fetch_pending_artifacts()
                if self.projection:self.project_pending()
            except Exception as e:self.repo.health('delivery_worker',{'ok':False,'error':type(e).__name__,'at':time.time()})
            self.stop.wait(.25)
    def project_pending(self):
        rows=self.repo.page(states=TERMINAL,after=getattr(self,'projection_cursor',''),limit=64)
        self.projection_cursor=rows[-1]['id'] if rows else ''
        for j in rows:
            # Administration is operational audit, never a creative recipe.
            if j['kind'] in ('native_admin','native_workspace','corpus','onboarding','acquisition','acquisition_batch','mask','censor'):continue
            feedback=self.repo.feedback(j['id']);reviews=self.repo.reviews(j['id'])
            h=digest({k:j.get(k) for k in ('state','payload','result','outputs','artifact_state','artifacts','native_ids')}|{'feedback':feedback,'reviews':reviews})
            if j.get('projection_hash')==h or j.get('index_retry_at',0)>time.time():continue
            try:
                receipt=self.projection.project(j,feedback,reviews)
                self.repo.update(j['id'],changes={'index_state':'indexed','projection_hash':h,'index_receipt':receipt,'index_retry_at':0},event='projection.verified')
            except Exception as e:self.repo.update(j['id'],changes={'index_state':'pending_retry','index_error':type(e).__name__,'index_retry_at':time.time()+10},event='projection.pending_retry')
    def fetch_pending_artifacts(self):
        rows=self.repo.page('generation',TERMINAL,after=getattr(self,'artifact_cursor',''),limit=16)
        for j in rows:
            self.artifact_cursor=j['id']
            if not j.get('outputs') or j.get('artifact_retry_at',0)>time.time():continue
            names={a['image_name'] for a in j.get('artifacts',[])}
            missing=[name for name in j['outputs'] if name not in names]
            if not missing:continue
            try:
                self.artifacts.fetch(j['id'],missing[0])
                self.repo.update(j['id'],changes={'artifact_retry_at':0,'artifact_error':None},event='artifact.auto_delivered')
            except Fault as e:
                self.repo.update(j['id'],changes={'artifact_retry_at':time.time()+10,'artifact_error':e.code},event='artifact.auto_retry')
            return # One bounded original-byte download per delivery tick.
        if not rows:self.artifact_cursor=''
    def call(self,op,args,principal='shared-user',source='local'):
        try:
            if op not in SCHEMAS:raise Fault('UNKNOWN_OPERATION','operation not available')
            p=SCHEMAS[op].model_validate(args).model_dump()
            if op=='legacy.request':
                from .legacy_compat import lookup
                return {'ok':True,'operation':op,'result':lookup(self.repo,p['namespace'],p['idempotency_key'],p['input'])}
            result=self.dispatch(op,p,principal,source)
            if op in ('capabilities','status'):
                result['readiness']={
                    'core':True,'canonical_corpus':bool(self.corpus),
                    'vocabulary':self.vocab.path is not None,
                    'invoke':{'configured':bool(getattr(self,'connection_id',None)),
                              'connection_id':getattr(self,'connection_id',None),
                              'verified':False},
                    'semantic':{'verified':False,'model_downloads':False}}
                if op=='capabilities':
                    result['curated_creative']['enabled']=self.vocab.path is not None
                    result['transport_acceptance']='partial; see SPEC.md, operation presence is not execution acceptance'
            return {'ok':True,'operation':op,'result':result}
        except ValidationError as e:return {'ok':False,'operation':op,'error':{'code':'VALIDATION','message':'arguments do not match schema','fields':[{'path':list(x['loc']),'type':x['type']} for x in e.errors()]}}
        except Fault as e:return {'ok':False,'operation':op,'error':{'code':e.code,'message':str(e)}}
        except Exception as e:
            import logging
            logging.getLogger('atlas').error('operation %s failed: %s',op,type(e).__name__)
            return {'ok':False,'operation':op,'error':{'code':'INTERNAL','message':'engine operation failed'}}
    def creative_context(self,p,principal):
        vocabulary,context=self.curated.prepare(p,self.vocab)
        return CreativeSearch(vocabulary),context
    def require_kind(self,jid,kind):
        j=self.repo.get(jid)
        if j['kind']!=kind:raise Fault('KIND_MISMATCH','job is not '+kind)
        return j
    def resolve(self,p):
        models=self.invoke.models();model=_find(models,p['model'],'main');ls=[]
        for l in p.get('loras',[]):
            m=_find(models,l['model'],'lora')
            if m.get('base') not in (model.get('base'),'any'):raise Fault('INCOMPATIBLE_MODEL','LoRA base does not match main model')
            ls.append({**m,'weight':l['weight']})
        return models,model,ls
    def plan(self,p):
        result=self._plan(p)
        if p.get('board_id'):
            from .image_graphs import apply_board
            for node in result['graph']['nodes'].values():
                if node.get('is_intermediate') is False:apply_board(node,p['board_id'])
        result['board_id']=p.get('board_id')
        result['submit_enabled']=self.allow_submit and p['mode'] in self.accepted_modes
        return result
    def validate_pixel_source(self,name):
        if not self.uploads.pixel(name):
            # Native final outputs also need actual-byte validation, not DTO guesses.
            from .uploads import validate_pixel_bytes
            from .artifacts import MAX_BYTES
            raw=bytearray()
            with self.invoke.http.stream('GET','/api/v1/images/i/'+quote(name,safe='')+'/full') as response:
                if response.status_code!=200:raise Fault('ARTIFACT_HTTP','pixel source GET rejected')
                for chunk in response.iter_bytes():
                    if len(raw)+len(chunk)>MAX_BYTES:raise Fault('IMAGE_LIMIT','pixel source byte budget exceeded')
                    raw.extend(chunk)
            validate_pixel_bytes(raw)
    def _plan(self,p):
        if p['mode']!='upscale' and p.get('upscale_engine','spandrel')!='spandrel':raise Fault('MODE_ARGUMENTS','upscale_engine only applies to upscale')
        if p['mode']=='upscale':
            if p['loras'] or p['prompt'] or p['negative_prompt']:raise Fault('MODE_ARGUMENTS','pixel upscale accepts no prompts or LoRAs; no hidden latent refinement')
            from .image_graphs import upscale_graph
            if not p['image_name']:raise Fault('VALIDATION','upscale requires image_name')
            self.validate_pixel_source(p['image_name'])
            im=self.invoke.call('GET','/api/v1/images/i/'+quote(p['image_name'],safe=''))
            if im['width']*im['height']*p['scale']**2>24*1024*1024:raise Fault('IMAGE_LIMIT','upscaled image exceeds budget')
            if p.get('upscale_engine','spandrel')=='esrgan':
                names={'RealESRGAN_x4plus.pth':4,'RealESRGAN_x4plus_anime_6B.pth':4,'ESRGAN_SRx4_DF2KOST_official-ff704c30.pth':4,'RealESRGAN_x2plus.pth':2}
                if p['model'] not in names:raise Fault('NOT_FOUND','unsupported built-in ESRGAN model')
                if p['scale']!=names[p['model']]:raise Fault('MODE_ARGUMENTS','ESRGAN scale is fixed by selected model; no silent resize')
                g=upscale_graph(image_name=p['image_name'],engine='esrgan',spandrel_model=None,esrgan_name=p['model'],scale=p['scale'],tile=512,board_id=None)
                return {'recipe_version':'atlas-esrgan-plan-v1','mode':'upscale','graph':g,'resolved_model':{'name':p['model'],'type':'builtin_esrgan'},'submit_enabled':False,'warnings':['Built-in node model availability and GPU output remain unaccepted; native runtime may download weights on first use.']}
            m=_find(self.invoke.models(),p['model'],'spandrel_image_to_image')
            g=upscale_graph(image_name=p['image_name'],engine='spandrel',spandrel_model=m,esrgan_name='',scale=p['scale'],tile=512,board_id=None)
            return {'recipe_version':'atlas-spandrel-plan-v1','mode':'upscale','graph':g,'resolved_model':_model_ref(m),'submit_enabled':False,'warnings':['Planning retained; image upload/pixel-family GPU acceptance required before enabling submission.']}
        if not p['prompt']:raise Fault('VALIDATION','text/inpaint generation requires prompt')
        models,m,ls=self.resolve(p)
        if p['mode']=='inpaint':
            from .image_graphs import sd1_graph,anima_graph
            if not p['image_name'] or not p['mask_name']:raise Fault('VALIDATION','inpaint requires existing image_name and mask_name')
            ims=[self.invoke.call('GET','/api/v1/images/i/'+quote(n,safe='')) for n in (p['image_name'],p['mask_name'])]
            if any((im['width'],im['height'])!=(p['width'],p['height']) for im in ims):raise Fault('RESIZE_REQUIRED','source/mask size must match; no silent resize')
            mask=self.uploads.mask(p['mask_name'])
            if mask['family']!=m['base']:raise Fault('MASK_FAMILY','normalized mask family differs from main model')
            kw=dict(width=p['width'],height=p['height'],steps=p['steps'],cfg=p['cfg'],seed=p['seed'],scheduler=p['scheduler'],loras=[(l,l['weight']) for l in ls],init_name=p['image_name'],mask_name=p['mask_name'],strength=p['strength'])
            if m['base']=='sd-1':g=sd1_graph(m,p['prompt'],p['negative_prompt'],clip_skip=2,**kw)
            elif m['base']=='anima':
                from .graphs import _component
                g=anima_graph(m,_component(models,{},'anima','vae_model','vae','Anima QwenImage VAE'),_component(models,{},'anima','qwen3_encoder_model','qwen3_encoder','Anima Qwen3 0.6B Text Encoder'),p['prompt'],p['negative_prompt'],**kw)
            else:raise Fault('UNSUPPORTED_MODEL_FAMILY','inpaint planner currently supports SD1/Anima')
            return {'recipe_version':'atlas-white-edit-inpaint-v2','mode':'inpaint','graph':g,'resolved_model':_model_ref(m),'submit_enabled':False,'warnings':['Owned public white-edit mask normalized through the versioned upload contract. GPU mask polarity and visual correctness remain unaccepted; accepted_modes is an explicit server gate.']}
        g=_build_graph(models,{},m,p['prompt'],p['negative_prompt'],p['seed'],p['width'],p['height'],p['steps'],p['cfg'],p['scheduler'],ls)
        return {'recipe_version':'atlas-txt2img-v1','mode':'txt2img','resolved_model':_model_ref(m),'resolved_loras':[{**_model_ref(l),'weight':l['weight']} for l in ls],'seed':p['seed'],'graph':g,'validation':'graph builder only; GPU family acceptance remains required','submit_enabled':self.allow_submit}
    def dispatch(self,op,p,principal,source):
        if op=='generation.submitGraph':return self.native_queue.submit_graph(p,principal,source)
        from .workspace import OPERATIONS as WORKSPACE_OPERATIONS
        if op in WORKSPACE_OPERATIONS:return self.workspace.call(op,p,principal,source)
        if op.startswith(('mask.','censor.','media.')):
            return self.media.call(op,p,principal,source)
        if op.startswith('models.acquire.'):
            return self.acquisition.call(op,p,principal,source)
        if op.startswith('models.onboard') or op=='corpus.files.read':
            return self.onboarding.call(op,p,principal,source)
        if op.startswith('corpus.'):
            if not self.corpus:raise Fault('CORPUS_DISABLED','explicit isolated corpus root required')
            return self.corpus.call(op,p,principal,source)
        if op.startswith(('loras.','generation.records.')):return self.records.call(op,p,principal,source)
        if op=='jobs.events':
            deadline=time.monotonic()+p['timeout_seconds']
            while True:
                with self.repo.connect() as c:
                    c.execute('BEGIN')
                    row=c.execute('select * from jobs where id=?',(p['job_id'],)).fetchone()
                    if not row:raise Fault('NOT_FOUND','job not found')
                    job=self.repo.decode(row)
                    rows=list(c.execute('select seq,kind,data,at from events where job_id=? and seq>? order by seq limit ?', (job['id'],p['after'],p['limit']+1)))
                more=len(rows)>p['limit'];rows=rows[:p['limit']]
                if rows or job['state'] in TERMINAL or time.monotonic()>=deadline or self.stop.is_set():
                    import json
                    return {'items':[{'seq':r['seq'],'kind':r['kind'],'data':json.loads(r['data']),'at':r['at']} for r in rows], 'next_after':rows[-1]['seq'] if rows else p['after'],'has_more':more,'state':job['state'],'terminal':job['state'] in TERMINAL,'subscription':'durable cursor long-poll; resume with next_after'}
                self.stop.wait(min(.1,max(0,deadline-time.monotonic())))
        from .native_admin import OPERATIONS
        if op in OPERATIONS:return self.native_admin.call(op,p,principal,source)
        if op=='admin.get':return self.require_kind(p['job_id'],'native_admin')
        if op=='projection.get':
            if not self.projection:raise Fault('PROJECTION_DISABLED','no projection configured')
            return self.projection.get(p['point_id'])
        if op=='projection.page':
            if not self.projection:raise Fault('PROJECTION_DISABLED','no projection configured')
            return self.projection.page(p['offset'],p['limit'])
        if op=='review.prepare':return self.artifacts.read(p['artifact_id'],0,1) | {'review_contract':'host-supplied structured decision; not automatic visual QC; read remaining bytes with artifact.read'}
        if op=='review.record':
            row,data=self.artifacts.get(p['artifact_id'])
            self.artifacts.read(p['artifact_id'],0,1)
            if data['sha256']!=p['sha256']:raise Fault('ARTIFACT_INTEGRITY','review hash does not match original')
            return self.repo.record_review(principal,source,row['job_id'],p)
        if op=='outbox.flush':
            if not self.projection:raise Fault('PROJECTION_DISABLED','no approved projection sink; nothing delivered')
            self.project_pending();return {'cursor':self.projection_cursor,'delivery':'bounded verified Qdrant projection only; no Hindsight Retain','page_size':64}
        if op=='batch.submit':return self.submit_batch(p,principal,source)
        if op=='batch.get':return self.get_batch(p['job_id'])
        if op=='upload.begin':return self.uploads.begin(p,principal)
        if op=='upload.chunk':return self.uploads.chunk(p,principal)
        if op=='upload.finalize':return self.uploads.finalize(p,principal)
        if op=='upload.get':return self.uploads.view(self.uploads.row(p['upload_id']))
        if op=='upload.abort':return self.uploads.abort(p['upload_id'],principal)
        if op=='upload.import':return self.uploads.import_native(p,principal,self.allow_submit)
        if op=='capabilities':return {'schema_version':1,'startup_mode':getattr(self,'startup_mode','embedded-explicit'),'operations':list(SCHEMAS),'scope':'shared-local','attribution_only':['principal','project','owner','source'],'generative_llm':False,'creative_algorithm':'opaque-directed-math-v1+MMR','generation_submit_enabled':self.allow_submit,'generation_modes':sorted(self.accepted_modes),'native_administration':{'writes_enabled':self.native_admin.enabled,'concurrency':'GET-before-write, not native CAS','replay':'uncertain remote effects retained as ack_unknown; never automatically retried'},'artifact_delivery':{'automatic':self.auto_fetch_artifacts,'retry_seconds':10},'projection':{'enabled':bool(self.projection),'mode':'legacy-compatibility-payload-only' if self.projection else 'disabled'},'canonical_corpus':{'enabled':bool(self.corpus),'backend':'qdrant-local','collections':['tags','loras','checkpoints','generations','tag_relations'],'implementation':'bounded raw/history/staged-import v1; legacy stores separate','coordination':'single local owner; serialized revision CAS, not distributed Qdrant CAS','relation_scope':'all_historical_observations','progress_visibility':'completed stages immediately readable; partial explicit'},'curated_creative':{'enabled':True,'max_references':16,'identity':'request-local literal identity lookup'},'media_modules':self.media.capabilities(),'image_embeddings':{'enabled':False},'not_enabled':['arbitrary_workflow_run','engine_visual_inference','service_control']}
        if op=='status':
            return {'observer':self.repo.health('observer'),'native_events':self.repo.health('native_events'),'worker_alive':bool(self.thread and self.thread.is_alive()),'jobs':self.repo.count()}
        if op=='enrich':
            creative,context=self.creative_context(p,principal)
            r=creative.enrich(p)
            self.curated.unchanged(context)
            r['reference_context']=context
            if p['model']:r['resolved_model']=_model_ref(self.resolve({'model':p['model']})[1])
            return r
        if op=='dream.submit':
            existing=self.repo.request(principal,p['request_id'],'dream',p)
            if existing:return existing
            creative,context=self.creative_context(p,principal)
            extra={'reference_context':context}
            if p['model']:extra['resolved_model']=_model_ref(self.resolve({'model':p['model']})[1])
            j,_=self.repo.reserve(principal,source,p['request_id'],'dream',p,extra);self.wake.set();return j
        if op in ('dream.get','generation.get'):return self.require_kind(p['job_id'],op.split('.')[0])
        if op=='generation.wait':
            deadline=time.monotonic()+p['timeout_seconds'];j=self.require_kind(p['job_id'],'generation')
            while j['state'] not in TERMINAL and time.monotonic()<deadline and not self.stop.is_set():
                self.stop.wait(min(.1,max(0,deadline-time.monotonic())));j=self.repo.get(j['id'])
            return {'job':j,'timed_out':j['state'] not in TERMINAL and not self.stop.is_set()}
        if op=='generation.retry':
            if not self.allow_submit:raise Fault('SUBMISSION_DISABLED','native writes disabled')
            old=self.require_kind(p['job_id'],'generation')
            self.native_queue.check(old)
            if old.get('plan',{}).get('mode')=='native_graph':raise Fault('RETRY_UNSAFE','explicit new graph intent requires a new submitGraph request_id')
            if old['state'] not in ('failed','canceled'):raise Fault('RETRY_UNSAFE','only authoritative failed/canceled jobs may be retried; unknown ACK never replays')
            cp={**old['payload'],'request_id':p['request_id']}
            if cp['mode'] not in self.accepted_modes:raise Fault('MODE_NOT_ACCEPTED','mode not accepted')
            existing=self.repo.request(principal,p['request_id'],'generation',cp)
            if existing:
                if existing.get('retry_of')!=old['id']:raise Fault('IDEMPOTENCY_CONFLICT','retry request belongs to another lineage')
                return existing
            j,_=self.repo.reserve(principal,source,p['request_id'],'generation',cp,{'plan':self.plan(cp),'native_ids':[],'native_scope':self.native_queue.scope(),'retry_of':old['id']})
            if j.get('retry_of')!=old['id']:raise Fault('IDEMPOTENCY_CONFLICT','retry lineage differs')
            return self.execute_submission(j)
        if op=='dream.cancel':
            self.require_kind(p['job_id'],'dream');return self.repo.update(p['job_id'],'canceled',expected={'queued','running'},event='dream.cancel_requested')[0]
        if op=='generation.plan':return self.plan(p)
        if op=='generation.submit':
            if not self.allow_submit:raise Fault('SUBMISSION_DISABLED','candidate is read-only toward InvokeAI; separate GPU acceptance required')
            # Compute plan before durable intent; no remote write occurs during planning.
            # Existing request returns without rerunning catalog resolution (even after model removal).
            existing=self.repo.request(principal,p['request_id'],'generation',p)
            if existing:return existing
            if p['mode'] not in self.accepted_modes:raise Fault('MODE_NOT_ACCEPTED','mode not accepted in server policy; separate family/mask/GPU acceptance required')
            plan=self.plan(p);j,created=self.repo.reserve(principal,source,p['request_id'],'generation',p,{'plan':plan,'native_ids':[],'native_scope':self.native_queue.scope()})
            if not created:return j
            return self.execute_submission(j)
        if op=='generation.cancel':
            j=self.require_kind(p['job_id'],'generation')
            self.native_queue.check(j)
            if not self.allow_submit:raise Fault('SUBMISSION_DISABLED','native cancel disabled in read-only candidate')
            if j['state'] in TERMINAL:return j
            if j['state']=='queued':
                canceled,changed=self.repo.update(j['id'],'canceled',expected={'queued'},event='generation.cancel_before_submit')
                if changed:return canceled
                j=canceled
            if not j.get('native_ids'):raise Fault('ACK_UNKNOWN','no native ID; reconcile before targeted cancel')
            self.repo.update(j['id'],changes={'cancel_requested':True},event='generation.cancel_intent')
            for nid in j['native_ids']:
                item=self.invoke.item(nid)
                if item.get('item_id')!=nid or (j.get('native_batch_id') and item.get('batch_id')!=j['native_batch_id']):raise Fault('NATIVE_IDENTITY_MISMATCH','native item no longer matches durable receipt')
                self.invoke.cancel(nid)
            self.reconcile();return self.repo.get(j['id'])
        if op=='artifact.list':return self.artifacts.list(p['job_id'])
        if op=='artifact.fetch':return self.artifacts.fetch(p['job_id'],p['image_name'])
        if op=='artifact.read':return self.artifacts.read(p['artifact_id'],p['offset'],p['size'])
        if op in ('jobs.list','jobs.page'):
            rows=self.repo.page(after=p['after'],limit=p['limit']+1)
            more=len(rows)>p['limit'];rows=rows[:p['limit']]
            if op=='jobs.list':return rows
            return {'items':rows,'has_more':more,'next_after':rows[-1]['id'] if more else None}
        if op=='knowledge.search':return self.knowledge.search(p['query'],p['kind'],p['after'],p['limit'])
        if op=='knowledge.get':return self.knowledge.get(p['entity_id'])
        if op=='feedback.record':return self.repo.record_feedback(principal,source,p)
        if op=='feedback.list':return self.repo.feedback(p['job_id'])
        if op=='catalog.list':return [m for m in self.invoke.models() if p['query'].lower() in m.get('name','').lower() and (not p['type'] or m.get('type')==p['type'])]
        if op=='catalog.resolve':
            _,m,ls=self.resolve(p);return {'model':_model_ref(m),'loras':[{**_model_ref(l),'weight':l['weight']} for l in ls]}
        if op=='catalog.get':return self.invoke.call('GET','/api/v2/models/i/'+quote(p['key'],safe=''))
        if op=='queue.status':return self.invoke.call('GET',self.invoke.qp+'/status')
        if op=='observer.reconcile':return self.reconcile()
        if op=='search':
            resources=[];tags=[];semantic=[]
            resource_source={'status':'not_requested'}
            if p['mode'] in ('literal','hybrid'):
                # Only resource availability may degrade literal search. Authentication,
                # malformed responses and upstream errors must still propagate.
                try:
                    resource_rows=(self.atlas.search_resources(p['query'],p['limit'])
                                   if hasattr(self.atlas,'search_resources') else self.atlas.resources())
                except Fault as e:
                    import httpx
                    unavailable=(e.code=='UPSTREAM_UNAVAILABLE' and isinstance(e.__cause__,httpx.TransportError)) or e.code in ('REMOTE_HTTP_502','REMOTE_HTTP_503','REMOTE_HTTP_504')
                    if not unavailable:raise
                    resource_rows=[]
                    resource_source={'status':'unavailable','error':{'code':e.code},'complete':False}
                else:resource_source={'status':'available','complete':True}
                resources=[x for x in resource_rows if p['query'].lower() in str(x['payload']).lower()][:p['limit']]
                tags=self.vocab.search(p['query'],p['limit'])
            if p['mode'] in ('semantic','hybrid'):
                if getattr(self.atlas,'semantic_enabled',True) is not True:raise Fault('EMBEDDING_SPACE_UNVERIFIED','legacy vectors preserved in quarantine; semantic queries disabled before encoding')
                vector=self.text_encoder.encode(p['query'])
                semantic=self.atlas.query(vector,p['limit'])
            # This is application-level literal+dense union, not sparse/dense RRF.
            knowledge=self.knowledge.search(p['query'],limit=p['limit']) if p['mode'] in ('literal','hybrid') else {'items':[],'next_after':None}
            return {'tags':tags,'resources':resources,'knowledge':knowledge,'semantic':semantic,'search_mode':p['mode'],'embedding_space':self.text_encoder.model_name if semantic else None,'fusion':'separate evidence lists; no score comparability claim','partial':resource_source['status']=='unavailable','sources':{'resources':resource_source}}
        if op=='evidence':
            legacy=self.atlas.evidence(p['kind'],p['key']) if p['kind'] in ('tag','resource') else []
            if p['kind']=='tag':return legacy
            try:item=self.knowledge.get(p['key'])
            except Fault as e:
                if e.code=='NOT_FOUND':return legacy
                raise
            if item['record']['kind']!=p['kind']:return legacy
            return legacy+[{'id':p['key'],'payload':item['record']['body'],'domain_record':item['record'],'sha256':item['sha256'],'source':'session-delta'}]
        paths={'boards.list':'/api/v1/boards/?all=true','workflows.list':'/api/v1/workflows/','workflows.get':'/api/v1/workflows/i/'+quote(p.get('key',''),safe=''),'image.metadata':'/api/v1/images/i/'+quote(p.get('key',''),safe='')+'/metadata','images.list':'/api/v1/images/?'+urlencode(p)}
        if op in paths:return self.invoke.call('GET',paths[op])
        raise Fault('UNKNOWN_OPERATION','unknown operation')
    def submit_batch(self,p,principal,source):
        if not self.allow_submit:raise Fault('SUBMISSION_DISABLED','native writes disabled')
        if p['mode'] not in self.accepted_modes:raise Fault('MODE_NOT_ACCEPTED','batch mode not accepted')
        if p['seed']+p['runs']-1>2147483647:raise Fault('VALIDATION','distinct seed range would overflow')
        group=self.repo.request(principal,p['request_id'],'batch',p)
        if not group:
            children=[]
            for i in range(p['runs']):
                cp={k:v for k,v in p.items() if k!='runs'};cp['seed']+=i
                cp['request_id']=str(uuid.uuid5(uuid.NAMESPACE_URL,'atlas-batch:'+principal+':'+p['request_id']+':'+str(i)))
                children.append({'payload':cp})
            # Check every deterministic child key before catalog/planning I/O.
            # A late child collision must not resolve earlier children's models.
            for child in children:
                self.repo.request(principal,child['payload']['request_id'],'generation',child['payload'])
            for child in children:child['plan']=self.plan(child['payload'])
            group,_=self.repo.reserve(principal,source,p['request_id'],'batch',p,{'prepared_children':children})
        jobs=self.repo.materialize_batch(group['id'])
        for j in jobs:self.execute_submission(j)
        self.wake.set()
        return self.get_batch(group['id'])
    def get_batch(self,jid):
        group=self.require_kind(jid,'batch');jobs=[self.repo.get(cid) for cid in group.get('children',[])]
        states=[j['state'] for j in jobs]
        state='observing'
        if len(states)==group['payload']['runs'] and all(s in TERMINAL for s in states):
            state='succeeded' if all(s=='succeeded' for s in states) else 'canceled' if all(s=='canceled' for s in states) else 'partially_succeeded' if any(s in ('succeeded','partially_succeeded') for s in states) else 'failed'
        if group['state']!=state:
            group=self.repo.update(jid,state,expected={'queued','observing'},event='batch.aggregate')[0]
        return {**group,'items':[{'job_id':j['id'],'seed':j['payload']['seed'],'native_ids':j.get('native_ids',[]),'outputs':j.get('outputs',[]),'state':j['state'],'board_id':j['payload'].get('board_id')} for j in jobs]}
    def execute_submission(self,j):
        if not self.allow_submit:raise Fault('SUBMISSION_DISABLED','native writes disabled')
        if j['state']=='queued':
            if j['payload'].get('mode')=='upscale':self.validate_pixel_source(j['payload']['image_name'])
            if j['plan'].get('mode',j['payload'].get('mode','txt2img')) not in self.accepted_modes:raise Fault('MODE_NOT_ACCEPTED','server policy no longer accepts this mode')
        self.native_queue.check(j)
        prepared=j.get('prepared_submission') or self.native_queue.prepare(j)
        current,claimed=self.repo.update(j['id'],'submitting',{'render_state':'ack_unknown','prepared_submission':prepared,'native_scope':j.get('native_scope',self.native_queue.scope())},expected={'queued'},event='submission.fenced')
        if not claimed:return current
        try:
            ack=self.invoke.submit_prepared(prepared)
            return self.native_queue.record(current,ack)
        except Exception:
            return self.repo.update(j['id'],'ack_unknown',{'error':{'code':'ACK_UNKNOWN','message':'native submission acknowledgment uncertain; will reconcile, never auto-resubmit'}},expected={'submitting'},event='submission.ambiguous')[0]
    def reconcile(self):
        if getattr(self,'startup_mode',None)=='standalone' and self.connection_id is None:
            return self.repo.health('observer',{'ok':False,'error':'INVOKE_UNCONFIGURED','configured':False})
        with self.observer_lock:
            try:
                self.native_queue.recover_receipts()
                self.native_queue.maintain_receipts()
                items=self.invoke.inventory()
                if not isinstance(items,list):raise Fault('QUEUE_SCHEMA','list_all must return array')
                seen=set()
                receipt_jobs={}
                for known in self.repo.iter_jobs('generation'):
                    if known.get('native_scope')==self.native_queue.scope() and known.get('native_receipt'):
                        for native_id in known.get('native_ids',[]):receipt_jobs[native_id]=known
                for item in items:
                    nid=item['item_id'];seen.add(nid);jid=self.repo.observed_job(nid)
                    known=receipt_jobs.get(nid)
                    if known:
                        if item.get('batch_id')!=known.get('native_batch_id'):raise Fault('NATIVE_IDENTITY_MISMATCH','observed item differs from receipt')
                        jid=known['id']
                    if not jid:
                        # Link engine ACK only with both persisted batch identity and graph identity.
                        origin=item.get('origin') or '';possible=origin.removeprefix('atlas-engine:') if origin.startswith('atlas-engine:') else ''
                        try:candidate=self.repo.get(possible) if possible else None
                        except Fault:candidate=None
                        if candidate and candidate.get('prepared_submission'):
                            # Origin is not a receipt. Wait for exact-ID recovery;
                            # do not invent an external job for our uncertain intent.
                            continue
                        if candidate and not candidate.get('prepared_submission') and item.get('batch_id')==possible and item.get('session',{}).get('graph',{}).get('id')==candidate.get('plan',{}).get('graph',{}).get('id'):jid=possible

                        if not jid:
                            origin=self.native_origins.get(item.get('user_id'),{})
                            owner=origin.get('principal','native-observer');project=origin.get('project',item.get('project'))
                            j,_=self.repo.reserve(owner,'invokeai-external',f'native:{self.native_queue.scope()}:{self.invoke.base}:{nid}','generation',{'native_item_id':nid,'project':project},{'native_scope':self.native_queue.scope(),'native_ids':[nid],'provenance':'observed_external','author':'unknown','upstream_user_id':item.get('user_id')})
                            jid=j['id']
                    self.native_queue.check(self.repo.get(jid))
                    self.repo.native(item,jid)
                    j=self.repo.get(jid);ids=sorted(set(j.get('native_ids',[])+[nid]));previous=j.get('native_items',{});previous[str(nid)]=self.compact(item)
                    states=[v['status'] for v in previous.values()]
                    if len(previous)<len(ids) or not states or any(x not in ('completed','failed','canceled') for x in states):state='observing'
                    elif all(x=='completed' for x in states):state='succeeded'
                    elif all(x=='canceled' for x in states):state='canceled'
                    elif 'completed' in states:state='partially_succeeded'
                    else:state='failed'
                    changes={'native_ids':ids,'native_items':previous,'render_state':state,'outputs':self.outputs(previous),'provenance':j.get('provenance','engine_intent'),'history_missing':False}
                    if any(j.get(k)!=v for k,v in changes.items()) or j['state']!=state:self.repo.update(jid,state,changes,event='native.observed')
                # Missing history does NOT mean canceled/failed and never triggers resubmission.
                history=self.repo.page('generation',after=getattr(self,'history_cursor',''),limit=128)
                if not history and getattr(self,'history_cursor',''):history=self.repo.page('generation',limit=128)
                self.history_cursor=history[-1]['id'] if history else ''
                for j in history:
                    if j.get('native_ids') and any(n not in seen for n in j['native_ids']) and not j.get('history_missing'):self.repo.update(j['id'],changes={'history_missing':True},event='native.history_missing')
                return self.repo.health('observer',{'ok':True,'at':time.time(),'seen_items':len(items),'mode':'item_ids+batched-items_by_ids','coverage':'retained visible default queue only; jobs submitted and deleted between polls/offline cannot be recovered'})
            except Exception as e:
                return self.repo.health('observer',{'ok':False,'at':time.time(),'error':getattr(e,'code',type(e).__name__),'mode':'item_ids+batched-items_by_ids'})
    @staticmethod
    def compact(item):
        d={k:item.get(k) for k in ('item_id','status','status_sequence','batch_id','origin','session_id','created_at','updated_at','error_type')}
        session=item.get('session') or {};graph=session.get('graph') or {};nodes=graph.get('nodes') or {}
        results=session.get('results') or {};out=[]
        for node_id,r in results.items():
            source_id=(session.get('prepared_source_mapping') or {}).get(node_id,node_id)
            n=nodes.get(source_id)
            # Only final, non-intermediate nodes. Never index inpaint input/mask or intermediate images as output.
            if n is not None and n.get('is_intermediate',True)==False and isinstance(r,dict):
                if isinstance(r.get('image'),dict) and isinstance(r['image'].get('image_name'),str):out.append(r['image']['image_name'])
        d['outputs']=out;d['graph_id']=graph.get('id');return d
    @staticmethod
    def outputs(items):return sorted({x for i in items.values() for x in i['outputs']})
