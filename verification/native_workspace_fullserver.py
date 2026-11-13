"""Scenarios for native_fullserver.py --workspace; original API-owned JSON.

No native service/schema replacement. API accepts opaque client documents; this
checks Atlas' narrow layered codec, not frontend hydration/Canvas compilation.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import sqlite3
import sys
from urllib.parse import urlsplit

import httpx


QUERY = '?max_document_schema_version=4&max_canvas_schema_version=2'


def run(c):
    op, save, passed = (c[k] for k in ('op', 'save', 'passed'))
    out, mode, calls = (c[k] for k in ('out', 'mode', 'calls'))
    stop, start_atlas, start_native = (c[k] for k in ('stop', 'start_atlas', 'start_native'))
    direct = []
    def native(method, path, body=None):
        value = c['native'](method, path, body)
        direct.append({'method': method, 'path': path, 'body': deepcopy(body), 'response': deepcopy(value)})
        save('native-direct.json', direct)
        return value
    def mcp(name, args):
        return asyncio.run(c['mcp'](name.replace('.', '_'), args))
    def fail(name, args, code):
        r = httpx.post(c['au']+'/v1/ops/'+name, json=args, headers=c['headers'], trust_env=False, timeout=40)
        r.raise_for_status()
        j = r.json()
        assert not j['ok'] and j['error']['code'] == code, j
        c['responses'].append({'negative': name, 'args': args, 'response': j})
        return j
    def puts():
        return [x for x in calls if x['method'] == 'PUT' and urlsplit(x['path']).path.endswith('/workspace')]
    # Discover the running original router. Workspace has no GET; an SPA 200 is not a readback.
    r = httpx.get(c['nu']+'/openapi.json', trust_env=False, timeout=40)
    r.raise_for_status()
    schema = r.json()
    selected = {k: v for k, v in schema['paths'].items() if k.startswith('/api/v1/projects')}
    assert set(selected['/api/v1/projects/{project_id}/workspace']) == {'put'}
    assert 'get' in selected['/api/v1/projects/{project_id}']
    save('native-openapi-selected.json', selected)
    # These fields are API-owned opaque JSON. Shapes follow splitProjectDocument's
    # division; no claim that this manually authored document renders in the UI.
    content = {'documentSchemaVersion': 4, 'id': 'owned-layered-project', 'name': 'Owned layered project',
        'canvas': {'document': {'version': 2, 'layers': [], 'selectedLayerId': None}},
        'workflows': {'entries': []}, 'futureContent': {'unicode': 'Привет 🦊', 'null': None, 'number': 1.25}}
    workspace = {'documentSchemaVersion': 4, 'settingsLayering': 1,
        'canvas': {'selectedLayerId': None, 'stagingArea': {'candidates': []}},
        'workflows': {'activeWorkflowId': '', 'lastRuns': {}},
        'layout': {'id': 'owned-layout', 'opaque': [None, True, {'x': 'unchanged'}]},
        'invocation': {'sourceId': 'generate-1'}, 'settings': {'seed': 1234, 'futurePin': {'value': 0}},
        'widgetGraphs': {}, 'widgetRegions': {},
        'promptHistory': [{'positivePrompt': 'history must stay unchanged'}],
        'widgetInstances': {
            'generate-1': {'id': 'generate-1', 'typeId': 'generate', 'version': 1,
                'extension': {'opaque': ['α', False, None]}, 'state': {'futureState': {'a': 1}, 'values': {
                    'positivePrompt': 'before', 'negativePrompt': 'negative untouched',
                    'modelKey': 'not-loaded', 'futureValue': {'seed': 42}}}},
            'generate-2': {'id': 'generate-2', 'typeId': 'generate', 'state': {'values': {'positivePrompt': 'other widget'}}},
            'unknown-1': {'typeId': 'future-widget', 'state': {'values': {'opaque': ['a', 3, None]}}}},
        'futureWorkspace': {'__proto__': {'safe': True}, 'nested': [None, False, -123, 1.25, '🦊']}}
    created = native('POST', '/api/v1/projects/', {'name': content['name'], 'project_id': content['id'],
        'data': content, 'workspace': workspace, 'minimum_document_schema_version': 4,
        'max_document_schema_version': 4, 'minimum_canvas_schema_version': 2, 'max_canvas_schema_version': 2})
    ref = {**c['ref'], 'project_id': created['project_id']}
    path = '/api/v1/projects/'+ref['project_id']
    def read():
        return native('GET', path+QUERY)
    def get(via=op):
        return via('projects.get', ref)
    def request(rid, prompt='after', **extra):
        return {**ref, 'request_id': rid, 'widget_instance_id': 'generate-1', 'prompt': prompt,
            'expected_workspace_revision': get()['workspace_revision'], **extra}
    def put(p, via=op):
        return via('projects.savePrompt', p)
    def native_workspace(prompt, **opaque):
        before = read()
        data = deepcopy(before['workspace']['data'])
        data['widgetInstances']['generate-1']['state']['values']['positivePrompt'] = prompt
        data['futureWorkspace'].update(opaque)
        ack = native('PUT', path+'/workspace', {'data': data, 'expected_revision': before['workspace']['revision'],
            'max_document_schema_version': 4, 'max_canvas_schema_version': 2})
        after = read()
        assert after['workspace'] == ack and after['data'] == before['data']
        assert after['revision'] == before['revision']
        return after
    def verify_patch(before, after, prompt):
        expected = deepcopy(before['workspace']['data'])
        expected['widgetInstances']['generate-1']['state']['values']['positivePrompt'] = prompt
        assert after['workspace']['data'] == expected
        assert after['workspace']['revision'] == before['workspace']['revision']+1
        assert after['data'] == before['data'] and after['revision'] == before['revision']
        # Native summary exposes the caller's workspace timestamp separately too.
        unchanged = set(before) - {'workspace', 'workspace_updated_at'}
        assert {k: after[k] for k in unchanged} == {k: before[k] for k in unchanged}
        assert after['workspace_updated_at'] == after['workspace']['updated_at']
    before = read()
    assert before == created and before['data'] == content and before['workspace']['data'] == workspace
    assert get()['native'] == before and get(mcp)['native'] == before
    passed('running OpenAPI PUT-only workspace; API-created split-v4 layered opaque document read through HTTP and SDK MCP')
    receipts = []
    for transport, via in (('http', op), ('mcp', mcp)):
        p = request(transport+'-save', 'prompt '+transport)
        before = read()
        job = put(p, via)
        assert job['state'] == 'succeeded', job
        after = read()
        verify_patch(before, after, p['prompt'])
        assert job['result']['native'] == after and get()['native'] == after and get(mcp)['native'] == after
        count = len(puts())
        assert put(p, mcp if via == op else op) == job
        assert len(puts()) == count
        receipts.append((p, job))
    passed('installed HTTP/MCP selected-prompt CAS; exact direct native readback and complete untouched subtree equality; cross-transport replay')
    stale = request('stale-after-native-edit')
    reverse = native_workspace('native independent prompt', reverse={'nested': ['native', None]})
    assert get()['native'] == reverse and get(mcp)['native'] == reverse
    count = len(puts())
    refused = put(stale)
    assert refused['state'] == 'failed' and refused['error']['code'] == 'REVISION_CONFLICT'
    assert len(puts()) == count
    fail('projects.savePrompt', {**stale, 'prompt': 'rebound'}, 'IDEMPOTENCY_CONFLICT')
    passed('reverse native workspace edit visible fresh HTTP/MCP; old revision durably refused without PUT or request rebinding')
    independent = request('after-native-content-edit', 'workspace after content')
    before = read()
    changed = deepcopy(before['data'])
    changed['futureContent']['nativeEdit'] = {'nested': ['content', None]}
    native('PUT', path, {'name': before['name'], 'data': changed, 'expected_revision': before['revision'],
        'max_document_schema_version': 4, 'max_canvas_schema_version': 2})
    after_content = read()
    assert after_content['revision'] == before['revision']+1 and after_content['workspace'] == before['workspace']
    assert get()['native'] == after_content and get(mcp)['native'] == after_content
    assert put(independent)['state'] == 'succeeded'
    verify_patch(after_content, read(), independent['prompt'])
    passed('native content revision independent of workspace; fresh content visible and previously read workspace CAS remains valid')
    raced = request('native-real-race')
    mode['workspace_before_put'] = lambda: native_workspace('race wins', race=True)
    refused = put(raced)
    assert refused['state'] == 'failed' and refused['error']['code'] == 'REVISION_CONFLICT'
    assert puts()[-1]['status'] == 409
    reverse = read()
    assert get()['native'] == reverse and reverse['workspace']['data']['futureWorkspace']['race']
    p = request('concurrent-a', 'concurrent selected')
    with ThreadPoolExecutor() as pool:
        rows = list(pool.map(put, [p, {**p, 'request_id': 'concurrent-b'}]))
    assert sorted(x['state'] for x in rows) == ['failed', 'succeeded'], rows
    passed('real native race after preflight produces native HTTP409; concurrent independent intents admit only one PUT')
    count = len(puts())
    for key, value in (('connection_id', 'other'), ('backend_generation', 'other')):
        fail('projects.get', {**ref, key: value}, 'NATIVE_SCOPE_MISMATCH')
        fail('projects.savePrompt', {**request('scope-'+key), key: value}, 'NATIVE_SCOPE_MISMATCH')
    fail('projects.get', {**ref, 'project_id': '..'}, 'VALIDATION')
    bad = request('invalid-schema', expected_workspace_revision='4')
    error = fail('projects.savePrompt', bad, 'VALIDATION')['error']
    # Use raw SDK to compare structured errors (normal helper asserts success).
    async def invalid_mcp():
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        async with httpx.AsyncClient(headers=c['headers'], trust_env=False) as client:
            async with streamable_http_client(c['au']+'/mcp/', http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    return (await session.call_tool('projects_savePrompt', bad)).structuredContent
    assert asyncio.run(invalid_mcp())['error'] == error
    for widget in ('missing', 'unknown-1'):
        j = put(request('unsupported-widget-'+widget, widget_instance_id=widget))
        assert j['state'] == 'failed' and j['error']['code'] == 'WORKSPACE_CODEC_UNSUPPORTED', j
    assert len(puts()) == count
    stop('atlas')
    settings = json.loads(c['config'].read_text())
    settings['connections'][0]['enable_writes'] = False
    c['config'].write_text(json.dumps(settings))
    start_atlas()
    fail('projects.savePrompt', request('disabled-write'), 'ADMIN_WRITES_DISABLED')
    assert len(puts()) == count
    stop('atlas')
    settings['connections'][0]['enable_writes'] = True
    c['config'].write_text(json.dumps(settings))
    start_atlas()
    passed('scope/generation/path/schema/shared-MCP/widget and disabled-write refusals; zero mutation on refusal')
    # Unknown ACK does not become success just because its prompt/revision matches.
    for fault in ('lost-ack', 'crash-after-commit', 'fail-readback'):
        p = request(fault, 'same literal prompt')
        before = read()
        count = len(puts())
        mode['workspace_fault'] = fault
        if fault == 'crash-after-commit':
            try:
                put(p)
                raise AssertionError('expected killed Atlas transport')
            except httpx.TransportError:
                pass
            c['processes']['atlas'].wait(timeout=10)
        else:
            j = put(p)
            assert j['state'] == 'ack_unknown', j
            assert bool(j.get('native_ack')) == (fault == 'fail-readback')
        after = read()
        verify_patch(before, after, p['prompt'])
        stop('atlas')
        stop('native')
        start_native()
        assert read() == after
        start_atlas()
        replay = put(p, mcp)
        assert replay['state'] == ('succeeded' if fault == 'fail-readback' else 'ack_unknown'), replay
        assert len(puts()) == count+1
        with sqlite3.connect(out/'state/registry.sqlite3') as db:
            state, data = db.execute('select state,data from jobs where request_id=?', (fault,)).fetchone()
            persisted = json.loads(data)
        assert persisted['payload'] == {'operation': 'projects.savePrompt', **p}
        assert persisted['prepared']['expected_revision'] == p['expected_workspace_revision']
        assert state == replay['state']
        save(fault+'.json', {'request': p, 'receipt': replay, 'native_before': before, 'native_after': after,
            'put_delta': len(puts())-count})
        passed(fault+': native commit/readback; both-daemon restart/replay exactly one PUT; durable ACK alone allows GET recovery')
    # ACK exists, but another native editor has moved the workspace: stay uncertain.
    late = request('late-edit-after-ack')
    count = len(puts())
    mode['workspace_fault'] = 'fail-readback'
    j = put(late)
    assert j['state'] == 'ack_unknown' and j['native_ack']
    native_workspace('native later edit', lateEdit=True)
    stop('atlas')
    start_atlas()
    replay = put(late)
    assert replay['state'] == 'ack_unknown' and replay['native_ack'] == j['native_ack']
    assert len(puts()) == count+1
    passed('durable ACK with intervening native edit remains unknown; exact revision/equality cannot establish current readback')
    count = len(puts())
    stop('atlas')
    stop('native')
    with sqlite3.connect(c['root']/'databases/invokeai.db') as db:
        migrations = [r[0] for r in db.execute('select version from migrations order by version')]
        assert migrations == list(range(35))
        assert db.execute('pragma integrity_check').fetchone()[0] == 'ok'
        assert db.execute('select count(*) from models').fetchone()[0] == 0
        assert db.execute('select count(*) from session_queue').fetchone()[0] == 0
    start_atlas()
    for p, job in receipts:
        assert put(p) == job and put(p, mcp) == job
    assert len(puts()) == count
    passed('completed local receipts survive Atlas/native restart and native outage through HTTP/MCP; zero new PUT; fresh migrations0-34/integrity, zero models/queue')
    # Binding cannot be silently changed after any intent has been captured.
    stop('atlas')
    settings['connections'][0]['backend_generation'] = 'replacement'
    c['config'].write_text(json.dumps(settings))
    import subprocess
    r = subprocess.run([sys.executable, '-I', '-B', '-m', 'atlas_engine.server', '--config', str(c['config'])],
        env=c['env'], cwd=out, capture_output=True, text=True, timeout=30)
    assert r.returncode != 0 and 'connection binding changed' in r.stderr, r.stderr
    save('binding-refusal.json', {'returncode': r.returncode, 'stderr': r.stderr})
    passed('persisted native connection generation replacement refused on restart')
    mutation_calls = [x for x in direct if x['method'] in ('POST', 'PUT', 'PATCH', 'DELETE')]
    summary = {'checks': c['checks'], 'atlas_workspace_puts': len(puts()),
        'atlas_put_statuses': [x['status'] for x in puts()], 'direct_native_mutations': len(mutation_calls),
        'direct_native_workspace_puts': sum(x['method'] == 'PUT' and x['path'].endswith('/workspace') for x in direct),
        'direct_native_content_puts': sum(x['method'] == 'PUT' and x['path'] == path for x in direct),
        'intent_checks': len(c['intent_checks']), 'migrations': migrations, 'models': 0, 'queue': 0,
        'boundary': 'original launcher/auth/services/fresh migrations; API-valid opaque JSON, NOT UI hydration or broad codecs'}
    assert summary['intent_checks'] == summary['atlas_workspace_puts']
    save('result.json', summary)
    print(json.dumps(summary), flush=True)
