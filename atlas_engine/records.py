"""Legacy shared LoRA and generation metadata. No native install/delete or GPU effects.

Opaque bodies are retained verbatim as JSON. Revision CAS, tombstones, immutable
history and request idempotency commit together. Owner/project/source are
attribution only. This compatibility ledger is not the future canonical corpus.
"""
import json,time,uuid
from .contracts import Fault
from .repository import dump,digest

class Records:
    def __init__(self,repo):
        self.repo=repo
        with repo.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY,kind TEXT NOT NULL,owner TEXT NOT NULL,project TEXT,revision INTEGER NOT NULL,deleted INTEGER NOT NULL,data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS records_scope ON records(kind,owner,id);
            CREATE TABLE IF NOT EXISTS record_history(id TEXT NOT NULL,revision INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY(id,revision));
            CREATE TABLE IF NOT EXISTS record_requests(principal TEXT NOT NULL,request_id TEXT NOT NULL,hash TEXT NOT NULL,result TEXT NOT NULL,PRIMARY KEY(principal,request_id));
            CREATE TRIGGER IF NOT EXISTS record_history_no_update BEFORE UPDATE ON record_history BEGIN SELECT RAISE(ABORT,'immutable record history');END;
            CREATE TRIGGER IF NOT EXISTS record_history_no_delete BEFORE DELETE ON record_history BEGIN SELECT RAISE(ABORT,'immutable record history');END;
            ''')
    def row(self,c,key,kind):
        row=c.execute('select * from records where id=? and kind=?',(key,kind)).fetchone()
        if not row:raise Fault('NOT_FOUND','record not found')
        return row
    def call(self,op,p,principal,source):
        kind,action=('lora',op.split('.')[-1]) if op.startswith('loras.') else ('generation',op.split('.')[-1])
        with self.repo.connect() as c:
            if action=='list':
                items=[]
                for row in c.execute('select * from records where kind=? and id>? and deleted=0 order by id',(kind,p['after'])):
                    items.append(json.loads(row['data']))
                    if len(items)>p['limit']:break
                more=len(items)>p['limit'];items=items[:p['limit']]
                return {'items':items,'has_more':more,'next_after':items[-1]['id'] if more else None}
            if action in ('get','history'):
                row=self.row(c,p['record_id'],kind)
                if action=='history':
                    rows=list(c.execute('select data from record_history where id=? and revision>? order by revision limit ?', (p['record_id'],p['after_revision'],p['limit']+1)))
                    more=len(rows)>p['limit'];items=[json.loads(r[0]) for r in rows[:p['limit']]]
                    return {'items':items,'has_more':more,'next_after_revision':items[-1]['revision'] if more else None}
                return json.loads(row['data'])
            h=digest({'operation':op,'arguments':p})
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('select hash,result from record_requests where principal=? and request_id=?',(principal,p['request_id'])).fetchone()
            if old:
                if old['hash']!=h:raise Fault('IDEMPOTENCY_CONFLICT','record request_id already bound')
                result=json.loads(old['result'])
                return result
            now=time.time()
            if action=='create':
                result={'id':str(uuid.uuid4()),'kind':kind,'owner_principal':principal,'project':p['project'],'revision':1,'deleted':False,'body':p['body'],'created':now,'updated':now,'source_client':source}
            else:
                row=self.row(c,p['record_id'],kind)
                if row['revision']!=p['expected_revision']:raise Fault('REVISION_CONFLICT','record changed; fetch current revision')
                if row['deleted']:raise Fault('RECORD_DELETED','record has been deleted')
                result={**json.loads(row['data']),'revision':row['revision']+1,'updated':now,'updated_source_client':source,'updated_by':principal}
                if action=='delete':result['deleted']=True
                elif action=='update':result['body']=p['body']
                else:raise Fault('UNKNOWN_OPERATION','unknown record action')
            raw=dump(result)
            c.execute('insert into records values(?,?,?,?,?,?,?) on conflict(id) do update set revision=excluded.revision,deleted=excluded.deleted,data=excluded.data', (result['id'],kind,result['owner_principal'],result['project'],result['revision'],int(result['deleted']),raw))
            c.execute('insert into record_history values(?,?,?)',(result['id'],result['revision'],raw))
            c.execute('insert into record_requests values(?,?,?,?)',(principal,p['request_id'],h,raw))
            c.execute('insert into events(job_id,kind,data,at) values(?,?,?,?)',('record:'+result['id'],'record.'+action,dump({'revision':result['revision'],'kind':kind}),now))
            return result
