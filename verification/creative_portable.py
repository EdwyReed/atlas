"""Installed standalone populated creative acceptance, no live data/backends.
Run target Python -I -B with --wheel and a NEW private --out outside source.
Optional --vocabulary is copied from an operator-supplied CLOSED immutable SQLite
snapshot; --anchor selects a literal observed node. Never pass a live database.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
import zipfile

import atlas_engine
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(path):
    labels = ['observatory', 'brass_lens', 'star_chart', 'roof', 'spiral_stair',
              'archive', 'ink', 'lamp', 'copper', 'rain', 'glass', 'reflection']
    labels += [f'instrument_{i:03}' for i in range(148)]
    with sqlite3.connect(path) as c:
        c.executescript('''CREATE TABLE tags(id INTEGER PRIMARY KEY,tag TEXT UNIQUE,
            category TEXT,post_count INTEGER);
            CREATE TABLE related(tag TEXT,related_tag TEXT,score REAL,support REAL,
            pair_count INTEGER,pmi REAL,related_count INTEGER);
            CREATE INDEX outgoing ON related(tag); CREATE INDEX incoming ON related(related_tag);''')
        c.executemany('insert into tags values(?,?,?,?)',
                      [(i+1, s, 'opaque_'+str(i%4), 200+i*7) for i,s in enumerate(labels)])
        edges = []
        for i, label in enumerate(labels):
            group = labels[:12] if i < 12 else labels[12:]
            offset = i if i < 12 else i-12
            for delta in range(1, 4 if i < 12 else 31):
                target = group[(offset+delta)%len(group)]
                j = labels.index(target)
                edges.append((label, target, 1/delta, .6/delta, 90//delta, 2/delta, 200+j*7))
        c.executemany('insert into related values(?,?,?,?,?,?,?)', edges)
    return labels


def numeric_checks(out, vocab_path, anchor):
    from atlas_engine.creative import CreativeSearch, Vocabulary
    from atlas_engine.contracts import Creative, Dream, Fault
    from atlas_engine import creative_math as math
    # A renamed/category-scrambled copy with exactly the same opaque IDs/numbers.
    renamed = out/'renamed.sqlite'
    with sqlite3.connect(vocab_path) as src, sqlite3.connect(renamed) as dest:
        src.backup(dest)
    with sqlite3.connect(renamed) as c:
        mapping = dict(c.execute("select tag,'renamed_'||id from tags"))
        c.execute("update related set tag=(select 'renamed_'||id from tags where tags.tag=related.tag),related_tag=(select 'renamed_'||id from tags where tags.tag=related.related_tag)")
        c.execute("update tags set tag='renamed_'||id,category='arbitrary_changed'")
    v, w = Vocabulary(vocab_path), Vocabulary(renamed)
    def shape(result, vocabulary):
        ideas = result.get('ideas', [result])
        return [[(vocabulary.tag(a['tag'])['id'], a['utility'],
                  [vocabulary.tag(x)['id'] for x in a['ancestry']])
                 for a in idea['diff']['added']] for idea in ideas]
    checks = 0
    for strategy in ('beam', 'weighted_neighbors'):
        for seed in (0, 17):
            p = Creative(tags=[anchor], seed=seed, budget=3, strategy=strategy).model_dump()
            q = {**p, 'tags':[mapping[anchor]]}
            a = CreativeSearch(v).enrich(p)
            assert a == CreativeSearch(v).enrich(p)
            assert shape(a,v) == shape(CreativeSearch(w).enrich(q),w)
            dp = Dream(**p, request_id='metamorphic', trajectories=4, count=2).model_dump()
            assert shape(CreativeSearch(v).dream(dp),v) == shape(CreativeSearch(w).dream({**dp,'tags':q['tags']}),w)
            checks += 2
    empty = Dream(request_id='empty',seed=17,budget=3,trajectories=4,count=2).model_dump()
    assert shape(CreativeSearch(v).dream(empty),v) == shape(CreativeSearch(w).dream(empty),w)
    ticks = 0
    def cancel():
        nonlocal ticks
        ticks += 1
        return ticks >= 40
    started = time.monotonic()
    try:
        CreativeSearch(v).dream(Dream(tags=[anchor],request_id='cancel',budget=24,trajectories=64).model_dump(),cancel)
    except Fault as e:
        assert e.code == 'CANCELED'
    else:
        raise AssertionError('numeric work did not cancel')
    assert ticks == 40 and time.monotonic()-started < 5
    # Duplicate observations cannot multiply numerical support.
    edge = math.Edge(1,2,.4,80,2,200)
    base = math.explore([1],lambda n: [(2,edge)] if n==1 else [],budget=1)
    assert base == math.explore([1],lambda n: [(2,edge)]*2 if n==1 else [],budget=1)
    return {'rename_category_comparisons':checks+1,'active_numeric_cancel_checks':ticks,
            'duplicate_edge_invariant':True}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--wheel', type=Path)
    p.add_argument('--vocabulary', type=Path)
    p.add_argument('--anchor', default='observatory')
    p.add_argument('--child', action='store_true')
    p.add_argument('--config', type=Path)
    args = p.parse_args()
    if args.child:
        sys.path.insert(0,str(Path(__file__).absolute().parent))
        from acceptance import child
        return child(args.config,args.out/'forbidden-egress.log')
    out = args.out.absolute()
    out.mkdir(mode=0o700,parents=True,exist_ok=False)
    source = Path(__file__).absolute().parents[1]
    installed = Path(atlas_engine.__file__).parent
    modules = {f.name:sha(f) for f in (source/'atlas_engine').glob('*.py')}
    actual = {f.name:sha(f) for f in installed.glob('*.py')}
    assert installed != source/'atlas_engine' and actual == modules
    if args.wheel:
        with zipfile.ZipFile(args.wheel) as z:
            payload = {n.removeprefix('atlas_engine/'):hashlib.sha256(z.read(n)).hexdigest()
                       for n in z.namelist() if n.startswith('atlas_engine/') and not n.endswith('/')}
        assert payload == modules
    report = {'version':atlas_engine.__version__,'installed':str(installed),'modules':modules,
              'wheel_sha256':sha(args.wheel) if args.wheel else None,'checks':[], 'calls':[]}
    vocab = out/'vocabulary.sqlite'
    if args.vocabulary:
        # Operator promises the source is sealed; copy bytes before opening SQLite.
        import shutil
        before = sha(args.vocabulary)
        shutil.copyfile(args.vocabulary,vocab)
        assert sha(vocab) == before == sha(args.vocabulary)
        report['data'] = {'kind':'operator-supplied immutable vocabulary copy',
                          'source':str(args.vocabulary),'sha256':before}
    else:
        fixture(vocab)
        with sqlite3.connect(vocab) as c:
            counts = {'tags':c.execute('select count(*) from tags').fetchone()[0],
                      'directed_edges':c.execute('select count(*) from related').fetchone()[0]}
        report['data'] = {'kind':'synthetic populated graph; not real Danbooru vocabulary',
                          'sha256':sha(vocab),**counts}
    report['numeric'] = numeric_checks(out,vocab,args.anchor)
    # Existing offline importer prepares synthetic historical knowledge before ownership.
    state = out/'state'
    state.mkdir()
    (state/'.isolated-migration-destination').touch()
    archive = out/'archive'
    archive.mkdir()
    raw = b'synthetic observatory identity reference, owned acceptance data'
    h = hashlib.sha256(raw).hexdigest()
    (archive/h).write_bytes(raw)
    record = {'entity_id':'synthetic:observatory','revision':1,'supersedes':None,
              'kind':'character','body':{'record':{'name':'The Observer','danbooru':args.anchor},'literal':'  Ω  '},
              'acl':{},'provenance':{'attribution':'synthetic acceptance'},
              'sources':[{'uri':'urn:atlas:synthetic:observatory','sha256':h}],
              'epistemic_status':'attributed_observation'}
    manifest = {'format':'atlas-domain-delta-v1','records':[record],
                'sources':{h:{'bytes':len(raw)}},'watermarks':{},'baseline':{}}
    (out/'delta.json').write_text(json.dumps(manifest))
    subprocess.run([sys.executable,'-I','-m','atlas_engine.delta_import','--root',str(state),
                    '--manifest',str(out/'delta.json'),'--archive',str(archive),
                    '--receipt',str(out/'knowledge-import.json')],check=True,capture_output=True)
    token = secrets.token_urlsafe(36)
    (out/'clients.json').write_text(json.dumps([{'token':token,'principal':'creative-check','source':'synthetic'}]))
    (out/'clients.json').chmod(0o600)
    with socket.socket() as s:
        s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    endpoint = f'http://127.0.0.1:{port}'
    config = out/'atlas.json'
    config.write_text(json.dumps({'root':'state','vocabulary':'vocabulary.sqlite',
                                  'clients_file':'clients.json','port':port}))
    headers = {'Authorization':'Bearer '+token}
    home = out/'empty-home';home.mkdir()
    env = {k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME','VIRTUAL_ENV')}
    env.update(HOME=str(home),HF_HUB_OFFLINE='1')
    cmd = [sys.executable,'-I','-B',str(Path(__file__).absolute()),'--child','--out',str(out),'--config',str(config)]
    log = (out/'daemon.log').open('wb')
    process = None
    def op(name, arguments=None):
        r = httpx.post(endpoint+'/v1/ops/'+name,json=arguments or {},headers=headers,trust_env=False,timeout=30)
        r.raise_for_status()
        value = r.json()
        report['calls'].append({'operation':name,'arguments':arguments or {},'response':value})
        return value
    def ok(name,arguments=None):
        v = op(name,arguments);assert v['ok'],v
        return v['result']
    def passed(name):
        report['checks'].append(name);print('PASS',name,flush=True)
    def start():
        nonlocal process
        process = subprocess.Popen(cmd,cwd=out,env=env,stdout=log,stderr=log)
        for _ in range(200):
            assert process.poll() is None,'daemon exited'
            try:
                if ok('status')['worker_alive']:return
            except httpx.HTTPError:pass
            time.sleep(.1)
        raise AssertionError('startup timeout')
    def stop(kill=False):
        nonlocal process
        if process and process.poll() is None:
            process.kill() if kill else process.terminate()
            process.wait(timeout=30)
        process = None
    def wait(jid,state=None):
        deadline = time.monotonic()+120
        while time.monotonic()<deadline:
            job = ok('dream.get',{'job_id':jid})
            if state and job['state']==state:return job
            if job['state'] in ('failed','succeeded','canceled'):return job
            time.sleep(.02)
        raise AssertionError('job timeout')
    def validate(idea):
        assert idea['diff']['added'] and idea['generative_llm_calls']==0
        with sqlite3.connect(vocab) as c:
            for added in idea['diff']['added']:
                assert added['evidence']
                for e in added['evidence']:
                    if e['type']=='corpus_start':continue
                    row=c.execute('select pair_count from related where tag=? and related_tag=?',(e['source'],e['target'])).fetchone()
                    assert row and row[0]==e['pair_count']
                    assert e['anchor'] in added['ancestry']
        assert idea['vocabulary_sha256']==report['data']['sha256']
    async def mcp_replay(payload,job,feedback):
        async with streamable_http_client(endpoint+'/mcp/',http_client=httpx.AsyncClient(headers=headers,trust_env=False)) as (r,w,_):
            async with ClientSession(r,w) as session:
                await session.initialize()
                assert 'dream_submit' in {t.name for t in (await session.list_tools()).tools}
                for name,arguments,expected in [('dream_submit',payload,job),('feedback_record',feedback,ok('feedback.list',{'job_id':job['id']})[0])]:
                    reply=await session.call_tool(name,arguments)
                    assert not reply.isError and reply.structuredContent['result']==expected
    try:
        start()
        assert httpx.get(endpoint+'/v1/schema',trust_env=False).status_code==401
        assert ok('capabilities')['feedback_policy']['automatic_ranking_effect'] is False
        schema=httpx.get(endpoint+'/v1/schema',headers=headers,trust_env=False).json()
        assert {'dream.submit','enrich','feedback.record','search'} <= schema.keys()
        body={'literal':args.anchor+' archive Ω','opaque':[None,{'x':'  literal  '}]}
        ref={'collection':'tags','identity':{'tag':args.anchor+' archive Ω'}}
        put={**ref,'request_id':'canonical-1','expected_revision':0,'body':{**body,'superseded':'obsolete_head_only'},'attribution':{'source':'synthetic'}}
        assert ok('corpus.put',put)['state']=='succeeded'
        put2={**put,'request_id':'canonical-2','expected_revision':1,'body':{**body,'edition':2}}
        assert ok('corpus.put',put2)['state']=='succeeded'
        search=ok('search',{'query':args.anchor})
        assert search['tags'] and search['knowledge']['items'] and search['corpus']['items']
        assert search['partial'] and search['sources']['resources']['error']['code']=='REFERENCE_UNCONFIGURED'
        assert search['corpus']['items'][0]['body']==put2['body']
        assert ok('search',{'query':'obsolete_head_only'})['corpus']['items']==[]
        other={'collection':'tags','identity':{'tag':args.anchor+' second'},'request_id':'second-head',
               'expected_revision':0,'body':{'literal':'temporary synthetic head'}}
        assert ok('corpus.put',other)['state']=='succeeded'
        assert ok('search',{'query':args.anchor,'limit':1})['corpus']['complete'] is False
        assert ok('corpus.delete',{'collection':'tags','identity':other['identity'],
                                   'expected_revision':1,'request_id':'second-delete'})['state']=='succeeded'
        assert ok('search',{'query':args.anchor})['corpus']['items']==search['corpus']['items']
        assert ok('knowledge.get',{'entity_id':record['entity_id']})['record']==record
        assert ok('knowledge.search',{'query':'The Observer'})['items'][0]['record']==record
        passed('installed authenticated literal search across populated vocabulary, knowledge and canonical heads without reference backend')
        unknown='  Unknown Ω / exact  '
        args0={'tags':[args.anchor],'prompt':f'  {args.anchor};\n{unknown}  ',
               'locked_tags':[unknown],'characters':['The Observer'],
               'bindings':[{'character':'Unknown character Ω','tags':[' exact attribute ']}],
               'seed':17,'budget':3}
        enriched=ok('enrich',args0);validate(enriched)
        assert enriched['literal_prompt']==args0['prompt'] and unknown in enriched['unresolved']
        assert args0['prompt'] in enriched['prompt'] and enriched['literal_bindings']==args0['bindings']
        assert enriched['reference_context']['items'][0]['record']==record
        for name,arguments in [('enrich',{'prompt':unknown}),('dream.submit',{'prompt':unknown,'request_id':'unknown-only'})]:
            reply=op(name,arguments)
            if name=='dream.submit' and reply['ok']:
                assert wait(reply['result']['id'])['error']['code']=='NO_RESOLVED_ANCHORS'
            else:assert reply['error']['code']=='NO_RESOLVED_ANCHORS'
        payload={**args0,'request_id':'dream-main','count':2,'trajectories':4}
        job=wait(ok('dream.submit',payload)['id']);assert job['state']=='succeeded',job
        for idea in job['result']['ideas']:validate(idea)
        assert ok('dream.submit',payload)==job
        assert op('dream.submit',{**payload,'seed':18})['error']['code']=='IDEMPOTENCY_CONFLICT'
        feedback={'request_id':'reaction-1','target_id':job['id'],'reaction':'reject','note':'  keep literal note Ω; callers revise explicitly  '}
        fb=ok('feedback.record',feedback)
        assert ok('feedback.record',feedback)==fb
        assert op('feedback.record',{**feedback,'reaction':'keep'})['error']['code']=='IDEMPOTENCY_CONFLICT'
        assert op('feedback.record',{**feedback,'request_id':'bad-target','target_id':'missing'})['error']['code']=='NOT_FOUND'
        assert ok('enrich',args0)==enriched
        next_job=wait(ok('dream.submit',{**payload,'request_id':'after-feedback'})['id'])
        assert next_job['result']==job['result']
        asyncio.run(mcp_replay(payload,job,feedback))
        events=ok('jobs.events',{'job_id':job['id']})
        passed('literal input, unknowns, graph evidence, seeded output; durable feedback annotation-only policy and real SDK MCP replay')
        empty=wait(ok('dream.submit',{'request_id':'empty','budget':2,'trajectories':4,'count':2})['id'])
        assert empty['state']=='succeeded'
        for idea in empty['result']['ideas']:validate(idea)
        if not args.vocabulary:
            heavy={'tags':['instrument_000'],'budget':24,'trajectories':64,'count':2}
            cancel=ok('dream.submit',{**heavy,'request_id':'cancel-active'})
            running=wait(cancel['id'],'running');assert running['state']=='running',running
            t=time.monotonic()
            assert ok('dream.cancel',{'job_id':cancel['id']})['state']=='canceled'
            assert wait(cancel['id'])['state']=='canceled' and time.monotonic()-t<5
            interrupted=ok('dream.submit',{**heavy,'budget':6,'trajectories':8,'request_id':'restart-running'})
            assert wait(interrupted['id'],'running')['state']=='running'
            stop(kill=True);start()
            recovered=wait(interrupted['id']);assert recovered['state']=='succeeded',recovered
            passed('actual running CPU cancel and SIGKILL/restart numerical replay')
        before=ok('corpus.history',ref)
        stop();start()
        assert ok('dream.get',{'job_id':job['id']})==job
        assert ok('jobs.events',{'job_id':job['id']})==events
        assert ok('dream.submit',payload)==job
        assert ok('feedback.list',{'job_id':job['id']})==[fb]
        assert ok('corpus.history',ref)==before and len(before['items'])==2
        assert ok('knowledge.get',{'entity_id':record['entity_id']})['record']==record
        after_search=ok('search',{'query':args.anchor})
        assert after_search['corpus']['items']==search['corpus']['items']
        assert after_search['knowledge']==search['knowledge'] and after_search['tags']==search['tags']
        assert sha(vocab)==report['data']['sha256']
        assert not (out/'forbidden-egress.log').exists()
        passed('restart preserves exact inputs/results, feedback, knowledge, canonical history and unchanged vocabulary bytes; no non-loopback egress')
        if not args.vocabulary:
            # Change ONLY the private working vocabulary while its owner is dead.
            pending=ok('dream.submit',{**heavy,'request_id':'changed-vocabulary'})
            assert wait(pending['id'],'running')['state']=='running'
            stop(kill=True)
            original=vocab.read_bytes()
            with sqlite3.connect(vocab) as c:
                c.execute("update tags set category='changed_fixture' where id=1")
            start()
            failed=wait(pending['id'])
            assert failed['state']=='failed' and failed['error']['code']=='VOCABULARY_CHANGED',failed
            assert ok('dream.submit',payload)==job
            stop();vocab.write_bytes(original);start()
            assert sha(vocab)==report['data']['sha256']
            assert ok('dream.get',{'job_id':pending['id']})==failed
            passed('changed private vocabulary refuses interrupted work; completed replay never recomputes or loses history')
        report['passed']=True
    finally:
        stop();log.close()
        (out/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':
    main()
