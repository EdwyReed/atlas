"""Pinned failed-provision operator wrapper. No service lifecycle operations.

A private versioned entrypoint calls main(lock_path). The input activation report
and expanded persisted admission are distinct, independently pinned documents.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

SOURCE = Path(__file__).absolute().parent.parent
EXPANDED = {'admission', 'held_rows', 'historical_native_ids', 'held_upload_ids',
            'portable_metadata_sha256'}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(args, **kw):
    return subprocess.run(list(map(str, args)), check=True, **kw)


def activation_binding(lock):
    """Verify exact document bytes AND the lossless accepted input projection."""
    refs = lock['failed_activation']
    values = {}
    for name in ('input', 'accepted_admission'):
        ref = refs[name]
        require(sha(ref['path']) == ref['sha256'], name+' checksum differs')
        require(lock['pins'].get(ref['path']) == ref['sha256'], name+' not release-pinned')
        values[name] = json.loads(Path(ref['path']).read_text())
    original, accepted = values['input'], values['accepted_admission']
    require(not EXPANDED.intersection(original), 'activation input shadows expanded fields')
    require(set(accepted) == set(original) | EXPANDED, 'accepted admission field set differs')
    require({k: v for k, v in accepted.items() if k not in EXPANDED} == original,
            'accepted admission is not the original input projection')
    cut = lock['cutover']
    require(original['target_root'] == cut['target_root'], 'activation target differs')
    require(original['refresh']['sha256'] == cut['rebase_refresh_sha256'], 'activation refresh differs')
    require(original['retain_historical_effects'] is True, 'historical effects not retained')
    require(accepted['admission'] == 'portable-restored-v1', 'admission contract differs')
    require(accepted['held_rows'] == cut['expected_held_rows'], 'accepted held rows differ')
    return refs, accepted


def recovery_binding(lock, report):
    refs, accepted = activation_binding(lock)
    cut = lock['cutover']
    require(report['activation_report_sha256'] == refs['accepted_admission']['sha256'],
            'recovery must bind expanded admission, not activation input')
    require(report['admission'] == refs['state_admission'], 'recovery admission reference differs')
    require(report['admission']['sha256'] == refs['accepted_admission']['sha256'], 'admission hash differs')
    require(report['state']['held_admission'] == report['admission'], 'state admission differs')
    require(report['refresh'] == accepted['refresh'] == report['state']['refresh'], 'recovery refresh differs')
    require(report['refresh']['sha256'] == cut['rebase_refresh_sha256'], 'failed refresh differs')
    require(report['original_capture_manifest_sha256'] == cut['capture_manifest_sha256'], 'capture differs')
    require(report['target_root'] == cut['target_root'], 'recovery target differs')


def main(lock_path):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('phase', choices=['check', 'render', 'quarantine', 'bind', 'prepare', 'apply',
                                   'verify', 'report', 'activate', 'media-plan', 'media-finalize'])
    p.add_argument('--flavor', choices=['core', 'media'], default='media')
    p.add_argument('--out', type=Path)
    p.add_argument('--binding', type=Path)
    p.add_argument('--report', type=Path)
    p.add_argument('--sha256')
    p.add_argument('--operator')
    p.add_argument('--assessment')
    a = p.parse_args()
    os.umask(0o077)
    lock = json.loads(Path(lock_path).read_text())
    require(subprocess.check_output(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD'], text=True).strip()
            == lock['commit'], 'source commit drift')
    require(not subprocess.check_output(['git', '-C', str(SOURCE), 'status', '--porcelain'], text=True).strip(),
            'dirty source')
    for path, pin in lock['pins'].items():
        require(sha(path) == pin, 'release input drift: '+path)
    activation_binding(lock)
    # Validate the report before any Docker invocation, even image/volume inspect.
    r = {}
    if a.phase == 'quarantine':
        require(a.report and a.report.is_absolute() and a.sha256 and sha(a.report) == a.sha256,
                'absolute checksum-reviewed recovery report required')
        r = json.loads(a.report.read_text())
        recovery_binding(lock, r)
    for image in lock['images'].values():
        require(json.loads(subprocess.check_output(['docker', 'image', 'inspect', image], text=True))[0]['Id']
                == image, 'image drift')
    if a.phase == 'check':
        print('PASS: pinned source, images, retained acceptance and both activation documents; no production data opened')
        return
    if a.phase == 'render':
        require(a.out and a.out.is_absolute() and not a.out.exists(), 'new absolute render output required')
        expected = json.loads(Path(lock['compose_base']).read_text())
        for name in ['atlas', 'atlas-ssh-agent']:
            expected['services'][name]['image'] = lock['images'][a.flavor]
        a.out.mkdir(mode=0o700, parents=True)
        raw = a.out/'input.json'
        raw.write_text(json.dumps(expected, indent=2))
        rendered = json.loads(subprocess.check_output(['docker', 'compose', '-f', str(raw), 'config', '--format', 'json'], text=True))
        require(rendered == expected, 'non-image Compose drift')
        (a.out/'compose.json').write_text(json.dumps(rendered, indent=2)+'\n')
        print('PASS: complete actual Compose preservation except two pinned images')
        return
    image, cut = lock['images']['media'], lock['cutover']
    if a.phase == 'bind':
        require(a.out and a.out.is_absolute() and not a.out.exists(), 'new absolute binding output required')
        run([sys.executable, '-I', '-B', SOURCE/'verification/portable_binding.py', '--image', image,
             '--volume', cut['destination_volume'], '--source-root', cut['source_root'],
             '--capture-manifest', cut['capture_manifest_sha256'], '--capture-python', cut['capture_python'],
             '--expected-held', str(cut['expected_held']), '--rebase-refresh-sha256', cut['rebase_refresh_sha256'], '--out', a.out])
        b = json.loads((a.out/'binding.json').read_text())
        require(b['expected_held_rows'] == cut['expected_held_rows'], 'bound held rows differ')
        return
    if a.phase in ['prepare', 'apply', 'verify', 'report', 'activate']:
        require(a.binding and a.binding.is_absolute(), 'absolute binding required')
        b = json.loads(a.binding.read_text())
        for k in ['destination_volume', 'source_root', 'target_root', 'capture_manifest_sha256',
                  'expected_held', 'expected_held_rows', 'rebase_refresh_sha256']:
            require(b[k] == cut[k], 'cut binding drift: '+k)
        require(b['image'] == image, 'binding image differs')
        args = [sys.executable, '-I', '-B', SOURCE/'verification/portable_cutover.py', a.phase, '--binding', a.binding]
        if a.phase in ['apply', 'activate']:
            require(a.sha256, 'reviewed SHA256 required')
            args += ['--sha256', a.sha256]
        if a.phase == 'report':
            require(a.operator and a.assessment, 'explicit operator and assessment required')
            args += ['--operator', a.operator, '--assessment', a.assessment]
        run(args)
        return
    require(a.report and a.report.is_absolute() and not a.report.is_relative_to(SOURCE),
            'absolute external report path required')
    run(['docker', 'volume', 'inspect', cut['destination_volume'], lock['config_volume']], stdout=subprocess.DEVNULL)
    common = ['docker', 'run', '--rm', '--network', 'none', '--read-only', '--user', '0:0',
              '--cap-drop', 'ALL', '--cap-add', 'DAC_OVERRIDE', '--cap-add', 'CHOWN',
              '--security-opt', 'no-new-privileges',
              '--mount', 'type=volume,src='+cut['destination_volume']+',dst=/var/lib/atlas',
              '--mount', 'type=volume,src='+lock['config_volume']+',dst=/run/atlas',
              '--mount', 'type=bind,src='+str(a.report.parent)+',dst='+str(a.report.parent)]
    if a.phase == 'quarantine':
        # Mount only the explicit evidence files; never broad private directories.
        for evidence in r['evidence']:
            ep = Path(evidence['path'])
            require(ep.is_absolute() and ep.is_file() and not ep.is_symlink(), 'explicit regular evidence required')
            if ep.parent != a.report.parent:
                common += ['--mount', 'type=bind,src='+str(ep)+',dst='+str(ep)+',readonly']
        run(common+['--entrypoint', 'python', image, '-I', '-B', '-m', 'atlas_engine.portable_recovery',
                    cut['target_root'], a.report, '--sha256', a.sha256])
        return
    args = ['/opt/atlas-media/init.py', '--restored', '--state-root', cut['target_root']]
    if a.phase == 'media-plan':
        require(not a.report.exists(), 'new media plan required')
        args += ['--prepare-recovery', str(a.report)]
    else:
        require(a.sha256 and sha(a.report) == a.sha256, 'reviewed media plan required')
        args += ['--recovery-plan', str(a.report), '--sha256', a.sha256]
    run(common+['--entrypoint', 'python', image, '-I', '-B']+args)
