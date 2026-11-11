"""Source/built-package acceptance against a deterministic NON-native HTTP fixture.

No upstream source, credentials, production config or live proxy is used. Fixture
implements only the pinned projects GET/workspace PUT contract, not InvokeAI's
router/auth/service/SQLite stack. Pass --package-root for built-package checks.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


REF = {'connection_id': 'disposable', 'backend_generation': 'fixture-generation-1', 'project_id': 'fixture-project'}


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def child(config, package_root):
    sys.path.insert(0, str(package_root))
    def audit(event, args):
        if event == 'socket.connect' and isinstance(args[1], tuple):
            if not ipaddress.ip_address(args[1][0]).is_loopback:
                raise RuntimeError('acceptance forbids non-loopback egress')
    sys.addaudithook(audit)
    from atlas_engine.server import main
    sys.argv = ['atlas', '--config', str(config)]
    main()


class NativeFixture:
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = []
        self.mode = ''
        self.fail_read = False
        self.process = None
        self.record = {
            'project_id': REF['project_id'], 'name': 'Fixture', 'revision': 9,
            'minimum_document_schema_version': 4, 'minimum_canvas_schema_version': 2,
            'data': {'documentSchemaVersion': 4, 'id': REF['project_id'], 'name': 'Fixture',
                     'canvas': {'opaque': [1, {'unknown': True}]}, 'futureContent': 'preserved'},
            'workspace': {'revision': 4, 'updated_at': 'fixture', 'data': {
                'documentSchemaVersion': 4, 'settingsLayering': 1,
                'layout': {'opaque': ['dock', None]}, 'settings': {'future': 7},
                'promptHistory': [{'positivePrompt': 'history must not change'}],
                'widgetInstances': {'generate-1': {'typeId': 'generate', 'extension': 'opaque',
                    'state': {'futureState': [True, None], 'values': {
                        'positivePrompt': 'before', 'negativePrompt': 'untouched',
                        'modelKey': 'not-loaded', 'futureValue': {'seed': 42}}}}},
                'futureWorkspace': {'widgets': [1, 2, 3]}}}}
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def respond(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def do_GET(self):
                with fixture.lock:
                    if self.path.startswith('/api/v1/projects/'):
                        fixture.calls.append({'method': 'GET', 'path': self.path})
                        assert self.path == '/api/v1/projects/fixture-project?max_document_schema_version=4&max_canvas_schema_version=2'
                        if fixture.fail_read:
                            fixture.fail_read = False
                            return self.respond(503, {})
                        return self.respond(200, fixture.record)
                    if self.path == '/api/v1/queue/default/item_ids?order_dir=ASC':
                        return self.respond(200, {'item_ids': [], 'total_count': 0})
                    self.respond(404, {})
            def do_PUT(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                with fixture.lock:
                    fixture.calls.append({'method': 'PUT', 'path': self.path, 'body': body})
                    assert self.path == '/api/v1/projects/fixture-project/workspace'
                    assert set(body) == {'data', 'expected_revision', 'max_document_schema_version', 'max_canvas_schema_version'}
                    assert body['max_document_schema_version'] == 4 and body['max_canvas_schema_version'] == 2
                    mode, fixture.mode = fixture.mode, ''
                    if mode == 'race':
                        fixture.record['workspace']['revision'] += 1
                    ws = fixture.record['workspace']
                    if body['expected_revision'] != ws['revision']:
                        return self.respond(409, {'detail': {'current_revision': ws['revision']}})
                    if mode != 'crash-before-commit':
                        fixture.record['workspace'] = {'data': body['data'], 'revision': ws['revision'] + 1, 'updated_at': 'fixture'}
                    if mode in ('crash-after-commit', 'crash-before-commit'):
                        os.kill(fixture.process.pid, signal.SIGKILL)
                    if mode in ('lost-ack', 'crash-after-commit', 'crash-before-commit'):
                        self.close_connection = True
                        self.connection.shutdown(socket.SHUT_RDWR)
                        return
                    if mode == 'fail-readback':
                        fixture.fail_read = True
                    return self.respond(200, fixture.record['workspace'])
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def puts(self):
        return sum(c['method'] == 'PUT' for c in self.calls)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--package-root', type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument('--child', action='store_true')
    parser.add_argument('--config', type=Path)
    a = parser.parse_args()
    if a.child:
        return child(a.config, a.package_root)
    out = a.out.absolute()
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    package_root = a.package_root.absolute()
    fixture = NativeFixture()
    token = secrets.token_urlsafe(36)
    clients = out / 'clients.json'
    clients.write_text(json.dumps([{'token': token, 'principal': 'acceptance', 'source': 'fixture'}]))
    clients.chmod(0o600)
    port = free_port()
    endpoint = f'http://127.0.0.1:{port}'
    config = out / 'atlas.json'
    config.write_text(json.dumps({'root': 'state', 'clients_file': 'clients.json', 'port': port,
        'connections': [{'id': REF['connection_id'], 'backend_generation': REF['backend_generation'],
                         'account_scope': 'fixture-user', 'url': f'http://127.0.0.1:{fixture.server.server_port}',
                         'enable_writes': True}]}))
    headers = {'Authorization': 'Bearer ' + token}
    checks = []
    process = None
    log = (out / 'daemon.log').open('w')
    def start():
        nonlocal process
        process = subprocess.Popen([sys.executable, '-I', str(Path(__file__).absolute()), '--child',
            '--out', str(out), '--config', str(config), '--package-root', str(package_root)],
            cwd=out, stdout=log, stderr=log, env={**os.environ, 'HOME': str(out / 'empty-home')})
        fixture.process = process
        for _ in range(200):
            if process.poll() is not None:
                raise RuntimeError('daemon failed; see daemon.log')
            try:
                if op('status', {})['ok']:
                    return
            except httpx.TransportError:
                pass
            time.sleep(.05)
        raise RuntimeError('daemon readiness timeout')
    def stop():
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=45)
    def op(name, args):
        r = httpx.post(endpoint + '/v1/ops/' + name, json=args, headers=headers, trust_env=False, timeout=20)
        r.raise_for_status()
        return r.json()
    def get():
        r = op('projects.get', REF)
        assert r['ok'], r
        return r['result']
    def save(rid, prompt='after', **extra):
        return {**REF, 'request_id': rid, 'widget_instance_id': 'generate-1', 'prompt': prompt,
                'expected_workspace_revision': get()['workspace_revision'], **extra}
    def put(p):
        r = op('projects.savePrompt', p)
        assert r['ok'], r
        return r['result']
    async def mcp(name, args):
        async with httpx.AsyncClient(headers=headers, trust_env=False, timeout=20) as client:
            async with streamable_http_client(endpoint + '/mcp/', http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    return (await session.call_tool(name, args)).structuredContent
    def passed(name):
        checks.append(name)
        print('PASS', name, flush=True)
    try:
        start()
        before = get()
        content = deepcopy(before['native']['data'])
        workspace = deepcopy(before['native']['workspace']['data'])
        p = save('http-save')
        receipt = put(p)
        assert receipt['state'] == 'succeeded', receipt
        after = get()
        workspace['widgetInstances']['generate-1']['state']['values']['positivePrompt'] = 'after'
        assert after['native']['workspace']['data'] == workspace
        assert after['native']['data'] == content and after['content_revision'] == before['content_revision']
        assert after['workspace_revision'] == before['workspace_revision'] + 1
        passed('HTTP native-shaped read/CAS/readback; opaque fields and content unchanged')
        count = fixture.puts()
        assert asyncio.run(mcp('projects_savePrompt', p))['result']['id'] == receipt['id']
        assert fixture.puts() == count
        p2 = save('mcp-save', 'MCP edited')
        m = asyncio.run(mcp('projects_savePrompt', p2))
        assert m['ok'] and m['result']['state'] == 'succeeded', m
        assert asyncio.run(mcp('projects_get', REF))['result']['workspace_revision'] == get()['workspace_revision']
        passed('SDK MCP successful save/read and cross-transport exact replay')
        stale = put({**p, 'request_id': 'stale'})
        assert stale['state'] == 'failed' and stale['error']['code'] == 'REVISION_CONFLICT'
        assert fixture.puts() == count + 1
        assert op('projects.savePrompt', {**p, 'prompt': 'rebind'})['error']['code'] == 'IDEMPOTENCY_CONFLICT'
        assert op('projects.get', {**REF, 'backend_generation': 'other'})['error']['code'] == 'NATIVE_SCOPE_MISMATCH'
        assert op('projects.get', {**REF, 'project_id': '..'})['error']['code'] == 'VALIDATION'
        invalid = {**p, 'expected_workspace_revision': '4'}
        assert op('projects.savePrompt', invalid)['error'] == asyncio.run(mcp('projects_savePrompt', invalid))['error']
        passed('stale preflight refusal, payload rebinding, scope and shared schema errors')
        fixture.mode = 'race'
        raced = put(save('native-race'))
        assert raced['state'] == 'failed' and raced['error']['code'] == 'REVISION_CONFLICT'
        p3 = save('concurrent-a')
        with ThreadPoolExecutor() as pool:
            rows = list(pool.map(put, [p3, {**p3, 'request_id': 'concurrent-b'}]))
        assert sorted(j['state'] for j in rows) == ['failed', 'succeeded']
        passed('native 409 race and concurrent independent intents; no fresh-revision retry')
        # Content and workspace own independent revisions; editing one must not stale the other.
        independent = save('independent')
        with fixture.lock:
            fixture.record['revision'] += 1
            fixture.record['data']['futureContent'] = 'external edit'
        assert put(independent)['state'] == 'succeeded'
        assert get()['native']['data']['futureContent'] == 'external edit'
        passed('external content change does not conflict with workspace CAS')
        saved_record = deepcopy(fixture.record)
        fixture.record['workspace']['data']['documentSchemaVersion'] = 5
        unsupported = op('projects.get', REF)
        assert unsupported['error']['code'] == 'WORKSPACE_CODEC_UNSUPPORTED'
        fixture.record = saved_record
        passed('future codec fails closed')
        preflight = save('preflight-unavailable')
        count = fixture.puts()
        fixture.fail_read = True
        refused = put(preflight)
        assert refused['state'] == 'failed' and refused['error']['code'] == 'REMOTE_HTTP_503'
        assert put(preflight)['id'] == refused['id'] and fixture.puts() == count
        passed('preflight read failure is durably refused; replay never PUTs')
        for mode in ('lost-ack', 'crash-after-commit', 'crash-before-commit', 'fail-readback'):
            p4 = save(mode, 'same prompt deliberately')
            fixture.mode = mode
            count = fixture.puts()
            if mode.startswith('crash-'):
                try:
                    put(p4)
                    raise AssertionError('daemon should have crashed')
                except httpx.TransportError:
                    pass
                process.wait(timeout=10)
            else:
                unknown = put(p4)
                assert unknown['state'] == 'ack_unknown', unknown
            stop()
            start()
            replayed = put(p4)
            assert replayed['state'] == ('succeeded' if mode == 'fail-readback' else 'ack_unknown'), replayed
            assert fixture.puts() == count + 1
            # Re-read the durable binding, not just its transport response.
            import sqlite3
            with sqlite3.connect(out / 'state' / 'registry.sqlite3') as db:
                raw = db.execute('select data from jobs where request_id=?', (mode,)).fetchone()[0]
                persisted = json.loads(raw)
                assert persisted['payload']['backend_generation'] == REF['backend_generation']
                assert persisted['payload']['expected_workspace_revision'] == p4['expected_workspace_revision']
                assert persisted['prepared']['expected_revision'] == p4['expected_workspace_revision']
            passed(mode + ': restart/replay without another PUT; durable scoped intent')
        late = save('late-edit-after-ack')
        fixture.mode = 'fail-readback'
        count = fixture.puts()
        unknown = put(late)
        assert unknown['state'] == 'ack_unknown' and unknown['native_ack']
        with fixture.lock:
            fixture.record['workspace']['revision'] += 1
            fixture.record['workspace']['data']['futureWorkspace']['lateEdit'] = True
        stop()
        start()
        replay = put(late)
        assert replay['state'] == 'ack_unknown' and replay['native_ack'] == unknown['native_ack']
        assert fixture.puts() == count + 1
        passed('durable ACK plus intervening edit remains unknown; no inferred authorship or replay')
        count = fixture.puts()
        stop()
        start()
        assert put(p)['id'] == receipt['id'] and put(p)['state'] == 'succeeded'
        assert fixture.puts() == count
        passed('successful receipt survives restart without native replay')
        stop()
        settings = json.loads(config.read_text())
        settings['connections'][0]['backend_generation'] = 'replacement'
        config.write_text(json.dumps(settings))
        refused = subprocess.run([sys.executable, '-I', str(Path(__file__).absolute()), '--child', '--out', str(out),
            '--config', str(config), '--package-root', str(package_root)], capture_output=True, text=True, timeout=20)
        assert refused.returncode != 0 and 'connection binding changed' in refused.stderr
        passed('backend generation rebind refused across restart')
        (out / 'result.json').write_text(json.dumps({'acceptance': 'deterministic HTTP fixture, NOT native InvokeAI layering',
            'package_root': str(package_root), 'checks': checks, 'fixture_project_calls': fixture.calls}, indent=2))
        print(json.dumps({'passed': len(checks), 'native_layering_verified': False}))
    finally:
        stop()
        fixture.server.shutdown()
        fixture.server.server_close()
        log.close()


if __name__ == '__main__':
    main()
