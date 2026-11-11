"""Explicit scoped native workspace subset, shared by every transport."""
from pydantic import Field
from .contracts import Strict


class ProjectRef(Strict):
    connection_id: str = Field(min_length=1, max_length=128)
    backend_generation: str = Field(min_length=1, max_length=256)
    project_id: str = Field(min_length=1, max_length=255)


class SavePrompt(ProjectRef):
    request_id: str = Field(min_length=1, max_length=128)
    expected_workspace_revision: int = Field(ge=1)
    widget_instance_id: str = Field(min_length=1, max_length=255)
    prompt: str = Field(max_length=10000)


SCHEMAS = {'projects.get': ProjectRef, 'projects.savePrompt': SavePrompt}
