"""In-process MCP handlers for ProjectLens tools.

Callable without the optional ``mcp`` package so unit tests stay lightweight.
Formal Agent tool loop uses the five projectlens_* read-only tools via
ProjectAgentToolService (same logic as GET/POST /project-agent/tools[/call]).
projectlens_ask_project remains debug/smoke only and requires a trusted actor
from the MCP transport adapter.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from project_lens.application.project_agent_ask import (
    ProjectAgentAskRequest,
    ProjectAgentAskService,
)
from project_lens.application.project_agent_tools import (
    ProjectAgentToolCallRequest,
    ProjectAgentToolService,
)
from project_lens.domain.models import ProjectRef
from project_lens.domain.identity import ActorContext

# Debug / smoke umbrella tool — not part of the formal Agent tool loop.
TOOL_NAME = "projectlens_ask_project"

TOOL_DESCRIPTION = (
    "[DEBUG/SMOKE] One-shot ProjectLens ask for smoke tests and slash-command "
    "debugging. Do not use this as the formal Hermes Agent tool loop. "
    "Prefer the projectlens_* read-only tools from list_projectlens_tools / "
    "GET /api/v1/project-agent/tools. Apply / PR / deploy / rollback / restart "
    "remain disabled."
)

FORMAL_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "projectlens_search_context",
        "projectlens_read_project_file",
        "projectlens_query_graph",
        "projectlens_authorized_evidence",
        "projectlens_list_knowledge_gaps",
    }
)
MEMORY_TOOL_NAMES: frozenset[str] = frozenset({
    "projectlens_search_project_memory",
    "projectlens_get_memory_detail",
})
HISTORY_TOOL_NAMES: frozenset[str] = frozenset({
    "projectlens_search_project_history",
    "projectlens_get_run_detail",
})
CITATION_TOOL_NAMES: frozenset[str] = frozenset({
    "projectlens_get_citation_body",
})


def build_ask_service_from_app(app: FastAPI) -> ProjectAgentAskService:
    service = getattr(app.state, "project_agent_ask_service", None)
    if service is None:
        raise RuntimeError("app.state.project_agent_ask_service is not configured")
    return service


def build_tool_service_from_app(app: FastAPI) -> ProjectAgentToolService:
    service = getattr(app.state, "project_agent_tool_service", None)
    if service is None:
        raise RuntimeError("app.state.project_agent_tool_service is not configured")
    return service


def list_projectlens_tools(service: ProjectAgentToolService) -> dict[str, Any]:
    """Return the formal read-only tool catalog (same as GET /project-agent/tools)."""

    return service.list_tools()


async def call_projectlens_tool(
    service: ProjectAgentToolService,
    *,
    tool_name: str,
    project_id: str,
    tenant_id: str = "demo",
    user_id: str = "mcp-user",
    chat_id: str = "mcp-chat",
    arguments: dict[str, Any] | None = None,
    service_name: str | None = None,
    environment: str | None = None,
    actor: ActorContext | None = None,
) -> dict[str, Any]:
    """Call one formal ProjectLens tool via ProjectAgentToolService.

    Same path as POST /api/v1/project-agent/tools/call — no direct EvidenceIndex,
    graph store, or filesystem access from the MCP adapter.
    """

    if actor is None:
        return _trusted_actor_required()
    if actor.tenant_key != tenant_id:
        return _actor_tenant_mismatch(actor=actor, tenant_id=tenant_id)
    return await service.call_tool(
        ProjectAgentToolCallRequest(
            tool_name=tool_name,
            project=ProjectRef(
                tenant_id=tenant_id,
                project_id=project_id,
                service=service_name,
                environment=environment,
            ),
            user_id=actor.actor_id,
            chat_id=actor.chat_id,
            chat_type=actor.chat_type,
            identity_source=actor.source,
            arguments=arguments or {},
        )
    )


async def ask_project(
    service: ProjectAgentAskService,
    *,
    question: str,
    project_id: str,
    tenant_id: str = "demo",
    user_id: str = "mcp-user",
    channel_id: str | None = None,
    audience: str = "team",
    format: str = "concise",
    service_name: str | None = None,
    environment: str | None = None,
    actor: ActorContext | None = None,
) -> dict[str, Any]:
    """Debug/smoke one-shot ask via ProjectAgentAskService (not formal tool loop)."""

    if actor is None:
        return _trusted_actor_required()
    if actor.tenant_key != tenant_id:
        return _actor_tenant_mismatch(actor=actor, tenant_id=tenant_id)
    return await service.ask(
        ProjectAgentAskRequest(
            question=question,
            project=ProjectRef(
                tenant_id=tenant_id,
                project_id=project_id,
                service=service_name,
                environment=environment,
            ),
            user_id=user_id,
            channel_id=channel_id,
            audience=audience,
            mode="read_only",
            format=format,
            actor=actor,
        )
    )


def _trusted_actor_required() -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": "TRUSTED_ACTOR_REQUIRED",
        "message": "MCP calls require a trusted transport actor.",
        "retryable": False,
        "agent_recovery_hint": (
            "Configure MCP_TRUSTED_TENANT_ID, MCP_TRUSTED_ACTOR_ID, and "
            "MCP_TRUSTED_CHAT_ID, or inject a trusted actor resolver."
        ),
        "audit_ref": {"runtime": "hermes", "allow_apply": False, "tool_names": []},
    }


def _actor_tenant_mismatch(*, actor: ActorContext, tenant_id: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": "ACCESS_DENIED",
        "message": "trusted MCP actor tenant does not match the requested project",
        "retryable": False,
        "agent_recovery_hint": "Use a project in the trusted MCP actor tenant.",
        "audit_ref": {
            "runtime": "hermes",
            "allow_apply": False,
            "tool_names": [],
            "actor_id": actor.actor_id,
            "actor_tenant_id": actor.tenant_key,
            "requested_tenant_id": tenant_id,
        },
    }
