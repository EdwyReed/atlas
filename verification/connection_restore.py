"""Populated copied SINGLE migration and dual v2 portable offline restore.
Original evidence roots stay closed/read-only. No live state or native execution.
"""
import argparse, base64, fcntl, hashlib, json, os, socket, sqlite3, subprocess, sys, time
from pathlib import Path
from contextlib import closing
import httpx
from atlas_engine.config import Config, bind_connections
from atlas_engine.engine import Engine
from atlas_engine.state_backup import backup, restore, files, ro, points, offline


def registry_snapshot(root, inventory):
    # Caller holds both existing owner locks. immutable is safe ONLY when the
    # pre-read inventory proves there is no nonempty WAL or rollback journal.
    clean = all(not inventory.get('registry.sqlite3'+suffix, {}).get('bytes', 0)
                for suffix in ('-wal', '-journal'))
    uri = (root/'registry.sqlite3').as_uri() + ('?mode=ro&immutable=1' if clean else '?mode=ro')
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.execute('BEGIN')
        schema = db.execute('select * from sqlite_master order by type,name').fetchall()
        tables = {row[1]: sorted(db.execute('select * from "'+row[1].replace('"','""')+'"').fetchall(), key=repr)
                  for row in schema if row[0]=='table'}
    body = repr({'schema': schema, 'tables': tables})
    return {'read_mode': 'immutable_closed_no_pending_journal' if clean else 'read_only_including_WAL',
            'sha256': hashlib.sha256(body.encode()).hexdigest(), 'schema_and_rows': body,
            'table_count': len(tables)}


def preservation_changes(before, after):
    changes = {k: {'before': before.get(k), 'after': after.get(k)}
               for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
    runtime, durable = {}, {}
    for name, change in changes.items():
        sidecar = name == 'registry.sqlite3-shm' or (
            name == 'registry.sqlite3-wal' and
            all(v is None or v['bytes']==0 for v in change.values()))
        (runtime if sidecar else durable)[name] = change
    return changes, runtime, durable
from atlas_engine.upstream import Invoke, Atlas, unconfigured


def image(root):
    with closing(ro(root/'registry.sqlite3')) as db:
        names=[r[0] for r in db.execute("select name from sqlite_master where type='table' order by name")]
        return {n:sorted(db.execute('select * from "'+n.replace('"','""')+'"').fetchall(),key=repr) for n in names}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);p.add_argument('--single',type=Path,required=True);p.add_argument('--dual',type=Path,required=True);p.add_argument('--child',type=Path)
    a=p.parse_args()
    if a.child:
        def audit(event,args):
            if event=='socket.connect':
                (a.child.parent/'forbidden-network-attempt').write_text(repr(args))
                raise AssertionError('restored daemon attempted external access')
        sys.addaudithook(audit)
        sys.argv=['atlas','--config',str(a.child)]
        from atlas_engine.server import main as serve
        serve();return
    out=a.out.absolute();out.mkdir(parents=True,exist_ok=False,mode=0o700);os.umask(0o077);checks=[]
    for label,origin in [('single',a.single),('dual',a.dual)]:
        case=out/label;case.mkdir();origin=origin.absolute()
        with offline(origin/'state'):
            original_inventory=files(origin/'state')  # BEFORE the first SQLite read
            original_registry=registry_snapshot(origin/'state', original_inventory)
            after_initial_read=files(origin/'state')
        (case/'original-source-before.json').write_text(json.dumps({'files':original_inventory,'registry':original_registry,'after_initial_read':after_initial_read},indent=2))
        # Supported offline transfer, not a raw copy of an open SQLite/Qdrant.
        backup(origin/'state',case/'source-snapshot');staging=case/'copied';restore(case/'source-snapshot',staging)
        config=json.loads((origin/'atlas.json').read_text());config.update(root=str(staging),clients_file='clients.json',migrate_connection_binding=True)
        config.pop('policy_file',None)
        # All original acceptance credentials are owned ephemeral fixture data.
        (case/'clients.json').write_bytes((origin/'clients.json').read_bytes())
        for c in config['connections']:
            old=(origin/c['token_file']).resolve();c['token_file']=old.name
            (case/old.name).write_bytes(old.read_bytes());(case/old.name).chmod(0o600)
        conf=case/'atlas.json';conf.write_text(json.dumps(config));cfg=Config.load(conf)
        legacy=(staging/'connection-binding.json').read_bytes() if (staging/'connection-binding.json').exists() else None
        if label=='single':assert legacy is not None and not (staging/'connection-bindings.json').exists()
        manifest=bind_connections(staging,cfg.connections,True)
        assert all(v['token_file']==next(c['token_file'] for c in config['connections'] if c['id']==k) for k,v in manifest['connections'].items())
        if legacy:assert (staging/'connection-binding.json').read_bytes()==legacy
        before=image(staging)
        engine=Engine(staging,None,unconfigured(Invoke,'INVOKE_UNCONFIGURED'),unconfigured(Atlas,'REFERENCE_UNCONFIGURED'),True,corpus_root=staging/'corpus')
        for revision in (1,2):engine.corpus.append('tags',{'tag':'portable-'+label},{'literal':'v'+str(revision),'opaque':[None,'猫']},{'fixture':label},None,{'principal':'fixture'},['portable',label,revision])
        engine.close()
        assert all(image(staging)[n]==rows for n,rows in before.items()),'migration changed existing ledger rows'
        with sqlite3.connect(staging/'registry.sqlite3') as db:
            db.execute('create table retained_opaque_legacy(id text primary key, body blob, unknown text)')
            db.execute('insert into retained_opaque_legacy values(?,?,?)',('same',b'\x00\xffinert','not canonicalized'))
        expected=image(staging);expected_points=points(staging)
        sealed=case/'backup';m=backup(staging,sealed);assert 'connection-bindings.json' in m['included']
        relocated=case/'relocated-private';relocated.mkdir();destination=relocated/'state';restore(sealed,destination)
        assert {k:v for k,v in files(destination).items() if k not in ('restore-state.json','service.lock')}=={k:v for k,v in m['files'].items() if k!='restore-state.json'}
        assert image(destination)==expected and points(destination)==expected_points
        assert (destination/'connection-bindings.json').read_bytes()==(staging/'connection-bindings.json').read_bytes()
        # Copy the identical config-relative secret references and bytes to a new
        # directory. Root/port are runtime location, not native identity.
        (relocated/'clients.json').write_bytes((case/'clients.json').read_bytes())
        for c in config['connections']:
            (relocated/c['token_file']).write_bytes((case/c['token_file']).read_bytes());(relocated/c['token_file']).chmod(0o600)
        with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        config.update(root='state',port=port,migrate_connection_binding=False)
        target=relocated/'atlas.json';target.write_text(json.dumps(config))
        credentials=json.loads((relocated/'clients.json').read_text());headers={'Authorization':'Bearer '+credentials[0]['token']};url=f'http://127.0.0.1:{port}'
        command=[sys.executable,'-I','-B',str(Path(__file__).absolute()),'--out',str(out),'--single',str(a.single),'--dual',str(a.dual),'--child',str(target)]
        with (case/'restored-startup.log').open('w') as log:
            child=subprocess.Popen(command,stdout=log,stderr=log,cwd=relocated)
            try:
                for _ in range(300):
                    assert child.poll() is None,('restored startup failed',label)
                    try:
                        if httpx.get(url+'/v1/schema',headers=headers,trust_env=False,timeout=.5).status_code==200:break
                    except httpx.TransportError:pass
                    time.sleep(.1)
                else:raise AssertionError('startup timeout')
                def op(name,args):
                    v=httpx.post(url+'/v1/ops/'+name,json=args,headers=headers,trust_env=False,timeout=15).json();assert v['ok'],v;return v['result']
                assert op('status',{})['restore_reconciliation_required']
                history=op('corpus.history',{'collection':'tags','identity':{'tag':'portable-'+label}});assert len(history['items'])==2
                with sqlite3.connect(destination/'registry.sqlite3') as db:
                    rows=db.execute("select id,data from jobs where kind='generation' and state='succeeded'").fetchall();assert rows
                    for jid,raw in rows:
                        j=json.loads(raw);assert op('generation.get',{'job_id':jid})['state']=='succeeded'
                        payload=j['payload'];operation='workflows.run' if payload.get('workflow_id') else 'generation.submitGraph'
                        assert op(operation,payload)['id']==jid
                    artifacts=db.execute('select id,metadata from artifacts').fetchall() if False else db.execute('select * from artifacts').fetchall()
                    for row in artifacts:
                        metadata=json.loads(row[-1]);v=op('artifact.read',{'artifact_id':row[0]})
                        assert v['sha256']==metadata['sha256'] and hashlib.sha256(base64.b64decode(v['base64'])).hexdigest()==metadata['sha256']
            finally:
                child.terminate()
                try:child.wait(timeout=30)
                except subprocess.TimeoutExpired:child.kill();child.wait();raise
        assert child.returncode in (0,-15) and not (relocated/'forbidden-network-attempt').exists(),child.returncode
        actual=image(destination)
        assert all(actual[n]==rows for n,rows in expected.items() if n not in ('health','scoped_observations')),'restored reads/replay changed durable rows'
        assert all(row in actual['scoped_observations'] for row in expected['scoped_observations'])
        added=[row for row in actual['scoped_observations'] if row not in expected['scoped_observations']]
        if label=='single':
            for scope,native_id,job_id,data,at in added:
                assert json.loads(scope)['connection_id']==manifest['legacy_connection_id']
                assert (native_id,job_id,data,at) in expected['observations']
        else:assert not added
        with offline(origin/'state'):
            before_final_read=files(origin/'state')
            final_registry=registry_snapshot(origin/'state', before_final_read)
            final_inventory=files(origin/'state')
        source_changes,runtime_changes,durable_changes=preservation_changes(original_inventory,final_inventory)
        # Check every observation boundary, so a nonempty WAL cannot disappear
        # during our own logical reads and hide behind a later empty sidecar.
        durable_phases={}
        for phase,inventory in [('initial_read',after_initial_read),('before_final_read',before_final_read),('final_read',final_inventory)]:
            _,_,changed=preservation_changes(original_inventory,inventory)
            if changed:durable_phases[phase]=changed
        receipt={'changes':source_changes,'sqlite_read_only_runtime_effects':runtime_changes,
                 'durable_changes':durable_changes,'durable_phase_changes':durable_phases,
                 'before_final_read':before_final_read,'final_files':final_inventory,'registry':final_registry,
                 'logical_registry_equal':original_registry['sha256']==final_registry['sha256'],
                 'byte_zero_writes_claimed':False}
        (case/'original-source-changes.json').write_text(json.dumps(receipt,indent=2))
        assert not durable_phases,('original durable evidence changed',durable_phases)
        assert receipt['logical_registry_equal'],'original registry schema/rows changed'
        checks.append({'scenario':label,'original_durable_bytes_and_logical_registry_preserved':True,'sqlite_runtime_sidecar_changes':runtime_changes,'byte_zero_writes_claimed':False,'tables_preserved':len(expected),'jobs':len(expected['jobs']),'artifact_refs':len(expected['artifacts']),'quarantined_relocated_startup':True,'network_attempts':0,'native_reenqueue':0,'exact_preopen_inventory':True,'process_stopped':True})
        print('PASS',label,'populated migration/backup/restore/relocated startup/local replay; no network',flush=True)
    (out/'result.json').write_text(json.dumps({'ok':True,'checks':checks},indent=2))

if __name__=='__main__':main()
