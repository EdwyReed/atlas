"""Durable URL intake over the existing journal, native installer and corpus.

Remote intent is never replayed after an uncertain ACK. Recovery reads the
native-admin journal and exact typed inventory hash; onboarding resumes its own
persisted local job. Workers serialize across Engine instances using flock.
"""
import fcntl
import threading
import time
from .contracts import Fault
from .lora_windows import parse_link, InstallError
from .repository import digest


class Acquisition:
    def __init__(self, engine):
        self.e = engine
        self.lock = threading.Lock()
        # Same sanitized .red read transport and source policy as onboarding.
        self.metadata = lambda url: self.e.onboarding.json(url)

    def call(self, op, p, principal, source):
        repo = self.e.repo
        if op == 'models.acquire.page':
            rows = repo.page('acquisition', after=p['after'], limit=p['limit']+1)
            return {'items': rows[:p['limit']], 'has_more': len(rows)>p['limit'],
                    'next_after': rows[p['limit']-1]['id'] if len(rows)>p['limit'] else None,
                    'execution_enabled': self.e.native_admin.enabled}
        if op == 'models.acquire.select':
            with self.lock:
                j = repo.get(p['job_id'])
                if j['owner_principal'] != principal:
                    raise Fault('FORBIDDEN', 'only the intent owner may select its file')
                if j['kind'] != 'acquisition' or j['state'] != 'needs_input' or j.get('reason') != 'file_selection':
                    raise Fault('INVALID_STATE', 'job does not need a file selector')
                if p['file_id'] not in [str(f['id']) for f in j.get('files', [])]:
                    raise Fault('VALIDATION', 'file is not in the retained version')
                return repo.update(j['id'], 'queued', {'selected_file_id': p['file_id'], 'reason': None}, expected={'needs_input'})[0]
        if op == 'models.acquire.resume':
            # Same cross-process lock as workers: preserve the existing intent.
            with (repo.root/'acquisition.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                j = repo.get(p['job_id'])
                if j['kind'] != 'acquisition':
                    raise Fault('INVALID_STATE', 'not an acquisition job')
                if j['state'] in ('queued', 'resolving', 'submitting', 'ack_unknown', 'onboarding', 'succeeded'):
                    return j
                if j['state'] != 'needs_input' or j.get('reason') != 'installer_unavailable':
                    raise Fault('INVALID_STATE', 'only installer-unavailable jobs can be resumed here')
                if (j.get('snapshot') or {}).get('model', {}).get('type') not in ('LORA', 'LoCon', 'Checkpoint'):
                    raise Fault('INVALID_STATE', 'model type has no URL installer')
                state = 'ack_unknown' if j.get('install_args') else 'queued'
                return repo.update(j['id'], state, {'reason':None, 'message':None, 'error':None,
                                   'next_attempt':0}, expected={'needs_input'}, event='acquisition.resume')[0]
        identities = []
        for url in p['urls']:
            if len(url)>2000 or any(ord(c)<=32 or ord(c)==127 for c in url):
                raise Fault('VALIDATION', 'URL contains whitespace/control characters or is too long')
            try: mid, vid = parse_link(url)
            except (ValueError, InstallError) as exc: raise Fault('VALIDATION', str(exc)) from None
            identities.append((mid, vid, url))
        with self.lock:
            batch, _ = repo.reserve(principal, source, p['request_id'], 'acquisition_batch', p)
            items = []
            for mid, vid, url in identities:
                rid = 'acquire:' + digest({'model_id': mid, 'version_id': vid})
                j, _ = repo.reserve(principal, source, rid, 'acquisition',
                                    {'model_id': mid, 'version_id': vid}, {'url': url, 'batch_id': batch['id']})
                items.append(j)
            repo.update(batch['id'], 'succeeded', {'children': [j['id'] for j in items]})
            return {'batch_id': batch['id'], 'items': items}

    def recover(self):
        # Batch children may have been only partly reserved when the process died.
        for b in self.e.repo.iter_jobs('acquisition_batch', {'queued'}):
            self.call('models.acquire.enqueue', b['payload'], b['owner_principal'], b['source_client'])
        for j in self.e.repo.iter_jobs('acquisition', {'resolving', 'onboarding', 'submitting'}):
            self.e.repo.update(j['id'], 'ack_unknown' if j['state']=='submitting' else 'queued',
                               event='acquisition.recovery_no_remote_replay')

    def loop(self):
        while not self.e.stop.is_set():
            if self.e.native_admin.enabled:
                rows = sorted(self.e.repo.iter_jobs('acquisition', {'queued', 'onboarding', 'ack_unknown'}), key=lambda j:(j['created'], j['id']))
                for j in rows:
                    if self.e.stop.is_set(): break
                    if j.get('next_attempt', 0) <= time.time(): self.run(j)
            self.e.stop.wait(.5)

    def matches(self, resolved):
        from .onboarding import native_hash
        hashes = {k.upper():str(v).lower() for k,v in resolved['hashes'].items()}
        found = []
        for m in self.e.invoke.models():
            try: algorithm, value = native_hash(m)
            except Fault: continue
            if hashes.get(algorithm) == value: found.append(m)
        return found

    def run(self, j):
        # A second Engine may share this repository. Lock before claim/reconcile.
        with (self.e.repo.root/'acquisition.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            j = self.e.repo.get(j['id'])
            if j['state'] == 'ack_unknown': return self.reconcile(j)
            if j['state'] not in ('queued', 'onboarding'): return
            j, _ = self.e.repo.update(j['id'], 'resolving', event='acquisition.resolve')
            try:
                if j.get('installed'): return self.onboard(j, j['installed'], j['resolved'])
                if not j.get('snapshot'):
                    mid, vid = j['payload']['model_id'], j['payload']['version_id']
                    model = self.metadata('https://civitai.red/api/v1/models/'+mid)
                    if str(model.get('id')) != mid: raise Fault('SOURCE_IDENTITY', 'model mismatch')
                    if vid is None:
                        versions = model.get('modelVersions') or []
                        if not versions: raise Fault('SOURCE_SHAPE', 'no versions')
                        vid = str(versions[0]['id'])
                    # Pin latest BEFORE another remote read or a selector can interrupt.
                    j = self.e.repo.update(j['id'], changes={'snapshot': {'model':model, 'version_id':vid}, 'name':model.get('name')})[0]
                model, vid = j['snapshot']['model'], j['snapshot']['version_id']
                mid = j['payload']['model_id']
                version = j['snapshot'].get('version')
                if version is None:
                    version = self.metadata('https://civitai.red/api/v1/model-versions/'+vid)
                    if str(version.get('id'))!=vid or str(version.get('modelId'))!=mid:
                        raise Fault('SOURCE_IDENTITY', 'version mismatch')
                    j = self.e.repo.update(j['id'], changes={'snapshot':{**j['snapshot'], 'version':version}})[0]
                files = [f for f in version.get('files', []) if f.get('type')=='Model' and (f.get('metadata') or {}).get('format')=='SafeTensor']
                chosen = [f for f in files if str(f.get('id'))==j.get('selected_file_id')] if j.get('selected_file_id') else files
                if len(chosen)!=1:
                    return self.e.repo.update(j['id'], 'needs_input', {'reason':'file_selection',
                        'files':[{'id':f['id'], 'name':f.get('name')} for f in files]})[0]
                f = chosen[0]
                resolved = {'model_id':int(mid), 'version_id':int(vid), 'file_id':f['id'], 'file_name':f.get('name'), 'hashes':f.get('hashes') or {}}
                j = self.e.repo.update(j['id'], changes={'resolved':resolved})[0]
                matches = self.matches(resolved)
                if len(matches)==1: return self.onboard(j, matches[0], resolved)
                if len(matches)>1: raise Fault('AMBIGUOUS_REGISTRATION', 'multiple native entries match this file')
                if model.get('type') not in ('LORA', 'LoCon', 'Checkpoint'):
                    return self.e.repo.update(j['id'], 'needs_input', {'reason':'installer_unavailable', 'message':'This model type has no URL installer.'})[0]
                # Installer re-reads source metadata on Windows. Bind its bytes
                # to this durable selection, not merely the same mutable file ID.
                import re
                sha256 = (resolved['hashes'].get('SHA256') or '').lower()
                if not re.fullmatch(r'[a-f0-9]{64}', sha256):
                    raise Fault('SOURCE_HASH_REQUIRED', 'selected file needs a verified SHA256 before installation')
                args = {'request_id':'acq-install:'+j['id'], 'url':'https://civitai.red/models/'+mid+'?modelVersionId='+vid, 'file_id':str(f['id']), 'expected_sha256':sha256}
                operation = 'models.checkpoint.install' if model.get('type') == 'Checkpoint' else 'models.lora.install'
                j = self.e.repo.update(j['id'], 'submitting', {'install_args':args, 'install_operation':operation}, event='acquisition.remote_intent')[0]
                native = self.e.native_admin.call(operation, args, j['owner_principal'], 'atlas-acquisition')
                self.e.repo.update(j['id'], changes={'native_job_id':native['id']})
                return self.native_result(j, native)
            except Exception as exc:
                state = self.e.repo.get(j['id'])['state']
                return self.e.repo.update(j['id'], 'ack_unknown' if state=='submitting' else 'onboarding' if state=='onboarding' else 'failed',
                    {'next_attempt':time.time()+15, 'error':{'code':getattr(exc,'code','ACQUISITION_ERROR'), 'message':'Acquisition interrupted; inspect retained job state.'}}, event='acquisition.interrupted')[0]

    def native_result(self, j, native):
        if native['state'] != 'succeeded':
            return self.e.repo.update(j['id'], native['state'], {'error':native.get('error'), 'next_attempt':time.time()+15})[0]
        matches = self.matches(j['resolved'])
        installed = [m for m in matches if m['key']==native['result']['installed_key']]
        if len(installed)!=1: raise Fault('REGISTRATION_UNVERIFIED', 'installed key/hash absent from inventory')
        return self.onboard(j, installed[0], j['resolved'])

    def reconcile(self, j):
        # GET journal/inventory ONLY. Never re-enter native_admin.call here.
        try:
            args = j.get('install_args')
            native = self.e.repo.request(j['owner_principal'], 'acq-install:'+j['id'], 'native_admin', {'operation':j.get('install_operation', 'models.lora.install'), **args}) if args else None
            if native and native['state'] in ('succeeded', 'failed'):
                self.e.repo.update(j['id'], changes={'native_job_id':native['id']})
                return self.native_result(j, native)
            matches = self.matches(j['resolved']) if j.get('resolved') else []
            if len(matches)==1: return self.onboard(j, matches[0], j['resolved'])
            return self.e.repo.update(j['id'], changes={'next_attempt':time.time()+15, 'message':'ACK unknown. Journal/inventory checked read-only; no exact registration yet. No automatic reinstall.'})[0]
        except Exception:
            return self.e.repo.update(j['id'], changes={'next_attempt':time.time()+15, 'message':'Read-only reconciliation unavailable; remote submission is not replayed.'})[0]

    def onboard(self, j, installed, resolved):
        from .onboarding import native_hash
        repo = self.e.repo
        repo.update(j['id'], 'onboarding', {'installed':installed, 'resolved':resolved}, event='acquisition.registered')
        algorithm, measured = native_hash(installed)
        hashes = {k.upper():str(v).lower() for k,v in resolved['hashes'].items()}
        if hashes.get(algorithm) != measured:
            return repo.update(j['id'], 'partial', {'error':{'code':'FILE_HASH_MISMATCH', 'message':'Native typed hash is not confirmed by source; registered file retained.'}})[0]
        args = {k:resolved[k] for k in ('model_id','version_id','file_id','file_name')}
        args.update(request_id='acq-onboard:'+j['id'], registered_key=installed['key'], registered_path=installed['path'],
                    hash_algorithm=algorithm, file_hash=measured, gallery_limit=30, image_refresh='reuse')
        old = repo.request(j['owner_principal'], args['request_id'], 'onboarding', args)
        if old:
            progress = self.e.onboarding.call('models.onboard.get', {'job_id':old['id']}, j['owner_principal'], 'atlas-acquisition')
            if progress['state'] not in ('succeeded', 'failed') and (progress.get('retry_at') or 0)<=time.time():
                progress = self.e.onboarding.call('models.onboard.resume', {'job_id':old['id']}, j['owner_principal'], 'atlas-acquisition')
        else:
            progress = self.e.onboarding.call('models.onboard', args, j['owner_principal'], 'atlas-acquisition')
        running = progress['state'] not in ('succeeded', 'failed')
        state = 'onboarding' if running else 'succeeded' if progress['state']=='succeeded' and not progress.get('partial') else 'partial'
        return repo.update(j['id'], state, {'onboarding':progress, 'error':progress.get('error'),
                           'next_attempt':max(time.time()+15, progress.get('retry_at') or 0)}, event='acquisition.progress')[0]
