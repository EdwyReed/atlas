"""Optional genuine joint image/text encoding from pre-approved local CLIP weights.
Never downloads weights, never enabled implicitly. No claim of anime relevance without an evaluation.
"""
from dataclasses import dataclass
import hashlib,math
from pathlib import Path
from PIL import Image
from .contracts import Fault
@dataclass(frozen=True)
class Space:
    name: str
    model_revision: str
    dimensions: int
    modality: str = 'joint_text_image'
class LocalCLIP:
    def __init__(self,weights:Path,revision:str):
        if not weights.is_dir() or not revision:raise Fault('EMBEDDING_CONFIG','local weights and immutable revision required')
        try:
            import torch
            from transformers import CLIPModel,CLIPProcessor
        except ImportError:raise Fault('EMBEDDING_DISABLED','optional torch/transformers runtime not installed')
        self.torch=torch
        self.model=CLIPModel.from_pretrained(str(weights),local_files_only=True).eval().to('cpu')
        self.processor=CLIPProcessor.from_pretrained(str(weights),local_files_only=True)
        self.space=Space('clip_vit_b32_'+revision,revision,int(self.model.config.projection_dim))
    def normalized(self,tensor):
        # Current Transformers may wrap pooler output; reject unexpected dimensions, never return zero vectors.
        if hasattr(tensor,'pooler_output'):tensor=tensor.pooler_output
        values=tensor[0].detach().cpu().tolist()
        if len(values)!=self.space.dimensions or not all(math.isfinite(x) for x in values):raise Fault('EMBEDDING_INVALID','invalid model vector')
        norm=math.sqrt(sum(x*x for x in values))
        if norm==0:raise Fault('EMBEDDING_INVALID','zero model vector')
        return [x/norm for x in values]
    def image(self,path):
        path=Path(path)
        if path.stat().st_size>32*1024*1024:raise Fault('IMAGE_LIMIT','image too large')
        with Image.open(path) as im:
            if im.width*im.height>24*1024*1024:raise Fault('IMAGE_LIMIT','too many pixels')
            inputs=self.processor(images=im.convert('RGB'),return_tensors='pt')
        with self.torch.inference_mode():v=self.normalized(self.model.get_image_features(**inputs))
        return {'space':self.space.__dict__,'vector':v,'artifact_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    def text(self,text):
        inputs=self.processor(text=[text],return_tensors='pt',padding=True,truncation=True)
        with self.torch.inference_mode():return {'space':self.space.__dict__,'vector':self.normalized(self.model.get_text_features(**inputs))}
