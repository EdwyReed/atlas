"""Prepare an explicitly supplied PRIVATE media build context and model volume.

No discovery, network, donor mutation, credentials, runtime, gallery or venv copy.
Only Python source and license notices enter the source context; weights stay
separate. Redistribution rights remain the operator's responsibility.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for data in iter(lambda:f.read(1024*1024),b''):h.update(data)
    return h.hexdigest()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('mask-root','sam-source','sam-weights','censor-source','censor-weights','out'):
        p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();out=a.out.absolute();out.mkdir(parents=True,exist_ok=False,mode=0o700)
    receipts=[]
    def copy(source,target):
        if source.is_symlink() or not source.is_file():raise ValueError('regular source files required')
        target.parent.mkdir(parents=True,exist_ok=True)
        before=sha(source);shutil.copyfile(source,target)
        if sha(source)!=before or sha(target)!=before:raise ValueError('source changed during copy')
        receipts.append({'path':str(target.relative_to(out)),'bytes':target.stat().st_size,'sha256':before})
    for root,dest,package in [(a.mask_root,'mask','atlas_mask'),(a.sam_source,'mobile-sam','mobile_sam'),(a.censor_source,'censor',None)]:
        root=root.absolute();selected=root/package if package else root
        files=list(selected.rglob('*.py')) if package else [root/'mosaic.py']
        if not files:raise ValueError('required source package missing')
        for source in files:
            copy(source,out/'sources'/dest/source.relative_to(root))
        for pattern in ('LICENSE*','COPYING*','NOTICE*'):
            for source in root.glob(pattern):
                if source.is_file():copy(source,out/'sources'/dest/source.name)
    copy(a.sam_weights,out/'weights'/'mobile_sam.pt')
    if sha(out/'weights'/'mobile_sam.pt')!='6dbb90523a35330fedd7f1d3dfc66f995213d81b29a5ca8108dbcdd4e37d6c2f':
        raise ValueError('MobileSAM weights differ from accepted backend pin')
    copy(a.censor_weights,out/'weights'/'ntd11.pt')
    (out/'manifest.json').write_text(json.dumps({'format':'atlas-private-media-kit-v1','files':receipts},indent=2))
    print(json.dumps({'prepared':True,'files':len(receipts),'weights_in_build_context':False,'manifest_sha256':sha(out/'manifest.json')}))


if __name__=='__main__':main()
