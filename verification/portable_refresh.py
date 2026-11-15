"""Disposable current-source portable delta, failure recovery and public reads."""
import argparse
from contextlib import closing
import fcntl
import json
from pathlib import Path
import shutil
import sqlite3
import sys


def run(a):
    sys.path.insert(0,str(a.package.parent))
    from atlas_engine import portable as pt, portable_refresh as pr, adoption_control as ctl, adoption as ad, state_backup as sb
    from atlas_engine.engine import Engine
    from atlas_engine.repository import dump, Repository
    from atlas_engine.upstream import Invoke, Atlas, unconfigured
    from atlas_engine.adoption_native import row_pins
    out=a.out.absolute();out.mkdir(mode=0o700,parents=True)
    source=out/'source';target=out/'target'
    def engine(root):return Engine(root,None,unconfigured(Invoke,'UNCONFIGURED'),unconfigured(Atlas,'UNCONFIGURED'),corpus_root=root/'corpus')
    e=engine(source)
    holds=[]
    for n in range(7):
        kind='generation' if n<3 else 'native_admin'
        j,_=e.repo.reserve('fixture','fixture','hold-'+str(n),kind,{'native_item_id':2380+n,'project':None} if n<3 else {'operation':'fixture'})
        state=('succeeded' if n==2 else 'observing') if n<3 else 'ack_unknown'
        data={'payload':{'native_item_id':2380+n,'project':None},'source_client':'fixture',
              'artifact_state':'available','author':'fixture','embedding_state':'pending',
              'history_missing':False,'index_state':'pending','native_ids':[2380+n],
              'native_items':{str(2380+n):{'batch_id':'redacted-batch','created_at':'redacted-time',
                  'error_type':None,'graph_id':'redacted-graph','item_id':2380+n,'origin':'fixture',
                  'outputs':['redacted.png'],'session_id':'redacted-session','status':'completed',
                  'status_sequence':1,'updated_at':'redacted-time'}},'outputs':['redacted.png'],
              'provenance':'fixture','render_state':'completed','upstream_user_id':'system'}
        e.repo.update(j['id'],state,data if n<3 else {'source_client':'fixture'});holds.append(j['id'])
    old,_=e.repo.reserve('fixture','fixture','progress','dream',{'request_id':'progress'})
    e.corpus.append('tags',{'tag':'old'},{'value':1},{'source':'fixture'},None,{},['fixture',1])
    (source/'artifacts/blob').write_bytes(b'old artifact')
    (source/'artifacts/unchanged-large').write_bytes(b'A'*(8*1024**2))
    e.close();(source/'service.lock').touch(exist_ok=True)
    sb.write_json(source/sb.MARKER,{'format':'atlas-restored-state-v1','storage_target_root':str(source),
        'storage_source_roots':['/historical/not-current'],'preimage_verified':True,'reconciled':True,
        'backup_manifest_sha256':'historical','previous_restore':None})
    marker=ctl.read(source/sb.MARKER)
    hold_report={'admission':'configured-source-v1','held_rows':row_pins(source,sorted(holds)),
        'historical_native_ids':sorted(holds),'target_root':str(source),'runtime':sb.versions(),
        'operator':'fixture','previous_admission':None,'refresh':None,'final_cut_review':[],
        'capture_receipt_sha256':'redacted','registry_logical_sha256':sb.registry_digest(source),
        'evidence':{},'native':{},'native_preflight_requests':0,'progress_assessment':'Retain exact exclusions.'}
    marker['held_admission']=ctl.record(source,'configured-native',hold_report)
    ctl.atomic(source/sb.MARKER,marker)
    # Literal adoption provenance deliberately names a DIFFERENT historical root.
    sb.write_json(source/'adoption-receipt.json',{'plan':{'source_root':'/historical/not-current','native_binding':None},'opaque':'unchanged'})
    historical=out/'historical.sqlite3'
    with sqlite3.connect(historical) as db:db.executescript("create table history(value text);insert into history values('literal old provenance');")
    (source/'references').mkdir();shutil.copyfile(historical,source/'references/frozen.sqlite3')
    sb.write_json(source/'portable-state.json',{'format':pt.FORMAT,'source_root':'/historical/not-current',
        'reference_paths':{str(historical):'references/frozen.sqlite3'},
        'reference_files':{str(historical):{'bytes':historical.stat().st_size,'sha256':sb.sha(historical)}},
        'attachment':None,'vocabulary':False,'history':'literal original bytes'})
    import subprocess
    capture_versions=ad.runtime(a.capture_python)
    subprocess.run([str(a.capture_python),'-I','-B','-m','atlas_engine.portable','pack',str(source),str(out/'capture.tar.gz')],check=True,capture_output=True)
    pt.unpack(out/'capture.tar.gz',target)
    captured=sb.restore_state(target);held_before=row_pins(target,sorted(holds))
    checks=[]
    def refuse(label,fn):
        try:fn()
        except (ValueError,OSError) as exc:checks.append({'refused':label,'reason':str(exc)});return
        raise AssertionError('accepted '+label)
    refuse('old canonical-only migration',lambda:ctl.refresh_prepare(target,out/'old-plan'))
    # Current corpus + progressed terminal job + new artifact/ledger AFTER capture.
    from types import SimpleNamespace
    from atlas_engine.corpus import Corpus
    repo=Repository(source);corpus=Corpus(source/'corpus',repo)
    e=SimpleNamespace(repo=repo,corpus=corpus,close=corpus.close)
    e.repo.update(old['id'],'succeeded',{'result':{'finished':True}})
    added,_=e.repo.reserve('fixture','fixture','new-terminal','dream',{'request_id':'new-terminal'})
    e.repo.update(added['id'],'succeeded',{'result':{'new':True}})
    e.corpus.append('tags',{'tag':'old'},{'value':2},{'source':'fixture'},None,{},['fixture',2])
    e.corpus.append('tags',{'tag':'new'},{'value':3},{'source':'fixture'},None,{},['fixture',3])
    (source/'artifacts/blob').write_bytes(b'changed current artifact')
    (source/'artifacts/new').write_bytes(b'new current artifact')
    e.close()
    refuse('wrong original pack runtime',lambda:pr.prepare(target,out/'wrong-runtime.json',source,{**capture_versions,'wrong-runtime':True}))
    qwal=source/'corpus/qdrant/collection/tags/storage.sqlite-wal'
    qwal.write_bytes(b'uncheckpointed fixture WAL')
    refuse('uncheckpointed Qdrant',lambda:pr.prepare(target,out/'qdrant-wal.json',source,capture_versions))
    qwal.unlink()
    with sqlite3.connect(source/'registry.sqlite3') as db:
        db.execute('update jobs set state=? where id=?',('succeeded',holds[0]))
    changed_hold=pr.prepare(target,out/'changed-hold.json',source,capture_versions)
    refuse('changed uncertain historical hold',lambda:ctl.refresh(target,out/'changed-hold.json',changed_hold['plan_sha256']))
    with sqlite3.connect(source/'registry.sqlite3') as db:
        db.execute('update jobs set state=? where id=?',('observing',holds[0]))
    # Keep a committed operational WAL open: refresh must backup, not copy main DB.
    writer=sqlite3.connect(source/'registry.sqlite3');writer.execute('pragma journal_mode=WAL')
    writer.execute('pragma wal_autocheckpoint=0');writer.execute("insert into health values('wal-cut','{}')");writer.commit()
    before=ad.source_inventory(source)
    with (source/'service.lock').open() as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        refuse('active source owner',lambda:pr.prepare(target,out/'busy.json',source,capture_versions))
    with (target/'corpus/owner.lock').open() as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        refuse('active destination owner',lambda:pr.prepare(target,out/'busy-target.json',source,capture_versions))
    refuse('wrong explicit current source',lambda:pr.prepare(target,out/'wrong.json',out/'nonexistent',capture_versions))
    # A non-ledger successor write invalidates the original manifest.
    (target/'artifacts/new-write').write_bytes(b'foreign')
    refuse('successor artifact before prepare',lambda:pr.prepare(target,out/'foreign.json',source,capture_versions))
    (target/'artifacts/new-write').unlink()
    with sqlite3.connect(target/'registry.sqlite3') as db:db.execute("insert into health values('foreign','{}')")
    refuse('successor ledger before prepare',lambda:pr.prepare(target,out/'foreign-db.json',source,capture_versions))
    # Restore disposable baseline using the real portable restore, not marker edits.
    shutil.rmtree(target);pt.unpack(out/'capture.tar.gz',target)
    prepared=pr.prepare(target,out/'plan.json',source,capture_versions);pin=prepared['plan_sha256']
    ref=target/'references/frozen.sqlite3';original_ref=ref.read_bytes();ref.write_bytes(b'corrupt')
    refuse('portable historical reference corruption',lambda:ctl.refresh(target,out/'plan.json',pin))
    ref.write_bytes(original_ref)
    historical.write_bytes(b'corrupt')
    refuse('original historical reference corruption',lambda:ctl.refresh(target,out/'plan.json',pin))
    historical.write_bytes(original_ref)
    plan=ctl.read(out/'plan.json')
    bad=dict(plan);bad['source_root']=str(out/'wrong-root');sb.write_json(out/'bad-plan.json',bad)
    refuse('plan hash mismatch',lambda:ctl.refresh(target,out/'bad-plan.json',pin))
    (target/'artifacts/blob').write_bytes(b'foreign')
    refuse('successor file after prepare',lambda:ctl.refresh(target,out/'plan.json',pin))
    (target/'artifacts/blob').write_bytes(b'old artifact')
    with sqlite3.connect(target/'registry.sqlite3') as db:db.execute("insert into health values('foreign','{}')")
    refuse('successor ledger after prepare',lambda:ctl.refresh(target,out/'plan.json',pin))
    with sqlite3.connect(target/'registry.sqlite3') as db:db.execute("delete from health where key='foreign'")
    stale={'target_root':str(target),'operator':'fixture','assessment':'Retain history.',
        'retain_historical_effects':True,'runtime':sb.versions(),
        'backup_manifest_sha256':captured['backup_manifest_sha256'],
        'registry_logical_sha256':sb.registry_digest(target)}
    sb.write_json(out/'stale-activation.json',stale)
    def interrupt():raise OSError('injected after registry replacement')
    refuse('interruption',lambda:ctl.refresh(target,out/'plan.json',pin,after_apply=interrupt))
    refuse('partial Engine construction',lambda:engine(target))
    refuse('partial activation',lambda:pt.activate(target,out/'stale-activation.json'))
    stage=target/ctl.CONTROL/('refresh-'+pin)
    changed=next(iter(ctl.read(stage/'pending.json')['changes']))
    staged=stage/'files'/changed;raw=staged.read_bytes();staged.write_bytes(b'corrupt')
    refuse('staged corruption',lambda:ctl.refresh(target,out/'plan.json',pin))
    staged.write_bytes(raw)
    (source/'artifacts/new').write_bytes(b'drift')
    refuse('source drift blocks resume',lambda:ctl.refresh(target,out/'plan.json',pin))
    (source/'artifacts/new').write_bytes(b'new current artifact')
    result=ctl.refresh(target,out/'plan.json',pin)
    assert ad.source_inventory(source)==before
    assert ctl.payload_files(source)==ctl.payload_files(target)
    with closing(ad.connection(source/'registry.sqlite3')) as s,closing(ad.connection(target/'registry.sqlite3')) as t:
        assert ad.ledger(s)==ad.ledger(t)
        assert t.execute('select state from jobs where id=?',(old['id'],)).fetchone()[0]=='succeeded'
        assert t.execute('select state from jobs where id=?',(added['id'],)).fetchone()[0]=='succeeded'
    assert row_pins(target,sorted(holds))==held_before
    assert (target/'adoption-receipt.json').read_bytes()==(source/'adoption-receipt.json').read_bytes()
    assert ctl.read(target/'portable-state.json')==plan['portable_metadata']
    transition=ctl.bound_record(sb.restore_state(target)['refresh'])
    assert transition['portable_held_rows']==held_before
    assert 'artifacts/unchanged-large' not in transition['changes']
    assert ad.raw_collections(source)==ad.raw_collections(target)
    pr.validate_final(target,sb.restore_state(target))
    refuse('old capture activation after final cut',lambda:pt.activate(target,out/'stale-activation.json'))
    stale.update(registry_logical_sha256=sb.registry_digest(target))
    sb.write_json(out/'stale-current-ledger.json',stale)
    refuse('old manifest even with current ledger',lambda:pt.activate(target,out/'stale-current-ledger.json'))
    previous_final=sb.restore_state(target)
    with sqlite3.connect(source/'registry.sqlite3') as db:
        db.execute("insert into health values('OLD-resumed-after-verify','{}')")
    rebase_before=ad.source_inventory(source)
    rebase=pr.prepare(target,out/'rebase-plan.json',source,capture_versions,
        previous_final['refresh']['sha256'],captured['backup_manifest_sha256'])
    ctl.refresh(target,out/'rebase-plan.json',rebase['plan_sha256'])
    assert ad.source_inventory(source)==rebase_before
    assert pt.protected_rows(target)==held_before
    with closing(ad.connection(source/'registry.sqlite3',closed=True)) as s,closing(ad.connection(target/'registry.sqlite3',closed=True)) as t:
        assert ad.ledger(s)==ad.ledger(t)
    pr.validate_final(target,sb.restore_state(target))
    final=sb.restore_state(target)
    accepted={**stale,'backup_manifest_sha256':final['backup_manifest_sha256'],'refresh':final['refresh'],
              'registry_logical_sha256':sb.registry_digest(target)}
    sb.write_json(out/'activation.json',accepted);pt.activate(target,out/'activation.json')
    refuse('activated successor refresh',lambda:pr.prepare(target,out/'activated.json',source,capture_versions))
    writer.close()
    # Small real HTTP + MCP read/restart check via the installed wheel's server.
    import asyncio, httpx, os, secrets, socket, subprocess, time
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    token=secrets.token_urlsafe(32);sb.write_json(out/'clients.json',[{'token':token,'principal':'fixture','source':'fixture'}])
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    sb.write_json(out/'config.json',{'root':str(target),'clients_file':str(out/'clients.json'),'port':port})
    endpoint='http://127.0.0.1:'+str(port);headers={'Authorization':'Bearer '+token}
    def op(name,args={}):return httpx.post(endpoint+'/v1/ops/'+name,json=args,headers=headers,trust_env=False,timeout=15).json()
    async def mcp():
        async with streamable_http_client(endpoint+'/mcp/',http_client=httpx.AsyncClient(headers=headers,trust_env=False)) as (rd,wr,_):
            async with ClientSession(rd,wr) as session:
                await session.initialize();r=await session.call_tool('dream_get',{'job_id':added['id']})
                result=r.structuredContent or json.loads(r.content[0].text)
                assert result['ok'],result
    for iteration in range(2):
        with (out/'server.log').open('ab') as log:
            proc=subprocess.Popen([str(a.server_python),'-I','-B','-m','atlas_engine.server','--config',str(out/'config.json')],stdout=log,stderr=log)
            try:
                for _ in range(200):
                    if proc.poll() is not None:raise AssertionError('server exited; see server.log')
                    try:
                        if op('status')['ok']:break
                    except httpx.HTTPError:pass
                    time.sleep(.1)
                else:raise AssertionError('startup timeout')
                for job_id in (old['id'],added['id']):
                    observed=op('dream.get',{'job_id':job_id});assert observed['ok'],observed
                asyncio.run(mcp())
            finally:proc.terminate();proc.wait(timeout=30)
        assert row_pins(target,sorted(holds))==held_before
    sb.write_json(out/'result.json',{'passed':True,'synthetic_only':True,'checks':checks,'refresh':result,
        'held_unchanged':len(holds),'exact_held_rows':held_before,'rebase_before_http_mcp':True,
        'authoritative_ledger_equal':True,'raw_corpus_equal':True,
        'source_unchanged':True,'http_mcp_starts':2,'delta_bytes':sum(x['bytes'] for x in transition['changes'].values()),
        'unchanged_large_file_not_staged':True,'runtime':sb.versions()})
    print(dump({'passed':True,'evidence':str(out/'result.json')}))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--package',required=True,type=Path);p.add_argument('--out',required=True,type=Path)
    p.add_argument('--server-python',required=True,type=Path)
    p.add_argument('--capture-python',required=True,type=Path)
    run(p.parse_args())
