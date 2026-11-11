"""Materialize every archived point with the shared typed schema, without live reads.
Vectors with unknown provenance/invalid shape go to a separate quarantine table.
"""
import argparse,hashlib,json,sqlite3,time,zlib
from pathlib import Path
from .full_migration import Mirror
from .migrate import convert
from .repository import dump
from .typed import validate

def materialize(path,out,batch=512):
    mirror=Mirror(path);c=mirror.db
    c.executescript('''CREATE TABLE IF NOT EXISTS entities(collection TEXT NOT NULL,id TEXT NOT NULL,entity_type TEXT NOT NULL,payload BLOB NOT NULL,PRIMARY KEY(collection,id));
    CREATE INDEX IF NOT EXISTS entities_type ON entities(entity_type,collection,id);
    CREATE TABLE IF NOT EXISTS vector_quarantine(collection TEXT NOT NULL,id TEXT NOT NULL,vectors BLOB NOT NULL,reasons TEXT NOT NULL,PRIMARY KEY(collection,id));
    CREATE TABLE IF NOT EXISTS typed_progress(collection TEXT PRIMARY KEY,last_id TEXT NOT NULL);
    ''')
    started=time.monotonic();verified=0
    try:
        for name,rawschema in c.execute('select name,schema from collections').fetchall():
            schema=json.loads(rawschema)['vectors'];schema={'unnamed':schema} if 'size' in schema else schema
            row=c.execute('select last_id from typed_progress where collection=?',(name,)).fetchone();after=row[0] if row else ''
            while True:
                rows=c.execute('select id,raw from points where collection=? and id>? order by id limit ?',(name,after,batch)).fetchall()
                if not rows:break
                with c:
                    for key,raw in rows:
                        point=json.loads(zlib.decompress(raw));target,typed,bad=convert(name,point,schema);p=validate(typed['payload'])
                        encoded=dump(p).encode();vectors=point.get('vector')
                        c.execute('insert or replace into entities values(?,?,?,?)',(name,key,p['entity_type'],zlib.compress(encoded,1)))
                        reasons={'unknown_space':True,'invalid_vectors':bad,'schema':schema,'semantic_queries_enabled':False}
                        c.execute('insert or replace into vector_quarantine values(?,?,?,?)',(name,key,zlib.compress(dump(vectors).encode(),1),dump(reasons)))
                        rb=c.execute('select e.payload,q.vectors from entities e join vector_quarantine q using(collection,id) where e.collection=? and e.id=?',(name,key)).fetchone()
                        if json.loads(zlib.decompress(rb[0]))!=p or json.loads(zlib.decompress(rb[1]))!=vectors:raise AssertionError('typed full readback failed')
                        verified+=1
                    after=rows[-1][0];c.execute('insert or replace into typed_progress values(?,?)',(name,after))
                if verified%102400==0:print(dump({'typed_this_run':verified,'collection':name,'seconds':round(time.monotonic()-started,2)}),flush=True)
        # Independent exhaustive pass includes previously committed resume batches.
        h=hashlib.sha256();total=0;badcounts={};types={}
        for n,key,raw,ep,vq,reasons in c.execute('select p.collection,p.id,p.raw,e.payload,q.vectors,q.reasons from points p join entities e using(collection,id) join vector_quarantine q using(collection,id) order by p.collection,p.id'):
            point=json.loads(zlib.decompress(raw));envelope=json.loads(zlib.decompress(ep));vectors=json.loads(zlib.decompress(vq))
            validate(envelope)
            if point.get('payload',{})!=envelope['source_payload'] or point.get('vector')!=vectors or point['id']!=envelope['source']['id']:raise AssertionError('typed preservation mismatch')
            h.update(dump([n,key,hashlib.sha256(ep).hexdigest(),hashlib.sha256(vq).hexdigest()]).encode()+b'\n');total+=1
            for vector in json.loads(reasons)['invalid_vectors']:badcounts[vector]=badcounts.get(vector,0)+1
            typ=envelope['entity_type'];types[typ]=types.get(typ,0)+1
        counts={t:c.execute('select count(*) from '+t).fetchone()[0] for t in ('points','entities','vector_quarantine')}
        if len(set(counts.values()))!=1 or total!=counts['points']:raise AssertionError('coverage mismatch')
        result={'coverage':counts,'every_point_readback':total,'entity_types':types,'invalid_vectors':badcounts,'all_vectors_quarantined_unknown_model_revision':True,'content_sha256':h.hexdigest(),'elapsed_seconds':time.monotonic()-started,'source_writes':False,'llm_calls':0,'complete':mirror.manifest()['complete']}
        Path(out).write_text(json.dumps(result,indent=2));print(dump(result),flush=True);return result
    finally:mirror.close()

def main():
    p=argparse.ArgumentParser();p.add_argument('--mirror',required=True);p.add_argument('--manifest',required=True);a=p.parse_args();materialize(a.mirror,a.manifest)
if __name__=='__main__':main()
