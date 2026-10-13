"""MCP ProjectLens tool envelope handlers (in-process; mcp package optional)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from project_lens.application.hermes_runtime import HermesRuntimeService
from project_lens.application.project_agent_ask import ProjectAgentAskService
from project_lens.application.project_agent_tools import ProjectAgentToolService
from project_lens.application.run_service import InMemoryRunRepository, RunService
from project_lens.context.bootstrap import (
    build_registered_context_engine,
    default_local_project_registrations,
)
from project_lens.domain.identity import ActorContext
from project_lens.domain.models import ProjectAnswer, ProjectRef
from project_lens.integrations.feishu.hermes_tool_loop import FeishuHermesToolLoopResult
from project_lens.integrations.mcp.handler import (
    FORMAL_TOOL_NAMES,
    TOOL_DESCRIPTION,
    TOOL_NAME,
    ask_project,
    call_projectlens_tool,
    list_projectlens_tools,
)
from project_lens.project_space.registry import (
    load_project_spaces_from_dir,
    registry_from_local_registrations,
)
from project_lens.runtime.events import InMemoryEventSink
from project_lens.runtime.lifecycle import LifecycleBus

ROOT = Path(__file__).resolve().parents[1]


def _actor() -> ActorContext:
    return ActorContext(
        tenant_key="demo", actor_id="u1", chat_id="mcp-chat",
        chat_type="group", source="test_fixture", authenticated=True,
    )


class _FakeHermesBridge:
    async def answer(self, **kwargs):  # noqa: ANN003
        project = kwargs["project"]
        return FeishuHermesToolLoopResult(
            ok=True,
            envelope={"ok": True, "audit_ref": {"allow_apply": False}},
            tool_names=("projectlens_search_context",),
            loop_id=kwargs["loop_id"], trace_id=kwargs["trace_id"],
            verified_answer=ProjectAnswer(
                project=project, status="unknown",
                business_summary="Evidence is insufficient.",
                technical_summary="Evidence is insufficient.",
                unknowns=("Need cited evidence.",),
            ),
        )


def _services() -> tuple[ProjectAgentAskService, ProjectAgentToolService]:
    registrations = default_local_project_registrations(ROOT)
    engine, _index = build_registered_context_engine(registrations)
    registry = registry_from_local_registrations(registrations)
    for space in load_project_spaces_from_dir(ROOT / "config" / "projects", base_dir=ROOT):
        registry.upsert(space)
    events = InMemoryEventSink()
    lifecycle = LifecycleBus(event_sink=events)
    run_service = RunService(
        InMemoryRunRepository(),
        event_sink=events,
        lifecycle=lifecycle,
        agent_mode="hermes",
    )
    ask = ProjectAgentAskService(
        hermes_runtime=HermesRuntimeService(
            run_service=run_service, bridge=_FakeHermesBridge()  # type: ignore[arg-type]
        ),
        project_registry=registry,
    )
    tools = ProjectAgentToolService(context_engine=engine, project_registry=registry)
    return ask, tools


def _ask_service() -> ProjectAgentAskService:
    return _services()[0]


def _tool_service() -> ProjectAgentToolService:
    return _services()[1]


@pytest.mark.asyncio
async def test_mcp_ask_project_returns_answer_envelope() -> None:
    service = _ask_service()
    result = await ask_project(
        service,
        question="这个项目的订单创建入口在哪里？",
        project_id="payment",
        tenant_id="demo",
        user_id="u1",
        actor=_actor(),
    )
    assert result["ok"] is True
    assert result["project"]["project_id"] == "payment"
    assert result["run_id"]
    assert result["trace_id"]
    assert result["answer_summary"]
    assert result["audit_ref"]["allow_apply"] is False
    assert result["audit_ref"]["agent_mode"] == "hermes"
    for fact in result["facts"]:
        assert fact["citations"]
    assert result["facts"] or result["unknowns"]
    assert result["role_views_available"]


@pytest.mark.asyncio
async def test_mcp_ask_project_not_found() -> None:
    service = _ask_service()
    result = await ask_project(
        service,
        question="介绍一下",
        project_id="nope",
        tenant_id="demo",
        actor=_actor(),
    )
    assert result["ok"] is False
    assert result["error_code"] == "PROJECT_NOT_FOUND"
    assert result["audit_ref"]["allow_apply"] is False


@pytest.mark.asyncio
async def test_mcp_ask_write_denied() -> None:
    service = _ask_service()
    result = await ask_project(
        service,
        question="请帮我部署到生产环境",
        project_id="payment",
        tenant_id="demo",
        actor=_actor(),
    )
    assert result["ok"] is False
    assert result["error_code"] == "WRITE_ACTION_DENIED"
    assert result["audit_ref"]["allow_apply"] is False


def test_mcp_server_module_tool_name_constant() -> None:
    assert TOOL_NAME == "projectlens_ask_project"
    assert "DEBUG" in TOOL_DESCRIPTION or "SMOKE" in TOOL_DESCRIPTION
    assert TOOL_NAME not in FORMAL_TOOL_NAMES


def test_mcp_list_projectlens_tools_matches_formal_catalog() -> None:
    catalog = list_projectlens_tools(_tool_service())
    assert catalog["ok"] is True
    names = {item["name"] for item in catalog["tools"]}
    assert names == set(FORMAL_TOOL_NAMES)
    assert all(item["category"] == "read" for item in catalog["tools"])
    assert all(item["allow_apply"] is False for item in catalog["tools"])
    assert TOOL_NAME not in names
    for blocked in ("grep", "list_files", "read_file_range", "apply", "deploy", "restart"):
        assert not any(blocked in name for name in names)


@pytest.mark.asyncio
async def test_mcp_call_projectlens_tool_search_context_envelope() -> None:
    result = await call_projectlens_tool(
        _tool_service(),
        tool_name="projectlens_search_context",
        project_id="payment",
        tenant_id="demo",
        user_id="u1",
        arguments={"query": "create_order", "limit": 3},
        actor=_actor(),
    )
    assert result["ok"] is True
    assert result["tool_name"] == "projectlens_search_context"
    assert result["internal_tool_name"] == "search_context"
    assert result["summary"]
    assert result["audit_ref"]["allow_apply"] is False
    assert isinstance(result["citations"], list)


@pytest.mark.asyncio
async def test_mcp_call_projectlens_tool_list_knowledge_gaps() -> None:
    gaps = await call_projectlens_tool(
        _tool_service(),
        tool_name="projectlens_list_knowledge_gaps",
        project_id="payment",
        user_id="u1",
        arguments={},
        actor=_actor(),
    )
    assert gaps["ok"] is True
    assert gaps["internal_tool_name"] == "list_knowledge_gaps"
    assert gaps["audit_ref"]["allow_apply"] is False


@pytest.mark.asyncio
async def test_mcp_call_projectlens_tool_rejects_write_with_recovery_hint() -> None:
    result = await call_projectlens_tool(
        _tool_service(),
        tool_name="projectlens_deploy",
        project_id="payment",
        arguments={},
        actor=_actor(),
    )
    assert result["ok"] is False
    assert result["error_code"] in {"TOOL_NOT_ALLOWED", "UNKNOWN_TOOL"}
    assert result["agent_recovery_hint"]
    assert result["audit_ref"]["allow_apply"] is False


@pytest.mark.asyncio
async def test_mcp_call_rejects_internal_grep_tool() -> None:
    result = await call_projectlens_tool(
        _tool_service(),
        tool_name="projectlens_grep_project",
        project_id="payment",
        arguments={"query": "password"},
        actor=_actor(),
    )
    assert result["ok"] is False
    assert result["agent_recovery_hint"]
    assert result["audit_ref"]["allow_apply"] is False


def test_mcp_server_create_requires_extra_or_succeeds() -> None:
    """create_mcp_server either builds FastMCP or exits with install hint."""

    try:
        from project_lens.integrations.mcp.server import create_mcp_server
    except SystemExit:
        pytest.skip("mcp extra not installed")
        return

    ask, tools = _services()
    try:
        server = create_mcp_server(ask_service=ask, tool_service=tools)
    except SystemExit as exc:
        assert "pip install" in str(exc) or "mcp" in str(exc).lower()
        return
    assert server is not None


@pytest.mark.asyncio
async def test_mcp_tool_json_roundtrip_matches_handler() -> None:
    """Simulate what the MCP tool returns (JSON string) for Hermes clients."""

    service = _ask_service()
    payload = await ask_project(
        service,
        question="知识库现在缺什么资料？",
        project_id="payment",
        tenant_id="demo",
        user_id="u1",
        actor=_actor(),
    )
    encoded = json.dumps(payload, ensure_ascii=False)
    decoded = json.loads(encoded)
    assert decoded["ok"] is True
    assert decoded["audit_ref"]["allow_apply"] is False
