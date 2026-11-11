"""Read-only typed Qdrant reference adapter; no vector search in unknown spaces.
Explicit source collections; preserved ACL fields are opaque source metadata.
"""
from urllib.parse import quote
from .upstream import JsonHTTP
from .typed import validate
from .contracts import Fault

class TypedReference(JsonHTTP):
    semantic_enabled=False
    def __init__(self,base,collections,token=''):
        super().__init__(base,token,timeout=60)
        if not collections or len(set(collections))!=len(collections):
            raise ValueError('explicit unique typed reference collections required')
        if any(not isinstance(c,str) or not c.startswith('atlas_v1_') or '/' in c for c in collections):
            raise ValueError('typed namespace required')
        self.collections=tuple(collections)
    @staticmethod
    def unpack(row):
        p=validate(row['payload'])
        # IDs must remain the original identities, not regenerated migration IDs.
        if p['source'].get('id')!=row['id']:
            raise Fault('SCHEMA_SOURCE','typed source ID mismatch')
        body=p['source_payload']
        return {'id':row['id'],'payload':body,'typed_schema_version':p['schema_version'],
            'provenance':p['provenance'],'vector_provenance':p['vector_provenance']}
    def rows(self,entity_type,key=None,value=None,limit=None):
        out=[]
        for collection in self.collections:
            offset=None
            while True:
                must=[{'key':'entity_type','match':{'value':entity_type}}]
                if key is not None:must.append({'key':'source_payload.'+key,'match':{'value':value}})
                body={'filter':{'must':must},'limit':128,'with_payload':True,'with_vector':False}
                if offset is not None:body['offset']=offset
                r=self.call('POST','/collections/'+quote(collection,safe='')+'/points/scroll',body)['result']
                for row in r['points']:
                    record=self.unpack(row)
                    if record is not None:out.append(record)
                    if limit and len(out)>=limit:return out
                offset=r.get('next_page_offset')
                if offset is None:break
        return out
    def resources(self):return self.rows('resource')
    def evidence(self,kind,key):
        if kind not in ('tag','resource'):raise Fault('VALIDATION','typed reference kind must be tag/resource')
        return self.rows(kind,'tag' if kind=='tag' else 'asset_id',key,limit=10)
    def query(self,vector,limit):
        raise Fault('EMBEDDING_SPACE_UNVERIFIED','typed legacy vectors quarantined; semantic search disabled before encoding')
