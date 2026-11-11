"""Assemble a NEW isolated staged destination with a shared read-only baseline.

No live cutover, source mutation, GPU, source fence or completeness claim. Source
SQLite transactions have individual watermarks. Native historical rows stay inert.
"""
import argparse,hashlib,json,os,shutil,sqlite3,time
from pathlib import Path
from .repository import dump
from .final_delta import code_pin,save,sha256
from .scoped_delta import import_capture
from .legacy_import import run as import_legacy

def table_state(c):
    result={}
    for (name,) in c.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name"):
        q='"'+name.replace('"','""')+'"'
        # Sort canonical row values, independent of physical page/rowid layout.
        def encode(row):
            return dump([{'bytes_hex':v.hex()} if isinstance(v,bytes) else v for v in row])
        rows=sorted(encode(r) for r in c.execute('select * from '+q))
        h=hashlib.sha256()
        for r in rows:h.update((r+'\n').encode())
        result[name]={'rows':len(rows),'sha256':h.hexdigest()}
    return result

def assemble(destination,baseline,capture,capture_sha256,knowledge,registries):
    destination=Path(destination).absolute()
    if destination.exists() or any(p.is_symlink() for p in (destination,*destination.parents)):
        raise ValueError('destination must be NEW and unlinked')
    files=[Path(knowledge),Path(capture),*[Path(p) for p in registries]]
    if any(not p.is_file() or p.is_symlink() for p in files):raise ValueError('missing/linked source')
    # Conservative bounded SQLite/WAL/index budget; never copy the full baseline.
    peak=sum(p.stat().st_size for p in files)*24+256*1024**2
    reserve=2*1024**3;free=shutil.disk_usage(destination.parent).free
    if free<peak+reserve:raise ValueError('insufficient measured free space for staged budget plus reserve')
    if sha256(capture)!=capture_sha256:raise ValueError('capture checksum mismatch')
    destination.mkdir(mode=0o700)
    marker={'format':'atlas-staged-overlay-v1','complete':False,'started_at':time.time(),'code':code_pin(),'disk':{'free_before':free,'peak_ceiling_bytes':peak,'reserve_bytes':reserve},'global_coverage_complete':False,'production_ready':False,'source_writes':False,'real_GPU_submissions':0}
    save(destination/'assembly.json',marker)
    overlay=import_capture(Path(capture),capture_sha256,Path(baseline),destination/'references')
    root=destination/'ledger';root.mkdir(mode=0o700)
    source=sqlite3.connect(Path(knowledge).resolve().as_uri()+'?mode=ro',uri=True)
    target=sqlite3.connect(root/'registry.sqlite3')
    try:
        # backup() obtains a consistent SQLite snapshot; verify destination and
        # source logical contents under a source read transaction before/after.
        source.execute('BEGIN');expected=table_state(source);source.backup(target)
        actual=table_state(target)
        if actual!=expected or target.execute('pragma integrity_check').fetchone()[0]!='ok':raise ValueError('knowledge backup mismatch')
        if any(v['rows'] for k,v in actual.items() if k in ('jobs','observations')):raise ValueError('accepted knowledge ledger contains active job state; reviewed inert conversion required')
    finally:target.close();source.close()
    native=[]
    for i,path in enumerate(registries):
        receipt=import_legacy(path,root)
        native.append(receipt);save(destination/('legacy-'+str(i)+'.json'),receipt)
    with sqlite3.connect(root/'registry.sqlite3') as c:
        if c.execute('pragma integrity_check').fetchone()[0]!='ok':raise ValueError('final ledger integrity')
        counts={t:c.execute('select count(*) from '+t).fetchone()[0] for t in ('knowledge_revisions','knowledge_heads','legacy_rows','jobs')}
        c.execute('pragma wal_checkpoint(TRUNCATE)')
    if counts['jobs']!=0:raise ValueError('migration cannot activate native requests')
    for p in destination.rglob('*'):
        if p.is_file():p.chmod(0o600)
    result={**marker,'complete':True,'finished_at':time.time(),'baseline':str(Path(baseline).resolve()),'baseline_usage':'existing read-only reference; no full new corpus copy','overlay_counts':overlay['counts'],'overlay_points':overlay['typed']['every_point_readback'],'knowledge_source':str(Path(knowledge).resolve()),'knowledge_exact_tables':expected,'counts':counts,'native_sources':native,'ledger_sha256':sha256(root/'registry.sqlite3'),'unverified':['fresh full tags/relations delta','global writer-fenced consistent source cut','current Hindsight/resource revisions after accepted watermarks','remote native inventory and byte availability','live GPU/mask/family acceptance','consumer switch and rollback after new live writes'],'disk':{**marker['disk'],'free_after':shutil.disk_usage(destination).free}}
    if result['disk']['free_before']-result['disk']['free_after']>peak:raise ValueError('assembly exceeded disk ceiling')
    save(destination/'assembly.json',result)
    return result

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--destination',type=Path,required=True);p.add_argument('--baseline',type=Path,required=True);p.add_argument('--capture',type=Path,required=True);p.add_argument('--capture-sha256',required=True);p.add_argument('--knowledge',type=Path,required=True);p.add_argument('--legacy-registry',action='append',default=[],type=Path)
    a=p.parse_args();os.umask(0o077)
    result=assemble(a.destination,a.baseline,a.capture,a.capture_sha256,a.knowledge,a.legacy_registry)
    print(dump({k:result[k] for k in ('complete','counts','overlay_counts','overlay_points','production_ready','global_coverage_complete','disk')}))
if __name__=='__main__':main()
