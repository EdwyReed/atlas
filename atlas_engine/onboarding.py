"""Registered-file reuse + bounded Civitai gallery ingestion. No native writes.

Frozen request snapshots, ordered unique source IDs, independent image identities.
Canonical records/history are delegated to Corpus; jobs contain operational intent.
"""
import base64
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import hashlib
import ipaddress
import json
import os
import re
import socket
import shutil
import threading
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from typing import Callable
import httpx
from .contracts import Fault
from .corpus import entity_id
from .corpus_contracts import CorpusImport
from .repository import digest, dump


# Single serialized gallery transfer; disk-backed bodies never enter the journal.
IMAGE_MAX_BYTES = 32 * 1024 * 1024
VIDEO_MAX_BYTES = 256 * 1024 * 1024
VIDEO_SECONDS = 600


def file_sha256(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def sync_directory(path):
    fd = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)



def atomic(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.pending')
    with temporary.open('wb') as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class SourceDeadline:
    """Abort an owned HTTP connection even while headers arrive as a slow drip.

    HTTPX trace and httpcore network_stream/socket extras are public transport
    interfaces; no source client or credential state is shared with this socket.
    """
    def __init__(self, deadline):
        self.deadline = deadline
        self.lock = threading.Lock()
        self.socket = None
        self.expired = False

    def abort(self):
        with self.lock:
            self.expired = True
            if self.socket is not None:
                try:
                    self.socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def trace(self, event, info):
        if event in ('connection.connect_tcp.complete', 'connection.start_tls.complete'):
            with self.lock:
                if self.expired:
                    stream = info['return_value']
                    stream.close()
                    raise Fault('SOURCE_LIMIT', 'source request exceeds time budget')
                self.socket = info['return_value'].get_extra_info('socket')

    def __enter__(self):
        self.timer = threading.Timer(max(0, self.deadline - time.monotonic()), self.abort)
        self.timer.daemon = True
        self.timer.start()
        return self

    def __exit__(self, kind, value, traceback):
        self.timer.cancel()
        self.timer.join()
        if self.expired or time.monotonic() > self.deadline:
            raise Fault('SOURCE_LIMIT', 'source request exceeds time budget') from None


@contextmanager
def source_stream(client, url, headers, trace=None):
    # HTTPX parses Location even with follow_redirects=False. Keep malformed
    # protocol/location errors typed and do not expose their raw URL in logs.
    try:
        with client.stream('GET', url, headers=headers, extensions={'trace': trace}) as response:
            yield response
    except httpx.RemoteProtocolError:
        raise Fault('SOURCE_HTTP_PROTOCOL', 'invalid source HTTP response or redirect location') from None
    except httpx.TimeoutException:
        raise Fault('SOURCE_LIMIT', 'source request exceeds idle/time budget') from None
    except httpx.TransportError:
        raise Fault('SOURCE_HTTP_TRANSPORT', 'source transport interrupted') from None


def native_hash(model):
    raw = model.get('hash')
    if not isinstance(raw, str):
        raise Fault('HASH_ALGORITHM_UNKNOWN', 'registered inventory has no typed hash')
    match = re.fullmatch(r'(sha256|blake3):([a-fA-F0-9]{64})', raw, re.I)
    if match:
        return match[1].upper(), match[2].lower()
    algorithm = model.get('hash_algorithm')
    if algorithm in ('SHA256', 'BLAKE3') and re.fullmatch(r'[a-fA-F0-9]{64}', raw):
        return algorithm, raw.lower()
    raise Fault('HASH_ALGORITHM_UNKNOWN', 'bare native hash is not proof of SHA256 or BLAKE3')


def numeric(record):
    # No prompt parser, implicit polarity, splitting, case folding or weights.
    meta = record.get('meta')
    meta = meta if isinstance(meta, dict) else {}
    positive = meta.get('tags', record.get('tags'))
    negative = meta.get('negativeTags', record.get('negativeTags'))
    def literal(value):
        return value if isinstance(value, list) and all(isinstance(t, str) for t in value) else None
    positive, negative = literal(positive), literal(negative)
    if any(v is not None and len(v) > 64 for v in (positive, negative)):
        return None, {'scope': 'explicit_source_arrays_only', 'status': 'occurrence_resource_bound', 'prompt_parsing': 'unknown'}
    occurrences = None if positive is None and negative is None else {
        'positive': positive or [], 'negative': negative or []}
    return occurrences, {'scope': 'explicit_source_arrays_only', 'positive_known': positive is not None,
                         'negative_known': negative is not None, 'prompt_parsing': 'unknown',
                         'status': 'no_tags_available' if occurrences is None else 'partial_semantics'}


class Onboarding:
    def __init__(self, engine, source_url='https://civitai.red'):
        self.e = engine
        self.base = source_url.rstrip('/')
        u = urlsplit(self.base)
        self.fixture = u.scheme == 'http' and u.hostname in ('127.0.0.1', 'localhost')
        if self.base != 'https://civitai.red' and not self.fixture:
            raise ValueError('Civitai endpoint must be civitai.red or explicit owned loopback fixture')
        self.lock = threading.RLock()  # one bounded import/download at a time; readers remain free
        self.fault_hook: Callable[[str, dict], None] | None = None

    def hook(self, event, detail):
        if self.fault_hook:
            self.fault_hook(event, detail)

    @property
    def files(self):
        return self.e.corpus.root / 'files'

    def path(self, sha):
        return self.files / 'sha256' / sha[:2] / sha

    def verify_file(self, ref):
        path = self.path(ref['sha256'])
        if not path.is_file() or path.stat().st_size != ref['size'] or file_sha256(path) != ref['sha256']:
            raise Fault('FILE_INTEGRITY', 'saved image reference failed byte/hash verification')
        return path

    def read_file(self, p):
        path = self.path(p['sha256'])
        if not path.is_file():
            raise Fault('NOT_FOUND', 'corpus file not found')
        size = path.stat().st_size
        self.verify_file({'sha256': p['sha256'], 'size': size})
        with path.open('rb') as f:
            f.seek(p['offset'])
            raw = f.read(p['size'])
        return {'sha256': p['sha256'], 'size': size, 'offset': p['offset'],
                'base64': base64.b64encode(raw).decode(), 'next_offset': p['offset'] + len(raw),
                'eof': p['offset'] + len(raw) >= size}

    def url(self, value, image=False):
        # Reject controls before urllib can silently strip/normalize them.
        if not isinstance(value, str) or not value or re.search(r'[\x00-\x20\x7f\\]|%(?:0[0-9a-f]|1[0-9a-f]|7f)', value, re.I):
            raise Fault('SOURCE_URL', 'invalid source URL characters')
        try:
            value = urljoin(self.base + '/', value)
            u = urlsplit(value)
            port = u.port
        except ValueError:
            raise Fault('SOURCE_URL', 'malformed source URL') from None
        if u.username is not None or u.password is not None or u.fragment or any(k.lower() in ('token', 'access_token', 'api_key') for k in parse_qs(u.query, keep_blank_values=True)):
            raise Fault('SOURCE_URL', 'credential-bearing URL not journaled or followed')
        if self.fixture and (u.scheme, u.netloc) == (urlsplit(self.base).scheme, urlsplit(self.base).netloc):
            return value
        if u.scheme != 'https' or not u.hostname or port not in (None, 443):
            raise Fault('SOURCE_URL', 'expected public HTTPS source URL')
        # Literal IP/private hosts are not a gallery download authorization.
        try:
            address = ipaddress.ip_address(u.hostname)
        except ValueError:
            address = None
        if (address and not address.is_global) or u.hostname in ('localhost',) or '.' not in u.hostname:
            raise Fault('SOURCE_URL', 'private image endpoint not authorized')
        if any(not ipaddress.ip_address(result[4][0]).is_global for result in socket.getaddrinfo(u.hostname, u.port or 443, type=socket.SOCK_STREAM)):
            raise Fault('SOURCE_URL', 'source hostname resolves to private endpoint')
        if u.hostname in ('civitai.com', 'www.civitai.com') or (u.path.startswith('/api/') and u.hostname != 'civitai.red'):
            raise Fault('SOURCE_URL', 'Civitai site/API requests must use civitai.red')
        if not image and u.netloc != urlsplit(self.base).netloc:
            raise Fault('SOURCE_URL', 'pagination must stay on configured source')
        return value

    def request(self, url, headers=None, maximum=4*1024*1024, *, image=False, validator_url=None, sink=None, seconds=60):
        # Only images navigate redirects. Validators belong to the exact URL
        # that returned their representation, never to another hop/target.
        headers = headers or {}
        if set(headers) - {'Accept-Encoding', 'If-None-Match', 'If-Modified-Since'}:
            raise Fault('SOURCE_HEADERS', 'unsupported source request headers')
        current = self.url(url, image=image)
        seen = set()
        deadline = time.monotonic() + seconds
        for hop in range(6):  # initial request plus at most five redirects
            if current in seen:
                raise Fault('SOURCE_REDIRECT', 'image redirect cycle')
            seen.add(current)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Fault('SOURCE_LIMIT', 'source request exceeds time budget')
            outgoing = {'User-Agent': 'AtlasEngine/0.3 metadata verification', 'Accept-Encoding': 'identity'}
            if current == validator_url:
                outgoing.update({k: v for k, v in headers.items() if k.startswith('If-')})
            # A fresh client per hop discards Set-Cookie, even on same-host hops.
            # No source/native clients, ambient proxy, netrc or auth are reused.
            with httpx.Client(timeout=min(20, remaining), follow_redirects=False, trust_env=False) as client:
                with SourceDeadline(deadline) as guard, source_stream(client, current, outgoing, guard.trace) as r:
                    if image and r.status_code in (301, 302, 303, 307, 308):
                        if hop == 5:
                            raise Fault('SOURCE_REDIRECT', 'image redirect hop bound exceeded')
                        locations = r.headers.get_list('location')
                        if len(locations) != 1:
                            raise Fault('SOURCE_REDIRECT', 'one image redirect location required')
                        # Validate literal Location before urljoin strips controls;
                        # then resolve relative to this hop, not the metadata base.
                        self.url(locations[0], image=True)
                        current = self.url(urljoin(current, locations[0]), image=True)
                        continue
                    if r.status_code == 304:
                        if not any(k.startswith('If-') for k in outgoing):
                            raise Fault('SOURCE_SHAPE', '304 without representation-bound validator')
                        return None, dict(r.headers), current
                    if r.status_code != 200:
                        retry = r.headers.get('retry-after')
                        until = time.time() + 60
                        if retry:
                            try:
                                until = time.time() + max(0, int(retry))
                            except ValueError:
                                try:
                                    until = parsedate_to_datetime(retry).timestamp()
                                except (ValueError, TypeError):
                                    pass
                        exc = Fault('SOURCE_HTTP_' + str(r.status_code), 'source rejected request; no auth/rate-limit retry or fallback')
                        setattr(exc, 'retry_at', until)
                        raise exc
                    if r.headers.get('content-encoding', 'identity').lower() != 'identity':
                        raise Fault('SOURCE_SHAPE', 'encoded source body not supported')
                    length = r.headers.get('content-length')
                    if length is not None:
                        if len(length) > 20 or not re.fullmatch(r'[0-9]+', length):
                            raise Fault('SOURCE_SHAPE', 'invalid source content length')
                        length = int(length)
                        if length > maximum:
                            raise Fault('SOURCE_LIMIT', 'source response exceeds byte/time budget')
                    if sink is not None and shutil.disk_usage(self.files).free < maximum + 16 * 1024 * 1024:
                        raise Fault('SOURCE_STORAGE', 'insufficient gallery storage headroom')
                    raw = bytearray()
                    size, sha = 0, hashlib.sha256()
                    # Do not ask HTTPX to aggregate a fixed-size chunk: a slow
                    # drip could keep resetting its idle timeout while hiding
                    # received bytes from our wall-clock deadline checks.
                    for chunk in r.iter_raw():
                        if size + len(chunk) > maximum or time.monotonic() > deadline:
                            raise Fault('SOURCE_LIMIT', 'source response exceeds byte/time budget')
                        size += len(chunk)
                        if sink is None:
                            raw.extend(chunk)
                        else:
                            sink.write(chunk)
                            sha.update(chunk)
                    if time.monotonic() > deadline:
                        raise Fault('SOURCE_LIMIT', 'source response exceeds time budget')
                    if length is not None and size != length:
                        raise Fault('SOURCE_HTTP_PROTOCOL', 'source body length mismatch')
                    body = bytes(raw) if sink is None else {'sha256': sha.hexdigest(), 'size': size}
                    return body, dict(r.headers), current
        raise Fault('SOURCE_REDIRECT', 'image redirect hop bound exceeded')

    def json(self, url):
        if not self.fixture:
            from .civitai_readonly import fetch
            return fetch(self.e.native_admin.policy, self.url(url))
        raw, _, _ = self.request(self.url(url), headers={'Accept-Encoding': 'identity'})
        if not isinstance(raw, bytes):
            raise Fault('SOURCE_SHAPE', 'unexpected metadata body')
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            raise Fault('SOURCE_SHAPE', 'source JSON expected') from None
        if not isinstance(value, dict):
            raise Fault('SOURCE_SHAPE', 'source object expected')
        return value

    def expected(self, collection, identity):
        with self.e.corpus.lock:
            head = self.e.corpus.head(collection, entity_id(collection, identity))
            return head['revision'] if head else 0

    def identity(self, p):
        return {'system': 'civitai', 'model_id': p['model_id'], 'version_id': p['version_id']}

    def verify(self, p, version, model):
        matches = [m for m in self.e.invoke.models() if m.get('key') == p['registered_key']]
        if len(matches) != 1:
            raise Fault('REGISTERED_KEY_MISMATCH', 'exact unique registered key required')
        registered = matches[0]
        collection = {'LORA': 'loras', 'LoCon': 'loras', 'Checkpoint': 'checkpoints'}.get(model.get('type'))
        if collection is None or registered.get('type') != {'loras': 'lora', 'checkpoints': 'main'}[collection]:
            raise Fault('MODEL_TYPE_MISMATCH', 'source/native kind mismatch or unsupported source model kind')
        if registered.get('path') != p['registered_path']:
            raise Fault('REGISTERED_FILE_MISMATCH', 'literal registered path differs')
        if any(type(actual) is not int or actual != expected for actual, expected in (
                (model.get('id'), p['model_id']), (version.get('id'), p['version_id']), (version.get('modelId'), p['model_id']))):
            raise Fault('SOURCE_ID_MISMATCH', 'source model/version identity differs')
        files = [f for f in version.get('files', []) if type(f.get('id')) is int and f.get('id') == p['file_id'] and f.get('name') == p['file_name']]
        if len(files) != 1:
            raise Fault('SOURCE_FILE_MISMATCH', 'exact unique source file id/name required')
        selected = files[0]
        algorithm, value = native_hash(registered)
        source_hash = selected.get('hashes', {}).get(p['hash_algorithm'])
        if algorithm != p['hash_algorithm'] or value != p['file_hash'].lower() or not isinstance(source_hash, str) or source_hash.lower() != value:
            raise Fault('FILE_HASH_MISMATCH', 'same confirmed hash algorithm and exact file digest required')
        return collection, selected, registered

    def save_image(self, record, refresh, job_id, index):
        # Operational completion belongs to this frozen intent/sample, not the
        # shared URL cache. A newer request must still perform its revalidation.
        receipt_key = str(index)
        binding = digest({'record': record, 'refresh': refresh})
        receipts = self.e.repo.get(job_id).get('image_receipts', {})
        completed = receipts.get(receipt_key)
        if completed:
            if completed['binding'] != binding:
                raise Fault('FILE_INTEGRITY', 'saved image receipt differs from frozen intent')
            self.verify_file(completed['file'])
            return completed['file']

        def complete(ref):
            receipts[receipt_key] = {'binding': binding, 'file': ref}
            self.e.repo.update(job_id, changes={'image_receipts': receipts}, event='onboarding.image_saved')
            return ref

        raw_url = record.get('url')
        if not isinstance(raw_url, str) or not raw_url:
            return {'status': 'unavailable', 'reason': 'source_url_missing', 'representation': 'unknown'}
        url = self.url(raw_url, image=True)
        cache = self.files / 'urls' / (digest(url) + '.json')
        prior = json.loads(cache.read_text()) if cache.exists() else None
        if prior:
            self.verify_file(prior)
            if refresh == 'reuse':
                return complete(prior)
        headers = {'Accept-Encoding': 'identity'}
        if prior:
            if prior.get('etag'):
                headers['If-None-Match'] = prior['etag']
            elif prior.get('last_modified'):
                headers['If-Modified-Since'] = prior['last_modified']
        video = record.get('type') == 'video'
        maximum = VIDEO_MAX_BYTES if video else IMAGE_MAX_BYTES
        seconds = VIDEO_SECONDS if video else 60
        staging = self.files / 'pending' / (digest(url) + '.download')
        staging.parent.mkdir(parents=True, exist_ok=True)
        # A killed attempt leaves at most one bounded partial per URL. It is not
        # evidence of completion; retry truncates it, never appends mixed bytes.
        try:
            with staging.open('wb') as sink:
                body, response, representation_url = self.request(
                    url, headers, maximum, image=True, sink=sink, seconds=seconds,
                    validator_url=prior.get('representation_url', url) if prior else None)
                if body is None:
                    if not prior:
                        raise Fault('SOURCE_SHAPE', '304 without cached image')
                    return complete(prior)
                sink.flush()
                os.fsync(sink.fileno())
            assert isinstance(body, dict)
            sha = body['sha256']
            ref = {'status': 'saved', 'sha256': sha, 'size': body['size'], 'source_url': raw_url,
                   'representation': 'unknown', 'representation_url': representation_url, 'etag': response.get('etag'),
                   'last_modified': response.get('last-modified'), 'content_type': response.get('content-type')}
            target = self.path(sha)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                os.replace(staging, target)
                sync_directory(target.parent)
            self.verify_file(ref)
        except OSError:
            raise Fault('SOURCE_STORAGE', 'gallery storage write failed') from None
        finally:
            staging.unlink(missing_ok=True)
        atomic(cache, dump(ref).encode())
        complete(ref)
        self.hook('after_saved_image', ref)
        return ref

    def progress(self, job):
        if job.get('collector_semantics') == 'civitai-dual-stream-v2':
            from .onboarding_v2 import progress
            return progress(self, job)
        corpus_progress = self.e.corpus.progress(self.e.repo.get(job['corpus_job_id'])) if job.get('corpus_job_id') else None
        return {'job_id': job['id'], 'state': job['state'], 'partial': job['state'] != 'succeeded' or len(job.get('sample', [])) < job['payload']['gallery_limit'] or any(x.get('file', {}).get('status') != 'saved' for x in job.get('sample', [])),
                'requested': job['payload']['gallery_limit'], 'available_examples': len(job.get('sample', [])),
                'saved_images': sum(x.get('file', {}).get('status') == 'saved' for x in job.get('sample', [])),
                'sample': [{'image_id': x['raw']['id'], 'source_position': x['position'], 'file': x.get('file')} for x in job.get('sample', [])],
                'pages': job.get('pages', []), 'source_order': 'first unique image ID in source response order; no sort/rating/thematic query',
                'pagination_complete': job.get('pagination_complete', False), 'stop_reason': job.get('stop_reason'),
                'corpus_job_id': job.get('corpus_job_id'), 'corpus_progress': corpus_progress,
                'completed_stages': job.get('completed_stages', []), 'error': job.get('onboarding_error'),
                'retry_at': job.get('retry_at'), 'visibility': 'saved files and completed corpus stages immediately readable; no atomic publication',
                'native_install': 'not performed; verified existing inventory only'}

    def call(self, op, p, principal, source):
        if not self.e.corpus:
            raise Fault('CORPUS_DISABLED', 'explicit isolated corpus root required')
        if op == 'corpus.files.read':
            return self.read_file(p)
        if op == 'models.onboard.get':
            return self.progress(self.e.require_kind(p['job_id'], 'onboarding'))
        with self.lock:
            refresh_of = None
            recovery_of = None
            if op in ('models.onboard.refresh', 'models.onboard.recover'):
                prior = self.e.require_kind(p['job_id'], 'onboarding')
                if prior.get('source_base', 'https://civitai.red') != self.base:
                    raise Fault('SOURCE_BINDING_CHANGED', 'refresh requires the original onboarding source')
                if op == 'models.onboard.recover':
                    if prior['state'] != 'failed' or prior.get('onboarding_error', {}).get('code') != 'REVISION_CONFLICT':
                        raise Fault('RECOVERY_NOT_SUPPORTED', 'recovery requires a terminal revision-conflict onboarding')
                    recovery_of = prior['id']
                elif prior['state'] != 'succeeded':
                    raise Fault('REFRESH_NOT_TERMINAL', 'resume the existing non-succeeded intent instead')
                else:
                    refresh_of = prior['id']
                p = {**prior['payload'], 'request_id': p['request_id'], 'image_refresh': 'reuse'}
                # Bind the new request to its exact predecessor as well as its
                # selectors. The old job/snapshot/children remain immutable.
                p['recovery_of' if recovery_of else 'refresh_of'] = recovery_of or refresh_of
            if op == 'models.onboard.resume':
                job = self.e.require_kind(p['job_id'], 'onboarding')
            else:
                with self.e.corpus.lock:
                    initial_heads = {c: self.expected(c, self.identity(p)) for c in ('loras', 'checkpoints')}
                    generation_heads = {}
                    # A revision is the commit decision, including a write whose
                    # head/stage ACK was lost. Recovery captures those heads too.
                    for r in self.e.corpus.scan('generations', kind='revision' if recovery_of else 'head'):
                        generation_heads[r['entity_id']] = max(generation_heads.get(r['entity_id'], 0), r['revision'])
                    job, _ = self.e.repo.reserve(principal, source, p['request_id'], 'onboarding', p,
                                                {'completed_stages': [], 'sample': [], 'pages': [], 'initial_heads': initial_heads,
                                                 'refresh_of': refresh_of,
                                                 'recovery_of': recovery_of,
                                                 'collector_semantics': 'civitai-dual-stream-v2',
                                                 'source_base': self.base,
                                                 'initial_generation_heads': generation_heads})
            # Absolute frozen pagination URLs and relative metadata requests must
            # never be mixed across operator source reconfiguration. Historical
            # intents without a binding retain the original public-source default.
            if job.get('source_base', 'https://civitai.red') != self.base:
                raise Fault('SOURCE_BINDING_CHANGED', 'resume requires the original onboarding source')
            return self.run(job)

    def run(self, job):
        if job.get('collector_semantics') == 'civitai-dual-stream-v2':
            from .onboarding_v2 import run
            return run(self, job)
        repo = self.e.repo
        if job['state'] in ('succeeded', 'failed'):
            return self.progress(job)
        # Respect persisted source backoff across jobs/restarts, not only this request.
        blocked = max((j.get('retry_at', 0) or 0 for j in repo.iter_jobs('onboarding')), default=0)
        if blocked > time.time():
            raise Fault('SOURCE_BACKOFF', 'source Retry-After/backoff is active')
        p = job['payload']
        def update(**changes):
            nonlocal job
            job = repo.update(job['id'], 'running', changes, event='onboarding.progress')[0]
        try:
            if 'verified' not in job:
                version = self.json('/api/v1/model-versions/' + str(p['version_id']))
                model = self.json('/api/v1/models/' + str(p['model_id']))
                collection, selected, registered = self.verify(p, version, model)
                update(verified={'collection': collection, 'version': version, 'model': model,
                                 'selected_file': selected, 'registered': registered,
                                 'expected_revision': job['initial_heads'][collection]},
                       next_page=self.base + '/api/v1/images?' + urlencode({'modelVersionId': p['version_id'], 'limit': p['gallery_limit']}),
                       completed_stages=['verified_registered_file'])
            else:
                # Resume never silently accepts a replaced registry entry.
                self.verify(p, job['verified']['version'], job['verified']['model'])
            while not job.get('pagination_complete'):
                if len(job['pages']) >= 32:
                    update(pagination_complete=True, stop_reason='page_resource_bound')
                    break
                url = job['next_page']
                page = self.json(url)
                items = page.get('items')
                if not isinstance(items, list) or len(items) > 200:
                    raise Fault('SOURCE_SHAPE', 'bounded source items array required')
                sample = list(job['sample'])
                seen = {dump(x['raw']['id']) for x in sample}
                for index, record in enumerate(items):
                    if not isinstance(record, dict) or type(record.get('id')) not in (str, int):
                        raise Fault('SOURCE_SHAPE', 'every gallery item needs source image ID')
                    key = dump(record['id'])
                    if key in seen:
                        continue
                    seen.add(key)
                    identity = {'system': 'civitai', 'image_id': record['id']}
                    sample.append({'raw': record, 'position': {'page': len(job['pages']), 'index': index},
                                   'expected_revision': self.expected('generations', identity)})
                    if len(sample) >= p['gallery_limit']:
                        break
                next_page = (page.get('metadata') or {}).get('nextPage')
                stop = 'gallery_limit' if len(sample) >= p['gallery_limit'] else 'source_exhausted' if not next_page else None
                if next_page and not stop:
                    next_page = self.url(next_page)
                    u = urlsplit(next_page)
                    query = parse_qs(u.query)
                    if u.path != '/api/v1/images' or set(query) - {'modelVersionId', 'limit', 'page', 'cursor'} or query.get('modelVersionId') != [str(p['version_id'])]:
                        raise Fault('SOURCE_PAGINATION', 'unsupported pagination contract; raw source not rewritten')
                    if next_page in [x['url'] for x in job['pages']] + [url]:
                        stop = 'pagination_cycle'
                update(sample=sample, pages=job['pages'] + [{'url': url, 'raw': page}], next_page=next_page,
                       pagination_complete=bool(stop), stop_reason=stop)
            for index in range(len(job['sample'])):
                if 'file' not in job['sample'][index]:
                    ref = self.save_image(job['sample'][index]['raw'], p['image_refresh'], job['id'], index)
                    sample = list(job['sample'])
                    sample[index] = {**sample[index], 'file': ref}
                    update(sample=sample)
                elif job['sample'][index]['file']['status'] == 'saved':
                    self.verify_file(job['sample'][index]['file'])
            if 'corpus_payload' not in job:
                v = job['verified']
                # Progress/order belong to this import, not to independent image observations.
                entries = [{'collection': v['collection'], 'identity': self.identity(p),
                            'expected_revision': v['expected_revision'],
                            'body': {'source_model': v['model'], 'source_version': v['version'],
                                     'selected_file': v['selected_file'], 'registered_reference': v['registered'],
                                     'gallery': [{'image_id': x['raw']['id'], 'source_position': x['position']} for x in job['sample']],
                                     'source_pages': job['pages'], 'literal_triggers': v['version'].get('trainedWords'),
                                     'numeric_scope': 'image explicit arrays only; triggers retained literally, not inferred present in images'},
                            'attribution': {'system': 'civitai', 'model_id': p['model_id'], 'version_id': p['version_id']}}]
                for x in job['sample']:
                    occurrences, scope = numeric(x['raw'])
                    entries.append({'collection': 'generations', 'identity': {'system': 'civitai', 'image_id': x['raw']['id']},
                                    'expected_revision': x['expected_revision'],
                                    'body': {'source_record': x['raw'], 'file': x['file'], 'numeric_inputs': scope},
                                    'attribution': {'system': 'civitai', 'image_id': x['raw']['id']}, 'occurrences': occurrences})
                # Literal model tags/triggers are model vocabulary, not evidence
                # that these phrases occurred in any gallery prompt.
                literal_tags = []
                for array in (v['model'].get('tags'), v['version'].get('trainedWords')):
                    if isinstance(array, list):
                        for tag in array:
                            if isinstance(tag, str) and tag not in literal_tags:
                                literal_tags.append(tag)
                entries[0]['body']['literal_tag_scope'] = {'status': 'complete' if len(literal_tags) <= 64 else 'resource_bound',
                                                         'source': 'model.tags + version.trainedWords', 'image_occurrences_inferred': False}
                if len(literal_tags) <= 64:
                    with self.e.corpus.lock:
                        for tag in literal_tags:
                            identity = {'tag': tag}
                            if self.e.corpus.head('tags', entity_id('tags', identity)) is None:
                                entries.append({'collection': 'tags', 'identity': identity, 'body': {}, 'expected_revision': 0})
                payload = CorpusImport.model_validate({'request_id': 'onboard-corpus:' + job['id'], 'entries': entries}).model_dump()
                update(corpus_payload=payload)
            if 'corpus_job_id' not in job:
                payload = job['corpus_payload']
                # Reserve child before running: lost ACK/restart cannot capture newer heads.
                from .corpus import entity_id as eid
                with self.e.corpus.lock:
                    captured = {}
                    for entry in payload['entries']:
                        head = self.e.corpus.head(entry['collection'], eid(entry['collection'], entry['identity']))
                        captured[eid(entry['collection'], entry['identity'])] = head['revision_id'] if head else None
                    child, _ = repo.reserve(job['owner_principal'], job['source_client'], payload['request_id'], 'corpus',
                                            {'operation': 'corpus.import', **payload},
                                            {'corpus_operation': 'corpus.import', 'completed_stages': [], 'receipts': [], 'import_heads': captured})
                self.hook('after_corpus_reserved', {'job_id': child['id']})
                update(corpus_job_id=child['id'], completed_stages=['verified_registered_file', 'source_snapshot', 'image_files'])
            progress = self.e.corpus.run(repo.get(job['corpus_job_id']))
            self.hook('after_corpus_write', progress)
            update(corpus_progress=progress, completed_stages=['verified_registered_file', 'source_snapshot', 'image_files', 'corpus'])
            job = repo.update(job['id'], 'succeeded', {'onboarding_error': None, 'retry_at': 0}, event='onboarding.completed')[0]
        except Exception as exc:
            terminal = isinstance(exc, Fault) and exc.code in {
                'REVISION_CONFLICT', 'TOMBSTONED', 'REGISTERED_KEY_MISMATCH', 'MODEL_TYPE_MISMATCH',
                'REGISTERED_FILE_MISMATCH', 'SOURCE_ID_MISMATCH', 'SOURCE_FILE_MISMATCH',
                'HASH_ALGORITHM_UNKNOWN', 'FILE_HASH_MISMATCH'}
            job = repo.update(job['id'], 'failed' if terminal else 'running',
                              {'onboarding_error': {'code': getattr(exc, 'code', 'INTERRUPTED'), 'type': type(exc).__name__,
                                                    'message': str(exc) if isinstance(exc, Fault) else 'interrupted; resume frozen import'},
                               'retry_at': getattr(exc, 'retry_at', 0)}, event='onboarding.interrupted')[0]
            if not isinstance(exc, Fault):
                raise
        return self.progress(job)
