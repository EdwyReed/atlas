"""Installed offline attachment acceptance. All generated evidence is private.

--mapping uses an existing sealed source (no live capture); omission builds a
small labelled synthetic counterexample, never evidence of real migration.
"""
import argparse
import asyncio
from collections import defaultdict
from contextlib import closing
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
import zlib

import httpx
import atlas_engine
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from atlas_engine.attachment import (import_attachment, validate, immutable, schema,
                                      CONSISTENCY, verify_files)
from atlas_engine.staged_import import table_state
from atlas_engine.repository import dump, digest, Repository


def fixture(out):
    from atlas_engine.knowledge import Knowledge
    from atlas_engine.full_migration import Mirror
    from atlas_engine.materialize import materialize
    from atlas_engine.reference_mirror import build_index
    source = out/'synthetic-source'
    source.mkdir()
    ledger = source/'ledger'
    repo = Repository(ledger)
    Knowledge(repo)
    with repo.connect() as c:
        c.execute('create table unknown_history(id TEXT,at TEXT,tombstone INTEGER,opaque BLOB,extra TEXT)')
        c.execute('insert into unknown_history values(?,?,?,?,?)', (' literal Ω ','old:timestamp',1,b'\x00\xff\x01','opaque\n\t'))
        c.execute('create table legacy_rows(source TEXT,table_name TEXT,row_number INTEGER,sha256 TEXT,data TEXT,PRIMARY KEY(source,table_name,row_number))')
        data=dump({'unknown':{'bytes_hex':'00ff'},'timestamp':123.25,'deleted':True,'evidence':['literal Ω']})
        c.execute('insert into legacy_rows values(?,?,?,?,?)',('synthetic:inert','requests',1,hashlib.sha256(data.encode()).hexdigest(),data))
        prior=None
        for rev in (1,2,3):
            r={'entity_id':'synthetic Ω','revision':rev,'supersedes':prior,'kind':'research',
               'body':{'literal':' \tΩ\n ','revision':rev,'unknown':[None,True,{'x':rev}]},
               'acl':{'historical':'not a grant'},'provenance':{'attribution':'original-author','at':'old '+str(rev)},
               'sources':[{'uri':'inert:original','sha256':'f'*64}], 'epistemic_status':'unresolved',
               'unknown_tombstone':rev==3}
            prior=digest(r)
            c.execute('insert into knowledge_revisions values(?,?,?,?)',(r['entity_id'],rev,prior,dump(r)))
        c.execute('insert into knowledge_heads values(?,?,?)',('synthetic Ω',3,prior))
        c.commit()
        c.execute('pragma wal_checkpoint(TRUNCATE)')
    c.close()
    baseline=source/'baseline.sqlite3'
    delta=source/'delta';delta.mkdir()
    def mirror(path, points):
        m=Mirror(path)
        vs={'vectors':{}}
        with m.db:
            m.db.execute('insert into collections(name,schema,expected,done,after_schema,after_count) values(?,?,?,?,?,?)',('legacy',dump(vs),len(points),1,dump(vs),len(points)))
        m.ingest('legacy',{},points,None,True);m.close()
        materialize(path,Path(str(path)+'.typed.json'))
        build_index(path,Path(str(path)+'.index.json'))
        with sqlite3.connect(path) as c:c.execute('pragma wal_checkpoint(TRUNCATE)')
        c.close()
    mirror(baseline,[{'id':1,'payload':{'kind':'model_asset','asset_id':'fixture-visible','name':'fixture-visible'},'vector':{}},
                     {'id':'1','payload':{'kind':'model_asset','asset_id':'shadow-me','name':'shadow-me'},'vector':{}},
                     {'id':2,'payload':{'kind':'model_asset','asset_id':'held-me','name':'held-me'},'vector':{}}])
    mirror(delta/'overlay.sqlite3',[{'id':'1','payload':{'opaque':['unknown',None],'deleted_hint':True},'vector':{'unknown':[0.2,-3.5]}}])
    with sqlite3.connect(delta/'observations.sqlite3') as c:
        c.execute('create table holds(collection TEXT,id TEXT,reason TEXT,PRIMARY KEY(collection,id))')
        c.execute('insert into holds values(?,?,?)',('legacy','2','missing_held'))
    c.close()
    def pin(p):return {'path':str(p),'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
    receipt={'format':'atlas-stream-delta-v1','complete':True,'baseline':str(baseline),'source_consistency':CONSISTENCY,
             'overlay_sha256':pin(delta/'overlay.sqlite3')['sha256'],'observation_sha256':pin(delta/'observations.sqlite3')['sha256']}
    (delta/'receipt.json').write_text(dump(receipt))
    attachment={'format':'atlas-checkpoint-attachment-v1','complete':True,'baseline':str(baseline),'reference_delta':str(delta),
                'reference_delta_receipt_sha256':pin(delta/'receipt.json')['sha256'],
                'knowledge_ledger':str(ledger/'registry.sqlite3'),'knowledge_ledger_sha256':pin(ledger/'registry.sqlite3')['sha256']}
    ap=source/'attachment.json';ap.write_text(dump(attachment))
    files=[ap,baseline,ledger/'registry.sqlite3',delta/'receipt.json',delta/'overlay.sqlite3',delta/'observations.sqlite3']
    return {'format':'atlas-legacy-attachment-mapping-v1','mapping':'ledger-preserve-v1','attachment':str(ap),
            'source_consistency':CONSISTENCY,'files':[pin(p) for p in files]}


def guard(mapping, log):
    pins={p['path'] for p in mapping['files']}
    def audit(event,args):
        violation=None
        if event=='socket.connect':
            import ipaddress
            target=args[1]
            if not isinstance(target,tuple) or not ipaddress.ip_address(target[0]).is_loopback:
                violation='non-loopback network'
        if event=='sqlite3.connect':
            value=str(args[0])
            for p in pins:
                if p in value and value != Path(p).as_uri()+'?mode=ro&immutable=1':
                    violation='non-immutable source sqlite open'
        if event=='open' and isinstance(args[0],str) and args[0] in pins:
            flags=args[2] or 0
            if flags & (os.O_WRONLY|os.O_RDWR|os.O_TRUNC|os.O_CREAT):violation='source write open'
        if violation:
            with log.open('a') as f:f.write(violation+'\n')
            raise RuntimeError(violation)
    sys.addaudithook(audit)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--mapping',type=Path)
    p.add_argument('--root',type=Path,help='verify a root already created by the installed CLI')
    p.add_argument('--wheel',type=Path)
    p.add_argument('--child',type=Path,help='private config; runner internal')
    a=p.parse_args()
    if a.child:
        guard(json.loads(a.mapping.read_text()),a.out/'violations.log')
        from atlas_engine.server import main as serve
        sys.argv=['atlas','--config',str(a.child)]
        return serve()
    out=a.out.absolute();out.mkdir(parents=True,mode=0o700,exist_ok=False)
    source=Path(__file__).absolute().parents[1]/'atlas_engine'
    installed=Path(atlas_engine.__file__).parent
    def modules(path):return {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in path.glob('*.py')}
    expected=modules(source)
    assert installed!=source and modules(installed)==expected
    if a.wheel:
        with zipfile.ZipFile(a.wheel) as z:
            assert {n.removeprefix('atlas_engine/'):hashlib.sha256(z.read(n)).hexdigest() for n in z.namelist() if n.startswith('atlas_engine/') and not n.endswith('/')}==expected
    mapping=json.loads(a.mapping.read_text()) if a.mapping else fixture(out)
    if not a.mapping:
        # Older fixture constructors use SQLite transaction contexts, not closing
        # contexts. Finish their GC-owned connections before sealing sidecars.
        import gc
        gc.collect()
    mp=out/'mapping.private.json';mp.write_text(dump(mapping));mp.chmod(0o600)
    guard(mapping,out/'violations.log')
    validate(mapping)
    before={p['path']:os.stat(p['path']) for p in mapping['files']}
    def sidecars():
        paths=[Path(pin['path']+suffix) for pin in mapping['files'] for suffix in ('-wal','-shm','-journal')]
        return {str(p):{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} if p.exists() else None for p in paths}
    sidecars_before=sidecars()
    ap=json.loads(Path(mapping['attachment']).read_text())
    with closing(immutable(ap['knowledge_ledger'])) as c:
        tables=table_state(c);original_schema=schema(c)
        histories=defaultdict(list)
        for key,rev,h,raw in c.execute('select * from knowledge_revisions order by entity_id,revision'):
            histories[key].append({'record':json.loads(raw),'sha256':h})
        inert=c.execute('select source,table_name,row_number,sha256,data from legacy_rows order by source,table_name,row_number limit 1').fetchone()
    root=a.root.absolute() if a.root else out/'state'
    if not a.root:import_attachment(mapping,root)
    with closing(immutable(root/'registry.sqlite3')) as c:
        initial=table_state(c)
        assert all(initial[k]==v for k,v in tables.items())
        assert all(row in schema(c) for row in original_schema)
    token=secrets.token_urlsafe(36)
    clients=out/'clients.json';clients.write_text(dump([{'token':token,'principal':'attachment-acceptance','source':'isolated-runner'}]));clients.chmod(0o600)
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    config=out/'config.json';config.write_text(dump({'root':str(root),'clients_file':str(clients),'host':'127.0.0.1','port':port}))
    endpoint=f'http://127.0.0.1:{port}'
    headers={'Authorization':'Bearer '+token}
    client=httpx.Client(headers=headers,trust_env=False,timeout=60)
    def call(name,args=None):
        r=client.post(endpoint+'/v1/ops/'+name,json=args or {});r.raise_for_status();return r.json()
    def ok(name,args=None):
        r=call(name,args);assert r['ok'],r;return r['result']
    command=[sys.executable,'-I','-B',str(Path(__file__).absolute()),'--out',str(out),'--mapping',str(mp),'--child',str(config)]
    log=(out/'daemon.log').open('wb')
    proc=None
    checks=[]
    def passed(s):checks.append(s);print('PASS',s,flush=True)
    def start():
        proc=subprocess.Popen(command,cwd=out,stdout=log,stderr=log)
        try:
            for _ in range(1200):
                assert proc.poll() is None,'daemon exit; inspect private daemon.log'
                try:
                    status=ok('status')
                    assert status['restore_reconciliation_required'] and not status['worker_alive']
                    return proc
                except httpx.HTTPError:time.sleep(.1)
            raise AssertionError('startup timeout')
        except BaseException:
            if proc.poll() is None:proc.terminate();proc.wait(timeout=30)
            raise
    raw_requests=[]
    for view,path in [('baseline',ap['baseline']),('overlay',str(Path(ap['reference_delta'])/'overlay.sqlite3'))]:
        with closing(immutable(path)) as c:
            row=c.execute('select collection,id,raw,sha256 from points order by collection,id limit 1').fetchone()
        if row:raw_requests.append(({'view':view,'collection':row[0],'id_json':row[1]},json.loads(zlib.decompress(row[2])),row[3]))
    try:
        proc=start()
        passed('installed source/wheel/module equality; quarantined daemon; no workers')
        assert ok('attachment.status')['mapping']==mapping
        # Every retained revision through real HTTP, including all non-head bodies.
        for key,expected_history in histories.items():
            got=[];after=0
            while True:
                page=ok('knowledge.history',{'entity_id':key,'after_revision':after,'limit':100})
                got.extend(page['items'])
                if not page['has_more']:break
                after=page['next_after_revision']
            assert got==expected_history
        passed('every retained knowledge revision exact over HTTP')
        for request,point,h in raw_requests:
            got=ok('attachment.get',request);assert got['point']==point and got['sha256']==h and not got['canonical']
            first=ok('attachment.page',{'view':request['view'],'collection':request['collection'],'limit':1})
            assert first['items'][0]==got
        passed('baseline/overlay raw point and page exact; vectors/unknown fields preserved')
        legacy_args={'source':inert[0],'table_name':inert[1],'limit':1} if inert else None
        if legacy_args:
            assert ok('legacy.rows.page',legacy_args)['items'][0]==dict(zip(('row_number','sha256','data'),inert[2:]))
        key=next(k for k,v in histories.items() if len(v)>1)
        async def mcp():
            async with streamable_http_client(endpoint+'/mcp/',http_client=httpx.AsyncClient(headers=headers,trust_env=False,timeout=60)) as (read,write,_):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    for name,args in [('knowledge_history',{'entity_id':key,'limit':100}),('attachment_get',raw_requests[0][0]),('attachment_status',{})]+([('legacy_rows_page',legacy_args)] if legacy_args else []):
                        response=await session.call_tool(name,args)
                        assert not response.isError
                        operation={'knowledge_history':'knowledge.history','attachment_get':'attachment.get','attachment_status':'attachment.status','legacy_rows_page':'legacy.rows.page'}[name]
                        assert response.structuredContent==call(operation,args)
        asyncio.run(mcp())
        passed('real SDK MCP exact history/raw/status/inert-row readback')
        # Historical typed views remain separate from canonical entities.
        query='model'
        literal=ok('search',{'query':query,'mode':'literal','limit':10})
        assert literal['corpus']['items']==[]
        if a.mapping:assert literal['resources'],'retained literal lookup returned no real resource'
        assert call('search',{'query':query,'mode':'semantic'})['error']['code']=='EMBEDDING_SPACE_UNVERIFIED'
        if not a.mapping:
            assert not ok('search',{'query':'shadow-me'})['resources']
            assert not ok('search',{'query':'held-me'})['resources']
            assert len(ok('search',{'query':'fixture-visible'})['resources'])==1
        passed('sealed typed literal view; empty canonical subset; semantic refusal')
        assert call('dream.submit',{'request_id':'no-work'})['error']['code']=='RESTORE_RECONCILIATION_REQUIRED'
        refusal=call('corpus.put',{'collection':'tags','identity':{'tag':'no-write'},'request_id':'no-write','expected_revision':0,'body':{},'attribution':{}})
        assert refusal['error']['code'] in ('RESTORE_RECONCILIATION_REQUIRED','LEGACY_UNBOUND'),refusal
        saved=ok('knowledge.history',{'entity_id':key,'limit':100})
        proc.terminate();assert proc.wait(timeout=30) in (0,-15);proc=None
        proc=start()
        assert ok('knowledge.history',{'entity_id':key,'limit':100})==saved
        for request,point,h in raw_requests:assert ok('attachment.get',request)['point']==point
        assert ok('search',{'query':query,'mode':'literal','limit':10})==literal
        passed('restart/repeated reads exact; no effects/recovery; mutations refused')
    finally:
        if proc is not None and proc.poll() is None:proc.terminate();proc.wait(timeout=30)
        client.close();log.close()
    with closing(immutable(root/'registry.sqlite3')) as c:
        final=table_state(c)
        assert all(final[k]==v for k,v in tables.items())
        assert all(row in schema(c) for row in original_schema)
    from atlas_engine.state_backup import reconcile, backup
    for fn,args in [(reconcile,(root,out/'no-report')),(backup,(root,out/'not-portable'))]:
        try:fn(*args)
        except ValueError:pass
        else:raise AssertionError('partial activation/self-contained backup must refuse')
    if not a.mapping:
        # Failure after bytes arrive must remain durably unreadable.
        broken=out/'interrupted'
        def interrupt(stage):
            if stage=='ledger':raise RuntimeError('synthetic interruption')
        try:import_attachment(mapping,broken,interrupt)
        except RuntimeError:pass
        from atlas_engine.state_backup import restore_state
        try:restore_state(broken)
        except ValueError:pass
        else:raise AssertionError('incomplete import admitted')
        changed=json.loads(dump(mapping));changed['files'][0]['sha256']='0'*64
        try:import_attachment(changed,out/'changed-refused')
        except ValueError:pass
        else:raise AssertionError('changed source accepted')
        assert not (out/'changed-refused').exists()
        try:import_attachment(mapping,root)
        except ValueError:pass
        else:raise AssertionError('existing destination accepted')
    verify_files(mapping)
    unchanged=all(os.stat(p).st_mtime_ns==s.st_mtime_ns and os.stat(p).st_ino==s.st_ino and os.stat(p).st_size==s.st_size for p,s in before.items())
    sidecars_after=sidecars()
    (out/'source-check.private.json').write_text(json.dumps({'primary_metadata_unchanged':unchanged,'sidecars_before':sidecars_before,'sidecars_after':sidecars_after},indent=2))
    assert unchanged and sidecars_after==sidecars_before and not (out/'violations.log').exists()
    passed('full original ledger tables/schema retained after restart; source byte pins unchanged; quarantine activation/backup refused')
    result={'kind':'real-retained-source' if a.mapping else 'synthetic-counterexample-only',
            'root':str(root),'checks':checks,'source_unchanged':True,'source_consistency':CONSISTENCY,
            'knowledge_revisions':sum(len(v) for v in histories.values()),'knowledge_entities':len(histories),
            'source_tables':tables,'module_sha256':expected,'portable_self_contained':False,
            'source_sidecars_before_after':sidecars_before,
            'full_fidelity_migration_accepted':False}
    (out/'result.private.json').write_text(json.dumps(result,indent=2))


if __name__=='__main__':main()
