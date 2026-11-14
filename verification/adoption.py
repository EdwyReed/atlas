"""Installed adoption acceptance using a synthetic root made by actual dev20.

Source schema input is metadata only. No production rows/files are imported.
Private paths are CLI inputs; no credentials or host bindings belong in Git.
"""
import argparse
import asyncio
from contextlib import closing
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
from unittest.mock import patch


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def make_source(root, schema_file):
    # This branch runs with the ORIGINAL installed dev20, not successor imports.
    def audit(event, args):
        if event == 'socket.connect':
            raise AssertionError('source fixture must not use network')
    sys.addaudithook(audit)
    from atlas_engine.engine import Engine
    from atlas_engine.repository import dump, digest
    from atlas_engine.corpus import COLLECTIONS
    from atlas_engine.contracts import SCHEMAS
    from atlas_engine.legacy_compat import input_hash
    root.mkdir(mode=0o700); (root/'service.lock').touch()
    with sqlite3.connect(root/'registry.sqlite3') as db:
        metadata = json.loads(schema_file.read_text())
        for typ in ('table', 'index', 'trigger', 'view'):
            for t, name, _, sql in metadata:
                if t == typ and sql and name != 'sqlite_sequence':
                    db.execute(sql)
    class NoBackend:
        def __getattr__(self, key):
            raise AssertionError('old fixture touched backend: ' + key)
    vocab=root.parent/'fixture-vocabulary.sqlite3'
    with sqlite3.connect(vocab) as db:
        db.execute('create table tags(id integer primary key,tag text,post_count integer)')
        db.execute("insert into tags values(1,'猫',1)")
    engine = Engine(root, vocab, NoBackend(), NoBackend(), True, corpus_root=root/'corpus')
    identities = {'tags':{'tag':'猫'}, 'loras':{'system':'fixture','model_id':'1','version_id':'1'},
                  'checkpoints':{'system':'fixture','model_id':2,'version_id':2},
                  'generations':{'system':'fixture','image_id':'3'},
                  'tag_relations':{'endpoints':[{'tag':'猫','polarity':'positive'},{'tag':'bad','polarity':'negative'}]}}
    for name in COLLECTIONS:
        for revision in (1, 2):
            engine.corpus.append(name, identities[name], {'opaque':[None, revision, 'é']},
                                 {'source':'synthetic-dev20'}, None, {'principal':'fixture'}, [name,revision])
        if name == 'tags':
            engine.corpus.append(name, identities[name], {}, {}, None, {}, ['delete'], action='delete')
    payload = SCHEMAS['generation.submit'].model_validate(
        {'request_id':'original-intent','model':'original-model','prompt':'猫'}).model_dump()
    jobs = []
    for i in range(7):
        kind = 'generation' if i < 3 else 'native_admin'
        data = payload if i == 0 else {**payload, 'request_id':'intent-'+str(i)}
        j, _ = engine.repo.reserve('fixture', 'old-native', data['request_id'], kind, data)
        state = 'observing' if i < 3 else 'ack_unknown'
        engine.repo.update(j['id'], state, {'native_ids':[i+10] if i < 3 else [],
                           'native_batch_id':'original-batch', 'unknown_extension':{'keep':True}})
        jobs.append(engine.repo.get(j['id']))
    artifact = b'\x00\xffexact artifact\n'; art = root/'artifacts/blob'; art.write_bytes(artifact)
    import hashlib
    h = hashlib.sha256(artifact).hexdigest()
    record = {'entity_id':'fixture-knowledge','revision':1,'supersedes':None,'kind':'reference',
              'body':{'text':'historical 猫'},'acl':{'source_scope':'retain exactly'},
              'provenance':{'attribution':'synthetic-original'},'sources':[{'uri':'fixture:source','sha256':h}],
              'epistemic_status':'reference_only'}
    raw = {'id':'legacy-id','idem':'legacy-key','input_hash':input_hash({'猫':1}), 'status':'ack_unknown',
           'data':dump({'request_id':'legacy-id','idempotency_key':'legacy-key','input':{'猫':1}})}
    with engine.repo.connect() as db:
        db.execute('insert into observations values(?,?,?,?)',(10,jobs[0]['id'],dump({'item_id':10,'status':'pending'}),1))
        db.execute('insert into feedback values(?,?,?,?,?,?,?)',('f','fixture','feedback-key',jobs[0]['id'],'h','{}',1))
        db.execute('insert into health values(?,?)',('fixture','{}'))
        db.execute('insert into reviews values(?,?,?,?,?)',('fixture','review-key',jobs[0]['id'],'h','{}'))
        db.execute('insert into native_images values(?,?)',('fixture.png',jobs[0]['id']))
        db.execute('insert into legacy_namespaces values(?,?,?,?,?,?)'.replace('?,?,?,?,?,?','?,?,?,?,?'),('original','source','fixture',None,'manifest'))
        db.execute('insert into legacy_reservations values(?,?,?,?,?,?)',('original','fixture','legacy-key','legacy-id',input_hash({'猫':1}),'ack_unknown'))
        db.execute('insert into legacy_rows values(?,?,?,?,?)',('source','requests',1,hashlib.sha256(dump(raw).encode()).hexdigest(),dump(raw)))
        db.execute('insert into knowledge_revisions values(?,?,?,?)',('fixture-knowledge',1,digest(record),dump(record)))
        db.execute('insert into knowledge_heads values(?,?,?)',('fixture-knowledge',1,digest(record)))
        db.execute('insert into knowledge_imports values(?,?)',('manifest','{}'))
        db.execute('insert into artifacts values(?,?,?,?,?)',('a',jobs[0]['id'],'fixture.png',str(art),dump({'sha256':h,'size':len(artifact),'artifact_id':'a'})))
        db.execute('insert into uploads values(?,?,?,?,?,?,?,?,?,?,?)',('u','fixture','upload-key','h',None,5,'hash','image','ready',b'\x00\xffabc','{}'))
        db.execute('insert into records values(?,?,?,?,?,?,?)',('record','generation','fixture',None,1,0,'{}'))
        db.execute('insert into record_history values(?,?,?)',('record',1,'{}'))
        db.execute('insert into record_requests values(?,?,?,?)',('fixture','record-key','h','{}'))
    media=root/'media';media.mkdir(exist_ok=True);(media/'prior.npy').write_bytes(b'original media prior bytes')
    cas=root/'corpus/files/sha256'/h[:2];cas.mkdir(parents=True,exist_ok=True);(cas/h).write_bytes(artifact)
    engine.close()
    (root.parent/'fixture.json').write_text(dump({'jobs':jobs,'payload':payload,'artifact_sha256':h}))
    # A complete external dependency fixture is deliberately separate from the
    # canonical root. Attachment-owner is exercised without ever reseeding it.
    external=root.parent/'external';external.mkdir();(external/'dependency').write_bytes(b'reference only')
    attachment=external/'attachment.json'
    attachment.write_text(dump({'complete':True,'baseline':str(external/'dependency')}))
    (root/'attachment-owner.json').write_text(dump({'format':'atlas-attachment-runtime-v1',
        'attachment':{'path':str(attachment),'sha256':hashlib.sha256(attachment.read_bytes()).hexdigest()},'code_sha256':'original-code-pin'}))


def serve(config, attempts):
    def audit(event, args):
        if event == 'socket.connect':
            with attempts.open('a') as f: f.write('attempted forbidden backend connection\n')
            raise AssertionError('adopted server must perform no outbound I/O')
    sys.addaudithook(audit)
    from atlas_engine.server import main
    sys.argv=['atlas','--config',str(config)]
    main()


async def proxy_child(path, token, endpoint):
    # Exact old client body; only its hardcoded transport address is rebound.
    module=load(path,'actual_old_proxy')
    original=module.streamable_http_client
    module.streamable_http_client=lambda _url, **kw: original(endpoint+'/mcp/', **kw)
    await module.main(str(token))


def run(a):
    import hashlib
    import zipfile
    import atlas_engine
    from atlas_engine import adoption as ad
    from atlas_engine import state_backup as sb
    from atlas_engine.engine import Engine
    from atlas_engine.config import Config, bind_connections
    from atlas_engine.connections import install
    out=a.out.absolute();out.mkdir(mode=0o700)
    checks=[]
    def passed(name):
        checks.append(name); print('PASS',name,flush=True)
    def refuse(fn):
        try: fn()
        except (ValueError, OSError): return
        raise AssertionError('refusal expected')
    installed=Path(atlas_engine.__file__).parent
    hashes=lambda root:{p.name:sb.sha(p) for p in root.glob('*.py')}
    expected=hashes(a.source_package)
    assert hashes(installed)==expected and installed != a.source_package
    with zipfile.ZipFile(a.wheel) as wheel:
        members={n.split('/')[1]:hashlib.sha256(wheel.read(n)).hexdigest() for n in wheel.namelist() if n.startswith('atlas_engine/')}
        assert members==expected
    passed('complete source/wheel/installed file-set and byte identity')
    root=out/'original'
    subprocess.run([str(a.source_python),'-I','-B',str(Path(__file__).absolute()),'--make-source',str(root),'--source-schema',str(a.source_schema)],check=True,timeout=60)
    info=json.loads((out/'fixture.json').read_text())
    passed('closed populated fixture built by installed dev20 using actual production schema only')
    binding={'id':'original-native','backend_generation':'retained-dev20','account_scope':'original-unscoped',
             'url':'http://127.0.0.1:9','token_file':None,'enable_writes':False}
    plan=out/'plan.json'
    ad.plan(root,a.source_python,plan,[out/'external/dependency'],out/'external/attachment.json',binding)
    # Exercise the installed CLI's mandatory operator checksum before any copy.
    cli = [sys.executable, '-I', '-B', '-m', 'atlas_engine.adoption',
           'capture', str(plan), str(out/'bad-pin')]
    for suffix, message in (([], '--plan-sha256'),
                            (['--plan-sha256', '0'*64], 'checksum mismatch')):
        r = subprocess.run(cli + suffix, capture_output=True, text=True, timeout=30)
        assert r.returncode != 0 and message in r.stderr and not (out/'bad-pin').exists()
    passed('capture CLI missing or wrong approved plan SHA256 refuses before copy')
    # Committed WAL held open across capture (fixture writer is idle at fence).
    writer=sqlite3.connect(root/'registry.sqlite3');writer.execute('pragma journal_mode=WAL')
    writer.execute('pragma wal_autocheckpoint=0');writer.execute("insert into health values('committed-wal','{}')");writer.commit()
    original=ad.source_inventory(root)
    for lockpath in (root/'service.lock',root/'corpus/owner.lock'):
        with lockpath.open() as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            refuse(lambda:ad.capture(plan,out/'busy'))
    assert not (out/'busy').exists()
    passed('both existing owner locks refuse capture before destination creation')
    def interrupt(name):
        if name=='registry.sqlite3': raise OSError('injected after registry copy')
    refuse(lambda:ad.capture(plan,out/'interrupted',after_copy=interrupt))
    class NoBackend:
        def __getattr__(self,name):raise AssertionError('backend touched: '+name)
    refuse(lambda:Engine(out/'interrupted',None,NoBackend(),NoBackend(),True,corpus_root=out/'interrupted/corpus'))
    assert ad.source_inventory(root)==original
    passed('interrupted capture is durably incomplete; Engine refuses before recovery; source unchanged')
    target=out/'adopted';receipt=ad.capture(plan,target)
    verified=ad.verify(target)
    assert verified['verified'] and verified['quarantined']
    assert ad.source_inventory(root)==original
    assert 'committed-wal' in [r[0] for r in ad.connection(target/'registry.sqlite3').execute('select key from health')]
    writer.close()
    passed('one-copy WAL-aware capture: all tables/schema/rows, raw collections, CAS/artifacts/media exact before Engine open')
    r = subprocess.run([sys.executable, '-I', '-B', '-m', 'atlas_engine.state_backup',
                        'backup', str(target), str(out/'unsafe-backup')],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode != 0 and 'pinned external dependencies' in r.stderr
    assert not (out/'unsafe-backup').exists() and ad.verify(target)['verified']
    passed('ordinary adopted-root backup refuses dependency loss before destination creation')
    refuse(lambda:ad.capture(plan,target))
    dependency=out/'external/dependency';saved=dependency.read_bytes();dependency.write_bytes(saved+b'drift')
    refuse(lambda:ad.capture(plan,out/'dependency-drift'));dependency.write_bytes(saved)
    with sqlite3.connect(root/'registry.sqlite3') as db:db.execute('create table unreviewed(value blob)')
    refuse(lambda:ad.capture(plan,out/'schema-drift'))
    passed('existing destination, dependency drift and source schema drift refused')
    # Exact original binding is mandatory; no inference from current default.
    token=out/'token';token.write_text('fixture-token-'+'x'*40);token.chmod(0o600)
    clients=out/'clients.json';clients.write_text(json.dumps([{'principal':'fixture','source':'old-native','token':token.read_text()}]));clients.chmod(0o600)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
    cfg=out/'server.json';cfg.write_text(json.dumps({'root':str(target),'clients_file':str(clients),'port':port,'connections':[binding]}))
    config=Config.load(cfg)
    wrong=Config.load(cfg);wrong.connections[0].backend_generation='wrong'
    refuse(lambda:bind_connections(target,wrong.connections))
    passed('unscoped dev20 native history explicitly bound; changed backend generation refused')
    baseline=receipt['registry']['tables']
    # Real installed standalone daemon, with an outbound socket tripwire.
    endpoint='http://127.0.0.1:'+str(port)
    from atlas_engine.client import call
    plugin=load(a.legacy_plugin,'actual_native_plugin')
    client_cfg={'endpoint':endpoint,'token_file':str(token)}
    attempts=out/'outbound-attempts.log'
    def request(op,args):
        result=plugin.call(op,args,client_cfg)
        assert result['ok'],(op,result)
        return result['result']
    for restart in range(2):
        log=(out/('server-'+str(restart)+'.log')).open('w')
        proc=subprocess.Popen([sys.executable,'-I','-B',str(Path(__file__).absolute()),'--serve',str(cfg),'--attempts',str(attempts)],stdout=log,stderr=log)
        try:
            for _ in range(150):
                if proc.poll() is not None:raise AssertionError('server exited: '+str(out/('server-'+str(restart)+'.log')))
                r=call('status',{},client_cfg)
                if r.get('ok'):break
                time.sleep(.1)
            else:raise AssertionError('startup timeout')
            assert not request('status',{})['worker_alive']
            assert request('capabilities',{})['restore_reconciliation_required']
            jobs=request('jobs.list',{'limit':20});assert len(jobs)==7
            assert {j['id']:j['state'] for j in jobs}=={j['id']:j['state'] for j in info['jobs']}
            assert request('knowledge.get',{'entity_id':'fixture-knowledge'})['record']['body']['text']=='historical 猫'
            assert len(request('knowledge.history',{'entity_id':'fixture-knowledge'})['items'])==1
            assert request('generation.submit',info['payload'])['id']==info['jobs'][0]['id']
            changed={**info['payload'],'steps':21}
            assert plugin.call('generation.submit',changed,client_cfg)['error']['code']=='IDEMPOTENCY_CONFLICT'
            assert request('legacy.request',{'namespace':'original','idempotency_key':'legacy-key','input':{'猫':1}})
            assert plugin.call('generation.cancel',{'job_id':info['jobs'][0]['id']},client_cfg)['error']['code']=='RESTORE_RECONCILIATION_REQUIRED'
            artifact=request('artifact.read',{'artifact_id':'a','size':64})
            assert artifact['sha256']==info['artifact_sha256']
            assert request('upload.get',{'upload_id':'u'})
            assert request('corpus.page',{'collection':'tags'})
            assert request('corpus.files.read',{'sha256':info['artifact_sha256'],'size':64})
            asyncio.run(check_mcp(a,token,endpoint,out))
        finally:
            proc.terminate();proc.wait(timeout=50);log.close()
        assert proc.returncode in (0, -15), proc.returncode
        with closing(ad.connection(target/'registry.sqlite3',closed=True)) as db:
            current=ad.ledger(db)['tables']
        assert {k:current[k] for k in baseline}==baseline
        assert not attempts.exists(), 'outbound I/O attempted'
    passed('two installed daemon starts: original plugin HTTP, old stdio proxy/MCP, history, files and exact replay; no workers/outbound I/O or old-table mutation')
    report={'target_root':str(target),'backup_manifest_sha256':sb.restore_state(target)['backup_manifest_sha256'],
            'registry_logical_sha256':sb.registry_digest(target),'external_effects_reconciled':True,
            **{k:'fixture attestation cannot resolve unknown effects' for k in ('generation','workspace','media','acquisition','projection','uploads')}}
    report_file=out/'reconcile.json';report_file.write_text(json.dumps(report))
    refuse(lambda:sb.reconcile(target,report_file))
    passed('operator attestation cannot activate seven unresolved native effects')
    result={'checks':checks,'checks_passed':len(checks),'production_capture':False,
            'old_versions':receipt['plan']['source_versions'],'target_versions':sb.versions(),
            'tables':baseline,'fixture_source_data':'synthetic; production schema metadata only',
            'legacy_proxy_sha256':sb.sha(a.legacy_proxy),'legacy_plugin_sha256':sb.sha(a.legacy_plugin)}
    (out/'result.json').write_text(json.dumps(result,indent=2))


async def check_mcp(a,token,endpoint,out):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params=StdioServerParameters(command=sys.executable,args=['-I','-B',str(Path(__file__).absolute()),
        '--proxy-child',str(a.legacy_proxy),'--token',str(token),'--endpoint',endpoint])
    with (out/'proxy.log').open('a') as err:
        async with stdio_client(params,errlog=err) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                tools=await session.list_tools();names={t.name for t in tools.tools}
                assert {'status','capabilities','knowledge_get','knowledge_search','jobs_list'}<=names
                for name,args in [('status',{}),('jobs_list',{'limit':20}),('knowledge_get',{'entity_id':'fixture-knowledge'})]:
                    r=await session.call_tool(name,args)
                    assert not r.isError,(name,r)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('out','source-python','source-schema','source-package','wheel','legacy-plugin','legacy-proxy',
                 'make-source','serve','attempts','proxy-child','token'):
        p.add_argument('--'+name,type=Path)
    p.add_argument('--endpoint')
    a=p.parse_args()
    if a.make_source:make_source(a.make_source,a.source_schema)
    elif a.serve:serve(a.serve,a.attempts)
    elif a.proxy_child:asyncio.run(proxy_child(a.proxy_child,a.token,a.endpoint))
    else:run(a)
