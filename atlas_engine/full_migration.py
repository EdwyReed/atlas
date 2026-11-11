"""Resumable full-fidelity disk mirror. Only source GET/scroll and local SQLite writes.
The mirror is an isolated destination/export, NOT a Qdrant atomic source snapshot.
Unknown embedding spaces remain quarantined, even if their vectors are viable.
"""
import argparse, hashlib, json, sqlite3, time, zlib
from pathlib import Path
from .migrate import Source, convert
from .repository import dump, digest

class Mirror:
    def __init__(self, path):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.path);self.path.chmod(0o600)
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS collections(name TEXT PRIMARY KEY, schema TEXT NOT NULL, expected INTEGER NOT NULL, offset TEXT, done INTEGER NOT NULL DEFAULT 0, after_schema TEXT, after_count INTEGER);
        CREATE TABLE IF NOT EXISTS points(collection TEXT NOT NULL,id TEXT NOT NULL,raw BLOB NOT NULL,sha256 TEXT NOT NULL,entity_type TEXT NOT NULL,target TEXT NOT NULL,quarantine TEXT NOT NULL,PRIMARY KEY(collection,id));
        CREATE INDEX IF NOT EXISTS point_types ON points(entity_type,collection,id);
        ''')
    def ingest(self, name, schema, points, offset, done):
        # Data and scroll cursor commit together; crash replays at most one batch.
        with self.db:
            for p in points:
                target, converted, bad=convert(name,p,schema)
                raw=dump(p).encode(); key=dump(p['id'])
                quarantine={'excluded_vectors':bad,'space':'legacy:'+name,'verified_model':False,'reason':'embedding model/revision not established; unavailable for semantic retrieval'}
                old=self.db.execute('select sha256 from points where collection=? and id=?',(name,key)).fetchone()
                h=hashlib.sha256(raw).hexdigest()
                if old and old[0]!=h:raise ValueError('source changed across resumed scan; use new destination')
                self.db.execute('insert or ignore into points values(?,?,?,?,?,?,?)',(name,key,zlib.compress(raw,1),h,converted['payload']['entity_type'],target,dump(quarantine)))
            self.db.execute('update collections set offset=?,done=? where name=?',(dump(offset),int(done),name))
            for p in points:
                row=self.db.execute('select raw,sha256 from points where collection=? and id=?',(name,dump(p['id']))).fetchone()
                raw=zlib.decompress(row[0])
                if hashlib.sha256(raw).hexdigest()!=row[1] or json.loads(raw)!=p:raise AssertionError('full point readback mismatch')
    def verify(self):
        h=hashlib.sha256(); count=0
        for name,key,raw,expected in self.db.execute('select collection,id,raw,sha256 from points order by collection,id'):
            raw=zlib.decompress(raw)
            if hashlib.sha256(raw).hexdigest()!=expected or dump(json.loads(raw)['id'])!=key:raise AssertionError('mirror integrity')
            h.update(dump([name,key,expected]).encode()+b'\n');count+=1
        return {'points_verified':count,'ordered_content_sha256':h.hexdigest()}
    def manifest(self):
        result={'destination':str(self.path.resolve()),'source_writes':False,'consistency':'online-resumable-scan; NOT atomic source snapshot','collections':{},'complete':True}
        for n,s,expected,offset,done,after,ac in self.db.execute('select * from collections'):
            count=self.db.execute('select count(*) from points where collection=?',(n,)).fetchone()[0]
            types=dict(self.db.execute('select entity_type,count(*) from points where collection=? group by entity_type',(n,)))
            complete=bool(done and count==expected==ac and s==after)
            result['collections'][n]={'expected':expected,'copied':count,'after_count':ac,'schema':json.loads(s),'schema_unchanged':s==after,'complete':complete,'entity_types':types}
            result['complete'] &= complete
        if not result['collections']:result['complete']=False
        return result
    def freeze(self, dest):
        if Path(dest).exists():raise ValueError('freeze destination must be new')
        # SQLite backup is a consistent snapshot of this local ingestion store.
        Path(dest).touch(mode=0o600,exist_ok=False)
        with sqlite3.connect(dest) as target:self.db.backup(target)
        frozen=Mirror(dest)
        try:return {'kind':'consistent-local-sqlite-backup','source_consistency':self.manifest()['consistency'],**frozen.verify()}
        finally:frozen.close()
    def close(self):self.db.close()

class FrozenSource:
    """Read a sealed local ingestion snapshot without opening it for writing.
    Snapshot consistency describes the local store, not the earlier online source.
    """
    def __init__(self,path):
        self.db=sqlite3.connect('file:'+str(Path(path).resolve())+'?mode=ro',uri=True)
        self.db.execute('BEGIN')
        # Validate ALL collections before any consumer opens/writes a destination.
        try:
            names=self.collections()
            if not names:raise ValueError('empty local snapshot')
            for name in names:self.schema(name)
            if self.db.execute('select 1 from points where collection not in (select name from collections) limit 1').fetchone():raise ValueError('orphan snapshot points')
        except BaseException:
            self.db.close();raise
    def collections(self):return [r[0] for r in self.db.execute('select name from collections order by name')]
    def schema(self,name):
        r=self.db.execute('select schema,expected,done,after_count,after_schema from collections where name=?',(name,)).fetchone()
        count=self.db.execute('select count(*) from points where collection=?',(name,)).fetchone()[0]
        if not r or r[2]!=1 or not count==r[1]==r[3] or r[4] is None or r[0]!=r[4]:raise ValueError('incomplete or schema-inconsistent local snapshot')
        return {'config':{'params':json.loads(r[0])},'points_count':count}
    def points(self,name,limit=None):
        cursor=self.db.execute('select raw from points where collection=? order by id',(name,))
        for i,(raw,) in enumerate(cursor):
            if limit and i>=limit:return
            yield json.loads(zlib.decompress(raw))
    def close(self):self.db.close()

def ingest_frozen(source_path,destination,out):
    """Full snapshot ingestion, resumable by exact point identity; no network."""
    if Path(source_path).resolve()==Path(destination).resolve():raise ValueError('destination must differ')
    source=FrozenSource(source_path);mirror=Mirror(destination)
    try:
        for n in source.collections():
            s=source.schema(n);schema=s['config']['params'];vs=schema['vectors'];vs={'unnamed':vs} if 'size' in vs else vs
            old=mirror.db.execute('select schema,expected from collections where name=?',(n,)).fetchone()
            if old and (old[0]!=dump(schema) or old[1]!=s['points_count']):raise ValueError('snapshot identity differs')
            with mirror.db:mirror.db.execute('insert or ignore into collections(name,schema,expected) values(?,?,?)',(n,dump(schema),s['points_count']))
            batch=[]
            for point in source.points(n):
                batch.append(point)
                if len(batch)==512:mirror.ingest(n,vs,batch,None,False);batch=[]
            mirror.ingest(n,vs,batch,None,True)
            with mirror.db:mirror.db.execute('update collections set after_schema=?,after_count=? where name=?',(dump(schema),s['points_count'],n))
        result=mirror.manifest();result['consistency']='consistent-local-frozen-ingestion; upstream consistency remains that of the original source export';result['verification']=mirror.verify()
        Path(out).write_text(json.dumps(result,indent=2));return result
    finally:mirror.close();source.close()

def run(url,path,out,batch=512):
    source=Source(url); mirror=Mirror(path);started=time.monotonic()
    try:
        names=source.collections()
        existing={r[0] for r in mirror.db.execute('select name from collections')}
        if existing-set(names):raise ValueError('source collection disappeared')
        for n in names:
            before=source.schema(n); schema=before['config']['params']; stored=mirror.db.execute('select * from collections where name=?',(n,)).fetchone()
            if stored and stored[1]!=dump(schema):raise ValueError('source schema changed')
            if not stored:
                with mirror.db:mirror.db.execute('insert into collections(name,schema,expected) values(?,?,?)',(n,dump(schema),before['points_count']))
                stored=mirror.db.execute('select * from collections where name=?',(n,)).fetchone()
            if stored[4]:
                # Crash may have committed the final batch before metadata readback.
                if stored[5] is None:
                    after=source.schema(n)
                    with mirror.db:mirror.db.execute('update collections set after_schema=?,after_count=? where name=?',(dump(after['config']['params']),after['points_count'],n))
                continue
            vs=schema['vectors'];vs={'unnamed':vs} if 'size' in vs else vs
            offset=json.loads(stored[3]) if stored[3] else None
            last_report=0
            while True:
                body={'limit':batch,'with_payload':True,'with_vector':True}
                if offset is not None:body['offset']=offset
                r=source.call('POST','/collections/'+n+'/points/scroll',body)
                offset=r.get('next_page_offset');mirror.ingest(n,vs,r['points'],offset,offset is None)
                if time.monotonic()-last_report>30:
                    count=mirror.db.execute('select count(*) from points where collection=?',(n,)).fetchone()[0]
                    print(dump({'collection':n,'copied':count,'elapsed_seconds':round(time.monotonic()-started,2)}),flush=True);last_report=time.monotonic()
                    Path(out).write_text(json.dumps(mirror.manifest(),indent=2))
                if offset is None:break
            after=source.schema(n)
            with mirror.db:mirror.db.execute('update collections set after_schema=?,after_count=? where name=?',(dump(after['config']['params']),after['points_count'],n))
        result=mirror.manifest();result['verification']=mirror.verify();result['elapsed_seconds']=time.monotonic()-started
        Path(out).write_text(json.dumps(result,indent=2));print(dump(result),flush=True)
        return result
    finally:mirror.close();source.client.close()

def main():
    p=argparse.ArgumentParser();p.add_argument('--source',default='http://127.0.0.1:6333');p.add_argument('--destination',required=True);p.add_argument('--manifest',required=True);p.add_argument('--batch',type=int,default=512);a=p.parse_args()
    run(a.source,a.destination,a.manifest,a.batch)
if __name__=='__main__':main()
