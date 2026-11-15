"""Bounded real read-only Windows SSH acceptance. No Atlas data/backend mounts.

Explicit existing identity is mounted ONLY into a private constrained-agent
sidecar. It is never read by Python, copied, printed, or mounted in application.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid


def run(*args, check=True, **kwargs):
    return subprocess.run(list(args), check=check, capture_output=True, text=True,
                          timeout=90, **kwargs)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--image', action='append', required=True)
    p.add_argument('--identity', required=True)
    p.add_argument('--known-hosts', required=True)
    p.add_argument('--host', required=True)
    p.add_argument('--hostname', required=True)
    p.add_argument('--user', required=True)
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    a.out.mkdir(mode=0o700, parents=True, exist_ok=False)
    name = 'atlas-ssh-check-' + uuid.uuid4().hex[:10]
    volume = name + '-socket'
    agent = name + '-agent'
    app = name + '-app'
    config = a.out / 'config'
    config.write_text(f'''Host {a.host}
    HostName {a.hostname}
    User {a.user}
    IdentityAgent /run/atlas-ssh-agent/socket
    IdentityFile none
    UserKnownHostsFile /run/atlas-ssh/known_hosts
    GlobalKnownHostsFile /dev/null
    StrictHostKeyChecking yes
    UpdateHostKeys no
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    ForwardAgent no
''')
    # Copy only the public, already trusted host entries. No network enrollment.
    public = run('ssh-keygen', '-F', a.hostname, '-f', a.known_hosts).stdout
    public = '\n'.join(l for l in public.splitlines() if not l.startswith('#')) + '\n'
    if not public.strip():
        raise RuntimeError('existing host pin missing')
    (a.out / 'known_hosts').write_text(public)
    config.chmod(0o644)
    (a.out / 'known_hosts').chmod(0o644)
    host_cmd = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ConnectTimeout=10', a.host,
                'python -I -c "import json,sys;print(json.dumps(dict(platform=sys.platform,version=list(sys.version_info[:3]))))"']
    baseline = json.loads(run(*host_cmd).stdout)
    assert baseline['platform'] == 'win32'
    probe = '''import json,sys,subprocess,shutil,os
from atlas_engine.windows_ssh import command
cmd=command({'ssh_host':sys.argv[1],'python':'python'})
script='import json,sys;print(json.dumps(dict(platform=sys.platform,version=list(sys.version_info[:3]))))'
r=subprocess.run(cmd[:1]+['-vv']+cmd[1:],input=script,text=True,capture_output=True,timeout=30)
print(json.dumps(dict(code=r.returncode,value=json.loads(r.stdout) if r.returncode==0 else None,uid=os.getuid(),ssh=shutil.which('ssh'),error=r.stderr[-6000:] if r.returncode else '')))
'''
    result = {'baseline': baseline, 'images': {}, 'limits': 'remote Python platform/version only; no file reads/writes, downloads, installer or native requests'}
    created = []
    try:
        run('docker', 'volume', 'create', volume)
        for image in a.image:
            run('docker', 'run', '-d', '--name', agent, '--read-only', '--network', 'none',
                '--tmpfs', '/var/lib/atlas:rw,noexec,nosuid,size=1m',
                '--user', '0:0', '--cap-drop', 'ALL', '--cap-add', 'CHOWN',
                '--cap-add', 'SETUID', '--cap-add', 'SETGID', '--cap-add', 'DAC_OVERRIDE', '--cap-add', 'KILL',
                '--security-opt', 'no-new-privileges', '--pids-limit', '32', '--memory', '128m',
                '-v', f'{volume}:/run/atlas-ssh-agent',
                '-v', f'{a.identity}:/run/atlas-ssh/identity:ro',
                '-v', f'{a.out / "known_hosts"}:/run/atlas-ssh/known_hosts:ro',
                '--entrypoint', 'python', image, '/opt/atlas/ssh-agent.py',
                '--destination', a.user + '@' + a.hostname)
            created.append(agent)
            for _ in range(60):
                logs = run('docker', 'logs', agent).stdout
                if 'destination-constrained agent ready' in logs:
                    break
                if run('docker','inspect','--format','{{.State.Running}}',agent).stdout.strip() != 'true':
                    raise RuntimeError('agent initialization failed: ' + logs)
                time.sleep(.1)
            else:
                raise RuntimeError('agent readiness timed out')
            run('docker', 'run', '-d', '--name', app, '--read-only', '--cap-drop', 'ALL',
                '--tmpfs', '/var/lib/atlas:rw,noexec,nosuid,size=1m',
                '--security-opt', 'no-new-privileges', '--pids-limit', '64', '--memory', '256m',
                '-e', 'ATLAS_SSH_CONFIG=/run/atlas-ssh/config',
                '-v', f'{volume}:/run/atlas-ssh-agent:ro',
                '-v', f'{config}:/run/atlas-ssh/config:ro',
                '-v', f'{a.out / "known_hosts"}:/run/atlas-ssh/known_hosts:ro',
                '--entrypoint', 'python', image, '-c', 'import time;time.sleep(3600)')
            created.append(app)
            first = json.loads(run('docker','exec','-i',app,'python','-I','-B','-',a.host,input=probe).stdout)
            assert first == dict(code=0,value=baseline,uid=10001,ssh='/usr/bin/ssh',error=''), first
            # No private identity or user HOME is attached to the application.
            mounts = json.loads(run('docker','inspect',app).stdout)[0]['Mounts']
            assert not any(m['Destination'].endswith('identity') or m['Destination']=='/home/atlas/.ssh' for m in mounts)
            run('docker','restart',app)
            after_app = json.loads(run('docker','exec','-i',app,'python','-I','-B','-',a.host,input=probe).stdout)
            assert after_app == first
            run('docker','stop','-t','5',agent)
            stopped = json.loads(run('docker','exec','-i',app,'python','-I','-B','-',a.host,input=probe).stdout)
            assert stopped['code'] != 0 and stopped['value'] is None
            run('docker','start',agent)
            for _ in range(60):
                ready = run('docker','exec',agent,'test','-S','/run/atlas-ssh-agent/socket',check=False)
                if ready.returncode == 0:
                    time.sleep(.3)
                    break
                time.sleep(.1)
            after_agent = json.loads(run('docker','exec','-i',app,'python','-I','-B','-',a.host,input=probe).stdout)
            assert after_agent == first
            # An absent host pin cannot enroll/trust the remote host.
            bad = '''import subprocess,sys,json
from atlas_engine.windows_ssh import command
c=command({'ssh_host':sys.argv[1]});c[1:1]=['-o','UserKnownHostsFile=/dev/null']
r=subprocess.run(c,input='print(1)',text=True,capture_output=True,timeout=30)
print(json.dumps(dict(code=r.returncode,host_key_refused='Host key verification failed' in r.stderr)))
'''
            refusal = json.loads(run('docker','exec','-i',app,'python','-I','-B','-',a.host,input=bad).stdout)
            assert refusal['code'] != 0 and refusal['host_key_refused']
            deps = run('docker','exec',app,'dpkg-query','-W','-f=${Package}=${Version}\n','openssh-client','libssl3','openssl','libbsd0','libedit2','libcbor0.8','libfido2-1').stdout
            run('docker','exec',app,'atlas','--help')
            run('docker','exec',app,'atlas-portable','--help')
            result['images'][image] = dict(image_id=run('docker','inspect','--format','{{.Image}}',app).stdout.strip(),
                first=first,app_restart=after_app,agent_stop=stopped,agent_restart=after_agent,
                unknown_host=refusal,dependencies=deps,mounts=mounts,
                agent_log=run('docker','logs',agent).stdout)
            run('docker','rm','-f','-v',app,agent)
            created.clear()
        result['ok'] = True
    finally:
        for item in reversed(created):
            run('docker','rm','-f','-v',item,check=False)
        run('docker','volume','rm',volume,check=False)
        result['cleanup'] = dict(container_absent=all(run('docker','inspect',n,check=False).returncode != 0 for n in [agent,app]),
                                 socket_volume_absent=run('docker','volume','inspect',volume,check=False).returncode != 0)
        (a.out / 'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))


if __name__ == '__main__':
    main()
