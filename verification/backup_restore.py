"""Compact populated offline outcome; private synthetic state, no live data.
Run installed Python -I verification/backup_restore.py --out NEW_DIRECTORY.
"""
import argparse
import base64
from contextlib import closing
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time

import atlas_engine
from atlas_engine.corpus import COLLECTIONS, Corpus, entity_id, point_id
from atlas_engine.engine import Engine
from atlas_engine.repository import Repository, dump
from atlas_engine.state_backup import backup, restore, reconcile, files, points, ro, sha, storage_path
from atlas_engine.contracts import SCHEMAS


class NoBackend:
    def __getattr__(self, name):
        raise AssertionError('external backend touched: ' + name)


def sql_image(path):
    with closing(ro(path)) as c:
        names = [r[0] for r in c.execute("select name from sqlite_master where type='table' order by name")]
        return {n: sorted(c.execute('select * from "' + n.replace('"','""') + '"').fetchall(), key=repr) for n in names}


def raw_image(root):
    return {n: sql_image(root/'corpus'/'qdrant'/'collection'/n/'storage.sqlite') for n in COLLECTIONS}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    out=a.out.absolute();out.mkdir(parents=True,exist_ok=False,mode=0o700)
    source=Path(__file__).absolute().parents[1]/'atlas_engine'
    installed=Path(atlas_engine.__file__).parent
    assert installed != source, 'must exercise installed package'
    assert {f.name:sha(f) for f in source.glob('*.py')} == {f.name:sha(f) for f in installed.glob('*.py')}
    def audit(event,args):
        if event=='socket.connect':raise AssertionError('network forbidden in offline outcome')
    sys.addaudithook(audit)
    checks=[]
    def passed(name):checks.append(name);print('PASS',name,flush=True)
    def refuse(fn):
        try:fn()
        except (ValueError,OSError):return
        raise AssertionError('expected refusal')
    root=out/'original';root.mkdir();(root/'service.lock').touch()
    engine=Engine(root,None,NoBackend(),NoBackend(),True,corpus_root=root/'corpus')
    (root/'connection-binding.json').write_text(dump({'id':'synthetic','url':'http://127.0.0.1:9','backend_generation':'fixture','account_scope':'test'}))
    (root/'clients-private.json').write_text('DO NOT COPY')
    identities={'tags':{'tag':'猫'},'loras':{'system':'test','model_id':'same','version_id':'1'},
                'checkpoints':{'system':'test','model_id':'same','version_id':'1'},
                'generations':{'system':'test','image_id':'same'},
                'tag_relations':{'endpoints':[{'tag':'猫','polarity':'positive'},{'tag':'bad','polarity':'negative'}]}}
    corpus=engine.corpus
    for name in COLLECTIONS:
        occ={'positive':['猫','猫'],'negative':['bad']} if name=='generations' else None
        for revision in (1,2):
            corpus.append(name,identities[name],{'opaque':{'unknown':[None,'é',-1]},'statement':str(revision)},
                          {'source':'synthetic'},occ,{'principal':'test'},['fixture',name,revision])
        if name=='tags':
            corpus.append(name,identities[name],{}, {},None,{},['delete'],action='delete')
    generation=corpus.head('generations',entity_id('generations',identities['generations']))
    corpus.historical_relations(generation,{'principal':'test'})
    # Missing head preimage must survive transfer before logical reads repair it.
    corpus.client.delete('loras',[point_id(['head','loras',entity_id('loras',identities['loras'])])],wait=True)
    graph=SCHEMAS['generation.submitGraph'].model_validate({'connection_id':'synthetic','backend_generation':'fixture','request_id':'uncertain','graph':{'nodes':{},'edges':[]},'runs':1}).model_dump()
    job,_=engine.repo.reserve('test','synthetic','uncertain','generation',graph)
    engine.repo.update(job['id'],'ack_unknown',{'native_ids':[]})
    # Explicit upload BLOB preservation; not an image-inference acceptance.
    with engine.repo.connect() as c:
        c.execute('insert into uploads values(?,?,?,?,?,?,?,?,?,?,?)',('u','test','blob','h',None,5,'hash','image','receiving',b'\x00\xffabc','{}'))
    data=b'\x00\xffartifact bytes\n';path=root/'artifacts'/'blob';path.write_bytes(data)
    metadata={'artifact_id':'a','job_id':job['id'],'image_name':'fixture','sha256':hashlib.sha256(data).hexdigest(),'size':len(data)}
    with engine.repo.connect() as c:c.execute('insert into artifacts values(?,?,?,?,?)',('a',job['id'],'fixture',str(path),dump(metadata)))
    media=root/'media';media.mkdir();(media/'manifest.json').write_text('{"source":"synthetic","opaque":true}')
    (media/'candidate-0.npy').write_bytes(b'synthetic prior bytes, not inference')
    refuse(lambda:backup(root,out/'busy-corpus'))
    passed('active corpus owner refused')
    engine.close()
    with (root/'service.lock').open() as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        refuse(lambda:backup(root,out/'busy-service'))
    passed('same daemon service.lock refused')
    # Leave a committed operational WAL with a read-only open connection so it
    # cannot disappear on final writer close. Backup must include its event.
    writer=sqlite3.connect(root/'registry.sqlite3');writer.execute('PRAGMA wal_autocheckpoint=0')
    writer.execute("insert into health values('wal-boundary','{\"synthetic\":true}')");writer.commit()
    before_sql=sql_image(root/'registry.sqlite3');before_raw=raw_image(root)
    before_files=files(root)
    manifest=backup(root,out/'backup')
    assert (out/'backup'/'state'/'clients-private.json').exists() is False
    assert manifest['configuration_secrets_included'] is False
    restored=out/'restored';restore(out/'backup',restored)
    assert raw_image(restored)==before_raw
    assert sql_image(restored/'registry.sqlite3')==before_sql
    assert points(restored)==manifest['collections']
    assert files(root)==before_files, 'source changed during backup/restore'
    writer.close()
    assert (restored/'media'/'manifest.json').read_bytes()==(media/'manifest.json').read_bytes()
    assert (restored/'media'/'candidate-0.npy').read_bytes()==(media/'candidate-0.npy').read_bytes()
    assert storage_path(restored,str(path))==restored/'artifacts'/'blob'
    passed('exact five-collection raw point preimage and WAL ledger/BLOB/files/binding preservation')
    refuse(lambda:restore(out/'backup',restored));refuse(lambda:backup(root,out/'backup'))
    refuse(lambda:restore(out/'backup',out/'restored'/'..'/'traversal'))
    (out/'link').symlink_to(out/'backup',target_is_directory=True)
    refuse(lambda:restore(out/'link',out/'linked-restore'))
    (root/'artifacts'/'escape').symlink_to(out/'backup'/'manifest.json')
    refuse(lambda:backup(root,out/'symlink-backup'));(root/'artifacts'/'escape').unlink()
    corrupt=out/'corrupt';shutil.copytree(out/'backup',corrupt)
    with (corrupt/'state'/'artifacts'/'blob').open('ab') as f:f.write(b'bad')
    refuse(lambda:restore(corrupt,out/'corrupt-restore'))
    passed('overwrite/traversal/symlink/hash corruption refused')
    engine=Engine(restored,None,NoBackend(),NoBackend(),True,allow_admin=True,corpus_root=restored/'corpus')
    engine.start();time.sleep(.1)
    assert engine.thread is None and engine.restore_quarantined
    assert engine.call('status',{})['result']['restore_reconciliation_required']
    assert engine.repo.count()==1
    replay=engine.call('generation.submitGraph',graph,'test','synthetic')
    assert replay['ok'] and replay['result']['id']==job['id'] and engine.repo.count()==1
    new=engine.call('generation.submitGraph',graph|{'request_id':'new'},'test','synthetic')
    assert new['error']['code']=='RESTORE_RECONCILIATION_REQUIRED'
    assert engine.repo.get(job['id'])['state']=='ack_unknown'
    read=engine.call('artifact.read',{'artifact_id':'a','offset':0,'size':100})
    assert read['ok'] and base64.b64decode(read['result']['base64'])==data
    for name in COLLECTIONS:
        result=engine.call('corpus.history',{'collection':name,'identity':identities[name]})
        assert result['ok'] and len(result['result']['items'])>=2
    assert engine.call('corpus.get',{'collection':'loras','identity':identities['loras']})['ok']
    assert raw_image(root)==before_raw, 'head repair touched source'
    after=raw_image(restored)
    for name in COLLECTIONS:
        if name!='loras':assert after[name]==before_raw[name]
    assert len(after['loras']['points'])==len(before_raw['loras']['points'])+1
    engine.close()
    passed('restored local reads/receipt replay; no workers/network; head repair only on destination')
    # Unknown external effects cannot be cleared by an empty or generic report.
    report=out/'reconciliation.json'
    assessment={k:'synthetic fixture reviewed; no live effects' for k in ('generation','workspace','media','acquisition','projection','uploads')}
    report.write_text(dump(assessment|{'registry_sha256':sha(restored/'registry.sqlite3'),'external_effects_reconciled':True}))
    refuse(lambda:reconcile(restored,report))
    # Simulate an explicit operator resolution only in the synthetic restored DB.
    repo=Repository(restored);repo.update(job['id'],'failed',{'synthetic_reconciliation':'no external operation executed'})
    report.write_text(dump(assessment|{'registry_sha256':sha(restored/'registry.sqlite3'),'external_effects_reconciled':True}))
    state=reconcile(restored,report);assert state['reconciled'] and state['reconciliation_report']==json.loads(report.read_text())
    engine=Engine(restored,None,NoBackend(),NoBackend(),True,corpus_root=restored/'corpus')
    assert not engine.restore_quarantined
    engine.close()  # Do not start activated core against any backend.
    passed('explicit bound reconciliation required; uncertain job prevents activation')
    receipt={'checks':checks,'synthetic':True,'legacy_corpus_migration':False,'native_execution':False,
             'installed_package':str(installed),'collections':manifest['collections'],'network_connections':0}
    (out/'receipt.json').write_text(json.dumps(receipt,indent=2));print(json.dumps(receipt,indent=2))


if __name__=='__main__':main()
