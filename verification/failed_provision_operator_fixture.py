"""Exercise the exact versioned owner entrypoint on owned disposable volumes.

Reuses an explicitly labelled synthetic portable fixture. No production volumes,
service operations, inference, credentials or new dependency acceptance.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import uuid
from typing import Any


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def write(p, value):
    p.write_text(json.dumps(value, indent=2)+'\n')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for n in ('entrypoint', 'fixture', 'image', 'capture-python', 'out'):
        p.add_argument('--'+n, required=True)
    a = p.parse_args()
    os.umask(0o077)
    out, fixture, entry = Path(a.out), Path(a.fixture), Path(a.entrypoint)
    assert json.loads((fixture/'result.json').read_text())['synthetic_only']
    out.mkdir(mode=0o700, parents=True)
    assert not (out/'release-lock-v2.json').exists()
    shutil.copyfile(entry, out/entry.name)
    (out/entry.name).chmod(0o700)
    source = Path(__file__).absolute().parent.parent
    name = 'atlas-operator-fixture-'+uuid.uuid4().hex[:12]
    volumes = [name+'-state', name+'-config']
    checks = []
    def run(args, **kw):
        return subprocess.run(list(map(str, args)), check=True, capture_output=True, text=True, **kw).stdout
    common = ['docker', 'run', '--rm', '--network', 'none', '--read-only', '--user', '0:0',
              '--cap-drop', 'ALL', '--cap-add', 'DAC_OVERRIDE', '--cap-add', 'CHOWN',
              '--security-opt', 'no-new-privileges',
              '--mount', 'type=volume,src='+volumes[0]+',dst=/var/lib/atlas',
              '--mount', 'type=bind,src='+str(fixture)+',dst='+str(fixture)+',readonly',
              '--mount', 'type=bind,src='+str(out)+',dst='+str(out)]
    def inside(code):
        return run(common+['--entrypoint', 'python', a.image, '-I', '-B', '-c', code])
    def owner(phase, *args, env=None):
        result = subprocess.run([str(out/entry.name), phase, *map(str, args)], capture_output=True, text=True, env=env)
        (out/('wrapper-'+phase+'-'+str(len(list(out.glob('wrapper-*.log'))))+'.log')).write_text(result.stdout+result.stderr)
        return result
    try:
        for volume in volumes:
            run(['docker', 'volume', 'create', volume])
            assert json.loads(run(['docker', 'volume', 'inspect', volume]))[0]['Name'] == volume
        with tarfile.open(fixture/'capture.tar.gz') as archive:
            stream = archive.extractfile('manifest.json')
            assert stream is not None
            manifest = json.load(stream)
        write(out/'capture-versions.json', manifest['versions'])
        setup = f'''import json
from pathlib import Path
from atlas_engine import portable as pt, portable_refresh as pr, adoption_control as ctl, adoption as ad, state_backup as sb, portable_recovery as rec
r=Path('/var/lib/atlas/state');out=Path({str(out)!r});source=Path({str(fixture/'source')!r})
pt.unpack(Path({str(fixture/'capture.tar.gz')!r}),r)
prep=pr.prepare(r,out/'first-plan.json',source,ctl.read(out/'capture-versions.json'))
ctl.refresh(r,out/'first-plan.json',prep['plan_sha256'])
s=sb.restore_state(r)
report=dict(target_root=str(r),operator='disposable operator',assessment='retain all historical effects',retain_historical_effects=True,runtime=sb.versions(),refresh=s['refresh'],backup_manifest_sha256=s['backup_manifest_sha256'],registry_logical_sha256=sb.registry_digest(r))
sb.write_json(out/'activation.json',report);pt.activate(r,out/'activation.json');s=sb.restore_state(r)
(out/'accepted-admission.json').write_bytes(Path(s['held_admission']['path']).read_bytes())
(r/'media-kit').mkdir();(r/'media-kit/incomplete').touch()
sb.write_json(out/'fence.json',dict(synthetic_only=True,never_launched=True,fenced=True))
report=dict(format=rec.FORMAT,target_root=str(r),state=s,original_capture_manifest_sha256=ctl.bound_record(s['portable_final_manifest'])['capture_manifest_sha256'],refresh=s['refresh'],admission=s['held_admission'],activation_report_sha256=s['held_admission']['sha256'],runtime=sb.versions(),failure_phase='media-provision-before-runtime-admission',runtime_fenced=True,never_started=True,operator='disposable operator',assessment='explicit synthetic independent fence',evidence=[ad.pin(out/'fence.json')],target_inventory=ad.source_inventory(r))
rec.validate(r,report);sb.write_json(out/'recovery.json',report)
sb.write_json(out/'before.json',dict(state=s,inventory=ad.source_inventory(r),held=pt.protected_rows(r)))
'''
        (out/'setup.log').write_text(inside(setup))
        report = json.loads((out/'recovery.json').read_text())
        lock: dict[str, Any] = dict(commit=run(['git', '-C', source, 'rev-parse', 'HEAD']).strip(), images=dict(core=a.image, media=a.image),
                    config_volume=volumes[1], pins={}, failed_activation=dict(
                        input=dict(path=str(out/'activation.json'), sha256=sha(out/'activation.json')),
                        accepted_admission=dict(path=str(out/'accepted-admission.json'), sha256=sha(out/'accepted-admission.json')),
                        state_admission=report['admission']),
                    cutover=dict(destination_volume=volumes[0], source_root=str(fixture/'source'), target_root=report['target_root'],
                                 capture_manifest_sha256=report['original_capture_manifest_sha256'],
                                 rebase_refresh_sha256=report['refresh']['sha256'], expected_held=7,
                                 expected_held_rows=json.loads((out/'before.json').read_text())['held'], capture_python=a.capture_python))
        for f in [out/'activation.json', out/'accepted-admission.json', out/entry.name, source/'verification/failed_provision_operator.py']:
            lock['pins'][str(f)] = sha(f)
        write(out/'release-lock-v2.json', lock)
        # Negative paths must fail BEFORE Docker, not merely fail in the module.
        guard = out/'guard';guard.mkdir();docker = guard/'docker'
        docker.write_text('#!/bin/sh\nprintf invoked > "'+str(out/'unexpected-docker')+'"\nexit 99\n');docker.chmod(0o700)
        env = dict(os.environ, PATH=str(guard)+':'+os.environ['PATH'])
        baseline_input = (out/'activation.json').read_bytes()
        baseline_accepted = (out/'accepted-admission.json').read_bytes()
        def refuse(label, candidate, candidate_report, reason):
            write(out/'release-lock-v2.json', candidate);write(out/'negative-report.json', candidate_report)
            result = owner('quarantine', '--report', out/'negative-report.json', '--sha256', sha(out/'negative-report.json'), env=env)
            assert result.returncode and reason in result.stderr, (label, result.stdout, result.stderr)
            assert not (out/'unexpected-docker').exists(), label+' reached Docker'
            checks.append(dict(refused=label, before_docker=True, reason=reason))
        candidate = copy.deepcopy(lock)
        x, y = candidate['failed_activation']['input'], candidate['failed_activation']['accepted_admission']
        x['sha256'], y['sha256'] = y['sha256'], x['sha256']
        refuse('swapped input/admission hashes', candidate, report, 'checksum differs')
        altered = json.loads(baseline_input);altered['assessment'] += ' altered'
        write(out/'activation.json', altered)
        refuse('altered input bytes', lock, report, 'release input drift')
        (out/'activation.json').write_bytes(baseline_input)
        altered = json.loads(baseline_accepted);altered['assessment'] += ' unrelated'
        write(out/'accepted-admission.json', altered)
        refuse('altered admission bytes', lock, report, 'release input drift')
        candidate = copy.deepcopy(lock)
        candidate['failed_activation']['accepted_admission']['sha256'] = sha(out/'accepted-admission.json')
        candidate['pins'][str(out/'accepted-admission.json')] = sha(out/'accepted-admission.json')
        refuse('repinned altered admission projection', candidate, report, 'not the original input projection')
        (out/'accepted-admission.json').write_bytes(baseline_accepted)
        altered = json.loads(baseline_input);altered['operator'] = 'unrelated activation'
        write(out/'activation.json', altered)
        candidate = copy.deepcopy(lock)
        candidate['failed_activation']['input']['sha256'] = sha(out/'activation.json')
        candidate['pins'][str(out/'activation.json')] = sha(out/'activation.json')
        refuse('repinned unrelated accepted input', candidate, report, 'not the original input projection')
        (out/'activation.json').write_bytes(baseline_input)
        altered = copy.deepcopy(report);altered['activation_report_sha256'] = lock['failed_activation']['input']['sha256']
        refuse('recovery uses input hash instead of admission', lock, altered, 'must bind expanded admission')
        write(out/'release-lock-v2.json', lock)
        result = owner('quarantine', '--report', out/'recovery.json', '--sha256', sha(out/'recovery.json'))
        assert result.returncode == 0, result.stderr
        readback = inside(f'''from pathlib import Path
from atlas_engine import adoption_control as ctl,state_backup as sb,portable as pt,adoption as ad
r=Path('/var/lib/atlas/state');out=Path({str(out)!r})
with sb.offline(r):
 before=ctl.read(out/'before.json');s=sb.restore_state(r);receipt=ctl.bound_record(s['failed_provision_recovery'])
 assert not s['reconciled'] and 'held_admission' not in s
 assert receipt['previous_state']==before['state'] and s['preserved_holds']==before['state']['held_admission']
 assert pt.protected_rows(r)==before['held'] and len(before['held'])==7
 current=ad.source_inventory(r)
 for k,v in before['inventory'].items():
  if k!=sb.MARKER: assert current[k]==v,(k,'changed')
 sb.write_json(out/'after.json',dict(state=s,held=pt.protected_rows(r),inventory=current))
 print('PASS exact wrapper quarantine readback; prior marker/admission/partial kit/ledger/holds preserved')
''')
        (out/'readback.log').write_text(readback)
        result = owner('quarantine', '--report', out/'recovery.json', '--sha256', sha(out/'recovery.json'))
        assert result.returncode == 0 and 'replayed' in result.stdout, result.stderr
        # Exercise remaining recovery-cutover wrapper contracts, no model/runtime startup.
        result = owner('bind', '--out', out/'fresh-cut')
        assert result.returncode == 0, result.stderr
        binding = out/'fresh-cut/binding.json'
        for phase in ('prepare', 'apply', 'verify', 'report', 'activate'):
            args = ['--binding', binding]
            if phase == 'apply':args += ['--sha256', sha(out/'fresh-cut/refresh-plan.json')]
            if phase == 'activate':args += ['--sha256', sha(out/'fresh-cut/activation.json')]
            if phase == 'report':args += ['--operator', 'wrapper fixture', '--assessment', 'retain all effects; wrapper forwarding verified']
            result = owner(phase, *args)
            assert result.returncode == 0, (phase, result.stderr)
        activation = json.loads((out/'fresh-cut/activation.json').read_text())
        assert activation['operator'] == 'wrapper fixture' and activation['assessment'] == 'retain all effects; wrapper forwarding verified'
        inside("from pathlib import Path\nfrom atlas_engine import state_backup as sb,adoption_control as ctl\nr=Path('/var/lib/atlas/state')\ns=sb.restore_state(r);a=ctl.bound_record(s['held_admission']);assert s['reconciled'] and a['operator']=='wrapper fixture' and not (r/'.portable-runtime-admitted.json').exists()\nprint('PASS fresh activation readback')")
        write(out/'result.json', dict(passed=True, synthetic_only=True, exact_entrypoint_sha256=sha(entry),
                                     fixture_entrypoint_sha256=sha(out/entry.name), image=a.image,
                                     quarantine_readback=True, exact_seven_holds=True, exact_replay=True,
                                     wrapper_phases=['quarantine','bind','prepare','apply','verify','report','activate'],
                                     report_arguments_forwarded=True, checks=checks, production_writes=False))
    finally:
        for volume in volumes:
            run(['docker', 'volume', 'rm', volume])
            result = subprocess.run(['docker', 'volume', 'inspect', volume], capture_output=True)
            assert result.returncode != 0
        write(out/'cleanup.json', dict(removed=volumes, readback_absent=True))
    print(json.dumps(dict(passed=True, receipt=str(out/'result.json'))))


if __name__ == '__main__':
    main()
