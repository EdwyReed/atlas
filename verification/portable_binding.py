"""Render a cutover binding from the selected quarantine; no state/data writes.

Reads only portable metadata and restore marker from a read-only volume. Does not
open SQLite/Qdrant, stop/start services, contact upstreams or activate anything.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


def run(args):return subprocess.run(list(map(str,args)),check=True,capture_output=True,text=True).stdout.strip()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('volume','source-root','capture-manifest','capture-python','image','out'):
        p.add_argument('--'+name,required=True)
    p.add_argument('--expected-held',type=int,required=True)
    a=p.parse_args();os.umask(0o077)
    if not re.fullmatch('sha256:[0-9a-f]{64}',a.image):raise ValueError('immutable image required')
    if not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]+',a.volume):raise ValueError('named volume required')
    if not re.fullmatch('[0-9a-f]{64}',a.capture_manifest):raise ValueError('capture manifest SHA256 required')
    source=Path(a.source_root);out=Path(a.out);capture=Path(a.capture_python)
    if not all(x.is_absolute() and '..' not in x.parts for x in (source,out,capture)):raise ValueError('absolute safe paths required')
    if out.is_relative_to(source) or a.expected_held<0:raise ValueError('external evidence and explicit held count required')
    run(['docker','volume','inspect',a.volume])  # Never implicitly create a typo volume.
    probe='''import json
from pathlib import Path
from atlas_engine.state_backup import MARKER
root=Path('/var/lib/atlas/state')
print(json.dumps(dict(metadata=json.loads((root/'portable-state.json').read_text()),state=json.loads((root/MARKER).read_text()))))
'''
    observed=json.loads(run(['docker','run','--rm','--network','none','--read-only','--user','0:0',
        '--cap-drop','ALL','--cap-add','DAC_OVERRIDE','--security-opt','no-new-privileges',
        '--mount','type=volume,src='+a.volume+',dst=/var/lib/atlas,readonly',
        '--entrypoint','python',a.image,'-I','-B','-c',probe]))
    state=observed['state'];metadata=observed['metadata']
    if (state['backup_manifest_sha256']!=a.capture_manifest or not state.get('portable_restore')
            or state.get('reconciled') or state.get('refresh') or state.get('held_admission')
            or metadata['source_root']!=str(source)):
        raise ValueError('not selected original portable quarantine/source')
    versions=run([capture,'-I','-B','-c','from atlas_engine.state_backup import versions;from atlas_engine.repository import dump;print(dump(versions()))'])+'\n'
    out.mkdir(mode=0o700,parents=True,exist_ok=False)
    version_path=out/'capture-versions.json';version_path.write_text(versions)
    binding={'image':a.image,'destination_volume':a.volume,'source_root':str(source),
        'target_root':'/var/lib/atlas/state','evidence_dir':str(out),
        'capture_versions':str(version_path),'capture_versions_sha256':hashlib.sha256(versions.encode()).hexdigest(),
        'capture_manifest_sha256':a.capture_manifest,'expected_held':a.expected_held,
        'historical_files':[{'path':path,**pin} for path,pin in sorted(metadata['reference_files'].items())]}
    (out/'binding.json').write_text(json.dumps(binding,indent=2)+'\n')
    print(json.dumps({'rendered':True,'binding':str(out/'binding.json'),'state_writes':False}))


if __name__=='__main__':main()
