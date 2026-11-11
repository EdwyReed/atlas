"""Inert legacy reservations. No native handles are adopted and no jobs created.

Bindings are operator-owned, never supplied by an API caller. SQLite's writer
transaction serializes binding/import with modern reservation and batch children.
"""
import hashlib
import json
from .contracts import Fault


def input_hash(payload):
    # Exact legacy plugin algorithm, including ASCII escaping (not job_digest).
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def unbound_keys(c, source=None):
    """Identity-only tombstones, not ownership, lookup grants or replay intents.

    Unknown owners reserve both literal identities across ALL principals and
    namespaces. Malformed identity evidence remains fail-closed. Never inspect
    profile paths or trust owner/ACL assertions inside historical payloads.
    Caller holds a read snapshot or the admission/import writer transaction.
    """
    if not c.execute("select 1 from sqlite_master where type='table' and name='legacy_rows'").fetchone():
        return
    sql = "select r.sha256,r.data from legacy_rows r where table_name='requests' and not exists (select 1 from legacy_namespaces n where n.source=r.source)"
    args = ()
    if source is not None:
        sql += ' and r.source=?'
        args = (source,)
    for stored, data in c.execute(sql, args):
        try:
            if hashlib.sha256(data.encode()).hexdigest() != stored:raise ValueError()
            raw = json.loads(data)
            if not isinstance(raw, dict):raise ValueError()
            key, rid = raw['idem'], raw['id']
            if not all(isinstance(x, str) and x for x in (key, rid)):raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise Fault('LEGACY_UNBOUND', 'historical identity cannot be verified; admissions held pending offline repair') from None
        yield key, rid


def guard(c, principal, key):
    # Generic error does not disclose source, owner, input, status or native ref.
    # No key-scoped bypass: unbound evidence has no trustworthy owner namespace.
    for idem, rid in unbound_keys(c):
        if key in (idem, rid):
            raise Fault('LEGACY_UNBOUND', 'request identity unavailable; no historical access or replay')
    if c.execute('select 1 from legacy_reservations where idem=? or request_id=? limit 1', (key, key)).fetchone():
        raise Fault('LEGACY_HOLD', 'historical key is reserved; use legacy.request with its original input and namespace; no replay')


def guard_unbound_import(c, source):
    # Reverse ordering of import/reserve: no unbound tombstone may arrive after
    # a modern intent has already claimed that identity, in any namespace.
    for idem, rid in unbound_keys(c, source):
        if c.execute('select 1 from jobs where request_id in (?,?) limit 1', (idem, rid)).fetchone():
            raise Fault('LEGACY_JOB_COLLISION', 'modern intent uses a historical identity; reconcile offline before import')


def bind(c, source, namespace, principal, project, manifest_hash):
    old = c.execute('select * from legacy_namespaces where namespace=? or source=?', (namespace, source)).fetchall()
    identity = (namespace, source, principal, project, manifest_hash)
    if old and (len(old) != 1 or tuple(old[0]) != identity):
        raise Fault('LEGACY_BINDING_CONFLICT', 'immutable namespace/source binding differs')
    rows = list(c.execute("select data from legacy_rows where source=? and table_name='requests' order by row_number", (source,)))
    seen = set()
    reservations = []
    for row in rows:
        try:
            raw = json.loads(row[0])
            if not isinstance(raw, dict):raise ValueError()
            data = json.loads(raw['data'])
            if not isinstance(data, dict):raise ValueError()
            key, rid, h = raw['idem'], raw['id'], raw['input_hash']
            if data.get('request_id', rid) != rid:raise ValueError()
            if not all(isinstance(x, str) and x for x in (key, rid, h)):raise ValueError()
            if key in seen or data['idempotency_key'] != key or input_hash(data['input']) != h:raise ValueError()
            if not isinstance(data['input'], dict) or not isinstance(raw['status'], str):raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise Fault('LEGACY_INVALID', 'legacy request identity/input cannot be verified') from None
        seen.add(key)
        if c.execute('select 1 from jobs where request_id in (?,?) limit 1', (key, rid)).fetchone():
            raise Fault('LEGACY_JOB_COLLISION', 'modern intent already uses a historical key; reconcile offline before binding')
        # Ref is retained only inside quarantine. Numeric handles are not proof
        # of identity, and are never exposed as successor jobs or byte grants.
        reservations.append((namespace, principal, key, rid, h, raw['status']))
    c.execute('insert or ignore into legacy_namespaces values(?,?,?,?,?)', identity)
    for row in reservations:
        c.execute('insert or ignore into legacy_reservations values(?,?,?,?,?,?)', row)
    return len(reservations)


def lookup(repo, namespace, key, payload):
    with repo.connect() as c:
        c.execute('BEGIN')
        n = c.execute('select * from legacy_namespaces where namespace=?', (namespace,)).fetchone()
        # Namespace is a historical identity, not an access scope.
        if not n:raise Fault('NOT_FOUND', 'historical request not found')
        r = c.execute('select * from legacy_reservations where namespace=? and idem=?', (namespace, key)).fetchone()
        if not r:raise Fault('NOT_FOUND', 'historical request not found')
        if input_hash(payload) != r['input_hash']:
            raise Fault('IDEMPOTENCY_CONFLICT', 'historical key is bound to another input')
        return {'namespace': namespace, 'idempotency_key': key, 'request_id': r['request_id'],
                'input_hash': r['input_hash'], 'owner_principal': n['principal'], 'project': n['project'],
                'legacy_status': r['legacy_status'], 'reconciliation_state': 'unknown',
                'admission': 'hold', 'replay_allowed': False,
                'historical_reference': {'namespace': namespace, 'request_id': r['request_id']}}
