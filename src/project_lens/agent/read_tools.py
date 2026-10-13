"""Read-only investigation tools backed by ReadContextGateway / ToolGateway."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any
from uuid import UUID

from project_lens.context.models import AccessContext, ContextQuery
from project_lens.context.source_records import FactType, SourceRecord
from project_lens.domain.models import (
    Evidence,
    EvidenceType,
    ProjectRef,
    SourceRef,
)
from project_lens.project_space.policies import EffectiveAccessScope, source_path_allowed
from project_lens.runtime.read_gateway import ReadContextGateway
from project_lens.runtime.tool_gateway import ToolGateway
from project_lens.runtime.tools import BaseTool, ToolRegistry
from project_lens.runtime.types import ToolCategory

READ_ONLY_INVESTIGATION_TOOLS = frozenset(
    {
        "search_context",
        "authorized_evidence",
        "list_knowledge_gaps",
        "read_project_file",
        "list_project_files",
    }
)

WRITE_BLOCKED_TOOL_NAMES = frozenset(
    {
        "apply_patch",
        "create_pr",
        "deploy",
        "rollback",
        "restart",
        "write_file",
        "shell",
        "terminal",
    }
)


@dataclass
class InvestigationLedger:
    """Accumulates Evidence / ToolObservations for citation verification."""

    evidence: dict[UUID, Evidence] = field(default_factory=dict)
    tool_names: list[str] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)

    def add_evidence(self, item: Evidence) -> None:
        self.evidence[item.id] = item

    def add_many(self, items: tuple[Evidence, ...] | list[Evidence]) -> None:
        for item in items:
            self.add_evidence(item)

    def record_tool(self, name: str, summary: dict[str, Any]) -> None:
        self.tool_names.append(name)
        self.observations.append({"tool": name, **summary})

    def all_evidence(self) -> tuple[Evidence, ...]:
        return tuple(self.evidence.values())


def build_investigation_tool_registry(
    *,
    project: ProjectRef,
    access: AccessContext,
    read_gateway: ReadContextGateway,
    tool_gateway: ToolGateway | None,
    ledger: InvestigationLedger,
    access_scope: str,
    effective_scope: EffectiveAccessScope | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    gateway_kwargs = {
        "project": project,
        "access": access,
        "gateway": read_gateway,
        "ledger": ledger,
        "effective_scope": effective_scope,
    }
    registry.register(SearchContextTool(**gateway_kwargs))
    registry.register(AuthorizedEvidenceTool(**gateway_kwargs))
    registry.register(ListKnowledgeGapsTool(**gateway_kwargs))
    if tool_gateway is not None:
        file_kwargs = {
            "project": project,
            "gateway": tool_gateway,
            "ledger": ledger,
            "access_scope": access_scope,
            "effective_scope": effective_scope,
        }
        registry.register(ReadProjectFileTool(**file_kwargs))
        registry.register(ListProjectFilesTool(**file_kwargs))
    return registry


class SearchContextTool(BaseTool):
    name = "search_context"
    description = "Search project evidence (docs, code, commits) by query text."
    category = ToolCategory.READ
    required_permission = "project:read"

    def __init__(
        self,
        *,
        project: ProjectRef,
        access: AccessContext,
        gateway: ReadContextGateway,
        ledger: InvestigationLedger,
        effective_scope: EffectiveAccessScope | None = None,
    ) -> None:
        self._project = project
        self._access = access
        self._gateway = gateway
        self._ledger = ledger
        self._scope = effective_scope

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
                "fact_type": {
                    "type": "string",
                    "enum": [item.value for item in FactType],
                    "description": (
                        "Optional structured fact lookup. Use for requirement scope/status, "
                        "development progress, test status, technical decisions, or owner."
                    ),
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        query = str(kwargs["query"])
        raw_fact_type = kwargs.get("fact_type")
        if raw_fact_type:
            fact_type = FactType(str(raw_fact_type))
            resolution = self._gateway.resolve_fact(
                self._project,
                self._access,
                fact_type,
                scope=self._scope,
            )
            # ``resolve_fact`` has already applied project, actor, and chat ACLs.
            # Convert only those visible source snapshots into citation evidence;
            # do not call another tool internally and accidentally require an
            # additional permission such as ``authorized_evidence``.
            visible_records = (*resolution.candidates, *resolution.conflicts)
            self._ledger.add_many(
                tuple(item.to_evidence(project=self._project) for item in visible_records)
            )
            self._ledger.record_tool(
                self.name,
                {
                    "mode": "fact_resolution",
                    "fact_type": fact_type.value,
                    "candidate_count": len(resolution.candidates),
                    "conflict_count": len(resolution.conflicts),
                },
            )
            return json.dumps(
                _fact_resolution_payload(resolution),
                ensure_ascii=False,
            )
        limit = int(kwargs.get("limit") or 8)
        bundle = self._gateway.search_context(
            ContextQuery(text=query, project=self._project, limit=min(max(limit, 1), 20)),
            self._access,
            scope=self._scope,
        )
        self._ledger.add_many(bundle.evidence)
        self._ledger.record_tool(
            self.name,
            {"query": query[:120], "hit_count": len(bundle.hits)},
        )
        hits = [
            {
                "evidence_id": str(hit.evidence.id),
                "type": hit.evidence.type.value,
                "source_id": hit.evidence.source.source_id,
                "score": hit.score,
                "snippet": hit.evidence.content[:400],
            }
            for hit in bundle.hits[:8]
        ]
        return json.dumps({"hits": hits, "warnings": list(bundle.warnings)}, ensure_ascii=False)


def _fact_resolution_payload(resolution: Any) -> dict[str, Any]:
    selected = resolution.selected
    conflicts = resolution.conflicts
    if selected is None:
        state = "unknown"
    elif conflicts:
        state = "conflicted"
    elif selected.status in {"proposed", "draft", "unconfirmed"}:
        state = "unconfirmed"
    elif _is_stale(selected.observed_at):
        state = "stale"
    else:
        state = "confirmed"

    def serialize(item: SourceRecord | None) -> dict[str, Any] | None:
        if item is None:
            return None
        return {
            "source_id": item.source_id,
            "source_type": item.source_type.value,
            "title": item.title,
            "raw_uri": item.raw_uri,
            "revision": item.revision,
            "observed_at": item.observed_at.isoformat(),
            "status": item.status,
            "authority_scope": list(item.authority_scope),
            "fact_value": item.fact_values.get(resolution.fact_type.value),
        }

    return {
        "mode": "fact_resolution",
        "fact_type": resolution.fact_type.value,
        "state": state,
        "selected": serialize(selected),
        "candidates": [serialize(item) for item in resolution.candidates],
        "conflicts": [serialize(item) for item in conflicts],
        "reason": resolution.reason,
        "warnings": _fact_resolution_warnings(
            selected=selected,
            conflicts=conflicts,
            state=state,
        ),
    }


def _fact_resolution_warnings(
    *,
    selected: SourceRecord | None,
    conflicts: tuple[SourceRecord, ...],
    state: str,
) -> list[str]:
    warnings: list[str] = []
    if selected is None:
        warnings.append("没有授权资料可以确认该事实。")
    if conflicts:
        warnings.append("存在冲突来源，当前结论不能视为无条件确定。")
    if state == "stale":
        warnings.append("当前最佳来源可能已过期，不能视为最新事实。")
    return warnings


def _is_stale(observed_at: datetime, *, now: datetime | None = None) -> bool:
    current = now or datetime.now(timezone.utc)
    observed = observed_at if observed_at.tzinfo else observed_at.replace(tzinfo=timezone.utc)
    return current - observed > timedelta(days=30)


class AuthorizedEvidenceTool(BaseTool):
    name = "authorized_evidence"
    description = "List ACL-authorized evidence for the current project."
    category = ToolCategory.READ
    required_permission = "project:read"

    def __init__(
        self,
        *,
        project: ProjectRef,
        access: AccessContext,
        gateway: ReadContextGateway,
        ledger: InvestigationLedger,
        effective_scope: EffectiveAccessScope | None = None,
    ) -> None:
        self._project = project
        self._access = access
        self._gateway = gateway
        self._ledger = ledger
        self._scope = effective_scope

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
            "required": [],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        limit = int(kwargs.get("limit") or 20)
        evidence = self._gateway.authorized_evidence(
            self._project,
            self._access,
            limit=min(max(limit, 1), 50),
            scope=self._scope,
        )
        self._ledger.add_many(evidence)
        self._ledger.record_tool(self.name, {"hit_count": len(evidence)})
        rows = [
            {
                "evidence_id": str(item.id),
                "type": item.type.value,
                "source_id": item.source.source_id,
                "snippet": item.content[:300],
            }
            for item in evidence[:12]
        ]
        return json.dumps({"evidence": rows}, ensure_ascii=False)


class ListKnowledgeGapsTool(BaseTool):
    name = "list_knowledge_gaps"
    description = "List knowledge gaps for the current project."
    category = ToolCategory.READ
    required_permission = "project:read"

    def __init__(
        self,
        *,
        project: ProjectRef,
        access: AccessContext,
        gateway: ReadContextGateway,
        ledger: InvestigationLedger,
        effective_scope: EffectiveAccessScope | None = None,
    ) -> None:
        self._project = project
        self._access = access
        self._gateway = gateway
        self._ledger = ledger
        self._scope = effective_scope

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        report = self._gateway.list_knowledge_gaps(
            self._project,
            self._access,
            scope=self._scope,
        )
        self._ledger.record_tool(self.name, {"gap_count": len(report.gaps)})
        gaps = [
            {
                "type": gap.type.value,
                "title": gap.title,
                "description": gap.description[:200],
            }
            for gap in report.gaps[:10]
        ]
        return json.dumps({"gaps": gaps}, ensure_ascii=False)


class ReadProjectFileTool(BaseTool):
    name = "read_project_file"
    description = "Read a project file under the ProjectSpace allowlist (read-only)."
    category = ToolCategory.READ
    required_permission = "project:read"

    def __init__(
        self,
        *,
        project: ProjectRef,
        gateway: ToolGateway,
        ledger: InvestigationLedger,
        access_scope: str,
        effective_scope: EffectiveAccessScope | None = None,
    ) -> None:
        self._project = project
        self._gateway = gateway
        self._ledger = ledger
        self._access_scope = access_scope
        self._scope = effective_scope

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        relative = str(kwargs["path"]).replace("\\", "/").lstrip("./")
        _enforce_file_scope(self._scope, self.name, relative)
        content = self._gateway.read_project_file(relative)
        clipped = content[:8000]
        digest = sha256(clipped.encode("utf-8")).hexdigest()
        evidence = Evidence(
            type=EvidenceType.CODE,
            project=self._project,
            source=SourceRef(system="repository", source_id=relative),
            content=clipped if clipped.strip() else "(empty file)",
            observed_at=datetime.now(timezone.utc),
            access_scope=self._access_scope,
            content_hash=digest[:32],
            metadata={"path": relative, "tool": "read_project_file"},
        )
        self._ledger.add_evidence(evidence)
        self._ledger.record_tool(self.name, {"path": relative, "chars": len(clipped)})
        return json.dumps(
            {
                "path": relative,
                "evidence_id": str(evidence.id),
                "content": clipped[:2000],
            },
            ensure_ascii=False,
        )


def _code_snippet_evidence(
    *,
    project: ProjectRef,
    access_scope: str,
    source_id: str,
    content: str,
    tool: str,
    metadata: dict[str, Any] | None = None,
) -> Evidence:
    clipped = content[:8000] if content.strip() else "(empty)"
    digest = sha256(clipped.encode("utf-8")).hexdigest()
    meta = {"tool": tool, **(metadata or {})}
    return Evidence(
        type=EvidenceType.CODE,
        project=project,
        source=SourceRef(system="repository", source_id=source_id[:500]),
        content=clipped,
        observed_at=datetime.now(timezone.utc),
        access_scope=access_scope,
        content_hash=digest[:32],
        metadata=meta,
    )


class ListProjectFilesTool(BaseTool):
    name = "list_project_files"
    description = "List files under ProjectSpace file_allowlist (read-only)."
    category = ToolCategory.READ
    required_permission = "project:read"

    def __init__(
        self,
        *,
        project: ProjectRef,
        gateway: ToolGateway,
        ledger: InvestigationLedger,
        access_scope: str,
        effective_scope: EffectiveAccessScope | None = None,
    ) -> None:
        self._project = project
        self._gateway = gateway
        self._ledger = ledger
        self._access_scope = access_scope
        self._scope = effective_scope

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prefix": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": [],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        prefix = str(kwargs.get("prefix") or "src/")
        limit = int(kwargs.get("limit") or 50)
        _enforce_file_scope(self._scope, self.name, prefix)
        files = self._gateway.list_project_files(prefix=prefix, limit=limit)
        listing = "\n".join(files) if files else "(no files)"
        evidence = _code_snippet_evidence(
            project=self._project,
            access_scope=self._access_scope,
            source_id=f"list:{prefix}",
            content=listing,
            tool=self.name,
            metadata={"prefix": prefix, "file_count": len(files)},
        )
        self._ledger.add_evidence(evidence)
        self._ledger.record_tool(
            self.name, {"prefix": prefix, "file_count": len(files)}
        )
        return json.dumps(
            {
                "prefix": prefix,
                "files": files,
                "evidence_id": str(evidence.id),
            },
            ensure_ascii=False,
        )


def _enforce_file_scope(
    scope: EffectiveAccessScope | None,
    tool_name: str,
    relative_path: str,
) -> None:
    if scope is None:
        raise PermissionError("effective access scope is required")
    allowed = tool_name in scope.allowed_tools or (
        tool_name == "list_project_files" and "read_project_file" in scope.allowed_tools
    )
    if not allowed:
        raise PermissionError(f"tool {tool_name} is not allowed for this scope")
    if not source_path_allowed(scope, relative_path):
        raise PermissionError("source path is outside readable effective scope")
