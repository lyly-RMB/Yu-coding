"""Freeze/replay contracts for authorized project-memory context.

Candidates deliberately contain references only.  Memory text is resolved at
freeze time, after the actor, project, validity and provenance checks run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Callable, Iterable, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from project_lens.context.memory_authorization import authorize_memory_detail
from project_lens.context.memory_store import MemoryStore
from project_lens.context.source_store import SourceRecordStore
from project_lens.domain.memory import MemorySourceRef, MemoryStatus, ProjectMemory
from project_lens.domain.models import Evidence, ProjectRef
from project_lens.project_space.policies import EffectiveAccessScope

RENDERER_VERSION = "context_snapshot.v1"


class SnapshotModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class MemoryCandidate(SnapshotModel):
    """Reference-only candidate emitted by retrieval."""

    kind: str = "project_memory"
    source_id: UUID
    source_hash: str | None = None
    fact_key: str | None = None
    valid_from: datetime
    valid_to: datetime | None = None
    status_at_selection: MemoryStatus

    @property
    def memory_id(self) -> UUID:
        return self.source_id


class SnapshotMemoryRecord(SnapshotModel):
    memory_id: UUID
    fact_key: str | None = None
    content_hash: str
    source_refs: tuple[MemorySourceRef, ...] = ()
    valid_from: datetime
    valid_to: datetime | None = None
    authorization_result: str
    renderer_version: str = RENDERER_VERSION
    state: str = "included"
    reason_code: str | None = None
    text: str | None = None


class ContextSnapshot(SnapshotModel):
    snapshot_id: UUID = Field(default_factory=uuid4)
    project: ProjectRef
    actor_id: str
    chat_id: str
    as_of: datetime
    records: tuple[SnapshotMemoryRecord, ...] = ()
    renderer_version: str = RENDERER_VERSION
    context_hash: str = Field(min_length=16, max_length=128)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def included_memory_ids(self) -> tuple[UUID, ...]:
        return tuple(item.memory_id for item in self.records if item.state == "included")


class ContextChangedError(RuntimeError):
    code = "CONTEXT_CHANGED"

    def __init__(
        self,
        memory_id: UUID,
        expected: str | None,
        actual: str | None,
        *,
        snapshot: ContextSnapshot | None = None,
    ) -> None:
        self.memory_id = memory_id
        self.expected = expected
        self.actual = actual
        self.snapshot = snapshot
        super().__init__(f"CONTEXT_CHANGED: memory {memory_id} content hash changed")


class ContextFreezeError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        memory_id: UUID | None = None,
        snapshot: ContextSnapshot | None = None,
    ) -> None:
        self.code = code
        self.memory_id = memory_id
        self.snapshot = snapshot
        super().__init__(f"{code}: {message}")


class ContextSnapshotStore(Protocol):
    def put(self, snapshot: ContextSnapshot) -> None: ...
    def get(self, snapshot_id: UUID) -> ContextSnapshot | None: ...


class InMemoryContextSnapshotStore:
    def __init__(self) -> None:
        self._snapshots: dict[UUID, ContextSnapshot] = {}

    def put(self, snapshot: ContextSnapshot) -> None:
        self._snapshots[snapshot.snapshot_id] = snapshot

    def get(self, snapshot_id: UUID) -> ContextSnapshot | None:
        return self._snapshots.get(snapshot_id)


@dataclass(frozen=True)
class FrozenContext:
    snapshot: ContextSnapshot
    memories: tuple[ProjectMemory, ...]


class ContextSnapshotService:
    def __init__(
        self,
        *,
        memory_store: MemoryStore,
        snapshot_store: ContextSnapshotStore | None = None,
        evidence_lookup: Callable[[UUID], Evidence | None] | None = None,
        source_store: SourceRecordStore | None = None,
    ) -> None:
        self.memory_store = memory_store
        self.snapshot_store = snapshot_store or InMemoryContextSnapshotStore()
        self.evidence_lookup = evidence_lookup
        self.source_store = source_store

    def freeze_and_start(
        self,
        *,
        project: ProjectRef,
        scope: EffectiveAccessScope,
        candidates: Iterable[MemoryCandidate],
        as_of: datetime | None = None,
        max_memories: int = 40,
        max_chars: int = 20_000,
    ) -> FrozenContext:
        moment = as_of or datetime.now(timezone.utc)
        if scope.project != project:
            raise ContextFreezeError("MEMORY_FORBIDDEN", "scope project does not match snapshot project")
        candidate_list = tuple(candidates)
        if len(candidate_list) > max_memories:
            raise ContextFreezeError("MEMORY_BUDGET_EXCEEDED", "memory count exceeds snapshot budget")

        records: list[SnapshotMemoryRecord] = []
        included: list[ProjectMemory] = []
        total_chars = 0
        for candidate in candidate_list:
            memory = self.memory_store.get_memory(
                candidate.memory_id, project=project, include_inactive=True, as_of=moment
            )
            if memory is None:
                self._reject(
                    project=project,
                    scope=scope,
                    as_of=moment,
                    records=records,
                    candidate=candidate,
                    code="MEMORY_NOT_FOUND",
                    message="memory is not available",
                )
            try:
                authorize_memory_detail(memory, project=project, access_scope=scope, as_of=moment)
            except PermissionError as exc:
                self._reject(
                    project=project,
                    scope=scope,
                    as_of=moment,
                    records=records,
                    candidate=candidate,
                    memory=memory,
                    code="MEMORY_FORBIDDEN",
                    message=str(exc),
                )
            if memory.status is not MemoryStatus.ACTIVE:
                self._reject(
                    project=project,
                    scope=scope,
                    as_of=moment,
                    records=records,
                    candidate=candidate,
                    memory=memory,
                    code="MEMORY_REVOKED",
                    message="memory is not active",
                )
            if candidate.status_at_selection is not memory.status:
                self._reject(
                    project=project,
                    scope=scope,
                    as_of=moment,
                    records=records,
                    candidate=candidate,
                    memory=memory,
                    code="CONTEXT_CHANGED",
                    message="memory status changed",
                    state="changed",
                )
            actual_hash = memory.content_hash or sha256(memory.text.encode("utf-8")).hexdigest()
            if candidate.source_hash and candidate.source_hash != actual_hash:
                snapshot = self._failure_snapshot(
                    project=project,
                    scope=scope,
                    as_of=moment,
                    records=[
                        *records,
                        self._record(
                            candidate,
                            memory=memory,
                            content_hash=actual_hash,
                            state="changed",
                            authorization_result="authorized",
                            reason_code="CONTEXT_CHANGED",
                        ),
                    ],
                )
                raise ContextChangedError(
                    memory.id, candidate.source_hash, actual_hash, snapshot=snapshot
                )
            self._check_evidence(memory, project=project, scope=scope)
            self._check_source_refs(memory, project=project, scope=scope)
            if total_chars + len(memory.text) > max_chars:
                raise ContextFreezeError("MEMORY_BUDGET_EXCEEDED", "memory text exceeds snapshot budget", memory_id=memory.id)
            total_chars += len(memory.text)
            included.append(memory)
            records.append(SnapshotMemoryRecord(
                memory_id=memory.id,
                fact_key=memory.fact_key,
                content_hash=actual_hash,
                source_refs=memory.source_refs,
                valid_from=memory.valid_from,
                valid_to=memory.valid_to,
                authorization_result="authorized",
                text=memory.text,
            ))
        context_hash = _context_hash(records)
        snapshot = ContextSnapshot(
            project=project,
            actor_id=scope.actor_id,
            chat_id=scope.chat_id,
            as_of=moment,
            records=tuple(records),
            context_hash=context_hash,
        )
        self.snapshot_store.put(snapshot)
        return FrozenContext(snapshot=snapshot, memories=tuple(included))

    def replay(self, snapshot_id: UUID, *, project: ProjectRef, scope: EffectiveAccessScope) -> FrozenContext:
        snapshot = self.snapshot_store.get(snapshot_id)
        if snapshot is None:
            raise ContextFreezeError("MEMORY_NOT_FOUND", "context snapshot does not exist")
        if snapshot.project != project or scope.project != project or scope.actor_id != snapshot.actor_id or scope.chat_id != snapshot.chat_id:
            raise ContextFreezeError("MEMORY_FORBIDDEN", "snapshot is not bound to this project or actor/chat")
        memories: list[ProjectMemory] = []
        for record in snapshot.records:
            if record.state != "included":
                continue
            memory = self.memory_store.get_memory(record.memory_id, project=project, include_inactive=True, as_of=snapshot.as_of)
            if memory is None:
                raise ContextFreezeError("MEMORY_NOT_FOUND", "snapshot memory is unavailable", memory_id=record.memory_id)
            try:
                authorize_memory_detail(memory, project=project, access_scope=scope, as_of=snapshot.as_of)
            except PermissionError as exc:
                raise ContextFreezeError("MEMORY_FORBIDDEN", str(exc), memory_id=memory.id) from exc
            self._check_evidence(memory, project=project, scope=scope)
            self._check_source_refs(memory, project=project, scope=scope)
            actual_hash = memory.content_hash or sha256(memory.text.encode("utf-8")).hexdigest()
            if actual_hash != record.content_hash:
                raise ContextChangedError(memory.id, record.content_hash, actual_hash)
            memories.append(memory)
        return FrozenContext(snapshot=snapshot, memories=tuple(memories))

    def _check_evidence(self, memory: ProjectMemory, *, project: ProjectRef, scope: EffectiveAccessScope) -> None:
        if not memory.evidence_ids or self.evidence_lookup is None:
            return
        for evidence_id in memory.evidence_ids:
            evidence = self.evidence_lookup(evidence_id)
            if evidence is None or evidence.revoked:
                raise ContextFreezeError("MEMORY_INVALID_PROVENANCE", "memory evidence is unavailable", memory_id=memory.id)
            if evidence.project != project:
                raise ContextFreezeError("MEMORY_INVALID_PROVENANCE", "memory evidence is outside project", memory_id=memory.id)
            if evidence.source.source_id in scope.forbidden_sources or (scope.readable_sources and evidence.source.source_id not in scope.readable_sources):
                raise ContextFreezeError("MEMORY_FORBIDDEN", "memory evidence is not readable", memory_id=memory.id)

    def _check_source_refs(
        self,
        memory: ProjectMemory,
        *,
        project: ProjectRef,
        scope: EffectiveAccessScope,
    ) -> None:
        if not memory.source_refs or self.source_store is None:
            return
        for ref in memory.source_refs:
            source = self.source_store.get(
                project.tenant_id,
                project.project_id,
                ref.source_id,
                ref.revision,
            )
            if source is None or source.revoked:
                raise ContextFreezeError(
                    "MEMORY_INVALID_PROVENANCE",
                    "memory source reference is unavailable",
                    memory_id=memory.id,
                )
            if ref.content_hash and source.content_hash != ref.content_hash:
                raise ContextFreezeError(
                    "MEMORY_INVALID_PROVENANCE",
                    "memory source reference hash changed",
                    memory_id=memory.id,
                )
            if (
                source.source_id in scope.forbidden_sources
                or source.access_scope in scope.forbidden_sources
                or (
                    scope.readable_sources
                    and not any(
                        source.source_id == allowed
                        or source.source_id.startswith(allowed.rstrip("/"))
                        or source.access_scope == allowed
                        for allowed in scope.readable_sources
                    )
                )
            ):
                raise ContextFreezeError(
                    "MEMORY_FORBIDDEN",
                    "memory source reference is not readable",
                    memory_id=memory.id,
                )

    def _reject(
        self,
        *,
        project: ProjectRef,
        scope: EffectiveAccessScope,
        as_of: datetime,
        records: list[SnapshotMemoryRecord],
        candidate: MemoryCandidate,
        code: str,
        message: str,
        memory: ProjectMemory | None = None,
        state: str = "rejected",
    ) -> None:
        snapshot = self._failure_snapshot(
            project=project,
            scope=scope,
            as_of=as_of,
            records=[
                *records,
                self._record(
                    candidate,
                    memory=memory,
                    content_hash=(
                        memory.content_hash
                        or sha256(memory.text.encode("utf-8")).hexdigest()
                        if memory is not None
                        else candidate.source_hash or "0" * 64
                    ),
                    state=state,
                    authorization_result=(
                        "denied" if code == "MEMORY_FORBIDDEN" else "not_authorized"
                    ),
                    reason_code=code,
                ),
            ],
        )
        raise ContextFreezeError(
            code, message, memory_id=candidate.memory_id, snapshot=snapshot
        )

    def _failure_snapshot(
        self,
        *,
        project: ProjectRef,
        scope: EffectiveAccessScope,
        as_of: datetime,
        records: list[SnapshotMemoryRecord],
    ) -> ContextSnapshot:
        snapshot = ContextSnapshot(
            project=project,
            actor_id=scope.actor_id,
            chat_id=scope.chat_id,
            as_of=as_of,
            records=tuple(records),
            context_hash=_context_hash(records),
        )
        self.snapshot_store.put(snapshot)
        return snapshot

    @staticmethod
    def _record(
        candidate: MemoryCandidate,
        *,
        memory: ProjectMemory | None,
        content_hash: str,
        state: str,
        authorization_result: str,
        reason_code: str | None,
    ) -> SnapshotMemoryRecord:
        return SnapshotMemoryRecord(
            memory_id=candidate.memory_id,
            fact_key=memory.fact_key if memory is not None else candidate.fact_key,
            content_hash=content_hash,
            source_refs=memory.source_refs if memory is not None else (),
            valid_from=memory.valid_from if memory is not None else candidate.valid_from,
            valid_to=memory.valid_to if memory is not None else candidate.valid_to,
            authorization_result=authorization_result,
            state=state,
            reason_code=reason_code,
            text=None,
        )


def _context_hash(records: Iterable[SnapshotMemoryRecord]) -> str:
    payload = "|".join(
        f"{item.memory_id}:{item.content_hash}:{item.fact_key or ''}:{item.valid_from.isoformat()}:{item.valid_to.isoformat() if item.valid_to else ''}"
        for item in records
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def candidate_from_memory(memory: ProjectMemory) -> MemoryCandidate:
    return MemoryCandidate(
        source_id=memory.id,
        source_hash=memory.content_hash or sha256(memory.text.encode("utf-8")).hexdigest(),
        fact_key=memory.fact_key,
        valid_from=memory.valid_from,
        valid_to=memory.valid_to,
        status_at_selection=memory.status,
    )
