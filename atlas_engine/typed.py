"""One payload envelope for migration and runtime projections. No inferred authors."""
from .contracts import Fault
SCHEMA_VERSION=1
TYPES={'tag','relation','resource','generation_recipe','legacy_record','idea_job','idea','feedback','host_review','batch','reference','research','character','fandom'}
def payload(entity_type,source,body,owner_principal=None,project=None,provenance=None,vector_provenance=None):
    if entity_type not in TYPES:raise Fault('SCHEMA_TYPE','unknown entity type')
    if not isinstance(source,dict) or 'id' not in source:raise Fault('SCHEMA_SOURCE','source identity required')
    if not isinstance(body,dict):raise Fault('SCHEMA_PAYLOAD','object body required')
    return {'schema_version':SCHEMA_VERSION,'entity_type':entity_type,'source':source,'source_payload':body,'owner_principal':owner_principal,'project':project,'provenance':provenance or {'authorship':'unknown'},'vector_provenance':vector_provenance or {'space':None,'verified_model':False},'acl_preserved_in_source_payload':True}
def validate(p):
    if p.get('schema_version')!=SCHEMA_VERSION or p.get('entity_type') not in TYPES:raise Fault('SCHEMA_VERSION','unsupported typed projection')
    for key in ('source','source_payload','provenance','vector_provenance'):
        if not isinstance(p.get(key),dict):raise Fault('SCHEMA_PAYLOAD','malformed '+key)
    return p
