"""ContextPack: structured L0-L5 assembly for Agent Harness runs.

First cut is a structured carrier. Retrieval still goes through ContextEngine;
Feishu adapters must not build packs from EvidenceIndex directly.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from project_lens.context.models import AccessContext
from project_lens.domain.conversation import (
    ConversationSession,
    ConversationSummary,
    ConversationTurn,
)
from project_lens.domain.memory import ProjectMemory
from project_lens.domain.models import AgentRun, Evidence, ProjectRef
from project_lens.runtime.tool_specs import tool_policy_hash
from project_lens.workflow.models import ResolvedProject

RetrievalMode = Literal["authorized", "search"]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AnchorContext(FrozenModel):
    """L0 uncrompressible anchors. Never summarize into prose."""

    project: ProjectRef
    access_scope: str = Field(min_length=1, max_length=200)
    access: AccessContext
    user_id: str = Field(min_length=1, max_length=100)
    run_id: UUID
    trace_id: UUID
    channel_id: str | None = None
    question: str = Field(min_length=1, max_length=20_000)
    skill: str | None = None
    allow_apply: bool = False
    policy_version: str = "v1"
    tool_boundary: str = "tool_gateway"
    tool_policy_hash: str = Field(default="", max_length=64)
    session_id: UUID | None = None


class ContextProvenance(FrozenModel):
    """Where L4/L5 context came from — answers harness acceptance Q2."""

    retrieval_mode: RetrievalMode = "search"
    evidence_source: str = "read_context_gateway"
    memory_source: str = "approved_project_memory"
    graph_path_count: int = Field(default=0, ge=0)
    ops_signal_count: int = Field(default=0, ge=0)
    knowledge_gap_count: int = Field(default=0, ge=0)
    evidence_limit: int | None = None


class TaskScratchpad(FrozenModel):
    """L3 structured task workbench for Engineering / Ops / DocSync process state."""

    phase: str | None = None
    plan: str | None = None
    files_read: tuple[str, ...] = ()
    files_changed: tuple[str, ...] = ()
    patch_plan: str | None = None
    diff_summary: str | None = None
    test_commands: tuple[str, ...] = ()
    test_results: tuple[str, ...] = ()
    approval_status: str | None = None
    allow_apply: bool = False
    sync_state: dict[str, str] = Field(default_factory=dict)
    failed_attempts: tuple[str, ...] = ()
    rejected_plans: tuple[str, ...] = ()
    tool_audit_events: tuple[str, ...] = ()
    notes: dict[str, str] = Field(default_factory=dict)


class ContextPack(FrozenModel):
    """Assembled harness context for one AgentRun."""

    anchors: AnchorContext
    recent_turns: tuple[ConversationTurn, ...] = ()
    session_summary: ConversationSummary | None = None
    task_state: TaskScratchpad | None = None
    evidence: tuple[Evidence, ...] = ()
    memories: tuple[ProjectMemory, ...] = ()
    provenance: ContextProvenance = Field(default_factory=ContextProvenance)

    @property
    def evidence_ids(self) -> tuple[UUID, ...]:
        return tuple(item.id for item in self.evidence)

    @property
    def memory_ids(self) -> tuple[UUID, ...]:
        return tuple(item.id for item in self.memories)

    def audit_refs(self) -> dict[str, Any]:
        """Compact id/ref view for RunEvents. Does not dump full evidence bodies."""

        return {
            "run_id": str(self.anchors.run_id),
            "trace_id": str(self.anchors.trace_id),
            "session_id": (
                str(self.anchors.session_id) if self.anchors.session_id else None
            ),
            "project": {
                "tenant_id": self.anchors.project.tenant_id,
                "project_id": self.anchors.project.project_id,
                "service": self.anchors.project.service,
                "environment": self.anchors.project.environment,
            },
            "access_scope": self.anchors.access_scope,
            "allow_apply": self.anchors.allow_apply,
            "skill": self.anchors.skill,
            "recent_turn_count": len(self.recent_turns),
            "has_session_summary": self.session_summary is not None,
            "active_skill": (
                self.session_summary.active_skill if self.session_summary else None
            ),
            "task_phase": self.task_state.phase if self.task_state else None,
            "evidence_ids": [str(item) for item in self.evidence_ids],
            "memory_ids": [str(item) for item in self.memory_ids],
            "policy_version": self.anchors.policy_version,
            "tool_boundary": self.anchors.tool_boundary,
            "tool_policy_hash": self.anchors.tool_policy_hash,
            "provenance": self.provenance.model_dump(),
        }


# Session bag keys that are not TaskScratchpad fields (attached by ConversationService).
_SESSION_SCRATCHPAD_BAG_KEYS = frozenset(
    {"runtime_access", "hermes_tool_loop", "hermes_tool_loops"}
)
_NOTE_VALUE_MAX = 2_000


def _coerce_notes(raw_notes: object) -> dict[str, str]:
    """Keep notes as a flat str→str map; never stringify nested scratchpads."""

    if not isinstance(raw_notes, dict):
        return {}
    notes: dict[str, str] = {}
    for key, value in raw_notes.items():
        name = str(key).strip()
        if not name or name in _SESSION_SCRATCHPAD_BAG_KEYS:
            continue
        # Recursive self-key from prior bag dumps — drop entirely.
        if name == "notes":
            continue
        # Nested dict/list here is almost always a prior bag dump — drop it.
        if isinstance(value, (dict, list, tuple)):
            continue
        text = str(value)
        if len(text) > _NOTE_VALUE_MAX:
            text = text[:_NOTE_VALUE_MAX]
        notes[name] = text
    return notes


def task_scratchpad_from_session(
    session: ConversationSession | None,
) -> TaskScratchpad | None:
    if session is None or not session.task_scratchpad:
        return None
    raw = {
        key: value
        for key, value in dict(session.task_scratchpad).items()
        if key not in _SESSION_SCRATCHPAD_BAG_KEYS
    }
    if "notes" in raw:
        raw["notes"] = _coerce_notes(raw.get("notes"))
    try:
        return TaskScratchpad.model_validate(raw)
    except Exception:  # noqa: BLE001 - reserved bag may hold partial keys
        # Partial / legacy bags: keep only a coerced notes map. Never dump the
        # whole bag via str() — that recursively balloons session payloads.
        notes_raw = raw.get("notes")
        if isinstance(notes_raw, dict):
            return TaskScratchpad(notes=_coerce_notes(notes_raw))
        return TaskScratchpad(notes=_coerce_notes(raw))


def build_context_pack(
    *,
    run: AgentRun,
    resolved: ResolvedProject,
    access: AccessContext,
    session: ConversationSession | None = None,
    evidence: tuple[Evidence, ...] = (),
    memories: tuple[ProjectMemory, ...] = (),
    skill: str | None = None,
    allow_apply: bool = False,
    task_state: TaskScratchpad | None = None,
    provenance: ContextProvenance | None = None,
) -> ContextPack:
    """Assemble L0-L5 carriers for the current run.

    L4 evidence/memories must already be ACL-filtered by the caller (ContextEngine /
    MemoryStore). This function does not query indexes.
    """

    if allow_apply:
        raise ValueError("ContextPack refuses allow_apply=True in the read-only harness cut")

    scratchpad = task_state
    if scratchpad is None:
        scratchpad = task_scratchpad_from_session(session)
        if scratchpad is not None and scratchpad.allow_apply:
            scratchpad = scratchpad.model_copy(update={"allow_apply": False})

    anchors = AnchorContext(
        project=resolved.project,
        access_scope=resolved.access_scope,
        access=access,
        user_id=run.user_id,
        run_id=run.id,
        trace_id=run.trace_id,
        channel_id=run.channel_id,
        question=run.question,
        skill=skill,
        allow_apply=False,
        tool_policy_hash=tool_policy_hash(),
        session_id=session.session_id if session is not None else None,
    )
    return ContextPack(
        anchors=anchors,
        recent_turns=session.recent_turns if session is not None else (),
        session_summary=session.summary if session is not None else None,
        task_state=scratchpad,
        evidence=evidence,
        memories=memories,
        provenance=provenance or ContextProvenance(),
    )
