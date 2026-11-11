"""Scoped native admission using the existing generation ledger and executor.

A missing receipt is not permission to enqueue. Only the initial fenced POST
may write a batch; recovery reads receipts. Native owns all queue execution.
"""
from copy import deepcopy
import uuid
from .contracts import Fault


class NativeQueue:
    def __init__(self, engine):
        self.e = engine

    def scope(self):
        return {'connection_id': getattr(self.e, 'connection_id', None),
                'backend_generation': getattr(self.e, 'backend_generation', None),
                'queue_id': self.e.invoke.queue}

    def check(self, job):
        scope = job.get('native_scope')
        if scope is not None and scope != self.scope():
            raise Fault('NATIVE_SCOPE_MISMATCH', 'intent belongs to another pinned backend')

    def submit_graph(self, p, principal, source):
        if not self.scope()['connection_id']:
            raise Fault('INVOKE_UNCONFIGURED', 'configure a pinned native connection')
        if any(p[k] != self.scope()[k] for k in ('connection_id', 'backend_generation')):
            raise Fault('NATIVE_SCOPE_MISMATCH', 'reference differs from pinned backend')
        if not self.e.allow_submit or 'native_graph' not in self.e.accepted_modes:
            raise Fault('MODE_NOT_ACCEPTED', 'native_graph requires explicit accepted_modes and native writes')
        old = self.e.repo.request(principal, p['request_id'], 'generation', p)
        if old:
            self.check(old)
            return old
        graph = deepcopy(p['graph'])
        if set(graph) - {'id', 'nodes', 'edges'} or not isinstance(graph.get('nodes'), dict) or not graph['nodes'] or not isinstance(graph.get('edges', []), list):
            raise Fault('VALIDATION', 'graph requires nonempty nodes and an edges array')
        if len(graph['nodes']) > 128 or len(graph.get('edges', [])) > 512:
            raise Fault('VALIDATION', 'graph exceeds admission budget')
        graph.setdefault('id', str(uuid.uuid4()))
        graph.setdefault('edges', [])
        job, created = self.e.repo.reserve(principal, source, p['request_id'], 'generation', p,
            {'native_scope': self.scope(), 'plan': {'mode': 'native_graph', 'graph': graph}, 'native_ids': []})
        return self.e.execute_submission(job) if created else job

    def prepare(self, job):
        self.check(job)
        # Persist UUID/key, graph, runs and all batch defaults before network I/O.
        return {'batch': {'batch_id': job['id'], 'idempotency_key': 'atlas:' + job['id'],
                'graph': job['plan']['graph'], 'runs': job['payload'].get('runs', 1),
                'origin': 'atlas-engine:' + job['id'], 'destination': None,
                'project_id': None, 'workflow': None, 'data': None}, 'prepend': False}

    def record(self, job, ack, receipt=False):
        self.check(job)
        ids = ack.get('item_ids') if isinstance(ack, dict) else None
        batch_id = ack.get('batch_id') if receipt else ack.get('batch', {}).get('batch_id')
        requested = job['prepared_submission']['batch']['runs']
        if (not isinstance(ids, list) or not isinstance(batch_id, str) or not batch_id or
                type(ack.get('requested')) is not int or ack['requested'] != requested or
                type(ack.get('enqueued')) is not int or not 0 <= ack['enqueued'] <= requested or
                len(ids) != ack['enqueued'] or len(set(ids)) != len(ids) or
                any(type(n) is not int or n < 1 for n in ids)):
            raise Fault('ACK_UNKNOWN', 'invalid native receipt; no replay')
        # The complete receipt and identity are committed atomically before any ACK.
        result = self.e.repo.acknowledge_submission(job['id'], ids, batch_id, ack)
        if not ids:
            result = self.e.repo.update(job['id'], 'failed',
                {'error': {'code': 'QUEUE_NOT_ADMITTED', 'message': 'native admitted zero items'}},
                expected={'observing'}, event='submission.not_admitted')[0]
        # Deliberately leave the native receipt unacknowledged. Retention/GC is not
        # needed for this slice; no risk of deleting the only durable evidence.
        return result

    def recover_receipts(self):
        for job in self.e.repo.iter_jobs('generation', {'submitting', 'ack_unknown'}):
            if not job.get('prepared_submission'):
                continue  # Older submissions had no native retry key.
            try:
                self.check(job)
                key = job['prepared_submission']['batch']['idempotency_key']
                ack = self.e.invoke.enqueue_receipt(key)
                self.record(job, ack, receipt=True)
            except Exception as exc:
                self.e.repo.update(job['id'], 'ack_unknown',
                    {'receipt_error': getattr(exc, 'code', type(exc).__name__)},
                    expected={'submitting', 'ack_unknown'}, event='submission.receipt_unknown_no_replay')
