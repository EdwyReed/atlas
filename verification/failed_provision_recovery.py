"""Narrow disposable activated-never-started recovery; no real roots/services."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for n in ('package','fixture','old-python','out'):p.add_argument('--'+n,type=Path,required=True)
    a=p.parse_args();sys.path.insert(0,str(a.package.parent))
    from atlas_engine import portable as pt, portable_refresh as pr, portable_recovery as rec, adoption_control as ctl, adoption as ad, state_backup as sb
    from atlas_engine.repository import Repository
    from atlas_engine.engine import Engine
    from atlas_engine.upstream import Invoke,Atlas,unconfigured
    assert ctl.read(a.fixture/'result.json')['synthetic_only'] is True
    a.out.mkdir(mode=0o700,parents=True,exist_ok=False)
    source=a.fixture/'source';root=a.out/'target'
    oldcode='''import json,sys
from pathlib import Path
from atlas_engine import portable as pt, portable_refresh as pr, adoption_control as ctl, state_backup as sb
r,archive,source,out=map(Path,sys.argv[1:]);pt.unpack(archive,r)
v=sb.versions();q=pr.prepare(r,out/'first-plan.json',source,v);ctl.refresh(r,out/'first-plan.json',q['plan_sha256']);s=sb.restore_state(r)
report=dict(target_root=str(r),operator='fixture',assessment='retain exact history',retain_historical_effects=True,runtime=v,refresh=s['refresh'],backup_manifest_sha256=s['backup_manifest_sha256'],registry_logical_sha256=sb.registry_digest(r))
sb.write_json(out/'activation.json',report);pt.activate(r,out/'activation.json')
'''
    subprocess.run([str(a.old_python),'-I','-B','-c',oldcode,str(root),str(a.fixture/'capture.tar.gz'),str(source),str(a.out)],check=True)
    original=sb.restore_state(root);pins=pt.protected_rows(root);assert len(pins)==7
    (root/'media-kit').mkdir();(root/'media-kit/incomplete').touch()
    evidence=a.out/'fence.json';sb.write_json(evidence,{'synthetic_only':True,'helper_failed':True,'never_launched':True,'fenced':True})
    report={'format':rec.FORMAT,'target_root':str(root),'state':original,
        'original_capture_manifest_sha256':ctl.bound_record(original['portable_final_manifest'])['capture_manifest_sha256'],
        'refresh':original['refresh'],'admission':original['held_admission'],
        'activation_report_sha256':original['held_admission']['sha256'],'runtime':sb.versions(),
        'failure_phase':'media-provision-before-runtime-admission','runtime_fenced':True,'never_started':True,
        'operator':'synthetic fixture','assessment':'Independent fence plus exact activation ledger and payload. No successor admitted.',
        'evidence':[ad.pin(evidence)],'target_inventory':ad.source_inventory(root)}
    path=a.out/'recovery.json';sb.write_json(path,report);checks=[]
    def refuse(label,fn):
        try:fn()
        except (ValueError,OSError) as exc:checks.append({'refused':label,'reason':str(exc)});return
        raise AssertionError('accepted '+label)
    before=ad.source_inventory(root)
    refuse('wrong reviewed hash',lambda:rec.recover(root,path,'0'*64))
    refuse('ordinary activated rebase',lambda:pr.prepare(root,a.out/'illegal-plan',source,sb.versions(),original['refresh']['sha256'],report['original_capture_manifest_sha256']))
    (root/'artifacts/foreign').write_bytes(b'successor writes')
    refuse('successor payload even with updated operator inventory',lambda:rec.validate(root,{**report,'target_inventory':ad.source_inventory(root)}))
    (root/'artifacts/foreign').unlink()
    with closing(sqlite3.connect(root/'registry.sqlite3')) as db:db.execute("insert into health values('successor','{}')");db.commit()
    refuse('successor whole ledger',lambda:rec.validate(root,{**report,'target_inventory':ad.source_inventory(root)}))
    with closing(sqlite3.connect(root/'registry.sqlite3')) as db:db.execute("delete from health where key='successor'");db.commit()
    # SQLite physical layout need not match after the counterexample; update ONLY
    # this synthetic operator inventory. The final logical cut is still checked.
    report['target_inventory']=ad.source_inventory(root);ctl.atomic(path,report)
    (root/rec.WITNESS).write_text('{}')
    refuse('ever-admitted runtime even idle',lambda:rec.validate(root,{**report,'target_inventory':ad.source_inventory(root)}))
    (root/rec.WITNESS).unlink()
    result=rec.recover(root,path,sb.sha(path));assert rec.recover(root,path,sb.sha(path))['replayed']
    current=sb.restore_state(root);assert not current['reconciled'] and 'held_admission' not in current
    assert ctl.bound_record(current['failed_provision_recovery'])['previous_state']==original
    assert (root/'media-kit/incomplete').exists() and pt.protected_rows(root)==pins
    # OLD progresses AFTER failed provisioning: fresh authoritative ledger + bytes.
    repo=Repository(source);j,_=repo.reserve('fixture','fixture','after-provision-failure','dream',{'request_id':'after-provision-failure'})
    repo.update(j['id'],'succeeded',{'result':{'new_old_progress':True}})
    (source/'artifacts/after-provision-failure').write_bytes(b'new OLD progress')
    import gc;del repo;gc.collect()
    with closing(sqlite3.connect(source/'registry.sqlite3')) as db:db.execute('pragma wal_checkpoint(TRUNCATE)');db.execute('pragma journal_mode=DELETE')
    source_before=ad.source_inventory(source)
    prepared=pr.prepare(root,a.out/'new-plan.json',source,sb.versions(),original['refresh']['sha256'],report['original_capture_manifest_sha256'])
    def interrupt():raise OSError('injected same-cut interruption')
    refuse('interrupted new cut',lambda:ctl.refresh(root,a.out/'new-plan.json',prepared['plan_sha256'],after_apply=interrupt))
    ctl.refresh(root,a.out/'new-plan.json',prepared['plan_sha256'])
    assert ad.source_inventory(source)==source_before and pt.protected_rows(root)==pins
    refuse('stale activation report',lambda:pt.activate(root,a.out/'activation.json'))
    s=sb.restore_state(root);newreport={**ctl.read(a.out/'activation.json'),'runtime':sb.versions(),'refresh':s['refresh'],'backup_manifest_sha256':s['backup_manifest_sha256'],'registry_logical_sha256':sb.registry_digest(root)}
    sb.write_json(a.out/'fresh-activation.json',newreport);pt.activate(root,a.out/'fresh-activation.json')
    refuse('incomplete media startup',lambda:Engine(root,None,unconfigured(Invoke,'OFFLINE'),unconfigured(Atlas,'OFFLINE'),corpus_root=root/'corpus'))
    assert not (root/rec.WITNESS).exists()
    # Synthetic marker is removed only in this fixture to exercise real admission.
    (root/'media-kit/incomplete').unlink()
    e=Engine(root,None,unconfigured(Invoke,'OFFLINE'),unconfigured(Atlas,'OFFLINE'),corpus_root=root/'corpus');e.close()
    assert (root/rec.WITNESS).exists()
    root_before=ad.source_inventory(root)
    refuse('already admitted successor cannot recover',lambda:rec.recover(root,path,sb.sha(path)))
    assert ad.source_inventory(root)==root_before
    sb.write_json(a.out/'result.json',{'passed':True,'synthetic_only':True,'recovery':result,'exact_seven_rows':pins,'new_old_progress_preserved':True,'source_unchanged':True,'same_cut_resume':True,'checks':checks})
    print(json.dumps({'passed':True,'receipt':str(a.out/'result.json')}))


if __name__=='__main__':main()
