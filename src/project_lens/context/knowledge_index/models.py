"""Stable contracts for the unified knowledge index."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field

from project_lens.domain.models import FrozenModel, utc_now


class KnowledgeObjectKind(StrEnum):
    EVIDENCE = "evidence"
    SOURCE_RECORD = "source_record"
    PROJECT_MEMORY = "project_memory"
    EPISODE = "episode"
    MEMORY_OBSERVATION = "memory_observation"
    OBSIDIAN_WIKI = "obsidian_wiki"


class IndexRecordStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"
    PENDING = "pending"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class KnowledgeDocument(FrozenModel):
    """A versioned, source-linked projection before chunking."""

    id: str = Field(min_length=1, max_length=300)
    tenant_id: str = Field(min_length=1, max_length=100)
    project_id: str = Field(min_length=1, max_length=100)
    kind: KnowledgeObjectKind
    object_id: str = Field(min_length=1, max_length=500)
    source_system: str = Field(min_length=1, max_length=100)
    source_id: str = Field(min_length=1, max_length=500)
    revision: str | None = Field(default=None, max_length=200)
    title: str = Field(default="", max_length=500)
    text: str = Field(min_length=1)
    evidence_ids: tuple[str, ...] = ()
    source_record_key: str | None = None
    fact_key: str | None = None
    subject: str | None = None
    status: str = Field(default=IndexRecordStatus.ACTIVE.value, max_length=80)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    observed_at: datetime | None = None
    access_scope: str = Field(min_length=1, max_length=200)
    visibility_scope: tuple[str, ...] = ()
    content_hash: str = Field(min_length=16, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)
    indexable: bool = True


class KnowledgeChunk(FrozenModel):
    """A stable retrieval unit that can always be projected back to a document."""

    chunk_id: str = Field(min_length=16, max_length=128)
    document_id: str = Field(min_length=1, max_length=300)
    object_kind: KnowledgeObjectKind
    object_id: str = Field(min_length=1, max_length=500)
    tenant_id: str = Field(min_length=1, max_length=100)
    project_id: str = Field(min_length=1, max_length=100)
    source_id: str = Field(min_length=1, max_length=500)
    revision: str | None = Field(default=None, max_length=200)
    title_path: tuple[str, ...] = ()
    text: str = Field(min_length=1)
    content_hash: str = Field(min_length=16, max_length=128)
    chunker_version: str = Field(min_length=1, max_length=80)
    chunk_index: int = Field(ge=0)
    chunk_count: int = Field(ge=1)
    status: str = Field(default=IndexRecordStatus.ACTIVE.value, max_length=80)
    access_scope: str = Field(min_length=1, max_length=200)
    visibility_scope: tuple[str, ...] = ()
    indexable: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class EmbeddingRecord(FrozenModel):
    """A versioned embedding cache row. The vector never carries source text."""

    chunk_id: str
    tenant_id: str
    project_id: str
    model_version: str
    dimensions: int = Field(gt=0)
    content_hash: str
    chunker_version: str
    index_version: str = "v1"
    vector: tuple[float, ...]
    status: str = "ready"
    created_at: datetime = Field(default_factory=utc_now)


class KnowledgeIndexJob(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    tenant_id: str
    project_id: str
    object_kind: KnowledgeObjectKind
    object_id: str
    content_hash: str
    model_version: str
    chunker_version: str = "v1"
    index_version: str = "v1"
    status: str = "pending"
    attempt_count: int = Field(default=0, ge=0)
    last_error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_document_id(
    tenant_id: str,
    project_id: str,
    kind: KnowledgeObjectKind | str,
    object_id: str,
    revision: str | None,
) -> str:
    raw = "\x1f".join(
        (tenant_id, project_id, str(kind), object_id, revision or "")
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def stable_chunk_id(
    *,
    tenant_id: str,
    project_id: str,
    kind: KnowledgeObjectKind | str,
    object_id: str,
    revision: str | None,
    chunker_version: str,
    chunk_index: int,
    chunk_content_hash: str,
) -> str:
    raw = "\x1f".join(
        (
            tenant_id,
            project_id,
            str(kind),
            object_id,
            revision or "",
            chunker_version,
            str(chunk_index),
            chunk_content_hash,
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "EmbeddingRecord",
    "IndexRecordStatus",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "KnowledgeIndexJob",
    "KnowledgeObjectKind",
    "canonical_json",
    "content_hash",
    "stable_chunk_id",
    "stable_document_id",
]
