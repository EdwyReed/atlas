"""Installed Atlas -> original installed InvokeAI launcher, no-model CPU acceptance.

Use an explicitly owned native Python with InvokeAI 7 installed and a NEW --out.
No service overrides, model downloads, synthetic callers/tables or UI build.
Private credentials and evidence are written only beneath --out.
"""
import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import zipfile

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import atlas_engine


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', required=True, type=Path)
    p.add_argument('--native-python', required=True, type=Path)
    p.add_argument('--wheel', required=True, type=Path)
    p.add_argument('--images-boards', action='store_true', help='exercise native image/board lifecycle instead of integer graphs')
    p.add_argument('--workspace', action='store_true', help='exercise native split-v4 projects/workspace instead of integer graphs')
    p.add_argument('--image-workflows', action='store_true', help='exercise bounded saved IMAGE resize recipe')
    a = p.parse_args()
    assert sum((a.images_boards,a.workspace,a.image_workflows)) <= 1, 'select one acceptance slice'
    out = a.out.absolute()
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.umask(0o077)
    source = Path(__file__).absolute().parents[1]
    installed = Path(atlas_engine.__file__).parent
    expected = {x.name: hashlib.sha256(x.read_bytes()).hexdigest() for x in (source/'atlas_engine').glob('*.py')}
    actual = {x.name: hashlib.sha256(x.read_bytes()).hexdigest() for x in installed.glob('*.py')}
    assert installed != source/'atlas_engine' and actual == expected
    with zipfile.ZipFile(a.wheel) as z:
        payload = {n.removeprefix('atlas_engine/'): hashlib.sha256(z.read(n)).hexdigest()
                   for n in z.namelist() if n.startswith('atlas_engine/') and not n.endswith('/')}
    assert payload == expected
    (out/'module-equality.json').write_text(json.dumps({'source': str(source), 'installed': str(installed), 'modules': actual,
        'version': importlib.metadata.version('atlas-engine'), 'wheel_sha256': hashlib.sha256(a.wheel.read_bytes()).hexdigest()}, indent=2))
    def native_manifest():
        code="""import hashlib,json,os,invokeai;from pathlib import Path
root=Path(next(iter(invokeai.__path__)))
files=list(root.rglob('*.py'))+[Path(__import__('sys').executable).with_name('invokeai-web')]
print(json.dumps({str(p):{'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'inode':p.stat().st_ino,'realpath':str(p.resolve())} for p in files},sort_keys=True))"""
        return json.loads(subprocess.check_output([str(a.native_python),'-I','-B','-c',code],text=True))
    native_before=native_manifest()
    (out/'native-runtime-before.json').write_text(json.dumps(native_before,indent=2))
    np, ap = free_port(), free_port()
    nu, au = f'http://127.0.0.1:{np}', f'http://127.0.0.1:{ap}'
    root = out/'native'
    root.mkdir(mode=0o700)
    (root/'invokeai.yaml').write_text(f'schema_version: 4.0.2\nhost: 127.0.0.1\nport: {np}\ndevice: cpu\ngeneration_devices: [cpu]\nprecision: float32\npatchmatch: false\nimage_index_enabled: false\nmultiuser: true\nstrict_password_checking: true\nscan_models_on_startup: false\nlog_level: debug\n')
    home = out/'empty-home'
    home.mkdir()
    env = {'PATH': str(a.native_python.parent)+':/usr/bin:/bin', 'HOME': str(home), 'TMPDIR': os.environ['TMPDIR'],
           'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'CUDA_VISIBLE_DEVICES': '',
           'OMP_NUM_THREADS': '2', 'OPENBLAS_NUM_THREADS': '2', 'MPLCONFIGDIR': str(out/'mpl-cache')}
    native_argv = [str(a.native_python), '-I', '-B', str(a.native_python.with_name('invokeai-web')), '--root', str(root)]
    processes, all_processes, logs, calls, responses, checks = {}, [], [], [], [], []
    intent_checks=[]
    native_headers = {}
    mode = {'lost': False, 'missing': False, 'stale': False, 'drop_path': None, 'crash_atlas': False, 'fail_image_get': False}
    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def forward(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            content_type = self.headers.get('Content-Type', '')
            record = {'method': self.command, 'path': self.path, 'body': None}
            if body and content_type.startswith('application/json'):
                record['body'] = json.loads(body)
            elif body:
                record['body_bytes'] = len(body)  # Engine.IO polling is not JSON.
            calls.append(record)
            workspace_put = a.workspace and self.command == 'PUT' and self.path.endswith('/workspace')
            if workspace_put:
                # The original API receives only an already committed Atlas intent.
                with sqlite3.connect(out/'state/registry.sqlite3') as db:
                    rows = db.execute("select id,data from jobs where kind='native_workspace' and state='submitting'").fetchall()
                    assert len(rows) == 1, rows
                    job = json.loads(rows[0][1])
                    assert job['prepared'] == record['body']
                    assert job['payload']['connection_id'] == 'disposable'
                    assert job['payload']['backend_generation'] == 'native-fullserver-1'
                    assert self.path == '/api/v1/projects/'+job['payload']['project_id']+'/workspace'
                    intent_checks.append({'job_id': rows[0][0], 'request_id': job['payload']['request_id'],
                        'prepared': job['prepared'], 'payload': job['payload'], 'committed_before_native_forward': True})
                hook = mode.pop('workspace_before_put', None)
                if hook:
                    hook()
            if a.images_boards and self.command=='POST' and self.path.startswith(('/api/v1/images/upload','/api/v1/boards/','/api/v1/board_images/')):
                # Independent SQLite connection observes committed intent BEFORE
                # forwarding the only mutation to the original native server.
                with sqlite3.connect(out/'state/registry.sqlite3') as db:
                    db.row_factory=sqlite3.Row
                    rows=[dict(x) for x in db.execute("select id,state,data from jobs where kind='native_admin' and state='submitting'")]
                    assert len(rows)==1,rows
                    job=json.loads(rows[0]['data']);payload=job['payload']
                    assert payload['connection_id']=='disposable' and payload['backend_generation']=='native-fullserver-1'
                    if self.path.startswith('/api/v1/images/upload'):
                        u=db.execute('select * from uploads where id=?',(payload['upload_id'],)).fetchone()
                        assert u['state']=='importing' and json.loads(u['data'])['import_job_id']==rows[0]['id']
                        assert hashlib.sha256(u['bytes']).hexdigest()==job['prepared']['sha256']
                        assert json.loads(u['data'])['import_intent']=={k:v for k,v in payload.items() if k!='operation'}
                    intent_checks.append({'job_id':rows[0]['id'],'operation':payload['operation'],'scope':{k:payload[k] for k in ('connection_id','backend_generation')},'committed_before_native_forward':True})
            if a.image_workflows and self.command=='POST' and self.path.endswith('/enqueue_batch'):
                with sqlite3.connect(out/'state/registry.sqlite3') as db:
                    rows=db.execute("select id,data from jobs where kind='generation' and state='submitting'").fetchall()
                    assert len(rows)==1,rows
                    job=json.loads(rows[0][1])
                    assert job['prepared_submission']==record['body']
                    intent_checks.append({'job_id':rows[0][0],'prepared':job['prepared_submission'],'committed_before_native_forward':True})
                hook=mode.pop('enqueue_before_forward',None)
                if hook:hook()
            h = {'Content-Type': content_type} if content_type else {}
            if self.headers.get('Authorization'):
                h['Authorization'] = self.headers['Authorization']
            try:
                r = httpx.request(self.command, nu+self.path, content=body or None, headers=h, trust_env=False, timeout=40)
            except httpx.TransportError:
                r = httpx.Response(503, json={'detail': 'isolated native offline'})
            record['status'] = r.status_code
            if workspace_put and mode.get('workspace_fault'):
                fault = mode.pop('workspace_fault')
                assert r.is_success, r.text
                if fault == 'fail-readback':
                    mode['workspace_fail_get'] = True
                else:
                    assert fault in ('lost-ack', 'crash-after-commit')
                    if fault == 'crash-after-commit':
                        processes['atlas'].kill()
                        processes['atlas'].wait(timeout=10)
                    self.close_connection = True
                    return
            if a.workspace and self.command == 'GET' and self.path.startswith('/api/v1/projects/') and mode.pop('workspace_fail_get', False):
                r = httpx.Response(503, json={'detail': 'injected workspace verification read failure'})
            if mode['missing'] and '/item_ids?' in self.path:
                r = httpx.Response(200, json={'item_ids': [], 'total_count': 0})
            if mode['stale'] and self.path.endswith('/items_by_ids') and r.status_code == 200:
                rows = r.json()
                for row in rows:
                    row['status'] = 'pending'
                    row['session']['results'] = {}
                r = httpx.Response(200, json=rows)
            if mode['lost'] and self.path.endswith('/enqueue_batch'):
                mode['lost'] = False
                assert r.status_code < 300, r.text
                if mode.pop('crash_enqueue',False):
                    processes['atlas'].kill();processes['atlas'].wait(timeout=10)
                self.close_connection = True
                return
            if self.command=='POST' and mode.get('drop_path') and self.path.startswith(mode['drop_path']):
                mode['drop_path']=None
                assert r.is_success,r.text
                if mode.get('crash_atlas'):
                    processes['atlas'].kill()
                    processes['atlas'].wait(timeout=10)
                    mode['crash_atlas']=False
                self.close_connection=True
                return
            if self.command=='GET' and mode.get('fail_image_get') and self.path.startswith('/api/v1/images/i/'):
                mode['fail_image_get']=False
                r=httpx.Response(503,json={'detail':'injected verification read failure'})
            if self.command=='GET' and mode.get('replace_full') and self.path.endswith('/full'):
                r=httpx.Response(200,content=mode.pop('replace_full'),headers={'Content-Type':'image/png'})
            try:
                self.send_response(r.status_code)
                self.send_header('Content-Type', r.headers.get('Content-Type', 'application/octet-stream'))
                self.send_header('Content-Length', str(len(r.content)))
                self.end_headers()
                self.wfile.write(r.content)
            except (BrokenPipeError, ConnectionResetError):
                pass  # A stopping Socket.IO client can abandon its outstanding poll.
        do_GET = do_POST = do_PUT = do_PATCH = forward
    proxy = ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    token = secrets.token_urlsafe(36)
    (out/'clients.json').write_text(json.dumps([{'token': token, 'principal': 'acceptance', 'source': 'isolated-fullserver'}]))
    (out/'policy.json').write_text(json.dumps({'accepted_modes': ['native_graph', 'saved_workflow']}))
    config = out/'atlas.json'
    headers = {'Authorization': 'Bearer '+token}
    def save(name, value):
        (out/name).write_text(json.dumps(value, indent=2))
    def passed(text):
        checks.append(text)
        save('checks.json', checks)
        print('PASS', text, flush=True)
    def launch(name, argv, url):
        log = (out/(name+'.log')).open('a')
        logs.append(log)
        proc = subprocess.Popen(argv, env=env, cwd=out, stdout=log, stderr=log)
        processes[name] = proc
        all_processes.append((name, proc))
        save('commands.json', {'native': native_argv, 'atlas': [sys.executable, '-I', '-B', '-m', 'atlas_engine.server', '--config', str(config)], 'env': env})
        for _ in range(1200):
            assert proc.poll() is None, f'{name} startup failed: {proc.returncode}'
            try:
                if httpx.get(url, trust_env=False, timeout=1).status_code < 500:
                    return
            except httpx.TransportError:
                pass
            time.sleep(.1)
        raise RuntimeError(name+' readiness timeout')
    def start_native():
        launch('native', native_argv, nu+'/api/v1/auth/status')
    def start_atlas():
        launch('atlas', [sys.executable, '-I', '-B', '-m', 'atlas_engine.server', '--config', str(config)], au+'/health')
    def stop(name):
        proc = processes.get(name)
        if proc and proc.poll() is None:
            proc.send_signal(signal.SIGINT if name == 'native' else signal.SIGTERM)
            try:
                proc.wait(timeout=45)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise AssertionError(name+' did not stop gracefully')
    def native(method, path, body=None):
        r = httpx.request(method, nu+path, json=body, headers=native_headers, trust_env=False, timeout=40)
        assert r.is_success, (r.status_code, r.text)
        return r.json()
    def op(name, args):
        r = httpx.post(au+'/v1/ops/'+name, json=args, headers=headers, trust_env=False, timeout=45)
        r.raise_for_status()
        j = r.json()
        responses.append({'op': name, 'args': args, 'response': j})
        assert j['ok'], j
        return j['result']
    async def mcp(name, args):
        async with httpx.AsyncClient(headers=headers, trust_env=False) as client:
            async with streamable_http_client(au+'/mcp/', http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    j = (await session.call_tool(name, args)).structuredContent
                    assert j['ok'], j
                    responses.append({'mcp': name, 'args': args, 'response': j})
                    return j['result']
    def posts():
        return sum(c['method'] == 'POST' and c['path'].endswith('/enqueue_batch') for c in calls)
    def observe(jid):
        op('observer.reconcile', {})
        return op('generation.get', {'job_id': jid})
    def complete(job):
        for _ in range(400):
            item = native('GET', f"/api/v1/queue/default/i/{job['native_ids'][0]}")
            if item['status'] in ('completed', 'failed', 'canceled'):
                break
            time.sleep(.1)
        save('last-native-item.json', item)
        assert item['status'] == 'completed', item
        for _ in range(100):
            result = observe(job['id'])
            if result['state'] == 'succeeded':
                return result, item
            time.sleep(.1)
        raise AssertionError(result)
    def value(job, workflow=False):
        assert job['state'] == 'succeeded', job
        assert len(job['native_ids']) == 1 and job['outputs'] == []
        receipt = job['native_items'][str(job['native_ids'][0])]
        entries = receipt['native_results']['entries']
        assert len(entries) == 1 and entries[0]['status'] == 'available' and type(entries[0]['value']) is int and entries[0]['value'] == 17, job
        assert entries[0]['type'] == ('workflow_return_output' if workflow else 'integer_output')
        assert receipt['session_id'] and receipt['graph_id'] and job['native_scope']['backend_generation'] == 'native-fullserver-1'
        if workflow:
            assert entries[0]['return_key'] == 'result' and job['workflow_observation']['executed_revision'] is None
        return job
    def node(nid, kind, inputs):
        return {'id': nid, 'type': 'invocation', 'position': {'x': 0, 'y': 0}, 'data': {'id': nid, 'type': kind, 'version': '1.0.0', 'nodePack': 'invokeai', 'isIntermediate': True, 'useCache': False, 'inputs': {k: {'value': v} for k, v in inputs.items()}}}
    def edge(src, sh, dst, dh):
        return {'id': src+'-'+dst, 'type': 'default', 'source': src, 'sourceHandle': sh, 'target': dst, 'targetHandle': dh}
    ref = {'connection_id': 'disposable', 'backend_generation': 'native-fullserver-1'}
    graph = {**ref, 'request_id': 'integer17', 'graph': {'id': 'explicit-integer17', 'nodes': {'n': {'id': 'n', 'type': 'integer', 'value': 17, 'is_intermediate': True, 'use_cache': False}}, 'edges': []}, 'runs': 1}
    workflow = {'name': 'Owned fullserver integer callable', 'author': 'isolated acceptance', 'description': '', 'version': '1.0.0', 'contact': '', 'tags': '', 'notes': '', 'meta': {'version': '1.0.0', 'category': 'user'}, 'exposedFields': [{'nodeId': 'n', 'fieldName': 'value'}], 'form': None, 'nodes': [node('n', 'integer', {'value': 0}), node('rv', 'workflow_return_value', {'key': 'result', 'value': None}), node('ret', 'workflow_return', {'values': []})], 'edges': [edge('n', 'value', 'rv', 'value'), edge('rv', 'value', 'ret', 'values')]}
    try:
        start_native()
        initial = native('GET', '/api/v1/auth/status')
        assert initial['setup_required'] and initial['multiuser_enabled'] and initial['strict_password_checking']
        password = secrets.token_urlsafe(32)+'aA7!'
        credentials = {'email': 'atlas-acceptance@example.test', 'password': password}
        # Never persist credentials or tokens in response/call inventories.
        setup = native('POST', '/api/v1/auth/setup', {**credentials, 'display_name': 'Atlas isolated acceptance'})
        assert setup['success']
        assert not native('GET', '/api/v1/auth/status')['setup_required']
        negative = {}
        for label, h in [('missing', {}), ('wrong', {'Authorization': 'Bearer invalid-isolated-token'})]:
            r = httpx.get(nu+'/api/v1/queue/default/status', headers=h, trust_env=False)
            assert r.status_code in (401, 403), r.text
            negative[label] = r.status_code
        r = httpx.post(nu+'/api/v1/auth/login', json={**credentials, 'password': 'wrong'}, trust_env=False)
        assert r.status_code == 401
        negative['wrong_password'] = r.status_code
        login = native('POST', '/api/v1/auth/login', {**credentials, 'remember_me': False})
        native_headers['Authorization'] = 'Bearer '+login['token']
        (out/'native-token').write_text(login['token'])
        user = native('GET', '/api/v1/auth/me')
        assert user['user_id'] == setup['user']['user_id'] and user['is_admin']
        r = httpx.post(nu+'/api/v1/auth/setup', json={**credentials, 'display_name': 'Rejected repeated setup'}, trust_env=False)
        assert r.status_code == 400
        negative['repeat_setup'] = r.status_code
        save('auth.json', {'initial': initial, 'configured': native('GET', '/api/v1/auth/status'), 'user_id': user['user_id'], 'negatives': negative})
        config.write_text(json.dumps({'root': 'state', 'clients_file': 'clients.json', 'policy_file': 'policy.json', 'port': ap, 'connections': [{'id': 'disposable', 'backend_generation': 'native-fullserver-1', 'account_scope': user['user_id'], 'url': f'http://127.0.0.1:{proxy.server_port}', 'token_file': 'native-token', 'enable_writes': True}]}))
        passed('original launcher/bootstrap/multiuser setup/login/me; missing/wrong token/password and repeated setup refused')
        start_atlas()
        if a.image_workflows:
            sys.path.insert(0,str(Path(__file__).absolute().parent))
            from native_image_workflows import run
            run(locals())
            return
        if a.workspace:
            sys.path.insert(0, str(Path(__file__).absolute().parent))
            from native_workspace_fullserver import run
            run(locals())
            return
        if a.images_boards:
            sys.path.insert(0,str(Path(__file__).absolute().parent))
            from native_images_boards import run
            run(locals())
            return
        for _ in range(100):
            event_status = op('status', {})['native_events']
            if event_status and event_status.get('connected'):
                break
            time.sleep(.1)
        save('native-events-connect.json', event_status)
        assert event_status and event_status.get('connected'), event_status
        for h in ({}, {'Authorization': 'Bearer wrong'}):
            assert httpx.post(au+'/v1/ops/generation.submitGraph', json=graph, headers=h, trust_env=False).status_code in (401, 403)
        j = op('generation.submitGraph', graph)
        gdone, gitem = complete(j)
        value(gdone)
        value(asyncio.run(mcp('generation_get', {'job_id': j['id']})))
        assert asyncio.run(mcp('generation_submitGraph', graph))['id'] == j['id']
        save('graph.json', {'job': gdone, 'native': gitem})
        passed('installed HTTP explicit native integer17 graph -> original registry/queue/processor -> HTTP and SDK MCP result17')
        for _ in range(100):
            event_status = op('status', {})['native_events']
            if event_status and event_status.get('connected') and event_status.get('events_seen', 0) > 0:
                break
            time.sleep(.1)
        assert event_status and event_status.get('connected') and event_status.get('events_seen', 0) > 0, event_status
        save('native-events-observed.json', event_status)
        passed('authenticated original Socket.IO polling connects/subscribes and delivers native queue hints; authoritative GET hydration retained')
        created = native('POST', '/api/v1/workflows/', {'workflow': workflow})
        wid = created['workflow_id']
        assert created['revision'] == 1 and native('GET', '/api/v1/workflows/i/'+wid)['workflow'] == created['workflow']
        wref = {**ref, 'workflow_id': wid}
        schema = op('workflows.schema', wref)
        assert schema['observed_revision'] == 1 and list(schema['workflow_inputs']) == ['saved_workflow_input::n::value']
        assert asyncio.run(mcp('workflows_schema', wref)) == schema
        request = {**wref, 'request_id': 'workflow17', 'version_semantics': 'latest_at_execution', 'workflow_inputs': {'saved_workflow_input::n::value': 17}, 'return_key': 'result'}
        wj = asyncio.run(mcp('workflows_run', request))
        wdone, witem = complete(wj)
        value(wdone, True)
        value(asyncio.run(mcp('generation_get', {'job_id': wj['id']})), True)
        save('workflow.json', {'schema': schema, 'job': wdone, 'native': witem})
        passed('original authenticated workflow create/readback -> HTTP/MCP schema -> SDK MCP run -> native callable child -> integer17')
        count = posts()
        stop('atlas')
        stop('native')
        start_native()
        assert native('GET', '/api/v1/auth/me')['user_id'] == user['user_id']
        assert not native('GET', '/api/v1/auth/status')['setup_required']
        start_atlas()
        value(op('generation.submitGraph', graph))
        value(op('workflows.run', request), True)
        assert asyncio.run(mcp('generation_submitGraph', graph))['native_ids'] == j['native_ids']
        assert asyncio.run(mcp('workflows_run', request))['native_ids'] == wj['native_ids']
        assert posts() == count == 2
        passed('both original native and Atlas restarted; native auth survives; scoped HTTP/MCP replay no extra enqueue')
        mode['lost'] = True
        lost_request = {**request, 'request_id': 'lost-ack-workflow17'}
        lost = asyncio.run(mcp('workflows_run', lost_request))
        lost = observe(lost['id'])
        assert len(lost['native_ids']) == 1, lost
        count = posts()
        stop('atlas')
        start_atlas()
        lost2 = op('workflows.run', lost_request)
        assert lost2['id'] == lost['id'] and lost2['native_ids'] == lost['native_ids'] and posts() == count
        ldone, litem = complete(lost2)
        value(ldone, True)
        save('lost-ack.json', {'job': ldone, 'native': litem})
        mode['stale'] = True
        value(observe(wj['id']), True)
        mode['stale'] = False
        mode['missing'] = True
        missing = observe(wj['id'])
        assert missing['history_missing']
        value(missing, True)
        mode['missing'] = False
        passed('dropped post-admission reply; GET receipt recovery/restart replay without duplicate root/child; stale/missing history preserves return')
        stop('atlas')
        stop('native')
        with sqlite3.connect(root/'databases/invokeai.db') as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute('select item_id,parent_item_id,batch_id,status,user_id from session_queue')]
            tables = [r[0] for r in db.execute("select name from sqlite_master where type='table' order by name")]
            migrations = [dict(r) for r in db.execute('select * from migrations')]
            assert sorted(r['version'] for r in migrations) == list(range(35)), migrations
            schema_sql = [dict(r) for r in db.execute("select name,sql from sqlite_master where type in ('table','index') order by name")]
            users = [dict(r) for r in db.execute('select user_id,is_admin,is_active,token_epoch from users')]
            assert db.execute('pragma integrity_check').fetchone()[0] == 'ok'
            model_count = db.execute('select count(*) from models').fetchone()[0]
        roots = [r for r in rows if not r['parent_item_id']]
        children = [r for r in rows if r['parent_item_id']]
        assert len(roots) == posts() == 3 and len(children) == 2, rows
        assert all(r['status'] == 'completed' and r['user_id'] == user['user_id'] for r in rows)
        assert sum(r['parent_item_id'] == wj['native_ids'][0] for r in children) == 1
        assert sum(r['parent_item_id'] == lost['native_ids'][0] for r in children) == 1
        assert model_count == 0
        save('database.json', {'tables': tables, 'schema': schema_sql, 'migrations': migrations, 'users': users, 'queue_rows': rows, 'root_count': len(roots), 'child_count': len(children), 'models': model_count, 'integrity_check': 'ok'})
        passed('migrated original SQLite integrity; exactly 3 roots/2 children, authenticated ownership, all completed, zero model records')
        start_atlas()
        value(op('generation.submitGraph', graph))
        value(op('workflows.run', request), True)
        value(asyncio.run(mcp('generation_get', {'job_id': lost['id']})), True)
        assert posts() == 3
        passed('native offline: completed HTTP/MCP local results/replay preserved without enqueue')
        save('result.json', {'checks': checks, 'enqueue_posts': posts(), 'root_count': len(roots), 'child_count': len(children), 'graph_job_id': j['id'], 'workflow_job_id': wj['id'], 'lost_job_id': lost['id']})
    finally:
        try:
            stop('atlas')
        finally:
            stop('native')
            proxy.shutdown()
            proxy.server_close()
            thread.join(timeout=5)
            for log in logs:
                log.close()
            save('calls.json', calls)
            save('responses.json', responses)
            save('intent-checks.json',intent_checks)
            save('cleanup.json', {'children': [{'name': name, 'pid': proc.pid, 'returncode': proc.poll()} for name, proc in all_processes], 'owned_running': sum(proc.poll() is None for _, proc in all_processes), 'proxy_running': thread.is_alive()})
            native_after=native_manifest()
            save('native-runtime-after.json',native_after)
            assert native_before==native_after,'reused native package/launcher changed'


if __name__ == '__main__':
    main()
