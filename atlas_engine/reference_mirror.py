"""Typed reference reader over the full isolated mirror; no semantic fallback.
Literal keys are built locally from already archived payloads, never from live scans.
"""
import argparse,json,sqlite3,time,zlib
from pathlib import Path
from .contracts import Fault
from .typed import validate
from .repository import dump

class ReferenceMirror:
    semantic_enabled=False
    def __init__(self,path):
        self.path=Path(path).resolve()
        with self.connect() as c:
            if not c.execute("select 1 from sqlite_master where name='reference_keys'").fetchone():raise Fault('INDEX_REQUIRED','build local typed reference keys before enabling reader')
    def connect(self):return sqlite3.connect('file:'+str(self.path)+'?mode=ro',uri=True)
    @staticmethod
    def unpack(row):
        n,key,raw=row;p=validate(json.loads(zlib.decompress(raw)))
        return {'id':p['source']['id'],'payload':p['source_payload'],'typed_schema_version':p['schema_version'],'provenance':p['provenance'],'vector_provenance':p['vector_provenance']}
    def resources(self):
        with self.connect() as c:
            return [self.unpack(r) for r in c.execute("select collection,id,payload from entities where entity_type='resource' order by collection,id")]
    def search_resources(self,query,limit):
        from .reference_reader import search_resources
        return search_resources(self,query,limit)
    def evidence(self,kind,key):
        with self.connect() as c:
            return [self.unpack(r) for r in c.execute('select e.collection,e.id,e.payload from reference_keys k join entities e using(collection,id) where k.kind=? and k.key=? order by e.collection,e.id limit 10',(kind,key))]
    def get(self,collection,point_id):
        with self.connect() as c:r=c.execute("select collection,id,payload from entities where entity_type in ('tag','resource') and collection=? and id=?",(collection,dump(point_id))).fetchone()
        if not r:raise Fault('NOT_FOUND','typed entity not found')
        result=self.unpack(r)
        return result
    def query(self,vector,limit):raise Fault('EMBEDDING_SPACE_UNVERIFIED','legacy vectors preserved in quarantine; no compatible verified model/revision, semantic search disabled')

def build_index(path,out):
    c=sqlite3.connect(path);started=time.monotonic();count=0;keys={};acl={};hasher=__import__('hashlib').sha256()
    c.execute('create table if not exists reference_keys(kind TEXT NOT NULL,key TEXT NOT NULL,collection TEXT NOT NULL,id TEXT NOT NULL,PRIMARY KEY(kind,key,collection,id))')
    batch=[]
    for n,key,typ,raw in c.execute("select collection,id,entity_type,payload from entities where entity_type in ('tag','resource') order by entity_type,collection,id"):
        p=validate(json.loads(zlib.decompress(raw)));body=p['source_payload']
        for k in body:keys[k]=keys.get(k,0)+1
        for k in ('acl','private','visibility','allowed_principals','allowed_users','permissions','project','project_id','owner','owner_id'):
            if k in body:acl[k]=acl.get(k,0)+1
        lookup=body.get('tag') if typ=='tag' else body.get('asset_id')
        if lookup is not None:batch.append(('tag' if typ=='tag' else 'resource',str(lookup),n,key))
        count+=1
        if len(batch)>=1024:
            with c:c.executemany('insert or ignore into reference_keys values(?,?,?,?)',batch)
            batch=[]
    with c:c.executemany('insert or ignore into reference_keys values(?,?,?,?)',batch)
    total=c.execute('select count(*) from reference_keys').fetchone()[0]
    for row in c.execute('select * from reference_keys order by kind,key,collection,id'):hasher.update(dump(row).encode()+b'\n')
    result={'reference_entities_examined':count,'literal_keys':total,'key_manifest_sha256':hasher.hexdigest(),'source_payload_key_counts':keys,'acl_marker_counts':acl,'elapsed_seconds':time.monotonic()-started,'live_reads':False,'live_writes':False}
    Path(out).write_text(json.dumps(result,indent=2));print(dump(result),flush=True);c.close();return result
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--mirror',required=True);p.add_argument('--manifest',required=True);a=p.parse_args();build_index(a.mirror,a.manifest)
