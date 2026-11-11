"""Initialize a NEW dedicated /run/atlas volume; never overwrites existing files.
Run only as namespace-root in a disposable --network none helper, not the daemon.
"""
import json
import os
from pathlib import Path
import secrets
import sys

port = int(sys.argv[1])
if not 1 <= port <= 65535:
    raise SystemExit('invalid host port')
root = Path('/run/atlas')
if any(root.iterdir()):
    raise SystemExit('refusing nonempty config volume')
os.umask(0o077)
token = secrets.token_urlsafe(36)
values = {
    'token': token,
    'clients.json': json.dumps([{'token': token, 'principal': 'operator', 'source': 'local-client'}]),
    'atlas.json': json.dumps({'root': '/var/lib/atlas', 'clients_file': '/run/atlas/clients.json',
                             'host': '0.0.0.0', 'port': 12088,
                             'container_endpoint': f'http://127.0.0.1:{port}', 'connections': []}, indent=2),
}
for name, value in values.items():
    path = root / name
    with path.open('x') as f:
        f.write(value)
    os.chown(path, 10001, 10001)
os.chown(root, 10001, 10001)
os.chmod(root, 0o700)
print('Initialized private config volume for UID 10001; credential not printed.')
