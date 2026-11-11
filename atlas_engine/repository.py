"""Durable authoritative state; no per-profile partitions or external queue."""
import hashlib, json, sqlite3, time, uuid
from pathlib import Path
from .contracts import Fault

def dump(x): return json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
def digest(x): return hashlib.sha256(dump(x).encode()).hexdigest()
def job_digest(kind,payload):
    # New absent/None routing fields are semantically identical to v1 absence.
    normalized={k:v for k,v in payload.items() if not ((k in ('project','board_id') and v is None) or (k=='reference_ids' and v==[]) or (k=='upscale_engine' and v=='spandrel'))}
    return digest({'kind':kind,'payload':normalized})
class Repository:
    def __init__(self,root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.path=self.root/'registry.sqlite3'
        if self.path.exists():
            with sqlite3.connect('file:'+str(self.path.resolve())+'?mode=ro',uri=True) as existing:
                tables={r[0] for r in existing.execute("select name from sqlite_master where type='table'")}
                if tables & {'requests','mcp_jobs'}:raise Fault('LIVE_REGISTRY_REFUSED','legacy registry cannot be used as a candidate root; use isolated root and reviewed importer')
        with self.connect() as c:
            c.executescript('''PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS engine_meta(key TEXT PRIMARY KEY,value TEXT);
            INSERT OR IGNORE INTO engine_meta VALUES('schema_version','1');
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,principal TEXT NOT NULL,request_id TEXT NOT NULL,kind TEXT NOT NULL,hash TEXT NOT NULL,state TEXT NOT NULL,data TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,UNIQUE(principal,request_id));
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT NOT NULL,kind TEXT NOT NULL,data TEXT NOT NULL,at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS observations(native_id INTEGER PRIMARY KEY,job_id TEXT NOT NULL,item TEXT NOT NULL,at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS feedback(id TEXT PRIMARY KEY,principal TEXT NOT NULL,request_id TEXT NOT NULL,target TEXT NOT NULL,hash TEXT NOT NULL,data TEXT NOT NULL,at REAL NOT NULL,UNIQUE(principal,request_id));
            CREATE TABLE IF NOT EXISTS health(key TEXT PRIMARY KEY,data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reviews(principal TEXT NOT NULL,request_id TEXT NOT NULL,job_id TEXT NOT NULL,hash TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(principal,request_id));
            CREATE INDEX IF NOT EXISTS reviews_job ON reviews(job_id);
            CREATE INDEX IF NOT EXISTS jobs_active ON jobs(kind,state,id);
            CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created DESC,id);
            CREATE INDEX IF NOT EXISTS jobs_owner ON jobs(principal,id);
            CREATE INDEX IF NOT EXISTS observations_job ON observations(job_id);
            CREATE INDEX IF NOT EXISTS events_job_seq ON events(job_id,seq);
            CREATE TABLE IF NOT EXISTS native_images(name TEXT NOT NULL,job_id TEXT NOT NULL,PRIMARY KEY(name,job_id));
            CREATE TABLE IF NOT EXISTS legacy_namespaces(namespace TEXT PRIMARY KEY,source TEXT UNIQUE NOT NULL,principal TEXT NOT NULL,project TEXT,manifest_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS legacy_reservations(namespace TEXT NOT NULL,principal TEXT NOT NULL,idem TEXT NOT NULL,request_id TEXT NOT NULL,input_hash TEXT NOT NULL,legacy_status TEXT NOT NULL,PRIMARY KEY(namespace,idem));
            CREATE INDEX IF NOT EXISTS legacy_keys ON legacy_reservations(principal,idem);
            CREATE INDEX IF NOT EXISTS legacy_ids ON legacy_reservations(principal,request_id);
            ''')
            if c.execute("select value from engine_meta where key='schema_version'").fetchone()[0]!='1':raise Fault('SCHEMA_VERSION','unsupported registry schema')
            if not c.execute("select 1 from engine_meta where key='image_links_v1'").fetchone():
                c.execute("insert or ignore into native_images select o.value,jobs.id from jobs,json_each(jobs.data,'$.outputs') o")
                c.execute("insert into engine_meta values('image_links_v1','1')")
    def connect(self):
        c=sqlite3.connect(self.path,timeout=30);c.row_factory=sqlite3.Row;c.execute('PRAGMA synchronous=FULL');return c
    def reserve(self,principal,source,request_id,kind,payload,extra=None):
        h=job_digest(kind,payload); now=time.time()
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            from .legacy_compat import guard
            guard(c,principal,request_id)
            row=c.execute('select * from jobs where principal=? and request_id=?',(principal,request_id)).fetchone()
            if row:
                if row['hash']!=h and (row['kind']!=kind or job_digest(row['kind'],json.loads(row['data'])['payload'])!=h):raise Fault('IDEMPOTENCY_CONFLICT','request_id already bound to another payload')
                return self.decode(row),False
            if kind=='dream' and c.execute("select count(*) from jobs where kind='dream' and state in ('queued','running')").fetchone()[0]>=16:raise Fault('CAPACITY','bounded dream queue is full; wait or cancel an existing job')
            jid=str(uuid.uuid4());d={'payload':payload,'source_client':source,'render_state':'not_submitted','artifact_state':'not_fetched','index_state':'not_indexed','embedding_state':'disabled',**(extra or {})}
            c.execute('insert into jobs values(?,?,?,?,?,?,?,?,?)',(jid,principal,request_id,kind,h,'queued',dump(d),now,now))
            c.execute('insert into events(job_id,kind,data,at) values(?,?,?,?)',(jid,'intent.created',dump({'kind':kind}),now))
            return self.decode(c.execute('select * from jobs where id=?',(jid,)).fetchone()),True
    def materialize_batch(self,jid):
        """Commit every child intent and group linkage before any native I/O.
        Also repairs interrupted older groups using deterministic child request IDs.
        """
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('select * from jobs where id=?',(jid,)).fetchone()
            if not row or row['kind']!='batch':raise Fault('KIND_MISMATCH','not a batch')
            group=self.decode(row);ids=[];now=time.time()
            for child in group['prepared_children']:
                cp=child['payload'];h=job_digest('generation',cp)
                from .legacy_compat import guard
                guard(c,row['principal'],cp['request_id'])
                old=c.execute('select * from jobs where principal=? and request_id=?',(row['principal'],cp['request_id'])).fetchone()
                if old:
                    j=self.decode(old)
                    if old['kind']!='generation' or job_digest('generation',j['payload'])!=h or j.get('batch_job_id')!=jid:raise Fault('IDEMPOTENCY_CONFLICT','batch child belongs to another intent')
                    ids.append(j['id']);continue
                cid=str(uuid.uuid4());ids.append(cid)
                data={'payload':cp,'source_client':group['source_client'],'render_state':'not_submitted','artifact_state':'not_fetched','index_state':'not_indexed','embedding_state':'disabled','plan':child['plan'],'native_ids':[],'batch_job_id':jid}
                c.execute('insert into jobs values(?,?,?,?,?,?,?,?,?)',(cid,row['principal'],cp['request_id'],'generation',h,'queued',dump(data),now,now))
                c.execute('insert into events(job_id,kind,data,at) values(?,?,?,?)',(cid,'intent.created',dump({'kind':'generation','batch_job_id':jid}),now))
            if group.get('children')==ids:return [self.decode(c.execute('select * from jobs where id=?',(cid,)).fetchone()) for cid in ids]
            data=json.loads(row['data']);data['children']=ids
            c.execute('update jobs set data=?,updated=? where id=?',(dump(data),now,jid))
            c.execute('insert into events(job_id,kind,data,at) values(?,?,?,?)',(jid,'batch.children_recorded',dump({'children':ids}),now))
            return [self.decode(c.execute('select * from jobs where id=?',(cid,)).fetchone()) for cid in ids]
    @staticmethod
    def decode(row):
        if not row:return None
        return {**{k:row[k] for k in ['id','kind','state','created','updated']},**json.loads(row['data']),'owner_principal':row['principal']}
    def count(self):
        with self.connect() as c:return c.execute('select count(*) from jobs').fetchone()[0]
    def get(self,jid):
        with self.connect() as c:r=c.execute('select * from jobs where id=?',(jid,)).fetchone()
        if not r:raise Fault('NOT_FOUND','job not found')
        return self.decode(r)
    def list(self,limit=1000,kind=None):
        with self.connect() as c:
            return [self.decode(r) for r in c.execute('select * from jobs '+('where kind=? ' if kind else '')+'order by created desc limit ?',((kind,limit) if kind else (limit,)))]
    def page(self,kind=None,states=None,after='',limit=128):
        """Stable keyset traversal; no historical-tail truncation."""
        where=['id>?'];args=[after]
        if kind:where.append('kind=?');args.append(kind)
        if states:
            where.append('state in ('+','.join('?' for _ in states)+')');args.extend(sorted(states))
        with self.connect() as c:
            return [self.decode(r) for r in c.execute('select * from jobs where '+' and '.join(where)+' order by id limit ?',(*args,limit))]
    def iter_jobs(self,kind=None,states=None,batch=128):
        after=''
        while True:
            rows=self.page(kind,states,after,batch)
            if not rows:return
            for row in rows:yield row
            after=rows[-1]['id']
    def update(self,jid,state=None,changes=None,expected=None,event='state.updated'):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');r=c.execute('select * from jobs where id=?',(jid,)).fetchone()
            if not r:raise Fault('NOT_FOUND','job not found')
            if expected and r['state'] not in expected:return self.decode(r),False
            d=json.loads(r['data']);d.update(changes or {});s=state or r['state'];now=time.time()
            c.execute('update jobs set state=?,data=?,updated=? where id=?',(s,dump(d),now,jid))
            if changes and 'outputs' in changes:
                c.execute('delete from native_images where job_id=?',(jid,))
                c.executemany('insert or ignore into native_images values(?,?)',[(name,jid) for name in d['outputs']])
            c.execute('insert into events(job_id,kind,data,at) values(?,?,?,?)',(jid,event,dump({'state':s,'keys':sorted((changes or {}).keys())}),now))
            return self.decode(c.execute('select * from jobs where id=?',(jid,)).fetchone()),True
    def acknowledge_submission(self,jid,ids,batch_id,receipt=None):
        """Attach ACK identity without overwriting newer observer/control state.

        The decision and write share a SQLite writer transaction, including across
        Repository instances. ACK is identity evidence, not a native status event.
        """
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            r=c.execute('select * from jobs where id=?',(jid,)).fetchone()
            if not r:raise Fault('NOT_FOUND','job not found')
            d=json.loads(r['data']);state=r['state']
            if receipt is not None:d['native_receipt']=receipt
            d['native_ids']=sorted(set(d.get('native_ids',[])) | set(ids))
            if batch_id is not None:d['native_batch_id']=batch_id
            # Only an unobserved submission advances on ACK. Preserve observing,
            # terminal and control states, their render_state, outputs and metadata.
            if state in ('submitting','ack_unknown'):
                state='observing';d['render_state']='pending'
            now=time.time()
            c.execute('update jobs set state=?,data=?,updated=? where id=?',(state,dump(d),now,jid))
            c.execute('insert into events(job_id,kind,data,at) values(?,?,?,?)',
                      (jid,'submission.acknowledged',dump({'state':state,'native_ids':d['native_ids']}),now))
            return self.decode(c.execute('select * from jobs where id=?',(jid,)).fetchone())
    def native(self,item,jid):
        with self.connect() as c:
            c.execute('insert into observations values(?,?,?,?) on conflict(native_id) do update set job_id=excluded.job_id,item=excluded.item,at=excluded.at',(item['item_id'],jid,dump(item),time.time()))
    def observed_job(self,nid):
        with self.connect() as c:r=c.execute('select job_id from observations where native_id=?',(nid,)).fetchone()
        return r[0] if r else None
    def record_feedback(self,principal,source,p):
        self.get(p['target_id']);h=digest(p)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');r=c.execute('select * from feedback where principal=? and request_id=?',(principal,p['request_id'])).fetchone()
            if r:
                if r['hash']!=h:raise Fault('IDEMPOTENCY_CONFLICT','feedback request_id already used')
                return json.loads(r['data'])
            d={'id':str(uuid.uuid4()),**p,'author_principal':principal,'source_client':source}
            c.execute('insert into feedback values(?,?,?,?,?,?,?)',(d['id'],principal,p['request_id'],p['target_id'],h,dump(d),time.time()))
            return d
    def feedback(self,jid):
        self.get(jid)
        with self.connect() as c:return [json.loads(r[0]) for r in c.execute('select data from feedback where target=? order by at',(jid,))]
    def record_review(self,principal,source,jid,p):
        h=digest(p)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');r=c.execute('select hash,data from reviews where principal=? and request_id=?',(principal,p['request_id'])).fetchone()
            if r:
                if r['hash']!=h:raise Fault('IDEMPOTENCY_CONFLICT','review request already bound')
                return json.loads(r['data'])
            d={**p,'id':str(uuid.uuid4()),'job_id':jid,'reviewer_principal':principal,'source_client':source,'provenance':'host-reported-decision; model identity is host assertion, not engine inference'}
            c.execute('insert into reviews values(?,?,?,?,?)',(principal,p['request_id'],jid,h,dump(d)));return d
    def reviews(self,jid):
        with self.connect() as c:return [json.loads(r[0]) for r in c.execute('select data from reviews where job_id=? order by request_id',(jid,))]
    def event(self,jid,kind,data):
        with self.connect() as c:c.execute('insert into events(job_id,kind,data,at) values(?,?,?,?)',(jid,kind,dump(data),time.time()))
    def request(self,principal,request_id,kind,payload):
        with self.connect() as c:
            c.execute('BEGIN')
            from .legacy_compat import guard
            guard(c,principal,request_id)
            r=c.execute('select * from jobs where principal=? and request_id=?',(principal,request_id)).fetchone()
        if r and r['hash']!=job_digest(kind,payload) and (r['kind']!=kind or job_digest(r['kind'],json.loads(r['data'])['payload'])!=job_digest(kind,payload)):raise Fault('IDEMPOTENCY_CONFLICT','request_id already bound to another payload')
        return self.decode(r)
    def health(self,key,data=None):
        with self.connect() as c:
            if data is not None:c.execute('insert or replace into health values(?,?)',(key,dump(data)));return data
            r=c.execute('select data from health where key=?',(key,)).fetchone();return json.loads(r[0]) if r else None
