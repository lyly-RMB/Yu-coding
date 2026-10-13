"""Contracts passed between workflow agents."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from project_lens.domain.models import ProjectRef


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProjectRegistration(FrozenModel):
    project: ProjectRef
    access_scope: str = Field(min_length=1, max_length=200)


class ResolvedProject(FrozenModel):
    project: ProjectRef
    access_scope: str = Field(min_length=1, max_length=200)
    resolution: str
