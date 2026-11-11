"""Optional media contracts; HTTP/MCP never accept host filesystem paths."""
from typing import Literal
from pydantic import Field, field_validator
import math
from .contracts import Strict, Empty, Ref, UploadRef, UploadBegin, UploadChunk

class MediaUploadBegin(UploadBegin):
    size: int = Field(ge=1,le=64*1024*1024)
class MediaUploadChunk(UploadChunk):
    offset: int = Field(ge=0,le=64*1024*1024)
class MediaImage(Strict):
    upload_id: str = Field(min_length=1,max_length=128)
    name: str = Field(min_length=1,max_length=160,pattern=r'^[^/\\]+\.(?i:png|jpg|jpeg|webp)$')
class CensorProcess(Strict):
    request_id: str = Field(min_length=1,max_length=128)
    images: list[MediaImage] = Field(min_length=1,max_length=200)
    mode: Literal['mosaic','blur','black'] = 'mosaic'
    strength: int | None = Field(default=None,ge=1,le=200)
    confidence: float = Field(default=.25,ge=0,le=1,allow_inf_nan=False)
    padding: int = Field(default=10,ge=0,le=1000)
    include_nipple: bool = False
    include_anus: bool = False
    include_testicles: bool = False
    preview: int | None = Field(default=None,ge=1,le=100)
class MaskSetup(Strict):
    upload_id: str = Field(min_length=1,max_length=128)
class MaskCache(Strict):
    cache_id: str = Field(min_length=1,max_length=128)
class MaskPoint(Strict):
    x: float = Field(allow_inf_nan=False)
    y: float = Field(allow_inf_nan=False)
    label: Literal[0,1]
    @field_validator('label',mode='before')
    @classmethod
    def label_integer(cls,value):
        if type(value) is not int:raise ValueError('label must be an integer')
        return value
class MaskPrior(Strict):
    job_id: str | None = Field(default=None,min_length=1,max_length=128)
    manifest_upload_id: str | None = Field(default=None,min_length=1,max_length=128)
    logits_upload_id: str | None = Field(default=None,min_length=1,max_length=128)
    candidate: int = Field(ge=0,le=2)
class MaskSegment(MaskCache):
    request_id: str = Field(min_length=1,max_length=128)
    points: list[MaskPoint] = Field(default_factory=list,max_length=256)
    box: list[float] | None = Field(default=None,min_length=4,max_length=4)
    prior: MaskPrior | None = None
    multimask: bool = True
    annotate_prompts: bool = True
    @field_validator('box')
    @classmethod
    def finite_box(cls,value):
        if value is not None and not all(math.isfinite(x) for x in value):raise ValueError('box requires finite coordinates')
        return value

OPERATIONS = {'censor.health':Empty,'censor.process':CensorProcess,'censor.get':Ref,
              'mask.health':Empty,'mask.setup':MaskSetup,'mask.segment':MaskSegment,'mask.get':Ref,
              'mask.release':MaskCache,'mask.reset':Empty,'media.upload.finalize':UploadRef,'media.upload.begin':MediaUploadBegin,'media.upload.chunk':MediaUploadChunk}
