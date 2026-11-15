"""Offline, evidence-bound transitions for a captured canonical root.

Original adoption receipts are immutable. No service control or remote I/O.
"""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import zipfile

from . import state_backup as sb
from . import adoption as ad
from .repository import dump, digest

CONTROL = '.adoption'


def read(path):
    return json.loads(sb.safe_path(path).read_text())


def atomic(path, value):
    temp = path.with_name(path.name + '.new')
    temp.unlink(missing_ok=True)
    sb.write_json(temp, value)
    temp.replace(path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def record(root, name, value):
    folder = root / CONTROL
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / (name + '-' + digest(value) + '.json')
    if path.exists():
        if read(path) != value:
            raise ValueError('transition record collision')
    else:
        sb.write_json(path, value)
    return {'path': str(path), 'sha256': sb.sha(path)}


def bound_record(ref):
    path = sb.safe_path(ref['path'])
    if sb.sha(path) != ref['sha256']:
        raise ValueError('transition evidence changed')
    return read(path)


def current_versions(root, state, receipt):
    expected = receipt['plan']['target_versions']
    transitions = []
    ref = state.get('runtime_upgrade')
    seen = set()
    while ref:
        if ref['sha256'] in seen:raise ValueError('cyclic runtime transition')
        seen.add(ref['sha256'])
        transition = bound_record(ref)
        transitions.append(transition)
        ref = transition.get('previous_upgrade')
    for transition in reversed(transitions):
        if (transition['receipt_sha256'] != sb.sha(root/'adoption-receipt.json')
                or transition['from_versions'] != expected
                or transition['target_root'] != str(root)):
            raise ValueError('upgrade chain does not bind original capture')
        expected = transition['to_versions']
    return expected


def upgrade(root, report_path, report_sha256):
    root = sb.safe_path(root)
    if sb.sha(sb.safe_path(report_path)) != report_sha256:
        raise ValueError('upgrade report checksum differs')
    report = read(report_path)
    with sb.offline(root):
        state = read(root/sb.MARKER)
        receipt = read(root/'adoption-receipt.json')
        if (state.get('migration_kind') != ad.KIND or not state['preimage_verified']
                or sb.sha(root/'adoption-receipt.json') != state['adoption_receipt_sha256']
                or state.get('runtime_upgrade') or state['reconciled']):
            raise ValueError('original completed quarantined capture required')
        if (report.get('target_root') != str(root)
                or report.get('receipt_sha256') != state['adoption_receipt_sha256']
                or report.get('from_versions') != receipt['plan']['target_versions']
                or report.get('to_versions') != sb.versions()
                or report.get('registry_logical_sha256') != sb.registry_digest(root)
                or not report.get('operator') or not report.get('reason')):
            raise ValueError('upgrade must bind known runtime/capture/current ledger/operator')
        # Code artifact is separately bound, not an editable runtime-version label.
        artifact = bound_record(report['artifact'])
        if artifact['modules'] != sb.versions()['source_modules']:
            raise ValueError('artifact modules differ from executing package')
        wheel = sb.safe_path(artifact['wheel'])
        if sb.sha(wheel) != artifact['wheel_sha256']:
            raise ValueError('upgrade wheel changed')
        import hashlib
        with zipfile.ZipFile(wheel) as archive:
            modules = {n.split('/')[-1]:hashlib.sha256(archive.read(n)).hexdigest()
                       for n in archive.namelist() if n.startswith('atlas_engine/') and n.endswith('.py')}
        if modules != sb.versions()['source_modules']:
            raise ValueError('upgrade wheel modules differ from executing package')
        state['runtime_upgrade'] = record(root, 'upgrade', report)
        atomic(root/sb.MARKER, state)
        return {'upgraded': True, 'quarantined': True, 'transition': state['runtime_upgrade']}


def unknown_rows(root):
    with closing(ad.connection(root/'registry.sqlite3')) as db:
        db.row_factory = sqlite3.Row
        rows = []
        for row in db.execute('select * from jobs order by id'):
            if row['kind'] in sb.EFFECTS and row['state'] not in sb.TERMINAL:
                rows.append({'id': row['id'], 'kind': row['kind'], 'state': row['state'],
                             'row_sha256': digest(dict(row))})
        if db.execute("select 1 from uploads where state in ('importing','ack_unknown') limit 1").fetchone():
            raise ValueError('uncertain native uploads require separate reconciliation')
        return rows


def activate_holds(root, report_path, report_sha256):
    root = sb.safe_path(root)
    if sb.sha(sb.safe_path(report_path)) != report_sha256:
        raise ValueError('held-intent report checksum differs')
    report = read(report_path)
    with sb.offline(root):
        state = sb.restore_state(root)
        if not state or state.get('migration_kind') != ad.KIND or state['reconciled']:
            raise ValueError('completed canonical quarantine required')
        unknown = unknown_rows(root)
        if any(row['kind'] not in ('generation','native_admin') for row in unknown):
            raise ValueError('local-only holds currently support generation/admin uncertainty only')
        if (report.get('target_root') != str(root)
                or report.get('capture_receipt_sha256') != state['adoption_receipt_sha256']
                or report.get('refresh') != state.get('refresh')
                or report.get('registry_logical_sha256') != sb.registry_digest(root)
                or report.get('runtime') != sb.versions()
                or report.get('held_rows') != unknown
                or report.get('admission') != 'local-only-v1'
                or not report.get('operator')):
            raise ValueError('holds must bind exact current cut/runtime/unknown row set/operator')
        for key in ('generation', 'administration', 'native_identity', 'media', 'replay_policy'):
            if not isinstance(report.get(key), str) or not report[key].strip():
                raise ValueError('explicit evidence assessment required: ' + key)
        if not report.get('evidence'):
            raise ValueError('retained evidence files required')
        ad.check_pins(report['evidence'])
        state.update(reconciled=True, held_admission=record(root, 'holds', report))
        atomic(root/sb.MARKER, state)
        return {'activated': True, 'admission': 'local-only-v1', 'held': unknown}


def runtime_transition(root, report_path, report_sha256):
    """Append an audited successor transition without rewriting old receipts."""
    root=sb.safe_path(root)
    if sb.sha(sb.safe_path(report_path)) != report_sha256:raise ValueError('runtime report checksum differs')
    report=read(report_path)
    with sb.offline(root):
        state=read(root/sb.MARKER);receipt=read(root/'adoption-receipt.json')
        if (not state.get('preimage_verified') or state.get('migration_kind') != ad.KIND
                or sb.sha(root/'adoption-receipt.json') != state['adoption_receipt_sha256']):
            raise ValueError('verified original capture required')
        origin=current_versions(root,state,receipt)
        if (report.get('previous_state') != state
                or report.get('previous_upgrade') != state.get('runtime_upgrade')
                or report.get('target_root') != str(root)
                or report.get('receipt_sha256') != state['adoption_receipt_sha256']
                or report.get('from_versions') != origin or report.get('to_versions') != sb.versions()
                or ad.runtime(report['origin_python']) != origin
                or report.get('registry_logical_sha256') != sb.registry_digest(root)
                or not report.get('operator') or not report.get('reason')):
            raise ValueError('runtime transition must bind accepted state/old runtime/new runtime/ledger')
        if state.get('held_admission'):
            held_state(root,state,runtime=origin)
        artifact=bound_record(report['artifact'])
        import hashlib
        wheel=sb.safe_path(artifact['wheel'])
        if sb.sha(wheel) != artifact['wheel_sha256']:raise ValueError('successor wheel changed')
        with zipfile.ZipFile(wheel) as z:
            modules={n.split('/')[-1]:hashlib.sha256(z.read(n)).hexdigest() for n in z.namelist() if n.startswith('atlas_engine/') and n.endswith('.py')}
        if modules != artifact['runtime']['source_modules'] or modules != sb.versions()['source_modules']:
            raise ValueError('successor executing modules differ from wheel')
        state['runtime_upgrade']=record(root,'runtime-transition',report)
        atomic(root/sb.MARKER,state)
        return {'upgraded':True,'transition':state['runtime_upgrade'],'admission_preserved':state.get('held_admission')}


def held_state(root, state, runtime=None):
    if not state or not state.get('held_admission'):
        return None
    report = bound_record(state['held_admission'])
    from . import portable
    if report['admission'] == portable.ADMISSION:
        return portable.validate_admission(root,state,report)
    from . import adoption_native as native
    if report['admission'] == native.ADMISSION:
        return native.validate(root,state,report,runtime=runtime)
    expected_runtime=runtime or sb.versions()
    if report['runtime'] != expected_runtime:
        transition=bound_record(state['runtime_upgrade']) if state.get('runtime_upgrade') else {}
        if (transition.get('previous_state',{}).get('held_admission') != state['held_admission']
                or transition.get('from_versions') != report['runtime']
                or transition.get('to_versions') != expected_runtime):
            raise ValueError('held admission runtime transition differs')
    if (report['target_root'] != str(root)
            or report['capture_receipt_sha256'] != state['adoption_receipt_sha256']
            or report.get('refresh') != state.get('refresh')
            or report['admission'] != 'local-only-v1'):
        raise ValueError('held admission binding differs')
    # Job row equality survives unrelated local activity and process restarts.
    actual = [r for r in unknown_rows(Path(root)) if r['kind'] not in ('mask','censor')]
    if actual != report['held_rows']:
        raise ValueError('held row set changed; offline evidence reconciliation required')
    return report


LOCAL = sb.READS | {'legacy.request','search','evidence','enrich','dream.submit','dream.cancel',
    'generation.wait','feedback.record','feedback.list','review.prepare','review.record',
    'loras.create','loras.list','loras.update','loras.delete','loras.history',
    'generation.records.create','generation.records.list','generation.records.update',
    'generation.records.delete','generation.records.history','corpus.put','corpus.delete',
    'corpus.restore','corpus.import','corpus.import.resume','projection.get','projection.page'}


def guard_local(engine, op, p, principal):
    from .contracts import Fault
    # Exact old payload comparison precedes a refusal, but never dispatches again.
    kinds = {'generation.submit':'generation','generation.submitGraph':'generation',
             'workflows.run':'generation','batch.submit':'batch','projects.savePrompt':'native_workspace'}
    if op in kinds:
        kind = kinds[op]
        intent = {'operation':op,**p} if kind == 'native_workspace' else p
        previous = engine.repo.request(principal,p['request_id'],kind,intent)
        if previous is not None:
            return previous
    elif p.get('request_id'):
        with engine.repo.connect() as db:
            row=db.execute('select kind from jobs where principal=? and request_id=?',(principal,p['request_id'])).fetchone()
        if row and row['kind'] == 'native_admin':
            return engine.repo.request(principal,p['request_id'],'native_admin',{'operation':op,**p})
    if op == 'batch.get':
        return engine.require_kind(p['job_id'],'batch')  # no aggregate mutation
    if op in ('enrich','dream.submit') and p.get('model'):
        raise Fault('HELD_NATIVE_RESOURCE','model-dependent admission lacks proven backend identity')
    if op in ('search','evidence') and p.get('mode','literal') != 'literal':
        raise Fault('HELD_NATIVE_RESOURCE','only literal reference reads admitted')
    if op.startswith(('mask.','censor.','media.upload.')) or op in LOCAL:
        return None
    raise Fault('HELD_NATIVE_EFFECT','historical unknowns retained; this cut admits local CPU/corpus/media only')


def payload_files(root):
    result = {}
    for name in sorted(ad.PAYLOAD - {'connection-binding.json','connection-bindings.json'}):
        path = root/name
        if path.is_dir():
            result.update({name+'/'+k: v for k, v in sb.files(path).items()})
        elif path.exists():
            result[name] = {'bytes': path.stat().st_size, 'sha256': sb.sha(path)}
    return result


def registry_snapshot(root, dest):
    with closing(ad.connection(root/'registry.sqlite3', closed=True)) as source, closing(sqlite3.connect(dest)) as target:
        source.execute('BEGIN')
        source.backup(target)
        target.execute('pragma journal_mode=DELETE')
        result = ad.ledger(target)
        if target.execute('pragma integrity_check').fetchall() != [('ok',)]:
            raise ValueError('registry integrity failure')
    with dest.open('rb') as handle:
        os.fsync(handle.fileno())
    return result


def refresh_prepare(root, output):
    root = sb.safe_path(root)
    with sb.offline(root):
        state = sb.restore_state(root)
        if not state or state.get('migration_kind') != ad.KIND:
            raise ValueError('canonical root required')
        receipt = read(root/'adoption-receipt.json')
        # Only a captured ledger plus its additive identity index may be replaced.
        # User/acceptance writes conflict; acceptance archive must be closed first.
        with closing(ad.connection(root/'registry.sqlite3')) as db:
            target = ad.ledger(db)
        original = receipt['registry']
        if state.get('refresh'):
            original = bound_record(state['refresh'])['registry']
        tables = {k:v for k,v in target['tables'].items() if k != 'scoped_observations'}
        original_tables = {k:v for k,v in original['tables'].items() if k != 'scoped_observations'}
        if tables != original_tables:
            raise ValueError('target has durable writes beyond adopted identity metadata')
        def original_schema(rows):
            return [r for r in rows if r[2] != 'scoped_observations' and r[1] not in ('uploads_native_name','uploads_scoped_native_name')]
        if original_schema(target['schema']) != original_schema(original['schema']):
            raise ValueError('target schema changed beyond adopted identity metadata')
        upload_indexes = [r for r in target['schema'] if r[1] in ('uploads_native_name','uploads_scoped_native_name')]
        expected_upload = ['index','uploads_scoped_native_name','uploads',"CREATE UNIQUE INDEX uploads_scoped_native_name ON uploads(json_extract(data,'$.import_intent.connection_id'),json_extract(data,'$.import_intent.backend_generation'),json_extract(data,'$.image_name')) WHERE state='imported'"]
        if upload_indexes not in ([expected_upload], [r for r in original['schema'] if r[1] in ('uploads_native_name','uploads_scoped_native_name')]):
            raise ValueError('unreviewed upload index migration')
        value = {'format': 'atlas-refresh-plan-v1', 'root': str(root),
                 'state': state, 'target_registry': target, 'target_files': payload_files(root),
                 'runtime': sb.versions(), 'capture_receipt_sha256': sb.sha(root/'adoption-receipt.json'),
                 'source_root': receipt['plan']['source_root'],
                 'source_versions': receipt['plan']['source_versions'],
                 'source_python': receipt['plan']['source_python'],
                 'source_schema': receipt['plan']['source_schema'],
                 'external_dependencies': receipt['plan']['external_dependencies']}
        sb.write_json(Path(output), value)
        return {'prepared': True, 'plan_sha256': sb.sha(Path(output))}


def refresh(root, plan_path, plan_sha256, after_apply=None):
    root = sb.safe_path(root)
    if sb.sha(sb.safe_path(plan_path)) != plan_sha256:
        raise ValueError('refresh plan checksum differs')
    plan = read(plan_path)
    if plan['root'] != str(root) or plan['runtime'] != sb.versions():
        raise ValueError('refresh target/runtime differs')
    source = sb.safe_path(plan['source_root'])
    if source == root or source in root.parents or root in source.parents:
        raise ValueError('overlapping refresh roots')
    with sb.offline(source), sb.offline(root):
        from . import portable_refresh as pr
        portable_cut = plan.get('format') == pr.FORMAT
        if portable_cut:
            pr.validate_source(source, root, plan)
        else:
            if ad.runtime(plan['source_python']) != plan['source_versions']:
                raise ValueError('source runtime drift')
            ad.check_pins(plan['external_dependencies'])
        state = read(root/sb.MARKER)
        stage = root/CONTROL/('refresh-'+plan_sha256)
        pending = stage/'pending.json'
        if not pending.exists():
            receipt_sha = sb.sha(root/'adoption-receipt.json') if (root/'adoption-receipt.json').exists() else None
            if state != plan['state'] or receipt_sha != plan['capture_receipt_sha256']:
                raise ValueError('target marker/receipt drift')
            with closing(ad.connection(root/'registry.sqlite3')) as db:
                if ad.ledger(db) != plan['target_registry']:
                    raise ValueError('target ledger writes conflict')
            if payload_files(root) != plan['target_files']:
                raise ValueError('target file writes conflict')
            before = ad.source_inventory(source)
            desired = payload_files(source)
            with closing(ad.connection(source/'registry.sqlite3', closed=True)) as db:
                if ad.schema(db) != plan['source_schema']:
                    raise ValueError('source schema drift')
            stage.mkdir(parents=True, mode=0o700)
            registry = registry_snapshot(source, stage/'registry.sqlite3')
            portable_holds = pr.review(root, stage/'registry.sqlite3') if portable_cut else None
            # The final source ledger is authoritative, including progressed held
            # rows, events and result links. Preserve old receipts OUTSIDE jobs.
            preserved_holds=state.get('held_admission') or state.get('preserved_holds')
            held_review=[]
            if preserved_holds:
                holds=bound_record(preserved_holds)
                from .adoption_native import row_pins, review_progress
                if row_pins(root,[r['id'] for r in holds['held_rows']]) != holds['held_rows']:
                    raise ValueError('refresh historical holds changed')
                held_review=review_progress(root/'registry.sqlite3',stage/'registry.sqlite3',holds['held_rows'])
            changes = {k:v for k,v in desired.items() if plan['target_files'].get(k) != v}
            removed = sorted(set(plan['target_files'])-set(desired))
            if portable_cut and any(k.startswith(('artifacts/', 'media/')) for k in removed):
                raise ValueError('historical owned files missing from current source')
            need = (2*sum(v['bytes'] for v in changes.values())
                    + max([v['bytes'] for v in changes.values()] + [0])
                    + (stage/'registry.sqlite3').stat().st_size)
            if shutil.disk_usage(root).free < need+128*1024**2:
                raise ValueError('insufficient delta staging space')
            for rel in changes:
                dest = stage/'files'/rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source/rel, dest)
                if ad.pin(dest)['sha256'] != changes[rel]['sha256']:
                    raise ValueError('changed source file during stage')
            if ad.source_inventory(source) != before:
                raise ValueError('source changed during staging')
            transition = {'format': 'atlas-refresh-cut-v1', 'plan_sha256': plan_sha256,
                          'target_root': str(root), 'source_inventory': before,
                          'registry': registry, 'files': desired, 'changes': changes,
                          'preserved_holds':preserved_holds,'held_review':held_review,
                          'portable_held_rows':portable_holds,
                          'capacity':{'additional_budget_after_registry':need,
                                      'reserve':128*1024**2,
                                      'free_after_registry':shutil.disk_usage(root).free},
                          'removed': removed, 'runtime': sb.versions(),
                          'registry_file_sha256': sb.sha(stage/'registry.sqlite3')}
            sb.sync_tree(stage)
            sb.write_json(pending, transition)
            sb.sync_tree(stage)
        else:
            transition = read(pending)
            if transition['plan_sha256'] != plan_sha256:
                raise ValueError('pending refresh plan drift')
            incomplete = dict(plan['state'])
            incomplete.update(preimage_verified=False,reconciled=False,refresh_pending=plan_sha256)
            incomplete.pop('held_admission',None)
            if state not in (plan['state'],incomplete):
                raise ValueError('interrupted target marker writes conflict')
            if ad.source_inventory(source) != transition['source_inventory']:
                raise ValueError('source drift after interruption; preserve fenced source cut')
            # Resume only expected old/new file bytes, never overwrite foreign writes.
            actual = payload_files(root)
            for rel in set(actual)|set(plan['target_files'])|set(transition['files']):
                if actual.get(rel) not in (plan['target_files'].get(rel), transition['files'].get(rel)):
                    raise ValueError('interrupted target file writes conflict')
            with closing(ad.connection(root/'registry.sqlite3')) as db:
                if ad.ledger(db) not in (plan['target_registry'], transition['registry']):
                    raise ValueError('interrupted target ledger writes conflict')
        if sb.sha(stage/'registry.sqlite3') != transition['registry_file_sha256']:
            raise ValueError('staged registry changed')
        # Reject pending cuts from the superseded stale-jobs implementation.
        if 'source_registry' in transition:
            raise ValueError('superseded refresh cut; preserve evidence and prepare a reviewed new cut')
        state = dict(plan['state'])
        state.update(preimage_verified=False, reconciled=False, refresh_pending=plan_sha256)
        state.pop('held_admission', None)
        atomic(root/sb.MARKER, state)
        # From this point any interruption is a refused root until same-cut resume.
        for rel, info in transition['changes'].items():
            staged = stage/'files'/rel
            if sb.sha(staged) != info['sha256']:
                raise ValueError('staged file changed')
            dest = sb.safe_path(root/rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            temp = dest.with_name(dest.name+'.refresh-new')
            shutil.copyfile(staged, temp)
            with temp.open('rb') as handle: os.fsync(handle.fileno())
            temp.replace(dest)
        for rel in transition['removed']:
            sb.safe_path(root/rel).unlink(missing_ok=True)
        for suffix in ('-wal', '-shm', '-journal'):
            (root/('registry.sqlite3'+suffix)).unlink(missing_ok=True)
        temp = root/'registry.refresh-new'
        shutil.copyfile(stage/'registry.sqlite3', temp)
        with temp.open('rb') as handle: os.fsync(handle.fileno())
        temp.replace(root/'registry.sqlite3')
        if after_apply: after_apply()
        if payload_files(root) != transition['files']:
            raise ValueError('refreshed file inventory differs')
        with closing(ad.connection(root/'registry.sqlite3', closed=True)) as db:
            if ad.ledger(db) != transition['registry']:
                raise ValueError('refreshed whole ledger differs')
        if ad.source_inventory(source) != transition['source_inventory']:
            raise ValueError('source changed across refresh')
        if portable_cut:
            pr.validate_source(source, root, plan)
        else:
            ad.check_pins(plan['external_dependencies'])
        sb.sync_tree(root)
        state.pop('refresh_pending', None)
        state.update(preimage_verified=True, refresh=record(root, 'refresh-cut', transition))
        if portable_cut:
            state = pr.seal(root, state, transition)
        if transition.get('preserved_holds'):state['preserved_holds']=transition['preserved_holds']
        atomic(root/sb.MARKER, state)
        return {'refreshed': True, 'quarantined': True, 'changed_files': len(transition['changes']),
                'removed_files': len(transition['removed']), 'refresh': state['refresh']}


def acceptance_begin(root, folder, principal='atlas-cutover-acceptance', native_readonly=False):
    root = sb.safe_path(root); folder = sb.safe_path(folder)
    with sb.offline(root):
        state = sb.restore_state(root)
        if not state or not state.get('held_admission'):
            raise ValueError('held local admission required')
        if folder.exists(): raise ValueError('acceptance archive must be new')
        folder.mkdir(mode=0o700)
        registry = registry_snapshot(root, folder/'baseline.sqlite3')
        sb.write_json(folder/'session.json', {'root':str(root),'state':state,'principal':principal,'native_readonly':native_readonly,
            'registry':registry,'files':payload_files(root),'runtime':sb.versions()})
        sb.sync_tree(folder)
        return {'opened':True,'archive':str(folder)}


def acceptance_end(root, folder):
    root = sb.safe_path(root); folder = sb.safe_path(folder)
    session = read(folder/'session.json')
    if session['root'] != str(root) or session['runtime'] != sb.versions():
        raise ValueError('acceptance session binding differs')
    with sb.offline(root):
        if read(root/sb.MARKER) != session['state']:
            raise ValueError('acceptance marker changed')
        actual = payload_files(root)
        if any(actual.get(k) != v for k,v in session['files'].items()):
            raise ValueError('acceptance changed copied source files')
        added = sorted(set(actual)-set(session['files']))
        if any(not k.startswith('media/') for k in added):
            raise ValueError('acceptance added non-media payload')
        result_db = folder/'accepted.sqlite3'
        if result_db.exists(): raise ValueError('acceptance archive already closing; inspect before retry')
        result = registry_snapshot(root, result_db)
        with closing(sqlite3.connect(result_db)) as db:
            db.row_factory = sqlite3.Row
            db.execute('attach database ? as baseline',(str(folder/'baseline.sqlite3'),))
            if ad.schema(db) != session['registry']['schema']:
                raise ValueError('acceptance schema changed')
            for table in session['registry']['tables']:
                q = '"'+table.replace('"','""')+'"'
                if table not in ('sqlite_sequence','health') and db.execute('select * from baseline.'+q+' except select * from main.'+q).fetchone():
                    raise ValueError('acceptance changed original rows: '+table)
                allowed_tables=('jobs','events','artifacts','uploads','sqlite_sequence','health') + (('observations','scoped_observations') if session.get('native_readonly') else ())
                if table not in allowed_tables and result['tables'][table] != session['registry']['tables'][table]:
                    raise ValueError('acceptance changed non-media table: '+table)
            new_jobs = db.execute('select * from jobs where id not in (select id from baseline.jobs)').fetchall()
            def allowed_job(j):
                if session.get('native_readonly'):
                    data=json.loads(j['data'])
                    return j['kind']=='generation' and data.get('provenance')=='observed_external' and data.get('source_client')=='invokeai-external'
                return j['principal']==session['principal'] and j['kind'] in ('mask','censor','dream')
            if any(not allowed_job(j) for j in new_jobs):
                raise ValueError('foreign activity during acceptance')
            new_ids = {j['id'] for j in new_jobs}
            event_ids=new_ids | ({'native-events'} if session.get('native_readonly') else set())
            if any(r[0] not in event_ids for r in db.execute('select job_id from events where seq not in (select seq from baseline.events)')):
                raise ValueError('foreign acceptance event')
            if session.get('native_readonly'):
                for table in ('observations','scoped_observations'):
                    if any(r[0] not in new_ids for r in db.execute('select job_id from (select * from '+table+' except select * from baseline.'+table+')')):
                        raise ValueError('observer changed historical identity metadata')
                if added:raise ValueError('read-only native observation added payload files')
            if any(r[0] not in new_ids for r in db.execute('select job_id from artifacts where id not in (select id from baseline.artifacts)')):
                raise ValueError('foreign acceptance artifact')
            if any(r[0] != session['principal'] for r in db.execute('select principal from uploads where id not in (select id from baseline.uploads)')):
                raise ValueError('foreign acceptance upload')
        # Archive bytes and complete resulting ledger before restoring WHOLE baseline.
        for rel in added:
            dest = folder/'payload'/rel
            dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(root/rel,dest)
        sb.sync_tree(folder)
        sb.write_json(folder/'accepted.json', {'registry':result,'files':actual,'new_job_ids':sorted(new_ids),
            'source_history_preserved':True,'archive_only_acceptance':True})
        state = dict(session['state']); state['preimage_verified'] = False
        atomic(root/sb.MARKER,state)
        for suffix in ('-wal','-shm','-journal'):
            (root/('registry.sqlite3'+suffix)).unlink(missing_ok=True)
        temp = root/'registry.acceptance-new'
        shutil.copyfile(folder/'baseline.sqlite3',temp)
        with temp.open('rb') as handle: os.fsync(handle.fileno())
        temp.replace(root/'registry.sqlite3')
        for rel in added: (root/rel).unlink()
        if payload_files(root) != session['files']:
            raise ValueError('acceptance baseline file restore differs')
        with closing(ad.connection(root/'registry.sqlite3')) as db:
            if ad.ledger(db) != session['registry']: raise ValueError('acceptance ledger restore differs')
        sb.sync_tree(root)
        atomic(root/sb.MARKER,session['state'])
        return {'closed':True,'archived_jobs':sorted(new_ids),'copied_source_history_preserved':True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('upgrade','runtime-transition','native','holds','refresh-prepare','refresh','acceptance-begin','acceptance-end'))
    parser.add_argument('root', type=Path)
    parser.add_argument('report', type=Path)
    parser.add_argument('--sha256')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.action in ('acceptance-begin','acceptance-end'):
            result = (acceptance_begin if args.action == 'acceptance-begin' else acceptance_end)(args.root,args.report)
        elif args.action == 'refresh-prepare':
            result = refresh_prepare(args.root, args.report)
        else:
            if not args.sha256: raise ValueError('explicit reviewed checksum required')
            from .adoption_native import activate
            fn = {'upgrade':upgrade, 'runtime-transition':runtime_transition, 'native':activate, 'holds':activate_holds, 'refresh':refresh}[args.action]
            result = fn(args.root, args.report, args.sha256)
        print(dump(result))
    except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
        parser.exit(1, 'Atlas adoption transition refused: '+str(exc)+'\n')


if __name__ == '__main__':
    main()
