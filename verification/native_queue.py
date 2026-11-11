"""Compact HTTP/MCP queue acceptance against an explicitly isolated native URL.

The caller owns native bootstrap/isolation. A loopback fault proxy drops replies
or hides receipts; it never executes graphs or fabricates an admission ACK.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import httpx
sys.path.insert(0, str(Path(__file__).absolute().parent))
from native_workspace import child, free_port
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--package-root',type=Path,default=Path(__file__).absolute().parents[1])
    p.add_argument('--native-url')
    p.add_argument('--retention-hook-url', help='optional private loopback fixture purge hook, NEVER a production endpoint')
    p.add_argument('--child',action='store_true');p.add_argument('--config',type=Path)
    a=p.parse_args()
    if a.child:return child(a.config,a.package_root)
    assert urlsplit(a.native_url).hostname=='127.0.0.1', 'isolated loopback backend required'
    if a.retention_hook_url:
        assert urlsplit(a.retention_hook_url).hostname == '127.0.0.1'
    out=a.out.absolute();out.mkdir(parents=True,exist_ok=False,mode=0o700)
    mode={'fault':'','hide_receipt':False,'hide_items':False,'process':None}
    mode['ack_fault']=''
    calls=[]
    class Proxy(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def forward(self):
            body=self.rfile.read(int(self.headers.get('Content-Length',0)))
            calls.append({'method':self.command,'path':self.path,'body':json.loads(body) if body else None})
            is_ack=self.path.endswith('/enqueue_batch/acknowledge')
            if is_ack:
                # Independent SQLite connection proves the COMPLETE receipt transaction
                # is committed before any native acknowledgement can leave Atlas.
                with sqlite3.connect(out/'state/registry.sqlite3') as db:
                    rows=[json.loads(row[0]) for row in db.execute('select data from jobs')]
                saved=[row for row in rows if row.get('prepared_submission',{}).get('batch',{}).get('idempotency_key')==json.loads(body)['idempotency_key']]
                assert len(saved)==1 and saved[0].get('native_receipt')
                assert set(saved[0]['native_ids'])==set(saved[0]['native_receipt']['item_ids'])
                if mode['ack_fault']=='crash-local':
                    mode['ack_fault']=''
                    os.kill(mode['process'].pid,signal.SIGKILL)
                    self.close_connection=True
                    return
            fault=mode['fault'] if self.path.endswith('/enqueue_batch') else ''
            if fault:mode['fault']=''
            if fault=='crash-before':
                os.kill(mode['process'].pid,signal.SIGKILL)
                self.close_connection=True
                return
            if mode['hide_receipt'] and '/receipt?' in self.path:
                status,raw=404,b'{}'
            elif mode['hide_items'] and '/item_ids?' in self.path:
                status,raw=200,b'{"item_ids":[],"total_count":0}'
            else:
                r=httpx.request(self.command,a.native_url+self.path,content=body or None,
                    headers={'Content-Type':'application/json'},trust_env=False,timeout=15)
                status,raw=r.status_code,r.content
            if is_ack and mode['ack_fault']=='lost':
                assert status==204,(status,raw)
                self.close_connection=True
                return
            if fault in ('lost-ack','crash-after'):
                assert status<300,(status,raw)
                if fault=='crash-after':os.kill(mode['process'].pid,signal.SIGKILL)
                self.close_connection=True
                return
            self.send_response(status);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        do_GET=do_POST=do_PUT=do_DELETE=forward
    proxy=ThreadingHTTPServer(('127.0.0.1',0),Proxy)
    threading.Thread(target=proxy.serve_forever,daemon=True).start()
    token=os.urandom(32).hex();clients=out/'clients.json'
    clients.write_text(json.dumps([{'token':token,'principal':'acceptance','source':'isolated'}]));clients.chmod(0o600)
    (out/'policy.json').write_text(json.dumps({'accepted_modes':['native_graph']}))
    port=free_port();endpoint=f'http://127.0.0.1:{port}'
    config=out/'atlas.json'
    config.write_text(json.dumps({'root':'state','clients_file':'clients.json','policy_file':'policy.json','port':port,
        'connections':[{'id':'disposable','backend_generation':'queue-admission-1','account_scope':'synthetic',
        'url':f'http://127.0.0.1:{proxy.server_port}','enable_writes':True}]}))
    headers={'Authorization':'Bearer '+token};log=(out/'atlas.log').open('w');checks=[]
    def op(name,args):
        r=httpx.post(endpoint+'/v1/ops/'+name,json=args,headers=headers,trust_env=False,timeout=30)
        r.raise_for_status();return r.json()
    def ok(name,args):
        r=op(name,args);assert r['ok'],r;return r['result']
    def start():
        mode['process']=subprocess.Popen([sys.executable,'-I',str(Path(__file__).absolute()),'--child',
            '--out',str(out),'--config',str(config),'--package-root',str(a.package_root.absolute())],
            cwd=out,stdout=log,stderr=log,env={**os.environ,'HOME':str(out/'empty-home')})
        for _ in range(200):
            assert mode['process'].poll() is None,'daemon failed; see atlas.log'
            try:
                if op('status',{})['ok']:return
            except httpx.TransportError:pass
            time.sleep(.05)
        raise RuntimeError('readiness timeout')
    def stop():
        proc=mode['process']
        if proc and proc.poll() is None:proc.terminate()
        if proc:proc.wait(timeout=45)
    def payload(rid,runs=1):
        return {'connection_id':'disposable','backend_generation':'queue-admission-1','request_id':rid,'runs':runs,
                'graph':{'nodes':{'n':{'id':'n','type':'integer','value':17,'is_intermediate':True,'use_cache':False}},'edges':[]}}
    def posts():return sum(c['method']=='POST' and c['path'].endswith('/enqueue_batch') for c in calls)
    def get(jid):return ok('generation.get',{'job_id':jid})
    def reconcile():assert ok('observer.reconcile',{})['ok']
    def passed(s):checks.append(s);print('PASS',s,flush=True)
    async def mcp(name,args):
        async with httpx.AsyncClient(headers=headers,trust_env=False,timeout=30) as client:
            async with streamable_http_client(endpoint+'/mcp/',http_client=client) as streams:
                async with ClientSession(streams[0],streams[1]) as session:
                    await session.initialize();return (await session.call_tool(name,args)).structuredContent
    try:
        start();p1=payload('admit',3)
        j=ok('generation.submitGraph',p1);assert len(j['native_ids'])==3 and j['native_receipt']['requested']==3,j
        prepared=j['prepared_submission'];assert prepared['batch']['data'] is None and prepared['batch']['runs']==3
        assert j['native_scope']['backend_generation']=='queue-admission-1'
        count=posts();m=asyncio.run(mcp('generation_submitGraph',p1));assert m['ok'] and m['result']['id']==j['id'] and posts()==count,m
        assert op('generation.submitGraph',{**p1,'runs':2})['error']['code']=='IDEMPOTENCY_CONFLICT'
        assert op('generation.submitGraph',{**p1,'backend_generation':'wrong'})['error']['code']=='NATIVE_SCOPE_MISMATCH'
        passed('HTTP admission three runs, complete durable receipt, MCP exact replay and scope/rebind refusal')
        p2=payload('concurrent')
        with ThreadPoolExecutor(4) as pool:rs=list(pool.map(lambda _:ok('generation.submitGraph',p2),range(4)))
        assert len({r['id'] for r in rs})==1 and posts()==count+1
        passed('concurrent request-id reservation emits exactly one native POST')
        count=posts();stop();start()
        assert ok('generation.submitGraph',p1)['prepared_submission']==prepared and posts()==count
        passed('restart retains generated graph ID, exact payload, IDs and receipt without POST')
        mode['fault']='lost-ack';lost=ok('generation.submitGraph',payload('lost'))
        reconcile();lost=get(lost['id']);assert lost['native_receipt'] and len(lost['native_ids'])==1,lost
        count=posts();assert ok('generation.submitGraph',payload('lost'))['id']==lost['id'] and posts()==count
        passed('lost ACK recovered through native GET receipt, never repeat enqueue')
        mode['hide_receipt']=True;mode['hide_items']=True;mode['fault']='crash-after'
        try:op('generation.submitGraph',payload('crash-after'))
        except httpx.TransportError:pass
        stop();count=posts();start()
        unknown=ok('generation.submitGraph',payload('crash-after'));assert unknown['state']=='ack_unknown' and not unknown['native_ids'],unknown
        reconcile();assert posts()==count
        assert op('generation.cancel',{'job_id':unknown['id']})['error']['code']=='ACK_UNKNOWN'
        assert op('generation.retry',{'job_id':unknown['id'],'request_id':'unsafe'})['error']['code']=='RETRY_UNSAFE'
        passed('crash after native commit plus receipt 404/history absence stays unknown; retry/cancel cannot guess')
        mode['hide_receipt']=False;mode['hide_items']=False;reconcile()
        recovered=get(unknown['id']);assert recovered['native_receipt'] and recovered['native_ids'] and posts()==count,recovered
        mode['hide_items']=True;reconcile();missing=get(recovered['id'])
        assert missing['history_missing'] and missing['native_receipt']==recovered['native_receipt']
        assert missing['native_ids']==recovered['native_ids'] and posts()==count
        mode['hide_items']=False
        passed('receipt recovers exact IDs after restart; missing queue history retains durable identity')
        target=rs[0]['id'];target_ids=get(target)['native_ids']
        before=len([c for c in calls if c['method']=='PUT'])
        canceled=ok('generation.cancel',{'job_id':target});assert canceled['state']=='canceled',canceled
        writes=[c['path'] for c in calls if c['method']=='PUT'][before:]
        assert writes==[f'/api/v1/queue/default/i/{target_ids[0]}/cancel'],writes
        passed('targeted cancel reads and mutates only the receipt item; native canceled observation')
        mode['ack_fault']='lost'
        durable=ok('generation.submitGraph',payload('receipt-ack-lost'))
        assert durable['native_receipt_ack']=='pending' and durable['native_ids'],durable
        count=posts();reconcile()
        key=durable['prepared_submission']['batch']['idempotency_key']
        assert sum(c['path'].endswith('/acknowledge') and c['body']['idempotency_key']==key for c in calls)>=2
        if a.retention_hook_url:
            purge=httpx.post(a.retention_hook_url,trust_env=False);purge.raise_for_status()
            assert purge.json()['deleted']>0,purge.text
            r=httpx.get(a.native_url+'/api/v1/queue/default/enqueue_batch/receipt',params={'idempotency_key':key},trust_env=False)
            assert r.status_code==404,r.text
        mode['hide_receipt']=True;mode['ack_fault']='';reconcile()
        after=get(durable['id'])
        assert after['native_receipt_ack']=='acknowledged' and after['native_ids']==durable['native_ids']
        assert after['native_items'] and after['native_receipt']==durable['native_receipt'] and posts()==count
        assert asyncio.run(mcp('generation_submitGraph',payload('receipt-ack-lost')))['result']['id']==durable['id']
        assert posts()==count
        mode['hide_receipt']=False
        passed('receipt ACK follows complete local commit; lost/repeated ACK and receipt 404 retain exact-ID observation without enqueue')
        mode['ack_fault']='crash-local'
        try:op('generation.submitGraph',payload('receipt-local-crash'))
        except httpx.TransportError:pass
        stop();count=posts();start();reconcile()
        saved=ok('generation.submitGraph',payload('receipt-local-crash'))
        assert saved['native_receipt_ack']=='acknowledged' and saved['native_ids'] and posts()==count,saved
        passed('crash after local receipt commit before native ACK recovers maintenance on restart, never enqueue')
        mode['hide_receipt']=True;mode['hide_items']=True;mode['fault']='crash-before'
        try:op('generation.submitGraph',payload('crash-before'))
        except httpx.TransportError:pass
        stop();count=posts();start()
        never=ok('generation.submitGraph',payload('crash-before'));reconcile()
        assert never['state']=='ack_unknown' and posts()==count,never
        passed('crash before backend dispatch is conservatively unknown, not silently replayed')
        # Verify complete receipt really lives in SQLite, not just the reply object.
        with sqlite3.connect(out/'state/registry.sqlite3') as db:
            data=json.loads(db.execute('select data from jobs where id=?',(j['id'],)).fetchone()[0])
            assert data['native_receipt']==j['native_receipt'] and data['prepared_submission']==prepared
        (out/'result.json').write_text(json.dumps({'checks':checks,'enqueue_posts':posts(),'calls':calls},indent=2))
    finally:
        stop();proxy.shutdown();proxy.server_close();log.close()

if __name__=='__main__':main()
