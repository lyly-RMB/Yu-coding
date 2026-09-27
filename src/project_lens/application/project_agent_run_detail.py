"""Read-only run detail projection for Project Agent answers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from project_lens.application.answer_envelope import (
    project_answer_to_envelope,
    recoverable_error,
)
from project_lens.application.genai_trace_service import GenAITraceService
from project_lens.application.run_service import RunService
from project_lens.domain.identity import ActorContext
from project_lens.domain.models import ProjectAnswer, RunStatus
from project_lens.project_space.policies import (
    EffectiveAccessScope,
    effective_scope_from_audit_dict,
    effective_scope_to_audit_dict,
    ProjectRuntimeContextResolver,
)
from project_lens.runtime.events import AgentEvent, AgentEventType


@dataclass(frozen=True)
class ProjectAgentRunDetailRequest:
    run_id: UUID
    actor: ActorContext
    audience: str = "team"


class ProjectAgentRunDetailService:
    """Project-scoped, permission-checked run detail projection."""

    def __init__(
        self,
        *,
        run_service: RunService,
        project_runtime_context_resolver: ProjectRuntimeContextResolver,
        genai_trace_service: GenAITraceService | None = None,
    ) -> None:
        self._run_service = run_service
        self._resolver = project_runtime_context_resolver
        self._genai_trace_service = genai_trace_service

    def run_detail(self, request: ProjectAgentRunDetailRequest) -> dict[str, Any]:
        run = self._run_service.get(request.run_id)
        if run is None:
            return recoverable_error(
                error_code="RUN_NOT_FOUND",
                message=f"run {request.run_id} was not found",
                retryable=False,
                agent_recovery_hint="Retry the question so ProjectLens can create a fresh run.",
                extras={
                    "run_id": str(request.run_id),
                    "http_status": 404,
                },
                allow_apply=False,
            )

        if run.project.tenant_id != request.actor.tenant_key:
            return recoverable_error(
                error_code="ACCESS_DENIED",
                message="actor tenant does not match the requested run",
                retryable=False,
                agent_recovery_hint="Use the same trusted actor that created the run.",
                project=run.project,
                allow_apply=False,
                extras={
                    "run_id": str(run.id),
                    "trace_id": str(run.trace_id),
                    "http_status": 403,
                },
            )

        if run.user_id != request.actor.actor_id or run.channel_id != request.actor.chat_id:
            return recoverable_error(
                error_code="ACCESS_DENIED",
                message="actor or chat does not match the requested run",
                retryable=False,
                agent_recovery_hint="Open run details from the same trusted user and chat.",
                project=run.project,
                allow_apply=False,
                extras={
                    "run_id": str(run.id),
                    "trace_id": str(run.trace_id),
                    "http_status": 403,
                },
            )

        if run.runtime_access is None or run.channel_id is None:
            return recoverable_error(
                error_code="ACCESS_SCOPE_MISSING",
                message="run has no effective access scope for detail replay",
                retryable=False,
                agent_recovery_hint="Ask the project question again with a trusted identity.",
                project=run.project,
                allow_apply=False,
                extras={
                    "run_id": str(run.id),
                    "trace_id": str(run.trace_id),
                    "http_status": 409,
                },
            )

        try:
            stored_scope = effective_scope_from_audit_dict(
                run.runtime_access,
                project=run.project,
                actor_id=run.user_id,
                chat_id=run.channel_id,
            )
        except ValueError as exc:
            return recoverable_error(
                error_code="ACCESS_SCOPE_INVALID",
                message=str(exc),
                retryable=False,
                agent_recovery_hint="Ask the question again so ProjectLens can rebind scope.",
                project=run.project,
                allow_apply=False,
                extras={
                    "run_id": str(run.id),
                    "trace_id": str(run.trace_id),
                    "http_status": 409,
                },
            )

        try:
            resolved = self._resolver.resolve(
                tenant_id=run.project.tenant_id,
                project_id=run.project.project_id,
                chat_id=request.actor.chat_id,
                user_id=request.actor.actor_id,
                chat_type=request.actor.chat_type,
                identity_source=request.actor.source,
            )
            current_scope = resolved.effective_scope
        except PermissionError as exc:
            return recoverable_error(
                error_code="ACCESS_DENIED",
                message=str(exc),
                retryable=False,
                agent_recovery_hint="Use the same trusted actor and project chat that created the run.",
                project=run.project,
                allow_apply=False,
                extras={
                    "run_id": str(run.id),
                    "trace_id": str(run.trace_id),
                    "http_status": 403,
                },
            )

        if not _scopes_match(stored_scope, current_scope):
            return recoverable_error(
                error_code="ACCESS_SCOPE_MISMATCH",
                message="current role or policy version no longer matches this run",
                retryable=False,
                agent_recovery_hint="Ask the question again so ProjectLens can rebind current scope.",
                project=run.project,
                allow_apply=False,
                extras={
                    "run_id": str(run.id),
                    "trace_id": str(run.trace_id),
                    "http_status": 403,
                },
            )

        events = self._run_service.events(run.id)
        tool_names = _tool_names_from_events(events)

        if run.answer is not None:
            return self._completed_detail(
                run=run,
                answer=run.answer,
                events=events,
                tool_names=tool_names,
                audience=request.audience,
                scope=current_scope,
                stored_scope=stored_scope,
            )

        return self._failed_detail(
            run=run,
            events=events,
            tool_names=tool_names,
            audience=request.audience,
            scope=current_scope,
            stored_scope=stored_scope,
        )

    def _attach_genai_trace(self, detail: dict[str, Any], *, run) -> dict[str, Any]:
        """Migration dual-read: prefer GenAI trace json body when present."""

        from project_lens.domain.models import ProjectRef

        if self._genai_trace_service is None:
            detail["genai_trace"] = None
            detail["genai_trace_path"] = None
            return detail
        try:
            trace = self._genai_trace_service.get_by_run_id(run.id)
        except Exception:  # noqa: BLE001
            trace = None
        if trace is None:
            detail["genai_trace"] = None
            detail["genai_trace_path"] = None
            return detail
        matched_path = None
        for record in self._genai_trace_service.list_index_for_project(
            ProjectRef(tenant_id=trace.tenant_id, project_id=trace.project_id),
            limit=200,
        ):
            if record.run_id == run.id or record.trace_id == trace.trace_id:
                matched_path = record.file_path
                break
        detail["genai_trace_path"] = matched_path
        detail["genai_trace"] = {
            "trace_id": str(trace.trace_id),
            "started_at": trace.started_at.isoformat(),
            "ended_at": trace.ended_at.isoformat(),
            "user_instruction": trace.user_instruction,
            "messages": list(trace.messages),
            "ai_replies": list(trace.ai_replies),
            "tool_results": list(trace.tool_results),
            "tools": list(trace.tools),
            "runtime_state": dict(trace.runtime_state),
            "ordered_entry_kinds": [item.kind for item in trace.ordered_entries()],
        }
        return detail

    def _completed_detail(
        self,
        *,
        run,
        answer: ProjectAnswer,
        events: tuple[AgentEvent, ...],
        tool_names: tuple[str, ...],
        audience: str,
        scope: EffectiveAccessScope,
        stored_scope: EffectiveAccessScope,
    ) -> dict[str, Any]:
        envelope = project_answer_to_envelope(
            run=run,
            answer=answer,
            events=events,
            tool_names=tool_names or None,
            audience=audience,
            format="detail",
        )
        citations = list(envelope.get("citations") or [])
        source_summary = _citation_summaries(citations)
        detail = {
            "ok": True,
            "run_id": str(run.id),
            "trace_id": str(run.trace_id),
            "project": envelope.get("project"),
            "status": run.status.value if hasattr(run.status, "value") else str(run.status),
            "runtime": run.runtime,
            "entry_mode": run.entry_mode,
            "verification_state": "verified",
            "answer_summary": envelope.get("answer_summary"),
            "facts": envelope.get("facts") or [],
            "inferences": envelope.get("inferences") or [],
            "unknowns": envelope.get("unknowns") or [],
            "next_actions": envelope.get("next_actions") or [],
            "citations": citations,
            "citation_count": len(citations),
            "source_summary": source_summary,
            "failure_reason": None,
            "audit_ref": _run_audit_ref(
                run=run,
                envelope=envelope,
                tool_names=tool_names,
                scope=scope,
                stored_scope=stored_scope,
                citation_count=len(citations),
                source_summary=source_summary,
            ),
            "role_views_available": envelope.get("role_views_available") or [],
        }
        return self._attach_genai_trace(detail, run=run)

    def _failed_detail(
        self,
        *,
        run,
        events: tuple[AgentEvent, ...],
        tool_names: tuple[str, ...],
        audience: str,
        scope: EffectiveAccessScope,
        stored_scope: EffectiveAccessScope,
    ) -> dict[str, Any]:
        failure_reason = run.error or "Hermes run failed before a verified answer was produced."
        citations: list[dict[str, Any]] = []
        source_summary = _tool_source_summary(tool_names)
        unknowns = [failure_reason]
        if run.status == RunStatus.FAILED:
            unknowns.append("当前运行已失败，详细原因仅在运行详情中展示。")
        detail = {
            "ok": True,
            "run_id": str(run.id),
            "trace_id": str(run.trace_id),
            "project": {
                "tenant_id": run.project.tenant_id,
                "project_id": run.project.project_id,
                "service": run.project.service,
                "environment": run.project.environment,
            },
            "status": run.status.value if hasattr(run.status, "value") else str(run.status),
            "runtime": run.runtime,
            "entry_mode": run.entry_mode,
            "verification_state": "failed" if run.status == RunStatus.FAILED else "unknown",
            "answer_summary": failure_reason,
            "facts": [],
            "inferences": [],
            "unknowns": unknowns,
            "next_actions": [],
            "citations": citations,
            "citation_count": 0,
            "source_summary": source_summary,
            "failure_reason": failure_reason,
            "audit_ref": _run_audit_ref(
                run=run,
                envelope={},
                tool_names=tool_names,
                scope=scope,
                stored_scope=stored_scope,
                citation_count=0,
                source_summary=source_summary,
                verification_state="failed" if run.status == RunStatus.FAILED else "unknown",
                error=failure_reason,
            ),
            "role_views_available": [
                "team",
                "technical",
                "business",
                "qa",
                "manager",
                "ops",
                "onboarding",
                "evidence",
                "debug",
            ],
        }
        return self._attach_genai_trace(detail, run=run)


def _scopes_match(left: EffectiveAccessScope, right: EffectiveAccessScope) -> bool:
    return effective_scope_to_audit_dict(left) == effective_scope_to_audit_dict(right)


def _tool_names_from_events(events: tuple[AgentEvent, ...]) -> tuple[str, ...]:
    names: list[str] = []
    seen: set[str] = set()
    for event in events:
        payload = event.payload or {}
        tool = payload.get("tool")
        is_tool = event.type in {
            AgentEventType.TOOL_STARTED,
            AgentEventType.TOOL_COMPLETED,
            AgentEventType.TOOL_DENIED,
        }
        if not is_tool or not isinstance(tool, str) or not tool:
            continue
        if tool in seen:
            continue
        seen.add(tool)
        names.append(tool)
    return tuple(names)


def _citation_summaries(citations: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in citations[:6]:
        source_uri = str(item.get("source_uri") or "").strip()
        summary = str(item.get("summary") or "").strip()
        if source_uri and summary:
            lines.append(f"{source_uri} · {summary}")
        elif source_uri:
            lines.append(source_uri)
        elif summary:
            lines.append(summary)
    return lines


def _tool_source_summary(tool_names: tuple[str, ...]) -> list[str]:
    if not tool_names:
        return []
    return [f"已调用只读工具：{', '.join(tool_names[:8])}"]


def _run_audit_ref(
    *,
    run,
    envelope: dict[str, Any],
    tool_names: tuple[str, ...],
    scope: EffectiveAccessScope,
    stored_scope: EffectiveAccessScope,
    citation_count: int,
    source_summary: list[str],
    verification_state: str = "verified",
    error: str | None = None,
) -> dict[str, Any]:
    audit = dict(envelope.get("audit_ref") or {})
    audit.update(
        {
            "run_id": str(run.id),
            "trace_id": str(run.trace_id),
            "runtime": run.runtime,
            "entry_mode": run.entry_mode,
            "tool_names": list(tool_names),
            "allow_apply": False,
            "verification_state": verification_state,
            "citation_count": citation_count,
            "source_summary": source_summary[:6],
            "policy_version": scope.policy_version,
            "current_role": scope.role.value,
            "stored_role": stored_scope.role.value,
            "current_visibility_level": scope.visibility_level.value,
            "stored_visibility_level": stored_scope.visibility_level.value,
            "current_chat_type": scope.chat_type,
            "stored_chat_type": stored_scope.chat_type,
        }
    )
    if error:
        audit["error"] = error
    return audit
