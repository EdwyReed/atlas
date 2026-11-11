"""Transport for the Windows worker; reuses NativeAdmin journal and explicit read-only mode."""
import json
from pathlib import Path
import re
import subprocess
from .contracts import Fault
from .lora_windows import parse_link, InstallError


class WindowsNoEffect(Fault):
    def __init__(self,failure):
        self.failure=failure
        super().__init__('WINDOWS_INSTALL_REJECTED','Windows read-only preflight rejected; no install effects')


def descriptor(policy):
    cfg=(policy or {}).get('windows_lora_installer')
    if not isinstance(cfg,dict) or set(cfg)!={'ssh_host','python'}:
        raise Fault('POLICY_UNVERIFIED','explicit Windows installer ssh_host/python required')
    # A host alias from existing SSH config, not caller-controlled shell content.
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,100}',cfg['ssh_host']):raise Fault('POLICY_INVALID','invalid SSH host alias')
    if cfg['python']!='python':raise Fault('POLICY_INVALID','only remote python command supported')
    return cfg


def execute(policy,p,model_type=None):
    cfg=descriptor(policy)
    try:parse_link(p['url'])
    except (InstallError,ValueError) as e:raise Fault('VALIDATION',str(e)) from None
    script=Path(__file__).with_name('lora_windows.py').read_text()
    request={'url':p['url'],'file_id':p.get('file_id')}
    if model_type is not None:request['model_type']=model_type
    if 'expected_sha256' in p:request['expected_sha256']=p['expected_sha256']
    script+='\nmain('+repr(request)+')\n'
    cmd=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','-o','StrictHostKeyChecking=yes',cfg['ssh_host'],'python -I -c "exec(__import__(\'sys\').stdin.read())"']
    try:
        proc=subprocess.run(cmd,input=script,capture_output=True,text=True,timeout=1200)
        if proc.returncode:raise Fault('WINDOWS_INSTALL_UNCERTAIN','Windows worker failed; inspect remote journal, do not replay')
        value=json.loads(proc.stdout)
    except (subprocess.SubprocessError,ValueError,OSError):raise Fault('WINDOWS_INSTALL_UNCERTAIN','Windows worker unavailable/uncertain; no automatic replay') from None
    if not value.get('ok'):
        failure=value.get('failure')
        if (isinstance(failure,dict) and set(failure)<={'stage','outcome','no_effect','http_status'}
            and failure.get('stage') in ('platform','metadata','availability')
            and failure.get('outcome')=='rejected' and failure.get('no_effect') is True
            and ('http_status' not in failure or type(failure['http_status']) is int and 100<=failure['http_status']<=599)):
            raise WindowsNoEffect(failure)
        raise Fault('WINDOWS_INSTALL_UNCERTAIN','Windows worker rejected or retained uncertain install; inspect remote journal')
    r=value['result']
    if r.get('state')!='completed' or r.get('integrity_verified') is not True or not r.get('installed_key'):
        raise Fault('WINDOWS_INSTALL_UNCERTAIN','unverified installer result')
    return r
