"""Installed HTTP/SDK MCP onboarding on an owned synthetic source.

NEW --out only. Native inventory is explicitly synthetic; optional original
InvokeAI launcher supplies no-model catalog/registration-prerequisite evidence.
No product hooks, fake Corpus, registry edits, model downloads or GPU jobs.
"""
import argparse
import asyncio
import base64
from collections import Counter
from copy import deepcopy
import hashlib
import io
import ipaddress
import itertools
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import zipfile

import httpx
from PIL import Image
import atlas_engine


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def child(config, out):
    def audit(event, args):
        if event == 'socket.connect' and isinstance(args[1], tuple):
            if not ipaddress.ip_address(args[1][0]).is_loopback:
                with (out/'forbidden-egress.log').open('a') as f:
                    f.write(repr(args[1])+'\n')
                raise RuntimeError('non-loopback egress forbidden')
    sys.addaudithook(audit)
    from atlas_engine.server import main
    sys.argv = ['atlas', '--config', str(config)]
    main()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--wheel', type=Path)
    p.add_argument('--native-python', type=Path)
    p.add_argument('--acquisition', action='store_true', help='Synthetic HTTP native-install lifecycle plus complete onboarding')
    p.add_argument('--child', action='store_true')
    p.add_argument('--config', type=Path)
    a = p.parse_args()
    if a.child:
        return child(a.config, a.out)
    out = a.out.absolute()
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.umask(0o077)
    source = Path(__file__).absolute().parents[1]
    installed = Path(atlas_engine.__file__).parent
    expected = {f.name: sha(f.read_bytes()) for f in (source/'atlas_engine').glob('*.py')}
    actual = {f.name: sha(f.read_bytes()) for f in installed.glob('*.py')}
    assert expected == actual and installed != source/'atlas_engine'
    with zipfile.ZipFile(a.wheel) as z:
        payload = {n.removeprefix('atlas_engine/'): sha(z.read(n)) for n in z.namelist()
                   if n.startswith('atlas_engine/') and not n.endswith('/')}
    assert payload == expected
    def save(name, value):
        (out/name).write_text(json.dumps(value, indent=2, ensure_ascii=False))
    save('module-equality.json', {'modules': actual, 'installed': str(installed),
         'version': atlas_engine.__version__, 'wheel_sha256': sha(a.wheel.read_bytes())})
    checks, responses, calls, processes, logs = [], [], [], {}, []
    mode = {'crash': False, 'bad_version': False, 'bad_hash': False, 'lost_install_ack': False}
    install_posts=[]
    install_jobs={}
    install_inventory=[]
    def passed(text):
        checks.append(text)
        save('checks.json', checks)
        print('PASS', text, flush=True)
    ap = port()
    au = f'http://127.0.0.1:{ap}'
    token = secrets.token_urlsafe(36)
    headers = {'Authorization': 'Bearer '+token}
    (out/'clients.json').write_text(json.dumps([{'token': token, 'principal': 'acceptance', 'source': 'owned-source'}]))
    config = out/'atlas.json'
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(out/'home'), 'TMPDIR': os.environ['TMPDIR'],
           'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'CUDA_VISIBLE_DEVICES': '',
           'OMP_NUM_THREADS': '2', 'OPENBLAS_NUM_THREADS': '2', 'MPLCONFIGDIR': str(out/'mpl-cache')}
    (out/'home').mkdir()
    commands = []
    def start(name, argv, ready):
        commands.append({'name': name, 'argv': argv, 'cwd': str(out), 'env': env})
        save('commands.json', commands)
        log = (out/(name+'.log')).open('a'); logs.append(log)
        proc = subprocess.Popen(argv, cwd=out, env=env, stdout=log, stderr=log)
        processes[name] = proc
        for _ in range(1200):
            assert proc.poll() is None, (name, proc.returncode)
            try:
                if httpx.get(ready, timeout=1, trust_env=False).status_code < 500:
                    return proc
            except httpx.TransportError:
                pass
            time.sleep(.1)
        raise RuntimeError(name+' not ready')
    def stop(name):
        proc = processes.get(name)
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(40)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait(10)
    def start_atlas():
        return start('atlas', [sys.executable, '-I', '-B', str(Path(__file__).absolute()),
                    '--child', '--config', str(config), '--out', str(out)], au+'/health')
    def op(name, args, error=None):
        r = httpx.post(au+'/v1/ops/'+name, json=args, headers=headers, timeout=180, trust_env=False)
        r.raise_for_status(); j = r.json()
        responses.append({'operation': name, 'arguments': args, 'response': j})
        if error:
            assert not j['ok'] and j['error']['code'] == error, j
            return j
        assert j['ok'], j
        return j['result']
    async def sdk(name, args):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        async with httpx.AsyncClient(headers=headers, trust_env=False, timeout=180) as client:
            async with streamable_http_client(au+'/mcp/', http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert name.replace('.', '_') in {t.name for t in tools.tools}
                    j = (await session.call_tool(name.replace('.', '_'), args)).structuredContent
                    responses.append({'mcp': name, 'arguments': args, 'response': j})
                    assert j['ok'], j
                    return j['result']
    def mcp(name, args):
        return asyncio.run(sdk(name, args))
    def page(collection):
        rows, after = [], ''
        while True:
            r = op('corpus.page', {'collection': collection, 'limit': 100, 'after': after})
            rows += r['items']
            after = r.get('next_after')
            if not after:
                return rows
    def ledger():
        with sqlite3.connect(out/'state/registry.sqlite3') as db:
            return [{'id': x[0], 'state': x[1], **json.loads(x[2])} for x in db.execute("select id,state,data from jobs where kind='onboarding'")]
    # Sources contain literal values deliberately unsuitable for prompt inference.
    bodies, records, models, versions, inventory, params = {}, {}, {}, {}, [], {}
    base = None
    def make_inputs():
        for model_id, kind, native_kind in ((1, 'LORA', 'lora'), (2, 'Checkpoint', 'main')):
            vid = model_id*10
            offset = model_id*1000
            author = list(range(offset+1, offset+36))
            community = author[:5] + list(range(offset+101, offset+126))
            all_ids = sorted(set(author+community))
            for iid in all_ids:
                stream = io.BytesIO()
                Image.new('RGB', (2, 2), (iid % 255, model_id*40, (iid//255) % 255)).save(stream, format='PNG')
                bodies[iid] = stream.getvalue()
                meta = {'prompt': '  literal, (KEEP:1.25) <lora:opaque:-0.7>\nПривет  ',
                    'negativePrompt': '  negative, (RAW:0.25)\n', 'seed': '000042', 'cfgScale': '7.00',
                    'steps': 12, 'resources': [{'type': kind, 'modelVersionId': vid, 'weight': '-0.70', 'unknown': None}],
                    'unknownParameters': {'x': [None, False, ' untouched ']}}
                if iid in author[:2]:
                    meta.update(tags=['A', 'A', 'B'], negativeTags=['N'])
                if iid == author[-1]:
                    meta = None
                records[iid] = {'id': iid, 'url': base+'/original/'+str(iid)+'.png', 'meta': meta,
                    'postId': vid+1, 'modelVersionId': vid, 'user': {'id': model_id, 'username': 'owner'+str(model_id)},
                    'index': None, 'createdAt': '2026-01-02T03:04:05.006Z', 'unknown': {'null': None}}
            digest = sha(('owned synthetic identity '+kind).encode())
            name = kind+'.safetensors'
            inventory.append({'key': 'owned-'+kind, 'path': '/owned/'+name, 'type': native_kind, 'hash': 'sha256:'+digest})
            models[model_id] = {'id': model_id, 'type': kind, 'creator': {'username': 'owner'+str(model_id)},
                                'tags': ['model literal', 'do not split'], 'unknown': [None, {'keep': True}]}
            versions[vid] = {'id': vid, 'modelId': model_id, 'trainedWords': [' trigger phrase '],
                'files': [{'id': vid+2, 'name': name, 'hashes': {'SHA256': digest}, 'unknown': 'file',
                    'type':'Model','metadata':{'format':'SafeTensor'},
                    'downloadUrl':base+'/api/download/models/'+str(vid)+'?type=Model&format=SafeTensor'}],
                'images': [records[author[0]]], 'createdAt': '2026-01-01T00:00:00Z', 'unknown': {'keep': None}}
            params[model_id] = {'request_id': kind+'-initial', 'registered_key': 'owned-'+kind,
                'registered_path': '/owned/'+name, 'model_id': model_id, 'version_id': vid,
                'file_id': vid+2, 'file_name': name, 'hash_algorithm': 'SHA256', 'file_hash': digest}
            models[model_id]['_fixture_author'] = author
            models[model_id]['_fixture_community'] = community
    class Source(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            u=urlsplit(self.path);q=parse_qs(u.query)
            assert u.path=='/api/v2/models/install',self.path
            assert json.loads(self.rfile.read(int(self.headers['Content-Length'])))=={}
            selected=int(urlsplit(q['source'][0]).path.rsplit('/',1)[1])//10
            item=deepcopy(install_inventory[selected-1]);inventory.append(item)
            nid=len(install_posts)+1
            install_posts.append({'id':nid,'source':q['source'][0]})
            install_jobs[nid]={'id':nid,'status':'completed','config_out':{'key':item['key']}}
            if mode['lost_install_ack']:
                self.close_connection=True;return
            return self.send({'id':nid,'status':'waiting'})
        def do_GET(self):
            u = urlsplit(self.path); q = parse_qs(u.query)
            call = {'path': self.path, 'authorization_present': bool(self.headers.get('Authorization'))}
            calls.append(call)
            assert not call['authorization_present']
            if u.path == '/api/v2/models/':
                return self.send({'models': inventory})
            if u.path.startswith('/api/v2/models/install/'):
                return self.send(install_jobs[int(u.path.rsplit('/',1)[1])])
            if u.path.startswith('/original/'):
                iid = int(u.path.rsplit('/', 1)[1].split('.')[0])
                if mode['crash']:
                    # Crash only after an earlier representation has a committed
                    # SQLite completion receipt. No production fault hook needed.
                    jobs = [j for j in ledger() if j['payload']['request_id'] == mode['crash_request']]
                    if jobs and jobs[0].get('image_receipts'):
                        mode['crash'] = False
                        mode['crash_snapshot'] = deepcopy(jobs[0])
                        processes['atlas'].kill(); processes['atlas'].wait(10)
                        self.close_connection = True
                        return
                return self.send(bodies[iid], 'image/png')
            if u.path.startswith('/api/v1/model-versions/'):
                v = deepcopy(versions[int(u.path.rsplit('/', 1)[1])])
                if mode['bad_version']: v['id'] += 1
                if mode['bad_hash']: v['files'][0]['hashes']['SHA256'] = 'f'*64
                return self.send(v)
            if u.path.startswith('/api/v1/models/'):
                m = models[int(u.path.rsplit('/', 1)[1])]
                return self.send({k: v for k, v in m.items() if not k.startswith('_fixture')})
            if u.path.startswith('/api/trpc/'):
                query = json.loads(q['input'][0])['json']
                if u.path.endswith('post.getInfinite'):
                    vid = query['modelVersionId']; mid = vid//10
                    items = [{'id': vid+1, 'modelVersionId': vid, 'user': {'id': mid, 'username': 'owner'+str(mid)}, 'imageCount': 35}]
                    cursor = None
                else:
                    mid = (query['postId']-1)//10
                    ids = models[mid]['_fixture_author']
                    index = query.get('cursor', 0)
                    items = [records[i] for i in ids[index:index+18]]
                    cursor = index+18 if index+18 < len(ids) else None
                return self.send({'result': {'data': {'json': {'items': items, 'nextCursor': cursor}}}})
            if u.path == '/api/v1/images':
                mid = (int(q['modelVersionId'][0]) if 'modelVersionId' in q else int(q['postId'][0])-1)//10
                ids = models[mid]['_fixture_author' if 'postId' in q else '_fixture_community']
                # Explicit pagination and duplicates; quota must use unique sets.
                index = int(q.get('page', ['0'])[0]); size = 18
                items = [records[i] for i in ids[index:index+size]]
                if items: items.append(items[0])
                nxt = None
                if index+size < len(ids):
                    from urllib.parse import urlencode
                    nxt = base+u.path+'?'+urlencode({**{k: v[0] for k, v in q.items()}, 'page': index+size})
                return self.send({'items': items, 'metadata': {'nextPage': nxt}})
            self.send_error(404)
        def send(self, value, content_type='application/json'):
            raw = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(200); self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(raw))); self.end_headers()
            try: self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Source)
    base = f'http://127.0.0.1:{server.server_port}'
    make_inputs()
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    settings = {'root': 'state', 'clients_file': 'clients.json', 'port': ap, 'civitai_url': base,
        'connections': [{'id': 'synthetic-inventory', 'backend_generation': 'owned-fixture-v1', 'account_scope': 'owned',
                         'url': base, 'enable_writes': a.acquisition}]}
    config.write_text(json.dumps(settings))
    def image_gets():
        return Counter(c['path'] for c in calls if c['path'].startswith('/original/'))
    def canonical():
        return {c: page(c) for c in ('tags', 'loras', 'checkpoints', 'generations', 'tag_relations')}
    native_before = None
    def native_manifest():
        code = "import hashlib,json,invokeai;from pathlib import Path;r=Path(next(iter(invokeai.__path__)));print(json.dumps({str(p):{'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'inode':p.stat().st_ino,'realpath':str(p.resolve())} for p in list(r.rglob('*.py'))+[Path(__import__('sys').executable).with_name('invokeai-web')]}))"
        return json.loads(subprocess.check_output([str(a.native_python), '-I', '-B', '-c', code], text=True))
    try:
        if a.native_python:
            native_before = native_manifest()
            save('native-runtime-before.json', native_before)
            # Read-only reused original runtime; original launcher on a fresh root.
            native_url, native_token, native_catalog = native_probe(a.native_python, out, start, save, passed)
            (out/'native-token').write_text(native_token)
            native_settings = deepcopy(settings)
            native_settings['root'] = 'native-atlas-state'
            native_settings['connections'] = [{'id': 'original-no-model', 'backend_generation': 'owned-original-v1',
                'account_scope': 'owned', 'url': native_url, 'token_file': 'native-token', 'enable_writes': False}]
            config.write_text(json.dumps(native_settings)); start_atlas()
            observed = op('catalog.list', {})
            assert observed == native_catalog['models'] and mcp('catalog.list', {}) == observed
            for model in observed:
                assert op('catalog.get', {'key': model['key']})['type'] == 'unknown'
            passed('installed HTTP/SDK MCP catalog.list and HTTP catalog.get against original native unknown/no-model inventory, no typed fixture substitution')
            stop('atlas'); stop('native')
            config.write_text(json.dumps(settings))
        start_atlas()
        if a.acquisition:
            install_inventory[:]=deepcopy(inventory);inventory.clear()
            def acquired(jid):
                return next(j for j in op('models.acquire.page',{'after':'','limit':100})['items'] if j['id']==jid)
            scope={'installer':'native'}
            ids=[]
            for mid,via in ((1,op),(2,mcp)):
                args={'request_id':'acquire-'+str(mid),'urls':['https://civitai.red/models/'+str(mid)+'?modelVersionId='+str(mid*10)],**scope}
                batch=via('models.acquire.enqueue',args);jid=batch['items'][0]['id'];ids.append(jid)
                for _ in range(600):
                    job=acquired(jid)
                    if job['state'] in ('succeeded','failed','partial','needs_input','ack_unknown'):break
                    time.sleep(.1)
                assert job['state']=='succeeded',job
                assert len(install_posts)==mid,install_posts
                assert via('models.acquire.enqueue',args)['items'][0]['id']==jid
                result=op('models.onboard.get',{'job_id':job['onboarding']['job_id']})
                assert result['community']['observed']==30 and result['author']['observed']==35 and not result['partial'],result
            passed('synthetic native HTTP single-install LoRA/checkpoint -> typed exact hash/key -> all35 author plus30 unique community; HTTP/MCP exact replay')
            stop('atlas');start_atlas();time.sleep(1)
            assert len(install_posts)==2
            assert all(acquired(jid)['state']=='succeeded' for jid in ids)
            # Fresh state has no historical install receipt; dropped POST reply
            # must never be inferred from the now matching native inventory.
            stop('atlas');inventory.clear();mode['lost_install_ack']=True
            settings['root']='unknown-ack-state';config.write_text(json.dumps(settings));start_atlas()
            args={'request_id':'lost-ack','urls':['https://civitai.red/models/1?modelVersionId=10'],**scope}
            jid=op('models.acquire.enqueue',args)['items'][0]['id']
            for _ in range(200):
                job=acquired(jid)
                if job['state']=='ack_unknown':break
                time.sleep(.1)
            assert job['state']=='ack_unknown' and 'native_install_id' not in job,job
            stop('atlas');start_atlas();time.sleep(2)
            assert acquired(jid)['state']=='ack_unknown'
            assert op('models.acquire.enqueue',args)['items'][0]['state']=='ack_unknown'
            assert len(install_posts)==3
            assert not canonical()['generations']
            passed('lost native install ACK remains unknown across restart despite matching typed inventory; no reinstall or gallery side effects')
            save('acquisition-result.json',{'status':'passed','synthetic_native':True,'checks':checks,'install_posts':install_posts})
            return
        results = {}
        for mid, via in ((1, op), (2, mcp)):
            result = via('models.onboard', params[mid])
            assert result['state'] == 'succeeded' and not result['partial'], result
            author = set(models[mid]['_fixture_author']); community = set(models[mid]['_fixture_community'])
            assert result['requested'] == 30 and result['author']['observed'] == 35 and result['community']['observed'] == 30
            assert {s['image_id'] for s in result['sample']} == author | community
            for s in result['sample']:
                assert {m['stream'] for m in s['memberships']} == ({'author'} if s['image_id'] in author else set()) | ({'community'} if s['image_id'] in community else set())
                ref = s['file']; expected_bytes = bodies[s['image_id']]
                assert ref['sha256'] == sha(expected_bytes)
                got = (mcp if mid == 2 else op)('corpus.files.read', {'sha256': ref['sha256']})
                assert got['eof'] and base64.b64decode(got['base64']) == expected_bytes
            results[mid] = result
            passed(('LoRA' if mid == 1 else 'checkpoint')+' default30 community + entire35 author / 5 overlap / 60 unique; all original source bytes HTTP/MCP SHA256 readback')
        before = canonical()
        assert len(before['generations']) == 120 and len(before['loras']) == len(before['checkpoints']) == 1
        expected_tags = {'model literal', 'do not split', ' trigger phrase ', 'A', 'B', 'N'}
        assert {x['identity']['tag'] for x in before['tags']} == expected_tags
        expected_relations = {}
        counts = Counter([('positive', 'A'), ('positive', 'A'), ('positive', 'B'), ('negative', 'N')])
        endpoints = sorted(counts, key=lambda x: json.dumps({'tag': x[1], 'polarity': x[0]}, sort_keys=True, separators=(',', ':')))
        for left, right in itertools.combinations_with_replacement(endpoints, 2):
            n = counts[left]*counts[right] if left != right else counts[left]*(counts[left]-1)//2
            if n: expected_relations[tuple((p, t) for p, t in (left, right))] = (4, n*4)
        actual_relations = {tuple((e['polarity'], e['tag']) for e in x['identity']['endpoints']):
            (x['derived']['historical_support'], x['derived']['historical_pair_occurrences']) for x in before['tag_relations']}
        assert actual_relations == expected_relations, actual_relations
        for x in before['generations']:
            assert x['body']['source_record'] == records[x['identity']['image_id']]
        for mid, collection in ((1, 'loras'), (2, 'checkpoints')):
            body = before[collection][0]['body']
            assert body['source_version'] == versions[mid*10]
            assert body['source_model'] == {k: v for k, v in models[mid].items() if not k.startswith('_fixture')}
        save('canonical-before.json', before)
        passed('literal raw prompts/negative/resources/weights/parameters/null/unknown/timestamps; exact six tags and four signed relation identities/supports (no prompt parsing)')
        gets = image_gets()
        for mid in (1, 2):
            assert mcp('models.onboard', params[mid]) == results[mid]
            fresh = op('models.onboard.refresh', {'job_id': results[mid]['job_id'], 'request_id': 'refresh-'+str(mid)})
            assert fresh['state'] == 'succeeded' and not fresh['partial']
            assert fresh['refresh_of'] == results[mid]['job_id']
        assert image_gets() == gets and canonical() == before
        stop('atlas'); start_atlas()
        assert mcp('models.onboard.get', {'job_id': results[1]['job_id']}) == results[1]
        assert op('models.onboard', params[2]) == results[2] and canonical() == before
        passed('cross-transport exact replay / explicit refresh / daemon restart: identical canonical heads and relation evidence, zero new image GETs')
        for mid in (1, 2):
            crash = {**params[mid], 'request_id': 'crash-revalidate-'+str(mid), 'image_refresh': 'revalidate'}
            mode.update(crash=True, crash_request=crash['request_id'])
            try:
                op('models.onboard', crash)
                raise AssertionError('expected abrupt process death')
            except httpx.TransportError:
                pass
            processes['atlas'].wait(10)
            frozen = mode['crash_snapshot']; saved = frozen['image_receipts']
            assert len(saved) == 1 and frozen['state'] == 'running'
            completed_urls = [v['file']['source_url'].removeprefix(base) for v in saved.values()]
            counts_before = image_gets()
            save('crash-committed-job-'+str(mid)+'.json', frozen)
            ref = next(iter(saved.values()))['file']
            path = out/'state/corpus/files/sha256'/ref['sha256'][:2]/ref['sha256']
            original = path.read_bytes()
            path.write_bytes(b'owned corruption')
            start_atlas()
            op('corpus.files.read', {'sha256': ref['sha256']}, 'FILE_INTEGRITY')
            refused = op('models.onboard.resume', {'job_id': frozen['id']})
            assert refused['state'] == 'running' and refused['error']['code'] == 'FILE_INTEGRITY'
            path.write_bytes(original)
            resumed = mcp('models.onboard.resume', {'job_id': frozen['id']})
            assert resumed['state'] == 'succeeded' and not resumed['partial']
            assert all(image_gets()[u] == counts_before[u] for u in completed_urls)
            assert canonical() == before
        passed('both model kinds: real SIGKILL after durable file receipt; corrupt bytes refused on read/resume; repaired SDK resume does not repeat completed GETs or canonical history')
        for flag, code in (('bad_version', 'SOURCE_ID_MISMATCH'), ('bad_hash', 'FILE_HASH_MISMATCH')):
            mode[flag] = True
            failed = op('models.onboard', {**params[1], 'request_id': flag})
            mode[flag] = False
            assert failed['state'] == 'failed' and failed['error']['code'] == code, failed
        for field, value, code in (('registered_key', 'no-such-key', 'REGISTERED_KEY_MISMATCH'),
                ('registered_path', '/owned/not-selected', 'REGISTERED_FILE_MISMATCH'),
                ('file_name', 'not-selected.safetensors', 'SOURCE_FILE_MISMATCH')):
            failed = op('models.onboard', {**params[2], 'request_id': 'invalid-'+field, field: value})
            assert failed['state'] == 'failed' and failed['error']['code'] == code, failed
        inventory.append(deepcopy(inventory[0]))
        failed = op('models.onboard', {**params[1], 'request_id': 'duplicate-native-key'})
        assert failed['state'] == 'failed' and failed['error']['code'] == 'REGISTERED_KEY_MISMATCH'
        inventory.pop()
        bad_native = deepcopy(inventory[0]); inventory[0]['hash'] = params[1]['file_hash']
        failed = op('models.onboard', {**params[1], 'request_id': 'untyped-native'})
        assert failed['state'] == 'failed' and failed['error']['code'] == 'HASH_ALGORITHM_UNKNOWN'
        inventory[0] = bad_native
        assert canonical() == before
        passed('changed exact source version/hash and bare untyped native hash refused before canonical writes')
        stop('atlas')
        # Same binding/config, different source origin; no request to either source.
        settings['civitai_url'] = 'http://127.0.0.1:'+str(port())
        config.write_text(json.dumps(settings)); start_atlas(); count = len(calls)
        op('models.onboard.resume', {'job_id': frozen['id']}, 'SOURCE_BINDING_CHANGED')
        op('models.onboard.refresh', {'job_id': results[1]['job_id'], 'request_id': 'changed-source'}, 'SOURCE_BINDING_CHANGED')
        op('models.onboard', params[1], 'SOURCE_BINDING_CHANGED')
        assert len(calls) == count
        assert mcp('models.onboard.get', {'job_id': results[1]['job_id']}) == results[1]
        assert canonical() == before
        passed('changed operator source binding refuses resume/refresh/replay without sockets or new effects; historical get stays readable')
        save('summary.json', {'status': 'passed', 'version': atlas_engine.__version__, 'checks': checks,
            'counts': {c: len(rows) for c, rows in before.items()}, 'image_gets': dict(image_gets()),
            'native_boundary': 'synthetic exact inventory only; no loaded model or native install acceptance',
            'original_bytes': 'source-supplied URL bytes verified exactly; provider representation remains unknown',
            'live': 'untouched', 'docker': 'accepted dev12 commit6bce091; dev13 container not exercised'})
    finally:
        for name in processes: stop(name)
        server.shutdown(); server.server_close(); thread.join(10)
        for log in logs: log.close()
        save('responses.json', responses); save('source-calls.json', calls)
        save('cleanup.json', {'owned_running': sum(p.poll() is None for p in processes.values()), 'source_running': thread.is_alive()})
        if native_before is not None:
            native_after = native_manifest()
            save('native-runtime-after.json', native_after)
            assert native_before == native_after, 'reused original native runtime changed'
        assert not (out/'forbidden-egress.log').exists()


def native_probe(python, out, start, save, passed):
    np = port(); url = f'http://127.0.0.1:{np}'
    root = out/'native'; root.mkdir()
    (root/'invokeai.yaml').write_text(f'schema_version: 4.0.2\nhost: 127.0.0.1\nport: {np}\ndevice: cpu\ngeneration_devices: [cpu]\nprecision: float32\npatchmatch: false\nimage_index_enabled: false\nmultiuser: true\nstrict_password_checking: true\nscan_models_on_startup: false\n')
    start('native', [str(python), '-I', '-B', str(python.with_name('invokeai-web')), '--root', str(root)], url+'/api/v1/auth/status')
    def request(method, path, body=None, headers=None):
        r = httpx.request(method, url+path, json=body, headers=headers, timeout=60, trust_env=False)
        r.raise_for_status(); return r.json()
    password = secrets.token_urlsafe(32)+'aA7!'
    creds = {'email': 'onboarding@example.test', 'password': password}
    request('POST', '/api/v1/auth/setup', {**creds, 'display_name': 'Owned onboarding'})
    login = request('POST', '/api/v1/auth/login', {**creds, 'remember_me': False})
    headers = {'Authorization': 'Bearer '+login['token']}
    schema = request('GET', '/openapi.json')
    selected = {k: v for k, v in schema['paths'].items() if k.startswith('/api/v2/models')}
    save('native-model-openapi.json', {'paths': selected, 'schemas': schema['components']['schemas']})
    catalog = request('GET', '/api/v2/models/', headers=headers)
    assert catalog['models'] == []
    # Invalid owned local bytes exercise the genuine probe/installer prerequisite,
    # not a fabricated registered record or an attempt to load real weights.
    invalid = root/'owned-invalid.safetensors'; invalid.write_bytes(b'owned non-model fixture')
    r = httpx.post(url+'/api/v2/models/install', params={'source': str(invalid), 'inplace': 'true'}, json={}, headers=headers, trust_env=False, timeout=60)
    receipt = {'submit_status': r.status_code, 'submit': r.json()}
    if r.is_success:
        jid = r.json()['id']
        for _ in range(200):
            job = request('GET', '/api/v2/models/install/'+str(jid), headers=headers)
            if job['status'] in ('completed', 'error', 'cancelled'): break
            time.sleep(.1)
        receipt['job'] = job
        assert job['status'] == 'error' or (job['status'] == 'completed' and job['config_out']['type'] == 'unknown'), job
    after = request('GET', '/api/v2/models/', headers=headers)
    assert all(m['type'] == 'unknown' for m in after['models'])
    save('native-registration-boundary.json', {'catalog_before': catalog, 'catalog_after': after, 'invalid_local_file': receipt,
         'accepted': False, 'reason': 'Original API can retain non-model bytes as unknown; this is not registered LoRA/main or loaded-model evidence. Synthetic typed inventory used only for Atlas ingestion boundary.'})
    passed('original disposable native launcher/auth/no-model catalog and unknown-file registration; LoRA/checkpoint registration/loading explicitly unaccepted')
    return url, login['token'], after


if __name__ == '__main__':
    main()
