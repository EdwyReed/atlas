"""One operation schema registry used by both transports."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
class Empty(Strict): pass
class LegacyRequest(Strict):
    namespace: str = Field(min_length=1,max_length=256)
    idempotency_key: str = Field(min_length=1,max_length=2048)
    input: dict
class Binding(Strict):
    character: str = Field(min_length=1,max_length=200)
    tags: list[str] = Field(default_factory=list,max_length=24)
class Creative(Strict):
    reference_ids: list[str] = Field(default_factory=list,max_length=16)
    strategy: Literal['weighted_neighbors','beam'] = 'beam'
    prompt: str = Field(default='',max_length=2000)
    tags: list[str] = Field(default_factory=list,max_length=32)
    locked_tags: list[str] = Field(default_factory=list,max_length=32)
    characters: list[str] = Field(default_factory=list,max_length=8)
    fandoms: list[str] = Field(default_factory=list,max_length=8)
    bindings: list[Binding] = Field(default_factory=list,max_length=8)
    model: str | None = Field(default=None,max_length=200)
    seed: int = Field(default=0,ge=0,le=2147483647)
    # Legacy request identity only. Never read by numeric search or classification.
    explicitness: float = Field(default=0.0,ge=0,le=1)
    budget: int = Field(default=10,ge=1,le=24)
class Dream(Creative):
    project: str | None = Field(default=None,min_length=1,max_length=128)
    request_id: str = Field(min_length=1,max_length=128)
    count: int = Field(default=4,ge=1,le=8)
    trajectories: int = Field(default=16,ge=4,le=64)
class Ref(Strict): job_id: str = Field(min_length=1,max_length=128)
class WaitRef(Ref): timeout_seconds: int = Field(default=10,ge=0,le=30)
class JobEvents(Ref):
    after: int = Field(default=0,ge=0)
    limit: int = Field(default=50,ge=1,le=100)
    timeout_seconds: int = Field(default=0,ge=0,le=30)
class RetryRef(Ref): request_id: str = Field(min_length=1,max_length=128)
class ListQuery(Strict):
    limit: int = Field(default=20,ge=1,le=100)
    after: str = Field(default='',max_length=128)
class Search(Strict):
    mode: Literal['literal','semantic','hybrid'] = 'literal'
    query: str = Field(min_length=1,max_length=200)
    limit: int = Field(default=10,ge=1,le=30)
class Evidence(Strict):
    kind: Literal['tag','resource','character','fandom','reference','research','feedback'] = 'tag'
    key: str = Field(min_length=1,max_length=200)
class KnowledgeSearch(Strict):
    query: str = Field(min_length=1,max_length=200)
    kind: Literal['reference','research','character','fandom','resource','feedback'] | None = None
    after: str = Field(default='',max_length=512)
    limit: int = Field(default=20,ge=1,le=100)
class KnowledgeGet(Strict):
    entity_id: str = Field(min_length=1,max_length=512)
class LoRA(Strict):
    model: str = Field(min_length=1,max_length=200)
    weight: float = Field(default=1.0,ge=-2,le=2)
class Resolve(Strict):
    model: str = Field(min_length=1,max_length=200)
    loras: list[LoRA] = Field(default_factory=list,max_length=16)
class Plan(Resolve):
    project: str | None = Field(default=None,min_length=1,max_length=128)
    prompt: str = Field(default='',max_length=10000)
    mode: Literal['txt2img','inpaint','upscale'] = 'txt2img'
    board_id: str | None = Field(default=None,min_length=1,max_length=200)
    image_name: str | None = Field(default=None,min_length=1,max_length=200)
    mask_name: str | None = Field(default=None,min_length=1,max_length=200)
    strength: float = Field(default=0.75,gt=0,le=1)
    scale: float = Field(default=2.0,ge=1,le=4)
    upscale_engine: Literal['spandrel','esrgan'] = 'spandrel'
    negative_prompt: str = Field(default='',max_length=10000)
    width: int = Field(default=512,ge=64,le=1536,multiple_of=8)
    height: int = Field(default=512,ge=64,le=1536,multiple_of=8)
    steps: int = Field(default=20,ge=1,le=100)
    cfg: float = Field(default=7.0,ge=0,le=30)
    seed: int = Field(default=0,ge=0,le=2147483647)
    scheduler: Literal['euler','euler_a','dpmpp_2m','dpmpp_2m_sde','ddim'] = 'euler'
class Submit(Plan): request_id: str = Field(min_length=1,max_length=128)
class GraphSubmit(Strict):
    connection_id: str = Field(min_length=1,max_length=128)
    backend_generation: str = Field(min_length=1,max_length=128)
    request_id: str = Field(min_length=1,max_length=128)
    graph: dict
    runs: int = Field(default=1,ge=1,le=4)
class BatchSubmit(Submit): runs: int = Field(default=2,ge=1,le=4)
class WorkflowRef(Strict):
    connection_id: str = Field(min_length=1,max_length=128)
    backend_generation: str = Field(min_length=1,max_length=128)
    workflow_id: str = Field(min_length=1,max_length=200)
class WorkflowRun(WorkflowRef):
    request_id: str = Field(min_length=1,max_length=128)
    version_semantics: Literal['latest_at_execution']
    workflow_inputs: dict[str, int] = Field(max_length=32)
    return_key: str = Field(min_length=1,max_length=128)
class Feedback(Strict):
    request_id: str = Field(min_length=1,max_length=128)
    target_id: str = Field(min_length=1,max_length=128)
    reaction: Literal['keep','reject','revise']
    note: str = Field(default='',max_length=4000)
class ModelQuery(Strict):
    query: str = Field(default='',max_length=200)
    type: str | None = Field(default=None,max_length=80)
class NativeRef(Strict): key: str = Field(min_length=1,max_length=200)
class ImageRef(NativeRef): project: str | None = Field(default=None,min_length=1,max_length=128)
class Images(Strict):
    limit: int = Field(default=20,ge=1,le=100)
    offset: int = Field(default=0,ge=0,le=100000)
class ArtifactFetch(Ref): image_name: str = Field(min_length=1,max_length=200)
class ArtifactRead(Strict):
    artifact_id: str = Field(min_length=1,max_length=128)
    offset: int = Field(default=0,ge=0)
    size: int = Field(default=32768,ge=1,le=32768)
class ProjectionGet(Strict): point_id: str = Field(min_length=1,max_length=128)
class ProjectionPage(Strict):
    offset: str | None = Field(default=None,max_length=128)
    limit: int = Field(default=20,ge=1,le=100)
class ReviewRecord(Strict):
    artifact_id: str = Field(min_length=1,max_length=128)
    request_id: str = Field(min_length=1,max_length=128)
    sha256: str = Field(pattern='^[a-f0-9]{64}$')
    usable: bool
    reason: str = Field(min_length=1,max_length=4000)
    reviewer_model: str = Field(min_length=1,max_length=200)
class ReviewPrepare(Strict): artifact_id: str = Field(min_length=1,max_length=128)
class UploadBegin(Strict):
    request_id: str = Field(min_length=1,max_length=128)
    project: str | None = Field(default=None,min_length=1,max_length=128)
    size: int = Field(ge=1,le=32*1024*1024)
    sha256: str = Field(pattern='^[a-f0-9]{64}$')
    role: Literal['image','mask'] = 'image'
class UploadRef(Strict): upload_id: str = Field(min_length=1,max_length=128)
class UploadChunk(UploadRef):
    offset: int = Field(ge=0,le=32*1024*1024)
    base64: str = Field(min_length=1,max_length=43692)
class UploadImport(UploadRef): family: Literal['sd-1','anima','pixel']
# Native administration is typed and separately gated from GPU submission.
class BoardRef(Strict): board_id: str = Field(min_length=1,max_length=200)
class BoardDate(Strict): date: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
class Mutation(Strict): request_id: str = Field(min_length=1,max_length=128)
class BoardCreate(Mutation): name: str = Field(min_length=1,max_length=300)
class BoardUpdate(BoardRef,Mutation):
    project: str | None = Field(default=None,min_length=1,max_length=128)
    expected_revision: str = Field(pattern='^[a-f0-9]{64}$')
    name: str | None = Field(default=None,min_length=1,max_length=300)
    archived: bool | None = None
    cover_image_name: str | None = Field(default=None,min_length=1,max_length=200)
    clear_cover: bool = False
    visibility: Literal['private','shared','public'] | None = None
class BoardDelete(BoardRef,Mutation):
    expected_revision: str = Field(pattern='^[a-f0-9]{64}$')
    include_images: bool = False
class BoardImagesChange(Mutation):
    project: str | None = Field(default=None,min_length=1,max_length=128)
    board_id: str | None = Field(default=None,min_length=1,max_length=200)
    action: Literal['add','move','remove']
    image_names: list[str] = Field(min_length=1,max_length=100)
class ModelJob(Strict): id: int = Field(ge=0)
class ModelJobChange(ModelJob,Mutation): action: Literal['pause','resume','retry','cancel']
class ModelSource(Strict): source_id: str = Field(min_length=1,max_length=128)
class ModelInstall(ModelSource,Mutation): pass
class LoRAInstall(Mutation):
    url: str = Field(min_length=1,max_length=2000)
    file_id: str | None = Field(default=None,pattern='^[0-9]+$')
class RecordCreate(Mutation):
    project: str | None = Field(default=None,min_length=1,max_length=128)
    body: dict
class RecordRef(Strict): record_id: str = Field(min_length=1,max_length=128)
class RecordChange(RecordRef,Mutation): expected_revision: int = Field(ge=1)
class RecordUpdate(RecordChange): body: dict
class RecordHistory(RecordRef):
    after_revision: int = Field(default=0,ge=0)
    limit: int = Field(default=20,ge=1,le=100)
SCHEMAS = {
    'legacy.request':LegacyRequest,
    'models.lora.install':LoRAInstall,
    'loras.create':RecordCreate,'loras.get':RecordRef,'loras.list':ListQuery,'loras.update':RecordUpdate,'loras.delete':RecordChange,'loras.history':RecordHistory,
    'generation.records.create':RecordCreate,'generation.records.get':RecordRef,'generation.records.list':ListQuery,'generation.records.update':RecordUpdate,'generation.records.delete':RecordChange,'generation.records.history':RecordHistory,
    'jobs.events':JobEvents,
    'admin.get':Ref,
    'boards.get':BoardRef,'boards.images':BoardRef,'boards.dates':Empty,'boards.date':BoardDate,
    'boards.create':BoardCreate,'boards.update':BoardUpdate,'boards.delete':BoardDelete,'boards.images.update':BoardImagesChange,
    'models.install':ModelInstall,'models.jobs':Empty,'models.job':ModelJob,'models.job.update':ModelJobChange,
    'models.scan':ModelSource,'models.starters':Empty,'models.missing':Empty,'models.hf.status':Empty,
    'knowledge.search':KnowledgeSearch,'knowledge.get':KnowledgeGet,
    'projection.get':ProjectionGet,'projection.page':ProjectionPage,
    'review.prepare':ReviewPrepare,'review.record':ReviewRecord,'outbox.flush':Empty,
    'upload.begin':UploadBegin,'upload.chunk':UploadChunk,'upload.finalize':UploadRef,'upload.get':UploadRef,'upload.abort':UploadRef,'upload.import':UploadImport,
    'artifact.fetch':ArtifactFetch,'artifact.read':ArtifactRead,'artifact.list':Ref,
    'status':Empty,'capabilities':Empty,'enrich':Creative,
    'dream.submit':Dream,'dream.get':Ref,'dream.cancel':Ref,
    'batch.submit':BatchSubmit,'batch.get':Ref,
    'generation.plan':Plan,'generation.submit':Submit,'generation.submitGraph':GraphSubmit,'generation.get':Ref,'generation.wait':WaitRef,'generation.retry':RetryRef,'generation.cancel':Ref,
    'jobs.list':ListQuery,'jobs.page':ListQuery,'feedback.record':Feedback,'feedback.list':Ref,
    'search':Search,'evidence':Evidence,'catalog.list':ModelQuery,'catalog.resolve':Resolve,
    'catalog.get':NativeRef,'queue.status':Empty,'observer.reconcile':Empty,
    'images.list':Images,'image.metadata':ImageRef,'boards.list':Empty,
    'workflows.list':Empty,'workflows.get':NativeRef,'workflows.schema':WorkflowRef,'workflows.run':WorkflowRun,
}
class Fault(Exception):
    def __init__(self,code,message): self.code=code;super().__init__(message)

from .corpus_contracts import SCHEMAS as CORPUS_SCHEMAS
SCHEMAS.update(CORPUS_SCHEMAS)
from .onboarding_contracts import SCHEMAS as ONBOARDING_SCHEMAS
from .acquisition_contracts import SCHEMAS as ACQUISITION_SCHEMAS
SCHEMAS.update(ACQUISITION_SCHEMAS)
SCHEMAS.update(ONBOARDING_SCHEMAS)
from .media_contracts import OPERATIONS as MEDIA_SCHEMAS
SCHEMAS.update(MEDIA_SCHEMAS)
from .workspace_contracts import SCHEMAS as WORKSPACE_SCHEMAS
SCHEMAS.update(WORKSPACE_SCHEMAS)
