"""Installed consumer/bounds regression from owned full-server receipts.

Copies only synthetic upload rows read-only into a NEW local fixture registry.
Model catalog/DTOs are explicitly synthetic for planning, never generation.
"""
import argparse
import base64
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from atlas_engine.artifacts import Artifacts
from atlas_engine.contracts import Fault, Plan, SCHEMAS
from atlas_engine.engine import Engine
from atlas_engine.repository import Repository
from atlas_engine.uploads import Uploads


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',required=True,type=Path)
    p.add_argument('--fullserver',required=True,type=Path)
    a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False)
    checks=[]
    repo=Repository(a.out/'state')
    uploads=Uploads(repo,None)
    with sqlite3.connect('file:'+str((a.fullserver/'state/registry.sqlite3').absolute())+'?mode=ro',uri=True) as source:
        rows=source.execute('select * from uploads').fetchall()
    with repo.connect() as db:db.executemany('insert into uploads values(?,?,?,?,?,?,?,?,?,?,?)',rows)
    receipts=json.loads((a.fullserver/'family-mask.json').read_text())
    by_key={(x['job']['result']['family'],x['job']['result']['role']):x['job']['result'] for x in receipts}
    def refused(fn,code):
        try:fn()
        except Fault as e:assert e.code==code,(e.code,code)
        else:raise AssertionError('expected '+code)
    def model(key,base,kind,name=None):return {'key':key,'hash':'synthetic-hash','name':name or key,'base':base,'type':kind}
    models=[model('sd1','sd-1','main'),model('anima','anima','main'),
        model('vae','anima','vae','Anima QwenImage VAE'),
        model('encoder','anima','qwen3_encoder','Anima Qwen3 0.6B Text Encoder')]
    def dto(method,path):
        assert method=='GET' and path.startswith('/api/v1/images/i/')
        return {'width':128,'height':96}
    e=Engine.__new__(Engine);e.uploads=uploads;e.invoke=SimpleNamespace(models=lambda:models,call=dto)
    for family,key in [('sd-1','sd1'),('anima','anima')]:
        image,mask=(by_key[(family,role)] for role in ('image','mask'))
        meta=uploads.mask(mask['image_name'])
        assert meta['family']==family and meta['mask_contract']=='white-edit-black-preserve'
        assert meta['native_verified']==mask['native_representation']
        assert (image['native']['width'],image['native']['height'])==(128,96)
        assert (mask['native']['width'],mask['native']['height'])==(128,96)
        args=Plan(model=key,prompt='synthetic planner only',mode='inpaint',width=128,height=96,
            image_name=image['image_name'],mask_name=mask['image_name']).model_dump()
        plan=e._plan(args);g=plan['graph']
        assert g['nodes']['init']['image']['image_name']==image['image_name']
        assert g['nodes']['mask']['image']['image_name']==mask['image_name']
        assert g['nodes']['inpaint_mask']['type']==('create_denoise_mask' if family=='sd-1' else 'create_gradient_mask')
        assert plan['submit_enabled'] is False
        wrong=deepcopy(args);wrong['mask_name']=by_key[('anima' if family=='sd-1' else 'sd-1','mask')]['image_name']
        refused(lambda:e._plan(wrong),'MASK_FAMILY')
    checks.append('actual verified SD1/Anima upload metadata consumed by existing inpaint planners; exact native image/mask IDs; cross-family refusal; synthetic catalog only, no inference')
    refused(lambda:uploads.pixel(by_key[('sd-1','image')]['image_name']),'PIXEL_FORMAT')
    refused(lambda:uploads.pixel(by_key[('anima','image')]['image_name']),'PIXEL_FORMAT')
    refused(lambda:uploads.mask('unowned-mask.png'),'MASK_CONTRACT')
    checks.append('JPEG/RGBA accepted for native family imports remain invalid pixel-upscale sources; unowned mask refused')
    # Original native receipts include a mask whose POST committed but ACK was lost.
    for row in rows:
        if row[7]=='mask' and row[8]=='ack_unknown':
            assert not json.loads(row[10]).get('image_name')
    checks.append('unknown native masks have no adopted recipe identity')
    mask=by_key[('sd-1','mask')];row=uploads.row(mask['upload_id'])
    with patch('atlas_engine.uploads.MAX_BYTES',1):
        refused(lambda:uploads.native_representation(row,'sd-1'),'IMAGE_LIMIT')
    artifacts=Artifacts(repo,None)
    with patch('atlas_engine.artifacts.MAX_BYTES',1):
        refused(lambda:artifacts.capture_upload('oversized','source.png',row['bytes']),'IMAGE_LIMIT')
    with patch('atlas_engine.artifacts.MAX_PIXELS',1):
        refused(lambda:artifacts.capture_upload('oversized-pixels','source.png',row['bytes']),'IMAGE_LIMIT')
    assert not list(artifacts.root.iterdir())
    checks.append('native representation and original artifact byte/pixel bounds refuse before artifact write')
    inventory=json.loads((Path(__file__).absolute().parents[1]/'OPERATIONS.json').read_text())
    assert set(inventory['operations'])==set(SCHEMAS) and inventory['operation_count']==len(SCHEMAS)
    checks.append('OPERATIONS exact schema-name set/count, including images.get')
    result={'checks':checks,'scope':'installed synthetic planner/bounds regression using copied owned full-server synthetic receipts; no backend I/O or model execution'}
    (a.out/'result.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
