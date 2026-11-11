"""Installed mask ownership/restore guards; health only, no model inference."""
import argparse,ctypes,json,os,socket,subprocess,sys,time
from pathlib import Path
from atlas_engine.engine import Engine

p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);p.add_argument('--backend',type=Path,required=True);a=p.parse_args()
a.out.mkdir(parents=True,exist_ok=False);backend=a.backend.resolve();sock=backend/'runtime/worker.sock';assert not sock.exists()
config={'mask':{'root':str(backend)}};checks=[]
def engine(name,config=config):return Engine(a.out/name,None,None,None,media_config=config)
e=engine('restored');e.restore_quarantined=True;e.start()
assert not getattr(e,'media_thread',None)
assert e.call('mask.health',{})['error']['code']=='RESTORE_RECONCILIATION_REQUIRED'
assert e.media.mask_process is None and not sock.exists();e.close();checks.append('restore-quarantine admission/worker suppression, health cannot start inference')
e=engine('owner');other=engine('competitor')
try:
    health=e.call('mask.health',{});assert health['ok'] and not health['result']['loaded'],health
    pid=health['result']['pid'];assert other.call('mask.health',{})['error']['code']=='CAPACITY'
finally:other.close();e.close()
assert not Path(f'/proc/{pid}').exists() and not sock.exists();checks.append('private root single-owner; unloaded real worker; graceful terminate/reap')
with socket.socket(socket.AF_UNIX) as foreign:
    foreign.bind(str(sock));inode=sock.stat().st_ino;e=engine('foreign')
    try:
        assert e.call('mask.health',{})['error']['code']=='UNAVAILABLE'
        assert e.media.mask_process is None
    finally:e.close()
    assert sock.stat().st_ino==inode
sock.unlink();checks.append('pre-existing socket refused without attachment, removal or worker launch')
e=engine('missing',{'mask':{'root':str(a.out/'unconfigured')}})
try:assert e.call('mask.health',{})['error']['code']=='UNAVAILABLE'
finally:e.close()
checks.append('missing explicit backend binding refuses personal/sibling fallback')
# Adopt only our own orphan subtree so the probe can reap its parent-death child.
assert ctypes.CDLL(None,use_errno=True).prctl(36,1,0,0,0)==0
code="from atlas_engine.engine import Engine;import json,time;from pathlib import Path;e=Engine(Path(%r),None,None,None,media_config=%r);print(json.dumps(e.call('mask.health',{})),flush=True);time.sleep(60)"%(str(a.out/'crash'),config)
owner=subprocess.Popen([sys.executable,'-I','-B','-c',code],stdout=subprocess.PIPE,text=True)
try:
    health=json.loads(owner.stdout.readline());assert health['ok'] and not health['result']['loaded'];pid=health['result']['pid']
finally:owner.kill();owner.wait(timeout=10)
for _ in range(100):
    done,status=os.waitpid(pid,os.WNOHANG)
    if done:break
    time.sleep(.05)
assert done==pid and not Path(f'/proc/{pid}').exists()
# Worker SIGKILL can leave a socket; a new Engine must not attach to it.
if sock.exists():
    e=engine('stale')
    try:assert e.call('mask.health',{})['error']['code']=='UNAVAILABLE'
    finally:e.close()
    sock.unlink()  # Exact private socket of the now-reaped owned PID only.
checks.append('abrupt Engine death kills actual unloaded worker; adopted child reaped; stale socket fails closed')
(a.out/'summary.json').write_text(json.dumps({'checks':checks,'owned_workers_after':0,'actual_model_inference':False},indent=2));print(json.dumps(checks,indent=2))
