"""Side-effect-free adapters from domain objects to knowledge documents."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from project_lens.context.knowledge_index.models import (
    IndexRecordStatus,
    KnowledgeDocument,
    KnowledgeObjectKind,
    content_hash,
    stable_document_id,
)
from project_lens.context.source_records import SourceRecord
from project_lens.domain.memory import Episode, MemoryObservation, ProjectMemory
from project_lens.domain.models import Evidence


class KnowledgeAdapter(Protocol):
    def adapt(self, value: Any, **kwargs: Any) -> KnowledgeDocument: ...


class EvidenceAdapter:
    def adapt(self, value: Evidence, **_: Any) -> KnowledgeDocument:
        metadata = dict(value.metadata)
        revision = _optional(metadata.get("revision") or metadata.get("version"))
        status = _status(value.revoked, metadata.get("status"))
        return _document(
            tenant_id=value.project.tenant_id,
            project_id=value.project.project_id,
            kind=KnowledgeObjectKind.EVIDENCE,
            object_id=str(value.id),
            source_system=value.source.system,
            source_id=value.source.source_id,
            revision=revision,
            title=str(metadata.get("title") or value.source.source_id),
            text=value.content,
            evidence_ids=(str(value.id),),
            status=status,
            valid_from=_optional_datetime(metadata.get("valid_from") or metadata.get("effective_from")),
            valid_to=_optional_datetime(metadata.get("valid_to") or metadata.get("effective_to")),
            observed_at=value.observed_at,
            access_scope=value.access_scope,
            metadata=metadata,
            indexable=not value.revoked,
        )


class SourceRecordAdapter:
    def adapt(self, value: SourceRecord, **_: Any) -> KnowledgeDocument:
        status = IndexRecordStatus.REVOKED.value if value.revoked else value.status
        return _document(
            tenant_id=value.tenant_id,
            project_id=value.project_id,
            kind=KnowledgeObjectKind.SOURCE_RECORD,
            object_id=value.source_id,
            source_system=value.source_type.value,
            source_id=value.source_id,
            revision=value.revision,
            title=value.title or value.source_id,
            text=value.content,
            source_record_key=":".join(value.key),
            status=status,
            valid_from=value.effective_from,
            valid_to=value.effective_to,
            observed_at=value.observed_at,
            access_scope=value.access_scope,
            metadata={
                **value.metadata,
                "authority_scope": list(value.authority_scope),
                "raw_uri": value.raw_uri,
            },
            indexable=not value.revoked and not _expired(value.effective_to),
        )


class ProjectMemoryAdapter:
    def adapt(self, value: ProjectMemory, **_: Any) -> KnowledgeDocument:
        indexable = value.status.value == IndexRecordStatus.ACTIVE.value and not _expired(value.valid_to)
        status = value.status.value
        return _document(
            tenant_id=value.project.tenant_id,
            project_id=value.project.project_id,
            kind=KnowledgeObjectKind.PROJECT_MEMORY,
            object_id=str(value.id),
            source_system="project_memory",
            source_id=str(value.id),
            revision=str(value.updated_at.isoformat()),
            title=value.fact_key or value.memory_type.value,
            text=value.text,
            evidence_ids=tuple(str(item) for item in value.evidence_ids),
            fact_key=value.fact_key,
            subject=value.subject,
            status=status,
            valid_from=value.valid_from,
            valid_to=value.valid_to,
            observed_at=value.observed_at,
            access_scope=_memory_access_scope(value),
            visibility_scope=value.visibility_scope,
            metadata={
                "memory_type": value.memory_type.value,
                "authority_scope": list(value.authority_scope),
                "approved_by": value.approved_by,
            },
            indexable=indexable,
        )


class EpisodeAdapter:
    def adapt(self, value: Episode, **_: Any) -> KnowledgeDocument:
        text = f"{value.title}\n{value.summary}".strip()
        return _document(
            tenant_id=value.project.tenant_id,
            project_id=value.project.project_id,
            kind=KnowledgeObjectKind.EPISODE,
            object_id=str(value.id),
            source_system="episode",
            source_id=str(value.run_id),
            revision=value.ended_at.isoformat(),
            title=value.title,
            text=text,
            evidence_ids=tuple(str(item) for item in value.evidence_ids),
            status=value.status,
            observed_at=value.ended_at,
            access_scope=f"project:{value.project.project_id}:read",
            metadata={
                "run_id": str(value.run_id),
                "tool_names": list(value.tool_names),
                "historical": True,
            },
            indexable=True,
        )


class MemoryObservationAdapter:
    def adapt(self, value: MemoryObservation, **_: Any) -> KnowledgeDocument:
        return _document(
            tenant_id=value.project.tenant_id,
            project_id=value.project.project_id,
            kind=KnowledgeObjectKind.MEMORY_OBSERVATION,
            object_id=str(value.id),
            source_system="memory_observation",
            source_id=str(value.run_id),
            revision=value.observed_at.isoformat(),
            title=value.kind.value,
            text=value.text,
            evidence_ids=tuple(str(item) for item in value.evidence_ids),
            status=IndexRecordStatus.ACTIVE.value,
            observed_at=value.observed_at,
            access_scope=f"project:{value.project.project_id}:read",
            metadata={"historical": True, "event_type": value.event_type},
            indexable=True,
        )


class ObsidianWikiAdapter:
    """Adapt a manifest-validated Wiki page or draft; caller owns manifest checks."""

    def adapt(self, value: Any, **kwargs: Any) -> KnowledgeDocument:
        tenant_id = str(kwargs["tenant_id"])
        project_id = str(kwargs["project_id"])
        path = str(getattr(value, "path", kwargs.get("path", "")))
        title = str(getattr(value, "title", getattr(value, "summary", None).title if getattr(value, "summary", None) else Path(path).stem))
        text = str(getattr(value, "content", getattr(value, "snippet", "")))
        page_type = str(getattr(value, "page_type", "unknown"))
        status = str(getattr(value, "status", "derived"))
        source_keys = tuple(str(item) for item in getattr(value, "source_keys", ()))
        return _document(
            tenant_id=tenant_id,
            project_id=project_id,
            kind=KnowledgeObjectKind.OBSIDIAN_WIKI,
            object_id=path,
            source_system="obsidian",
            source_id=path,
            revision=str(kwargs.get("revision") or getattr(value, "generated_at", None) or content_hash(text)),
            title=title,
            text=text,
            source_record_key=source_keys[0] if len(source_keys) == 1 else None,
            status=status,
            access_scope=str(kwargs.get("access_scope") or f"project:{project_id}:read"),
            metadata={"page_type": page_type, "source_keys": list(source_keys), "derived": True},
            indexable=bool(kwargs.get("indexable", True)),
        )


def _document(
    *,
    tenant_id: str,
    project_id: str,
    kind: KnowledgeObjectKind,
    object_id: str,
    source_system: str,
    source_id: str,
    revision: str | None,
    title: str,
    text: str,
    access_scope: str,
    status: str,
    indexable: bool,
    evidence_ids: tuple[str, ...] = (),
    source_record_key: str | None = None,
    fact_key: str | None = None,
    subject: str | None = None,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    observed_at: datetime | None = None,
    visibility_scope: tuple[str, ...] = (),
    metadata: dict[str, Any] | None = None,
) -> KnowledgeDocument:
    digest = content_hash(text)
    return KnowledgeDocument(
        id=stable_document_id(tenant_id, project_id, kind, object_id, revision),
        tenant_id=tenant_id,
        project_id=project_id,
        kind=kind,
        object_id=object_id,
        source_system=source_system,
        source_id=source_id,
        revision=revision,
        title=title,
        text=text,
        evidence_ids=evidence_ids,
        source_record_key=source_record_key,
        fact_key=fact_key,
        subject=subject,
        status=status,
        valid_from=valid_from,
        valid_to=valid_to,
        observed_at=observed_at,
        access_scope=access_scope,
        visibility_scope=visibility_scope,
        content_hash=digest,
        metadata=metadata or {},
        indexable=indexable,
    )


def _status(revoked: bool, value: object) -> str:
    return IndexRecordStatus.REVOKED.value if revoked else str(value or IndexRecordStatus.ACTIVE.value)


def _optional(value: object) -> str | None:
    return str(value) if value not in (None, "") else None


def _optional_datetime(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError("knowledge metadata contains an invalid datetime") from exc


def _expired(value: datetime | None) -> bool:
    from datetime import datetime, timezone

    if value is None:
        return False
    current = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return current <= datetime.now(timezone.utc)


def _memory_access_scope(value: ProjectMemory) -> str:
    if value.visibility_scope:
        return value.visibility_scope[0]
    return f"project:{value.project.project_id}:read"


__all__ = [
    "EpisodeAdapter",
    "EvidenceAdapter",
    "KnowledgeAdapter",
    "MemoryObservationAdapter",
    "ObsidianWikiAdapter",
    "ProjectMemoryAdapter",
    "SourceRecordAdapter",
]
