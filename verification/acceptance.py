"""Installed-package boundary acceptance; synthetic state, no live backends.
Run with the target venv Python -I and an explicit NEW --out directory.
"""
import argparse
import asyncio
import hashlib
import importlib.metadata
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import zipfile

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import atlas_engine


def child(config, violations):
    # Reject and record unintended network access, even if a worker catches it.
    def audit(event, args):
        if event == 'socket.connect':
            target = args[1]
            if isinstance(target, tuple):
                try:
                    allowed = ipaddress.ip_address(target[0]).is_loopback
                except ValueError:
                    allowed = False
                if not allowed:
                    with open(violations, 'a') as f:
                        f.write(repr(target) + '\n')
                    raise RuntimeError('non-loopback egress forbidden in acceptance')
    sys.addaudithook(audit)
    sys.argv = ['atlas', '--config', str(config)]
    from atlas_engine.server import main
    main()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--wheel', type=Path)
    p.add_argument('--docker-image', help='exercise the same scenarios via isolated bridge container')
    p.add_argument('--child', action='store_true')
    p.add_argument('--config', type=Path)
    a = p.parse_args()
    if a.child:
        return child(a.config, a.out / 'forbidden-egress.log')
    out = a.out.absolute()
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    source = Path(__file__).absolute().parents[1]
    installed = Path(atlas_engine.__file__).parent
    expected = {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in (source / 'atlas_engine').glob('*.py')}
    actual = {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in installed.glob('*.py')}
    assert actual == expected, 'installed/source module sets or bytes differ'
    assert installed != source / 'atlas_engine', 'source import is not installed-package acceptance'
    if a.wheel:
        with zipfile.ZipFile(a.wheel) as z:
            payload = {n.removeprefix('atlas_engine/'): hashlib.sha256(z.read(n)).hexdigest()
                       for n in z.namelist() if n.startswith('atlas_engine/') and not n.endswith('/')}
        assert payload == expected, 'wheel/source payload differs (or extra package data)'
    token = secrets.token_urlsafe(36)
    for name, value in [('token', token), ('clients.json', json.dumps([
        {'token': token, 'principal': 'acceptance', 'source': 'synthetic'}]))]:
        (out / name).write_text(value)
        (out / name).chmod(0o600)
    host = '127.0.0.2'  # exercise a configured bind, not the daemon default
    with socket.socket() as sock:
        sock.bind((host, 0))
        port = sock.getsockname()[1]
    config = out / 'atlas.json'
    settings = {'root': 'state', 'clients_file': 'clients.json', 'host': host, 'port': port}
    config.write_text(json.dumps(settings))
    endpoint = f'http://{host}:{port}'
    container = None
    if a.docker_image:
        sys.path.insert(0, str(Path(__file__).absolute().parent))
        from container_lifecycle import Container
        host = '127.0.0.1'
        with socket.socket() as sock:
            sock.bind((host, 0))
            port = sock.getsockname()[1]
        endpoint = f'http://{host}:{port}'
        settings = {'root': '/var/lib/atlas', 'clients_file': '/run/atlas/clients.json',
                    'host': '0.0.0.0', 'port': 12088, 'container_endpoint': endpoint}
        container = Container(a.docker_image, out, port, token, expected)
        container.configure(settings)
    headers = {'Authorization': 'Bearer ' + token}
    checks = []
    def passed(name):
        checks.append(name)
        print('PASS', name, flush=True)
    def op(name, args=None):
        r = httpx.post(endpoint + '/v1/ops/' + name, json=args or {}, headers=headers, trust_env=False, timeout=20)
        r.raise_for_status()
        return r.json()
    def ok(name, args=None):
        value = op(name, args)
        assert value['ok'], value
        return value['result']
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV')}
    home = out / 'empty-home'
    home.mkdir()
    env.update(HOME=str(home), HF_HUB_OFFLINE='1', ATLAS_INVOKE_TOKEN='ignored-env-credential')
    command = [sys.executable, '-I', '-B', str(Path(__file__).absolute()), '--child', '--out', str(out), '--config', str(config)]
    log = (out / 'daemon.log').open('wb')
    process = None
    def start():
        proc = container.start() if container else subprocess.Popen(command, cwd=out, env=env, stdout=log, stderr=log)
        for _ in range(150):
            if proc.poll() is not None:
                raise AssertionError('daemon exited; inspect ' + str(out / 'daemon.log'))
            try:
                if ok('status')['worker_alive']:
                    return proc
            except httpx.HTTPError:
                pass
            time.sleep(.1)
        proc.terminate()
        proc.wait(timeout=15)
        raise AssertionError('startup timed out')
    def stop(proc):
        proc.terminate()
        code = proc.wait(timeout=20)
        # Uvicorn restores/re-raises SIGTERM after completing ASGI shutdown.
        assert code in (0, -15), ('unexpected shutdown exit', code)
    ref = {'collection': 'tags', 'identity': {'tag': ' Slice Ω '}}
    put = {**ref, 'request_id': 'slice-put', 'expected_revision': 0,
           'body': {'literal': '  Ω\t\n ', 'unknown': [None, True, {'opaque': 7}]},
           'attribution': {'source': 'synthetic-owned'}}
    try:
        process = start()
        if container:
            container.verify()
            passed('non-root installed image; normal bridge; host-loopback mapping; read-only root')
        status = ok('status')
        caps = ok('capabilities')
        schema_response = httpx.get(endpoint + '/v1/schema', headers=headers, trust_env=False)
        schema_response.raise_for_status()
        schemas = schema_response.json()
        assert set(schemas) == set(caps['operations'])
        assert caps['canonical_corpus']['enabled'] and not caps['readiness']['vocabulary']
        assert not caps['readiness']['invoke']['configured']
        assert status['jobs'] == 0
        passed('installed package; empty core; HTTP status/capabilities/schema')
        for route in ('/v1/schema', '/v1/ops/status', '/mcp/', '/absent-artifact'):
            for auth in ({}, {'Authorization': 'Bearer wrong'}):
                r = httpx.post(endpoint + route, json={}, headers=auth, trust_env=False)
                assert r.status_code == 401, (route, r.status_code)
        for extra in ({'Host': 'evil.invalid'}, {'Origin': 'http://evil.invalid'}, {'Origin': 'null'}):
            assert httpx.get(endpoint + '/v1/schema', headers=headers | extra, trust_env=False).status_code == 403
        assert httpx.get(endpoint + '/v1/schema', headers=headers | {'Origin': endpoint}, trust_env=False).status_code == 200
        if container:
            for extra in ({'Host': f'localhost:{port}'}, {'Host': '127.0.0.1:12088'}, {'Origin': 'http://127.0.0.1:12088'}):
                assert httpx.get(endpoint + '/v1/schema', headers=headers | extra, trust_env=False).status_code == 403
        assert httpx.get(endpoint + '/', headers=headers, trust_env=False).status_code == 404
        passed('common auth gate; hostile Host/Origin refused; UI absent')
        assert ok('corpus.page', {'collection': 'tags'})['items'] == []
        receipt = ok('corpus.put', put)
        assert receipt['state'] == 'succeeded'
        stored = ok('corpus.get', ref)
        assert stored['body'] == put['body'] and stored['revision'] == 1
        passed('durable HTTP corpus put and exact Qdrant readback')
        cli = [sys.executable, '-I', '-B', '-m', 'atlas_engine.client', '--endpoint', endpoint, '--token-file', str(out / 'token')]
        def cli_call(name, args):
            r = subprocess.run(cli + [name, '--arguments', json.dumps(args)], cwd=out, env=env, capture_output=True, text=True, timeout=20)
            return r.returncode, json.loads(r.stdout)
        rc, reply = cli_call('corpus.put', put)
        assert rc == 0 and reply['result'] == receipt
        rc, reply = cli_call('corpus.get', ref)
        assert rc == 0 and reply['result'] == stored
        async def mcp_check():
            async with streamable_http_client(endpoint + '/mcp/', http_client=httpx.AsyncClient(headers=headers, trust_env=False)) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = (await session.list_tools()).tools
                    assert {t.name: t.inputSchema for t in tools} == {n.replace('.', '_'): s for n, s in schemas.items()}
                    reply = await session.call_tool('corpus_put', put)
                    assert not reply.isError and reply.structuredContent['result'] == receipt
                    reply = await session.call_tool('corpus_get', ref)
                    assert reply.structuredContent['result'] == stored
                    invalid = await session.call_tool('corpus_put', put | {'expected_revision': '0'})
                    return invalid.structuredContent
        invalid_mcp = asyncio.run(mcp_check())
        invalid_http = op('corpus.put', put | {'expected_revision': '0'})
        rc, invalid_cli = cli_call('corpus.put', put | {'expected_revision': '0'})
        assert rc == 1 and invalid_mcp == invalid_http == invalid_cli
        assert invalid_http['error']['code'] == 'VALIDATION'
        assert op('corpus.put', put | {'body': {'changed': True}})['error']['code'] == 'IDEMPOTENCY_CONFLICT'
        passed('real MCP initialize/list/call; CLI/HTTP/MCP same intent, schemas and validation')
        duplicate = (container.exec(['atlas', '--config', '/run/atlas/atlas.json']) if container else
                     subprocess.run([sys.executable, '-I', '-m', 'atlas_engine.server', '--config', str(config)], cwd=out, env=env, capture_output=True, text=True, timeout=20))
        assert duplicate.returncode != 0 and 'STATE_BUSY' in duplicate.stderr
        owner_command = [sys.executable, '-I', '-c',
            'from pathlib import Path;from atlas_engine.corpus import Corpus;from atlas_engine.repository import Repository;import sys;Corpus(Path(sys.argv[1]),Repository(Path(sys.argv[2])))',
            str(out / 'state/corpus'), str(out / 'second-ledger')]
        owner = (container.exec(['python', '-I', '-c', owner_command[3], '/var/lib/atlas/corpus', '/tmp/second-ledger']) if container else
                 subprocess.run(owner_command, cwd=out, env=env, capture_output=True, text=True, timeout=20))
        assert owner.returncode != 0 and 'another process owns' in owner.stderr
        passed('second daemon and independent Qdrant owner refused')
        assert {'workflows.schema', 'workflows.run'} <= set(schemas)
        workflow_ref = {'connection_id': 'absent', 'backend_generation': 'synthetic', 'workflow_id': 'absent'}
        for name, args in [('workflows.schema', workflow_ref),
                           ('workflows.run', workflow_ref | {'request_id': 'workflow-unconfigured',
                            'version_semantics': 'latest_at_execution', 'workflow_inputs': {}, 'return_key': 'result'})]:
            assert op(name, args)['error']['code'] == 'INVOKE_UNCONFIGURED'
        passed('current workflow public schemas present; missing backend refused without native execution')
        assert op('catalog.list')['error']['code'] == 'INVOKE_UNCONFIGURED'
        assert op('enrich', {'tags': ['literal']})['error']['code'] == 'VOCABULARY_UNAVAILABLE'
        passed('optional backends fail explicitly without fallback')
        stop(process)
        process = None
        process = start()
        assert ok('corpus.get', ref) == stored
        assert ok('corpus.put', put) == receipt
        history = ok('corpus.history', ref)
        assert len(history['items']) == 1
        passed('restart preserves bytes/history and replay does not duplicate revision')
        stop(process)
        process = None
        for change, message in [({'host': '0.0.0.0', 'container_endpoint': None}, 'loopback'),
                                ({'host': '0.0.0.0', 'container_endpoint': 'http://evil.invalid:12088'}, 'container_endpoint requires'),
                                ({'connections': [{'id': 'later', 'url': 'http://127.0.0.1:9', 'backend_generation': 'fixture', 'account_scope': 'fixture'}]}, 'binding changed')]:
            config.write_text(json.dumps(settings | change))
            if container:
                container.configure(settings | change)
                container.start()
                for _ in range(150):
                    if container.poll() is not None:
                        break
                    time.sleep(.1)
                refused = subprocess.CompletedProcess([], container.poll(), '', container.run('logs', container.name).stderr)
            else:
                refused = subprocess.run([sys.executable, '-I', '-m', 'atlas_engine.server', '--config', str(config)], cwd=out, env=env, capture_output=True, text=True, timeout=20)
            assert refused.returncode not in (None, 0) and message in refused.stderr, refused.stderr
        config.write_text(json.dumps(settings))
        if container:
            container.configure(settings)
        passed('non-loopback bind and changed persisted connection binding refused')
        executable = Path(sys.executable).parent / 'atlas'
        assert subprocess.run([str(executable), '--help'], cwd=out, env=env, capture_output=True).returncode == 0
        command = [str(executable), '--config', str(config)]
        process = start()
        assert ok('corpus.get', ref) == stored
        stop(process)
        process = None
        passed('installed atlas entrypoint starts and reads the same durable corpus')
        assert not (out / 'forbidden-egress.log').exists()
        passed('normal Docker bridge; no external backend configured (no child egress audit)' if container else 'no non-loopback startup/operation egress under audit guard')
        report = {'status': 'pass', 'checks': checks, 'python': sys.version,
                  'package': importlib.metadata.version('atlas-engine'),
                  'modules': expected, 'operation_count': len(schemas),
                  'corpus_revision': stored['revision'], 'receipt': receipt,
                  'limits': ['Only exercised corpus operation/validation parity; not whole transport parity.',
                             'No InvokeAI, inference, populated-corpus migration or restore acceptance.',
                             'Docker accepted only when --docker-image is supplied; no live deployment.']}
        (out / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=20)
        log.close()
        if container:
            container.cleanup()


if __name__ == '__main__':
    main()
