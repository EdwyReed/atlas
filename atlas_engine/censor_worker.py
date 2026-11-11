"""Offline CPU subordinate. No API, queue, model download or independent store.
Renderer is the unchanged accepted anime-mosaic renderer; detection uses NTD11 only.
"""
import hashlib, io, json, os, sys, threading, time
from pathlib import Path

def sha(raw): return hashlib.sha256(raw).hexdigest()

def main():
    spec=json.loads(Path(sys.argv[1]).read_text())
    # Linux owner death must not leave an inference child competing after restart.
    import ctypes,signal
    if ctypes.CDLL(None,use_errno=True).prctl(1,signal.SIGKILL,0,0,0)!=0:
        raise RuntimeError('cannot bind worker lifetime to Engine')
    if os.getppid()!=spec['parent_pid']:raise RuntimeError('Engine owner already stopped')
    for k in ('OMP_NUM_THREADS','MKL_NUM_THREADS','OPENBLAS_NUM_THREADS','NUMEXPR_NUM_THREADS'):
        os.environ[k]='2'
    os.environ.update(CUDA_VISIBLE_DEVICES='',HF_HUB_OFFLINE='1',YOLO_OFFLINE='true')
    def memory_guard():
        while True:
            time.sleep(.1)
            rss=next(int(l.split()[1])*1024 for l in Path('/proc/self/status').read_text().splitlines() if l.startswith('VmRSS:'))
            if rss>spec.get('max_rss_bytes',3*1024**3):os._exit(72)
    threading.Thread(target=memory_guard,daemon=True).start()
    # Isolated interpreter ignores managed Hermes PYTHONPATH/PYTHONHOME.
    sys.path.insert(0,spec['upstream'])
    from mosaic import apply_censor, apply_padding
    from PIL import Image,ImageOps
    import torch
    from ultralytics import YOLO
    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    if spec['action']=='health':
        result={'ok':True,'cpu_only':True,'renderer_import_ok':True,'torch':torch.__version__}
    else:
        model_sha256=sha(Path(spec['model']).read_bytes())
        model=YOLO(spec['model'])
        opts=spec['options'];labels={'pussy','penis'}
        if opts['include_nipple']:labels.add('nipples')
        if opts['include_anus']:labels.add('anus')
        if opts['include_testicles']:labels.add('testicles')
        rows=[];output_bytes=0
        for entry in spec['images']:
            row={'name':entry['name'],'input_sha256':entry['sha256']}
            try:
                raw=Path(entry['path']).read_bytes()
                if sha(raw)!=entry['sha256']:raise ValueError('input snapshot changed')
                with Image.open(io.BytesIO(raw)) as im:
                    if im.width*im.height>24*1024*1024 or getattr(im,'n_frames',1)!=1:raise ValueError('image resource limit')
                    image=ImageOps.exif_transpose(im).convert('RGB');image.load()
                predictions=model.predict(source=image,conf=opts['confidence'],iou=.7,verbose=False,device='cpu')
                detections=[]
                for prediction in predictions:
                    for box in prediction.boxes:
                        label=model.names[int(box.cls.item())]
                        if label in labels:
                            bounds=[int(round(v)) for v in box.xyxy[0].tolist()]
                            detections.append({'box':bounds,'label':label,'confidence':float(box.conf.item())})
                detections.sort(key=lambda d:(d['box'],d['label'],d['confidence']))
                areas=[apply_padding(d['box'],opts['padding'],*image.size) for d in detections]
                target=Path(spec['output_dir'])/entry['name']
                if not areas:out=raw
                else:
                    rendered=apply_censor(image,areas,opts['mode'],opts['strength'] or {'mosaic':20,'blur':8,'black':0}[opts['mode']])
                    buffer=io.BytesIO();extension=target.suffix.lower()
                    rendered.save(buffer,format={'.jpg':'JPEG','.jpeg':'JPEG','.png':'PNG','.webp':'WEBP'}[extension],**({'quality':95,'subsampling':0} if extension in ('.jpg','.jpeg') else {}))
                    out=buffer.getvalue()
                if len(out)>32*1024*1024 or output_bytes+len(out)>256*1024*1024:raise ValueError('output byte budget exceeded')
                target.write_bytes(out);output_bytes+=len(out)
                row.update(status='censored' if areas else 'undetected',detections=detections,output_sha256=sha(out),size=len(out),width=image.width,height=image.height)
            except Exception as e:
                row.update(status='error',error={'code':'IMAGE_PROCESS','message':type(e).__name__+': '+str(e)[:200]})
            rows.append(row)
        counts={s:sum(r['status']==s for r in rows) for s in ('censored','undetected','error')}
        if sha(Path(spec['model']).read_bytes())!=model_sha256:raise RuntimeError('model bytes changed during inference')
        result={'ok':counts['error']==0,'detector':'ntd11','device':'cpu','model_sha256':model_sha256,'renderer_sha256':sha((Path(spec['upstream'])/'mosaic.py').read_bytes()),'inference_images':len(rows),'items':rows,'counts':counts,'options':opts}
    Path(spec['result_file']).write_text(json.dumps(result,sort_keys=True,allow_nan=False)+'\n')

if __name__=='__main__':main()
