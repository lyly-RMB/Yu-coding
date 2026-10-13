"""ProjectAnswer -> compact external envelope (projection only; no new facts)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from project_lens.application.audience_views import render_audience_view
from project_lens.domain.models import (
    AgentRun,
    ClaimType,
    Evidence,
    EvidenceType,
    ProjectAnswer,
    ProjectRef,
)
from project_lens.integrations.feishu.audiences import AnswerAudience
from project_lens.project_space.policies import effective_scope_from_audit_dict
from project_lens.runtime.events import AgentEvent, AgentEventType

ROLE_VIEWS_AVAILABLE: tuple[str, ...] = tuple(item.value for item in AnswerAudience)

_SUMMARY_MAX = 600
_CITATION_SUMMARY_MAX = 160
_FACT_TEXT_MAX = 400


def project_answer_to_envelope(
    *,
    run: AgentRun,
    answer: ProjectAnswer,
    events: tuple[AgentEvent, ...] = (),
    tool_names: tuple[str, ...] | None = None,
    audience: str = "team",
    format: str = "concise",
) -> dict[str, Any]:
    """Map a verified ProjectAnswer into a Hermes/MCP-safe compact envelope."""

    del format
    audience_view = None
    scope = None
    if run.runtime_access is not None and run.channel_id:
        scope = effective_scope_from_audit_dict(
            run.runtime_access,
            project=run.project,
            actor_id=run.user_id,
            chat_id=run.channel_id,
        )
        audience_view = render_audience_view(
            answer,
            role=scope.role,
            chat_type=scope.chat_type,
            scope=scope,
            audience=None if audience in {"", "team"} else audience,
        )
    evidence_by_id = {item.id: item for item in answer.evidence}
    facts: list[dict[str, Any]] = []
    inferences: list[str] = []
    unknowns: list[str] = list(answer.unknowns)
    citation_ids: set[str] = set()

    for claim in answer.claims:
        if claim.type == ClaimType.FACT:
            cites = [str(eid) for eid in claim.evidence_ids if eid in evidence_by_id]
            # Empty tool observations must not be presented as project facts.
            if cites and all(
                _is_empty_observation(evidence_by_id[UUID(cid)]) for cid in cites
            ):
                unknowns.append(
                    "当前检索未找到可用项目资料；"
                    f"相关工具结果为空：{claim.text[:120]}"
                )
                continue
            facts.append(
                {
                    "text": _truncate(claim.text, _FACT_TEXT_MAX),
                    "citations": cites,
                }
            )
            citation_ids.update(cites)
        elif claim.type == ClaimType.INFERENCE:
            inferences.append(_truncate(claim.text, _FACT_TEXT_MAX))

    if not facts and not inferences and not unknowns:
        unknowns.append("调查结束，但没有形成可展示的结论；请补充资料或更具体的文件路径。")

    citations = [
            _citation_entry(
                evidence_by_id[UUID(cid)],
                redact_body=(audience_view is not None and audience_view.role.value not in {"developer", "ops"}),
            )
        for cid in sorted(citation_ids)
        if UUID(cid) in evidence_by_id
    ]
    # Also include any evidence referenced but not yet listed (bounded).
    for item in answer.evidence:
        if str(item.id) not in citation_ids and len(citations) < 12:
            if _is_empty_observation(item):
                continue
            citations.append(
                _citation_entry(
                    item,
                    redact_body=(audience_view is not None and audience_view.role.value not in {"developer", "ops"}),
                )
            )
            citation_ids.add(str(item.id))

    resolved_tools = tool_names if tool_names is not None else _tool_names_from_events(events)
    return {
        "ok": True,
        "project": _project_payload(answer.project or run.project),
        "run_id": str(run.id),
        "trace_id": str(run.trace_id),
        "answer_text": answer.answer_text or answer.business_summary or answer.conclusion,
        "answer_summary": _truncate(
            audience_view.conclusion
            if audience_view is not None
            else (answer.answer_text or answer.business_summary or answer.conclusion)
            if facts or not unknowns
            else (unknowns[0] if unknowns else answer.business_summary),
            _SUMMARY_MAX,
        ),
        "facts": facts,
        "inferences": inferences,
        "unknowns": unknowns,
        "next_actions": [
            {
                "title": item.title,
                "description": _truncate(item.description, 240),
                "requires_approval": item.requires_approval,
            }
            for item in answer.recommended_actions[:8]
        ],
        "citations": citations[:12],
        "audience_view": audience_view.to_dict() if audience_view is not None else None,
        "audit_ref": {
            "trace_id": str(run.trace_id),
            "run_id": str(run.id),
            "agent_mode": run.runtime,
            "runtime": run.runtime,
            "entry_mode": run.entry_mode,
            "hermes_loop_id": str(run.hermes_loop_id) if run.hermes_loop_id else None,
            "tool_names": list(resolved_tools),
            "allow_apply": False,
            "skill": answer.skill,
            "status": run.status.value if hasattr(run.status, "value") else str(run.status),
            **(
                {
                    "role": audience_view.role.value,
                    "chat_type": audience_view.chat_type,
                    "policy_used": audience_view.policy_used,
                }
                if audience_view is not None
                else {}
            ),
        },
        "role_views_available": list(ROLE_VIEWS_AVAILABLE),
    }


def recoverable_error(
    *,
    error_code: str,
    message: str,
    retryable: bool,
    agent_recovery_hint: str,
    project: ProjectRef | None = None,
    allow_apply: bool = False,
    extras: dict[str, Any] | None = None,
    runtime: str = "hermes",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "error_code": error_code,
        "message": message,
        "retryable": retryable,
        "agent_recovery_hint": agent_recovery_hint,
        "audit_ref": {
            "agent_mode": runtime,
            "runtime": runtime,
            "allow_apply": allow_apply,
            "tool_names": [],
        },
        "role_views_available": list(ROLE_VIEWS_AVAILABLE),
    }
    if project is not None:
        payload["project"] = _project_payload(project)
    if extras:
        payload.update(extras)
    return payload


def _project_payload(project: ProjectRef) -> dict[str, Any]:
    return {
        "tenant_id": project.tenant_id,
        "project_id": project.project_id,
        "service": project.service,
        "environment": project.environment,
    }


def _is_empty_observation(item: Evidence) -> bool:
    """True when evidence is an empty tool observation, not project content."""

    content = item.content.strip().lower()
    if not content:
        return True
    markers = (
        "(no files)",
        "(no hits",
        "no hits for",
        "(empty)",
        "no matching",
    )
    return any(marker in content for marker in markers)


def _citation_entry(item: Evidence, *, redact_body: bool = False) -> dict[str, Any]:
    kind = _evidence_kind(item)
    if item.type in {EvidenceType.CODE, EvidenceType.LOG}:
        summary = "授权来源已记录；当前角色不展示正文。"
    else:
        summary = _truncate(item.content.strip().replace("\n", " "), _CITATION_SUMMARY_MAX)
    return {
        "id": str(item.id),
        "kind": kind,
        "source_uri": f"{item.source.system}:{item.source.source_id}",
        "summary": summary,
    }


def _evidence_kind(item: Evidence) -> str:
    mapping = {
        EvidenceType.DOCUMENT: "doc",
        EvidenceType.CODE: "file",
        EvidenceType.COMMIT: "commit",
        EvidenceType.PULL_REQUEST: "commit",
        EvidenceType.LOG: "tool_result",
        EvidenceType.TASK: "doc",
        EvidenceType.INCIDENT: "doc",
        EvidenceType.METRIC: "tool_result",
    }
    return mapping.get(item.type, "tool_result")


def _tool_names_from_events(events: tuple[AgentEvent, ...]) -> tuple[str, ...]:
    names: list[str] = []
    seen: set[str] = set()
    for event in events:
        payload = event.payload or {}
        tool = payload.get("tool")
        lifecycle = payload.get("lifecycle")
        is_tool = event.type in {
            AgentEventType.TOOL_STARTED,
            AgentEventType.TOOL_COMPLETED,
            AgentEventType.TOOL_DENIED,
        } or (
            event.type == AgentEventType.LIFECYCLE
            and isinstance(lifecycle, str)
            and lifecycle.startswith("tool.")
        )
        if not is_tool or not isinstance(tool, str) or not tool:
            continue
        if tool not in seen:
            seen.add(tool)
            names.append(tool)
    return tuple(names)


def _truncate(text: str, limit: int) -> str:
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)].rstrip() + "…"
