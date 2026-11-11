"""Request-local identity lookup over historical knowledge heads.
Source bodies are retained literally; only explicit identity fields build lookup
representations. No lifecycle, content or reader metadata restricts selection.
"""
import json
from .contracts import Fault
from .repository import digest

def identity(record):
    """Explicit known domain shapes only. No recursive string/tag extraction."""
    b=record['body'];aliases={record['entity_id']};canonical=None;category=None
    if record['kind']=='character':
        obj=b.get('record',{})
        if isinstance(obj,dict):
            canonical=obj.get('danbooru');category='character'
            aliases.update(x for x in (obj.get('name'),obj.get('identity'),canonical) if isinstance(x,str) and x)
    elif record['kind']=='fandom':
        obj=b.get('catalog',{})
        if isinstance(obj,dict):
            canonical=obj.get('booru_copyright_tag');category='copyright'
            aliases.update(x for x in (obj.get('fandom_tag'),obj.get('title'),obj.get('catalog_dir'),canonical) if isinstance(x,str) and x)
    if not isinstance(canonical,str) or not canonical or canonical.strip()!=canonical:canonical=None
    return aliases,canonical,category

class CuratedVocabulary:
    """Immutable per-request overlay; no mutation of global caches or source DB."""
    def __init__(self,base,mappings,categories):
        self.base=base;self.mappings=mappings;self.categories=categories
        self.fingerprint=base.fingerprint
    def canonical(self,value):
        return self.mappings.get(value.casefold()) or self.base.canonical(value)
    def tag(self,tag):
        if tag in self.categories:
            return self.base.tag(tag) or {'tag':tag,'category':self.categories[tag],'post_count':0}
        return self.base.tag(tag)
    def adjacent(self,tag):return self.base.adjacent(tag)
    def __getattr__(self,name):return getattr(self.base,name)

class Curated:
    def __init__(self,knowledge):self.knowledge=knowledge
    def prepare(self,p,base):
        explicit=set(p.get('reference_ids',[]))
        terms=set(p.get('characters',[])+p.get('fandoms',[])+[b['character'] for b in p.get('bindings',[])])
        # Also catch literal canonical character/copyright tags supplied via tags.
        terms.update(p.get('tags',[])+p.get('locked_tags',[]))
        folded={t.casefold() for t in terms}
        selected={};maps={};categories={};matches={}
        with self.knowledge.repo.connect() as c:
            rows=c.execute('select r.record,r.hash from knowledge_heads h join knowledge_revisions r using(entity_id,revision) order by h.entity_id')
            for row in rows:
                r=json.loads(row['record'])
                aliases,canonical,category=identity(r)
                matched={t for t in folded if t in {a.casefold() for a in aliases}}
                if r['entity_id'] not in explicit and not matched:continue
                if len(selected)>=16:raise Fault('REFERENCE_LIMIT','at most 16 current domain references per creative request')
                selected[r['entity_id']]={'record':r,'sha256':row['hash']}
                if canonical:
                    categories[canonical]=category
                    for alias in matched:
                        if alias in maps and maps[alias]!=canonical:raise Fault('REFERENCE_AMBIGUOUS','literal domain identity has conflicting canonical mappings')
                        maps[alias]=canonical;matches[alias]=r['entity_id']
                    maps[canonical.casefold()]=canonical
        # Explicit keys must exist; missing data is not an access decision.
        for key in sorted(explicit-set(selected)):
            item=self.knowledge.get(key)
            selected[key]=item
        items=[selected[k] for k in sorted(selected)]
        versions={i['record']['entity_id']:i['sha256'] for i in items}
        context={'snapshot_sha256':digest(versions),'heads':versions,'items':items,
                 'canonical_mappings':dict(sorted(maps.items())),
                 'application':'literal identity lookup; attributed source bodies are separate from computed mappings'}
        return CuratedVocabulary(base,maps,categories),context
    def unchanged(self,context):
        for key,sha in context['heads'].items():
            item=self.knowledge.get(key)
            if item['sha256']!=sha:raise Fault('REFERENCE_CHANGED','reference changed since creative intent; submit a new request_id')
        return True
