"""HTTP schemas for one-shot Project Agent ask API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from project_lens.domain.models import ProjectRef


class ProjectAgentAskRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=20_000)
    project: ProjectRef
    user_id: str = Field(default="api-user", min_length=1, max_length=100)
    channel_id: str | None = Field(default=None, max_length=200)
    audience: str = Field(default="team", max_length=50)
    mode: str = Field(default="read_only", max_length=32)
    format: str = Field(default="concise", max_length=32)


class ProjectAgentAskResponse(BaseModel):
    """Loose envelope — success and recoverable error share this shape."""

    model_config = ConfigDict(extra="allow")

    ok: bool = True
    project: dict[str, Any] | None = None
    run_id: str | None = None
    trace_id: str | None = None
    answer_summary: str | None = None
    facts: list[dict[str, Any]] = Field(default_factory=list)
    inferences: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    next_actions: list[dict[str, Any]] = Field(default_factory=list)
    citations: list[dict[str, Any]] = Field(default_factory=list)
    audit_ref: dict[str, Any] = Field(default_factory=dict)
    role_views_available: list[str] = Field(default_factory=list)
    error_code: str | None = None
    message: str | None = None
    retryable: bool | None = None
    agent_recovery_hint: str | None = None


class ProjectAgentRoleViewRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audience: str = Field(default="team", min_length=1, max_length=50)


class ProjectAgentRoleViewResponse(BaseModel):
    """RoleView replay result — presentation only, no new investigation."""

    model_config = ConfigDict(extra="allow")

    ok: bool = True
    audience: str | None = None
    run_id: str | None = None
    trace_id: str | None = None
    markdown: str | None = None
    envelope_ref: dict[str, Any] | None = None
    audit_ref: dict[str, Any] = Field(default_factory=dict)
    role_views_available: list[str] = Field(default_factory=list)
    error_code: str | None = None
    message: str | None = None
    retryable: bool | None = None
    agent_recovery_hint: str | None = None
    project: dict[str, Any] | None = None


class ProjectAgentRunDetailRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audience: str = Field(default="team", min_length=1, max_length=50)


class ProjectAgentRunDetailResponse(BaseModel):
    """Run detail replay result — safe, re-checked, and read-only."""

    model_config = ConfigDict(extra="allow")

    ok: bool = True
    run_id: str | None = None
    trace_id: str | None = None
    project: dict[str, Any] | None = None
    status: str | None = None
    runtime: str | None = None
    entry_mode: str | None = None
    verification_state: str | None = None
    answer_summary: str | None = None
    facts: list[dict[str, Any]] = Field(default_factory=list)
    inferences: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    next_actions: list[dict[str, Any]] = Field(default_factory=list)
    citations: list[dict[str, Any]] = Field(default_factory=list)
    citation_count: int = 0
    source_summary: list[str] = Field(default_factory=list)
    failure_reason: str | None = None
    audit_ref: dict[str, Any] = Field(default_factory=dict)
    role_views_available: list[str] = Field(default_factory=list)
    error_code: str | None = None
    message: str | None = None
    retryable: bool | None = None
    agent_recovery_hint: str | None = None


class ProjectAgentKnowledgeGapDigestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ProjectRef
    period_start: str = Field(min_length=1, max_length=50)
    period_end: str = Field(min_length=1, max_length=50)


class ProjectAgentKnowledgeGapDigestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ProjectRef
    period_start: str
    period_end: str
    digest_date: str
    new_count: int
    ongoing_count: int
    high_impact_count: int
    reopened_count: int
    resolved_count: int
    summary_hash: str
    card: dict[str, Any]


class ProjectAgentKnowledgeGapActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: ProjectRef
    action_type: str = Field(min_length=1, max_length=50)
    description: str = Field(min_length=1, max_length=2_000)
    target_owner_id: str | None = Field(default=None, max_length=200)
    linked_task_id: str | None = Field(default=None, max_length=300)


class ProjectAgentKnowledgeGapActionStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: ProjectRef
    status: str = Field(min_length=1, max_length=30)
    reason: str = Field(default="", max_length=2_000)


class ProjectAgentKnowledgeGapTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: ProjectRef
    status: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=1, max_length=2_000)


class ProjectAgentToolInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    category: str = "read"
    parameters: dict[str, Any] = Field(default_factory=dict)
    allow_apply: bool = False
    requires_approval: bool = False
    deprecated: bool = False


class ProjectAgentToolsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    tools: list[ProjectAgentToolInfo] = Field(default_factory=list)


class ProjectAgentToolCallRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1, max_length=100)
    project: ProjectRef
    user_id: str = Field(min_length=1, max_length=100)
    chat_id: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ProjectAgentToolCallResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: bool
    tool_name: str | None = None
    internal_tool_name: str | None = None
    tool_result_id: str | None = None
    project: dict[str, Any] | None = None
    summary: str | None = None
    citations: list[dict[str, Any]] = Field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    audit_ref: dict[str, Any] = Field(default_factory=dict)
    visibility_scope: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    message: str | None = None
    retryable: bool | None = None
    agent_recovery_hint: str | None = None
