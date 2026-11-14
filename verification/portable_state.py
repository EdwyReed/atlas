"""Portable populated restore; exact history, references, quarantine and restart.

Synthetic source for destructive/fault checks. Real corpus acceptance is separate.
"""
import argparse
import asyncio
from contextlib import closing
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from atlas_engine import portable as pt, state_backup as sb
from atlas_engine.repository import dump


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();out=a.out.absolute();out.mkdir(parents=True,mode=0o700,exist_ok=False)
    spec=importlib.util.spec_from_file_location('legacy_fixture',Path(__file__).with_name('legacy_attachment.py'))
    fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
    mapping=fixture.fixture(out)
    import gc;gc.collect()
    attachment=Path(mapping['attachment']);desc=json.loads(attachment.read_text())
    receipt=Path(desc['reference_delta'])/'receipt.json';r=json.loads(receipt.read_text())
    from atlas_engine.stream_delta import baseline_pin
    with closing(sqlite3.connect(Path(desc['baseline']).as_uri()+'?immutable=1',uri=True)) as db:
        r['baseline_pin']=json.loads(dump(baseline_pin(db)))
    receipt.write_text(dump(r));desc['reference_delta_receipt_sha256']=sb.sha(receipt);attachment.write_text(dump(desc))
    root=Path(desc['knowledge_ledger']).parent
    (root/'service.lock').touch()
    from atlas_engine.engine import Engine
    from atlas_engine.upstream import Invoke,Atlas,unconfigured
    engine=Engine(root,None,unconfigured(Invoke,'UNCONFIGURED'),unconfigured(Atlas,'UNCONFIGURED'),corpus_root=root/'corpus')
    # The borrowed corruption fixture has deliberately malformed request keys.
    # Use valid, unbound synthetic tombstones here: they reserve their exact keys
    # without globally preventing unrelated new admissions.
    with engine.repo.connect() as db:
        for source,number in db.execute("select source,row_number from legacy_rows where table_name='requests'").fetchall():
            raw=dump({'idem':'legacy-key-'+str(number),'id':'legacy-id-'+str(number)})
            db.execute("update legacy_rows set data=?,sha256=? where source=? and table_name='requests' and row_number=?",(raw,hashlib.sha256(raw.encode()).hexdigest(),source,number))
    for rev in (1,2):engine.corpus.append('tags',{'tag':'猫'},{'literal':' \t猫\n','revision':rev},{'source':'fixture'},None,{},['fixture',rev])
    with engine.repo.connect() as db:
        db.execute('insert into jobs values(?,?,?,?,?,?,?,?,?)',('uncertain-job','fixture','uncertain','generation','fixture-hash','ack_unknown',dump({'payload':{'request_id':'uncertain'}}),1,1))
    j=engine.repo.get('uncertain-job')
    # Opaque historical intents must stay byte-for-byte inert after activation,
    # including families with automatic startup/aggregation hooks.
    for kind in sorted(pt.HISTORICAL_EFFECTS-{'generation'}):
        engine.repo.reserve('fixture','fixture','historical-'+kind,kind,
                            {'request_id':'historical-'+kind}, {'fixture':'inert'})
    art=root/'artifacts'/'original';art.write_bytes(b'\x00\xffliteral artifact\n')
    with engine.repo.connect() as db:
        db.execute('insert into artifacts values(?,?,?,?,?)',('a',j['id'],'fixture',str(art),dump({'artifact_id':'a','job_id':j['id'],'sha256':sb.sha(art),'size':art.stat().st_size})))
    engine.close()
    gc.collect()
    with closing(sqlite3.connect(root/'registry.sqlite3')) as db:db.execute('pragma wal_checkpoint(TRUNCATE)')
    desc['knowledge_ledger_sha256']=sb.sha(root/'registry.sqlite3');attachment.write_text(dump(desc))
    (root/'attachment-owner.json').write_text(dump({'attachment':{'path':str(attachment),'sha256':sb.sha(attachment)},'code_sha256':'historical-code'}))
    (root/'adoption-receipt.json').write_text(dump({'original':'opaque immutable adoption provenance','plan':{'native_binding':None}}))
    (root/'secret.json').write_text('DO NOT COPY')
    vocab=out/'vocabulary.sqlite3'
    with sqlite3.connect(vocab) as db:
        db.executescript("create table tags(id integer primary key,tag text,post_count integer);insert into tags values(1,'猫',10);create table related(tag text,related_tag text,score real,pmi real,support real,pair_count integer,related_count integer);")
    before=sb.files(root)
    archive=out/'state.tar.gz';pt.pack(root,archive,attachment,vocab)
    assert sb.files(root)==before
    restored=out/'restored';pt.unpack(archive,restored)
    assert not (restored/'secret.json').exists()
    assert (restored/'adoption-receipt.json').read_bytes()==(root/'adoption-receipt.json').read_bytes()
    assert sb.storage_path(restored,str(art)).read_bytes()==art.read_bytes()
    assert sb.restore_state(restored)['reconciled'] is False
    assert pt.reference(restored).evidence('resource','fixture-visible')[0]['payload']['name']=='fixture-visible'
    # Source locations cease to exist before server startup. Originals remain retained.
    external=out/'synthetic-source';external.rename(out/'unavailable-original-source')
    vocab.rename(out/'unavailable-original-vocabulary')
    token=secrets.token_urlsafe(32);clients=out/'clients.json';clients.write_text(dump([{'token':token,'principal':'fixture','source':'portable-check'}]));clients.chmod(0o600)
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    config=out/'atlas.json';config.write_text(dump({'root':str(restored),'clients_file':str(clients),'port':port}))
    endpoint='http://127.0.0.1:'+str(port);headers={'Authorization':'Bearer '+token}
    def op(name,args={}):
        r=httpx.post(endpoint+'/v1/ops/'+name,json=args,headers=headers,trust_env=False,timeout=20);r.raise_for_status();return r.json()
    async def mcp():
        async with streamable_http_client(endpoint+'/mcp/',http_client=httpx.AsyncClient(headers=headers,trust_env=False)) as (rd,wr,_):
            async with ClientSession(rd,wr) as session:
                await session.initialize();r=await session.call_tool('knowledge_history',{'entity_id':'synthetic Ω','after_revision':0,'limit':10})
                result=r.structuredContent or json.loads(r.content[0].text)
                assert result['ok'],result
    def run_server(activated=False):
        log=(out/'daemon.log').open('ab')
        proc=subprocess.Popen([sys.executable,'-I','-B','-m','atlas_engine.server','--config',str(config)],stdout=log,stderr=log)
        try:
            for _ in range(300):
                if proc.poll() is not None:raise AssertionError('server exit; '+str(out/'daemon.log'))
                try:
                    if op('status')['ok']:break
                except httpx.HTTPError:pass
                time.sleep(.1)
            else:raise AssertionError('startup timeout')
            assert op('knowledge.get',{'entity_id':'synthetic Ω'})['ok']
            assert op('artifact.read',{'artifact_id':'a','offset':0,'size':32768})['ok']
            refusal=op('corpus.put',{'request_id':'new-after-activation' if activated else 'forbidden','collection':'tags','identity':{'tag':'new'},'expected_revision':0,'body':{'x':1}})
            if activated:
                assert refusal['ok'],refusal
                assert op('batch.get',{'job_id':historical_batch})['ok']
                time.sleep(1.5)
            else:
                assert refusal.get('error',{}).get('code') in ('RESTORE_RECONCILIATION_REQUIRED','LEGACY_UNBOUND'),refusal
            asyncio.run(mcp())
        finally:
            proc.terminate();proc.wait(timeout=30);log.close()
        assert proc.returncode in (0,-15),proc.returncode
    run_server();run_server()
    # Repack relocated populated root; no old path may be opened.
    second=out/'second.tar.gz';pt.pack(restored,second);pt.unpack(second,out/'second')
    assert pt.reference(out/'second').evidence('resource','fixture-visible')
    # Truncation and interruption never admit a partial ledger.
    bad=out/'truncated.tar.gz';bad.write_bytes(archive.read_bytes()[:archive.stat().st_size//2])
    for label,fn in [('truncated',lambda:pt.unpack(bad,out/'bad')),('interrupted',lambda:pt.unpack(archive,out/'interrupted',lambda name:(_ for _ in ()).throw(OSError('injected')) if name=='registry.sqlite3' else None))]:
        try:fn()
        except (ValueError,OSError,tarfile.TarError,EOFError):pass
        else:raise AssertionError('missing failure '+label)
        partial=out/('bad' if label=='truncated' else 'interrupted')
        try:sb.restore_state(partial)
        except ValueError:pass
        else:raise AssertionError('partial admitted '+label)
    from atlas_engine.adoption_native import row_pins
    from atlas_engine.adoption import connection
    with closing(connection(restored/'registry.sqlite3',closed=True)) as db:
        ids=[r[0] for r in db.execute('select id from jobs where kind in ('+','.join('?' for _ in pt.HISTORICAL_EFFECTS)+') order by id',tuple(pt.HISTORICAL_EFFECTS))]
        historical_batch=db.execute("select id from jobs where kind='batch'").fetchone()[0]
    original_pins=row_pins(restored,ids)
    report=out/'activation.json'
    report.write_text(dump({'target_root':str(restored),'operator':'synthetic-fixture',
        'assessment':'Retain all historical effects; permit new unrelated local work.',
        'registry_logical_sha256':sb.registry_digest(restored),
        'backup_manifest_sha256':sb.restore_state(restored)['backup_manifest_sha256'],
        'runtime':sb.versions(),'retain_historical_effects':True}))
    pt.activate(restored,report)
    run_server(True);run_server(True)
    assert row_pins(restored,ids)==original_pins
    result={'status':'pass','synthetic':True,'checks':['literal full-ledger and canonical history','separate immutable reference map','original paths absent at HTTP/MCP startup','artifact relocation','restart quarantine/no replay','repack/restored second root','truncation/interruption refusal','excluded configuration secrets','explicit activation and new corpus work','all historical effect families inert across two starts']}
    (out/'result.json').write_text(dump(result));print(dump(result))

if __name__=='__main__':main()
