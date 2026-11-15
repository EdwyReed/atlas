"""Existing Windows SSH transport with an optional operator-owned config binding."""
import os
from pathlib import Path
from .contracts import Fault


def command(cfg):
    args = ['ssh']
    config = os.environ.get('ATLAS_SSH_CONFIG')
    if config:
        path = Path(config)
        if not path.is_absolute() or not path.is_file():
            raise Fault('POLICY_UNVERIFIED', 'explicit SSH configuration is unavailable')
        args += ['-F', str(path)]
    return args + ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                   '-o', 'StrictHostKeyChecking=yes', '-o', 'ForwardAgent=no',
                   cfg['ssh_host'],
                   'python -I -c "exec(__import__(\'sys\').stdin.read())"']
