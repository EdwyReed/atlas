"""Atomic inert quarantine import and optional reviewed legacy owner binding.

Only sealed source snapshots belong here; no live cut/fence is inferred. Source
is opened read-only, no legacy Store/backend imports and no generation replay.
"""
import argparse,base64,hashlib,json,sqlite3,time
from contextlib import closing
from pathlib import Path
from .repository import Repository,dump,digest
from .contracts import Fault


def normalize(row):
    return {k:({'base64':base64.b64encode(v).decode()} if isinstance(v,bytes) else v) for k,v in dict(row).items()}


def run(source,root=None,namespace=None,owner_principal=None,project=None):
    if (namespace is None)!=(owner_principal is None) or (namespace is not None and (not namespace or not owner_principal or not root)):
        raise Fault('LEGACY_BINDING_REQUIRED','namespace and owner-principal require each other and a target root')
    if project is not None and namespace is None:raise Fault('LEGACY_BINDING_REQUIRED','project requires explicit binding')
    source=Path(source).resolve()
    with closing(sqlite3.connect('file:'+str(source)+'?mode=ro',uri=True)) as c:
        c.row_factory=sqlite3.Row;c.execute('BEGIN')
        manifest={'source':str(source),'at':time.time(),'source_writes':False,'mode':'quarantine-import' if root else 'dry-run','tables':{},'activation':'none; no jobs, delivery or replay'}
        repo=Repository(root) if root else None
        with closing(repo.connect() if repo else sqlite3.connect(':memory:')) as out:
            try:
                out.execute('BEGIN IMMEDIATE')
                if repo:
                    out.execute('create table if not exists legacy_rows(source TEXT NOT NULL,table_name TEXT NOT NULL,row_number INTEGER NOT NULL,sha256 TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(source,table_name,row_number))')
                tables=[r['name'] for r in c.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name")]
                for table in tables:
                    q='"'+table.replace('"','""')+'"';n=0;h=hashlib.sha256()
                    for r in c.execute('select * from '+q+' order by rowid'):
                        n+=1;raw=dump(normalize(r));rh=hashlib.sha256(raw.encode()).hexdigest();h.update((raw+'\n').encode())
                        if repo:
                            old=out.execute('select sha256 from legacy_rows where source=? and table_name=? and row_number=?',(str(source),table,n)).fetchone()
                            if old and old[0]!=rh:raise Fault('LEGACY_SOURCE_CHANGED','source changed; use a new sealed snapshot/target')
                            out.execute('insert or ignore into legacy_rows values(?,?,?,?,?)',(str(source),table,n,rh,raw))
                            rb=out.execute('select data from legacy_rows where source=? and table_name=? and row_number=?',(str(source),table,n)).fetchone()[0]
                            if rb!=raw:raise Fault('LEGACY_INTEGRITY','preservation readback mismatch')
                    manifest['tables'][table]={'rows':n,'sha256':h.hexdigest(),'verified_readbacks':n if repo else 0}
                if repo:
                    # Detect deleted rows/tables too. A changed import rolls back
                    # as a whole, including otherwise unchanged earlier rows.
                    existing={r[0]:r[1] for r in out.execute('select table_name,count(*) from legacy_rows where source=? group by table_name',(str(source),))}
                    expected={k:v['rows'] for k,v in manifest['tables'].items() if v['rows']}
                    if existing!=expected:raise Fault('LEGACY_SOURCE_CHANGED','source row/table set changed')
                    if namespace is not None:
                        from .legacy_compat import bind
                        mh=digest({k:{f:v[f] for f in ('rows','sha256')} for k,v in manifest['tables'].items()})
                        count=bind(out,str(source),namespace,owner_principal,project,mh)
                        manifest['binding']={'namespace':namespace,'owner_principal':owner_principal,'project':project,'reservations':count,'manifest_hash':mh}
                    elif out.execute('select 1 from legacy_namespaces where source=?',(str(source),)).fetchone():
                        raise Fault('LEGACY_BINDING_REQUIRED','bound source requires exact binding on reimport')
                    else:
                        from .legacy_compat import guard_unbound_import
                        guard_unbound_import(out, str(source))
                out.commit()
            except BaseException:
                out.rollback();raise
        return manifest


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',required=True);p.add_argument('--target-root');p.add_argument('--manifest',required=True)
    p.add_argument('--namespace');p.add_argument('--owner-principal');p.add_argument('--project')
    a=p.parse_args()
    result=run(a.source,a.target_root,a.namespace,a.owner_principal,a.project)
    path=Path(a.manifest);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    # Receipts include source/owner attribution; never default to world-readable.
    import os
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:json.dump(result,f,indent=2)
    print(json.dumps(result,indent=2))
if __name__=='__main__':main()
