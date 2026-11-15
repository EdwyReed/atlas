"""Stopped-owner final delta for a portable capture. No service control or replay.

The original portable manifest is reconstructed and checksum-verified, not trusted
from a writable target baseline. Only changed owned files and SQLite backup stage.
"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from . import adoption as ad, adoption_control as ctl, portable as pt, state_backup as sb
from .repository import dump, digest

FORMAT = 'atlas-portable-refresh-plan-v1'


def immutable_files(root):
    result = {}
    for name in ('portable-state.json','references','portable-media','vocabulary.sqlite3','adoption-receipt.json'):
        p = root/name
        if p.is_dir(): result.update({name+'/'+k:v for k,v in sb.files(p).items()})
        elif p.is_file(): result[name] = {'bytes':p.stat().st_size,'sha256':sb.sha(p)}
    return result


def capture_manifest(root, state, versions):
    # unpack retains the literal payload, but consumes its stream manifest. Rebuild
    # that manifest and REQUIRE its original hash; no operator-created baseline.
    inventory = {}
    for name in sorted(pt.STATE - {sb.MARKER}):
        p = root/name
        if p.is_dir(): inventory.update({name+'/'+k:v for k,v in sb.files(p).items()})
        elif p.is_file(): inventory[name] = {'bytes':p.stat().st_size,'sha256':sb.sha(p)}
    previous = state['previous_restore']
    if previous is not None:
        raw = (dump(previous)+'\n').encode()
        inventory[sb.MARKER] = {'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    with closing(ad.connection(root/'registry.sqlite3', closed=True)) as db:
        registry = ad.ledger(db)
    metadata = ctl.read(root/'portable-state.json')
    manifest = {'format':pt.FORMAT,'versions':versions,'source_root':metadata['source_root'],
                'previous_restore':previous,'files':inventory,'registry':registry,
                'collections':sb.points(root),'configuration_secrets_included':False,
                'consistency':'offline existing service/corpus locks; historical source qualifications retained'}
    if digest(manifest) != state['backup_manifest_sha256']:
        raise ValueError('capture manifest mismatch: target writes or wrong capture runtime; no refresh')
    return manifest


def validate_source(source, root, plan):
    if immutable_files(root) != plan['immutable_files']:
        raise ValueError('immutable portable files changed')
    if ad.source_inventory(source) != plan['source_inventory']:
        raise ValueError('explicit stopped source cut changed')
    if ctl.read(source/sb.MARKER) != plan['state']['previous_restore']:
        raise ValueError('current source marker differs from captured owner lineage')
    metadata = pt.References(root).validate().metadata
    if metadata != plan['portable_metadata'] or metadata['source_root'] != str(source):
        raise ValueError('portable current source binding differs')
    # Original historical paths remain literal and are checked, never redirected
    # to a historical adoption-receipt.plan.source_root.
    for old, pin in metadata['reference_files'].items():
        p = pt.sealed_file(old)
        if {'bytes':p.stat().st_size,'sha256':sb.sha(p)} != pin:
            raise ValueError('original historical reference drift')
    if (source/'attachment-owner.json').exists():
        if (source/'attachment-owner.json').read_bytes() != (root/'attachment-owner.json').read_bytes():
            raise ValueError('source historical attachment binding changed')
    ad.raw_collections(source)  # refuses uncheckpointed Qdrant; never loads it


def prepare(root, output, source, capture_versions, rebase_refresh_sha256=None, capture_manifest_sha256=None):
    root, source = sb.safe_path(root), sb.safe_path(source)
    if source == root or source in root.parents or root in source.parents:
        raise ValueError('overlapping refresh roots')
    output = sb.safe_path(output)
    if output.is_relative_to(root) or output.is_relative_to(source):
        raise ValueError('plan must be outside both roots')
    with sb.offline(source), sb.offline(root):
        state = sb.restore_state(root)
        if (not state or not state.get('portable_restore') or state['reconciled']
                or state.get('held_admission') or state.get('refresh_pending')):
            raise ValueError('completed never-activated portable quarantine required')
        before = ad.source_inventory(source)
        if capture_versions['qdrant-client'] != sb.versions()['qdrant-client']:
            raise ValueError('capture Qdrant version differs')
        if state.get('refresh'):
            if not rebase_refresh_sha256 or state['refresh']['sha256'] != rebase_refresh_sha256:
                raise ValueError('explicit previous refresh SHA256 required for rebase')
            # The reviewed previous cut pins its OWN historical executable. A
            # package-only successor necessarily has different source_modules.
            prior_cut = ctl.bound_record(state['refresh'])
            validate_final(root, state, expected_runtime=prior_cut['runtime'])
            previous = ctl.bound_record(state['portable_final_manifest'])
            original = previous['capture_manifest_sha256']
            if not capture_manifest_sha256 or original != capture_manifest_sha256:
                raise ValueError('rebase original capture manifest binding differs')
            manifest = {'registry':ctl.bound_record(state['refresh'])['registry']}
        else:
            if rebase_refresh_sha256:
                raise ValueError('rebase requires an existing completed final cut')
            manifest = capture_manifest(root, state, capture_versions)
            original = state['backup_manifest_sha256']
            if capture_manifest_sha256 and original != capture_manifest_sha256:
                raise ValueError('original capture manifest binding differs')
        pt.protected_rows(root,state)
        metadata = pt.References(root).validate().metadata
        if metadata['source_root'] != str(source) or str(source) not in state['storage_source_roots']:
            raise ValueError('explicit source must equal portable capture source')
        with closing(ad.connection(source/'registry.sqlite3',closed=True)) as db:
            schema = ad.schema(db)
        if schema != manifest['registry']['schema']:
            raise ValueError('source schema changed since capture')
        plan = {'format':FORMAT,'root':str(root),'state':state,'runtime':sb.versions(),
                'target_registry':manifest['registry'],'target_files':ctl.payload_files(root),
                'source_root':str(source),'source_inventory':before,'source_schema':schema,
                'portable_metadata':metadata,'capture_manifest':manifest,
                'original_capture_manifest_sha256':original,
                'rebase_refresh_sha256':rebase_refresh_sha256,
                'immutable_files':immutable_files(root),
                'capture_receipt_sha256':sb.sha(root/'adoption-receipt.json') if (root/'adoption-receipt.json').exists() else None}
        validate_source(source,root,plan)
        sb.write_json(output,plan)
    return {'prepared':True,'plan_sha256':sb.sha(output),'source_root':str(source)}


def review(root, staged):
    from .adoption_native import row_pins
    with closing(ad.connection(root/'registry.sqlite3',closed=True)) as old, closing(ad.connection(staged,closed=True)) as new:
        rows = old.execute('select id,kind,state from jobs order by id').fetchall()
        held = [r['id'] for r in pt.protected_rows(root)]
        for ident, kind, state in rows:
            current = new.execute('select state from jobs where id=?',(ident,)).fetchone()
            if current is None or (state in sb.TERMINAL and current[0] != state):
                raise ValueError('historical job missing or terminal state changed')
    pins = row_pins(root, held)
    # This cut deliberately preserves all seven uncertain holds exactly. No
    # evidence reconciliation is smuggled into the data movement operation.
    if row_pins(staged.parent, held) != pins:
        raise ValueError('historical held rows changed; separate review required')
    with closing(ad.connection(staged,closed=True)) as db:
        held = sorted(set(held)|{r[0] for r in db.execute('select id,kind,state from jobs')
                                if r[1] in pt.HISTORICAL_EFFECTS and r[2] not in sb.TERMINAL})
    return row_pins(staged.parent, held)


def seal(root, state, transition):
    previous = state.get('portable_final_manifest')
    original = (ctl.bound_record(previous)['capture_manifest_sha256'] if previous
                else state['backup_manifest_sha256'])
    manifest = {'format':'atlas-portable-final-cut-v1','target_root':str(root),
                'capture_manifest_sha256':original,
                'previous_final_manifest':previous,
                'portable_metadata_sha256':sb.sha(root/'portable-state.json'),
                'immutable_files':immutable_files(root),
                'runtime':sb.versions(),'cut':transition}
    state['previous_backup_manifest_sha256'] = state['backup_manifest_sha256']
    state['backup_manifest_sha256'] = digest(manifest)
    state['portable_final_manifest'] = ctl.record(root,'portable-final-manifest',manifest)
    return state


def validate_final(root, state, expected_runtime=None):
    if not state.get('portable_final_manifest'): return
    manifest = ctl.bound_record(state['portable_final_manifest'])
    cut = ctl.bound_record(state['refresh'])
    if (digest(manifest) != state['backup_manifest_sha256'] or manifest['cut'] != cut
            or manifest['runtime'] != (expected_runtime or sb.versions())
            or cut['runtime'] != manifest['runtime'] or manifest['target_root'] != str(root)
            or manifest['portable_metadata_sha256'] != sb.sha(root/'portable-state.json')
            or manifest['immutable_files'] != immutable_files(Path(root))
            or ctl.payload_files(root) != cut['files']):
        raise ValueError('portable final manifest/runtime/files differ')
    with closing(ad.connection(Path(root)/'registry.sqlite3',closed=True)) as db:
        if ad.ledger(db) != cut['registry']:
            raise ValueError('portable final ledger differs')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='action',required=True)
    prep=sub.add_parser('prepare');prep.add_argument('root',type=Path);prep.add_argument('output',type=Path)
    prep.add_argument('--source',type=Path,required=True);prep.add_argument('--capture-versions',type=Path,required=True)
    prep.add_argument('--rebase-refresh-sha256');prep.add_argument('--capture-manifest-sha256')
    apply=sub.add_parser('apply');apply.add_argument('root',type=Path);apply.add_argument('plan',type=Path);apply.add_argument('--sha256',required=True)
    a=p.parse_args()
    import os; os.umask(0o077)
    try:
        if a.action=='prepare':result=prepare(a.root,a.output,a.source,ctl.read(a.capture_versions),a.rebase_refresh_sha256,a.capture_manifest_sha256)
        else:result=ctl.refresh(a.root,a.plan,a.sha256)
        print(dump(result))
    except (ValueError,OSError,KeyError,sqlite3.Error) as e:p.exit(1,'Portable final refresh refused: '+str(e)+'\n')

if __name__=='__main__':main()
