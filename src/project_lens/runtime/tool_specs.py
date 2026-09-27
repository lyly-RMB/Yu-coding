"""Declarative tool harness specs (schema / risk / surface). Locked for agents."""

from __future__ import annotations

import hashlib
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from project_lens.runtime.policy import RiskClass


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ToolLane(StrEnum):
    """Capability lane from the Agent Harness design."""

    READ = "read"
    VALIDATE = "validate"
    APPROVAL = "approval"
    APPLY = "apply"


class SurfaceClass(StrEnum):
    LOCKED = "locked"
    EDITABLE = "editable"
    APPEND_ONLY = "append_only"
    HUMAN_CONTROLLED = "human_controlled"


class ToolSpec(FrozenModel):
    tool_name: str = Field(min_length=1, max_length=100)
    category: ToolLane
    risk_class: RiskClass
    project_scope: str = "current_project"
    allowed_paths: tuple[str, ...] = ()
    timeout_seconds: int = Field(default=30, ge=1, le=600)
    idempotent: bool = True
    requires_approval: bool = False
    surface: SurfaceClass
    description: str = ""
    audit_payload_keys: tuple[str, ...] = ()


# Locked registry: agents may read specs, not mutate this module at runtime.
TOOL_SPECS: dict[str, ToolSpec] = {
    "search_project_memory": ToolSpec(
        tool_name="search_project_memory",
        category=ToolLane.READ,
        risk_class=RiskClass.READ,
        surface=SurfaceClass.LOCKED,
        description="Bounded search over approved active project memories",
        audit_payload_keys=("project", "query", "returned_count", "omitted_count"),
    ),
    "get_memory_detail": ToolSpec(
        tool_name="get_memory_detail",
        category=ToolLane.READ,
        risk_class=RiskClass.READ,
        surface=SurfaceClass.LOCKED,
        description="Read one approved active project memory by exact id",
        audit_payload_keys=("project", "memory_id"),
    ),
    "search_project_history": ToolSpec(
        tool_name="search_project_history",
        category=ToolLane.READ,
        risk_class=RiskClass.READ,
        surface=SurfaceClass.LOCKED,
        description="Bounded search over historical AgentRuns and safe event summaries",
        audit_payload_keys=("project", "query", "returned_count", "omitted_count"),
    ),
    "get_run_detail": ToolSpec(
        tool_name="get_run_detail",
        category=ToolLane.READ,
        risk_class=RiskClass.READ,
        surface=SurfaceClass.LOCKED,
        description="Read one current-project AgentRun detail without raw tool arguments",
        audit_payload_keys=("project", "run_id"),
    ),
    "get_citation_body": ToolSpec(
        tool_name="get_citation_body",
        category=ToolLane.READ,
        risk_class=RiskClass.READ,
        surface=SurfaceClass.LOCKED,
        description="Read one conversation citation body by citation_id within current binding",
        audit_payload_keys=("project", "citation_id"),
    ),
    "search_context": ToolSpec(
        tool_name="search_context",
        category=ToolLane.READ,
        risk_class=RiskClass.READ,
        surface=SurfaceClass.LOCKED,
        description="ACL-filtered project context search via ContextEngine",
        audit_payload_keys=("project", "query", "hit_count"),
    ),
    "get_project_snapshot": ToolSpec(
        tool_name="get_project_snapshot",
        category=ToolLane.READ,
        risk_class=RiskClass.READ,
        surface=SurfaceClass.LOCKED,
        description="Read-only project snapshot",
    ),
    "get_timeline": ToolSpec(
        tool_name="get_timeline",
        category=ToolLane.READ,
        risk_class=RiskClass.READ,
        surface=SurfaceClass.LOCKED,
        description="Read-only project timeline",
    ),
    "get_change_impact": ToolSpec(
        tool_name="get_change_impact",
        category=ToolLane.READ,
        risk_class=RiskClass.READ,
        surface=SurfaceClass.LOCKED,
        description="Read-only change impact analysis",
    ),
    "query_graph": ToolSpec(
        tool_name="query_graph",
        category=ToolLane.READ,
        risk_class=RiskClass.READ,
        surface=SurfaceClass.LOCKED,
        description="Read-only graph path query",
    ),
    "query_logs": ToolSpec(
        tool_name="query_logs",
        category=ToolLane.READ,
        risk_class=RiskClass.READ,
        surface=SurfaceClass.LOCKED,
        description="Windowed ops logs (ephemeral, not long-term RAG)",
    ),
    "query_metrics": ToolSpec(
        tool_name="query_metrics",
        category=ToolLane.READ,
        risk_class=RiskClass.READ,
        surface=SurfaceClass.LOCKED,
        description="Windowed ops metrics (ephemeral, not long-term RAG)",
    ),
    "query_traces": ToolSpec(
        tool_name="query_traces",
        category=ToolLane.READ,
        risk_class=RiskClass.READ,
        surface=SurfaceClass.LOCKED,
        description="Windowed ops traces (ephemeral, not long-term RAG)",
    ),
    "read_project_file": ToolSpec(
        tool_name="read_project_file",
        category=ToolLane.READ,
        risk_class=RiskClass.READ,
        allowed_paths=("src/", "tests/", "knowledge/"),
        surface=SurfaceClass.LOCKED,
        description="Read a file under the project allowlist",
        audit_payload_keys=("path",),
    ),
    "list_project_files": ToolSpec(
        tool_name="list_project_files",
        category=ToolLane.READ,
        risk_class=RiskClass.READ,
        allowed_paths=("src/", "tests/", "knowledge/"),
        surface=SurfaceClass.LOCKED,
        description="List files under ProjectSpace file_allowlist",
        audit_payload_keys=("prefix", "file_count"),
    ),
    "grep_project_code": ToolSpec(
        tool_name="grep_project_code",
        category=ToolLane.READ,
        risk_class=RiskClass.READ,
        allowed_paths=("src/", "tests/", "knowledge/"),
        surface=SurfaceClass.LOCKED,
        description="Grep text in allowlisted project files",
        audit_payload_keys=("pattern", "hit_count"),
    ),
    "search_project_code": ToolSpec(
        tool_name="search_project_code",
        category=ToolLane.READ,
        risk_class=RiskClass.READ,
        allowed_paths=("src/", "tests/", "knowledge/"),
        surface=SurfaceClass.LOCKED,
        description="Alias of grep_project_code for code navigation",
        audit_payload_keys=("pattern", "hit_count"),
    ),
    "read_project_file_range": ToolSpec(
        tool_name="read_project_file_range",
        category=ToolLane.READ,
        risk_class=RiskClass.READ,
        allowed_paths=("src/", "tests/", "knowledge/"),
        surface=SurfaceClass.LOCKED,
        description="Read a line range from an allowlisted project file",
        audit_payload_keys=("path", "start_line", "end_line"),
    ),
    "get_doc_sync_status": ToolSpec(
        tool_name="get_doc_sync_status",
        category=ToolLane.READ,
        risk_class=RiskClass.READ,
        surface=SurfaceClass.LOCKED,
        description="Read Feishu document sync status",
    ),
    "authorized_evidence": ToolSpec(
        tool_name="authorized_evidence",
        category=ToolLane.READ,
        risk_class=RiskClass.READ,
        surface=SurfaceClass.LOCKED,
        description="ACL-filtered authorized evidence list via ContextEngine",
        audit_payload_keys=("project", "limit", "hit_count"),
    ),
    "list_knowledge_gaps": ToolSpec(
        tool_name="list_knowledge_gaps",
        category=ToolLane.READ,
        risk_class=RiskClass.READ,
        surface=SurfaceClass.LOCKED,
        description="Build KnowledgeGapReport from authorized evidence + graph",
        audit_payload_keys=("project", "gap_count"),
    ),
    "create_patch_plan": ToolSpec(
        tool_name="create_patch_plan",
        category=ToolLane.VALIDATE,
        risk_class=RiskClass.VALIDATE,
        allowed_paths=("src/", "tests/"),
        surface=SurfaceClass.EDITABLE,
        description="Create an isolated patch plan draft",
        audit_payload_keys=("title", "paths"),
    ),
    "validate_patch_plan": ToolSpec(
        tool_name="validate_patch_plan",
        category=ToolLane.VALIDATE,
        risk_class=RiskClass.VALIDATE,
        allowed_paths=("src/", "tests/"),
        surface=SurfaceClass.EDITABLE,
        description="Validate a patch plan inside an isolated worktree",
        audit_payload_keys=("paths", "test_passed"),
    ),
    "run_allowed_tests": ToolSpec(
        tool_name="run_allowed_tests",
        category=ToolLane.VALIDATE,
        risk_class=RiskClass.VALIDATE,
        surface=SurfaceClass.EDITABLE,
        description="Run allowlisted tests in the isolated worktree",
    ),
    "summarize_diff": ToolSpec(
        tool_name="summarize_diff",
        category=ToolLane.VALIDATE,
        risk_class=RiskClass.VALIDATE,
        surface=SurfaceClass.EDITABLE,
        description="Summarize worktree diff for cards/audit",
    ),
    "create_action_proposal": ToolSpec(
        tool_name="create_action_proposal",
        category=ToolLane.APPROVAL,
        risk_class=RiskClass.VALIDATE,
        requires_approval=True,
        surface=SurfaceClass.APPEND_ONLY,
        description="Create an ActionProposal requiring human approval",
    ),
    "create_memory_proposal": ToolSpec(
        tool_name="create_memory_proposal",
        category=ToolLane.APPROVAL,
        risk_class=RiskClass.VALIDATE,
        requires_approval=True,
        surface=SurfaceClass.APPEND_ONLY,
        description="Create a MemoryProposal; does not write ProjectMemory",
    ),
    "decide_memory_proposal": ToolSpec(
        tool_name="decide_memory_proposal",
        category=ToolLane.APPROVAL,
        risk_class=RiskClass.APPLY,
        requires_approval=True,
        surface=SurfaceClass.HUMAN_CONTROLLED,
        description="Human decision on a memory proposal",
    ),
    "apply_patch_plan": ToolSpec(
        tool_name="apply_patch_plan",
        category=ToolLane.APPLY,
        risk_class=RiskClass.APPLY,
        requires_approval=True,
        surface=SurfaceClass.HUMAN_CONTROLLED,
        description="Apply a validated patch to the main tree (disabled by default)",
        audit_payload_keys=("paths", "approval_id"),
    ),
    "create_pr": ToolSpec(
        tool_name="create_pr",
        category=ToolLane.APPLY,
        risk_class=RiskClass.APPLY,
        requires_approval=True,
        surface=SurfaceClass.HUMAN_CONTROLLED,
        description="Create a pull request (stub / human-controlled)",
    ),
    "rollback_release": ToolSpec(
        tool_name="rollback_release",
        category=ToolLane.APPLY,
        risk_class=RiskClass.APPLY,
        requires_approval=True,
        surface=SurfaceClass.HUMAN_CONTROLLED,
        description="Rollback a release (stub / human-controlled)",
    ),
    "restart_service": ToolSpec(
        tool_name="restart_service",
        category=ToolLane.APPLY,
        risk_class=RiskClass.APPLY,
        requires_approval=True,
        surface=SurfaceClass.HUMAN_CONTROLLED,
        description="Restart a production service (stub / human-controlled)",
    ),
}


def get_tool_spec(tool_name: str) -> ToolSpec:
    try:
        return TOOL_SPECS[tool_name]
    except KeyError as exc:
        raise KeyError(f"unknown tool spec: {tool_name}") from exc


def list_tool_specs(*, category: ToolLane | None = None) -> tuple[ToolSpec, ...]:
    specs = tuple(TOOL_SPECS.values())
    if category is None:
        return specs
    return tuple(item for item in specs if item.category == category)


def tool_policy_hash(*, length: int = 16) -> str:
    """Stable fingerprint of the locked tool registry for L0 non-compressible anchors."""

    parts: list[str] = []
    for name in sorted(TOOL_SPECS):
        spec = TOOL_SPECS[name]
        parts.append(
            f"{spec.tool_name}|{spec.category.value}|{spec.risk_class.value}|"
            f"{spec.surface.value}|approval={int(spec.requires_approval)}|"
            f"apply_lane={int(spec.category == ToolLane.APPLY)}"
        )
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return digest[: max(8, min(64, int(length)))]


def assert_tool_callable(tool_name: str, *, allow_apply: bool) -> ToolSpec:
    """Refuse Engineering Apply-lane tools unless allow_apply is enabled.

    APPROVAL-lane tools may use RiskClass.APPLY (e.g. decide_memory_proposal writes
    ProjectMemory after a human decision) without enabling engineering patch Apply.
    """

    spec = get_tool_spec(tool_name)
    if spec.category == ToolLane.APPLY and not allow_apply:
        raise PermissionError(
            f"{tool_name} is human-controlled and apply is disabled until approval"
        )
    if (
        spec.risk_class == RiskClass.APPLY
        and spec.category != ToolLane.APPROVAL
        and not allow_apply
    ):
        raise PermissionError(
            f"{tool_name} requires apply risk approval and is currently disabled"
        )
    return spec
