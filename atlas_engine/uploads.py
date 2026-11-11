"""Durable shared byte ingress. Unknown native upload ACK is never retried.
No file paths/URLs are accepted; source identity remains attribution.
"""
import base64,hashlib,io,json,time,uuid
from PIL import Image
from .artifacts import MAX_BYTES,MAX_PIXELS
from .contracts import Fault
from .repository import dump,digest

def validate_pixel_bytes(raw):
    """Reviewed pixel contract: original RGB PNG only; never silently flatten alpha."""
    try:
        with Image.open(io.BytesIO(raw)) as im:
            if (im.format!='PNG' or im.mode!='RGB' or 'transparency' in im.info
                or im.info.get('icc_profile') or im.getexif().get(274,1)!=1
                or getattr(im,'n_frames',1)!=1):
                raise Fault('PIXEL_FORMAT','pixel upscale requires RGB PNG without alpha/transparency, ICC profile or EXIF rotation')
            if im.width*im.height>MAX_PIXELS:raise Fault('IMAGE_LIMIT','pixel budget exceeded')
            im.load()
    except Fault:raise
    except Exception:raise Fault('IMAGE_INVALID','pixel source cannot be decoded')

class Uploads:
    def __init__(self,repo,invoke):
        self.repo=repo;self.invoke=invoke
        with repo.connect() as c:c.executescript('''
        CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY,principal TEXT NOT NULL,request_id TEXT NOT NULL,hash TEXT NOT NULL,project TEXT,size INTEGER NOT NULL,sha256 TEXT NOT NULL,role TEXT NOT NULL,state TEXT NOT NULL,bytes BLOB NOT NULL,data TEXT NOT NULL,UNIQUE(principal,request_id));
        CREATE INDEX IF NOT EXISTS uploads_native ON uploads(principal,project,state);
        CREATE UNIQUE INDEX IF NOT EXISTS uploads_native_name ON uploads(json_extract(data,'$.image_name')) WHERE state='imported';
        ''')
        # A process can die after a successful POST but before local ACK commit.
        with repo.connect() as c:c.execute("update uploads set state='ack_unknown' where state='importing'")
    def row(self,uid):
        with self.repo.connect() as c:r=c.execute('select * from uploads where id=?',(uid,)).fetchone()
        if not r:raise Fault('NOT_FOUND','upload not found')
        return r
    @staticmethod
    def view(r):return {'upload_id':r['id'],'project':r['project'],'size':r['size'],'sha256':r['sha256'],'role':r['role'],'state':r['state'],'received':len(r['bytes']),**json.loads(r['data'])}
    def begin(self,p,principal):
        h=digest(p)
        with self.repo.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('select * from uploads where principal=? and request_id=?',(principal,p['request_id'])).fetchone()
            if old:
                if old['hash']!=h:raise Fault('IDEMPOTENCY_CONFLICT','upload request already bound')
                return self.view(old)
            if c.execute("select count(*) from uploads where state='receiving'").fetchone()[0]>=16:raise Fault('CAPACITY','upload queue full')
            uid=str(uuid.uuid4())
            c.execute('insert into uploads values(?,?,?,?,?,?,?,?,?,?,?)',(uid,principal,p['request_id'],h,p['project'],p['size'],p['sha256'],p['role'],'receiving',b'',dump({})))
            return self.view(c.execute('select * from uploads where id=?',(uid,)).fetchone())
    def chunk(self,p,principal):
        try:raw=base64.b64decode(p['base64'],validate=True)
        except Exception:raise Fault('VALIDATION','invalid base64')
        if not raw or len(raw)>32768:raise Fault('IMAGE_LIMIT','chunk must contain 1..32768 bytes')
        with self.repo.connect() as c:
            c.execute('BEGIN IMMEDIATE');r=c.execute('select * from uploads where id=?',(p['upload_id'],)).fetchone()
            if not r:raise Fault('NOT_FOUND','upload not found')
            current=r['bytes'];offset=p['offset']
            if offset<len(current):
                if current[offset:offset+len(raw)]!=raw:raise Fault('CHUNK_CONFLICT','conflicting duplicate chunk')
                return self.view(r)
            if r['state']!='receiving' or offset!=len(current):raise Fault('UPLOAD_STATE','upload not writable or offset not contiguous')
            if len(current)+len(raw)>r['size']:raise Fault('IMAGE_LIMIT','declared size exceeded')
            c.execute('update uploads set bytes=? where id=?',(current+raw,r['id']))
            return self.view(c.execute('select * from uploads where id=?',(r['id'],)).fetchone())
    def finalize(self,p,principal):
        r=self.row(p['upload_id'])
        if r['state']!='receiving':return self.view(r)
        raw=r['bytes']
        if len(raw)!=r['size'] or hashlib.sha256(raw).hexdigest()!=r['sha256']:raise Fault('UPLOAD_INTEGRITY','size/hash mismatch')
        try:
            with Image.open(io.BytesIO(raw)) as im:
                if im.format not in ('PNG','JPEG') or im.width*im.height>MAX_PIXELS:raise Fault('IMAGE_LIMIT','unsupported format/pixel budget')
                if getattr(im,'n_frames',1)!=1:raise Fault('IMAGE_LIMIT','animated uploads unsupported')
                im.load()
                if r['role']=='mask' and (im.format!='PNG' or im.mode!='L'):raise Fault('MASK_FORMAT','mask requires grayscale L PNG, white-edit/black-preserve')
                if im.getexif().get(274,1)!=1:raise Fault('ORIENTATION','normalize EXIF orientation before upload')
                meta={'width':im.width,'height':im.height,'mode':im.mode,'format':im.format,'mask_contract':'white-edit-black-preserve' if r['role']=='mask' else None}
        except Fault:raise
        except Exception:raise Fault('IMAGE_INVALID','image cannot be fully decoded')
        with self.repo.connect() as c:c.execute("update uploads set state='ready',data=? where id=? and state='receiving'",(dump(meta),r['id']))
        return self.view(self.row(r['id']))
    def abort(self,uid,principal):
        self.row(uid)
        with self.repo.connect() as c:
            c.execute("update uploads set state='aborted',bytes=? where id=? and state in ('receiving','ready')",(b'',uid))
        return self.view(self.row(uid))
    def import_native(self,p,principal,enabled):
        if not enabled:raise Fault('SUBMISSION_DISABLED','native uploads disabled; bytes remain local')
        r=self.row(p['upload_id']);meta=json.loads(r['data']);family=p['family']
        if family=='pixel':validate_pixel_bytes(r['bytes'])
        if r['state']=='imported':
            if meta.get('family')!=family:raise Fault('IDEMPOTENCY_CONFLICT','native upload family differs')
            return self.view(r)
        if r['state']!='ready':raise Fault('UPLOAD_STATE','must finalize; uncertain import requires reconciliation, never blind retry')
        if r['role']=='mask' and family not in ('sd-1','anima'):raise Fault('MASK_FAMILY','mask requires explicit SD1/Anima family')
        raw=r['bytes']
        if r['role']=='mask':
            # Public white-edit contract. Old CLI differs; GPU polarity is still an
            # explicit enablement gate, never inferred from this normalization.
            from PIL import ImageOps
            with Image.open(io.BytesIO(raw)) as im:
                b=io.BytesIO();ImageOps.invert(im).save(b,format='PNG');raw=b.getvalue()
        meta.update({'family':family,'native_bytes_sha256':hashlib.sha256(raw).hexdigest()})
        with self.repo.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            changed=c.execute("update uploads set state='importing',data=? where id=? and state='ready'",(dump(meta),r['id'])).rowcount
            if not changed:raise Fault('UPLOAD_STATE','native upload already fenced')
        try:
            response=self.invoke.upload(raw,'mask.png' if r['role']=='mask' else 'source.'+meta['format'].lower())
            name=response.get('image_name')
            if not isinstance(name,str) or not name or len(name)>200:raise ValueError('invalid native ACK')
            meta['image_name']=name
            with self.repo.connect() as c:c.execute("update uploads set state='imported',data=? where id=?",(dump(meta),r['id']))
        except Exception:
            with self.repo.connect() as c:c.execute("update uploads set state='ack_unknown' where id=?",(r['id'],))
        return self.view(self.row(r['id']))
    def pixel(self,name):
        with self.repo.connect() as c:r=c.execute("select bytes from uploads where state='imported' and role='image' and json_extract(data,'$.image_name')=?",(name,)).fetchone()
        if not r:return False
        validate_pixel_bytes(r[0]);return True
    def mask(self,name):
        with self.repo.connect() as c:r=c.execute("select data from uploads where state='imported' and role='mask' and json_extract(data,'$.image_name')=?",(name,)).fetchone()
        if not r:raise Fault('MASK_CONTRACT','normalized mask upload required')
        return json.loads(r[0])
