"""Phase 3: Hermes plugin glue for ProjectLens ask API."""

from __future__ import annotations

import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from typing import Any

import pytest

from project_lens.integrations.hermes_plugin.commands import (
    DEFAULT_GAPS_QUESTION,
    DEFAULT_MAP_QUESTION,
    ProjectLensCommands,
    reset_last_run_id_for_tests,
)
from project_lens.integrations.hermes_plugin.config import ProjectLensPluginConfig
from project_lens.integrations.hermes_plugin.export import export_hermes_plugin, main as export_main
from project_lens.integrations.hermes_plugin.render import render_answer_envelope
from project_lens.integrations.hermes_plugin.smoke import (
    run_projectlens_tool_loop_smoke,
    run_exported_plugin_smoke,
    run_hermes_cli_command_smoke,
    run_hermes_projectlens_tool_registry_smoke,
)
from project_lens.integrations.hermes_plugin.tools import (
    FORMAL_TOOL_NAMES,
    TOOLSET_NAME,
    build_openai_tool_schema,
    build_tool_call_payload,
    ctx_supports_register_tool,
    register_projectlens_tools,
    register_tool_uses_hermes_kwargs,
    resolve_tool_catalog,
)
from project_lens.integrations.hermes_plugin import register


class FakeClient:
    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.response = response or {
            "ok": True,
            "run_id": "run-ask-1",
            "answer_summary": "ProjectLens summary",
            "facts": [],
            "unknowns": [],
            "citations": [],
            "audit_ref": {"allow_apply": False, "run_id": "run-ask-1"},
        }
        self.role_view_response: dict[str, Any] | None = None
        self.requests: list[dict[str, Any]] = []
        self.role_view_calls: list[tuple[str, str]] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.list_tools_response: dict[str, Any] = {
            "ok": True,
            "tools": [
                {
                    "name": name,
                    "description": f"desc {name}",
                    "category": "read",
                    "allow_apply": False,
                }
                for name in FORMAL_TOOL_NAMES
            ],
        }

    def ask_project(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        return self.response

    def role_view(self, run_id: str, audience: str) -> dict[str, Any]:
        self.role_view_calls.append((run_id, audience))
        if self.role_view_response is not None:
            return self.role_view_response
        return {
            "ok": True,
            "run_id": run_id,
            "audience": audience,
            "markdown": f"### replayed {audience} for {run_id}",
            "audit_ref": {"allow_apply": False, "run_id": run_id},
        }

    def list_tools(self) -> dict[str, Any]:
        return self.list_tools_response

    def call_tool(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.tool_calls.append(payload)
        return {
            "ok": True,
            "tool_name": payload["tool_name"],
            "summary": f"called {payload['tool_name']}",
            "citations": [],
            "evidence_refs": [],
            "unknowns": [],
            "audit_ref": {"allow_apply": False},
        }

class FakeContext:
    def __init__(self) -> None:
        self.commands: dict[str, dict[str, Any]] = {}
        self.tools: list[Any] = []
        self.skills: list[Any] = []
        self.hooks: list[tuple[str, Any]] = []

    def register_command(
        self,
        name: str,
        handler: Any,
        description: str = "",
        args_hint: str = "",
    ) -> None:
        self.commands[name] = {
            "handler": handler,
            "description": description,
            "args_hint": args_hint,
        }

    def register_tool(self, *args: Any, **kwargs: Any) -> None:
        self.tools.append((args, kwargs))

    def register_skill(self, *args: Any, **kwargs: Any) -> None:
        self.skills.append((args, kwargs))

    def register_hook(self, name: str, callback: Any) -> None:
        self.hooks.append((name, callback))


def test_project_command_builds_ask_request_with_defaults() -> None:
    reset_last_run_id_for_tests()
    client = FakeClient()
    commands = ProjectLensCommands(
        client=client,
        config=ProjectLensPluginConfig(
            default_tenant_id="demo",
            default_project_id="payment",
        ),
    )

    output = commands.project("What is this project?")

    assert "ProjectLens summary" in output
    assert "结论" in output
    assert client.requests == [
        {
            "question": "What is this project?",
            "project": {"tenant_id": "demo", "project_id": "payment"},
            "user_id": "hermes-user",
            "channel_id": None,
            "audience": "team",
            "mode": "read_only",
            "format": "concise",
        }
    ]


def test_project_map_and_gaps_use_default_questions() -> None:
    reset_last_run_id_for_tests()
    client = FakeClient()
    commands = ProjectLensCommands(client=client, config=ProjectLensPluginConfig())

    commands.project_map("")
    reset_last_run_id_for_tests()
    commands.project_gaps("")

    assert client.requests[0]["question"] == DEFAULT_MAP_QUESTION
    assert client.requests[1]["question"] == DEFAULT_GAPS_QUESTION
    assert client.requests[0]["audience"] == "technical"
    assert client.requests[1]["audience"] == "team"


def test_project_role_parses_audience_and_question() -> None:
    reset_last_run_id_for_tests()
    client = FakeClient()
    commands = ProjectLensCommands(client=client, config=ProjectLensPluginConfig())

    commands.project_role("technical where is the order entrypoint?")

    assert client.requests[0]["audience"] == "technical"
    assert client.requests[0]["question"] == "where is the order entrypoint?"
    assert not client.role_view_calls


def test_project_role_replays_cached_run_id() -> None:
    reset_last_run_id_for_tests()
    client = FakeClient()
    commands = ProjectLensCommands(client=client, config=ProjectLensPluginConfig())

    commands.project("What is this project?")
    output = commands.project_role("technical")

    assert client.role_view_calls == [("run-ask-1", "technical")]
    assert "replayed technical for run-ask-1" in output
    assert len(client.requests) == 1


def test_project_role_explicit_run_id_replays() -> None:
    reset_last_run_id_for_tests()
    client = FakeClient()
    commands = ProjectLensCommands(client=client, config=ProjectLensPluginConfig())

    output = commands.project_role("evidence run_id=run-xyz")

    assert client.role_view_calls == [("run-xyz", "evidence")]
    assert "replayed evidence for run-xyz" in output
    assert not client.requests


def test_project_role_rejects_unknown_audience() -> None:
    reset_last_run_id_for_tests()
    client = FakeClient()
    commands = ProjectLensCommands(client=client, config=ProjectLensPluginConfig())

    output = commands.project_role("random where is the order entrypoint?")

    assert "Unknown audience" in output
    assert not client.requests
    assert not client.role_view_calls


def test_binding_from_chat_id() -> None:
    config = ProjectLensPluginConfig(
        default_tenant_id="demo",
        default_project_id="payment",
        feishu_bindings={
            "oc_group_1": {"tenant_id": "acme", "project_id": "order"},
        },
    )

    assert config.project_for_chat("oc_group_1") == {
        "tenant_id": "acme",
        "project_id": "order",
    }
    assert config.project_for_chat("unknown") == {
        "tenant_id": "demo",
        "project_id": "payment",
    }


def test_error_envelope_rendered_without_fact_invention() -> None:
    output = render_answer_envelope(
        {
            "ok": False,
            "error_code": "PROJECT_NOT_FOUND",
            "message": "project not found",
            "agent_recovery_hint": "Check tenant_id/project_id binding.",
            "audit_ref": {"allow_apply": False},
        }
    )

    assert "PROJECT_NOT_FOUND" in output
    assert "Check tenant_id/project_id binding." in output
    assert "allow_apply=false" in output
    assert "事实" not in output


def test_success_envelope_rendering_defaults_to_team_first_screen() -> None:
    envelope = {
        "ok": True,
        "answer_summary": "This project handles order payments.",
        "facts": [
            {"text": "API entrypoint is app.py.", "citations": ["ev-1"]},
            {"text": "Payment logic is in services/payment.py.", "citations": ["ev-2"]},
        ],
        "unknowns": ["No deployment runbook was found."],
        "next_actions": [{"title": "补充 runbook", "requires_approval": False}],
        "citations": [
            {"id": "ev-1", "kind": "file", "source_uri": "code:app.py", "summary": "app.py snippet"},
            {
                "id": "ev-2",
                "kind": "file",
                "source_uri": "code:services/payment.py",
                "summary": "payment snippet",
            },
            {"id": "ev-3", "kind": "doc", "source_uri": "doc:README.md", "summary": "README snippet"},
            {"id": "ev-4", "kind": "doc", "source_uri": "doc:extra.md", "summary": "extra snippet"},
        ],
        "audit_ref": {
            "trace_id": "trace-1",
            "run_id": "run-1",
            "tool_names": ["search_context"],
            "allow_apply": False,
        },
    }
    output = render_answer_envelope(envelope)

    assert "团队协作视图" in output
    assert "This project handles order payments." in output
    assert "API entrypoint is app.py." in output
    assert "No deployment runbook was found." in output
    assert "补充 runbook" in output
    assert "已基于 4 条项目资料" in output
    assert "code:app.py" not in output
    assert "search_context" not in output
    assert "allow_apply=false" in output

    evidence = render_answer_envelope(envelope, audience="evidence")
    assert "code:app.py" in evidence
    assert "doc:README.md" in evidence


def test_register_registers_expected_commands_only(monkeypatch) -> None:
    monkeypatch.delenv("PROJECTLENS_FEISHU_CARDS", raising=False)
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    ctx = FakeContext()

    register(ctx)

    assert set(ctx.commands) == {
        "project",
        "project-map",
        "project-gaps",
        "project-role",
        "card",
    }
    # FakeContext supports register_tool; formal tools are registered (plus
    # optional memory/history tools from the client catalog).
    tool_names = {args[0] for args, _kwargs in ctx.tools}
    assert set(FORMAL_TOOL_NAMES).issubset(tool_names)
    assert ctx.skills == []
    assert "DEBUG" in ctx.commands["project"]["description"] or "SMOKE" in ctx.commands[
        "project"
    ]["description"]
    assert ctx.hooks == []


def test_register_skips_tools_when_ctx_lacks_register_tool() -> None:
    class CommandsOnlyContext:
        def __init__(self) -> None:
            self.commands: dict[str, dict[str, Any]] = {}
            self.skills: list[Any] = []
            self.hooks: list[tuple[str, Any]] = []

        def register_command(
            self,
            name: str,
            handler: Any,
            description: str = "",
            args_hint: str = "",
        ) -> None:
            self.commands[name] = {
                "handler": handler,
                "description": description,
                "args_hint": args_hint,
            }

        def register_skill(self, *args: Any, **kwargs: Any) -> None:
            self.skills.append((args, kwargs))

        def register_hook(self, name: str, callback: Any) -> None:
            self.hooks.append((name, callback))

    ctx = CommandsOnlyContext()
    assert ctx_supports_register_tool(ctx) is False

    register(ctx)

    assert "project" in ctx.commands
    assert ctx.skills == []


def test_register_projectlens_tools_calls_tools_call_http() -> None:
    client = FakeClient()
    ctx = FakeContext()
    config = ProjectLensPluginConfig(
        default_tenant_id="demo",
        default_project_id="payment",
        default_user_id="hermes-user",
    )

    names = register_projectlens_tools(ctx, client=client, config=config)
    assert set(names) == set(FORMAL_TOOL_NAMES)

    handler = next(args[1] for args, _ in ctx.tools if args[0] == "projectlens_search_context")
    output = handler({"query": "create_order", "limit": 2})
    decoded = json.loads(output)
    assert decoded["ok"] is True
    assert client.tool_calls == [
        {
            "tool_name": "projectlens_search_context",
            "project": {"tenant_id": "demo", "project_id": "payment"},
            "user_id": "hermes-user",
            "chat_id": "hermes-chat",
            "arguments": {"query": "create_order", "limit": 2},
        }
    ]


class FakeHermesContext:
    """Mirrors Hermes PluginContext.register_tool keyword signature."""

    def __init__(self) -> None:
        self.tools: list[dict[str, Any]] = []

    def register_tool(
        self,
        name: str,
        toolset: str,
        schema: dict[str, Any],
        handler: Any,
        check_fn: Any = None,
        requires_env: list[Any] | None = None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "",
        override: bool = False,
    ) -> None:
        del check_fn, requires_env, is_async, emoji, override
        self.tools.append(
            {
                "name": name,
                "toolset": toolset,
                "schema": schema,
                "handler": handler,
                "description": description,
            }
        )


def test_register_projectlens_tools_uses_hermes_kwargs_api() -> None:
    client = FakeClient()
    ctx = FakeHermesContext()
    assert register_tool_uses_hermes_kwargs(ctx.register_tool) is True
    config = ProjectLensPluginConfig(
        default_tenant_id="demo",
        default_project_id="payment",
        default_user_id="hermes-user",
    )

    names = register_projectlens_tools(ctx, client=client, config=config)
    assert set(names) == set(FORMAL_TOOL_NAMES)
    assert len(ctx.tools) == len(FORMAL_TOOL_NAMES)
    assert {item["toolset"] for item in ctx.tools} == {TOOLSET_NAME}
    search = next(item for item in ctx.tools if item["name"] == "projectlens_search_context")
    assert search["schema"]["name"] == "projectlens_search_context"
    assert "query" in search["schema"]["parameters"]["properties"]
    assert "project_id" in search["schema"]["parameters"]["properties"]

    # Hermes registry style: handler(args: dict)
    output = search["handler"](
        {
            "query": "create_order",
            "limit": 2,
            "chat_id": "oc_payment",
            "user_id": "u-phase3",
        }
    )
    decoded = json.loads(output)
    assert decoded["ok"] is True
    assert client.tool_calls[-1] == {
        "tool_name": "projectlens_search_context",
        "project": {"tenant_id": "demo", "project_id": "payment"},
        "user_id": "u-phase3",
        "chat_id": "oc_payment",
        "arguments": {"query": "create_order", "limit": 2},
    }


def test_build_openai_tool_schema_includes_context_params() -> None:
    schema = build_openai_tool_schema(
        {
            "name": "projectlens_search_context",
            "description": "search",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        }
    )
    assert schema["parameters"]["required"] == ["query"]
    assert "tenant_id" in schema["parameters"]["properties"]
    assert "chat_id" in schema["parameters"]["properties"]


def test_hermes_tool_registry_smoke_against_fake_http(tmp_path: Path) -> None:
    """Unit-level stand-in: Hermes kwargs registration + HTTP tools/call.

    Full PluginManager live smoke is covered by CLI against hermes-agent-main.
    """

    hermes_repo = Path(r"C:\Users\Administrator\Desktop\hermes-agent-main")
    if not (hermes_repo / "hermes_cli" / "plugins.py").is_file():
        pytest.skip("hermes-agent-main not available")

    server = _FakeToolLoopApiServer()
    server.start()
    try:
        result = run_hermes_projectlens_tool_registry_smoke(
            hermes_repo=hermes_repo,
            api_base_url=f"{server.base_url}/api/v1",
            tenant_id="demo",
            project_id="payment",
            chat_id="oc_payment",
            hermes_home=tmp_path / "hermes-home",
            query="create_order coupon",
        )
    finally:
        server.stop()

    # PluginManager load can fail on incomplete envs; accept skip via soft assert path.
    if not result.ok and "Unable to import Hermes" in result.output:
        pytest.skip(result.output)
    if not result.ok and "formal tool catalog mismatch" in result.output and not result.discovered_tools:
        # Plugin enabled but register_tool path failed before alignment — fail hard.
        pytest.fail(result.output)
    assert result.ok is True, result.output
    assert set(result.discovered_tools) == set(FORMAL_TOOL_NAMES)
    assert result.dispatched_tool == "projectlens_search_context"
    assert result.envelope is not None
    assert result.envelope["audit_ref"]["allow_apply"] is False
    assert isinstance(result.envelope["citations"], list)
    assert isinstance(result.envelope["evidence_refs"], list)
    assert "/api/v1/project-agent/ask" not in {request["path"] for request in server.requests}


def test_resolve_tool_catalog_falls_back_when_list_tools_fails() -> None:
    client = FakeClient()
    client.list_tools_response = {
        "ok": False,
        "error_code": "PROJECTLENS_UNAVAILABLE",
        "agent_recovery_hint": "Start API",
    }
    catalog = resolve_tool_catalog(client)
    assert {item["name"] for item in catalog} == set(FORMAL_TOOL_NAMES)


def test_build_tool_call_payload_uses_chat_binding() -> None:
    config = ProjectLensPluginConfig(
        default_tenant_id="demo",
        default_project_id="payment",
        feishu_bindings={"oc_1": {"tenant_id": "acme", "project_id": "order"}},
    )
    payload = build_tool_call_payload(
        tool_name="projectlens_list_knowledge_gaps",
        arguments={"limit": 3},
        config=config,
        chat_id="oc_1",
        user_id="u9",
    )
    assert payload == {
        "tool_name": "projectlens_list_knowledge_gaps",
        "project": {"tenant_id": "acme", "project_id": "order"},
        "user_id": "u9",
        "chat_id": "oc_1",
        "arguments": {"limit": 3},
    }


def test_register_enables_card_hook_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("PROJECTLENS_FEISHU_CARDS", "1")
    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret_test")
    ctx = FakeContext()

    register(ctx)

    assert "card" in ctx.commands
    assert len(ctx.hooks) == 1
    assert ctx.hooks[0][0] == "pre_gateway_dispatch"


def test_card_action_role_payload_replays() -> None:
    reset_last_run_id_for_tests()
    client = FakeClient()
    commands = ProjectLensCommands(client=client, config=ProjectLensPluginConfig())
    payload = json.dumps({"action": "projectlens_role", "audience": "business", "run_id": "run-1"})

    output = commands.card_button(payload)

    assert client.role_view_calls == [("run-1", "business")]
    assert "replayed business for run-1" in output
    assert not client.requests


def test_card_slash_command_parses_hermes_button_args() -> None:
    reset_last_run_id_for_tests()
    client = FakeClient()
    commands = ProjectLensCommands(client=client, config=ProjectLensPluginConfig())

    output = commands.card(
        'button {"action":"projectlens_role","audience":"evidence","run_id":"run-77"}'
    )

    assert client.role_view_calls == [("run-77", "evidence")]
    assert "replayed evidence for run-77" in output


@pytest.mark.parametrize("raw", ["", "   "])
def test_project_command_requires_question(raw: str) -> None:
    client = FakeClient()
    commands = ProjectLensCommands(client=client, config=ProjectLensPluginConfig())

    output = commands.project(raw)

    assert "Usage:" in output
    assert not client.requests


def test_export_hermes_plugin_writes_bootstrap_plugin(tmp_path: Path) -> None:
    project_root = tmp_path / "project-lens"
    project_src = project_root / "src"
    project_src.mkdir(parents=True)
    hermes_plugins_dir = tmp_path / "hermes-plugins"

    result = export_hermes_plugin(
        project_lens_root=project_root,
        hermes_plugins_dir=hermes_plugins_dir,
    )

    assert result.plugin_dir == hermes_plugins_dir / "projectlens"
    assert (result.plugin_dir / "plugin.yaml").is_file()
    bootstrap = (result.plugin_dir / "__init__.py").read_text(encoding="utf-8")
    assert str(project_src) in bootstrap
    assert "project_lens.integrations.hermes_plugin" in bootstrap
    assert "def register(ctx):" in bootstrap
    assert not (result.plugin_dir / "project_lens.db").exists()


def test_export_hermes_plugin_can_refuse_overwrite(tmp_path: Path) -> None:
    project_root = tmp_path / "project-lens"
    (project_root / "src").mkdir(parents=True)
    plugin_dir = tmp_path / "hermes-plugins" / "projectlens"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "custom.txt").write_text("keep me", encoding="utf-8")

    with pytest.raises(FileExistsError):
        export_hermes_plugin(
            project_lens_root=project_root,
            hermes_plugins_dir=tmp_path / "hermes-plugins",
            overwrite=False,
        )

    assert (plugin_dir / "custom.txt").read_text(encoding="utf-8") == "keep me"


def test_export_cli_writes_plugin_directory(tmp_path: Path) -> None:
    project_root = tmp_path / "project-lens"
    (project_root / "src").mkdir(parents=True)
    hermes_plugins_dir = tmp_path / "hermes-plugins"

    exit_code = export_main(
        [
            "--project-lens-root",
            str(project_root),
            "--hermes-plugins-dir",
            str(hermes_plugins_dir),
        ]
    )

    assert exit_code == 0
    assert (hermes_plugins_dir / "projectlens" / "__init__.py").is_file()
    assert (hermes_plugins_dir / "projectlens" / "plugin.yaml").is_file()


def test_smoke_loads_exported_plugin_and_calls_ask_api(tmp_path: Path) -> None:
    project_root = tmp_path / "project-lens"
    (project_root / "src").mkdir(parents=True)
    export_result = export_hermes_plugin(
        project_lens_root=Path.cwd(),
        hermes_plugins_dir=tmp_path / "hermes-plugins",
    )
    server = _FakeAskApiServer()
    server.start()
    try:
        result = run_exported_plugin_smoke(
            plugin_dir=export_result.plugin_dir,
            api_base_url=f"{server.base_url}/api/v1",
            question="What is this project?",
            tenant_id="demo",
            project_id="payment",
        )
    finally:
        server.stop()

    assert result.ok is True
    assert "smoke summary" in result.output
    assert server.requests[0]["path"] == "/api/v1/project-agent/ask"
    assert server.requests[0]["body"]["question"] == "What is this project?"
    assert server.requests[0]["body"]["project"] == {
        "tenant_id": "demo",
        "project_id": "payment",
    }


def test_hermes_cli_command_smoke_calls_process_command(tmp_path: Path) -> None:
    hermes_repo = tmp_path / "hermes"
    hermes_repo.mkdir()
    (hermes_repo / "cli.py").write_text(
        "\n".join(
            [
                "class HermesCLI:",
                "    def __init__(self, compact=False, verbose=False):",
                "        self.compact = compact",
                "        self.verbose = verbose",
                "    def process_command(self, command):",
                "        print('processed:' + command)",
                "        return command.startswith('/project')",
            ]
        ),
        encoding="utf-8",
    )
    hermes_home = tmp_path / "hermes-home"

    result = run_hermes_cli_command_smoke(
        hermes_repo=hermes_repo,
        hermes_home=hermes_home,
        api_base_url="http://127.0.0.1:8000/api/v1",
        tenant_id="demo",
        project_id="payment",
        command="/project What is this project?",
    )

    assert result.ok is True
    assert result.process_command_return is True
    assert "processed:/project What is this project?" in result.output


def test_smoke_cli_hermes_repo_mode_does_not_require_plugin_dir(tmp_path: Path) -> None:
    hermes_repo = tmp_path / "hermes"
    hermes_repo.mkdir()
    (hermes_repo / "cli.py").write_text(
        "\n".join(
            [
                "class HermesCLI:",
                "    def __init__(self, compact=False, verbose=False):",
                "        pass",
                "    def process_command(self, command):",
                "        print('ok')",
                "        return True",
            ]
        ),
        encoding="utf-8",
    )

    from project_lens.integrations.hermes_plugin.smoke import main as smoke_main

    assert (
        smoke_main(
            [
                "--hermes-repo",
                str(hermes_repo),
                "--hermes-home",
                str(tmp_path / "hermes-home"),
                "--api-base-url",
                "http://127.0.0.1:8000/api/v1",
                "--command",
                "/project What is this project?",
            ]
        )
        == 0
    )


def test_tool_loop_smoke_discovers_and_calls_formal_projectlens_tools() -> None:
    server = _FakeToolLoopApiServer()
    server.start()
    try:
        result = run_projectlens_tool_loop_smoke(
            api_base_url=f"{server.base_url}/api/v1",
            tenant_id="demo",
            project_id="payment",
            user_id="u-phase3",
            chat_id="oc_payment",
            tool_calls=(
                {
                    "tool_name": "projectlens_search_context",
                    "arguments": {"query": "create_order coupon", "limit": 2},
                },
                {
                    "tool_name": "projectlens_list_knowledge_gaps",
                    "arguments": {},
                },
            ),
        )
    finally:
        server.stop()

    assert result.ok is True
    assert set(result.discovered_tools) == set(FORMAL_TOOL_NAMES)
    assert result.called_tools == (
        "projectlens_search_context",
        "projectlens_list_knowledge_gaps",
    )
    assert "GET /project-agent/tools" in result.output
    assert "POST /project-agent/tools/call projectlens_search_context ok=true" in result.output
    assert "allow_apply=false" in result.output
    assert "/api/v1/project-agent/ask" not in {request["path"] for request in server.requests}
    assert server.requests[1]["body"]["project"] == {
        "tenant_id": "demo",
        "project_id": "payment",
    }
    assert server.requests[1]["body"]["user_id"] == "u-phase3"
    assert server.requests[1]["body"]["chat_id"] == "oc_payment"


def test_tool_loop_smoke_rejects_catalog_with_debug_or_unsafe_tools() -> None:
    server = _FakeToolLoopApiServer(
        extra_tools=(
            {"name": "projectlens_ask_project", "category": "debug", "allow_apply": False},
            {"name": "projectlens_deploy", "category": "write", "allow_apply": True},
        )
    )
    server.start()
    try:
        result = run_projectlens_tool_loop_smoke(
            api_base_url=f"{server.base_url}/api/v1",
            tenant_id="demo",
            project_id="payment",
            tool_calls=(
                {
                    "tool_name": "projectlens_search_context",
                    "arguments": {"query": "create_order"},
                },
            ),
        )
    finally:
        server.stop()

    assert result.ok is False
    assert result.called_tools == ()
    assert "formal tool catalog mismatch" in result.output
    assert len([request for request in server.requests if request["path"].endswith("/tools/call")]) == 0


def test_tool_loop_smoke_can_switch_project_space_by_request() -> None:
    server = _FakeToolLoopApiServer()
    server.start()
    try:
        result = run_projectlens_tool_loop_smoke(
            api_base_url=f"{server.base_url}/api/v1",
            tenant_id="demo",
            project_id="crm",
            chat_id="oc_crm",
            tool_calls=(
                {
                    "tool_name": "projectlens_list_knowledge_gaps",
                    "arguments": {"limit": 3},
                },
            ),
        )
    finally:
        server.stop()

    assert result.ok is True
    assert result.called_tools == ("projectlens_list_knowledge_gaps",)
    assert server.requests[1]["body"]["project"] == {
        "tenant_id": "demo",
        "project_id": "crm",
    }
    assert server.requests[1]["body"]["chat_id"] == "oc_crm"


class _FakeAskApiHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(content_length).decode("utf-8"))
        self.requests.append({"path": self.path, "body": body})
        payload = {
            "ok": True,
            "answer_summary": "smoke summary",
            "facts": [{"text": "Smoke fact.", "citations": ["ev-smoke"]}],
            "citations": [
                {
                    "id": "ev-smoke",
                    "kind": "tool_result",
                    "source_uri": "smoke:api",
                    "summary": "fake ask API response",
                }
            ],
            "audit_ref": {"allow_apply": False, "tool_names": ["smoke"]},
        }
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        return


class _FakeAskApiServer:
    def __init__(self) -> None:
        _FakeAskApiHandler.requests = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAskApiHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    @property
    def requests(self) -> list[dict[str, Any]]:
        return _FakeAskApiHandler.requests

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class _FakeToolLoopApiHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    extra_tools: tuple[dict[str, Any], ...] = ()

    def do_GET(self) -> None:  # noqa: N802
        self.requests.append({"method": "GET", "path": self.path})
        if self.path != "/api/v1/project-agent/tools":
            self.send_response(404)
            self.end_headers()
            return
        tools = [
            {
                "name": name,
                "description": f"desc {name}",
                "category": "read",
                "parameters": {},
                "allow_apply": False,
            }
            for name in FORMAL_TOOL_NAMES
        ]
        tools.extend(self.extra_tools)
        self._write_json({"ok": True, "tools": tools})

    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(content_length).decode("utf-8"))
        self.requests.append({"method": "POST", "path": self.path, "body": body})
        if self.path == "/api/v1/project-agent/ask":
            self._write_json(
                {
                    "ok": False,
                    "error_code": "ASK_SHOULD_NOT_BE_USED_IN_TOOL_LOOP_SMOKE",
                    "audit_ref": {"allow_apply": False},
                }
            )
            return
        if self.path != "/api/v1/project-agent/tools/call":
            self.send_response(404)
            self.end_headers()
            return
        self._write_json(
            {
                "ok": True,
                "tool_name": body["tool_name"],
                "summary": f"{body['tool_name']} smoke summary",
                "citations": [{"id": "ev-smoke", "source_uri": "smoke:tool"}],
                "evidence_refs": [{"id": "ev-smoke", "source_uri": "smoke:tool"}],
                "unknowns": [],
                "audit_ref": {
                    "allow_apply": False,
                    "tool_name": body["tool_name"],
                    "project_id": body["project"]["project_id"],
                },
            }
        )

    def _write_json(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        return


class _FakeToolLoopApiServer:
    def __init__(self, *, extra_tools: tuple[dict[str, Any], ...] = ()) -> None:
        _FakeToolLoopApiHandler.requests = []
        _FakeToolLoopApiHandler.extra_tools = extra_tools
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeToolLoopApiHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    @property
    def requests(self) -> list[dict[str, Any]]:
        return _FakeToolLoopApiHandler.requests

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
