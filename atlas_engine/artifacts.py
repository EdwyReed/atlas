"""Bounded original-byte artifacts, distinct from rendering and optional embeddings."""
import base64,hashlib,io,json,os,uuid
from pathlib import Path
from urllib.parse import quote
from PIL import Image,UnidentifiedImageError
from .contracts import Fault
from .repository import dump
from .state_backup import storage_path
MAX_BYTES=32*1024*1024;MAX_PIXELS=24*1024*1024
class Artifacts:
    def __init__(self,repo,invoke):
        import threading
        self.lock=threading.Lock()
        self.native_scope=lambda: None
        self.repo=repo;self.invoke=invoke;self.root=repo.root/'artifacts';self.root.mkdir(exist_ok=True)
        with repo.connect() as c:c.execute('''create table if not exists artifacts(id TEXT PRIMARY KEY,job_id TEXT NOT NULL,image_name TEXT NOT NULL,path TEXT NOT NULL,data TEXT NOT NULL,UNIQUE(job_id,image_name))''')
    def get(self,aid):
        with self.repo.connect() as c:r=c.execute('select * from artifacts where id=?',(aid,)).fetchone()
        if not r:raise Fault('NOT_FOUND','artifact not found')
        return r,json.loads(r['data'])
    def list(self,jid):
        self.repo.get(jid)
        with self.repo.connect() as c:
            return [json.loads(r[0]) for r in c.execute('select data from artifacts where job_id=? order by image_name',(jid,))]
    def sync(self,jid):
        # Repair a crash after durable artifact commit but before job metadata.
        j=self.repo.get(jid);items=self.list(jid)
        names={a['image_name'] for a in items}
        state='fetched' if j.get('outputs') and set(j['outputs'])<=names else 'partially_fetched' if names else 'not_fetched'
        if j.get('artifacts')!=items or j.get('artifact_state')!=state:
            self.repo.update(jid,changes={'artifact_state':state,'artifacts':items},event='artifact.lineage_synced')
    def fetch(self,jid,name):
        with self.lock:return self._fetch(jid,name)
    def _fetch(self,jid,name):
        j=self.repo.get(jid)
        if name not in j.get('outputs',[]):raise Fault('ARTIFACT_NOT_LINKED','image is not an observed final output of this job')
        aid=str(uuid.uuid5(uuid.NAMESPACE_URL,'atlas-artifact:'+jid+':'+name))
        with self.repo.connect() as c:r=c.execute('select data from artifacts where id=?',(aid,)).fetchone()
        if r:
            row,data=self.get(aid)
            if storage_path(self.repo.root,row['path']).is_file() and hashlib.sha256(storage_path(self.repo.root,row['path']).read_bytes()).hexdigest()==data['sha256']:
                self.sync(jid);return data
            raise Fault('ARTIFACT_INTEGRITY','stored artifact missing or changed; no silent overwrite')
        lineage=None
        invoke=self.resolve_backend(j) if hasattr(self,'resolve_backend') else self.invoke
        if j.get('plan',{}).get('workflow_image_recipe'):
            scope=j['native_scope']
            if not hasattr(self,'resolve_backend') and scope != self.native_scope():
                raise Fault('NATIVE_SCOPE_MISMATCH','artifact source differs from current native backend')
            matches=[e for item in j.get('native_items',{}).values() for e in item.get('native_results',{}).get('entries',[])
                     if e.get('status')=='available' and e.get('image',{}).get('image_name')==name and e.get('lineage')]
            if len(matches)!=1:raise Fault('ARTIFACT_NOT_LINKED','verified selected IMAGE lineage required')
            lineage=matches[0]['lineage']
        self.repo.update(jid,changes={'artifact_state':'fetching'},event='artifact.fetch_intent')
        try:
            raw=bytearray()
            with invoke.http.stream('GET','/api/v1/images/i/'+quote(name,safe='')+'/full') as r:
                if r.status_code!=200:raise Fault('ARTIFACT_HTTP','image GET rejected')
                for chunk in r.iter_bytes():
                    if len(raw)+len(chunk)>MAX_BYTES:raise Fault('IMAGE_LIMIT','image exceeds byte limit')
                    raw.extend(chunk)
            with Image.open(io.BytesIO(raw)) as im:
                if im.width*im.height>MAX_PIXELS or im.format not in ('PNG','JPEG'):raise Fault('IMAGE_LIMIT','unsupported image or pixel limit')
                im.load();width,height=im.size;mime='image/png' if im.format=='PNG' else 'image/jpeg'
                pixels_sha256=hashlib.sha256(im.tobytes()).hexdigest();image_mode=im.mode
            data={'artifact_id':aid,'job_id':jid,'image_name':name,'sha256':hashlib.sha256(raw).hexdigest(),'size':len(raw),'width':width,'height':height,'mime':mime,'embedding_state':'disabled'}
            if lineage:
                if (width,height)!=(lineage['width'],lineage['height']):
                    raise Fault('ARTIFACT_INTEGRITY','download dimensions differ from native child result')
                data.update(native_lineage=lineage,pixels_sha256=pixels_sha256,image_mode=image_mode)
            dest=self.root/aid;tmp=self.root/(aid+'.'+uuid.uuid4().hex+'.part')
            try:
                with tmp.open('xb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
                os.replace(tmp,dest)
                with self.repo.connect() as c:c.execute('insert into artifacts values(?,?,?,?,?)',(aid,jid,name,str(dest.resolve()),dump(data)))
            finally:
                if tmp.exists():tmp.unlink()
            self.sync(jid)
            return data
        except Exception as e:
            self.repo.update(jid,changes={'artifact_state':'fetch_failed','artifact_error':getattr(e,'code',type(e).__name__)},event='artifact.fetch_failed')
            if isinstance(e,Fault):raise
            raise Fault('ARTIFACT_FETCH_FAILED','download or decoding failed; rendering state unchanged') from e
    def read(self,aid,offset,size):
        row,d=self.get(aid);p=storage_path(self.repo.root,row['path'])
        if not p.is_file() or p.stat().st_size!=d['size'] or hashlib.sha256(p.read_bytes()).hexdigest()!=d['sha256']:raise Fault('ARTIFACT_INTEGRITY','artifact file changed')
        if offset>d['size']:raise Fault('RANGE','offset outside artifact')
        with p.open('rb') as f:f.seek(offset);b=f.read(size)
        return {**d,'offset':offset,'base64':base64.b64encode(b).decode(),'eof':offset+len(b)==d['size']}

    def capture_upload(self,jid,name,raw):
        """Preserve ingress bytes, not the native server's PNG re-encoding."""
        with self.lock:
            aid=str(uuid.uuid5(uuid.NAMESPACE_URL,'atlas-upload-original:'+jid))
            with self.repo.connect() as c:old=c.execute('select data from artifacts where id=?',(aid,)).fetchone()
            if old:
                self.read(aid,0,1)
                return json.loads(old[0])
            if len(raw)>MAX_BYTES:raise Fault('IMAGE_LIMIT','original artifact byte budget exceeded')
            with Image.open(io.BytesIO(raw)) as im:
                if im.width*im.height>MAX_PIXELS:raise Fault('IMAGE_LIMIT','original artifact pixel budget exceeded')
                im.load();width,height=im.size;mime='image/png' if im.format=='PNG' else 'image/jpeg'
            data={'artifact_id':aid,'job_id':jid,'image_name':name,'sha256':hashlib.sha256(raw).hexdigest(),
                  'size':len(raw),'width':width,'height':height,'mime':mime,'representation':'original-upload','embedding_state':'disabled'}
            dest=self.root/aid
            # A crash before the SQLite commit may leave our deterministic file.
            if dest.exists():
                if dest.read_bytes()!=raw:raise Fault('ARTIFACT_INTEGRITY','original artifact differs; no overwrite')
            else:
                with dest.open('xb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
            fd=os.open(self.root,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)
            with self.repo.connect() as c:c.execute('insert into artifacts values(?,?,?,?,?)',(aid,jid,name,str(dest.resolve()),dump(data)))
            return data
