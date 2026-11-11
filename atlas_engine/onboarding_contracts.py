"""Common HTTP/MCP/CLI contracts for exact registered-file onboarding."""
from typing import Literal
from pydantic import Field
from .corpus_contracts import Strict, Ref


class Onboard(Strict):
    request_id: str = Field(min_length=1, max_length=128)
    registered_key: str = Field(min_length=1, max_length=256)
    registered_path: str = Field(min_length=1, max_length=2048)
    model_id: int = Field(gt=0)
    version_id: int = Field(gt=0)
    file_id: int = Field(gt=0)
    file_name: str = Field(min_length=1, max_length=512)
    hash_algorithm: Literal['SHA256', 'BLAKE3']
    file_hash: str = Field(pattern=r'^[a-fA-F0-9]{64}$')
    gallery_limit: int = Field(default=30, ge=10, le=30, description='Unique community quota only; author carousel is independent and unbounded')
    image_refresh: Literal['reuse', 'revalidate'] = 'reuse'


class Refresh(Ref):
    request_id: str = Field(min_length=1, max_length=128)


class FileRead(Strict):
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    offset: int = Field(default=0, ge=0)
    size: int = Field(default=32768, ge=1, le=32768)


SCHEMAS = {'models.onboard': Onboard, 'models.onboard.get': Ref,
           'models.onboard.resume': Ref, 'models.onboard.refresh': Refresh,
           'models.onboard.recover': Refresh, 'corpus.files.read': FileRead}
