"""Literal vocabulary adapter and application orchestration for opaque math.

The corpus projection uses labels as literal identities, never as content rules.
Literal source fields remain separate from computed numeric representations.
"""
import functools, hashlib, json, re, sqlite3
from pathlib import Path
from .contracts import Fault
from . import creative_math as numeric

WARNINGS=['Scores are uncalibrated ranking utilities, not probabilities.', 'Missing edges mean unknown compatibility, not prohibition.', 'Prose is parsed as literal comma-separated tags only; unresolved text is not interpreted.', 'Wiki links/other_names are not automatically treated as active aliases or logical implications.']
@functools.lru_cache(maxsize=4)
def file_fingerprint(path,size,mtime):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()
class Vocabulary:
    def __init__(self,path):
        if path is None:
            self.path=None;self.fingerprint=None
            return
        self.path=Path(path)
        if not self.path.is_file():raise Fault('VOCABULARY_UNAVAILABLE','configure existing vocabulary path')
        st=self.path.stat();self.fingerprint=file_fingerprint(str(self.path),st.st_size,st.st_mtime_ns)
    def connect(self):
        if self.path is None:raise Fault('VOCABULARY_UNAVAILABLE','configure a vocabulary to use creative discovery')
        c=sqlite3.connect('file:'+str(self.path)+'?mode=ro',uri=True);c.row_factory=sqlite3.Row;return c
    @functools.lru_cache(maxsize=16000)
    def tag(self,t):
        with self.connect() as c:r=c.execute('select * from tags where tag=?',(t,)).fetchone()
        return dict(r) if r else None
    def canonical(self,t):
        t=t.strip().lower().replace(' ','_')
        return t if self.tag(t) else None
    @functools.lru_cache(maxsize=16000)
    def neighbors(self,t):
        with self.connect() as c:return tuple(dict(r) for r in c.execute('select r.* from related r join tags t on t.tag=r.related_tag where r.tag=? order by r.score desc,t.id limit 100',(t,)))
    @functools.lru_cache(maxsize=16000)
    def incoming(self,t):
        with self.connect() as c:return tuple(dict(r) for r in c.execute('select r.* from related r join tags t on t.tag=r.tag where r.related_tag=? order by r.score desc,t.id limit 100',(t,)))
    def adjacent(self,t):
        rows=[dict(r,direction='source_to_target',proposal=r['related_tag']) for r in self.neighbors(t)]
        for r in self.incoming(t):
            rev=dict(r,direction='target_to_source',proposal=r['tag'],original_support=r['support'])
            rev['support']=(r['pair_count'] or 0)/max(1,r['related_count'] or 1)
            rows.append(rev)
        return rows
    @functools.lru_cache(maxsize=4)
    def starts(self):
        """Observed incident identities only; no thematic/category admission rules."""
        with self.connect() as c:
            return tuple(dict(r) for r in c.execute('''
                select t.id,t.tag,t.post_count from tags t join (
                    select tag from related where pmi>0 or (support>0 and pair_count>0)
                    union
                    select related_tag from related where pmi>0 or (pair_count>0 and related_count>0)
                ) observed on observed.tag=t.tag order by t.id
            '''))
    def search(self,q,limit):
        with self.connect() as c:
            q=q.lower().replace(' ','_').replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
            return [dict(r) for r in c.execute("select * from tags where tag like ? escape '\\' order by post_count desc limit ?",('%'+q+'%',limit))]
    def coverage(self):
        with self.connect() as c:
            return dict(c.execute('select count(*) edges,count(distinct tag) anchors,count(distinct related_tag) targets from related').fetchone())

class _Graph:
    """Request-local corpus projection: identities, numbers and evidence only.

        There are no label/category admission rules in creative operations.
    """
    def __init__(self,vocabulary):
        self.v=vocabulary;self.labels={};self.ids={};self.rows={};self.evidence={}
    def node(self,label):
        if label not in self.ids:
            record=self.v.tag(label) or {}
            node=record.get('id')
            # Edge-free curated identities have request-local opaque IDs. Do not
            # hash their spelling: even their identity must survive label renaming.
            if node is None:node=-(len([n for n in self.labels if n<0])+1)
            self.ids[label]=node;self.labels[node]=label
        return self.ids[label]
    def adjacent(self,node):
        if node not in self.rows:
            rows=[]
            for r in self.v.adjacent(self.labels[node]):
                label=r['proposal'];record=self.v.tag(label)
                if not record:continue  # unresolved endpoint, not a semantic rule
                target=self.node(label);source_id=self.node(r['tag']);target_id=self.node(r['related_tag'])
                edge=numeric.Edge(source_id,target_id,r.get('support') or 0,r.get('pair_count') or 0,r.get('pmi') or 0,record.get('post_count',0))
                rows.append((target,edge))
                self.evidence[(node,target,edge)]={'anchor':self.labels[node],'source':r['tag'],'target':r['related_tag'],'type':'cooccurrence','direction':r['direction'],**{k:r.get(k) for k in ['score','support','pair_count','pmi']}}
            self.rows[node]=tuple(rows)
        return self.rows[node]

class CreativeSearch:
    def __init__(self,vocab):
        self.v=vocab
    @staticmethod
    def literals(p):
        return p['tags']+p['locked_tags']+p['characters']+p['fandoms']+([x for x in re.split(r'[,;\n]',p['prompt']) if x.strip()] if p['prompt'] else [])
    def parse(self,p):
        tags=[];unknown=[]
        for x in self.literals(p):
            t=self.v.canonical(x)
            if t and t not in tags:tags.append(t)
            elif not t and x.strip():unknown.append(x)
        # Bindings remain scoped and literal, including unresolved identities and
        # attributes. They do not create guessed corpus edges or flatten into tags.
        bindings=[]
        for b in p['bindings']:
            c=self.v.canonical(b['character']) or b['character']
            bt=[self.v.canonical(x) or x for x in b['tags']]
            existing=next((item for item in bindings if item['character']==c),None)
            if existing:existing['tags']=list(dict.fromkeys(existing['tags']+bt))
            else:bindings.append({'character':c,'tags':list(dict.fromkeys(bt)),'conflicts':[]})
            if self.v.tag(c) and c not in tags:tags.append(c)
        return tags,unknown,bindings
    def _search(self,p,graph,adjacent,original,cancel,seed):
        try:
            return numeric.explore([graph.node(t) for t in original],adjacent,seed=seed,budget=p['budget'],width=1 if p['strategy']=='weighted_neighbors' else 6,branch=1 if p['strategy']=='weighted_neighbors' else 3,one_hop=p['strategy']=='weighted_neighbors',cancel=cancel)
        except numeric.Canceled:raise Fault('CANCELED','creative work canceled')
    def _result(self,p,graph,path,original,unknown,bindings,seed):
        added=[]
        for step in path.steps:
            start=not step.ancestry and not step.evidence
            evidence=[graph.evidence[(anchor,step.node,edge)] for anchor,edge in step.evidence]
            if start:
                record=self.v.tag(graph.labels[step.node])
                evidence=[{'type':'corpus_start','node_id':step.node,'post_count':record.get('post_count',0),'vocabulary_sha256':self.v.fingerprint,'selection':'uniform seeded proposal over observed incident IDs; not cooccurrence support'}]
            added.append({'tag':graph.labels[step.node],'utility':round(step.utility,6),'ancestry':[graph.labels[n] for n in step.ancestry],'reason':'seeded corpus start; not inferred from user intent' if start else 'observed directed graph support; numerical bounded search','evidence':evidence})
        tags=original+[x['tag'] for x in added]
        warnings=list(WARNINGS)
        if path.steps and not path.steps[0].ancestry and not path.steps[0].evidence:
            warnings.append('Empty Dream uses a seeded corpus start, not inferred user intent or compatibility evidence.')
        uncovered=[t for t in original if not self.v.adjacent(t)]
        if uncovered:warnings.append('No incident cooccurrence observations for: '+', '.join(uncovered))
        if unknown:warnings.append('Unresolved literal text retained verbatim in prompt and unresolved; not interpreted.')
        if bindings:warnings.append('Character attributes remain in bindings, not flattened into the tag prompt; downstream renderer must explicitly support character binding.')
        # Canonical IDs/labels are a lookup view; prompt retains the literal input,
        # including known labels' case, spaces and resource handles.
        literals=self.literals(p)
        # Parsing is a lookup view only. Never reconstruct the original prompt
        # from split fragments: separators and whitespace are user data too.
        prompt_parts=p['tags']+p['locked_tags']+p['characters']+p['fandoms']
        if p['prompt']:prompt_parts=prompt_parts+[p['prompt']]
        prompt_parts=prompt_parts+[x['tag'] for x in added]
        return {'algorithm':'opaque-directed-math-v1','strategy':p['strategy'],'seed':seed,'tags':tags,'bindings':bindings,'unresolved':unknown,'literal_inputs':literals,'literal_bindings':p['bindings'],'literal_prompt':p['prompt'],'prompt':', '.join(prompt_parts),'diff':{'added':added,'removed':[]},'score':{'utility':round(path.utility,6),'observed_anchors':len(original)-len(uncovered),'anchors':len(original)},'conflicts':[],'warnings':warnings,'model_binding':p['model'],'explicitness':p['explicitness'],'generative_llm_calls':0,'vocabulary_sha256':self.v.fingerprint}
    def enrich(self,p,cancel=lambda:False,seed=None):
        original,unknown,bindings=self.parse(p)
        if not original:raise Fault('NO_RESOLVED_ANCHORS','Enrichment needs at least one known literal tag in prompt or tags; unresolved text is not interpreted.')
        graph=_Graph(self.v);seed=p['seed'] if seed is None else seed
        adjacent=graph.adjacent
        path=self._search(p,graph,adjacent,original,cancel,seed)
        return self._result(p,graph,path,original,unknown,bindings,seed)
    def dream(self,p,cancel=lambda:False):
        original,unknown,bindings=self.parse(p)
        graph=_Graph(self.v);paths=[]
        adjacent=graph.adjacent
        # Empty Dream is an unconditional corpus proposal, not enrich(empty).
        # Nonempty unresolved intent must never silently become a random scene.
        unanchored=not original
        if unanchored and (unknown or bindings):
            raise Fault('NO_RESOLVED_ANCHORS','No known literal anchors; unresolved intent cannot be replaced by random corpus sampling.')
        starts=self.v.starts() if unanchored else ()
        if unanchored and not starts:raise Fault('NO_CREATIVE_EVIDENCE','No observed corpus nodes available for an empty Dream.')
        labels={r['id']:r['tag'] for r in starts}
        for i in range(p['trajectories']):
            seed=(p['seed']+i*104729)%2147483648
            if cancel():raise Fault('CANCELED','dream canceled')
            if unanchored:
                root=numeric.choose_start(labels,seed)
                assert root is not None  # nonempty observed population checked above
                path=self._search({**p,'budget':p['budget']-1},graph,adjacent,[labels[root]],cancel,seed)
                path=numeric.Path(path.nodes,(numeric.Step(root,0.0,(),()),)+path.steps,path.utility)
            else:path=self._search(p,graph,adjacent,original,cancel,seed)
            if path.steps:paths.append((i,path))
        if not paths:raise Fault('NO_CREATIVE_EVIDENCE','No additions supported by the observed graph for these anchors.')
        try:selected=numeric.diverse([path for _,path in paths],p['count'],cancel=cancel)
        except numeric.Canceled:raise Fault('CANCELED','dream canceled')
        ideas=[]
        for i,path,utility in selected:
            i=paths[i][0]
            r=self._result(p,graph,path,original,unknown,bindings,(p['seed']+i*104729)%2147483648)
            r.update(requested_anchors=original,trajectory=i,theme=None,description=' / '.join(r['tags']),selection_utility=round(utility,6));ideas.append(r)
        return {'seed':p['seed'],'ideas':ideas,'trajectories':p['trajectories'],'selection':'duplicate-set collapse; MMR mean utility minus 2*maximum additions Jaccard similarity','generative_llm_calls':0,'warnings':WARNINGS+(['Empty Dream starts from uniformly sampled observed corpus identities; proposals are not inferred user intent.'] if unanchored else [])+(['Fewer distinct ideas than requested.'] if len(ideas)<p['count'] else [])}
