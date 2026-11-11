"""Typed Qdrant writer/reader, usable with local or explicitly configured HTTP client.
Registry is authoritative; no zero vectors, legacy collection writes or aliases.
"""
import threading,uuid
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct
from .typed import payload,validate
from .contracts import Fault
class Projection:
    collection='atlas_v1_creative_records'
    def __init__(self,path=None,*,client=None,collection=None):
        if (path is None)==(client is None):raise ValueError('provide path or explicit client')
        self.client=client if client is not None else QdrantClient(path=str(path))
        self.collection=collection or self.collection
        if not self.collection.startswith('atlas_v1_'):raise ValueError('refuse legacy collection namespace')
        self.lock=threading.Lock()
        if not self.client.collection_exists(self.collection):self.client.create_collection(self.collection,vectors_config={})
        info=self.client.get_collection(self.collection)
        if info.config.params.vectors!={}:raise ValueError('runtime projection requires payload-only collection; vectors need separately reviewed space')
    @classmethod
    def http(cls,url,token='',collection=None):
        from urllib.parse import urlsplit
        parsed=urlsplit(url)
        if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost') or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):raise ValueError('explicit loopback Qdrant URL required')
        return cls(client=QdrantClient(url=url,api_key=token or None,timeout=30,check_compatibility=False),collection=collection)
    def project(self,job,feedback,reviews=None):
        owner=job.get('owner_principal');project=job['payload'].get('project')
        source={'system':'atlas-engine','id':job['id'],'client':job.get('source_client')}
        prov={'authorship':job.get('author','server-authenticated-principal' if owner!='native-observer' else 'unknown'),'observation':job.get('provenance','engine_intent'),'not_a_human_preference':True}
        body={k:job.get(k) for k in ('kind','state','payload','result','native_ids','outputs','render_state','artifact_state','artifacts','embedding_state','batch_job_id','children')}
        typ='idea_job' if job['kind']=='dream' else 'batch' if job['kind']=='batch' else 'generation_recipe'
        points=[PointStruct(id=job['id'],vector={},payload=payload(typ,source,body,owner,project,prov))]
        for idea in (job.get('result') or {}).get('ideas',[]):
            aid=str(uuid.uuid5(uuid.NAMESPACE_URL,'atlas-idea:'+job['id']+':'+str(idea['trajectory'])))
            points.append(PointStruct(id=aid,vector={},payload=payload('idea',{**source,'id':aid},idea,owner,project,prov)))
        for f in feedback:
            points.append(PointStruct(id=f['id'],vector={},payload=payload('feedback',{'system':'atlas-engine','id':f['id'],'target_job_id':job['id']},f,owner,project,{'authorship':f['author_principal'],'meaning':'explicit recorded reaction only; no popularity inference'})))
        for r in reviews or []:
            points.append(PointStruct(id=r['id'],vector={},payload=payload('host_review',{'system':'atlas-engine','id':r['id'],'target_job_id':job['id']},r,owner,project,{'authorship':r['reviewer_principal'],'meaning':'host-reported visual assessment, not a preference'})))
        with self.lock:
            for p in points:validate(p.payload)
            self.client.upsert(self.collection,points,wait=True)
            rb=self.client.retrieve(self.collection,[p.id for p in points],with_payload=True,with_vectors=True)
            expected={p.id:p.payload for p in points}
            if len(rb)!=len(points) or any(p.payload!=expected[p.id] or p.vector for p in rb):raise ValueError('projection readback failed')
        return {'collection':self.collection,'points_readback':len(rb),'schema_version':1,'vectors':'none; payload-only until reviewed embedding provider enabled'}
    def get(self,point_id):
        rows=self.client.retrieve(self.collection,[point_id],with_payload=True,with_vectors=False)
        if not rows:raise Fault('NOT_FOUND','projection point not found')
        return validate(rows[0].payload)
    def page(self,offset=None,limit=100):
        rows,next_offset=self.client.scroll(self.collection,offset=offset,limit=limit,with_payload=True)
        return {'items':[{'id':r.id,'payload':validate(r.payload)} for r in rows],'next_offset':next_offset}
    def close(self):self.client.close()
