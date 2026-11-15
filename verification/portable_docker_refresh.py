"""Disposable Docker cutover acceptance; never selects a production volume.

Uses a completed portable_refresh.py fixture's original dev18 archive and source.
Only that disposable source gets a WAL fixture write. Runs the actual operator
runner through prepare/apply/verify/report/checksum-bound activate in both images.
"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid


def run(args):
    return subprocess.run(list(map(str,args)),check=True,capture_output=True,text=True).stdout.strip()


def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()


def inventory(root):
    return {str(p.relative_to(root)):sha(p) for p in sorted(root.rglob('*')) if p.is_file()}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--fixture',required=True,type=Path);p.add_argument('--out',required=True,type=Path)
    p.add_argument('--capture-python',required=True,type=Path)
    p.add_argument('--image',required=True,action='append')
    a=p.parse_args();a.out.mkdir(mode=0o700,parents=True,exist_ok=False)
    assert json.loads((a.fixture/'result.json').read_text())['synthetic_only'] is True
    source=a.fixture/'source';historical=a.fixture/'historical.sqlite3'
    versions=run([a.capture_python,'-I','-B','-c','from atlas_engine.state_backup import versions;from atlas_engine.repository import dump;print(dump(versions()))'])
    (a.out/'capture-versions.json').write_text(versions+'\n')
    runner=Path(__file__).with_name('portable_cutover.py').absolute()
    receipts=[]
    for index,image in enumerate(a.image):
        info=json.loads(run(['docker','image','inspect',image]))[0];image=info['Id']
        scenario='clean' if index==0 else 'wal'
        work=a.out/scenario  # Binding renderer must create a NEW evidence dir.
        volume='atlas-portable-fixture-'+uuid.uuid4().hex
        writer=None
        if scenario=='wal':
            writer=sqlite3.connect(source/'registry.sqlite3')
            writer.execute('pragma journal_mode=WAL');writer.execute('pragma wal_autocheckpoint=0')
            writer.execute("insert into health values('docker-wal-cut','{}')");writer.commit()
            assert Path(str(source/'registry.sqlite3')+'-wal').stat().st_size>0
        else:
            assert not any(Path(str(source/'registry.sqlite3')+s).exists() for s in ('-wal','-shm'))
        before=inventory(source)
        run(['docker','volume','create',volume])
        try:
            # Empty fixture volume only. UID10001 owns restored state throughout.
            run(['docker','run','--rm','--network','none','--read-only','--user','0:0',
                 '--mount','type=volume,src='+volume+',dst=/var/lib/atlas',
                 '--mount','type=bind,src='+str(a.fixture/'capture.tar.gz')+',dst=/run/capture.tar.gz,readonly',
                 '--entrypoint','python',image,'-I','-B','-c',
                 'import os;from pathlib import Path;from atlas_engine.portable import unpack;unpack(Path("/run/capture.tar.gz"),Path("/var/lib/atlas/state"));[(os.chown(b,10001,10001),[os.chown(Path(b)/n,10001,10001) for n in ds+fs]) for b,ds,fs in os.walk("/var/lib/atlas")]'])
            manifest=json.loads(run(['docker','run','--rm','--network','none','--read-only',
                 '--mount','type=volume,src='+volume+',dst=/var/lib/atlas,readonly',
                 '--entrypoint','python',image,'-I','-B','-c',
                 'import json;from pathlib import Path;from atlas_engine.state_backup import MARKER;print(json.dumps(json.loads((Path("/var/lib/atlas/state")/MARKER).read_text())["backup_manifest_sha256"]))']))
            rendered=run([sys.executable,'-I','-B',runner.with_name('portable_binding.py'),
                '--image',image,'--volume',volume,'--source-root',source,
                '--capture-manifest',manifest,'--capture-python',a.capture_python,
                '--expected-held','7','--out',work])
            assert json.loads(rendered)['state_writes'] is False
            path=work/'binding.json'
            def phase(name,*extra):
                output=run([sys.executable,'-I','-B',runner,name,'--binding',path,*extra])
                (work/(name+'.stdout')).write_text(output+'\n')
                return json.loads(output.splitlines()[-1])
            prepared=phase('prepare');phase('apply','--sha256',prepared['plan_sha256'])
            verified=phase('verify');report=phase('report','--operator','fixture','--assessment','Retain all historical effects; synthetic Docker acceptance.')
            assert len(verified['held_rows'])==7 and any(r['state']=='succeeded' for r in verified['held_rows'])
            original_binding=json.loads(path.read_text())
            wrong_binding={**original_binding,'expected_held_rows':[dict(r) for r in original_binding['expected_held_rows']]}
            wrong_binding['expected_held_rows'][0]['row_sha256']='0'*64
            wrong_path=work/'wrong-exact-binding.json';wrong_path.write_text(json.dumps(wrong_binding))
            rejected=subprocess.run([sys.executable,'-I','-B',str(runner),'verify','--binding',str(wrong_path)],capture_output=True,text=True)
            assert rejected.returncode and 'exact protected identities/digests differ' in rejected.stderr
            assert inventory(source)==before,'first cut changed source'
            old_work=work;old_path=path;old_report=(work/'activation.json').read_bytes()
            with closing(sqlite3.connect(source/'registry.sqlite3')) as db:
                db.execute("insert into health values(?, '{}')",('docker-OLD-resumed-'+scenario,));db.commit()
            before=inventory(source)
            # A previously valid report and stopped-source plan are now stale.
            stale=subprocess.run([sys.executable,'-I','-B',str(runner),'activate','--binding',str(old_path),
                '--sha256',report['activation_report_sha256']],capture_output=True,text=True)
            assert stale.returncode and 'source cut changed' in stale.stderr,stale.stderr
            work=a.out/(scenario+'-rebase')
            run([sys.executable,'-I','-B',runner.with_name('portable_binding.py'),
                '--image',image,'--volume',volume,'--source-root',source,
                '--capture-manifest',manifest,'--capture-python',a.capture_python,'--expected-held','7',
                '--rebase-refresh-sha256',verified['refresh']['sha256'],'--out',work])
            path=work/'binding.json'
            prepared=phase('prepare');phase('apply','--sha256',prepared['plan_sha256'])
            verified=phase('verify');report=phase('report','--operator','fixture','--assessment','Retain exact seven after OLD resumed.')
            assert verified['held_rows']==original_binding['expected_held_rows']
            new_report=(work/'activation.json').read_bytes();(work/'activation.json').write_bytes(old_report)
            stale=subprocess.run([sys.executable,'-I','-B',str(runner),'activate','--binding',str(path),
                '--sha256',sha(work/'activation.json')],capture_output=True,text=True)
            assert stale.returncode and 'activation must bind' in stale.stderr,stale.stderr
            (work/'activation.json').write_bytes(new_report)
            wrong=subprocess.run([sys.executable,'-I','-B',str(runner),'activate','--binding',str(path),'--sha256','0'*64],capture_output=True,text=True)
            assert wrong.returncode and 'checksum differs' in wrong.stderr,wrong.stderr
            (work/'wrong-report-refusal.log').write_text(wrong.stderr)
            phase('activate','--sha256',report['activation_report_sha256'])
            # App UID10001 must read activated state after namespace-root helper.
            observed=json.loads(run(['docker','run','--rm','--network','none','--read-only',
                 '--cap-drop','ALL','--mount','type=volume,src='+volume+',dst=/var/lib/atlas,readonly',
                 '--entrypoint','python',image,'-I','-B','-c',
                 'import json,os;from pathlib import Path;from atlas_engine.state_backup import MARKER;from atlas_engine.adoption import connection;root=Path("/var/lib/atlas/state");s=json.loads((root/MARKER).read_text());db=connection(root/"registry.sqlite3",closed=True);print(json.dumps(dict(uid=os.getuid(),reconciled=s["reconciled"],wal_row=db.execute("select count(*) from health where key=\'docker-wal-cut\'").fetchone()[0],bad_owner=[str(p) for p in root.rglob("*") if p.stat().st_uid!=10001])))']))
            assert observed['uid']==10001 and observed['reconciled'] and not observed['bad_owner'],observed
            if scenario=='wal':assert observed['wal_row']==1,observed
            assert inventory(source)==before,'source bytes or file set changed'
            receipts.append({'scenario':scenario,'image':image,'verified':verified,'app':observed,
                'source_exact_bytes_and_files_unchanged':True,'wrong_activation_sha_refused':True,
                'rebase_after_OLD_resumed':True,'exact_identity_mismatch_refused':True,
                'stale_source_and_prior_report_refused':True})
        finally:
            run(['docker','volume','rm',volume])
            if writer:writer.close()
    (a.out/'result.json').write_text(json.dumps({'passed':True,'synthetic_only':True,'scenarios':receipts,'owned_volumes_removed':True},indent=2)+'\n')
    print(json.dumps({'passed':True,'receipt':str(a.out/'result.json')}))


if __name__=='__main__':main()
