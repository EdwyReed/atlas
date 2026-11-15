"""Reproduce the shipped six/seven cut, then rebase ONLY owned synthetic data."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('package','fixture','old-python','capture-python','out'):
        p.add_argument('--'+name,required=True,type=Path)
    a=p.parse_args();sys.path.insert(0,str(a.package.parent))
    from atlas_engine import portable as pt, portable_refresh as pr, adoption as ad, adoption_control as ctl, state_backup as sb
    from atlas_engine.repository import Repository
    from atlas_engine.adoption_native import row_pins
    from atlas_engine.engine import Engine
    from atlas_engine.upstream import Invoke,Atlas,unconfigured
    assert ctl.read(a.fixture/'result.json')['synthetic_only'] is True
    a.out.mkdir(mode=0o700,parents=True,exist_ok=False)
    source=a.fixture/'source';target=a.out/'target';archive=a.fixture/'capture.tar.gz'
    capture_versions=ad.runtime(a.capture_python)
    pt.unpack(archive,target);capture=sb.restore_state(target)['backup_manifest_sha256']
    expected=pt.protected_rows(target)
    assert len(expected)==7 and sum(r['state']=='succeeded' for r in expected)==1
    checks=[]
    def refuse(label,fn):
        try:fn()
        except (ValueError,OSError) as exc:checks.append({'refused':label,'reason':str(exc)});return
        raise AssertionError('accepted '+label)
    old_plan=a.out/'old-plan.json'
    code='''from pathlib import Path
import json,sys
from atlas_engine import portable_refresh as pr, adoption_control as ctl
root,source,plan=map(Path,sys.argv[1:4]);versions=json.loads(sys.argv[4])
r=pr.prepare(root,plan,source,versions);print(ctl.refresh(root,plan,r['plan_sha256']))
'''
    old=subprocess.run([str(a.old_python),'-I','-B','-c',code,str(target),str(source),str(old_plan),json.dumps(capture_versions)],check=True,capture_output=True,text=True)
    (a.out/'old-cut.stdout').write_text(old.stdout)
    state=sb.restore_state(target);first=ctl.bound_record(state['refresh'])
    missing=[r for r in expected if r['id'] not in {p['id'] for p in first['portable_held_rows']}]
    assert len(first['portable_held_rows'])==6 and len(missing)==1 and missing[0]['state']=='succeeded'
    assert row_pins(target,[r['id'] for r in expected])==expected
    stale={'target_root':str(target),'operator':'fixture','assessment':'Retain exact history.',
           'retain_historical_effects':True,'runtime':sb.versions(),'refresh':state['refresh'],
           'backup_manifest_sha256':state['backup_manifest_sha256'],'registry_logical_sha256':sb.registry_digest(target)}
    sb.write_json(a.out/'old-report.json',stale)
    previous=state['refresh']['sha256']
    # OLD resumed after the failed verify: a new terminal receipt and owned bytes.
    repo=Repository(source)
    added,_=repo.reserve('fixture','fixture','after-failed-verify','dream',{'request_id':'after-failed-verify'})
    repo.update(added['id'],'succeeded',{'result':{'after_failed_verify':True}})
    (source/'artifacts/after-failed-verify').write_bytes(b'new OLD progress')
    # Close fixture Repository connection cycles before declaring OLD stopped.
    import gc
    gc.collect()
    with closing(sqlite3.connect(source/'registry.sqlite3')) as db:
        db.execute('pragma wal_checkpoint(TRUNCATE)')
        db.execute('pragma journal_mode=DELETE')
    before=ad.source_inventory(source)
    def prepare(path,prev=previous,original=capture):
        return pr.prepare(target,a.out/path,source,capture_versions,prev,original)
    refuse('implicit rebase',lambda:pr.prepare(target,a.out/'implicit.json',source,capture_versions))
    refuse('wrong prior cut',lambda:prepare('wrong-cut.json','0'*64))
    refuse('wrong original capture',lambda:prepare('wrong-capture.json',original='0'*64))
    (target/'artifacts/foreign').write_bytes(b'foreign successor')
    refuse('successor payload before rebase',lambda:prepare('foreign.json'))
    (target/'artifacts/foreign').unlink()
    with sqlite3.connect(target/'registry.sqlite3') as db:db.execute("insert into health values('foreign','{}')")
    refuse('successor ledger before rebase',lambda:prepare('foreign-ledger.json'))
    with sqlite3.connect(target/'registry.sqlite3') as db:db.execute("delete from health where key='foreign'")
    refuse('stale first apply after OLD resumed',lambda:ctl.refresh(target,old_plan,sb.sha(old_plan)))
    prepared=prepare('plan.json');plan=a.out/'plan.json';pin=prepared['plan_sha256']
    # Preserve completed first-cut evidence and never patch the original marker.
    assert sb.restore_state(target)==state
    def interrupt():raise OSError('injected repeated cut after registry replacement')
    refuse('second cut interruption',lambda:ctl.refresh(target,plan,pin,after_apply=interrupt))
    refuse('incomplete rebase prepare',lambda:prepare('incomplete.json'))
    refuse('incomplete Engine',lambda:Engine(target,None,unconfigured(Invoke,'OFFLINE'),unconfigured(Atlas,'OFFLINE'),corpus_root=target/'corpus'))
    refuse('incomplete activation',lambda:pt.activate(target,a.out/'old-report.json'))
    (source/'artifacts/after-failed-verify').write_bytes(b'drift')
    refuse('changed source cannot resume second cut',lambda:ctl.refresh(target,plan,pin))
    (source/'artifacts/after-failed-verify').write_bytes(b'new OLD progress')
    result=ctl.refresh(target,plan,pin)
    final=sb.restore_state(target);cut=ctl.bound_record(final['refresh'])
    assert cut['portable_held_rows']==expected==pt.protected_rows(target)
    assert row_pins(target,[r['id'] for r in expected])==expected
    assert ctl.payload_files(source)==ctl.payload_files(target)
    with closing(ad.connection(source/'registry.sqlite3',closed=True)) as s,closing(ad.connection(target/'registry.sqlite3',closed=True)) as t:
        assert ad.ledger(s)==ad.ledger(t)
    assert ad.source_inventory(source)==before
    assert ctl.bound_record(final['portable_final_manifest'])['capture_manifest_sha256']==capture
    assert ctl.bound_record(final['portable_final_manifest'])['previous_final_manifest']==state['portable_final_manifest']
    pr.validate_final(target,final)
    refuse('stale report after rebase',lambda:pt.activate(target,a.out/'old-report.json'))
    refuse('superseded prior plan',lambda:ctl.refresh(target,old_plan,sb.sha(old_plan)))
    refuse('second plan replay after completion',lambda:ctl.refresh(target,plan,pin))
    current={**stale,'refresh':final['refresh'],'backup_manifest_sha256':final['backup_manifest_sha256'],
             'registry_logical_sha256':sb.registry_digest(target)}
    sb.write_json(a.out/'activation.json',current)
    activated=pt.activate(target,a.out/'activation.json');assert activated['held']==7
    assert ctl.bound_record(sb.restore_state(target)['held_admission'])['held_rows']==expected
    # Prove prior activation PLUS a real successor write is an absolute boundary.
    successor=Repository(target);j,_=successor.reserve('fixture','fixture','successor-write','dream',{'request_id':'successor-write'})
    successor.update(j['id'],'succeeded',{'result':{'successor':True}})
    gc.collect()
    with closing(sqlite3.connect(target/'registry.sqlite3')) as db:
        db.execute('pragma wal_checkpoint(TRUNCATE)')
        db.execute('pragma journal_mode=DELETE')
    target_before=ad.source_inventory(target)
    refuse('activated successor with writes cannot rebase',lambda:prepare('activated.json',final['refresh']['sha256']))
    refuse('activated successor cannot apply previous plan',lambda:ctl.refresh(target,plan,pin))
    assert ad.source_inventory(target)==target_before
    # Explicit exclusions are not restricted to a native kind or nonterminal state.
    family_root=a.out/'families';r=Repository(family_root)
    ids=[]
    for kind in ('generation','native_admin','batch','dream','acquisition','mask','censor'):
        j,_=r.reserve('fixture','fixture',kind,kind,{'request_id':kind});r.update(j['id'],'succeeded');ids.append(j['id'])
    family_pins=row_pins(family_root,sorted(ids))
    ref=ctl.record(family_root,'configured-native',{'held_rows':family_pins})
    family_state={'storage_target_root':str(family_root),'held_admission':ref,'previous_restore':None}
    assert pt.protected_rows(family_root,family_state)==family_pins
    sb.write_json(a.out/'result.json',{'passed':True,'synthetic_only':True,'old_cut_count':6,'missing':missing,
        'exact_protected_rows':expected,'rebase':result,'checks':checks,'all_family_explicit_exclusions':True,
        'original_capture_preserved':True,'authoritative_ledger_equal':True,'source_unchanged':True,
        'activated_successor_writes_preserved':True})
    print(json.dumps({'passed':True,'receipt':str(a.out/'result.json')}))


if __name__=='__main__':main()
