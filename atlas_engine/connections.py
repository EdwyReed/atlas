"""Small domain routing: immutable backend contexts, one operational owner.

Contexts are shallow Engine views, NOT lifecycle owners. Repository, corpus,
projection, locks and workers remain shared. No thread-local/current client.
"""
from copy import copy
from .contracts import Fault, NATIVE
# These adapters invoke an operator-bound Windows installer outside Invoke HTTP.
UNSUPPORTED_MULTI = {'models.lora.install', 'models.checkpoint.install',
                     'models.acquire.enqueue', 'models.acquire.resume', 'models.acquire.select'}
JOB_REMOTE = {'generation.cancel', 'generation.retry', 'artifact.fetch',
              'models.onboard.resume', 'models.onboard.refresh', 'models.onboard.recover'}


def install(owner, config, manifest, clients):
    owner.connection_contexts = {}
    owner.default_connection_id = config.default_connection_id if config.compatibility_routing else None
    owner.legacy_connection_id = manifest['legacy_connection_id']
    owner.multi_connection = len(config.connections) > 1
    for binding in config.connections:
        view = copy(owner)
        view.connection_contexts = None
        view.connection_owner = owner
        view.connection_id = binding.id
        view.backend_generation = binding.backend_generation
        view.account_scope = binding.account_scope
        view.invoke = clients[binding.id]
        view.allow_submit = binding.enable_writes and (not owner.held_admission or owner.native_activated)
        for name in ('native_admin', 'native_queue', 'workspace', 'onboarding'):
            helper = copy(getattr(owner, name))
            helper.e = view
            setattr(view, name, helper)
        view.native_admin.enabled = view.allow_submit
        if owner.native_activated and owner.held_admission['admission'] != 'portable-restored-v1':
            from .adoption_native import preflight
            preflight(binding.model_dump(), view.invoke, owner.held_admission['native'])
        for name in ('uploads', 'artifacts'):
            helper = copy(getattr(owner, name))
            helper.invoke = view.invoke
            setattr(view, name, helper)
        view.uploads.native_scope = view.native_queue.scope
        view.artifacts.native_scope = view.native_queue.scope
        owner.connection_contexts[binding.id] = view
    owner.artifacts.resolve_backend = lambda job: job_context(owner, job).invoke
    for view in owner.connection_contexts.values():
        view.artifacts.resolve_backend = owner.artifacts.resolve_backend
    # Preserve original observation rows and copy only their identity index into
    # the scoped table, using their own job binding or explicit legacy receipt.
    from .repository import dump
    with owner.repo.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for row in db.execute('select * from observations').fetchall():
            job=owner.repo.get(row['job_id'])
            try:view=job_context(owner,job)
            except Fault:continue
            db.execute('insert or ignore into scoped_observations values(?,?,?,?,?)',
                       (dump(view.native_queue.scope()),row['native_id'],row['job_id'],row['item'],row['at']))
    # Acquisitions are intentionally unsupported with multiple bindings. Do not
    # resume old external installer intents through any newly selected default.
    if not owner.multi_connection and owner.connection_contexts:
        view = next(iter(owner.connection_contexts.values()))
        owner.acquisition.e = view


def job_context(owner, job):
    contexts = getattr(owner, 'connection_contexts', None)
    if contexts is None:
        return owner
    scope = job.get('native_scope') or job.get('payload', {})
    cid = scope.get('connection_id') or owner.legacy_connection_id
    if not cid or cid not in contexts:
        raise Fault('NATIVE_SCOPE_MISSING', 'persisted intent has no recognized original binding')
    view = contexts[cid]
    generation = scope.get('backend_generation')
    if generation is not None and generation != view.backend_generation:
        raise Fault('NATIVE_SCOPE_MISMATCH', 'persisted backend generation differs')
    return view


def owns(view, job):
    owner = getattr(view, 'connection_owner', None)
    if owner is None:
        return True
    try:
        return job_context(owner, job) is view
    except Fault:
        return False


def select(owner, op, p, principal):
    contexts = getattr(owner, 'connection_contexts', None)
    if contexts is None:
        return owner, p
    if owner.multi_connection and op in UNSUPPORTED_MULTI:
        raise Fault('MULTI_CONNECTION_UNSUPPORTED', 'external installer/acquisition has no scoped multi-backend contract')
    if op in JOB_REMOTE:
        job = owner.repo.get(p['job_id'])
        # Cached artifacts are served locally even without a configured backend.
        if op == 'artifact.fetch':
            return owner, p
        return job_context(owner, job), p
    if op not in NATIVE and not (op in ('enrich', 'dream.submit') and p.get('model')):
        return owner, p
    if not contexts:
        raise Fault('INVOKE_UNCONFIGURED', 'configure a native connection')
    cid = p.get('connection_id')
    previous = None
    if p.get('request_id'):
        with owner.repo.connect() as db:
            row = db.execute('select * from jobs where principal=? and request_id=?', (principal, p['request_id'])).fetchone()
        if row:
            previous = owner.repo.decode(row)
            if not cid:
                cid = job_context(owner, previous).connection_id
    if not cid:
        cid = next(iter(contexts)) if len(contexts) == 1 else owner.default_connection_id
    if not cid:
        raise Fault('CONNECTION_REQUIRED' if contexts else 'INVOKE_UNCONFIGURED', 'explicit connection_id required')
    if cid not in contexts:
        raise Fault('CONNECTION_UNKNOWN', 'connection_id is not configured')
    view = contexts[cid]
    if p.get('backend_generation') not in (None, view.backend_generation):
        raise Fault('NATIVE_SCOPE_MISMATCH', 'backend generation differs from binding')
    scoped = {**p, 'connection_id': cid, 'backend_generation': view.backend_generation}
    # Legacy replay remains bound to its ORIGINAL receipt, never the default.
    # Keep its exact old payload hash. New explicit scope changes still conflict.
    if p.get('request_id'):
        with owner.repo.connect() as db:
            row = db.execute('select * from jobs where principal=? and request_id=?', (principal, p['request_id'])).fetchone()
        if row:
            job = owner.repo.decode(row)
            old = job['payload']
            original = job_context(owner, job)
            if original is not view:
                raise Fault('IDEMPOTENCY_CONFLICT', 'request_id is already bound to another connection')
            if 'connection_id' not in old:
                scoped = {k: v for k, v in p.items() if k not in ('connection_id', 'backend_generation')}
    return view, scoped
