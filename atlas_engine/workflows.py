"""Observed native saved-workflow contract; native alone builds child graphs."""
import hashlib
import json
import uuid
from urllib.parse import quote
from .contracts import Fault


def scoped(engine, p):
    scope = engine.native_queue.scope()
    if not scope['connection_id']:
        raise Fault('INVOKE_UNCONFIGURED', 'configure a pinned native connection')
    if any(p[k] != scope[k] for k in ('connection_id', 'backend_generation')):
        raise Fault('NATIVE_SCOPE_MISMATCH', 'workflow reference differs from pinned backend')
    return scope


def schema(engine, p):
    scoped(engine, p)
    record = engine.invoke.call('GET', '/api/v1/workflows/i/' + quote(p['workflow_id'], safe=''))
    if not isinstance(record, dict) or record.get('workflow_id') != p['workflow_id']:
        raise Fault('WORKFLOW_SCHEMA_UNSUPPORTED', 'native workflow identity missing or different')
    workflow = record.get('workflow')
    if not isinstance(workflow, dict) or not isinstance(workflow.get('meta'), dict):
        raise Fault('WORKFLOW_SCHEMA_UNSUPPORTED', 'native workflow document and meta required')
    compatibility = record.get('call_saved_workflow_compatibility')
    if not isinstance(compatibility, dict) or compatibility.get('is_callable') is not True or compatibility.get('reason') != 'ok':
        raise Fault('WORKFLOW_NOT_CALLABLE', 'native compatibility is missing or refuses this workflow')
    if workflow.get('id') != p['workflow_id'] or workflow.get('meta', {}).get('version') != '1.0.0':
        raise Fault('WORKFLOW_SCHEMA_UNSUPPORTED', 'only native workflow schema 1.0.0 is supported')
    if workflow.get('form') is not None:
        raise Fault('WORKFLOW_INPUT_UNSUPPORTED', 'form-based exposed inputs are outside this subset; exposedFields fallback only')
    nodes = workflow.get('nodes')
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 32 or not isinstance(workflow.get('edges'), list) or len(workflow['edges']) > 64:
        raise Fault('WORKFLOW_SCHEMA_UNSUPPORTED', 'observed workflow exceeds the bounded subset')
    by_id = {}
    keys = []
    returns = 0
    for node in nodes:
        data = node.get('data', {}) if isinstance(node, dict) else {}
        if not isinstance(data, dict) or not isinstance(data.get('inputs'), dict):
            raise Fault('WORKFLOW_NODE_UNSUPPORTED', 'native invocation data/inputs required')
        if (not isinstance(node, dict) or node.get('type') != 'invocation' or data.get('type') not in ('integer', 'workflow_return_value', 'workflow_return') or
                data.get('version') != '1.0.0' or not isinstance(node.get('id'), str) or node['id'] in by_id):
            raise Fault('WORKFLOW_NODE_UNSUPPORTED', 'observed nodes are outside the integer callable subset')
        by_id[node['id']] = data
        if data['type'] == 'workflow_return':
            returns += 1
        if data['type'] == 'workflow_return_value':
            key_field = data['inputs'].get('key')
            key = key_field.get('value') if isinstance(key_field, dict) else None
            if not isinstance(key, str) or not key or key != key.strip() or len(key.encode('utf-8')) > 128 or key in keys:
                raise Fault('WORKFLOW_OUTPUT_UNSUPPORTED', 'return keys must be unique literal nonblank bounded names')
            keys.append(key)
    if returns != 1 or not keys:
        raise Fault('WORKFLOW_OUTPUT_UNSUPPORTED', 'one explicit return and named return values required')
    inputs = {}
    fields = workflow.get('exposedFields')
    if not isinstance(fields, list) or len(fields) > 32:
        raise Fault('WORKFLOW_INPUT_UNSUPPORTED', 'bounded exposedFields required')
    for field in fields:
        node_id = field.get('nodeId') if isinstance(field, dict) else None
        name = field.get('fieldName') if isinstance(field, dict) else None
        if not isinstance(node_id, str) or by_id.get(node_id, {}).get('type') != 'integer' or name != 'value':
            raise Fault('WORKFLOW_INPUT_UNSUPPORTED', 'only exposed integer.value inputs are supported')
        key = 'saved_workflow_input::' + node_id + '::' + name
        if key in inputs or len(key.encode('utf-8')) > 256:
            raise Fault('WORKFLOW_INPUT_UNSUPPORTED', 'duplicate or oversized exposed input')
        inputs[key] = {'type': 'integer', 'minimum': -(2**63), 'maximum': 2**63-1}
    revision = record.get('revision')
    if type(revision) is not int or revision < 1:
        raise Fault('WORKFLOW_SCHEMA_UNSUPPORTED', 'native content revision required')
    return {**{k:p[k] for k in ('connection_id', 'backend_generation', 'workflow_id')},
            'observed_revision': revision,
            'observed_sha256': hashlib.sha256(json.dumps(workflow, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest(),
            'call_saved_workflow_compatibility': {'is_callable': True, 'reason': 'ok'},
            'workflow_inputs': inputs, 'observed_return_keys': keys, 'return_type': 'integer',
            'version_semantics': 'latest_at_execution', 'executed_revision': None,
            'limits': 'Observed subset only; native reauthorizes and loads by ID at execution. No immutable revision or CPU-only guarantee after edits.'}


def run(engine, p, principal, source):
    scope = scoped(engine, p)
    # Exact replay is a local receipt read, without repeating schema reads.
    old = engine.repo.request(principal, p['request_id'], 'generation', p)
    if old:
        engine.native_queue.check(old)
        return old
    if not engine.allow_submit or 'saved_workflow' not in engine.accepted_modes:
        raise Fault('MODE_NOT_ACCEPTED', 'saved_workflow requires explicit accepted_modes and native writes')
    observed = schema(engine, p)
    if set(p['workflow_inputs']) - set(observed['workflow_inputs']):
        raise Fault('WORKFLOW_INPUT_UNSUPPORTED', 'input must name an observed exposed integer.value field exactly')
    if any(type(v) is not int or not -(2**63) <= v < 2**63 for v in p['workflow_inputs'].values()):
        raise Fault('WORKFLOW_INPUT_UNSUPPORTED', 'literal signed-64-bit integer inputs required')
    if p['return_key'] not in observed['observed_return_keys']:
        raise Fault('WORKFLOW_OUTPUT_UNSUPPORTED', 'return key is not an observed literal return name')
    graph = {'id': str(uuid.uuid4()), 'nodes': {'call': {
        'id': 'call', 'type': 'call_saved_workflow', 'workflow_id': p['workflow_id'],
        'workflow_inputs': p['workflow_inputs'], 'is_intermediate': True, 'use_cache': False}}, 'edges': []}
    job, created = engine.repo.reserve(principal, source, p['request_id'], 'generation', p,
        {'native_scope': scope, 'native_ids': [], 'workflow_observation': observed,
         'plan': {'mode': 'saved_workflow', 'graph': graph, 'workflow_returns': {'call': p['return_key']}}})
    return engine.execute_submission(job) if created else job
