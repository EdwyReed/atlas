"""Offline release parity; never opens Engine, data, credentials or a network.

Checks complete package file sets (excluding generated Python bytecode only),
not a handpicked module list. Containers have no host mounts or backend bindings.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile


def sha(data):
    return hashlib.sha256(data).hexdigest()


def inventory(root):
    return {str(p.relative_to(root)): sha(p.read_bytes())
            for p in sorted(root.rglob('*')) if p.is_file()
            and '__pycache__' not in p.parts and p.suffix != '.pyc'}


def run(args):
    return subprocess.run(args, check=True, text=True, capture_output=True,
                          timeout=120).stdout.strip()


PROBE = '''import hashlib,json,importlib.metadata as m
from pathlib import Path
import atlas_engine
root=Path(atlas_engine.__file__).parent
files={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
       for p in sorted(root.rglob('*')) if p.is_file()
       and '__pycache__' not in p.parts and p.suffix!='.pyc'}
print(json.dumps(dict(root=str(root),version=m.version('atlas-engine'),files=files)))
'''


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--build-lib', type=Path, required=True)
    p.add_argument('--wheel', type=Path, required=True)
    p.add_argument('--python', required=True, help='absolute isolated install interpreter')
    p.add_argument('--image', action='append', required=True)
    p.add_argument('--transport-receipt', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    if a.out.exists():
        p.error('output exists; preserve earlier receipts')
    assert Path(a.python).is_absolute()
    source = inventory(a.source / 'atlas_engine')
    build = inventory(a.build_lib / 'atlas_engine')
    with zipfile.ZipFile(a.wheel) as z:
        wheel = {n.removeprefix('atlas_engine/'): sha(z.read(n))
                 for n in z.namelist() if n.startswith('atlas_engine/') and not n.endswith('/')}
    assert source and source == build == wheel, 'source/build/wheel file-set or byte mismatch'
    installed = json.loads(run([a.python, '-I', '-B', '-c', PROBE]))
    assert installed['files'] == source and installed['version'] == '0.4.0.dev19'
    assert Path(installed['root']).is_relative_to(Path(a.python).parent.parent), 'not isolated install'
    transport = json.loads(a.transport_receipt.read_text())
    assert transport['ok'] and all(transport['cleanup'].values())
    receipt: dict = dict(equal=True, file_count=len(source), files=source,
                   build_lib=str(a.build_lib), wheel=str(a.wheel),
                   wheel_sha256=sha(a.wheel.read_bytes()), installed=installed,
                   transport_receipt=str(a.transport_receipt),
                   transport_receipt_sha256=sha(a.transport_receipt.read_bytes()), images={})
    expected_ids = {v['image_id'] for v in transport['images'].values()}
    seen = set()
    for image in a.image:
        info = json.loads(run(['docker', 'image', 'inspect', image]))[0]
        image_id = info['Id']
        assert image_id in expected_ids and image_id not in seen
        seen.add(image_id)
        args = ['docker', 'run', '--rm', '--network', 'none', '--read-only',
                '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--tmpfs', '/var/lib/atlas:rw,noexec,nosuid,size=1m',
                '--entrypoint', 'python', image_id, '-I', '-B', '-c']
        observed = json.loads(run(args + [PROBE]))
        assert observed['files'] == source and observed['version'] == '0.4.0.dev19'
        helper_probe = '''import hashlib,json
from pathlib import Path
paths=['/opt/atlas/ssh-agent.py','/opt/atlas-media/init.py']
wheels=list(Path('/opt/atlas/ssh-release').glob('*.whl'))
print(json.dumps(dict(helpers={p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths if Path(p).is_file()},wheels={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in wheels})))
'''
        extras = json.loads(run(args + [helper_probe]))
        assert extras['helpers']['/opt/atlas/ssh-agent.py'] == sha((a.source / 'container/ssh-agent.py').read_bytes())
        if '/opt/atlas-media/init.py' in extras['helpers']:
            assert extras['helpers']['/opt/atlas-media/init.py'] == sha((a.source / 'container/init-media.py').read_bytes())
        assert extras['wheels'] == {a.wheel.name: receipt['wheel_sha256']}
        receipt['images'][image] = dict(id=image_id, user=info['Config']['User'],
                                       package=observed, **extras)
        assert info['Config']['User'] == '10001:10001'
    assert seen == expected_ids, 'BOTH transport-accepted images required'
    receipt['scope'] = 'all non-bytecode package files; exact names and SHA256; no state opens or transport rerun'
    a.out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
    print(json.dumps(dict(equal=True, file_count=len(source), wheel_sha256=receipt['wheel_sha256'],
                          image_ids=sorted(seen), receipt=str(a.out)), indent=2))


if __name__ == '__main__':
    main()
