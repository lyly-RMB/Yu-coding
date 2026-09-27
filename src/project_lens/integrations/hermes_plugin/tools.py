"""Hermes plugin helpers for ProjectLens formal read-only tools.

Registers tools only when the Hermes plugin ctx exposes ``register_tool``.
Uses Hermes kwargs API (name/toolset/schema/handler) when available; falls
back to positional FakeContext style for unit tests. Does not modify Hermes
core. Does not touch EvidenceIndex / graph / filesystem.
"""

from __future__ import annotations

import inspect
import json
from typing import Any, Protocol

from project_lens.integrations.hermes_plugin.client import ProjectLensApiClient
from project_lens.integrations.hermes_plugin.config import ProjectLensPluginConfig

FORMAL_TOOL_NAMES: tuple[str, ...] = (
    "projectlens_search_context",
    "projectlens_read_project_file",
    "projectlens_list_project_files",
    "projectlens_authorized_evidence",
    "projectlens_list_knowledge_gaps",
)
OBSIDIAN_TOOL_NAMES: tuple[str, ...] = (
    "projectlens_search_wiki",
    "projectlens_read_wiki_page",
)
KNOWLEDGE_OPERATION_TOOL_NAMES: tuple[str, ...] = (
    "projectlens_sync_sources",
    "projectlens_export_obsidian",
    "projectlens_lint_wiki",
)
INBOX_PROPOSAL_TOOL_NAMES: tuple[str, ...] = (
    "projectlens_import_obsidian_inbox",
    "projectlens_propose_wiki_update",
)
MEMORY_TOOL_NAMES: tuple[str, ...] = (
    "projectlens_search_project_memory",
    "projectlens_get_memory_detail",
)
HISTORY_TOOL_NAMES: tuple[str, ...] = (
    "projectlens_search_project_history",
    "projectlens_get_run_detail",
)
CITATION_TOOL_NAMES: tuple[str, ...] = (
    "projectlens_get_citation_body",
)
ADVANCED_TOOL_NAMES: tuple[str, ...] = (
    "projectlens_propose_patch_plan",
    "projectlens_propose_test_plan",
    "projectlens_propose_risk_escalation",
    "projectlens_propose_project_todo",
    "projectlens_validate_patch_plan",
    "projectlens_check_diff_scope",
    "projectlens_verify_evidence_links",
)

TOOLSET_NAME = "projectlens"

_CONTEXT_PARAM_PROPERTIES: dict[str, dict[str, Any]] = {
    "tenant_id": {"type": "string", "description": "ProjectLens tenant id (default from plugin config)"},
    "project_id": {
        "type": "string",
        "description": "ProjectSpace id (default from plugin config / chat binding)",
    },
    "chat_id": {"type": "string", "description": "Chat/channel id for ChatVisibilityPolicy"},
    "user_id": {"type": "string", "description": "Actor user id for RolePolicy"},
}

_DEFAULT_PARAMETERS: dict[str, dict[str, Any]] = {
    "projectlens_search_project_memory": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Memory search text"},
            "memory_types": {"type": "array", "items": {"type": "string"}, "description": "Optional memory type filter"},
            "service": {"type": "string", "description": "Optional project service filter"},
            "limit": {"type": "integer", "description": "1-8 cards; server capped"},
        },
        "required": ["query"],
    },
    "projectlens_get_memory_detail": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "Exact memory id returned by projectlens_search_project_memory"},
        },
        "required": ["memory_id"],
    },
    "projectlens_search_project_history": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Historical run search text"},
            "from": {"type": "string", "description": "Optional ISO date lower bound"},
            "to": {"type": "string", "description": "Optional ISO date upper bound"},
            "limit": {"type": "integer", "description": "1-8 historical summaries; server capped"},
        },
        "required": ["query"],
    },
    "projectlens_get_run_detail": {
        "type": "object",
        "properties": {
            "run_id": {"type": "string", "description": "Exact run id returned by project history search"},
        },
        "required": ["run_id"],
    },
    "projectlens_get_citation_body": {
        "type": "object",
        "properties": {
            "citation_id": {
                "type": "string",
                "description": "Exact citation_id from the session citation ledger",
            },
        },
        "required": ["citation_id"],
    },
    "projectlens_search_context": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search text"},
            "limit": {"type": "integer", "description": "1-20 results; default 8"},
            "fact_type": {
                "type": "string",
                "enum": [
                    "requirement_scope",
                    "requirement_status",
                    "development_progress",
                    "test_status",
                    "technical_decision",
                    "owner",
                ],
                "description": "Optional structured fact lookup with authority and conflict detection.",
            },
        },
        "required": ["query"],
    },
    "projectlens_read_project_file": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Project-relative allowlisted path"},
        },
        "required": ["path"],
    },
    "projectlens_list_project_files": {
        "type": "object",
        "properties": {
            "prefix": {"type": "string", "description": "Allowlisted path prefix, e.g. docs/"},
            "limit": {"type": "integer", "description": "Maximum file names"},
        },
        "required": [],
    },
    "projectlens_authorized_evidence": {
        "type": "object",
        "properties": {"limit": {"type": "integer"}},
        "required": [],
    },
    "projectlens_list_knowledge_gaps": {
        "type": "object",
        "properties": {},
        "required": [],
    },
    "projectlens_search_wiki": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "page_types": {"type": "array", "items": {"type": "string"}},
            "statuses": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer"},
        },
        "required": ["query"],
    },
    "projectlens_read_wiki_page": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "max_chars": {"type": "integer"},
        },
        "required": ["path"],
    },
    "projectlens_import_obsidian_inbox": {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Optional controlled Inbox path"}},
        "required": [],
    },
    "projectlens_propose_wiki_update": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "content": {"type": "string"},
            "kind": {"type": "string"},
        },
        "required": ["title", "content"],
    },
}
_ADVANCED_PARAMETERS: dict[str, dict[str, Any]] = {
    "projectlens_propose_project_todo": {"type": "object", "properties": {"title": {"type": "string"}, "description": {"type": "string"}, "owner_ids": {"type": "array", "items": {"type": "string"}}}, "required": ["title", "description"]},
    "projectlens_check_diff_scope": {"type": "object", "properties": {"title": {"type": "string"}, "rationale": {"type": "string"}, "patches": {"type": "array", "items": {"type": "object"}}}, "required": ["title", "rationale", "patches"]},
    "projectlens_verify_evidence_links": {"type": "object", "properties": {"evidence_ids": {"type": "array", "items": {"type": "string"}}, "citation_ids": {"type": "array", "items": {"type": "string"}}}, "required": ["evidence_ids", "citation_ids"]},
}

# Fallback catalog when GET /project-agent/tools is unavailable at register time.
# Names must stay identical to ProjectAgentToolService.list_tools().
_FALLBACK_TOOL_SPECS: tuple[dict[str, Any], ...] = tuple(
    {
        "name": name,
        "description": (
            f"ProjectLens read-only tool `{name}`. "
            "Call via ProjectLens POST /project-agent/tools/call. "
            "Apply / PR / deploy / rollback / restart remain disabled."
        ),
        "category": "read",
        "allow_apply": False,
        "requires_approval": False,
        "parameters": _DEFAULT_PARAMETERS[name],
    }
    for name in FORMAL_TOOL_NAMES
)


class SupportsRegisterTool(Protocol):
    def register_tool(self, *args: Any, **kwargs: Any) -> Any: ...


def ctx_supports_register_tool(ctx: Any) -> bool:
    return callable(getattr(ctx, "register_tool", None))


def register_tool_uses_hermes_kwargs(register_tool: Any) -> bool:
    """True when register_tool looks like Hermes PluginContext.register_tool."""

    try:
        params = inspect.signature(register_tool).parameters
    except (TypeError, ValueError):
        return False
    return "toolset" in params and "schema" in params and "handler" in params


def resolve_tool_catalog(client: ProjectLensApiClient) -> list[dict[str, Any]]:
    """Discover formal tools via GET /project-agent/tools, with safe fallback."""

    envelope = client.list_tools()
    tools = envelope.get("tools") if isinstance(envelope, dict) else None
    if envelope.get("ok") is True and isinstance(tools, list) and tools:
        allowed_names = (
            set(FORMAL_TOOL_NAMES)
            | set(OBSIDIAN_TOOL_NAMES)
            | set(KNOWLEDGE_OPERATION_TOOL_NAMES)
            | set(INBOX_PROPOSAL_TOOL_NAMES)
            | set(MEMORY_TOOL_NAMES)
            | set(HISTORY_TOOL_NAMES)
            | set(CITATION_TOOL_NAMES)
        )
        client_config = getattr(client, "config", None)
        if getattr(client_config, "advanced_tools_enabled", False):
            allowed_names.update(ADVANCED_TOOL_NAMES)
        formal = [
            item
            for item in tools
            if isinstance(item, dict) and item.get("name") in allowed_names
        ]
        if {item["name"] for item in formal}.issuperset(FORMAL_TOOL_NAMES):
            return formal
    fallback = [dict(item) for item in _FALLBACK_TOOL_SPECS]
    client_config = getattr(client, "config", None)
    if getattr(client_config, "advanced_tools_enabled", False):
        fallback.extend({
            "name": name,
            "description": f"Approval-gated Hermes draft/report tool: {name}.",
            "category": "validate" if "validate" in name else "propose",
            "allow_apply": False,
            "requires_approval": "validate" not in name,
            "parameters": _ADVANCED_PARAMETERS[name],
        } for name in ADVANCED_TOOL_NAMES)
    return fallback


def build_openai_tool_schema(spec: dict[str, Any]) -> dict[str, Any]:
    """Build Hermes/OpenAI-style tool schema from ProjectLens catalog entry."""

    name = str(spec["name"])
    description = str(spec.get("description") or name)
    raw_params = spec.get("parameters") if isinstance(spec.get("parameters"), dict) else {}
    if not raw_params.get("properties"):
        raw_params = (
            _DEFAULT_PARAMETERS.get(name)
            or _ADVANCED_PARAMETERS.get(name)
            or {"type": "object", "properties": {}, "required": []}
        )
    properties = dict(raw_params.get("properties") or {})
    properties.update(_CONTEXT_PARAM_PROPERTIES)
    required = list(raw_params.get("required") or [])
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


def build_tool_call_payload(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    config: ProjectLensPluginConfig,
    chat_id: str | None = None,
    user_id: str | None = None,
    tenant_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Build POST /project-agent/tools/call body (no Kernel imports)."""

    if project_id or tenant_id:
        project = {
            "tenant_id": tenant_id or config.default_tenant_id,
            "project_id": project_id or config.default_project_id,
        }
    elif chat_id:
        project = config.project_for_chat(chat_id)
    else:
        project = {
            "tenant_id": config.default_tenant_id,
            "project_id": config.default_project_id,
        }
    return {
        "tool_name": tool_name,
        "project": project,
        "user_id": user_id or config.default_user_id,
        "chat_id": chat_id or "hermes-chat",
        "arguments": arguments,
    }


def register_projectlens_tools(
    ctx: Any,
    *,
    client: ProjectLensApiClient,
    config: ProjectLensPluginConfig,
) -> list[str]:
    """Register formal projectlens_* tools when Hermes ctx supports it.

    Returns the list of registered tool names (empty when register_tool is absent).
    Never patches Hermes core.
    """

    if not ctx_supports_register_tool(ctx):
        return []

    use_hermes = register_tool_uses_hermes_kwargs(ctx.register_tool)
    registered: list[str] = []
    for spec in resolve_tool_catalog(client):
        name = str(spec["name"])
        description = str(spec.get("description") or name)
        schema = build_openai_tool_schema(spec)
        handler = _make_tool_handler(name, client=client, config=config)

        if use_hermes:
            ctx.register_tool(
                name=name,
                toolset=TOOLSET_NAME,
                schema=schema,
                handler=handler,
                description=description,
            )
        else:
            # FakeContext / unit-test style: positional name + handler.
            ctx.register_tool(name, handler, description=description)
        registered.append(name)
    return registered


def _make_tool_handler(
    tool_name: str,
    *,
    client: ProjectLensApiClient,
    config: ProjectLensPluginConfig,
) -> Any:
    def _handler(args: dict[str, Any] | str | None = None, **kwargs: Any) -> str:
        # Hermes registry calls handler(args: dict, **kwargs).
        # Unit tests may pass arguments=... or a dict as first positional.
        raw = args if args is not None else kwargs.get("arguments")
        parsed = _normalize_arguments(raw, kwargs)
        chat_id = (
            parsed.pop("chat_id", None)
            or kwargs.get("chat_id")
            or kwargs.get("channel_id")
        )
        user_id = parsed.pop("user_id", None) or kwargs.get("user_id")
        tenant_id = parsed.pop("tenant_id", None)
        project_id = parsed.pop("project_id", None)
        payload = build_tool_call_payload(
            tool_name=tool_name,
            arguments=parsed,
            config=config,
            chat_id=chat_id if isinstance(chat_id, str) else None,
            user_id=user_id if isinstance(user_id, str) else None,
            tenant_id=tenant_id if isinstance(tenant_id, str) else None,
            project_id=project_id if isinstance(project_id, str) else None,
        )
        envelope = client.call_tool(payload)
        return json.dumps(envelope, ensure_ascii=False)

    _handler.__name__ = tool_name
    _handler.__qualname__ = tool_name
    return _handler


def _normalize_arguments(
    arguments: dict[str, Any] | str | None,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return dict(arguments)
    if isinstance(arguments, str) and arguments.strip():
        try:
            decoded = json.loads(arguments)
            if isinstance(decoded, dict):
                return decoded
        except json.JSONDecodeError:
            return {"query": arguments}
    cleaned = {
        key: value
        for key, value in kwargs.items()
        if key
        not in {"chat_id", "channel_id", "user_id", "tenant_id", "project_id", "arguments"}
        and value is not None
    }
    return cleaned
