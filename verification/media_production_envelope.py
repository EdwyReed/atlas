"""Disposable Compose lifecycle with the exact failed production helper caps.

No production bindings accepted: all mounts are newly owned volumes. SSH signing
is not needed for CPU media; its separately accepted transport remains unchanged.
"""
import hashlib
import json
from pathlib import Path
import subprocess
from container_lifecycle import Container as Base


class Container(Base):
    old_helper = None

    def helper(self, code, *args, check=True):
        return self.run('run','--rm','--network','none','--read-only','--user','0:0',
            '--cap-drop','ALL','--cap-add','DAC_OVERRIDE','--cap-add','CHOWN',
            '--security-opt','no-new-privileges',
            '-v',self.state+':/var/lib/atlas','-v',self.secrets+':/run/atlas',
            '--entrypoint','python',self.image,'-I','-B','-c',code,*args,check=check)

    def provision(self, weights):
        # Build an honest populated portable root with seven uncertain effects.
        code='''import json,gc,os,sqlite3
from pathlib import Path
from atlas_engine.engine import Engine
from atlas_engine.upstream import Invoke,Atlas,unconfigured
from atlas_engine import portable as pt,state_backup as sb,adoption_control as ctl
r=Path('/var/lib/atlas/seed');e=Engine(r,None,unconfigured(Invoke,'OFFLINE'),unconfigured(Atlas,'OFFLINE'),corpus_root=r/'corpus')
for i in range(7):e.repo.reserve('acceptance','synthetic',str(i),'generation',{'request_id':str(i)})
e.close();del e;gc.collect()
with sqlite3.connect(r/'registry.sqlite3') as db:db.execute('pragma wal_checkpoint(TRUNCATE)');db.execute('pragma journal_mode=DELETE')
(r/'service.lock').touch()
archive=Path('/var/lib/atlas/capture.tar.gz');pt.pack(r,archive,media_weights='/inputs')
t=Path('/var/lib/atlas/state');pt.unpack(archive,t);s=sb.restore_state(t)
p=Path('/var/lib/atlas/activation.json');sb.write_json(p,dict(target_root=str(t),operator='disposable acceptance',assessment='Keep all seven uncertain effects unchanged; never replay',retain_historical_effects=True,runtime=sb.versions(),backup_manifest_sha256=s['backup_manifest_sha256'],registry_logical_sha256=sb.registry_digest(t)))
assert pt.activate(t,p)['held']==7
ctl.atomic(Path('/var/lib/atlas/holds.json'),pt.protected_rows(t))
for d,ds,fs in os.walk('/var/lib/atlas'):
 os.chown(d,10001,10001)
 for n in fs:os.chown(Path(d)/n,10001,10001)
'''
        args=['run','--rm','--network','none','--read-only','--tmpfs','/tmp:rw,noexec,nosuid,size=64m','--user','0:0','--cap-drop','ALL','--cap-add','DAC_OVERRIDE','--cap-add','CHOWN','--security-opt','no-new-privileges','-v',self.state+':/var/lib/atlas','-v',str(weights.absolute())+':/inputs:ro','--entrypoint','python',self.image,'-I','-B','-c',code]
        self.run(*args)
        old=Path(self.old_helper).read_text()
        failure=self.helper(old,'--restored','--state-root','/var/lib/atlas/state',check=False)
        (self.out/'old-helper.log').write_text(failure.stdout+failure.stderr)
        assert failure.returncode and 'PermissionError' in failure.stderr and 'Operation not permitted' in failure.stderr,failure.stderr
        # Existing config and bytes survive. Plan lives outside both state/config.
        prepare="import runpy,sys;sys.argv=['init','--restored','--state-root','/var/lib/atlas/state','--prepare-recovery','/var/lib/atlas/review.json'];runpy.run_path('/opt/atlas-media/init.py',run_name='__main__')"
        plan=json.loads(self.helper(prepare).stdout)
        pin=plan['plan_sha256']
        invoke="import runpy,sys;sys.argv=['init','--restored','--state-root','/var/lib/atlas/state','--recovery-plan','/var/lib/atlas/review.json','--sha256',sys.argv[1]];runpy.run_path('/opt/atlas-media/init.py',run_name='__main__')"
        wrong=self.helper(invoke,'0'*64,check=False);assert wrong.returncode
        fixed=self.helper(invoke,pin);again=self.helper(invoke,pin)
        assert json.loads(again.stdout)['replayed']
        (self.out/'provision.json').write_text(json.dumps({'old_eperm_reproduced':True,'caps':['DAC_OVERRIDE','CHOWN'],'no_FOWNER':True,'plan':plan,'recovery':json.loads(fixed.stdout),'replay':json.loads(again.stdout)},indent=2))
        # Helper cannot modify any unrelated config content.
        self.config_before=self.helper("from atlas_engine.state_backup import files;from pathlib import Path;import json;print(json.dumps(files(Path('/run/atlas'))))").stdout

    def start(self):
        if not self.created:
            doc={'name':self.name,'services':{'atlas':{
                'image':self.image,'container_name':self.name,'user':'10001:10001','read_only':True,
                'cap_drop':['ALL'],'security_opt':['no-new-privileges:true'],
                'cpus':2,'mem_limit':'2g','pids_limit':128,'init':True,
                'restart':'no','stop_grace_period':'30s',
                'ports':[f'127.0.0.1:{self.port}:12088'],
                'tmpfs':['/tmp:rw,noexec,nosuid,size=64m'],
                'volumes':[self.state+':/var/lib/atlas',self.secrets+':/run/atlas:ro']}},
                'volumes':{self.state:{'external':True},self.secrets:{'external':True}},
                'networks':{'default':{'external':True,'name':self.name}}}
            self.compose=self.out/'compose.json';self.compose.write_text(json.dumps(doc,indent=2))
            subprocess.run(['docker','compose','-f',str(self.compose),'up','-d'],check=True,capture_output=True,text=True)
            self.name_actual=self.run('compose','-f',str(self.compose),'ps','-q','atlas').stdout.strip()
            self.compose_name=self.name;self.created=True
            self.commands.append(['docker','compose','-f',str(self.compose),'up','-d'])
        else:self.run('start',self.name)
        return self

    def verify(self,state_root='/var/lib/atlas/state'):
        value=super().verify(state_root)
        code='''import json
from pathlib import Path
from atlas_engine import portable as pt,adoption_control as ctl
r=Path('/var/lib/atlas/state');pins=pt.protected_rows(r)
assert pins==ctl.read(Path('/var/lib/atlas/holds.json')) and len(pins)==7
assert (r/'.portable-runtime-admitted.json').is_file()
print(json.dumps({'exact_seven_rows':pins,'durable_admission_witness':True}))
'''
        readback=self.exec(['python','-I','-B','-c',code])
        assert readback.returncode==0,readback.stderr
        assert json.loads(readback.stdout)['durable_admission_witness']
        (self.out/'hold-readback.json').write_text(readback.stdout)
        return value

    def cleanup(self):
        if hasattr(self,'config_before'):
            assert self.helper("from atlas_engine.state_backup import files;from pathlib import Path;import json;print(json.dumps(files(Path('/run/atlas'))))").stdout==self.config_before
        if self.created:
            self.verify()
            (self.out/'container.log').write_text(self.run('logs',self.name,check=False).stdout+self.run('logs',self.name,check=False).stderr)
            self.run('compose','-f',str(self.compose),'down')
            self.name=self.compose_name;self.created=False
        super().cleanup()
