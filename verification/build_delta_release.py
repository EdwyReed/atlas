"""Build source-only delta successor artifacts, without touching an active venv."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile


def run(args, **kwargs):
    return subprocess.run(list(map(str,args)),check=True,**kwargs)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--donor-python',type=Path,required=True)
    a=p.parse_args();a.out.mkdir(mode=0o700,parents=True,exist_ok=False)
    stage=a.out/'build';stage.mkdir()
    for n in ('README.md','pyproject.toml'):shutil.copyfile(a.source/n,stage/n)
    shutil.copytree(a.source/'atlas_engine',stage/'atlas_engine',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    env={**os.environ,'SOURCE_DATE_EPOCH':'1794700800','PYTHONDONTWRITEBYTECODE':'1'}
    for folder in ('release','repro'):
        run(['uv','build','--wheel','--python',a.donor_python,'--out-dir',a.out/folder,stage],env=env)
    wheel=next((a.out/'release').glob('*.whl'));other=next((a.out/'repro').glob('*.whl'))
    assert wheel.read_bytes()==other.read_bytes(),'wheel not reproducible'
    venv=a.out/'isolated-install';run([a.donor_python,'-m','venv','--without-pip',venv])
    python=venv/'bin/python';run(['uv','pip','install','--offline','--python',python,'--no-deps',wheel])
    donor=run([a.donor_python,'-I','-c','import sysconfig;print(sysconfig.get_paths()["purelib"])'],capture_output=True,text=True).stdout.strip()
    own=run([python,'-I','-c','import sysconfig;print(sysconfig.get_paths()["purelib"])'],capture_output=True,text=True).stdout.strip()
    Path(own,'atlas-fixture-donor.pth').write_text(donor+'\n')
    receipt={'wheel':str(wheel),'sha256':hashlib.sha256(wheel.read_bytes()).hexdigest(),
             'reproducible':True,'source_date_epoch':env['SOURCE_DATE_EPOCH'],'fixture_python':str(python),
             'donor_site':donor,'scope':'new package-only isolated install; donor runtime dependencies unchanged'}
    (a.out/'build-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt))

if __name__=='__main__':main()
