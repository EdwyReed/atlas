"""Offline CLI for reviewed delta manifests; no service or production endpoints."""
import argparse,json
from pathlib import Path
from .repository import Repository
from .knowledge import Knowledge

def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--manifest',type=Path,required=True);p.add_argument('--archive',type=Path,required=True);p.add_argument('--receipt',type=Path,required=True);a=p.parse_args()
    # Refuse the source/full export and require an explicit isolated destination
    # marker minted by the operator. This CLI is not exposed over HTTP/MCP.
    if not (a.root/'.isolated-migration-destination').is_file():raise SystemExit('explicit isolated destination marker required')
    result=Knowledge(Repository(a.root)).ingest(json.loads(a.manifest.read_text()),a.archive)
    a.receipt.write_text(json.dumps(result,indent=2));a.receipt.chmod(0o600);print(json.dumps(result))
if __name__=='__main__':main()
