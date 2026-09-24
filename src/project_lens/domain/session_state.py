"""Structured, durable state that survives Hermes transcript rotation."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from project_lens.domain.models import ProjectRef, utc_now


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SessionFactRef(FrozenModel):
    """A confirmed fact with enough provenance to re-read its source."""

    fact_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=2_000)
    evidence_ids: tuple[str, ...] = ()
    source_keys: tuple[str, ...] = ()
    revisions: tuple[str, ...] = ()
    content_hashes: tuple[str, ...] = ()
    status: Literal["confirmed", "conflicted", "unknown"] = "confirmed"


class SessionDecision(FrozenModel):
    """A decision or proposed direction retained for continuation."""

    decision_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=2_000)
    rationale: str | None = Field(default=None, max_length=2_000)
    evidence_ids: tuple[str, ...] = ()
    status: Literal["confirmed", "proposed", "rejected", "open"] = "open"
    created_at: datetime = Field(default_factory=utc_now)


class SessionEvidenceRef(FrozenModel):
    """Compact Evidence identity retained outside the model transcript."""

    evidence_id: str = Field(min_length=1, max_length=200)
    source_key: str = Field(min_length=1, max_length=500)
    revision: str | None = Field(default=None, max_length=200)
    content_hash: str | None = Field(default=None, max_length=200)
    authority: str | None = Field(default=None, max_length=100)
    status: str | None = Field(default=None, max_length=100)


class SessionArtifactRef(FrozenModel):
    """File, symbol, run, or other artifact needed to continue work."""

    kind: Literal["file", "symbol", "run", "trace", "document", "other"] = "other"
    key: str = Field(min_length=1, max_length=500)
    detail: str | None = Field(default=None, max_length=1_000)


class ProjectSessionState(FrozenModel):
    """Structured session memory owned by ProjectLens, not Hermes."""

    session_id: UUID
    project: ProjectRef
    actor_id: str = Field(min_length=1, max_length=100)
    task_intent: str | None = Field(default=None, max_length=2_000)
    current_status: str = Field(default="in_progress", max_length=100)
    confirmed_facts: tuple[SessionFactRef, ...] = ()
    decisions: tuple[SessionDecision, ...] = ()
    open_questions: tuple[str, ...] = ()
    blocked_items: tuple[str, ...] = ()
    evidence_refs: tuple[SessionEvidenceRef, ...] = ()
    artifact_refs: tuple[SessionArtifactRef, ...] = ()
    summary_markdown: str = ""
    summary_version: int = Field(default=0, ge=0)
    last_compressed_turn_id: str | None = Field(default=None, max_length=200)
    last_handoff_id: str | None = Field(default=None, max_length=200)
    rotation_count: int = Field(default=0, ge=0)
    rotation_pending: bool = False
    updated_at: datetime = Field(default_factory=utc_now)


class HandoffSummary(FrozenModel):
    """Bounded context injected into a new Hermes session."""

    handoff_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=200)
    session_id: UUID
    project_id: str
    summary_version: int = Field(ge=0)
    markdown: str = Field(min_length=1, max_length=20_000)
    evidence_ids: tuple[str, ...] = ()
    artifact_keys: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)


def render_session_state_markdown(state: ProjectSessionState) -> str:
    """Render fixed sections for stable, inspectable incremental summaries."""

    def bullets(values: tuple[str, ...]) -> str:
        return "\n".join(f"- {item}" for item in values) or "- (none)"

    facts = "\n".join(
        f"- {item.text} [evidence: {', '.join(item.evidence_ids) or 'none'}]"
        for item in state.confirmed_facts
    ) or "- (none)"
    decisions = "\n".join(
        f"- {item.text} (status={item.status}; rationale={item.rationale or 'none'})"
        for item in state.decisions
    ) or "- (none)"
    artifacts = "\n".join(
        f"- {item.kind}:{item.key}{f' ({item.detail})' if item.detail else ''}"
        for item in state.artifact_refs
    ) or "- (none)"
    evidence = "\n".join(
        f"- {item.evidence_id} | {item.source_key} | revision={item.revision or 'none'} | hash={item.content_hash or 'none'}"
        for item in state.evidence_refs
    ) or "- (none)"
    return "\n".join(
        (
            "## Session Intent",
            state.task_intent or "(none)",
            "",
            "## Current State",
            f"status={state.current_status}",
            "",
            "## Confirmed Facts",
            facts,
            "",
            "## Decisions",
            decisions,
            "",
            "## Open Questions",
            bullets(state.open_questions),
            "",
            "## Blocked Items",
            bullets(state.blocked_items),
            "",
            "## Evidence References",
            evidence,
            "",
            "## Files And Artifacts",
            artifacts,
            "",
            "## Next Steps",
            bullets(tuple(item.text for item in state.decisions if item.status in {"open", "proposed"})),
            "",
            "## Compression Metadata",
            f"summary_version={state.summary_version}",
            f"last_compressed_turn_id={state.last_compressed_turn_id or 'none'}",
            f"last_handoff_id={state.last_handoff_id or 'none'}",
            f"rotation_count={state.rotation_count}",
        )
    )


def build_handoff_summary(state: ProjectSessionState) -> HandoffSummary:
    """Create a bounded, auditable handoff without copying raw transcript text."""

    handoff_id = str(uuid4())
    markdown = (
        f"handoff_id={handoff_id}\n"
        f"session_id={state.session_id}\n"
        f"project_id={state.project.project_id}\n"
        f"summary_version={state.summary_version}\n\n"
        f"{state.summary_markdown or render_session_state_markdown(state)}"
    )[:20_000]
    return HandoffSummary(
        handoff_id=handoff_id,
        session_id=state.session_id,
        project_id=state.project.project_id,
        summary_version=state.summary_version,
        markdown=markdown,
        evidence_ids=tuple(item.evidence_id for item in state.evidence_refs)[-80:],
        artifact_keys=tuple(item.key for item in state.artifact_refs)[-80:],
    )
