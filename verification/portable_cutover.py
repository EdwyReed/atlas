"""Explicit operator runner for a stopped portable final cut.

Never stops/starts services. No native/network access. Run each phase explicitly;
activation requires a separately reviewed report SHA256. See docs/portable-refresh.md.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def inside(a,b):
    from atlas_engine import adoption as ad, adoption_control as ctl, portable as pt, portable_refresh as pr, state_backup as sb
    root=sb.safe_path(b['target_root']);source=sb.safe_path(b['source_root']);out=Path('/run/cutover')
    plan=out/'refresh-plan.json';report=out/'activation.json'
    if sb.sha(Path('/run/capture-versions.json'))!=b['capture_versions_sha256']:
        raise ValueError('capture executable version pin differs')
    if a.phase=='prepare':
        if ctl.read(root/sb.MARKER)['backup_manifest_sha256']!=b['capture_manifest_sha256']:
            raise ValueError('not selected original capture')
        result=pr.prepare(root,plan,source,ctl.read('/run/capture-versions.json'))
    elif a.phase=='apply':
        result=ctl.refresh(root,plan,a.sha256)
    elif a.phase=='activate':
        if sb.sha(report)!=a.sha256:raise ValueError('reviewed activation report checksum differs')
        # Source must STILL be stopped and byte-identical to this final cut.
        with sb.offline(source):
            pr.validate_source(source,root,ctl.read(plan))
            result=pt.activate(root,report)
    else:
        with sb.offline(source),sb.offline(root):
            state=sb.restore_state(root)
            if state['reconciled']:raise ValueError('final verification/report requires quarantine')
            pr.validate_source(source,root,ctl.read(plan));pr.validate_final(root,state)
            with closing(ad.connection(source/'registry.sqlite3',closed=True)) as s,closing(ad.connection(root/'registry.sqlite3',closed=True)) as t:
                if ad.ledger(s)!=ad.ledger(t):raise ValueError('whole final ledger differs')
            if ctl.payload_files(source)!=ctl.payload_files(root):raise ValueError('whole owned payload differs')
            cut=ctl.bound_record(state['refresh']);held=cut['portable_held_rows']
            if len(held)!=b['expected_held']:raise ValueError('held count differs from operator binding')
            result={'verified':True,'manifest_sha256':state['backup_manifest_sha256'],
                    'refresh':state['refresh'],'held':len(held),'source_unchanged':True}
            if a.phase=='report':
                if not a.operator or not a.assessment:raise ValueError('explicit operator and assessment required')
                sb.write_json(report,{'target_root':str(root),'operator':a.operator,'assessment':a.assessment,
                    'retain_historical_effects':True,'backup_manifest_sha256':state['backup_manifest_sha256'],
                    'refresh':state['refresh'],'registry_logical_sha256':sb.registry_digest(root),'runtime':sb.versions()})
                result['activation_report_sha256']=sb.sha(report)
    # Namespace root is used ONLY for host-source RO access. Restore target
    # ownership for application UID10001, on success or on fail-closed interruption.
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('phase',choices=('prepare','apply','verify','report','activate'))
    p.add_argument('--binding',type=Path,required=True);p.add_argument('--sha256')
    p.add_argument('--operator');p.add_argument('--assessment');p.add_argument('--inside',action='store_true',help=argparse.SUPPRESS)
    a=p.parse_args();os.umask(0o077)
    try:
        b=json.loads(a.binding.read_text())
        if a.phase in ('apply','activate') and not re.fullmatch('[0-9a-f]{64}',a.sha256 or ''):
            raise ValueError('explicit reviewed SHA256 required')
        if a.inside:
            try:result=inside(a,b)
            finally:
                # Never traverse/chown the read-only source or historical mounts.
                root=Path(b['target_root'])
                for base,dirs,files in os.walk(root,followlinks=False):
                    for name in dirs+files:
                        path=Path(base)/name
                        if path.is_symlink():raise ValueError('unexpected target symlink')
                        if path.stat().st_uid==0:os.chown(path,10001,10001)
                if root.exists() and root.stat().st_uid==0:os.chown(root,10001,10001)
            print(json.dumps(result,sort_keys=True));return
        if not re.fullmatch('sha256:[0-9a-f]{64}',b['image']):raise ValueError('immutable image ID required')
        if not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]+',b['destination_volume']):raise ValueError('named volume required')
        for key in ('source_root','target_root','evidence_dir','capture_versions'):
            path=Path(b[key])
            if not path.is_absolute() or '..' in path.parts or ',' in str(path):raise ValueError('explicit safe absolute path required: '+key)
        if b['source_root']==b['target_root']:raise ValueError('different source/target roots required')
        if b['target_root']!='/var/lib/atlas/state':raise ValueError('runner targets only /var/lib/atlas/state')
        source=Path(b['source_root'])
        if any(source.is_relative_to(Path(x)) or Path(x).is_relative_to(source) for x in ('/var/lib/atlas','/run','/tmp')):
            raise ValueError('source overlaps runner mounts')
        subprocess.run(['docker','volume','inspect',b['destination_volume']],check=True,capture_output=True)
        if b['expected_held']<0:raise ValueError('explicit expected held count required')
        evidence=Path(b['evidence_dir']);evidence.mkdir(mode=0o700,parents=True,exist_ok=True)
        args=['docker','run','--rm','--network','none','--read-only','--user','0:0',
              '--cap-drop','ALL','--cap-add','DAC_OVERRIDE','--cap-add','CHOWN',
              '--security-opt','no-new-privileges','--tmpfs','/tmp:rw,noexec,nosuid,size=16m',
              '--mount','type=volume,src='+b['destination_volume']+',dst=/var/lib/atlas',
              '--mount','type=bind,src='+b['source_root']+',dst='+b['source_root']+',readonly',
              '--mount','type=bind,src='+str(evidence)+',dst=/run/cutover',
              '--mount','type=bind,src='+str(a.binding.absolute())+',dst=/run/binding.json,readonly',
              '--mount','type=bind,src='+b['capture_versions']+',dst=/run/capture-versions.json,readonly',
              '--mount','type=bind,src='+str(Path(__file__).absolute())+',dst=/run/portable-cutover.py,readonly']
        # Explicit individual historical files only; never HOME, config or keys.
        for pin in b['historical_files']:
            path=Path(pin['path'])
            if not path.is_absolute() or not path.is_file() or path.is_symlink() or ',' in str(path):raise ValueError('regular explicit historical file required')
            if path.stat().st_size!=pin['bytes'] or sha(path)!=pin['sha256']:raise ValueError('historical input pin changed')
            if path.is_relative_to(Path(b['source_root'])):continue
            args+=['--mount','type=bind,src='+str(path)+',dst='+str(path)+',readonly']
        args+=['--entrypoint','python',b['image'],'-I','-B','/run/portable-cutover.py',a.phase,'--inside','--binding','/run/binding.json']
        if a.sha256:args+=['--sha256',a.sha256]
        if a.operator:args+=['--operator',a.operator]
        if a.assessment:args+=['--assessment',a.assessment]
        subprocess.run(args,check=True)
    except (ValueError,OSError,KeyError,subprocess.CalledProcessError) as e:p.exit(1,'Portable cutover refused: '+str(e)+'\n')

if __name__=='__main__':main()
