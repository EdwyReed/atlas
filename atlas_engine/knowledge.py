"""Versioned domain-reference delta in the existing authoritative engine ledger.
Offline importer only: no network, inference, native writes or bank mutation.
History is immutable, head updates use exact revision/hash lineage inside one writer
transaction. Research and observations are not generation eligibility/preferences.
"""
import json,re,sqlite3,time,uuid
from pathlib import Path
from .repository import dump,digest
from .contracts import Fault
from .typed import payload
KINDS={'reference','research','character','fandom','resource','feedback'}
STATES={'verified_record','attributed_observation','attributed_interpretation','explicit_feedback','unresolved','reference_only'}
class Knowledge:
    def __init__(self,repo):
        self.repo=repo
        with repo.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS knowledge_revisions(entity_id TEXT NOT NULL,revision INTEGER NOT NULL,hash TEXT NOT NULL,record TEXT NOT NULL,PRIMARY KEY(entity_id,revision));
            CREATE TABLE IF NOT EXISTS knowledge_heads(entity_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS knowledge_imports(manifest_hash TEXT PRIMARY KEY,receipt TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS knowledge_immutable_update BEFORE UPDATE ON knowledge_revisions BEGIN SELECT RAISE(ABORT,'immutable knowledge revision');END;
            CREATE TRIGGER IF NOT EXISTS knowledge_immutable_delete BEFORE DELETE ON knowledge_revisions BEGIN SELECT RAISE(ABORT,'immutable knowledge revision');END;
            ''')
    @staticmethod
    def validate(r):
        required={'entity_id','revision','supersedes','kind','body','acl','provenance','sources','epistemic_status'}
        if set(r)!=required:raise Fault('DELTA_SCHEMA','explicit complete record contract required')
        if not isinstance(r['entity_id'],str) or not 0<len(r['entity_id'])<=512:raise Fault('DELTA_SCHEMA','literal stable entity ID required')
        if type(r['revision']) is not int or r['revision']<1:raise Fault('DELTA_SCHEMA','positive integer revision required')
        if r['supersedes'] is not None and not re.fullmatch('[0-9a-f]{64}',str(r['supersedes'])):raise Fault('DELTA_LINEAGE','supersedes must be exact prior record hash')
        if r['kind'] not in KINDS or r['epistemic_status'] not in STATES:raise Fault('DELTA_SCHEMA','unknown domain kind/status')
        if not isinstance(r['body'],dict) or not isinstance(r['provenance'],dict) or not r['provenance'].get('attribution'):raise Fault('DELTA_SCHEMA','object body and explicit attribution required')
        if not isinstance(r['acl'],dict):raise Fault('DELTA_SCHEMA','historical acl metadata must be an object; never interpreted as permissions')
        if not isinstance(r['sources'],list) or not r['sources']:raise Fault('DELTA_SOURCE','source anchors required')
        for s in r['sources']:
            if not isinstance(s,dict) or not s.get('uri') or not re.fullmatch('[0-9a-f]{64}',str(s.get('sha256',''))):raise Fault('DELTA_SOURCE','source URI and content hash required')
        dump(r)
        return r
    def ingest(self,manifest,archive):
        """archive is trusted local content-addressed storage; no remote input path."""
        if set(manifest)!={'format','records','sources','watermarks','baseline'} or manifest['format']!='atlas-domain-delta-v1':raise Fault('DELTA_SCHEMA','unsupported manifest')
        archive=Path(archive).resolve();records=manifest['records'];h=digest(manifest)
        if not isinstance(records,list) or not isinstance(manifest['sources'],dict):raise Fault('DELTA_SCHEMA','malformed manifest')
        for name,info in manifest['sources'].items():
            if not re.fullmatch('[0-9a-f]{64}',name) or set(info)!={'bytes'}:raise Fault('DELTA_SOURCE','content addressed source map required')
            p=archive/name
            if p.is_symlink() or p.resolve().parent!=archive or not p.is_file():raise Fault('DELTA_SOURCE','source missing or escaped archive')
            import hashlib
            b=p.read_bytes()
            if len(b)!=info['bytes'] or hashlib.sha256(b).hexdigest()!=name:raise Fault('DELTA_SOURCE','source changed/corrupt')
        seen=set()
        for r in records:
            self.validate(r)
            if (r['entity_id'],r['revision']) in seen:raise Fault('DELTA_CONFLICT','duplicate entity revision in manifest')
            seen.add((r['entity_id'],r['revision']))
            if any(s['sha256'] not in manifest['sources'] for s in r['sources']):raise Fault('DELTA_SOURCE','unarchived source')
        inserted=0;unchanged=0
        with self.repo.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for r in records:
                rh=digest(r);key=r['entity_id'];rev=r['revision']
                old=c.execute('select hash,record from knowledge_revisions where entity_id=? and revision=?',(key,rev)).fetchone()
                if old:
                    if old['hash']!=rh:raise Fault('DELTA_CONFLICT','same revision differs')
                    unchanged+=1;continue # Historical replay never changes a newer head.
                head=c.execute('select revision,hash from knowledge_heads where entity_id=?',(key,)).fetchone()
                if head:
                    if rev<=head['revision']:raise Fault('DELTA_STALE','older source revision cannot overwrite head')
                    if rev!=head['revision']+1 or r['supersedes']!=head['hash']:raise Fault('DELTA_LINEAGE','exact contiguous supersedes lineage required')
                    previous=json.loads(c.execute('select record from knowledge_revisions where entity_id=? and revision=?',(key,head['revision'])).fetchone()[0])
                    if previous['kind']!=r['kind']:raise Fault('DELTA_KIND','entity kind cannot change within a revision lineage')
                    # A caller cannot relabel a known obsolete body with a fresh
                    # revision number to resurrect it. Intentional historical
                    # reversions require a separate explicit reviewed path.
                    old_bodies=(json.loads(x[0])['body'] for x in c.execute('select record from knowledge_revisions where entity_id=? and revision<?',(key,head['revision'])))
                    if any(digest(b)==digest(r['body']) for b in old_bodies):raise Fault('DELTA_STALE','known superseded body cannot be relabelled as a newer source')
                elif rev!=1 or r['supersedes'] is not None:raise Fault('DELTA_LINEAGE','initial record must start revision 1 with null supersedes')
                c.execute('insert into knowledge_revisions values(?,?,?,?)',(key,rev,rh,dump(r)))
                c.execute('insert into knowledge_heads values(?,?,?) on conflict(entity_id) do update set revision=excluded.revision,hash=excluded.hash',(key,rev,rh));inserted+=1
            total=c.execute('select count(*) from knowledge_heads').fetchone()[0]
            receipt={'manifest_sha256':h,'inserted_revisions':inserted,'unchanged_revisions':unchanged,'heads':total,'source_objects_verified':len(manifest['sources']),'production_writes':False}
            c.execute('insert or ignore into knowledge_imports values(?,?)',(h,dump(receipt)))
            # Verify every original value before transaction commit; raw files stay outside readers.
            for r in records:
                stored=c.execute('select record from knowledge_revisions where entity_id=? and revision=?',(r['entity_id'],r['revision'])).fetchone()[0]
                if stored!=dump(r):raise Fault('DELTA_READBACK','exact revision readback failed')
        return receipt
    def get(self,key):
        with self.repo.connect() as c:row=c.execute('select r.record,r.hash from knowledge_heads h join knowledge_revisions r using(entity_id,revision) where h.entity_id=?',(key,)).fetchone()
        if not row:raise Fault('NOT_FOUND','domain record not found')
        r=json.loads(row['record'])
        return {'record':r,'sha256':row['hash']}
    def search(self,query,kind=None,after='',limit=20):
        items=[];cursor=after
        # Keyset scan is bounded by the stored corpus; no caller-controlled SQL wildcard.
        with self.repo.connect() as c:
            rows=c.execute('select r.record,r.hash,h.entity_id from knowledge_heads h join knowledge_revisions r using(entity_id,revision) where h.entity_id>? order by h.entity_id',(after,))
            for row in rows:
                r=json.loads(row['record']);cursor=row['entity_id']
                if kind and r['kind']!=kind:continue
                # Search effective body only, not superseded source text or historical names.
                if query.casefold() not in dump(r['body']).casefold() and query.casefold() not in r['entity_id'].casefold():continue
                items.append({'record':r,'sha256':row['hash']})
                if len(items)>=limit:return {'items':items,'next_after':cursor}
        return {'items':items,'next_after':None}
    def project(self,projection):
        """Rebuildable payload-only projection, including immutable history and heads.
        One stable point per revision; head selection stays in authoritative SQLite.
        """
        from qdrant_client.models import PointStruct
        count=0
        with self.repo.connect() as c:
            rows=c.execute('select record,hash from knowledge_revisions order by entity_id,revision').fetchall()
        for start in range(0,len(rows),64):
            batch=[]
            for row in rows[start:start+64]:
                r=json.loads(row['record']);key=str(uuid.uuid5(uuid.NAMESPACE_URL,'atlas-domain:'+r['entity_id']+':'+str(r['revision'])))
                body={'record':r,'record_sha256':row['hash'],'private':True,'acl':r['acl']}
                p=payload(r['kind'],{'system':'atlas-domain-delta','id':key,'entity_id':r['entity_id'],'revision':r['revision']},body,project=r['acl'].get('project'),provenance=r['provenance'])
                batch.append(PointStruct(id=key,vector={},payload=p))
            projection.client.upsert(projection.collection,batch,wait=True)
            actual=projection.client.retrieve(projection.collection,[p.id for p in batch],with_payload=True,with_vectors=True)
            expected={p.id:p.payload for p in batch}
            if len(actual)!=len(batch) or any(p.payload!=expected[p.id] or p.vector for p in actual):raise Fault('DELTA_READBACK','typed Qdrant revision readback mismatch')
            count+=len(batch)
        return {'collection':projection.collection,'revision_readbacks':count,'vectors':'payload-only; no fake embeddings'}
