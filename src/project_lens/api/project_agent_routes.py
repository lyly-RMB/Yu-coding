"""One-shot Project Agent HTTP routes for Hermes/MCP Kernel interface."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from project_lens.api.trusted_actor import (
    actor_context_from_headers,
    get_trusted_actor_context,
    resolve_project_agent_actor,
)
from project_lens.api.project_agent_schemas import (
    ProjectAgentAskRequestBody,
    ProjectAgentAskResponse,
    ProjectAgentToolCallRequestBody,
    ProjectAgentToolCallResponse,
    ProjectAgentToolsResponse,
    ProjectAgentRoleViewRequestBody,
    ProjectAgentRoleViewResponse,
    ProjectAgentRunDetailRequestBody,
    ProjectAgentRunDetailResponse,
    ProjectAgentKnowledgeGapDigestRequest,
    ProjectAgentKnowledgeGapDigestResponse,
    ProjectAgentKnowledgeGapActionRequest,
    ProjectAgentKnowledgeGapActionStatusRequest,
    ProjectAgentKnowledgeGapTransitionRequest,
)
from project_lens.application.project_agent_ask import (
    ProjectAgentAskRequest,
    ProjectAgentAskService,
)
from project_lens.application.project_agent_tools import (
    ProjectAgentToolCallRequest,
    ProjectAgentToolService,
)
from project_lens.application.project_space_inspect import (
    ProjectSpaceInspectService,
    ProjectSpaceScopeInspectRequest,
)
from project_lens.application.project_agent_role_view import (
    ProjectAgentRoleViewService,
    RoleViewReplayRequest,
)
from project_lens.application.project_agent_run_detail import (
    ProjectAgentRunDetailRequest,
    ProjectAgentRunDetailService,
)
from project_lens.application.knowledge_gap_digest import (
    KnowledgeGapDigestService,
    render_knowledge_gap_digest,
)
from project_lens.application.knowledge_gap_service import KnowledgeGapService
from project_lens.domain.knowledge_gap import GapActionStatus, GapActionType, KnowledgeGapStatus
from project_lens.context.models import AccessContext
from project_lens.project_space.policies import RoleKind

router = APIRouter(tags=["project-agent"])


def get_project_agent_ask_service(request: Request) -> ProjectAgentAskService:
    service = getattr(request.app.state, "project_agent_ask_service", None)
    if service is None:
        raise RuntimeError("project_agent_ask_service is not configured")
    return service


def get_project_agent_role_view_service(request: Request) -> ProjectAgentRoleViewService:
    service = getattr(request.app.state, "project_agent_role_view_service", None)
    if service is None:
        # Same ask RunService is enough for replay (shared repository).
        ask = get_project_agent_ask_service(request)
        return ProjectAgentRoleViewService(run_service=ask.run_service)
    return service


def get_project_agent_run_detail_service(request: Request) -> ProjectAgentRunDetailService:
    service = getattr(request.app.state, "project_agent_run_detail_service", None)
    if service is None:
        ask = get_project_agent_ask_service(request)
        return ProjectAgentRunDetailService(
            run_service=ask.run_service,
            project_runtime_context_resolver=request.app.state.project_runtime_context_resolver,
        )
    return service


def get_project_agent_tool_service(request: Request) -> ProjectAgentToolService:
    service = getattr(request.app.state, "project_agent_tool_service", None)
    if service is None:
        raise RuntimeError("project_agent_tool_service is not configured")
    return service


def get_knowledge_gap_digest_service(request: Request) -> KnowledgeGapDigestService:
    service = getattr(request.app.state, "knowledge_gap_digest_service", None)
    if service is None:
        raise RuntimeError("knowledge_gap_digest_service is not configured")
    return service


def get_knowledge_gap_service(request: Request) -> KnowledgeGapService:
    service = getattr(request.app.state, "knowledge_gap_service", None)
    if service is None:
        raise RuntimeError("knowledge_gap_service is not configured")
    return service


def get_project_space_inspect_service(request: Request) -> ProjectSpaceInspectService:
    service = getattr(request.app.state, "project_space_inspect_service", None)
    if service is None:
        raise RuntimeError("project_space_inspect_service is not configured")
    return service


def _require_knowledge_gap_access(request: Request, actor, project, *, write: bool = False):
    if not actor.authenticated or actor.tenant_key != project.tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="actor/project mismatch")
    try:
        resolved = request.app.state.project_runtime_context_resolver.resolve(
            tenant_id=project.tenant_id,
            project_id=project.project_id,
            chat_id=actor.chat_id,
            user_id=actor.actor_id,
            chat_type=actor.chat_type,
            identity_source=actor.source,
        )
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown project") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    if "list_knowledge_gaps" not in resolved.effective_scope.allowed_tools:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="knowledge-gap access is not allowed")
    if write and resolved.effective_scope.role in {RoleKind.GUEST, RoleKind.ONBOARDING}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="knowledge-gap changes require a project role")
    return resolved


@router.get(
    "/project-agent/tools",
    response_model=ProjectAgentToolsResponse,
)
def project_agent_tools(
    service: ProjectAgentToolService = Depends(get_project_agent_tool_service),
) -> ProjectAgentToolsResponse:
    """List the ProjectLens read-only tool envelope for Hermes/runtime agents."""

    return ProjectAgentToolsResponse.model_validate(service.list_tools())


@router.get("/project-agent/project-spaces")
def project_agent_project_spaces(
    service: ProjectSpaceInspectService = Depends(get_project_space_inspect_service),
) -> dict[str, object]:
    """List ProjectSpace manifests without exposing indexed file/document contents."""

    return service.list_project_spaces()


@router.get("/project-agent/project-spaces/{tenant_id}/{project_id}")
def project_agent_project_space_detail(
    tenant_id: str,
    project_id: str,
    service: ProjectSpaceInspectService = Depends(get_project_space_inspect_service),
) -> dict[str, object]:
    """Inspect one ProjectSpace manifest and validation report."""

    try:
        return service.project_space_detail(tenant_id=tenant_id, project_id=project_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown project space: {tenant_id}/{project_id}",
        ) from exc


@router.get("/project-agent/project-spaces/{tenant_id}/{project_id}/scope")
def project_agent_project_space_scope(
    request: Request,
    tenant_id: str,
    project_id: str,
    user_id: str,
    chat_id: str,
    service: ProjectSpaceInspectService = Depends(get_project_space_inspect_service),
) -> dict[str, object]:
    """Inspect role/chat effective scope before answer generation."""

    actor = resolve_project_agent_actor(
        request,
        body_user_id=user_id,
        body_channel_id=chat_id,
        project_tenant_id=tenant_id,
    )
    try:
        return service.inspect_scope(
            ProjectSpaceScopeInspectRequest(
                tenant_id=tenant_id,
                project_id=project_id,
                user_id=actor.actor_id,
                chat_id=actor.chat_id,
                chat_type=actor.chat_type,
                identity_source=actor.source,
            )
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown project space: {tenant_id}/{project_id}",
        ) from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc


@router.post(
    "/project-agent/tools/call",
    response_model=ProjectAgentToolCallResponse,
)
async def project_agent_tool_call(
    request: Request,
    payload: ProjectAgentToolCallRequestBody,
    service: ProjectAgentToolService = Depends(get_project_agent_tool_service),
) -> ProjectAgentToolCallResponse:
    """Call one ProjectLens read-only tool through ProjectSpace/RolePolicy/Gateway."""

    actor = resolve_project_agent_actor(
        request,
        body_user_id=payload.user_id,
        body_channel_id=payload.chat_id,
        project_tenant_id=payload.project.tenant_id,
    )
    result = await service.call_tool(
        ProjectAgentToolCallRequest(
            tool_name=payload.tool_name,
            project=payload.project,
            user_id=actor.actor_id,
            chat_id=actor.chat_id,
            chat_type=actor.chat_type,
            identity_source=actor.source,
            arguments=payload.arguments,
        )
    )
    return ProjectAgentToolCallResponse.model_validate(result)


@router.post(
    "/project-agent/ask",
    response_model=ProjectAgentAskResponse,
)
async def project_agent_ask(
    request: Request,
    payload: ProjectAgentAskRequestBody,
    service: ProjectAgentAskService = Depends(get_project_agent_ask_service),
) -> ProjectAgentAskResponse:
    """Create + execute a Hermes run and return a compact answer envelope.

    Does not invent facts. Does not query EvidenceIndex / graph / ops directly.
    """

    actor = resolve_project_agent_actor(
        request,
        body_user_id=payload.user_id,
        body_channel_id=payload.channel_id,
        project_tenant_id=payload.project.tenant_id,
    )
    result = await service.ask(
        ProjectAgentAskRequest(
            question=payload.question,
            project=payload.project,
            user_id=actor.actor_id,
            channel_id=actor.chat_id,
            chat_type=actor.chat_type,
            identity_source=actor.source,
            audience=payload.audience,
            mode=payload.mode,
            format=payload.format,
            actor=actor,
        )
    )
    return ProjectAgentAskResponse.model_validate(result)


@router.post(
    "/project-agent/knowledge-gaps/digest/preview",
    response_model=ProjectAgentKnowledgeGapDigestResponse,
)
def project_agent_knowledge_gap_digest_preview(
    request: Request,
    payload: ProjectAgentKnowledgeGapDigestRequest,
    service: KnowledgeGapDigestService = Depends(get_knowledge_gap_digest_service),
) -> ProjectAgentKnowledgeGapDigestResponse:
    """Build a read-only, actor-authorized digest preview; never sends notifications."""

    actor = get_trusted_actor_context(request)
    if not actor.authenticated or actor.tenant_key != payload.project.tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="actor/project mismatch")
    try:
        resolved = request.app.state.project_runtime_context_resolver.resolve(
            tenant_id=payload.project.tenant_id,
            project_id=payload.project.project_id,
            chat_id=actor.chat_id,
            user_id=actor.actor_id,
            chat_type=actor.chat_type,
            identity_source=actor.source,
        )
        if "list_knowledge_gaps" not in resolved.effective_scope.allowed_tools:
            raise PermissionError("knowledge-gap digest preview is not allowed")
        period_start = _parse_digest_datetime(payload.period_start)
        period_end = _parse_digest_datetime(payload.period_end)
        access_scope = resolved.project_space.access_scope or f"project:{payload.project.project_id}:read"
        evidence = request.app.state.context_engine.authorized_evidence(
            resolved.project,
            AccessContext(
                tenant_id=resolved.project.tenant_id,
                user_id=actor.actor_id,
                permissions=frozenset({access_scope}),
            ),
            limit=50,
        )
        digest = service.build(
            resolved.project,
            period_start=period_start,
            period_end=period_end,
            authorized_evidence_ids=frozenset(item.id for item in evidence),
        )
    except HTTPException:
        raise
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown project") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    return ProjectAgentKnowledgeGapDigestResponse(
        project=digest.project,
        period_start=digest.period_start.isoformat(),
        period_end=digest.period_end.isoformat(),
        digest_date=digest.digest_date.isoformat(),
        new_count=len(digest.new_cases),
        ongoing_count=len(digest.ongoing_cases),
        high_impact_count=len(digest.high_impact_cases),
        reopened_count=len(digest.reopened_cases),
        resolved_count=len(digest.resolved_cases),
        summary_hash=digest.summary_hash,
        card=render_knowledge_gap_digest(digest),
    )


@router.post("/project-agent/knowledge-gaps/{gap_id}/actions")
def project_agent_knowledge_gap_action(
    request: Request,
    gap_id: UUID,
    payload: ProjectAgentKnowledgeGapActionRequest,
    service: KnowledgeGapService = Depends(get_knowledge_gap_service),
) -> dict[str, object]:
    actor = get_trusted_actor_context(request)
    _require_knowledge_gap_access(request, actor, payload.project, write=True)
    case = service.get_case(gap_id)
    if case is None or case.project.tenant_id != payload.project.tenant_id or case.project.project_id != payload.project.project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="knowledge gap not found")
    try:
        action = service.propose_action(gap_id, action_type=GapActionType(payload.action_type), proposed_by=actor.actor_id, description=payload.description, target_owner_id=payload.target_owner_id, linked_task_id=payload.linked_task_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return action.model_dump(mode="json")


@router.post("/project-agent/knowledge-gaps/actions/{action_id}/status")
def project_agent_knowledge_gap_action_status(
    request: Request,
    action_id: UUID,
    payload: ProjectAgentKnowledgeGapActionStatusRequest,
    service: KnowledgeGapService = Depends(get_knowledge_gap_service),
) -> dict[str, object]:
    actor = get_trusted_actor_context(request)
    _require_knowledge_gap_access(request, actor, payload.project, write=True)
    action = service.get_action(action_id)
    case = service.get_case(action.gap_id) if action else None
    if action is None or case is None or case.project.tenant_id != payload.project.tenant_id or case.project.project_id != payload.project.project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="knowledge gap action not found")
    try:
        updated = service.update_action(action_id, GapActionStatus(payload.status), actor_id=actor.actor_id, reason=payload.reason)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return updated.model_dump(mode="json")


@router.post("/project-agent/knowledge-gaps/{gap_id}/transition")
def project_agent_knowledge_gap_transition(
    request: Request,
    gap_id: UUID,
    payload: ProjectAgentKnowledgeGapTransitionRequest,
    service: KnowledgeGapService = Depends(get_knowledge_gap_service),
) -> dict[str, object]:
    actor = get_trusted_actor_context(request)
    _require_knowledge_gap_access(request, actor, payload.project, write=True)
    case = service.get_case(gap_id)
    if case is None or case.project.tenant_id != payload.project.tenant_id or case.project.project_id != payload.project.project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="knowledge gap not found")
    try:
        updated = service.transition(gap_id, KnowledgeGapStatus(payload.status), actor_id=actor.actor_id, reason=payload.reason)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return updated.model_dump(mode="json")


def _parse_digest_datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("period timestamps must be valid ISO-8601 datetimes") from exc


@router.post(
    "/project-agent/runs/{run_id}/role-view",
    response_model=ProjectAgentRoleViewResponse,
)
async def project_agent_role_view(
    request: Request,
    run_id: UUID,
    payload: ProjectAgentRoleViewRequestBody,
    service: ProjectAgentRoleViewService = Depends(get_project_agent_role_view_service),
) -> ProjectAgentRoleViewResponse | JSONResponse:
    """Replay RoleView Markdown for an existing completed run.

    Does not execute investigation again. Does not invent facts.
    """

    try:
        actor = get_trusted_actor_context(request)
    except HTTPException:
        actor = actor_context_from_headers(request)
    result = service.role_view(
        RoleViewReplayRequest(run_id=run_id, audience=payload.audience, actor=actor)
    )
    http_status = result.pop("http_status", None) if isinstance(result, dict) else None
    body = ProjectAgentRoleViewResponse.model_validate(result)
    if body.ok:
        return body
    code = {
        400: status.HTTP_400_BAD_REQUEST,
        404: status.HTTP_404_NOT_FOUND,
        409: status.HTTP_409_CONFLICT,
    }.get(int(http_status) if http_status else 0)
    if code is None:
        # Fallback: map known error codes when http_status was omitted.
        if body.error_code == "RUN_NOT_FOUND":
            code = status.HTTP_404_NOT_FOUND
        elif body.error_code == "RUN_NOT_READY":
            code = status.HTTP_409_CONFLICT
        elif body.error_code == "INVALID_AUDIENCE":
            code = status.HTTP_400_BAD_REQUEST
        elif body.error_code in {"ACCESS_DENIED", "ACCESS_SCOPE_MISSING", "ACCESS_SCOPE_INVALID"}:
            code = status.HTTP_403_FORBIDDEN
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=body.message or "role-view failed",
            )
    return JSONResponse(status_code=code, content=body.model_dump(mode="json"))


@router.post(
    "/project-agent/runs/{run_id}/detail",
    response_model=ProjectAgentRunDetailResponse,
)
async def project_agent_run_detail(
    request: Request,
    run_id: UUID,
    payload: ProjectAgentRunDetailRequestBody,
    service: ProjectAgentRunDetailService = Depends(get_project_agent_run_detail_service),
) -> ProjectAgentRunDetailResponse | JSONResponse:
    """Replay a safe run detail view for the existing completed run."""

    try:
        actor = get_trusted_actor_context(request)
    except HTTPException:
        actor = actor_context_from_headers(request)
        if actor is None:
            raise
    run = service.run_detail(
        ProjectAgentRunDetailRequest(
            run_id=run_id,
            actor=actor,
            audience=payload.audience,
        )
    )
    http_status = run.pop("http_status", None) if isinstance(run, dict) else None
    body = ProjectAgentRunDetailResponse.model_validate(run)
    if body.ok:
        return body
    code = {
        400: status.HTTP_400_BAD_REQUEST,
        403: status.HTTP_403_FORBIDDEN,
        404: status.HTTP_404_NOT_FOUND,
        409: status.HTTP_409_CONFLICT,
    }.get(int(http_status) if http_status else 0)
    if code is None:
        if body.error_code == "RUN_NOT_FOUND":
            code = status.HTTP_404_NOT_FOUND
        elif body.error_code in {"ACCESS_DENIED", "ACCESS_SCOPE_MISMATCH"}:
            code = status.HTTP_403_FORBIDDEN
        elif body.error_code in {"ACCESS_SCOPE_MISSING", "ACCESS_SCOPE_INVALID"}:
            code = status.HTTP_409_CONFLICT
        elif body.error_code == "INVALID_AUDIENCE":
            code = status.HTTP_400_BAD_REQUEST
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=body.message or "run detail failed",
            )
    return JSONResponse(status_code=code, content=body.model_dump(mode="json"))


@router.get("/project-agent/hermes-loops/{run_id}/events")
def project_agent_hermes_loop_events(
    run_id: UUID,
    request: Request,
) -> dict[str, object]:
    """List durable audit events for the real AgentRun behind a Hermes loop."""

    sink = getattr(request.app.state, "event_sink", None)
    bus = getattr(request.app.state, "lifecycle_bus", None)
    events: list[dict[str, object]] = []
    if sink is not None and hasattr(sink, "for_run"):
        for event in sink.for_run(run_id):
            events.append(
                {
                    "run_id": str(event.run_id),
                    "trace_id": str(event.trace_id),
                    "type": event.type.value if hasattr(event.type, "value") else str(event.type),
                    "occurred_at": event.occurred_at.isoformat(),
                    "payload": dict(event.payload or {}),
                }
            )
    elif bus is not None:
        for event in bus.for_run(run_id):
            events.append(
                {
                    "run_id": str(event.run_id) if event.run_id else None,
                    "trace_id": str(event.trace_id) if event.trace_id else None,
                    "type": event.type.value if hasattr(event.type, "value") else str(event.type),
                    "occurred_at": event.occurred_at.isoformat(),
                    "payload": dict(event.payload or {}),
                }
            )
    return {
        "ok": True,
        "run_id": str(run_id),
        "allow_apply": False,
        "mode": "feishu_hermes_llm_tool_loop",
        "event_count": len(events),
        "events": events,
    }
