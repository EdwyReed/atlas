"""InvokeAI split-v4 prompt patch. Native owns state; SQLite owns intent/ACK.

No remote receipt lookup exists in this API. Missing ACK is therefore unknown,
not inferred ownership from revision, whole-document equality or prompt equality.
"""
from copy import deepcopy
import fcntl
from .contracts import Fault
from .native_admin import part

OPERATIONS = {'projects.get', 'projects.savePrompt'}
QUERY = '?max_document_schema_version=4&max_canvas_schema_version=2'


class Workspace:
    def __init__(self, engine):
        self.e = engine

    def scope(self, p):
        if not getattr(self.e, 'connection_id', None):
            raise Fault('INVOKE_UNCONFIGURED', 'configure a native connection')
        if (p['connection_id'] != self.e.connection_id or
                p['backend_generation'] != getattr(self.e, 'backend_generation', None)):
            raise Fault('NATIVE_SCOPE_MISMATCH', 'reference does not match the pinned backend')
        return '/api/v1/projects/' + part(p['project_id'])

    def read(self, p):
        record = self.e.invoke.call('GET', self.scope(p) + QUERY)
        if (not isinstance(record, dict) or record.get('project_id') != p['project_id'] or
                type(record.get('revision')) is not int or record['revision'] < 1 or
                not isinstance(record.get('data'), dict)):
            raise Fault('REMOTE_SHAPE', 'invalid native project record')
        # This is a codec, not a whole-document migration or Canvas compiler.
        workspace = record.get('workspace')
        if (record.get('minimum_document_schema_version') != 4 or
                record.get('minimum_canvas_schema_version') not in (1, 2) or
                record['data'].get('documentSchemaVersion') != 4 or
                not isinstance(workspace, dict) or
                type(workspace.get('revision')) is not int or workspace['revision'] < 1 or
                not isinstance(workspace.get('data'), dict) or
                workspace['data'].get('documentSchemaVersion') != 4 or
                workspace['data'].get('settingsLayering') != 1):
            raise Fault('WORKSPACE_CODEC_UNSUPPORTED', 'requires split-v4, canvas <=2, existing layered workspace v1')
        return record

    @staticmethod
    def view(p, record):
        return {'reference': {k: p[k] for k in ('connection_id', 'backend_generation', 'project_id')},
                'codec': 'invokeai7-split4-generate1',
                'content_revision': record['revision'],
                'workspace_revision': record['workspace']['revision'], 'native': record}

    @staticmethod
    def patch(record, p):
        data = deepcopy(record['workspace']['data'])
        try:
            widget = data['widgetInstances'][p['widget_instance_id']]
            values = widget['state']['values']
            if widget['typeId'] != 'generate' or not isinstance(values, dict):
                raise KeyError('generate')
            if 'positivePrompt' in values and not isinstance(values['positivePrompt'], str):
                raise KeyError('positivePrompt')
        except (KeyError, TypeError):
            raise Fault('WORKSPACE_CODEC_UNSUPPORTED', 'selected generate widget values are not supported') from None
        values['positivePrompt'] = p['prompt']
        return {'data': data, 'expected_revision': p['expected_workspace_revision'],
                'max_document_schema_version': 4, 'max_canvas_schema_version': 2}

    def verify(self, job):
        """Read-only reconciliation is possible only with a durable native ACK."""
        ack = job.get('native_ack')
        if not ack:
            return job
        try:
            record = self.read(job['payload'])
            if (record['workspace']['revision'] != ack['revision'] or
                    record['workspace']['data'] != ack['data']):
                raise Fault('NATIVE_READBACK_CHANGED', 'acknowledged workspace is no longer current')
        except Exception:
            return self.e.repo.update(job['id'], 'ack_unknown', expected={'submitting', 'ack_unknown'},
                                      event='workspace.readback_uncertain')[0]
        return self.e.repo.update(job['id'], 'succeeded',
                                  {'result': self.view(job['payload'], record)},
                                  expected={'submitting', 'ack_unknown'}, event='workspace.verified')[0]

    def recover(self):
        for job in self.e.repo.iter_jobs('native_workspace', {'queued', 'submitting', 'ack_unknown'}):
            # Never dispatch a prepared request on startup, even if it was not sent.
            state = 'failed' if job['state'] == 'queued' else 'ack_unknown'
            job = self.e.repo.update(job['id'], state, event='workspace.restart_no_replay')[0]
            if state == 'ack_unknown':
                self.verify(job)

    def call(self, op, p, principal, source):
        path = self.scope(p)
        if op == 'projects.get':
            return self.view(p, self.read(p))
        if not self.e.native_admin.enabled:
            raise Fault('ADMIN_WRITES_DISABLED', 'native writes disabled')
        # File lock also protects concurrent calls through separate Engine instances.
        with (self.e.repo.root / 'workspace.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            payload = {'operation': op, **p}
            old = self.e.repo.request(principal, p['request_id'], 'native_workspace', payload)
            if old:
                if old['state'] in ('queued', 'submitting'):
                    old = self.e.repo.update(old['id'], 'ack_unknown', event='workspace.interrupted_no_replay')[0]
                return self.verify(old) if old['state'] == 'ack_unknown' else old
            # Binding is durable even if preflight fails; callers cannot rebind a stale request.
            job, created = self.e.repo.reserve(principal, source, p['request_id'], 'native_workspace', payload)
            if not created:
                return job
            try:
                record = self.read(p)
                if record['workspace']['revision'] != p['expected_workspace_revision']:
                    raise Fault('REVISION_CONFLICT', 'workspace changed; new intent requires an explicit new request_id')
                prepared = self.patch(record, p)
            except Fault as exc:
                return self.e.repo.update(job['id'], 'failed',
                    {'error': {'code': exc.code, 'message': str(exc)}}, event='workspace.preflight_refused')[0]
            job = self.e.repo.update(job['id'], 'submitting', {'prepared': prepared},
                                     expected={'queued'}, event='workspace.remote_intent')[0]
            try:
                ack = self.e.invoke.call('PUT', path + '/workspace', prepared)
            except Exception as exc:
                conflict = getattr(exc, 'code', None) == 'REMOTE_HTTP_409'
                return self.e.repo.update(job['id'], 'failed' if conflict else 'ack_unknown',
                    {'error': {'code': 'REVISION_CONFLICT' if conflict else 'NATIVE_ACK_UNKNOWN',
                               'message': 'native CAS refused' if conflict else 'no durable ACK; no replay'}},
                    expected={'submitting'}, event='workspace.cas_refused' if conflict else 'workspace.ack_unknown')[0]
            if (not isinstance(ack, dict) or type(ack.get('revision')) is not int or
                    ack['revision'] != p['expected_workspace_revision'] + 1 or
                    ack.get('data') != prepared['data']):
                return self.e.repo.update(job['id'], 'ack_unknown', event='workspace.invalid_ack_no_replay')[0]
            job = self.e.repo.update(job['id'], changes={'native_ack': ack},
                                     expected={'submitting'}, event='workspace.ack_recorded')[0]
            return self.verify(job)
