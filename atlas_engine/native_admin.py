"""Typed native administration owned by Engine, never a CLI/shell proxy.

Mutations use the existing SQLite jobs/events journal. Once remote I/O starts,
an uncertain outcome is retained and is never automatically replayed. Revisions
are GET-before-write checks, NOT native CAS against external InvokeAI clients.
"""
import fcntl
from urllib.parse import quote, urlencode
from .contracts import Fault
from .repository import digest

MUTATIONS = {'upload.import','boards.create','boards.update','boards.delete','boards.images.update','models.install','models.job.update','models.lora.install','models.checkpoint.install'}
READS = {'boards.get','boards.images','boards.dates','boards.date','models.jobs','models.job','models.scan','models.starters','models.missing','models.hf.status'}
OPERATIONS = MUTATIONS | READS

def part(value):
    # Even quoted dot segments may be normalized by an HTTP client.
    if value in ('.','..') or '/' in value or '\\' in value or any(ord(c)<32 for c in value):
        raise Fault('VALIDATION','invalid native identifier')
    return quote(value,safe='')

def brief(job):
    """Do not expose upstream download tokens, config, source URLs or tracebacks."""
    if not isinstance(job,dict) or type(job.get('id')) is not int:raise Fault('REMOTE_SHAPE','invalid native install job')
    return {k:job[k] for k in ('id','status','bytes','total_bytes') if k in job}

class NativeAdmin:
    def __init__(self,engine,policy,enabled=False):
        self.e=engine;self.policy=policy or {};self.enabled=enabled
        self.sources=self.policy.get('model_sources',{})
        if not isinstance(self.sources,dict):raise Fault('POLICY_INVALID','model_sources must be an object')
        for name,s in self.sources.items():
            if not isinstance(name,str) or not isinstance(s,dict) or set(s)-{'source','inplace','scan_path','config'}:
                raise Fault('POLICY_INVALID','invalid model source descriptor')
            if 'source' in s and (not isinstance(s['source'],str) or not s['source']):raise Fault('POLICY_INVALID','source must be literal string')
            if 'scan_path' in s and (not isinstance(s['scan_path'],str) or not s['scan_path']):raise Fault('POLICY_INVALID','scan_path must be literal string')
            if 'inplace' in s and type(s['inplace']) is not bool:raise Fault('POLICY_INVALID','inplace must be boolean')
            config=s.get('config',{})
            if not isinstance(config,dict) or set(config)-{'name','description','type','base'} or any(not isinstance(v,str) for v in config.values()):raise Fault('POLICY_INVALID','invalid model config')
            # No credential query strings or URI userinfo accepted even in journalled config.
            if any(t in s.get('source','').lower() for t in ('access_token=','token=','@')):raise Fault('POLICY_INVALID','credential-bearing sources are not supported')
    def board(self,bid):
        board=self.e.invoke.call('GET','/api/v1/boards/'+part(bid))
        if not isinstance(board,dict) or board.get('board_id')!=bid:raise Fault('REMOTE_SHAPE','native board identity mismatch')
        return board
    @staticmethod
    def board_result(board):return {'board':board,'revision':digest(board),'concurrency':'GET-before-write; no remote atomic CAS'}
    def scope(self,p):
        if not getattr(self.e,'connection_id',None):raise Fault('INVOKE_UNCONFIGURED','configure a native connection')
        if p['connection_id']!=self.e.connection_id or p['backend_generation']!=self.e.backend_generation:
            raise Fault('NATIVE_SCOPE_MISMATCH','reference does not match the pinned backend')

    def verify_import(self,job):
        import hashlib,io
        from PIL import Image
        from .artifacts import MAX_BYTES,MAX_PIXELS
        p=job['payload'];ack=job['native_receipt'];name=ack['image_name']
        actual=self.e.invoke.call('GET','/api/v1/images/i/'+part(name))
        expected=job['prepared']
        if (actual.get('image_name')!=name or actual.get('board_id')!=p['board_id'] or
            actual.get('is_intermediate') is not False or
            any(actual.get(k)!=expected[k] for k in ('width','height'))):
            raise Fault('REMOTE_VERIFY','image identity/dimensions/routing readback mismatch')
        if p['board_id'] is not None:
            self.board(p['board_id'])
            names=self.e.invoke.call('GET','/api/v1/boards/'+part(p['board_id'])+'/image_names')
            if name not in names:raise Fault('REMOTE_VERIFY','image missing from exact board')
        raw=bytearray()
        with self.e.invoke.http.stream('GET','/api/v1/images/i/'+part(name)+'/full') as response:
            if response.status_code!=200:raise Fault('REMOTE_VERIFY','native image bytes unavailable')
            for chunk in response.iter_bytes():
                if len(raw)+len(chunk)>MAX_BYTES:raise Fault('IMAGE_LIMIT','native image too large')
                raw.extend(chunk)
        row=self.e.uploads.row(p['upload_id']);original=row['bytes']
        if hashlib.sha256(original).hexdigest()!=expected['sha256']:raise Fault('UPLOAD_INTEGRITY','original changed')
        submitted,representation=self.e.uploads.native_representation(row,p['family'])
        if representation!=expected['native_representation']:raise Fault('UPLOAD_INTEGRITY','native representation changed')
        with Image.open(io.BytesIO(raw)) as native,Image.open(io.BytesIO(submitted)) as source:
            if native.width*native.height>MAX_PIXELS:
                raise Fault('IMAGE_LIMIT','native pixel budget exceeded')
            native.load();source.load()
            if native.size!=source.size or native.mode!=source.mode or native.tobytes()!=source.tobytes():
                raise Fault('REMOTE_VERIFY','native pixels differ from explicit submitted representation')
        artifact=self.e.artifacts.capture_upload(job['id'],name,original)
        native_representation={**representation,'download_sha256':hashlib.sha256(raw).hexdigest(),
            'download_size':len(raw),'original_sha256':expected['sha256'],'upload_id':p['upload_id'],
            'import_job_id':job['id'],'image_name':name,**{k:p[k] for k in ('connection_id','backend_generation')}}
        # Repair the crash window between durable ACK and upload linkage.
        import json
        from .repository import dump
        with self.e.repo.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('select data from uploads where id=?',(p['upload_id'],)).fetchone()
            meta=json.loads(row['data']);meta.update({'image_name':name,'family':p['family'],
                'native_verified':native_representation})
            db.execute("update uploads set state='imported',data=? where id=?",(dump(meta),p['upload_id']))
        return {'reference':{k:p[k] for k in ('connection_id','backend_generation','board_id')},
                'image_name':name,'upload_id':p['upload_id'],'native':actual,
                'original':artifact,'family':p['family'],'role':expected['role'],
                'native_representation':native_representation,'native_bytes_sha256':native_representation['download_sha256']}

    def verify_ack(self,job):
        p=job['payload'];op=p['operation']
        if op=='upload.import':return self.verify_import(job)
        b=self.board(job['native_receipt']['board_id'])
        if b.get('board_name')!=p['name']:raise Fault('REMOTE_VERIFY','board creation readback mismatch')
        return self.board_result(b)

    def call(self,op,p,principal,source):
        if op in MUTATIONS and (op.startswith('boards.') or op=='upload.import'):self.scope(p)
        if op in MUTATIONS and not self.enabled:raise Fault('ADMIN_WRITES_DISABLED','native writes disabled in explicit read-only mode')
        if op in ('models.install','models.scan') and p['source_id'] not in self.sources:raise Fault('NOT_FOUND','model source alias not configured')
        # Serializes our own checks/writes, including multiple Engine instances.
        # Does not pretend to lock external UI/API clients.
        with (self.e.repo.root/'admin.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            if op not in MUTATIONS:return self.read(op,p,principal)
            payload={'operation':op,**p}
            old=self.e.repo.request(principal,p['request_id'],'native_admin',payload)
            if old:
                if old['state']=='submitting':old=self.e.repo.update(old['id'],'ack_unknown',expected={'submitting'},event='admin.ambiguous_no_replay')[0]
                if old['state']=='ack_unknown' and old.get('native_receipt') and op in ('upload.import','boards.create'):
                    try:result=self.verify_ack(old)
                    except Exception:return old
                    return self.e.repo.update(old['id'],'succeeded',{'result':result},expected={'ack_unknown'},event='admin.get_verified')[0]
                return old
            prepared=self.prepare(op,p,principal)
            job,created=self.e.repo.reserve(principal,source,p['request_id'],'native_admin',payload,{'prepared':prepared})
            if not created:return job
            job,claimed=self.e.repo.update(job['id'],'submitting',expected={'queued'},event='admin.remote_intent')
            if not claimed:return job
            try:
                result=self.mutate(op,p,prepared,principal,job['id'])
            except Exception as exc:
                from .lora_install import WindowsNoEffect
                if op in ('models.lora.install','models.checkpoint.install') and isinstance(exc,WindowsNoEffect):
                    return self.e.repo.update(job['id'],'failed',{'error':{'code':exc.code,'message':str(exc)},'installer_outcome':exc.failure},expected={'submitting'},event='admin.preflight_no_effect')[0]
                # Includes rejected HTTP and failed verification: the upstream may
                # have committed a partial effect. No automatic second attempt.
                return self.e.repo.update(job['id'],'ack_unknown',{'error':{'code':getattr(exc,'code','UPSTREAM_UNAVAILABLE'),'message':'native outcome or verification uncertain; inspect native state, do not replay'}},expected={'submitting'},event='admin.ambiguous_no_replay')[0]
            return self.e.repo.update(job['id'],'succeeded',{'result':result},expected={'submitting'},event='admin.verified')[0]
    def read(self,op,p,principal):
        inv=self.e.invoke
        if op=='boards.get':
            if p['board_id']=='none':
                return {'board':{'board_id':'none','board_name':'uncategorized'},'virtual':True}
            return self.board_result(self.board(p['board_id']))
        if op=='boards.images':
            if p['board_id']!='none':self.board(p['board_id'])
            return inv.call('GET','/api/v1/boards/'+part(p['board_id'])+'/image_names')
        if op in ('boards.dates','boards.date'):
            # These are global metadata inventories, not scoped board views.
            return inv.call('GET','/api/v1/virtual_boards/by_date'+('/'+part(p['date'])+'/image_names' if op=='boards.date' else ''))
        if op=='models.jobs':return [brief(j) for j in inv.call('GET','/api/v2/models/install')]
        if op=='models.job':return brief(inv.call('GET','/api/v2/models/install/'+str(p['id'])))
        if op=='models.scan':
            path=self.sources[p['source_id']].get('scan_path')
            if not path:raise Fault('POLICY_INVALID','source alias has no scan_path')
            return inv.call('GET','/api/v2/models/scan_folder?'+urlencode({'scan_path':path}))
        path={'models.starters':'starter_models','models.missing':'missing','models.hf.status':'hf_login'}[op]
        return inv.call('GET','/api/v2/models/'+path)
    def prepare(self,op,p,principal):
        if op=='upload.import':
            result=self.e.uploads.prepare_import(p)
            if p['board_id'] is not None:self.board(p['board_id'])
            return result
        if op in ('models.lora.install','models.checkpoint.install'):
            from .lora_install import descriptor
            from .lora_windows import parse_link, InstallError
            cfg=descriptor(self.policy)
            try:parse_link(p['url'])
            except (InstallError,ValueError) as e:raise Fault('VALIDATION',str(e)) from None
            return {'windows_installer_sha256':digest(cfg),'destination':r'E:\ModelsAI'}
        if op in ('boards.update','boards.delete'):
            b=self.board(p['board_id'])
            if digest(b)!=p['expected_revision']:raise Fault('REVISION_CONFLICT','board changed; fetch current revision')
            if op=='boards.update':
                changes={target:p[key] for key,target in [('name','board_name'),('archived','archived'),('cover_image_name','cover_image_name'),('visibility','board_visibility')] if p.get(key) is not None}
                if p['clear_cover']:
                    if p['cover_image_name'] is not None:raise Fault('VALIDATION','clear_cover conflicts with cover_image_name')
                    changes['cover_image_name']=None
                if not changes:raise Fault('VALIDATION','at least one board change required')
                return {'changes':changes,'before_revision':digest(b)}
            return {'before_revision':digest(b)}
        if op=='boards.images.update':
            if len(set(p['image_names']))!=len(p['image_names']):raise Fault('VALIDATION','duplicate image names')
            if p['action'] in ('add','move'):
                if not p['board_id']:raise Fault('VALIDATION','destination board required')
                destination=self.board(p['board_id'])
            elif p['board_id'] is not None:raise Fault('VALIDATION','remove requires null board_id')
            for name in p['image_names']:
                im=self.e.invoke.call('GET','/api/v1/images/i/'+part(name))
                if im.get('board_id'):self.board(im['board_id'])
            return {}
        if op=='models.install':
            s=self.sources[p['source_id']]
            if not s.get('source'):raise Fault('POLICY_INVALID','alias has no install source')
            return {'source_id':p['source_id'],'descriptor_sha256':digest(s)}
        if op=='models.job.update':return {'before':brief(self.e.invoke.call('GET','/api/v2/models/install/'+str(p['id'])))}
        return {}
    def mutate(self,op,p,prepared,principal,job_id):
        inv=self.e.invoke
        if op=='upload.import':
            self.e.uploads.send_import(p,job_id)
            return self.verify_import(self.e.repo.get(job_id))
        if op in ('models.lora.install','models.checkpoint.install'):
            from .lora_install import execute
            return execute(self.policy,p,model_type='Checkpoint') if op=='models.checkpoint.install' else execute(self.policy,p)
        if op=='boards.create':
            b=inv.call('POST','/api/v1/boards/?'+urlencode({'board_name':p['name']}))
            if not isinstance(b,dict) or not b.get('board_id'):raise Fault('REMOTE_SHAPE','invalid created board')
            self.e.repo.update(job_id,changes={'native_receipt':{'board_id':b['board_id']}},expected={'submitting'},event='admin.native_acknowledged')
            actual=inv.call('GET','/api/v1/boards/'+part(b['board_id']))
            if actual.get('board_id')!=b['board_id'] or actual.get('board_name')!=p['name']:raise Fault('REMOTE_VERIFY','board creation readback mismatch')
            return self.board_result(actual)
        if op in ('boards.update','boards.delete'):
            path='/api/v1/boards/'+part(p['board_id'])
            if op=='boards.update':
                inv.call('PATCH',path,prepared['changes']);b=self.board(p['board_id'])
                if any(b.get(k)!=v for k,v in prepared['changes'].items()):raise Fault('REMOTE_VERIFY','board patch readback mismatch')
                return self.board_result(b)
            inv.call('DELETE',path+'?'+urlencode({'include_images':str(p['include_images']).lower()}))
            try:inv.call('GET',path)
            except Fault as e:
                if e.code=='REMOTE_HTTP_404':return {'board_id':p['board_id'],'deleted':True,'include_images':p['include_images']}
                raise
            raise Fault('REMOTE_VERIFY','deleted board is still visible')
        if op=='boards.images.update':
            removing=p['action']=='remove'
            inv.call('POST','/api/v1/board_images/batch/delete' if removing else '/api/v1/board_images/batch',{'image_names':p['image_names'],**({} if removing else {'board_id':p['board_id']})})
            for name in p['image_names']:
                im=inv.call('GET','/api/v1/images/i/'+part(name))
                if im.get('board_id')!=(None if removing else p['board_id']):raise Fault('REMOTE_VERIFY','image board readback mismatch')
            return {'image_names':p['image_names'],'board_id':None if removing else p['board_id'],'verified':True}
        if op=='models.install':
            s=self.sources[p['source_id']]
            j=brief(inv.call('POST','/api/v2/models/install?'+urlencode({'source':s['source'],'inplace':str(s.get('inplace',False)).lower()}),s.get('config',{})))
            self.e.repo.update(job_id,changes={'native_receipt':{'model_job_id':j['id']}},expected={'submitting'},event='admin.native_acknowledged')
            return {'native_job':brief(inv.call('GET','/api/v2/models/install/'+str(j['id']))),'meaning':'installation accepted; completion tracked with models.job'}
        jid=str(p['id']);action=p['action']
        inv.call('DELETE' if action=='cancel' else 'POST','/api/v2/models/install/'+jid+('' if action=='cancel' else '/'+{'retry':'restart_failed'}.get(action,action)))
        return {'native_job':brief(inv.call('GET','/api/v2/models/install/'+jid)),'meaning':'control acknowledged and native job read back; asynchronous transition may still be pending'}
