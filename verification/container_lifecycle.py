"""Docker lifecycle adapter for the existing boundary scenarios (owned fixtures only)."""
import json
from pathlib import Path
import subprocess
import uuid


class Container:
    def __init__(self, image, out, port, token, modules):
        self.image, self.out, self.port = image, out, port
        self.name = 'atlas-check-' + uuid.uuid4().hex[:12]
        self.state, self.secrets = self.name + '-state', self.name + '-secrets'
        self.run('network', 'create', self.name)
        self.run('volume', 'create', self.state)
        self.run('volume', 'create', self.secrets)
        self.token = token
        self.modules = modules
        self.created = False
        self.commands = []

    def run(self, *args, check=True, input=None):
        r = subprocess.run(['docker', *args], input=input, capture_output=True, text=True, timeout=60)
        if check and r.returncode:
            raise RuntimeError(r.stderr)
        return r

    def configure(self, settings):
        # The init helper alone is namespace-root, never the service. No host chown.
        payload = {'atlas.json': json.dumps(settings), 'token': self.token,
                   'clients.json': json.dumps([{'token': self.token, 'principal': 'acceptance', 'source': 'synthetic'}])}
        code = ('import json,os,sys;from pathlib import Path;'
                'p=Path("/secrets");os.chown(p,10001,10001);os.chmod(p,0o700);'
                'data=json.load(sys.stdin);'
                '[( (p/k).write_text(v),os.chmod(p/k,0o600),os.chown(p/k,10001,10001)) for k,v in data.items()]')
        self.run('run', '--rm', '-i', '--network', 'none', '--user', '0:0',
                 '-v', self.secrets + ':/secrets', '--entrypoint', 'python', self.image,
                 '-c', code, input=json.dumps(payload))

    def start(self):
        if not self.created:
            args = ['create', '--name', self.name, '--network', self.name,
                    '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                    '--memory', '512m', '--cpus', '1', '--pids-limit', '128',
                    '--tmpfs', '/tmp:rw,noexec,nosuid,size=32m',
                    '-p', f'127.0.0.1:{self.port}:12088',
                    '-v', self.state + ':/var/lib/atlas',
                    '-v', self.secrets + ':/run/atlas:ro', self.image]
            self.commands.append(['docker', *args])
            self.run(*args)
            self.created = True
        self.run('start', self.name)
        return self

    def poll(self):
        if not self.created:
            return 0
        value = json.loads(self.run('inspect', '--format', '{{json .State}}', self.name).stdout)
        return None if value['Running'] else value['ExitCode']

    def terminate(self):
        self.run('stop', '--time', '20', self.name)

    def wait(self, timeout=20):
        return int(self.run('inspect', '--format', '{{.State.ExitCode}}', self.name).stdout)

    def exec(self, args):
        return self.run('exec', self.name, *args, check=False)

    def verify(self):
        code = ('import atlas_engine,hashlib,json,os,sys,importlib.metadata;from pathlib import Path;'
                'print(json.dumps({"uid":os.getuid(),"python":sys.version,"dependencies":{d.metadata["Name"]:d.version for d in importlib.metadata.distributions()},"modules":{f.name:hashlib.sha256(f.read_bytes()).hexdigest() '
                'for f in Path(atlas_engine.__file__).parent.glob("*.py")}}))')
        value = json.loads(self.exec(['python', '-I', '-c', code]).stdout)
        assert value['uid'] == 10001 and value['modules'] == self.modules
        inspected = json.loads(self.run('inspect', self.name).stdout)[0]
        assert inspected['Config']['User'] == '10001:10001'
        assert inspected['HostConfig']['PortBindings']['12088/tcp'] == [{'HostIp': '127.0.0.1', 'HostPort': str(self.port)}]
        assert inspected['HostConfig']['ReadonlyRootfs']
        assert json.loads(self.run('network', 'inspect', self.name).stdout)[0]['Driver'] == 'bridge'
        value.update(image_id=inspected['Image'], network=self.name, state_volume=self.state,
                     ports=inspected['NetworkSettings']['Ports'], commands=self.commands)
        (self.out / 'container.json').write_text(json.dumps(value, indent=2) + '\n')
        return value

    def cleanup(self):
        if self.created:
            (self.out / 'container.log').write_text(self.run('logs', self.name, check=False).stdout +
                                                   self.run('logs', self.name, check=False).stderr)
            self.run('rm', '-f', self.name)
        self.run('volume', 'rm', self.state, self.secrets)
        self.run('network', 'rm', self.name)
        assert self.run('inspect', self.name, check=False).returncode != 0
        assert self.run('volume', 'inspect', self.state, check=False).returncode != 0
        assert self.run('volume', 'inspect', self.secrets, check=False).returncode != 0
        assert self.run('network', 'inspect', self.name, check=False).returncode != 0
        (self.out / 'cleanup.json').write_text(json.dumps({'status': 'pass', 'removed':
            [self.name, self.state, self.secrets, 'network:' + self.name]}) + '\n')
