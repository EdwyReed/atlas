"""One optional Engine media owner. Existing repository, uploads and artifacts.
SAM's accepted worker is subordinate inference/cache only, never a second product API.
All file paths originate from operator config or Engine-owned immutable snapshots.
"""
import fcntl,hashlib,importlib.util,io,json,os,signal,socket,subprocess,sys,threading,time,uuid
from pathlib import Path
from PIL import Image
from .contracts import Fault
from .repository import dump
from .artifacts import MAX_BYTES,MAX_PIXELS

class Media:
    def __init__(self,engine,config=None):
        self.engine=engine;self.repo=engine.repo;self.config=config or {}
        self.root=self.repo.root/'media';self.lock=threading.Lock();self.mask_client=None
        self.mask_process=None;self.mask_owner=None
        if self.config:self.root.mkdir(exist_ok=True,mode=0o700)
    def capabilities(self):
        return {k:{'enabled':bool(self.config.get(k)), 'optional':True,'automatic':False,'admission':'durable_async_one_slot' if k=='censor' else 'synchronous','cancellation':'unsupported','restart':'interrupted_no_replay'} for k in ('mask','censor')}
    def loop(self):
        while not self.engine.stop.is_set():
            try:
                with self.lock:
                    for job in self.repo.page('censor',{'queued'},limit=1):
                        if self.engine.stop.is_set():break
                        with (self.root/'dispatch.lock').open('a') as lock:
                            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                            self.execute(job,Path(job['media_folder']))
            except Exception as exc:
                self.repo.health('media_worker',{'ok':False,'error':type(exc).__name__})
            self.engine.stop.wait(.1)
        self.recover()
    @staticmethod
    def durable(path,raw):
        with path.open('xb') as f:
            f.write(raw);f.flush();os.fsync(f.fileno())
        fd=os.open(path.parent,os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    def recover(self):
        for kind in ('censor','mask'):
            for job in self.repo.iter_jobs(kind,{'queued','running'}):
                self.repo.update(job['id'],'failed',{'error':{'code':'INTERRUPTED','message':'Engine stopped during media request; no automatic replay; partial artifacts retained'}},event='media.interrupted')
    def enabled(self,kind):
        config=self.config.get(kind)
        if not config:raise Fault('MODULE_DISABLED',kind+' is optional and not enabled')
        return config
    def finalize(self,p,principal):
        row=self.engine.uploads.row(p['upload_id'])
        if row['state']!='receiving':return self.engine.uploads.view(row)
        raw=row['bytes']
        if len(raw)!=row['size'] or hashlib.sha256(raw).hexdigest()!=row['sha256']:raise Fault('UPLOAD_INTEGRITY','size/hash mismatch')
        self.validate(raw)
        with self.repo.connect() as c:c.execute("update uploads set state='ready',data=? where id=? and state='receiving'",(dump({'media':True}),row['id']))
        return self.engine.uploads.view(self.engine.uploads.row(row['id']))
    def validate(self,raw):
        if not raw or len(raw)>64*1024*1024:raise Fault('RESOURCE_LIMIT','image byte budget exceeded')
        try:
            with Image.open(io.BytesIO(raw)) as im:
                if im.format not in ('PNG','JPEG','WEBP') or im.width*im.height>MAX_PIXELS or getattr(im,'n_frames',1)!=1:raise Fault('RESOURCE_LIMIT','single PNG/JPEG/WebP within pixel budget required')
                im.load()
        except Fault:raise
        except Exception:raise Fault('IMAGE_INVALID','image cannot be decoded')
    def upload(self,uid,principal):
        row=self.engine.uploads.row(uid)
        if row['state'] not in ('ready','imported') or row['role']!='image':raise Fault('UPLOAD_STATE','finalized image upload required')
        raw=row['bytes'];self.validate(raw)
        if hashlib.sha256(raw).hexdigest()!=row['sha256']:raise Fault('UPLOAD_INTEGRITY','upload changed')
        return raw,row['sha256']
    def mask_call(self,args,principal):
        config=self.enabled('mask');root=Path(config['root']).resolve()
        if not (root/'backend-paths.json').is_file():
            raise Fault('UNAVAILABLE','mask requires explicit deployment-owned backend-paths.json in a private package root')
        if self.mask_client is None:
            runtime=root/'runtime';runtime.mkdir(mode=0o700,exist_ok=True)
            owner=(runtime/'engine.lock').open('a')
            try:fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                owner.close();raise Fault('CAPACITY','mask root already owned; configure a new private package root')
            # Refuse even a stale socket: never attach to or remove another worker.
            if (runtime/'worker.sock').exists():
                owner.close();raise Fault('UNAVAILABLE','mask socket already exists; configure a new private package root')
            self.mask_owner=owner
            name='_atlas_mask_subordinate_'+hashlib.sha256(str(root).encode()).hexdigest()[:12]
            spec=importlib.util.spec_from_file_location(name,root/'atlas_mask/__init__.py',submodule_search_locations=[str(root/'atlas_mask')])
            module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
            self.mask_client=importlib.import_module(name+'.client')
            # The subordinate client must never spawn an unowned replacement.
            def connect(deadline):
                s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
                try:
                    s.settimeout(max(.001,deadline-time.monotonic()));s.connect(str(self.mask_client.SOCKET));return s
                except BaseException:s.close();raise
            self.mask_client.connect=connect
        if self.mask_process is None or self.mask_process.poll() is not None:
            if self.mask_process is not None:self.mask_process.wait()
            env={k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME','VIRTUAL_ENV')}
            env.update(CUDA_VISIBLE_DEVICES='',HF_HUB_OFFLINE='1',PYTHONDONTWRITEBYTECODE='1')
            with (root/'runtime'/'worker.log').open('ab') as log:
                self.mask_process=subprocess.Popen([str(self.mask_client.PYTHON),'-I','-B',str(Path(__file__).with_name('mask_worker.py')),str(os.getpid()),str(root)],cwd=root,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,close_fds=True,start_new_session=True)
            deadline=time.monotonic()+15
            while not self.mask_client.SOCKET.exists():
                if self.engine.stop.is_set():raise Fault('STOPPING','Engine is stopping')
                if self.mask_process.poll() is not None:raise Fault('UNAVAILABLE','owned mask worker exited; inspect private runtime log')
                if time.monotonic()>=deadline:
                    self.close();raise Fault('TIMEOUT','owned mask worker startup deadline')
                self.engine.stop.wait(.02)
        # Shared trusted Engine namespace, not a per-source authorization gate.
        result=self.mask_client.call(args,hashlib.sha256(b'atlas-engine:shared-media').hexdigest())
        if not result.get('ok'):raise Fault(result['error']['code'],result['error']['message'])
        return result
    def close(self):
        process=self.mask_process
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=2)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=2)
            else:process.wait()
            # Only the socket of our reaped worker; never an existing foreign socket.
            self.mask_client.SOCKET.unlink(missing_ok=True)
            self.mask_process=None
        if self.mask_owner is not None:self.mask_owner.close();self.mask_owner=None
        self.mask_client=None
    def censor_call(self,spec,folder):
        config=self.enabled('censor')
        if not all(Path(config[k]).is_file() for k in ('python','model')) or not (Path(config['upstream'])/'mosaic.py').is_file():raise Fault('UNAVAILABLE','configured shared NTD11 installation incomplete')
        env={k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME','VIRTUAL_ENV')}
        # No GPU/network model fallback, even with a managed parent environment.
        env.update(CUDA_VISIBLE_DEVICES='',HF_HUB_OFFLINE='1',YOLO_OFFLINE='true',YOLO_AUTOINSTALL='false',YOLO_CONFIG_DIR=str(self.root/'yolo-config'))
        spec={**spec,'upstream':config['upstream'],'model':config['model'],'result_file':str(folder/'result.json'),'max_rss_bytes':config.get('max_rss_bytes',3*1024**3),'parent_pid':os.getpid()}
        (folder/'request.json').write_text(dump(spec))
        try:
            with (folder/'worker.log').open('wb') as log:
                run=subprocess.Popen([config['python'],'-I','-B',str(Path(__file__).with_name('censor_worker.py')),str(folder/'request.json')],env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,close_fds=True,start_new_session=True)
                deadline=time.monotonic()+config.get('timeout_seconds',1800)
                try:
                    while run.poll() is None:
                        if self.engine.stop.is_set():raise Fault('INTERRUPTED','Engine stopped; no automatic replay')
                        if time.monotonic()>=deadline:raise Fault('TIMEOUT','NTD11 CPU deadline exceeded; no automatic replay')
                        self.engine.stop.wait(.05)
                finally:
                    if run.poll() is None:
                        os.killpg(run.pid,signal.SIGTERM)
                        try:run.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            os.killpg(run.pid,signal.SIGKILL);run.wait(timeout=2)
            if run.returncode:raise Fault('RESOURCE_LIMIT' if run.returncode==72 else 'INFERENCE_FAILED','NTD11 CPU worker exited; inspect operator-owned worker.log')
        except subprocess.TimeoutExpired:raise Fault('TIMEOUT','NTD11 CPU deadline exceeded; no automatic replay')
        return json.loads((folder/'result.json').read_text())
    def artifact(self,jid,name,path,mime='application/octet-stream'):
        with path.open('rb') as f:
            raw=f.read();os.fsync(f.fileno())
        fd=os.open(path.parent,os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
        if len(raw)>64*1024*1024:raise Fault('RESOURCE_LIMIT','output byte budget exceeded')
        aid=str(uuid.uuid5(uuid.NAMESPACE_URL,'atlas-media:'+jid+':'+name))
        data={'artifact_id':aid,'job_id':jid,'image_name':name,'sha256':hashlib.sha256(raw).hexdigest(),'size':len(raw),'mime':mime,'embedding_state':'disabled'}
        with self.repo.connect() as c:c.execute('insert into artifacts values(?,?,?,?,?)',(aid,jid,name,str(path.resolve()),dump(data)))
        return data
    def result_job(self,jid,principal,kind):
        j=self.repo.get(jid)
        if j['kind']!=kind:raise Fault('NOT_FOUND','media result not available')
        return j
    def call(self,op,p,principal,source):
        if op.startswith('media.upload.'):
            if not self.config:raise Fault('MODULE_DISABLED','optional media modules disabled')
            if op=='media.upload.begin':return self.engine.uploads.begin(p,principal)
            if op=='media.upload.chunk':
                return self.engine.uploads.chunk(p,principal)
            return self.finalize(p,principal)
        kind,action=op.split('.');self.enabled(kind)
        if action=='get':return self.result_job(p['job_id'],principal,kind)
        if action in ('process','segment'):
            old=self.repo.request(principal,p['request_id'],kind,p)
            if old:return old
        # One bounded media dispatch at a time; no competing queue or unbounded threads.
        if not self.lock.acquire(blocking=False):raise Fault('CAPACITY','media CPU request in flight; retry explicitly')
        try:
            if self.engine.stop.is_set():raise Fault('STOPPING','Engine is stopping')
            if action in ('process','segment'):
                old=self.repo.request(principal,p['request_id'],kind,p)
                if old:return old
            if any(self.repo.page(k,{'queued','running'},limit=1) for k in ('mask','censor')):
                raise Fault('CAPACITY','media CPU slot occupied; retry explicitly')
            with (self.root/'dispatch.lock').open('a') as lock:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise Fault('CAPACITY','media CPU owner busy')
                return self.dispatch(kind,action,p,principal,source)
        finally:self.lock.release()
    def dispatch(self,kind,action,p,principal,source):
        if action=='get':return self.result_job(p['job_id'],principal,kind)
        if kind=='mask' and action in ('health','release','reset'):
            return self.mask_call({'action':action,**p},principal)
        if kind=='mask' and action=='setup':
            raw,sha=self.upload(p['upload_id'],principal)
            used=sum(f.stat().st_size for f in self.root.rglob('*') if f.is_file())
            if used+len(raw)+520*1024*1024>self.config.get('max_storage_bytes',2*1024**3):raise Fault('RESOURCE_LIMIT','media input storage admission budget exceeded')
            directory=self.root/'inputs';directory.mkdir(exist_ok=True)
            path=directory/sha
            if not path.exists():
                with path.open('xb') as f:f.write(raw)
            if hashlib.sha256(path.read_bytes()).hexdigest()!=sha:raise Fault('ARTIFACT_INTEGRITY','input snapshot changed')
            result=self.mask_call({'action':'setup','image_path':str(path)},principal)
            result.pop('image_path',None)
            return result
        if kind=='censor' and action=='health':
            directory=self.root/('health-'+uuid.uuid4().hex);directory.mkdir()
            try:return self.censor_call({'action':'health'},directory)
            finally:
                import shutil
                shutil.rmtree(directory)
        old=self.repo.request(principal,p['request_id'],kind,p)
        if old:return old
        used=sum(f.stat().st_size for f in self.root.rglob('*') if f.is_file())
        if used+520*1024*1024>self.config.get('max_storage_bytes',2*1024**3):raise Fault('RESOURCE_LIMIT','media output storage admission budget exceeded; operator cleanup required')
        # Validate every input before intent; processing uses this same byte snapshot.
        images=[]
        if kind=='censor':
            names=set();total=0
            for image in p['images']:
                name=image['name']
                if name in ('.','..') or name.casefold() in names or any(ord(c)<32 for c in name):raise Fault('VALIDATION','unsafe or duplicate output name')
                names.add(name.casefold());raw,sha=self.upload(image['upload_id'],principal);total+=len(raw)
                if len(raw)>MAX_BYTES:raise Fault('RESOURCE_LIMIT','censor image exceeds 32 MiB')
                if total>256*1024*1024:raise Fault('RESOURCE_LIMIT','batch exceeds 256 MiB')
                images.append((name,raw,sha))
            images.sort(key=lambda r:r[0])
            if p['preview']:images=images[:p['preview']]
        prior=None
        if kind=='mask' and p['prior']:
            ref=p['prior'];idx=ref['candidate']
            if ref['job_id']:
                if ref['manifest_upload_id'] or ref['logits_upload_id']:raise Fault('VALIDATION','choose job prior OR legacy upload pair')
                previous=self.result_job(ref['job_id'],principal,'mask')
                if previous['state']!='succeeded':raise Fault('VALIDATION','prior mask result not successful')
                from .state_backup import storage_path
                prior={'manifest_path':str(storage_path(self.repo.root,previous['media_manifest_path'])),'candidate':idx}
            else:
                def blob(uid,limit):
                    if not uid:raise Fault('VALIDATION','legacy prior requires both uploads')
                    row=self.engine.uploads.row(uid);raw=row['bytes']
                    if row['state'] not in ('receiving','ready') or len(raw)!=row['size'] or len(raw)>limit or hashlib.sha256(raw).hexdigest()!=row['sha256']:raise Fault('UPLOAD_INTEGRITY','legacy prior byte provenance mismatch')
                    return raw
                manifest_raw=blob(ref['manifest_upload_id'],131072);logits=blob(ref['logits_upload_id'],300000)
                with self.repo.connect() as c:
                    for uid in (ref['manifest_upload_id'],ref['logits_upload_id']):c.execute("update uploads set state='ready',data=? where id=? and state='receiving'",(dump({'media_prior':True}),uid))
                try:
                    imported=json.loads(manifest_raw);candidate=imported['candidates'][idx]
                    if candidate['index']!=idx or candidate['logits_file']!=f'candidate-{idx}.npy' or candidate['logits_sha256']!=hashlib.sha256(logits).hexdigest():raise ValueError()
                except (ValueError,KeyError,IndexError,TypeError):raise Fault('PROVENANCE','invalid legacy prior manifest/logits pair')
                directory=self.root/('prior-'+uuid.uuid4().hex);directory.mkdir(mode=0o700)
                (directory/'manifest.json').write_bytes(manifest_raw)
                (directory/f'candidate-{idx}.npy').write_bytes(logits)
                # Accepted SAM core additionally checks model/image/dimensions/float32 shape.
                prior={'manifest_path':str(directory/'manifest.json'),'candidate':idx}
        if kind=='censor':
            # Seal bytes and directory entries before publishing the queued intent.
            folder=self.root/uuid.uuid4().hex;folder.mkdir(mode=0o700)
            src=folder/'input';src.mkdir();entries=[]
            for name,raw,sha in images:
                path=src/name;self.durable(path,raw)
                entries.append({'name':name,'path':str(path),'sha256':sha})
            self.durable(folder/'inputs.json',(dump(entries)+'\n').encode())
            fd=os.open(self.root,os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
            job,_=self.repo.reserve(principal,source,p['request_id'],kind,p,
                {'media_folder':str(folder),'media_inputs':entries,'provenance':'engine_upload_snapshot'})
            return job
        job,created=self.repo.reserve(principal,source,p['request_id'],kind,p)
        if not created:return job
        jid=job['id'];folder=self.root/jid;folder.mkdir(mode=0o700)
        return self.execute(job,folder,prior)
    def execute(self,job,folder,prior=None):
        jid=job['id'];kind=job['kind'];p=job['payload'];principal=job['owner_principal']
        _,claimed=self.repo.update(jid,'running',expected={'queued'},event='media.started')
        if not claimed:return self.repo.get(jid)
        try:
            artifacts=[]
            if kind=='censor':
                out=folder/'output';out.mkdir();entries=job['media_inputs']
                result=self.censor_call({'action':'process','images':entries,'output_dir':str(out),'options':{k:v for k,v in p.items() if k not in ('request_id','images')}},folder)
                for row in result['items']:
                    if row['status']!='error':artifacts.append(self.artifact(jid,row['name'],out/row['name'],'image/'+('jpeg' if Path(row['name']).suffix.lower() in ('.jpg','.jpeg') else Path(row['name']).suffix.lower()[1:])))
                manifest=out/'manifest.json';manifest.write_text(dump(result)+'\n')
            else:
                args={k:v for k,v in p.items() if k not in ('request_id','prior') and v is not None}
                result=self.mask_call({'action':'segment',**args,'prior':prior,'output_dir':str(folder)},principal)
                manifest=Path(result['manifest_path'])
                for candidate in result['candidates']:
                    for key in ('mask_file','overlay_file','logits_file'):
                        name=candidate[key];artifacts.append(self.artifact(jid,name,manifest.parent/name,'image/png' if name.endswith('.png') else 'application/octet-stream'))
                # Keep exact SAM manifest on disk for accepted prior/logit verification.
                result={k:v for k,v in result.items() if k not in ('manifest_path','output_dir','image_path')}
                result['params']={k:v for k,v in p.items() if k!='request_id'}
                result['mask_polarity']='white=foreground; black=background'
                result['raster_policy']='stored raster; no EXIF transpose; alpha converted to RGB by subordinate'
                if prior:
                    prior_manifest=Path(prior['manifest_path']).read_bytes()
                    candidate=json.loads(prior_manifest)['candidates'][prior['candidate']]
                    result['prior_provenance']={'candidate':prior['candidate'],'manifest_sha256':hashlib.sha256(prior_manifest).hexdigest(),'logits_sha256':candidate['logits_sha256'],'reference':p['prior']}
            public_manifest=folder/'manifest.json';public_manifest.write_text(dump({**result,'engine_job_id':jid})+'\n')
            artifacts.append(self.artifact(jid,'manifest.json',public_manifest,'application/json'))
            state='succeeded' if result['ok'] else 'partially_succeeded' if any(a['image_name']!='manifest.json' for a in artifacts) else 'failed'
            return self.repo.update(jid,state,{'result':result,'artifacts':artifacts,'artifact_state':'fetched','media_manifest_path':str(manifest)},event='media.finished')[0]
        except Exception as exc:
            code=getattr(exc,'code','MEDIA_FAILED')
            return self.repo.update(jid,'failed',{'error':{'code':code,'message':str(exc) if isinstance(exc,Fault) else type(exc).__name__}},event='media.failed')[0]
