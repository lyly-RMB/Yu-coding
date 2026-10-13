"""Read-lane ContextEngine facade with tool-spec gates and audit.

Workflow must collect project knowledge through this gateway (or ToolGateway for
engineering file reads). Feishu adapters must not call ContextEngine / EvidenceIndex.
Apply remains disabled on every read tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from project_lens.context.engine import ContextEngine
from project_lens.context.knowledge_gaps import build_knowledge_gap_report
from project_lens.context.models import AccessContext, ContextQuery, EvidenceBundle
from project_lens.context.authority import AuthorityResolution, resolve_fact as resolve_authority_fact
from project_lens.context.source_records import FactType, SourceRecord
from project_lens.domain.models import Evidence, KnowledgeGapReport, ProjectRef
from project_lens.domain.ops import OpsFinding, OpsQuery
from project_lens.project_space.policies import EffectiveAccessScope, source_path_allowed
from project_lens.runtime.policy import RiskClass
from project_lens.runtime.tool_gateway import ToolAuditEvent
from project_lens.runtime.tool_specs import (
    ToolLane,
    assert_tool_callable,
    list_tool_specs,
)


@dataclass
class ReadContextGateway:
    """Thin ACL + audit wrapper around ContextEngine read tools."""

    engine: ContextEngine
    allow_apply: bool = False
    require_scope: bool = False
    audit_events: list[ToolAuditEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.allow_apply:
            raise ValueError("ReadContextGateway refuses allow_apply=True")

    def list_read_specs(self):
        return list_tool_specs(category=ToolLane.READ)

    def search_context(
        self,
        query: ContextQuery,
        access: AccessContext,
        scope: EffectiveAccessScope | None = None,
    ) -> EvidenceBundle:
        assert_tool_callable("search_context", allow_apply=False)
        self._enforce_scope(scope, "search_context", query.project)
        bundle = self.engine.search(query, access)
        if scope is not None:
            visible_hits = tuple(
                hit for hit in bundle.hits if _evidence_allowed(scope, hit.evidence)
            )
            hidden_count = len(bundle.hits) - len(visible_hits)
            warnings = bundle.warnings
            if hidden_count:
                warnings = (*warnings, "some results were hidden by effective access scope")
            bundle = bundle.model_copy(
                update={
                    "hits": visible_hits,
                    "warnings": warnings,
                    "retrieval_trace": {
                        **bundle.retrieval_trace,
                        "scope_hidden_count": hidden_count,
                        "scope_visible_count": len(visible_hits),
                    },
                }
            )
        self._audit(
            "search_context",
            _audit_arguments(query.project, scope, {
                "query": query.text[:200],
                "limit": query.limit,
                "hit_count": len(bundle.hits),
            }),
            f"hits={len(bundle.hits)}",
            True,
        )
        return bundle

    def authorized_evidence(
        self,
        project: ProjectRef,
        access: AccessContext,
        *,
        limit: int = 50,
        scope: EffectiveAccessScope | None = None,
    ) -> tuple[Evidence, ...]:
        assert_tool_callable("authorized_evidence", allow_apply=False)
        self._enforce_scope(scope, "authorized_evidence", project)
        evidence = self.engine.authorized_evidence(project, access, limit=limit)
        if scope is not None:
            evidence = tuple(item for item in evidence if _evidence_allowed(scope, item))
        self._audit(
            "authorized_evidence",
            _audit_arguments(project, scope, {
                "limit": limit,
                "hit_count": len(evidence),
            }),
            f"hit_count={len(evidence)}",
            True,
        )
        return evidence

    def resolve_fact(
        self,
        project: ProjectRef,
        access: AccessContext,
        fact_type: FactType,
        scope: EffectiveAccessScope | None = None,
    ) -> AuthorityResolution:
        """Resolve one structured project fact after ACL and chat-scope filtering.

        This is intentionally exposed through the existing search read lane. It
        gives Hermes a deterministic fact lookup for high-value questions without
        adding a second tool surface or allowing the model to choose authority
        rules itself.
        """

        assert_tool_callable("search_context", allow_apply=False)
        self._enforce_scope(scope, "search_context", project)
        if scope is None:
            result = self.engine.resolve_fact(project, access, fact_type)
        else:
            resolved = self.engine.resolve_fact(project, access, fact_type)
            records = tuple(
                item
                for item in (*resolved.candidates, *resolved.conflicts)
                if _source_record_allowed(scope, item)
            )
            result = resolve_authority_fact(records, fact_type)
        self._audit(
            "search_context",
            _audit_arguments(
                project,
                scope,
                {
                    "mode": "fact_resolution",
                    "fact_type": fact_type.value,
                    "candidate_count": len(result.candidates),
                    "conflict_count": len(result.conflicts),
                    "selected_source_id": result.selected.source_id if result.selected else None,
                },
            ),
            f"fact_type={fact_type.value}; selected={result.selected.source_id if result.selected else 'none'}",
            True,
        )
        return result

    def list_knowledge_gaps(
        self,
        project: ProjectRef,
        access: AccessContext,
        scope: EffectiveAccessScope | None = None,
    ) -> KnowledgeGapReport:
        assert_tool_callable("list_knowledge_gaps", allow_apply=False)
        self._enforce_scope(scope, "list_knowledge_gaps", project)
        if scope is None:
            report = self.engine.knowledge_gaps(project, access)
        else:
            evidence = self.engine.authorized_evidence(project, access, limit=50)
            visible = tuple(item for item in evidence if _evidence_allowed(scope, item))
            report = build_knowledge_gap_report(visible, project=project)
        self._audit(
            "list_knowledge_gaps",
            _audit_arguments(project, scope, {"gap_count": len(report.gaps)}),
            f"gap_count={len(report.gaps)}",
            True,
        )
        return report

    def query_ops(
        self,
        query: OpsQuery,
        access: AccessContext,
        scope: EffectiveAccessScope | None = None,
    ) -> OpsFinding:
        """Windowed ops query; audited as query_logs (ephemeral, not long-term RAG)."""

        assert_tool_callable("query_logs", allow_apply=False)
        self._enforce_scope(scope, "query_logs", query.project)
        finding = self.engine.query_ops(query, access)
        self._audit(
            "query_logs",
            _audit_arguments(query.project, scope, {
                "signal_count": len(finding.signals),
                "evidence_count": len(finding.evidence),
                "ephemeral": True,
            }),
            f"signals={len(finding.signals)}",
            True,
        )
        return finding

    def assert_source_readable(
        self,
        scope: EffectiveAccessScope,
        path: str,
    ) -> None:
        """Reject reads before ToolGateway when path is outside effective scope."""

        if not source_path_allowed(scope, path):
            raise PermissionError("source path is outside readable effective scope")

    def audit_summary(self) -> dict[str, Any]:
        names = [event.tool_name for event in self.audit_events]
        return {
            "read_tool_calls": len(self.audit_events),
            "read_tool_names": names,
            "allow_apply": False,
            "ok": all(event.ok for event in self.audit_events),
            "apply_events": [
                event.tool_name
                for event in self.audit_events
                if event.risk_class == RiskClass.APPLY
            ],
        }

    def reset_audit(self) -> None:
        self.audit_events.clear()

    def _enforce_scope(
        self,
        scope: EffectiveAccessScope | None,
        tool_name: str,
        project: ProjectRef,
    ) -> None:
        if scope is None:
            if self.require_scope:
                raise PermissionError("effective access scope is required")
            return
        if tool_name not in scope.allowed_tools:
            raise PermissionError(f"tool {tool_name} is not allowed for this actor/chat scope")
        if (
            project.tenant_id != scope.project.tenant_id
            or project.project_id != scope.project.project_id
        ):
            raise PermissionError("project mismatch for effective access scope")

    def _audit(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: str,
        ok: bool,
    ) -> None:
        self.audit_events.append(
            ToolAuditEvent(
                id=uuid4(),
                tool_name=tool_name,
                risk_class=RiskClass.READ,
                arguments=arguments,
                result=result[:2_000],
                ok=ok,
            )
        )


def _audit_arguments(
    project: ProjectRef,
    scope: EffectiveAccessScope | None,
    extra: dict[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {"project": _project_ref(project), **extra}
    if scope is not None:
        payload.update(
            {
                "actor_id": scope.actor_id,
                "chat_id": scope.chat_id,
                "role": scope.role.value,
                "identity_source": scope.identity_source,
                "policy_version": scope.policy_version,
                "chat_type": scope.chat_type,
            }
        )
    return payload


def _project_ref(project: ProjectRef) -> dict[str, str | None]:
    return {
        "tenant_id": project.tenant_id,
        "project_id": project.project_id,
        "service": project.service,
        "environment": project.environment,
    }


def _evidence_allowed(scope: EffectiveAccessScope, evidence: Evidence) -> bool:
    return any(source_path_allowed(scope, path) for path in _evidence_paths(evidence))


def _evidence_paths(evidence: Evidence) -> tuple[str, ...]:
    raw = str(
        evidence.metadata.get("path")
        or evidence.metadata.get("file")
        or evidence.source.source_id
    ).replace("\\", "/")
    raw = raw.split("#", 1)[0]
    candidates = [raw]
    virtual_prefix = {
        "document": ("knowledge/", "docs/"),
        "commit": ("commits/",),
        "pull_request": ("pull_requests/",),
        "log": ("logs/",),
        "task": ("tasks/",),
        "incident": ("incidents/",),
        "metric": ("metrics/",),
    }.get(evidence.type.value, ())
    for prefix in virtual_prefix:
        if not raw.startswith(prefix):
            candidates.append(f"{prefix}{raw.lstrip('/')}")
    return tuple(dict.fromkeys(path for path in candidates if path))


def _source_record_allowed(scope: EffectiveAccessScope, record: SourceRecord) -> bool:
    """Apply the same readable-source policy to durable source snapshots."""

    return _evidence_allowed(scope, record.to_evidence(project=scope.project))
