"""Read-only full dry-run and isolated side-by-side Qdrant migration.
No HTTP writes, aliases, recreate_collection or deletes exist in this module.
"""
import argparse,collections,gzip,hashlib,json,math,time
from pathlib import Path
import httpx
from .repository import dump,digest

SCHEMA_VERSION=1

def domain(payload):
    kind=payload.get('kind')
    if kind=='tag_relation':return 'relation'
    if kind=='danbooru_tag':return 'tag'
    if kind=='model_asset' or payload.get('editorial_status')=='catalog_documented':return 'resource'
    if payload.get('editorial_status') in ('accepted','pending') and (payload.get('prompt') or payload.get('positive_prompt') or payload.get('image_name') or payload.get('image_id')):return 'generation_recipe'
    return 'legacy_record' # unknown is never invented into a successful generation

def convert(collection,point,vector_schema):
    payload=point.get('payload') or {};typ=domain(payload)
    vectors=point.get('vector') or {};vectors={'unnamed':vectors} if isinstance(vectors,list) else vectors
    kept={};excluded={}
    for name,v in vectors.items():
        expected=(vector_schema.get(name) or {}).get('size')
        if isinstance(v,list) and len(v)==expected and all(isinstance(x,(int,float)) and math.isfinite(x) for x in v) and any(v):kept[name]=v
        else:excluded[name]='zero_missing_nonfinite_or_wrong_dimension'
    # Do not mingle legacy embedding spaces merely because they share dimension/name.
    suffix=hashlib.sha256(dump({'collection':collection,'schema':vector_schema}).encode()).hexdigest()[:8]
    # Unknown model/revision spaces remain physically separated quarantine targets.
    target='atlas_v1_quarantine_'+typ+'_'+suffix
    from .typed import payload as typed_payload
    envelope=typed_payload(typ,{'collection':collection,'id':point['id']},payload,provenance={'authorship':'preserved source fields only; not assigned to current human','legacy_scope':payload.get('profile') or payload.get('agent')},vector_provenance={'space':'legacy:'+collection,'schema_sha256':digest(vector_schema),'verified_model':False,'excluded':excluded,'semantic_queries_enabled':False})
    return target,{'id':point['id'],'payload':envelope,'vector':kept},excluded

class Source:
    def __init__(self,url):self.client=httpx.Client(base_url=url,timeout=60,trust_env=False,follow_redirects=False)
    def call(self,method,path,p=None):
        r=self.client.request(method,path,json=p);r.raise_for_status();return r.json()['result']
    def collections(self):return [c['name'] for c in self.call('GET','/collections')['collections']]
    def schema(self,n):return self.call('GET','/collections/'+n)
    def points(self,n,limit=None):
        offset=None;seen=0
        while True:
            body={'limit':min(256,limit-seen) if limit else 256,'with_payload':True,'with_vector':True}
            if offset is not None:body['offset']=offset
            r=self.call('POST','/collections/'+n+'/points/scroll',body)
            for p in r['points']:yield p;seen+=1
            offset=r.get('next_page_offset')
            if offset is None or (limit and seen>=limit):return

def run(source,out,limit=None,local_target=None):
    out=Path(out);out.mkdir(parents=True,exist_ok=True);manifest={'schema_version':1,'at':time.time(),'mode':'isolated_local' if local_target else 'dry_run','source_writes':False,'collections':{},'targets':{},'complete':False,'requested_full_scan':limit is None,'consistency':'online scan, not an atomic snapshot; freeze writers/snapshot required for cutover'}
    q=None
    if local_target:
        from qdrant_client import QdrantClient
        from qdrant_client.models import VectorParams,Distance,PointStruct
        local_target=Path(local_target)
        if local_target.exists() and any(local_target.iterdir()):raise ValueError('local target must be new/empty')
        q=QdrantClient(path=str(local_target))
    try:
        for n in source.collections():
            before=source.schema(n);vs=before['config']['params']['vectors']
            vs={'unnamed':vs} if 'size' in vs else vs
            counts=collections.Counter();excluded=collections.Counter();hasher=hashlib.sha256();id_hasher=hashlib.sha256();count=0;readback=0
            archive=gzip.open(out/(n+'.original.jsonl.gz'),'wt') if q else None
            try:
                for point in source.points(n,limit):
                    raw=dump(point);hasher.update((raw+'\n').encode());id_hasher.update((dump(point['id'])+'\n').encode());count+=1
                    target,p,bad=convert(n,point,vs);counts[p['payload']['entity_type']]+=1;excluded.update(bad.keys())
                    manifest['targets'].setdefault(target,{'source_collection':n,'vector_schema':vs,'points':0});manifest['targets'][target]['points']+=1
                    if q:
                        archive.write(raw+'\n')
                        if not q.collection_exists(target):q.create_collection(target,vectors_config={k:VectorParams(size=v['size'],distance=Distance(v['distance'])) for k,v in vs.items()})
                        q.upsert(target,[PointStruct(**p)],wait=True)
                        rb=q.retrieve(target,[p['id']],with_payload=True,with_vectors=True)[0]
                        if rb.id!=p['id'] or rb.payload!=p['payload']:raise AssertionError('payload/ID preservation mismatch')
                        # Cosine Qdrant normalizes usable vectors; compare direction rather than raw float bits.
                        for k,v in p['vector'].items():
                            actual=rb.vector[k];dot=sum(a*b for a,b in zip(v,actual));norm=math.sqrt(sum(x*x for x in v)*sum(x*x for x in actual))
                            if vs[k]['distance']=='Cosine' and abs(dot/norm-1)>1e-5:raise AssertionError('vector direction not preserved')
                            if vs[k]['distance']!='Cosine' and any(abs(a-b)>1e-5 for a,b in zip(v,actual)):raise AssertionError('vector changed')
                        readback+=1
                    if count%100000==0:print(f'{n}: {count} points scanned',flush=True)
            finally:
                if archive:archive.close()
            after=source.schema(n)
            manifest['collections'][n]={'expected_points':before['points_count'],'scanned':count,'entity_types':dict(counts),'excluded_vectors':dict(excluded),'source_point_sha256':hasher.hexdigest(),'source_ordered_ids_sha256':id_hasher.hexdigest(),'preservation_readbacks':readback,'schema_before':before['config']['params'],'points_after':after['points_count'],'count_verified':count==before['points_count']==after['points_count'] if not limit else None,'schema_unchanged':before['config']['params']==after['config']['params']}
            (out/'manifest.json').write_text(json.dumps(manifest,indent=2))
        manifest['complete']=limit is None and all(c['count_verified'] and c['schema_unchanged'] for c in manifest['collections'].values())
        return manifest
    finally:
        if q:q.close()
        (out/'manifest.json').write_text(json.dumps(manifest,indent=2))
def main():
    a=argparse.ArgumentParser();a.add_argument('--source',default='http://127.0.0.1:6333');a.add_argument('--out',required=True);a.add_argument('--limit-per-collection',type=int);a.add_argument('--local-target');p=a.parse_args()
    print(json.dumps(run(Source(p.source),p.out,p.limit_per_collection,p.local_target),indent=2))
if __name__=='__main__':main()
