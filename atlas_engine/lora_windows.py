"""Windows-side LoRA/checkpoint installer. Sent over SSH, never model bytes.

No shell interpolation, service management, content policies or GPU operations.
Credentials stay in the Windows environment. This module also runs offline with
injected HTTP/download adapters for end-to-end acceptance.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import urllib.error
import urllib.request
import uuid

class InstallError(Exception):
    pass


class PreflightError(InstallError):
    """Only raised before ANY filesystem/download/native-write operation."""
    def __init__(self,stage,cause):
        self.failure={'stage':stage,'outcome':'rejected','no_effect':True}
        if isinstance(cause,urllib.error.HTTPError):
            self.failure['http_status']=cause.code
        super().__init__('Windows installer read-only preflight failed')


def parse_link(link):
    u=urlsplit(link)
    if u.scheme!='https' or u.hostname!='civitai.red' or u.port is not None or u.username or u.password or u.fragment:
        raise InstallError('expected credential-free https civitai.red model link')
    match=re.fullmatch(r'/models/([0-9]+)(?:/[A-Za-z0-9_-]+)?/?',u.path)
    if not match:raise InstallError('invalid model path')
    q=parse_qs(u.query,keep_blank_values=True)
    if set(q)-{'modelVersionId'} or any(len(v)!=1 for v in q.values()):raise InstallError('unsupported or duplicate query')
    version=q.get('modelVersionId',[None])[0]
    if version is not None and not re.fullmatch(r'[0-9]+',version):raise InstallError('invalid literal modelVersionId')
    return match[1],version


def select(link,api,file_id=None,model_type=None):
    mid,vid=parse_link(link)
    model=api('GET','https://civitai.red/api/v1/models/'+mid)
    allowed = ('Checkpoint',) if model_type == 'Checkpoint' else ('LORA','LoCon')
    if model_type not in (None, 'Checkpoint') or str(model.get('id'))!=mid or model.get('type') not in allowed:raise InstallError('model identity/type mismatch')
    resolution='literal modelVersionId'
    if vid is None:
        versions=model.get('modelVersions') or []
        if not versions or not isinstance(versions[0].get('id'),int):raise InstallError('API did not resolve a version')
        vid=str(versions[0]['id']);resolution='API modelVersions[0]; first version listed by API, not inferred from URL/name'
    version=api('GET','https://civitai.red/api/v1/model-versions/'+vid)
    if str(version.get('id'))!=vid or str(version.get('modelId'))!=mid:raise InstallError('literal model/version mismatch; no identifier normalization')
    files=[f for f in version.get('files',[]) if f.get('type')=='Model' and (f.get('metadata') or {}).get('format')=='SafeTensor']
    if file_id is not None:files=[f for f in files if str(f.get('id'))==file_id]
    elif len(files)>1:files=[f for f in files if f.get('primary') is True]
    if len(files)!=1:raise InstallError('ambiguous/missing SafeTensor file; specify file_id')
    f=files[0];name=f.get('name','');h=(f.get('hashes') or {}).get('SHA256','')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._()-]{0,180}\.safetensors',name,re.I) or name.endswith((' ','.')) or ':' in name:
        raise InstallError('unsafe filename')
    if name.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}:raise InstallError('reserved filename')
    if not re.fullmatch(r'[a-fA-F0-9]{64}',h):raise InstallError('verified SHA256 required')
    if not isinstance(f.get('id'),int):raise InstallError('invalid file id')
    # Keep API file selectors; never accept API-supplied arbitrary download hosts,
    # token query strings, credentials or another version.
    u=urlsplit(f.get('downloadUrl',''))
    if u.scheme!='https' or u.hostname != 'civitai.red' or u.port is not None or u.username or u.password or u.fragment or u.path!='/api/download/models/'+vid:
        raise InstallError('unsupported download URL')
    q=parse_qs(u.query,keep_blank_values=True)
    if set(q)-{'type','format','size','fp','fileId'} or any(len(v)!=1 for v in q.values()):raise InstallError('unsafe download selector')
    if 'fileId' in q and q['fileId']!=[str(f['id'])]:raise InstallError('literal download fileId mismatch')
    url='https://civitai.red'+u.path+('?' +u.query if u.query else '')
    return {'source_url':link,'model_id':mid,'modelVersionId':vid,'version_resolution':resolution,'model_name':model.get('name'),'version_name':version.get('name'),'base_model':version.get('baseModel'),'trained_words':version.get('trainedWords',[]),'file_id':str(f['id']),'filename':name,'hashes':f['hashes'],'sha256':h.lower(),'sizeKB':f.get('sizeKB'),'download_url':url}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*a,**k):return None

class Http:
    def __init__(self):self.opener=urllib.request.build_opener(NoRedirect())
    def open(self,method,url,body=None):
        # Rebuild honest client identity on every allowed redirect, without forwarding auth.
        u=urlsplit(url);headers={'User-Agent':'AtlasEngine/0.3 metadata verification'}
        native = u.scheme == 'http' and u.netloc == '127.0.0.1:9090'
        storage = u.hostname in ('b2.civitai.com', 'blobs-b2.civitai.com', 'image.civitai.com') or (u.hostname or '').endswith('.cloudflarestorage.com')
        if not native and (u.scheme != 'https' or u.username or u.password or u.port is not None or u.fragment
                           or not (u.hostname == 'civitai.red' or storage)
                           or (storage and u.path.startswith('/api/'))):
            raise InstallError('only civitai.red site/API or provider storage allowed')
        if u.hostname == 'civitai.red' and u.path.startswith('/api/v1/'):
            headers['Accept']='application/json'
        if u.hostname=='civitai.red':
            token=os.environ.get('CIVITAI_API_KEY') or os.environ.get('CIVITAI_API_TOKEN')
            if token:headers['Authorization']='Bearer '+token
        if u.hostname=='127.0.0.1':
            token=os.environ.get('ATLAS_INVOKE_TOKEN')
            if token:headers['Authorization']='Bearer '+token
        if body is not None:headers['Content-Type']='application/json'
        return self.opener.open(urllib.request.Request(url,data=None if body is None else json.dumps(body).encode(),headers=headers,method=method),timeout=60)
    def __call__(self,method,url,body=None):
        with self.open(method,url,body) as r:return json.load(r)
    def download(self,url,target):
        for _ in range(8):
            try:
                with self.open('GET',url) as r, target.open('xb') as out:
                    while True:
                        chunk=r.read(1024*1024)
                        if not chunk:break
                        out.write(chunk)
                    out.flush();os.fsync(out.fileno())
                return
            except urllib.error.HTTPError as e:
                if e.code not in (301,302,303,307,308):raise InstallError('download rejected by remote server') from None
                next_url=e.headers.get('Location','');u=urlsplit(next_url)
                if u.scheme!='https' or u.username or u.password or u.port is not None or not (u.hostname in ('civitai.red','b2.civitai.com','blobs-b2.civitai.com','image.civitai.com') or (u.hostname or '').endswith('.cloudflarestorage.com')):
                    raise InstallError('download redirect host not allowed')
                # Headers are rebuilt on each request, never forwarded to storage.
                url=next_url
        raise InstallError('too many download redirects')


def hash_file(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()


def safe_path(path):
    for p in (path,*path.parents):
        if p.is_symlink() or (p.exists() and getattr(p.stat(),'st_file_attributes',0)&1024):raise InstallError('reparse/symlink paths not allowed')


@contextlib.contextmanager
def file_lock(path):
    safe_path(path)
    with path.open('a+b') as f:
        if os.name=='nt':
            import msvcrt
            if f.tell()==0:f.write(b'0');f.flush()
            f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            try:yield
            finally:f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            yield


def install(request,root,api,download,timeout=600):
    # Metadata and availability checks are read-only and happen before download.
    try:
        meta=select(request['url'],api,request.get('file_id'),model_type=request.get('model_type'))
        expected=request.get('expected_sha256')
        if expected is not None and (not isinstance(expected,str) or not re.fullmatch(r'[a-f0-9]{64}',expected) or meta['sha256']!=expected):
            raise InstallError('selected file SHA256 changed since intake')
    except Exception as e:raise PreflightError('metadata',e) from None
    local='http://127.0.0.1:9090'
    native_type = 'main' if request.get('model_type') == 'Checkpoint' else 'lora'
    try:
        root=Path(root);safe_path(root)
        if not root.is_dir():raise InstallError('configured models root missing')
        api('GET',local+'/api/v1/app/version')
    except Exception as e:raise PreflightError('availability',e) from None
    # Recheck typed exact-file inventory on Windows before any download/write.
    # A matching registration may use another filename or have appeared since
    # acquisition's inventory read. Bare 64-hex digests prove no algorithm.
    def source_match(m):
        raw = m.get('hash', '')
        algorithm = m.get('hash_algorithm', '')
        if isinstance(raw, str) and ':' in raw:
            algorithm, raw = raw.split(':', 1)
        if not isinstance(algorithm, str) or not isinstance(raw, str):return False
        hashes = {k.upper():str(v).lower() for k,v in meta.get('hashes', {}).items()}
        return bool(algorithm) and hashes.get(algorithm.upper()) == raw.lower()
    try:
        value=api('GET',local+'/api/v2/models/')
        rows=value['models'] if isinstance(value,dict) else value
        exact=[m for m in rows if source_match(m)]
        if len(exact)>1:raise InstallError('ambiguous exact-file native registration')
        if exact:
            m=api('GET',local+'/api/v2/models/i/'+exact[0]['key'])
            if m.get('key')!=exact[0]['key'] or m.get('path')!=exact[0].get('path') or m.get('type')!=native_type or not source_match(m):
                raise InstallError('exact-file native readback mismatch')
            path=Path(m['path']);safe_path(path)
            if not path.is_file() or hash_file(path)!=meta['sha256']:raise InstallError('registered bytes do not match selected SHA256')
            return {**meta,'path':str(path),'bytes':path.stat().st_size,'installed_key':m['key'],
                    'integrity_verified':True,'state':'completed','reused':True}
    except Exception as e:raise PreflightError('availability',e) from None
    state=root/'.atlas-lora-installs';safe_path(state);state.mkdir(exist_ok=True)
    identity=meta['sha256'];record=state/(identity+'.json');target=root/meta['filename'];safe_path(target);safe_path(record)
    def save(value):
        tmp=state/(uuid.uuid4().hex+'.tmp')
        with tmp.open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
        os.replace(tmp,record)
    def inventory():
        value=api('GET',local+'/api/v2/models/')
        return value['models'] if isinstance(value,dict) else value
    def registered():
        matches=[m for m in inventory() if str(m.get('path','')).replace('/','\\').casefold()==str(target).replace('/','\\').casefold()]
        if len(matches)>1:raise InstallError('ambiguous native path registration')
        if matches:
            m=api('GET',local+'/api/v2/models/i/'+matches[0]['key'])
            if m.get('type')!=native_type or m.get('key')!=matches[0]['key'] or str(m.get('path','')).replace('/','\\').casefold()!=str(target).replace('/','\\').casefold():raise InstallError('native registration readback mismatch')
            return m
    with file_lock(state/(identity+'.lock')):
        existing=json.loads(record.read_text(encoding='utf-8')) if record.exists() else {}
        if target.exists():
            if not target.is_file() or hash_file(target)!=identity:raise InstallError('existing file differs; never overwrite')
        else:
            if existing:raise InstallError('journaled file missing; manual reconciliation required')
            part=root/('.atlas-'+uuid.uuid4().hex+'.part')
            try:
                download(meta['download_url'],part)
                if hash_file(part)!=identity:raise InstallError('download SHA256 mismatch; not registered')
                safe_path(target)
                # Hard-link is atomic no-replace on Windows/NTFS and POSIX; unlike
                # rename it cannot silently overwrite a concurrent destination.
                os.link(part,target)
            finally:
                if part.exists():part.unlink()
        native=registered()
        if native:
            result={**meta,'path':str(target),'bytes':target.stat().st_size,'installed_key':native['key'],'integrity_verified':True,'state':'completed','reused':True}
            save(result);return result
        if existing.get('state') in ('install_intent','ack_unknown'):
            raise InstallError('native registration outcome unknown; no automatic replay')
        intent={**meta,'path':str(target),'state':'install_intent'};save(intent)
        try:
            job=api('POST',local+'/api/v2/models/install?'+urlencode({'source':str(target),'inplace':'true'}),{'name':meta['model_name']+' / '+str(meta['version_name']),'type':native_type})
            jid=job['id'];intent.update(native_job_id=jid);save(intent)
            deadline=time.monotonic()+timeout
            while True:
                job=api('GET',local+'/api/v2/models/install/'+str(jid))
                if job.get('status')=='completed':break
                if job.get('status') in ('error','cancelled','canceled'):raise InstallError('native install failed; inspect native job')
                if time.monotonic()>deadline:raise InstallError('native install timeout; do not replay')
                time.sleep(1)
            native=registered()
            if not native:raise InstallError('completed install missing verified native key')
            if hash_file(target)!=identity:raise InstallError('file changed during native registration')
            result={**meta,'path':str(target),'bytes':target.stat().st_size,'native_job_id':jid,'installed_key':native['key'],'integrity_verified':True,'state':'completed','reused':False}
            save(result);return result
        except Exception:
            save({**intent,'state':'ack_unknown'});raise InstallError('registration outcome uncertain; retained journal, no replay') from None


def run(request):
    if os.name!='nt':
        return {'ok':False,'failure':{'stage':'platform','outcome':'rejected','no_effect':True}}
    http=Http()
    try:return {'ok':True,'result':install(request,r'E:\ModelsAI',http,http.download)}
    except PreflightError as e:return {'ok':False,'failure':e.failure}
    except Exception:
        # Past the read-only boundary we cannot prove no effect. Retain journals.
        return {'ok':False,'failure':{'stage':'install','outcome':'uncertain','no_effect':False}}


def main(request):
    # No raw HTTP/SSH exception, URL redirect, headers or tokens in output.
    print(json.dumps(run(request),ensure_ascii=True))
