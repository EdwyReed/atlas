"""Exhaustive online comparison with a read-only baseline, retaining only deltas.

Two matching passes are evidence of repeated observations, NOT a writer fence or
an atomic source cut. Missing source points suppress stale base identities.
Disk use is bounded; no full fresh mirror, snapshots, GPU or live mutations.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
import zlib

from .final_delta import code_pin, save, sha256
from .full_migration import Mirror, FrozenSource
from .materialize import materialize
from .migrate import Source
from .reference_mirror import build_index
from .repository import dump, digest


def baseline_pin(db):
    h = hashlib.sha256()
    for row in db.execute('select collection,id,sha256 from points order by collection,id'):
        h.update((dump(row)+'\n').encode())
    return {'collections': list(db.execute('select name,schema,expected,done,after_schema,after_count from collections order by name')),
            'ordered_point_hashes': h.hexdigest()}


def run(source, baseline, destination, ceiling=3*1024**3):
    destination = Path(destination).absolute()
    if destination.exists() or any(p.is_symlink() for p in (destination,*destination.parents)):
        raise ValueError('destination must be NEW and unlinked')
    baseline = Path(baseline).resolve()
    frozen = FrozenSource(baseline)
    reserve = 2*1024**3
    if shutil.disk_usage(destination.parent).free < ceiling+reserve:
        frozen.close(); raise ValueError('insufficient free space for ceiling plus reserve')
    names = sorted(frozen.collections())
    if sorted(source.collections()) != names:
        frozen.close(); raise ValueError('source/baseline collection set changed')
    destination.mkdir(mode=0o700)
    receipt = {'format':'atlas-stream-delta-v1', 'complete':False, 'code':code_pin(),
               'baseline':str(baseline), 'baseline_pin':baseline_pin(frozen.db),
               'source_writes':False, 'production_ready':False, 'global_coverage_complete':False,
               'source_consistency':'two exhaustive equal online passes; NOT atomic; writers NOT fenced',
               'disk_ceiling_bytes':ceiling, 'reserve_bytes':reserve, 'started_at':time.time(), 'collections':{}}
    save(destination/'receipt.json', receipt)
    seen = sqlite3.connect(destination/'observations.sqlite3')
    seen.executescript('PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; CREATE TABLE seen(collection TEXT,id TEXT,sha256 TEXT,verified INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(collection,id)) WITHOUT ROWID; CREATE TABLE holds(collection TEXT,id TEXT,reason TEXT,PRIMARY KEY(collection,id)) WITHOUT ROWID;')
    seen.execute('attach database ? as baseline', (baseline.as_uri()+'?mode=ro',))
    overlay = Mirror(destination/'overlay.sqlite3')
    def budget():
        allocated = sum(p.stat().st_blocks*512 for p in destination.iterdir() if p.is_file())
        if allocated > ceiling or shutil.disk_usage(destination).free < reserve:
            raise ValueError('disk budget reached; partial evidence retained, not complete')
    try:
        for name in names:
            before = source.schema(name)
            schema = before['config']['params']
            if schema != frozen.schema(name)['config']['params']:
                raise ValueError('schema differs from baseline; explicit mapping required')
            vectors = schema['vectors']; vectors = {'unnamed':vectors} if 'size' in vectors else vectors
            with overlay.db:
                overlay.db.execute('insert into collections(name,schema,expected) values(?,?,0)',(name,dump(schema)))
            counts = {'new':0,'changed':0,'unchanged':0,'missing_held':0,'acl_relaxation_held':0}
            pass_counts = []
            for pass_number in (1,2):
                offset = None; cursors = set(); count = 0
                while True:
                    body = {'limit':512,'with_payload':True,'with_vector':True}
                    if offset is not None:body['offset'] = offset
                    page = source.call('POST','/collections/'+name+'/points/scroll',body)
                    changed = []
                    with seen:
                        for point in page['points']:
                            key = dump(point['id']); h = digest(point); count += 1
                            if pass_number == 1:
                                seen.execute('insert into seen(collection,id,sha256) values(?,?,?)',(name,key,h))
                                old = frozen.db.execute('select sha256,raw from points where collection=? and id=?',(name,key)).fetchone()
                                status = 'new' if old is None else 'unchanged' if old[0] == h else 'changed'
                                counts[status] += 1
                                if status != 'unchanged':
                                    changed.append(point)
                            else:
                                updated = seen.execute('update seen set verified=1 where collection=? and id=? and sha256=? and verified=0',(name,key,h))
                                if updated.rowcount != 1:raise ValueError('source drift or duplicate during second pass')
                    if changed:overlay.ingest(name,vectors,changed,None,False)
                    budget()
                    offset = page.get('next_page_offset')
                    if count % 32768 < 512 or offset is None:
                        print(dump({'collection':name,'pass':pass_number,'observed':count,'changes':counts}),flush=True)
                        save(destination/'progress.json',{'collection':name,'pass':pass_number,'observed':count,'counts':counts,'at':time.time()})
                    if offset is None:break
                    cursor = dump(offset)
                    if cursor in cursors:raise ValueError('scroll cursor loop')
                    cursors.add(cursor)
                after = source.schema(name)
                exact = source.call('POST','/collections/'+name+'/points/count',{'exact':True})['count']
                if count != exact or exact != before['points_count'] or after['config'] != before['config'] or after['points_count'] != exact:
                    raise ValueError('schema/count drift during exhaustive observation')
                pass_counts.append(count)
            if seen.execute('select count(*) from seen where collection=? and verified=0',(name,)).fetchone()[0]:raise ValueError('second pass missing points')
            with seen:
                seen.execute("insert into holds select p.collection,p.id,'missing_held' from baseline.points p left join seen s using(collection,id) where p.collection=? and s.id is null",(name,))
                counts['missing_held'] = seen.execute("select count(*) from holds where collection=? and reason='missing_held'",(name,)).fetchone()[0]
            n = counts['new']+counts['changed']
            with overlay.db:
                overlay.db.execute('update collections set expected=?,done=1,after_schema=?,after_count=? where name=?',(n,dump(schema),n,name))
            receipt['collections'][name] = {'before':before,'after':after,'passes':pass_counts,'counts':counts}
            save(destination/'receipt.json',receipt)
        if sorted(source.collections()) != names:raise ValueError('collection inventory drift')
        if baseline_pin(frozen.db) != receipt['baseline_pin']:raise ValueError('baseline drift')
        receipt['raw_verification'] = overlay.verify()
        for name,key,h in overlay.db.execute('select collection,id,sha256 from points'):
            if seen.execute('select sha256,verified from seen where collection=? and id=?',(name,key)).fetchone() != (h,1):
                raise ValueError('delta does not match both source observations')
        overlay.close();overlay = None
        receipt['typed'] = materialize(destination/'overlay.sqlite3',destination/'typed.json')
        receipt['index'] = build_index(destination/'overlay.sqlite3',destination/'index.json')
        with sqlite3.connect(destination/'overlay.sqlite3') as c:c.execute('pragma wal_checkpoint(TRUNCATE)')
        seen.execute('pragma wal_checkpoint(TRUNCATE)')
        if seen.execute('pragma integrity_check').fetchone()[0] != 'ok':raise ValueError('observation integrity')
        budget()
        receipt.update(complete=True,finished_at=time.time(),observation_sha256=sha256(destination/'observations.sqlite3'),overlay_sha256=sha256(destination/'overlay.sqlite3'))
        save(destination/'receipt.json',receipt)
        return receipt
    except BaseException as error:
        receipt.update(error=type(error).__name__+': '+str(error),finished_at=time.time())
        save(destination/'receipt.json',receipt)
        raise
    finally:
        if overlay is not None:overlay.close()
        seen.close();frozen.close()


class StreamReferenceMirror:
    """Delta-only offline view. Missing source IDs suppress stale base rows."""
    semantic_enabled=False
    def __init__(self,baseline,destination):
        from .scoped_delta import ScopedReferenceMirror
        from .reference_mirror import ReferenceMirror
        root=Path(destination).resolve()
        self.receipt=json.loads((root/'receipt.json').read_text())
        r=self.receipt
        if r.get('format')!='atlas-stream-delta-v1' or not r.get('complete') or r.get('baseline')!=str(Path(baseline).resolve()):raise ValueError('unverified delta or wrong baseline')
        with sqlite3.connect(Path(baseline).resolve().as_uri()+'?mode=ro',uri=True) as c:
            c.execute('BEGIN')
            if json.loads(dump(baseline_pin(c)))!=r['baseline_pin']:raise ValueError('baseline lineage drift')
        if sha256(root/'observations.sqlite3')!=r['observation_sha256'] or sha256(root/'overlay.sqlite3')!=r['overlay_sha256']:raise ValueError('delta archive drift')
        self.base=ReferenceMirror(baseline);self.overlay=ReferenceMirror(root/'overlay.sqlite3')
        from .reference_reader import DiskKeys
        self.holds=DiskKeys(root/'observations.sqlite3','holds',where="reason!='acl_relaxation_held'")
        self.covered=DiskKeys(self.overlay.path,extra=self.holds)
        self.items=ScopedReferenceMirror.items
    def get(self,collection,point_id):
        from .contracts import Fault
        key=(collection,dump(point_id))
        if key in self.holds:raise Fault('SOURCE_MISSING','point absent in newer source snapshot')
        return (self.overlay if key in self.covered else self.base).get(collection,point_id)
    def resources(self):
        from .reference_reader import iter_items, RESOURCE_SQL
        return list(iter_items(self,RESOURCE_SQL))
    def search_resources(self,query,limit):
        from .reference_reader import search_resources
        return search_resources(self,query,limit)
    def evidence(self,kind,key):
        from .reference_reader import iter_items, EVIDENCE_SQL
        return list(iter_items(self,EVIDENCE_SQL,(kind,key),10))
    def query(self,vector,limit):return self.base.query(vector,limit)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline',required=True,type=Path);p.add_argument('--destination',required=True,type=Path)
    a=p.parse_args();os.umask(0o077)
    source=Source('http://127.0.0.1:6333')
    try:print(dump(run(source,a.baseline,a.destination)),flush=True)
    finally:source.client.close()

if __name__=='__main__':main()
