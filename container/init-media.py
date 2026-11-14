"""One-shot namespace-root initialization on NEW operator-owned state/config volumes.

Mount media-kit weights at /inputs:ro. No network, system service or source host
paths are used. This is provisioning, never run on an active/nonempty media kit.
"""
import hashlib
import argparse
import json
import os
from pathlib import Path
import shutil


def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state-root',type=Path,default=Path('/var/lib/atlas'))
    p.add_argument('--config',type=Path,default=Path('/run/atlas/media.json'))
    p.add_argument('--restored',action='store_true',help='use only hash-bound weights in this restored root, never /inputs')
    a=p.parse_args()
    if a.restored:
        from atlas_engine.state_backup import offline
        with offline(a.state_root):provision(a)
    else:provision(a)


def provision(a):
    from atlas_engine.state_backup import safe_path, restore_state
    state=safe_path(a.state_root);root=state/'media-kit';config=safe_path(a.config)
    inputs=state/'portable-media'/'weights' if a.restored else Path('/inputs')
    pins=None
    if a.restored:
        receipt=restore_state(state)
        if not receipt or not receipt.get('portable_restore') or not receipt.get('preimage_verified'):
            raise ValueError('verified portable restore required')
        binding=json.loads((state/'portable-state.json').read_text()).get('media',{})
        if binding.get('format')!='atlas-container-media-weights-v1':raise ValueError('explicit archived media binding required')
        pins=binding['weights']
    if root.exists() or config.exists():raise ValueError('refuse existing media kit/config')
    for n in ('mobile_sam.pt','ntd11.pt'):
        source=safe_path(inputs/n)
        if not source.is_file():raise ValueError('explicit model files required')
        if pins and pins[n]!={'bytes':source.stat().st_size,'sha256':sha(source)}:raise ValueError('restored media model pin differs')
    if sha(inputs/'mobile_sam.pt')!='6dbb90523a35330fedd7f1d3dfc66f995213d81b29a5ca8108dbcdd4e37d6c2f':
        raise ValueError('MobileSAM hash differs')
    root.mkdir(parents=True,mode=0o700)
    # Refuse-on-retry leaves a visible incomplete kit after any copy failure.
    (root/'incomplete').touch()
    shutil.copytree('/opt/atlas-media/mask/atlas_mask',root/'mask'/'atlas_mask')
    (root/'weights').mkdir()
    weights={}
    for n in ('mobile_sam.pt','ntd11.pt'):
        source=inputs/n;target=root/'weights'/n;before=sha(source)
        shutil.copyfile(source,target)
        if sha(source)!=before or sha(target)!=before:raise ValueError('model changed during copy')
        weights[n]=before
    (root/'mask'/'backend-paths.json').write_text(json.dumps({'python':'/opt/atlas-media/venv/bin/python',
        'source':'/opt/atlas-media/mobile-sam','weights':str(root/'weights'/'mobile_sam.pt')}))
    config.write_text(json.dumps({'mask':{'root':str(root/'mask')},'censor':{
        'python':'/opt/atlas-media/venv/bin/python','upstream':'/opt/atlas-media/censor',
        'model':str(root/'weights'/'ntd11.pt'),'timeout_seconds':90}}))
    for folder,dirs,files in os.walk(root):
        os.chown(folder,10001,10001);os.chmod(folder,0o700)
        for n in files:
            p=Path(folder)/n;os.chown(p,10001,10001);os.chmod(p,0o600)
    os.chown(config,10001,10001);os.chmod(config,0o600)
    (root/'incomplete').unlink()
    (root/'models.json').write_text(json.dumps(weights));os.chown(root/'models.json',10001,10001)
    print(json.dumps({'provisioned':True,'models':weights,'host_dependencies':False}))


if __name__=='__main__':main()
