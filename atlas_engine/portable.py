"""Portable offline state + exact historical dependencies, without rewriting history.

Private operator artifact, never a distributable image layer. No service control.
"""
import argparse
from contextlib import closing, nullcontext
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
import tarfile
import sys
import io
import gzip

from . import state_backup as sb
from .repository import dump

FORMAT = 'atlas-portable-state-v1'
ADMISSION = 'portable-restored-v1'
HISTORICAL_EFFECTS = sb.EFFECTS | {'batch'}
STATE = sb.INCLUDED | {'adoption-receipt.json', 'attachment-owner.json', '.adoption', 'portable-state.json', 'references', 'vocabulary.sqlite3', 'portable-media'}


def member(name):
    p = PurePosixPath(name)
    if not name or p.is_absolute() or '..' in p.parts or '\\' in name or str(p) != name:
        raise ValueError('invalid portable member')
    return p


def sealed_file(path):
    p = sb.safe_path(path)
    if not p.is_file():
        raise ValueError('regular sealed dependency required')
    for suffix in ('-wal', '-journal'):
        side = Path(str(p) + suffix)
        if side.exists() and side.stat().st_size:
            raise ValueError('dependency has nonempty SQLite sidecar')
    return p


class References:
    """Explicit closed mapping: original source names stay literal in the archive."""
    def __init__(self, root, metadata=None):
        self.root = sb.safe_path(root)
        self.metadata = metadata or json.loads((self.root/'portable-state.json').read_text())
        if self.metadata.get('format') != FORMAT:
            raise ValueError('unsupported portable state')
        self.paths = self.metadata['reference_paths']

    def __call__(self, original):
        key = str(original)
        # Directory references are mapped separately, never longest-prefix guessed.
        if key not in self.paths:
            raise ValueError('unbound portable historical reference')
        relative = member(self.paths[key])
        path = sb.safe_path(self.root / str(relative))
        if not path.is_relative_to(self.root/'references'):
            raise ValueError('reference outside portable storage')
        return path

    def validate(self):
        for old, pin in self.metadata['reference_files'].items():
            p = self(old)
            if not p.is_file() or p.stat().st_size != pin['bytes'] or sb.sha(p) != pin['sha256']:
                raise ValueError('portable reference bytes changed')
        media = self.metadata.get('media')
        folder = self.root/'portable-media'
        if media:
            if (media.get('format')!='atlas-container-media-weights-v1'
                    or set(media.get('weights',{}))!={'mobile_sam.pt','ntd11.pt'}):
                raise ValueError('unsupported portable media binding')
            observed=sb.files(folder)
            if observed!={'weights/'+name:pin for name,pin in media['weights'].items()}:
                raise ValueError('portable media weights changed or unexpected file')
        elif folder.exists():
            raise ValueError('portable media assets require explicit binding')
        return self


def reference(root):
    """Build existing typed readers over relocated, immutable original bytes."""
    resolver = References(root).validate()
    from .attachment import SealedReference
    from .reference_reader import DiskKeys
    from .sealed_delta import SealedReferenceMirror, ATTACHMENT
    from .stream_delta import StreamReferenceMirror
    class Keys(DiskKeys):
        def connect(self):
            return sqlite3.connect(self.path.as_uri()+'?immutable=1',uri=True)
    def read(old, expected=None):
        path=resolver(old)
        if expected and sb.sha(path)!=expected: raise ValueError('historical declared pin differs')
        return json.loads(path.read_text())
    def build(old, expected=None, seen=()):
        if old in seen or len(seen)>=64: raise ValueError('attachment cycle/depth limit')
        a=read(old,expected)
        if not a.get('complete'): raise ValueError('incomplete historical attachment')
        if sb.sha(resolver(a['knowledge_ledger'])) != a['knowledge_ledger_sha256']:
            raise ValueError('historical ledger pin differs')
        if a['format']==ATTACHMENT:
            r=object.__new__(SealedReferenceMirror)
            r.base=build(a['parent']['path'],a['parent']['sha256'],(*seen,old))
            r.overlay=SealedReference.make(resolver(a['overlay']['path']))
            if sb.sha(r.overlay.path)!=a['overlay']['sha256']: raise ValueError('overlay pin differs')
            seal=read(a['seal']['path'],a['seal']['sha256'])
            receipt=read(a['receipt']['path'],a['receipt']['sha256'])
            if not receipt['complete'] or receipt['parent']!=a['parent'] or receipt['seal']!=a['seal']:
                raise ValueError('historical attachment receipt mismatch')
            r.holds={(op['collection'],dump(op['id'])) for op in seal['operations'] if op['action'] in ('hold','tombstone')}
            r.covered=Keys(r.overlay.path,extra=r.holds)
            return r
        if a['format']!='atlas-checkpoint-attachment-v1': raise ValueError('unsupported historical attachment')
        directory=resolver(a['reference_delta']); receipt=read(str(Path(a['reference_delta'])/'receipt.json'),a['reference_delta_receipt_sha256'])
        if (receipt.get('format')!='atlas-stream-delta-v1' or not receipt.get('complete')
                or receipt['baseline']!=a['baseline']): raise ValueError('historical stream binding differs')
        from .stream_delta import baseline_pin
        with closing(sqlite3.connect(resolver(a['baseline']).as_uri()+'?immutable=1',uri=True)) as db:
            if json.loads(dump(baseline_pin(db)))!=receipt['baseline_pin']:
                raise ValueError('historical baseline lineage differs')
        for name,key in (('overlay.sqlite3','overlay_sha256'),('observations.sqlite3','observation_sha256')):
            if sb.sha(directory/name)!=receipt[key]: raise ValueError('historical stream archive drift')
        r=object.__new__(StreamReferenceMirror);r.receipt=receipt
        r.base=SealedReference.make(resolver(a['baseline']))
        r.overlay=SealedReference.make(directory/'overlay.sqlite3')
        r.holds=Keys(directory/'observations.sqlite3','holds',where="reason!='acl_relaxation_held'")
        r.covered=Keys(r.overlay.path,extra=r.holds)
        from .scoped_delta import ScopedReferenceMirror
        r.items=ScopedReferenceMirror.items
        return r
    attachment=resolver.metadata['attachment']
    if not attachment: return None
    return build(attachment)


def pack(source, destination, attachment=None, vocabulary=None, media_weights=None):
    source = sb.safe_path(source)
    destination = sb.safe_path(destination) if destination is not None else None
    if destination is not None and (destination.exists() or destination.is_relative_to(source) or not destination.parent.is_dir()):
        raise ValueError('NEW archive outside source required')
    from . import adoption as ad
    with sb.offline(source):
        before = ad.source_inventory(source)
        previous = json.loads((source/sb.MARKER).read_text()) if (source/sb.MARKER).exists() else None
        # A source can be older than this executable. Never activate/open it here.
        if previous and previous.get('preimage_verified') is False:
            raise ValueError('incomplete source refused')
        pins = ad.dependency_pins(attachment, []) if attachment else []
        inherited = json.loads((source/'portable-state.json').read_text()) if (source/'portable-state.json').exists() else None
        if inherited: References(source,inherited).validate()
        owner = source/'attachment-owner.json'
        if owner.exists():
            expected = json.loads(owner.read_text())['attachment']
            bound = inherited['attachment'] if inherited else str(sb.safe_path(attachment)) if attachment else None
            physical = References(source,inherited)(bound) if inherited and bound else Path(bound) if bound else None
            if physical is None or expected != {'path':bound, 'sha256':sb.sha(physical)}:
                raise ValueError('explicit original attachment binding required')
        if inherited and (attachment or vocabulary or media_weights):
            raise ValueError('portable source already binds references/vocabulary/media')
        metadata = {'format':FORMAT, 'source_root':str(source), 'reference_paths':{}, 'reference_files':{},
                    'attachment':str(sb.safe_path(attachment)) if attachment else None,
                    'vocabulary':bool(vocabulary), 'history':'literal original bytes; path map is not source-data rewriting'}
        reference_sources = {}
        for pin in pins:
            p = sealed_file(pin['path'])
            # Preserve complete directory relationships for delta receipts/archives.
            rel = 'references/tree/' + p.as_posix().lstrip('/')
            metadata['reference_paths'][str(p)] = rel
            metadata['reference_files'][str(p)] = {k:pin[k] for k in ('bytes','sha256')}
            reference_sources[rel] = p
            for parent in p.parents:
                if parent == Path('/'): break
                metadata['reference_paths'][str(parent)] = 'references/tree/' + parent.as_posix().lstrip('/')
        if inherited:
            metadata = {**inherited, 'source_root':str(source)}
        selected = sorted(STATE & {p.name for p in source.iterdir()})
        inputs = {}
        for name in selected:
            if name in ('registry.sqlite3', 'portable-state.json'): continue
            p = source/name; sb.safe_path(p)
            if p.is_dir():
                for rel in sb.files(p): inputs[name+'/'+rel] = p/rel
            elif p.is_file(): inputs[name] = p
            else: raise ValueError('nonregular state entry')
        inputs.update(reference_sources)
        media_pins = []
        if media_weights:
            folder = sb.safe_path(media_weights)
            if 'portable-media' in selected:
                raise ValueError('media assets already bound')
            for name in ('mobile_sam.pt', 'ntd11.pt'):
                path = sealed_file(folder/name)
                pin = ad.pin(path)
                media_pins.append(pin)
                inputs['portable-media/weights/'+name] = path
            if media_pins[0]['sha256'] != '6dbb90523a35330fedd7f1d3dfc66f995213d81b29a5ca8108dbcdd4e37d6c2f':
                raise ValueError('MobileSAM weights differ from accepted backend pin')
            metadata['media'] = {'format':'atlas-container-media-weights-v1',
                'weights':{Path(pin['path']).name:{k:pin[k] for k in ('bytes','sha256')} for pin in media_pins},
                'provisioning':'explicit init-media.py with matching private media image; no runtime or credentials bundled'}
        vocabulary_pin = None
        if vocabulary:
            inputs['vocabulary.sqlite3'] = sealed_file(vocabulary)
            vocabulary_pin = ad.pin(inputs['vocabulary.sqlite3'])
        collections = sb.points(source)
        with closing(ad.connection(source/'registry.sqlite3', closed=True)) as db:
            db.execute('BEGIN')
            if db.execute('pragma integrity_check').fetchall() != [('ok',)]:
                raise ValueError('registry integrity failure')
            logical = ad.ledger(db)
            # Temporary SQLite file only, never a second full corpus/reference copy.
            with tempfile.TemporaryDirectory(prefix='atlas-pack-', dir=destination.parent if destination else None) as temp:
                registry = Path(temp)/'registry.sqlite3'
                with closing(sqlite3.connect(registry)) as target:
                    db.backup(target); target.execute('pragma journal_mode=DELETE')
                inputs['registry.sqlite3'] = registry
                inventory = {}
                try:
                    output_context = destination.open('xb') if destination else nullcontext(sys.stdout.buffer)
                    with output_context as output, gzip.GzipFile(fileobj=output,mode='wb',compresslevel=1,mtime=0) as compressed, tarfile.open(fileobj=compressed,mode='w|',format=tarfile.GNU_FORMAT) as archive:
                        class DigestReader:
                            def __init__(self, stream): self.stream=stream;self.h=hashlib.sha256();self.size=0
                            def read(self, size=-1):
                                data=self.stream.read(size);self.h.update(data);self.size+=len(data);return data
                        def add_bytes(name, raw):
                            entry=tarfile.TarInfo(name);entry.size=len(raw);entry.mode=0o600
                            archive.addfile(entry,io.BytesIO(raw))
                        for relative, p in sorted(inputs.items()):
                            member(relative); sb.safe_path(p)
                            entry=tarfile.TarInfo('state/'+relative);entry.size=p.stat().st_size;entry.mode=0o600
                            with p.open('rb') as src:
                                reader=DigestReader(src);archive.addfile(entry,reader)
                            inventory[relative] = {'bytes':reader.size,'sha256':reader.h.hexdigest()}
                        raw = (dump(metadata)+'\n').encode()
                        add_bytes('state/portable-state.json', raw)
                        inventory['portable-state.json'] = {'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
                        if ad.source_inventory(source) != before:
                            raise ValueError('source durable bytes changed during pack')
                        if pins: ad.check_pins(pins)
                        if media_pins:
                            ad.check_pins(media_pins)
                            for pin in media_pins:
                                if inventory['portable-media/weights/'+Path(pin['path']).name] != {k:pin[k] for k in ('bytes','sha256')}:
                                    raise ValueError('media weights changed across pack')
                        if vocabulary_pin:
                            sealed_file(vocabulary_pin['path']); ad.check_pins([vocabulary_pin])
                            if inventory['vocabulary.sqlite3'] != {k:vocabulary_pin[k] for k in ('bytes','sha256')}:
                                raise ValueError('vocabulary changed across pack')
                        for old,pin in metadata['reference_files'].items():
                            if inventory[metadata['reference_paths'][old]] != pin:
                                raise ValueError('archived reference differs from pinned source')
                        manifest = {'format':FORMAT, 'versions':sb.versions(), 'source_root':str(source),
                                    'previous_restore':previous, 'files':inventory, 'registry':logical,
                                    'collections':collections, 'configuration_secrets_included':False,
                                    'consistency':'offline existing service/corpus locks; historical source qualifications retained'}
                        add_bytes('manifest.json', (dump(manifest)+'\n').encode())
                    if destination:
                        with destination.open('rb') as f: os.fsync(f.fileno())
                        destination.chmod(0o600)
                except BaseException:
                    if destination: destination.unlink(missing_ok=True)
                    raise
    return {'packed':True,'files':len(inventory),
            'archive_bytes':destination.stat().st_size if destination else None,
            'archive_sha256':sb.sha(destination) if destination else None,
            'manifest_sha256':hashlib.sha256(dump(manifest).encode()).hexdigest(),
            'configuration_secrets_included':False}


def unpack(source, destination, after_copy=None):
    destination=sb.safe_path(destination)
    if source is not None: source,destination=sb.new_target(source,destination)
    elif destination.exists() or not destination.parent.is_dir():
        raise ValueError('NEW destination with existing parent required')
    from . import adoption as ad
    state={'format':'atlas-restored-state-v1','reconciled':False,'preimage_verified':False,
           'storage_target_root':str(destination),'storage_source_roots':[],
           'portable_restore':True}
    destination.mkdir(mode=0o700)
    sb.write_json(destination/sb.MARKER,state);sb.sync_tree(destination)
    observed={}; folded=set(); manifest=None
    input_context=source.open('rb') if source else nullcontext(sys.stdin.buffer)
    with input_context as input_stream, tarfile.open(fileobj=input_stream,mode='r|gz') as archive:
        for entry in archive:
            name=entry.name;member(name)
            if name.casefold() in folded or not entry.isfile() or entry.pax_headers:
                raise ValueError('duplicate/nonregular/extended archive member')
            folded.add(name.casefold())
            if manifest is not None: raise ValueError('manifest must be final member')
            stream=archive.extractfile(entry)
            if stream is None: raise ValueError('unreadable portable member')
            if name=='manifest.json':
                if entry.size>32*1024**2:raise ValueError('oversized portable manifest')
                manifest=json.loads(stream.read());continue
            if not name.startswith('state/'):
                raise ValueError('unexpected portable member')
            relative=name.removeprefix('state/');member(relative)
            if relative.split('/')[0] not in STATE or relative=='service.lock':
                raise ValueError('unexpected state member')
            if entry.size<0 or shutil.disk_usage(destination).free < entry.size+256*1024**2:
                raise ValueError('insufficient space for portable member plus reserve')
            h=hashlib.sha256();size=0
            target=destination/relative
            if relative==sb.MARKER:
                target=destination/'previous-restore.json'
            target.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
            with target.open('xb') as dst:
                for chunk in iter(lambda:stream.read(1024*1024),b''):
                    h.update(chunk);size+=len(chunk);dst.write(chunk)
            target.chmod(0o600)
            if size!=entry.size:raise ValueError('truncated portable member')
            observed[relative]={'bytes':size,'sha256':h.hexdigest()}
            if after_copy:after_copy(relative)
    if (not manifest or manifest.get('format')!=FORMAT
            or manifest['versions']['qdrant-client']!=sb.versions()['qdrant-client']
            or manifest['files']!=observed):
        raise ValueError('portable manifest/runtime/preimage mismatch')
    previous=manifest['previous_restore']
    prior=destination/'previous-restore.json'
    if prior.exists():
        if json.loads(prior.read_text())!=previous:raise ValueError('previous marker differs from manifest')
        prior.unlink()
    elif previous is not None:raise ValueError('missing original restore marker')
    roots=[manifest['source_root']]+(previous or {}).get('storage_source_roots',[])
    state.update(previous_restore=previous,storage_source_roots=sorted(set(roots)),
                 backup_manifest_sha256=hashlib.sha256(dump(manifest).encode()).hexdigest())
    with closing(ad.connection(destination/'registry.sqlite3',closed=True)) as db:
        if db.execute('pragma integrity_check').fetchall()!=[('ok',)] or ad.ledger(db)!=manifest['registry']:
            raise ValueError('restored full ledger differs')
    if sb.points(destination)!=manifest['collections']:raise ValueError('restored corpus differs')
    References(destination).validate()
    (destination/'service.lock').touch(mode=0o600)
    sb.sync_tree(destination)
    state['preimage_verified']=True
    sb.write_json(destination/'restore-state.new',state)
    (destination/'restore-state.new').replace(destination/sb.MARKER);sb.sync_tree(destination)
    return {'restored':True,'quarantined':True,'files':len(observed),'external_dependencies':False,
            'manifest_sha256':state['backup_manifest_sha256']}


def validate_admission(root,state,report):
    from .adoption_native import row_pins
    if (report.get('admission')!=ADMISSION or report.get('runtime')!=sb.versions()
            or report.get('target_root')!=str(root)
            or report.get('backup_manifest_sha256')!=state['backup_manifest_sha256']
            or report.get('refresh')!=state.get('refresh')
            or report.get('portable_metadata_sha256')!=sb.sha(Path(root)/'portable-state.json')):
        raise ValueError('portable admission binding/runtime differs')
    if row_pins(root,[r['id'] for r in report['held_rows']])!=report['held_rows']:
        raise ValueError('portable historical held rows changed')
    return report


def activate(root, report_path):
    """Enable new work; exported native identities remain inert, never replayed."""
    from . import adoption_control as ctl, adoption as ad
    from .adoption_native import row_pins
    root=sb.safe_path(root);report=json.loads(sb.safe_path(report_path).read_text())
    with sb.offline(root):
        state=sb.restore_state(root)
        if not state or not state.get('portable_restore') or state['reconciled']:
            raise ValueError('completed quarantined portable restore required')
        if (report.get('target_root')!=str(root) or not report.get('operator')
                or report.get('registry_logical_sha256')!=sb.registry_digest(root)
                or report.get('backup_manifest_sha256')!=state['backup_manifest_sha256']
                or report.get('refresh')!=state.get('refresh')
                or report.get('runtime')!=sb.versions()
                or report.get('retain_historical_effects') is not True
                or not isinstance(report.get('assessment'),str) or not report['assessment'].strip()):
            raise ValueError('activation must bind target/ledger/runtime/backup/operator and retain historical effects')
        from .portable_refresh import validate_final
        validate_final(root,state)
        References(root).validate()
        with closing(ad.connection(root/'registry.sqlite3',closed=True)) as db:
            rows=db.execute('select id,kind,state from jobs order by id').fetchall()
            historical=[r[0] for r in rows if r[1] in HISTORICAL_EFFECTS]
            held=[r[0] for r in rows if r[1] in HISTORICAL_EFFECTS and r[2] not in sb.TERMINAL]
            uploads=[r[0] for r in db.execute("select id from uploads where state in ('importing','ack_unknown') order by id")]
        accepted={**report,'admission':ADMISSION,'held_rows':row_pins(root,held),
                  'historical_native_ids':historical,'held_upload_ids':uploads,
                  'portable_metadata_sha256':sb.sha(root/'portable-state.json')}
        state.update(reconciled=True,held_admission=ctl.record(root,'portable-activation',accepted))
        validate_admission(root,state,accepted);ctl.atomic(root/sb.MARKER,state)
        return {'activated':True,'held':len(held),'historical_effects_inert':len(historical),
                'native_backend_identity_verified':False}


def main():
    p=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''Activation REPORT is a private JSON object with these REQUIRED fields:
  target_root: exact absolute destination root
  operator: nonempty operator attribution
  assessment: explicit nonempty offline assessment (never automatic consent)
  retain_historical_effects: true
  backup_manifest_sha256: restore-state.json.backup_manifest_sha256
  registry_logical_sha256: atlas_engine.state_backup.registry_digest(ROOT)
  runtime: atlas_engine.state_backup.versions() JSON object
Both existing owner locks must be available. Activation binds the current full
ledger and keeps ALL original native/media/acquisition/batch effects inert.
It does not reconcile unknown effects, configure credentials, or authorize GPU,
model installation, native writes, production service changes or a cutover.
Media archive provisioning is a separate explicit offline init-media action.''')
    p.add_argument('action',choices=('pack','unpack','activate'))
    p.add_argument('source',type=Path);p.add_argument('destination',type=Path)
    p.add_argument('--attachment',type=Path);p.add_argument('--vocabulary',type=Path)
    p.add_argument('--media-weights',type=Path,help='explicit private directory with mobile_sam.pt and ntd11.pt; no runtime/config copied')
    a=p.parse_args();os.umask(0o077)
    try:
        if a.action=='activate':
            if a.attachment or a.vocabulary or a.media_weights: raise ValueError('activate uses restored bindings only')
            result=activate(a.source,a.destination)
        elif a.action=='unpack':
            if a.attachment or a.vocabulary or a.media_weights: raise ValueError('unpack uses archive bindings only')
            result=unpack(None if str(a.source)=='-' else a.source,a.destination)
        else: result=pack(a.source,None if str(a.destination)=='-' else a.destination,a.attachment,a.vocabulary,a.media_weights)
        print(dump(result),file=sys.stderr if a.action=='pack' and str(a.destination)=='-' else sys.stdout)
    except (ValueError,OSError,KeyError,sqlite3.Error,tarfile.TarError) as e:
        p.exit(1,'Atlas portable operation refused: '+str(e)+'\n')

if __name__=='__main__':main()
