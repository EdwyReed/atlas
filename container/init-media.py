"""Offline media provisioning with explicit checksum-reviewed partial recovery.

Default refuses every existing kit/config. Recovery only finalizes a completely
copied, byte-exact kit; it never deletes a kit or recopies/overwrites model bytes.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil


def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()


def pin(p):return {'bytes':p.stat().st_size,'sha256':sha(p)}


def expected(state):
    root=state/'media-kit'
    return json.dumps({'mask':{'root':str(root/'mask')},'censor':{
        'python':'/opt/atlas-media/venv/bin/python','upstream':'/opt/atlas-media/censor',
        'model':str(root/'weights'/'ntd11.pt'),'timeout_seconds':90}})


def validate_copy(state, config):
    from atlas_engine.state_backup import files
    root=state/'media-kit';inputs=state/'portable-media'/'weights'
    binding=json.loads((state/'portable-state.json').read_text()).get('media',{})
    if binding.get('format')!='atlas-container-media-weights-v1':raise ValueError('archived media binding required')
    models={}
    canonical={'mask/atlas_mask/'+n:v for n,v in files(Path('/opt/atlas-media/mask/atlas_mask')).items()}
    for n in ('mobile_sam.pt','ntd11.pt'):
        if binding['weights'][n]!=pin(inputs/n):raise ValueError('archived model pin differs')
        canonical['weights/'+n]=pin(inputs/n);models[n]=sha(inputs/n)
    backend=json.dumps({'python':'/opt/atlas-media/venv/bin/python','source':'/opt/atlas-media/mobile-sam',
                        'weights':str(root/'weights'/'mobile_sam.pt')}).encode()
    canonical['mask/backend-paths.json']={'bytes':len(backend),'sha256':hashlib.sha256(backend).hexdigest()}
    actual=files(root)
    # Exact complete copy only; arbitrary interrupted copies are safely refused.
    if actual.pop('incomplete',None)!={'bytes':0,'sha256':hashlib.sha256(b'').hexdigest()}:
        raise ValueError('incomplete marker required')
    if 'models.json' in actual:
        if json.loads((root/'models.json').read_text())!=models:raise ValueError('models receipt differs')
        actual.pop('models.json')
    if actual!=canonical or config.read_text()!=expected(state):raise ValueError('partial kit/config bytes differ')
    return models


def metadata(root,config):
    import stat
    from atlas_engine.state_backup import safe_path
    result={}
    for p in [root,*sorted(root.rglob('*')),config]:
        safe_path(p);s=p.lstat()
        if not (stat.S_ISREG(s.st_mode) or stat.S_ISDIR(s.st_mode)):raise ValueError('nonregular kit')
        result[str(p)]={'uid':s.st_uid,'gid':s.st_gid,'mode':stat.S_IMODE(s.st_mode)}
    return result


def finish(root, config, models):
    # CHOWN + DAC_OVERRIDE suffice. On reviewed resume take ownership first;
    # chmod cannot follow a handoff to UID10001 without forbidden FOWNER.
    (root/'models.json').write_text(json.dumps(models))
    for p in [root,*sorted(root.rglob('*')),config]:
        if p.stat().st_uid!=os.getuid():os.chown(p,os.getuid(),os.getgid())
        os.chmod(p,0o700 if p.is_dir() else 0o600)
        os.chown(p,10001,10001)
    from atlas_engine.state_backup import sync_tree
    sync_tree(root)
    (root/'incomplete').unlink()
    fd=os.open(root,os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def provision(a):
    from atlas_engine import state_backup as sb, adoption_control as ctl
    state=sb.safe_path(a.state_root);root=state/'media-kit';config=sb.safe_path(a.config)
    if a.prepare_recovery or a.recovery_plan:
        if not a.restored:raise ValueError('partial recovery requires restored archived weights')
        output=sb.safe_path(a.prepare_recovery or a.recovery_plan)
        if output.is_relative_to(state) or output.is_relative_to(config.parent):raise ValueError('external review plan required')
        if a.prepare_recovery:
            models=validate_copy(state,config)
            plan={'format':'atlas-media-finalize-plan-v1','state_root':str(state),'config':str(config),
                  'marker':sb.restore_state(state),'files':sb.files(root),'config_pin':pin(config),
                  'metadata':metadata(root,config),'models':models,'helper_sha256':sha(Path(__file__))}
            if output.exists():raise ValueError('new review plan required')
            sb.write_json(output,plan)
            return {'prepared':True,'plan_sha256':sha(output),'no_state_writes':True}
        if not a.sha256 or sha(output)!=a.sha256:raise ValueError('reviewed media recovery checksum required')
        plan=ctl.read(output)
        if (plan.get('format')!='atlas-media-finalize-plan-v1' or plan.get('state_root')!=str(state)
                or plan.get('config')!=str(config) or plan.get('marker')!=sb.restore_state(state)
                or plan.get('helper_sha256')!=sha(Path(__file__))):raise ValueError('media recovery binding differs')
        receipt=root.parent/'.adoption'/('media-finalize-'+a.sha256+'.json')
        if not (root/'incomplete').exists():
            if not receipt.exists():
                # Crash after marker unlink but before completion receipt. Only
                # the retained review AND exact canonical final bytes/modes admit
                # completing the receipt; never redo ownership or recopy weights.
                review=state/'.adoption'/('media-finalize-review-'+__import__('atlas_engine.repository',fromlist=['digest']).digest({'plan':plan,'plan_sha256':a.sha256})+'.json')
                if not review.exists() or ctl.read(review)!={'plan':plan,'plan_sha256':a.sha256}:raise ValueError('no retained media review')
                wanted=dict(plan['files']);wanted.pop('incomplete',None)
                raw=json.dumps(plan['models']).encode();wanted['models.json']={'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
                current=metadata(root,config)
                if sb.files(root)!=wanted or pin(config)!=plan['config_pin'] or any(v!={'uid':10001,'gid':10001,'mode':0o700 if Path(k).is_dir() else 0o600} for k,v in current.items()):raise ValueError('incomplete final receipt bytes/modes differ')
                ctl.atomic(receipt,{'plan_sha256':a.sha256,'files':wanted,'config_pin':pin(config),'metadata':current})
            saved=ctl.read(receipt)
            if saved['plan_sha256']!=a.sha256 or saved['files']!=sb.files(root) or saved['config_pin']!=pin(config) or saved['metadata']!=metadata(root,config):
                raise ValueError('completed media recovery changed')
            return {'provisioned':True,'replayed':True}
        models=validate_copy(state,config)
        actual=sb.files(root);prior=dict(plan['files'])
        # models.json may have been durably added just before interruption.
        actual.pop('models.json',None);prior.pop('models.json',None)
        if actual!=prior or pin(config)!=plan['config_pin'] or models!=plan['models']:raise ValueError('reviewed partial bytes changed')
        current=metadata(root,config)
        for path,value in current.items():
            original=plan['metadata'].get(path)
            if original is None and Path(path).name!='models.json':raise ValueError('unreviewed directory')
            if value['uid'] not in (0,10001) or value['gid'] not in (0,10001):raise ValueError('foreign kit owner')
            if original and value['mode'] not in (original['mode'],0o700 if Path(path).is_dir() else 0o600):raise ValueError('unreviewed mode')
        # Journal before mutation retains the original mixed ownership/modes.
        ctl.record(state,'media-finalize-review',{'plan':plan,'plan_sha256':a.sha256})
        finish(root,config,models)
        ctl.atomic(receipt,{'plan_sha256':a.sha256,'files':sb.files(root),'config_pin':pin(config),'metadata':metadata(root,config)})
        return {'provisioned':True,'recovered':True,'receipt':str(receipt),'models':models}
    inputs=state/'portable-media'/'weights' if a.restored else Path('/inputs')
    pins=None
    if a.restored:
        receipt=sb.restore_state(state)
        if not receipt or not receipt.get('portable_restore') or not receipt.get('preimage_verified'):raise ValueError('verified portable restore required')
        binding=json.loads((state/'portable-state.json').read_text()).get('media',{})
        if binding.get('format')!='atlas-container-media-weights-v1':raise ValueError('explicit archived media binding required')
        pins=binding['weights']
    if root.exists() or config.exists():raise ValueError('refuse existing media kit/config; explicit reviewed recovery required')
    for n in ('mobile_sam.pt','ntd11.pt'):
        source=sb.safe_path(inputs/n)
        if not source.is_file():raise ValueError('explicit model files required')
        if pins and pins[n]!=pin(source):raise ValueError('restored media model pin differs')
    if sha(inputs/'mobile_sam.pt')!='6dbb90523a35330fedd7f1d3dfc66f995213d81b29a5ca8108dbcdd4e37d6c2f':raise ValueError('MobileSAM hash differs')
    root.mkdir(parents=True,mode=0o700);(root/'incomplete').touch()
    shutil.copytree('/opt/atlas-media/mask/atlas_mask',root/'mask'/'atlas_mask')
    (root/'weights').mkdir();models={}
    for n in ('mobile_sam.pt','ntd11.pt'):
        source=inputs/n;target=root/'weights'/n;before=sha(source);shutil.copyfile(source,target)
        if sha(source)!=before or sha(target)!=before:raise ValueError('model changed during copy')
        models[n]=before
    (root/'mask'/'backend-paths.json').write_text(json.dumps({'python':'/opt/atlas-media/venv/bin/python',
        'source':'/opt/atlas-media/mobile-sam','weights':str(root/'weights'/'mobile_sam.pt')}))
    config.write_text(expected(state));finish(root,config,models)
    return {'provisioned':True,'models':models,'host_dependencies':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state-root',type=Path,default=Path('/var/lib/atlas'))
    p.add_argument('--config',type=Path,default=Path('/run/atlas/media.json'))
    p.add_argument('--restored',action='store_true')
    g=p.add_mutually_exclusive_group();g.add_argument('--prepare-recovery',type=Path);g.add_argument('--recovery-plan',type=Path)
    p.add_argument('--sha256');a=p.parse_args();os.umask(0o077)
    if a.restored:
        from atlas_engine.state_backup import offline
        with offline(a.state_root):result=provision(a)
    else:result=provision(a)
    print(json.dumps(result))


if __name__=='__main__':main()
