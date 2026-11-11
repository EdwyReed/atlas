"""URL intake schemas without runtime imports (safe in either import order)."""
from pydantic import BaseModel, ConfigDict, Field
class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
class Enqueue(Strict):
    request_id: str = Field(min_length=1, max_length=128)
    urls: list[str] = Field(min_length=1, max_length=32)
class Select(Strict):
    job_id: str = Field(min_length=1, max_length=128)
    file_id: str = Field(pattern=r'^[0-9]+$')
class Page(Strict):
    after: str = Field(default='', max_length=128)
    limit: int = Field(default=100, ge=1, le=100)
class Resume(Strict):
    job_id: str = Field(min_length=1, max_length=128)
class CheckpointInstall(Strict):
    request_id: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1, max_length=2000)
    file_id: str = Field(pattern=r'^[0-9]+$')
    expected_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
SCHEMAS = {'models.acquire.enqueue':Enqueue,'models.acquire.select':Select,'models.acquire.page':Page,
           'models.acquire.resume':Resume,'models.checkpoint.install':CheckpointInstall}
