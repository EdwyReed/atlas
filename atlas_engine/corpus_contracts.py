"""Canonical corpus transport schemas; source JSON is opaque, not a policy input."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, JsonValue

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

class Mutation(Strict):
    request_id: str = Field(min_length=1, max_length=128)

class Ref(Strict):
    job_id: str = Field(min_length=1, max_length=128)

Collection = Literal['tags', 'loras', 'checkpoints', 'generations', 'tag_relations']

class Occurrences(Strict):
    positive: list[str] = Field(default_factory=list, max_length=64)
    negative: list[str] = Field(default_factory=list, max_length=64)

class CorpusRef(Strict):
    collection: Collection
    identity: dict[str, JsonValue]

class CorpusBody(CorpusRef):
    body: dict[str, JsonValue]
    attribution: dict[str, JsonValue] = Field(default_factory=dict)
    occurrences: Occurrences | None = None

class CorpusEntry(CorpusBody):
    expected_revision: int | None = Field(default=None, ge=0)

class CorpusPut(CorpusBody, Mutation):
    expected_revision: int = Field(ge=0)

class CorpusDelete(CorpusRef, Mutation):
    expected_revision: int = Field(ge=1)

class CorpusRestore(CorpusDelete):
    restore_revision: int = Field(ge=1)

class CorpusHistory(CorpusRef):
    after_revision: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)

class CorpusPage(Strict):
    collection: Collection
    after: str = ''
    limit: int = Field(default=20, ge=1, le=100)
    include_deleted: bool = False

class CorpusImport(Mutation):
    entries: list[CorpusEntry] = Field(min_length=1, max_length=100)

SCHEMAS = {
    'corpus.put': CorpusPut, 'corpus.delete': CorpusDelete,
    'corpus.restore': CorpusRestore, 'corpus.get': CorpusRef,
    'corpus.history': CorpusHistory, 'corpus.page': CorpusPage,
    'corpus.import': CorpusImport, 'corpus.import.get': Ref,
    'corpus.import.resume': Ref,
}
