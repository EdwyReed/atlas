"""Focused offline counterexamples, then separate real-copy acceptance.
Run with the selected executable; --package selects already verified build bytes.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys


def run(a):
    sys.path.insert(0,str(a.package.parent))
    from atlas_engine import adoption as ad, adoption_control as ctl, state_backup as sb
    from atlas_engine.repository import dump
    from atlas_engine.engine import Engine
    out=a.out;out.mkdir(mode=0o700)
    source=out/'source';target=out/'target'
    subprocess.run([str(a.old_python),'-I','-B',str(a.make_source),'--make-source',str(source),
                    '--source-schema',str(a.schema)],check=True)
    with sqlite3.connect(out/'fixture-vocabulary.sqlite3') as db:
        db.execute('create table related(tag text, related_tag text, score real, pmi real, support integer, pair_count integer, related_count integer)')
    oldcode="""from atlas_engine import adoption as a
from pathlib import Path
import sys
p=Path(sys.argv[1]); a.plan(p/'source',Path(sys.argv[2]),p/'plan.json',[p/'external/dependency'],p/'external/attachment.json',native_binding={'id':'fixture','url':'http://127.0.0.1:9','backend_generation':'fixture-native','account_scope':'fixture-no-token','token_file':None,'enable_writes':False})
a.capture(p/'plan.json',p/'target')
"""
    subprocess.run([str(a.accepted_python),'-I','-B','-c',oldcode,str(out),str(a.old_python)],check=True)
    def refuse(fn):
        try:fn()
        except (ValueError,OSError):return
        raise AssertionError('expected refusal')
    checks=[]
    receipt=json.loads((target/'adoption-receipt.json').read_text())
    artifact=out/'artifact.json';sb.write_json(artifact,{'modules':sb.versions()['source_modules'],
        'wheel':str(a.wheel),'wheel_sha256':sb.sha(a.wheel)})
    report={'target_root':str(target),'receipt_sha256':sb.sha(target/'adoption-receipt.json'),
            'from_versions':receipt['plan']['target_versions'],'to_versions':sb.versions(),
            'registry_logical_sha256':sb.registry_digest(target),'operator':'fixture',
            'reason':'explicit transition from accepted installed package',
            'artifact':{'path':str(artifact),'sha256':sb.sha(artifact)}}
    up=out/'upgrade.json';sb.write_json(up,report)
    refuse(lambda:sb.restore_state(target))
    refuse(lambda:ctl.upgrade(target,up,'0'*64))
    ctl.upgrade(target,up,sb.sha(up));sb.restore_state(target)
    assert sb.sha(target/'adoption-receipt.json')==report['receipt_sha256']
    checks.append('real old-runtime capture upgraded explicitly; wrong pin/startup refused; original receipt unchanged')
    state=sb.restore_state(target)
    hold={'target_root':str(target),'capture_receipt_sha256':state['adoption_receipt_sha256'],
          'refresh':None,'runtime':sb.versions(),'registry_logical_sha256':sb.registry_digest(target),
          'held_rows':ctl.unknown_rows(target),'admission':'local-only-v1','operator':'fixture',
          **{k:'Unknown retained; no replay/terminal inference' for k in ('generation','administration','native_identity','media','replay_policy')},
          'evidence':[ad.pin(out/'external/dependency')]}
    hp=out/'holds.json';sb.write_json(hp,hold)
    stale=dict(hold);stale['held_rows']=hold['held_rows'][:-1];bad=out/'bad.json';sb.write_json(bad,stale)
    refuse(lambda:ctl.activate_holds(target,bad,sb.sha(bad)))
    ctl.activate_holds(target,hp,sb.sha(hp))
    class NoBackend:
        def __getattr__(self,key):raise AssertionError('native backend touched: '+key)
    Engine(target,None,NoBackend(),NoBackend(),False,corpus_root=target/'corpus').close()
    ctl.acceptance_begin(target,out/'acceptance')
    e=Engine(target,out/'fixture-vocabulary.sqlite3',NoBackend(),NoBackend(),False,corpus_root=target/'corpus')
    before=ctl.unknown_rows(target)
    e.start()
    assert e.cpu_thread.is_alive() and e.media_thread.is_alive() and e.thread is None
    assert e.call('generation.cancel',{'job_id':before[0]['id']})['error']['code']=='HELD_NATIVE_EFFECT'
    assert e.call('dream.submit',{'request_id':'model','model':'ambiguous'})['error']['code']=='HELD_NATIVE_RESOURCE'
    r=e.call('enrich',{'tags':['猫']});assert r['ok'],r
    r=e.call('dream.submit',{'request_id':'local-dream','tags':['猫']},'atlas-cutover-acceptance','acceptance');assert r['ok'],r
    e.close();assert ctl.unknown_rows(target)==before
    ctl.acceptance_end(target,out/'acceptance')
    checks.append('held local workers start; uncertain rows exact; remote cancel/model admission refused without native I/O')
    # Restore quarantine via a new complete refresh, not marker hand-editing.
    fp=out/'refresh.json';ctl.refresh_prepare(target,fp)
    # Target writes refuse before any refresh marker mutation.
    with sqlite3.connect(target/'registry.sqlite3') as db:db.execute("insert into health values('foreign','{}')")
    refuse(lambda:ctl.refresh(target,fp,sb.sha(fp)))
    with sqlite3.connect(target/'registry.sqlite3') as db:db.execute("delete from health where key='foreign'")
    # Include committed WAL, new native image/job change/event and changed owned bytes.
    writer=sqlite3.connect(source/'registry.sqlite3');writer.execute('pragma journal_mode=WAL')
    writer.execute('pragma wal_autocheckpoint=0');writer.execute("insert into health values('new-cut','{}')");writer.commit()
    # Reproduce the real data-loss counterexample: an originally held row has
    # completed in OLD, with matching events/result links. NEVER revert it.
    progressed=before[0]['id']
    data=json.loads(writer.execute('select data from jobs where id=?',(progressed,)).fetchone()[0])
    data.update(outputs=['final.png'],render_state='succeeded',native_items={'10':{'status':'completed'}})
    writer.execute('update jobs set state=?,data=?,updated=updated+1 where id=?',('succeeded',dump(data),progressed))
    writer.execute('insert into native_images values(?,?)',('final.png',progressed))
    writer.execute('insert into events(job_id,kind,data,at) values(?,?,?,?)',(progressed,'native.observed','{}',99))
    writer.commit()
    from atlas_engine.adoption_native import review_progress,intent,required_holds
    review=review_progress(target/'registry.sqlite3',source/'registry.sqlite3',hold['held_rows'])
    assert any(r['observation_progressed'] for r in review)
    # Original request/hash/payload identity changes still refuse.
    writer.execute('update jobs set hash=? where id=?',('foreign-intent',progressed));writer.commit()
    refuse(lambda:review_progress(target/'registry.sqlite3',source/'registry.sqlite3',hold['held_rows']))
    writer.execute('update jobs set hash=? where id=?',(next(r['original_row']['hash'] for r in review if r['id']==progressed),progressed));writer.commit()
    (source/'artifacts/blob').write_bytes(b'new source bytes')
    (source/'artifacts/new').write_bytes(b'added at fence')
    def interrupt():raise OSError('interrupted after complete registry replacement')
    refuse(lambda:ctl.refresh(target,fp,sb.sha(fp),after_apply=interrupt))
    refuse(lambda:Engine(target,None,NoBackend(),NoBackend()))
    # Drift blocks same-cut resume; removing only the synthetic drift restores cut.
    (source/'artifacts/new').write_bytes(b'drift')
    refuse(lambda:ctl.refresh(target,fp,sb.sha(fp)))
    (source/'artifacts/new').write_bytes(b'added at fence')
    result=ctl.refresh(target,fp,sb.sha(fp))
    assert result['refreshed'] and not sb.restore_state(target)['reconciled']
    with closing(ad.connection(source/'registry.sqlite3')) as s,closing(ad.connection(target/'registry.sqlite3')) as t:
        assert ad.ledger(s)==ad.ledger(t)
    assert ctl.payload_files(source)==ctl.payload_files(target)
    writer.close()
    state=sb.restore_state(target)
    assert required_holds(target,state)==__import__('atlas_engine.adoption_native',fromlist=['row_pins']).row_pins(target,sorted(r['id'] for r in hold['held_rows']))
    with sqlite3.connect(target/'registry.sqlite3') as db:
        assert db.execute('select state from jobs where id=?',(progressed,)).fetchone()[0]=='succeeded'
    checks.append('target-write conflict; committed WAL whole ledger and changed/additional bytes; interrupted startup and drift refusal; same-cut resume exact')
    checks.append('progressed held succeeded row/events/native_images retained exactly; immutable original provenance outside ledger; intent drift refused; final-cut holds repinned')
    sb.write_json(out/'result.json',{'checks':checks,'runtime':sb.versions()})
    print(dump({'passed':checks}))


if __name__=='__main__':
    p=argparse.ArgumentParser()
    for name in ('package','out','old-python','accepted-python','make-source','schema','wheel'):
        p.add_argument('--'+name,required=True,type=Path)
    run(p.parse_args())
