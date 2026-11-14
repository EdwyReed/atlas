"""Configured-source activation; historical uncertainty is not a global queue lock.

Only current single-user/no-token scope is supported here. This proves today's
configured API scope, not the identity or success of historical database rows.
"""
from pathlib import Path
import json
import sqlite3
from contextlib import closing
from urllib.parse import urlsplit, parse_qs
from . import state_backup as sb, adoption_control as ctl, adoption as ad
from .repository import digest
from .contracts import Fault

ADMISSION = 'configured-source-v1'


def preflight(binding, invoke, expected):
    actual_binding = {k: binding[k] for k in ('id','url','backend_generation','account_scope','token_file')}
    if actual_binding != expected['binding'] or invoke.base != binding['url'].rstrip('/'):
        raise ValueError('configured native origin/binding changed')
    if binding['token_file'] is not None or invoke.http.headers.get('authorization'):
        raise ValueError('configured activation requires original no-token scope')
    version = invoke.call('GET','/api/v1/app/version')
    auth = invoke.call('GET','/api/v1/auth/status')
    schema = invoke.call('GET','/openapi.json')
    if (version != expected['version'] or auth.get('multiuser_enabled') is not False
            or auth.get('setup_required') is not False
            or digest(schema) != expected['openapi_sha256']):
        raise ValueError('native version/API/account scope changed; no fallback')
    status = invoke.call('GET',invoke.qp+'/status')
    if not isinstance(status,dict) or status.get('queue',{}).get('queue_id',status.get('queue_id')) != invoke.queue:
        raise ValueError('native queue scope changed')
    ids=invoke.call('GET',invoke.qp+'/item_ids?order_dir=ASC')
    if not isinstance(ids,dict) or len(ids.get('item_ids',[])) != ids.get('total_count'):
        raise ValueError('native scope inventory incomplete')
    if ids['item_ids']:
        item=invoke.item(ids['item_ids'][-1])
        if item.get('user_id') != 'system':raise ValueError('configured no-token queue user changed')
    return {'binding':actual_binding,'version':version,'multiuser_enabled':False,
            'effective_user':'system','openapi_sha256':digest(schema),'queue_id':invoke.queue}


def row_pins(root, ids):
    with closing(ad.connection(Path(root)/'registry.sqlite3')) as db:
        db.row_factory=sqlite3.Row
        result=[]
        for jid in ids:
            r=db.execute('select * from jobs where id=?',(jid,)).fetchone()
            if r is None:raise ValueError('historical held row missing')
            result.append({'id':r['id'],'kind':r['kind'],'state':r['state'],'row_sha256':digest(dict(r))})
        return result


def intent(row):
    """Stable original intent, separate from mutable native observations."""
    data=json.loads(row['data'])
    return {**{k:row[k] for k in ('id','principal','request_id','kind','hash','created')},
            **{k:data.get(k) for k in ('payload','source_client','plan','prepared_submission','native_scope')}}


def review_progress(before, after, pins):
    result=[]
    with closing(ad.connection(before)) as old,closing(ad.connection(after)) as new:
        old.row_factory=new.row_factory=sqlite3.Row
        for pin in pins:
            a=old.execute('select * from jobs where id=?',(pin['id'],)).fetchone()
            b=new.execute('select * from jobs where id=?',(pin['id'],)).fetchone()
            if a is None or b is None:raise ValueError('source lost historical held intent')
            a,b=dict(a),dict(b)
            if digest(a)!=pin['row_sha256'] or intent(a)!=intent(b):
                raise ValueError('historical intent identity/payload/operation changed')
            result.append({'id':pin['id'],'original_row':a,'final_row':b,
                           'intent_sha256':digest(intent(a)),
                           'observation_progressed':a!=b})
    return result


def required_holds(root,state):
    """Repin original exclusions to the authoritative final source cut."""
    ids={r['id'] for r in ctl.unknown_rows(root)}
    if state.get('held_admission'):
        ids.update(r['id'] for r in ctl.bound_record(state['held_admission'])['held_rows'])
    if state.get('preserved_holds'):
        old=ctl.bound_record(state['preserved_holds'])
        ids.update(r['id'] for r in old['held_rows'])
        cut=ctl.bound_record(state['refresh'])
        review=cut['held_review']
        if cut['preserved_holds']!=state['preserved_holds'] or [r['id'] for r in review]!=[r['id'] for r in old['held_rows']]:
            raise ValueError('final cut hold provenance differs')
        for r,p in zip(review,old['held_rows']):
            a,b=r['original_row'],r['final_row']
            if (digest(a)!=p['row_sha256'] or intent(a)!=intent(b)
                    or digest(intent(a))!=r['intent_sha256']):
                raise ValueError('final cut intent invariant differs')
        pins=row_pins(root,[r['id'] for r in review])
        if any(p['row_sha256']!=digest(r['final_row']) for p,r in zip(pins,review)):
            raise ValueError('final source held rows drifted')
    return row_pins(root,sorted(ids))


def validate(root, state, report, runtime=None):
    expected_runtime=runtime or sb.versions()
    if report['runtime'] != expected_runtime:
        transition=ctl.bound_record(state['runtime_upgrade']) if state.get('runtime_upgrade') else {}
        if (transition.get('previous_state',{}).get('held_admission') != state.get('held_admission')
                or transition.get('from_versions') != report['runtime']
                or transition.get('to_versions') != expected_runtime):
            raise ValueError('configured admission runtime transition differs')
    if (report['target_root'] != str(root)
            or report['capture_receipt_sha256'] != state['adoption_receipt_sha256']
            or report.get('refresh') != state.get('refresh')
            or report['admission'] != ADMISSION):
        raise ValueError('configured admission binding differs')
    if row_pins(root,[r['id'] for r in report['held_rows']]) != report['held_rows']:
        raise ValueError('historical held rows changed')
    if not set(r['id'] for r in report['held_rows']) <= set(report['historical_native_ids']):
        raise ValueError('historical native exclusion omits a held row')
    return report


def activate(root, report_path, report_sha256):
    root=sb.safe_path(root)
    if sb.sha(sb.safe_path(report_path)) != report_sha256:raise ValueError('native activation checksum differs')
    report=ctl.read(report_path)
    with sb.offline(root):
        state=sb.restore_state(root)
        if not state or state.get('migration_kind') != ad.KIND:raise ValueError('canonical capture required')
        previous=ctl.bound_record(state['held_admission']) if state.get('held_admission') else None
        if report.get('previous_admission') != state.get('held_admission'):raise ValueError('previous admission changed')
        if report.get('registry_logical_sha256') != sb.registry_digest(root):raise ValueError('activation ledger changed')
        if previous and previous['admission'] != 'local-only-v1':
            raise ValueError('configured admission already accepted')
        if report['held_rows'] != required_holds(root,state):
            raise ValueError('must bind final cut unresolved set and original exclusions')
        review=ctl.bound_record(state['refresh']).get('held_review',[]) if state.get('preserved_holds') else []
        if report.get('final_cut_review',[]) != review or (review and not report.get('progress_assessment')):
            raise ValueError('explicit final-cut progress review required')
        if not report.get('operator') or not report.get('evidence'):raise ValueError('reviewed evidence/operator required')
        ad.check_pins(report['evidence'])
        validate(root,state,report)
        with closing(ad.connection(root/'registry.sqlite3')) as db:
            historical_ids=[r[0] for r in db.execute("select id from jobs where kind in ('generation','native_admin') order by id")]
        if report['historical_native_ids'] != historical_ids:raise ValueError('historical native exclusion differs from ledger')
        receipt=ctl.read(root/'adoption-receipt.json')
        binding={k:v for k,v in receipt['plan']['native_binding'].items() if k!='enable_writes'}
        if report['native']['binding'] != binding:raise ValueError('original native binding changed')
        # Fresh GET-only validation inside the locked transition, not a stale claim.
        from .upstream import Invoke
        inv=Invoke(binding['url'])
        try:preflight(binding,inv,report['native'])
        finally:inv.http.close()
        state.update(reconciled=True,held_admission=ctl.record(root,'configured-native',report))
        ctl.atomic(root/sb.MARKER,state)
        return {'activated':True,'admission':ADMISSION,'held':report['held_rows']}


def enabled(engine):
    from .portable import ADMISSION as PORTABLE
    return bool(engine.held_admission and engine.held_admission['admission'] in (ADMISSION,PORTABLE))


def held(engine, job):
    return bool(engine.held_admission and job['id'] in {r['id'] for r in engine.held_admission['held_rows']})


def historical(engine, job):
    # Captured rows have no current native database-generation proof. Observe new
    # configured jobs normally without rewriting captured native history.
    return held(engine,job) or (enabled(engine) and job['id'] in engine.held_admission['historical_native_ids'])


def resources(payload):
    """Literal installer resource selectors; no URL/name normalization."""
    found=set()
    if payload.get('file_id'):found.add(('file_id',payload['file_id']))
    if payload.get('url'):found.add(('url',payload['url']))
    if payload.get('source_id'):found.add(('source_id',payload['source_id']))
    if payload.get('expected_sha256'):found.add(('sha256',payload['expected_sha256']))
    return found


def guard(engine, op, p, principal):
    # Reuse old receipt/conflict semantics before any effect or verification.
    with engine.repo.connect() as db:
        row=db.execute('select * from jobs where principal=? and request_id=?',(principal,p.get('request_id'))).fetchone() if p.get('request_id') else None
    if row:
        job=engine.repo.decode(row)
        if historical(engine,job):
            intent={'operation':op,**p} if job['kind'] in ('native_admin','native_workspace') else p
            return engine.repo.request(principal,p['request_id'],job['kind'],intent)
    if p.get('job_id'):
        job=engine.repo.get(p['job_id'])
        if historical(engine,job) and op not in sb.READS and op not in ('generation.wait','admin.get'):
            raise Fault('HELD_NATIVE_EFFECT','exact historical intent is read-only; no retry, cancel or repair')
    if p.get('upload_id') in engine.held_admission.get('held_upload_ids',[]) and op not in sb.READS:
        raise Fault('HELD_NATIVE_EFFECT','historical upload outcome is uncertain; no import replay')
    from .native_admin import MUTATIONS
    if op in MUTATIONS or op.startswith('models.acquire.') or op.startswith('models.onboard'):
        candidates=resources(p)
        if p.get('source_id'):
            candidates |= resources(engine.native_admin.sources.get(p['source_id'],{}))
        for r in engine.held_admission['held_rows']:
            old=engine.repo.get(r['id'])
            if old['kind']=='native_admin':
                if candidates & resources(old['payload']):
                    raise Fault('HELD_NATIVE_RESOURCE','resource overlaps an unresolved historical administration intent')
    return None
