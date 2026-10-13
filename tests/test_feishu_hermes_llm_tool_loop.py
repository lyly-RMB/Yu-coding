"""Phase 3.1/3.2: HermesLoopRunner bridge + durable tool-trace audit."""

from __future__ import annotations

import inspect
import sys
from types import ModuleType
from uuid import UUID

import pytest

from project_lens.domain.models import ProjectRef
from project_lens.integrations.feishu import hermes_tool_loop as hermes_tool_loop_mod
from project_lens.integrations.feishu.hermes_loop_runner import (
    HermesAIAgentLoopRunner,
    HermesLoopRunResult,
    HermesLoopToolCall,
    _add_inherited_context_evidence,
    _is_recoverable_hermes_failure,
    _compact_tool_envelope,
    extract_tool_calls_from_messages,
)
from project_lens.context.governor import ContextGovernor, ContextGovernorConfig
from project_lens.integrations.feishu.hermes_tool_loop import (
    FeishuHermesToolLoopBridge,
    hermes_loop_scratchpad_entry,
)
from project_lens.integrations.hermes_plugin.config import ProjectLensPluginConfig
from project_lens.runtime.events import InMemoryEventSink
from project_lens.runtime.lifecycle import LifecycleBus, LifecycleEventType


def test_inherited_evidence_only_reads_explicit_evidence_ids_fields() -> None:
    inherited_id = "11111111-2222-3333-4444-555555555555"
    unrelated_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    calls = _add_inherited_context_evidence(
        context_text=(
            f"recent_turn[1] evidence_ids={inherited_id}\n"
            f"run_id={unrelated_id}"
        ),
        tool_calls=(),
    )
    assert len(calls) == 1
    assert calls[0].envelope["evidence_refs"][0]["id"] == inherited_id


class _FakeToolService:
    def list_tools(self) -> dict:
        return {"ok": True, "tools": []}

    async def call_tool(self, request):  # noqa: ANN001
        raise AssertionError("Fake bridge tests must not call ProjectAgentToolService directly")


class _FakeRunnerSelectingAuthorizedEvidence:
    """Chooses a tool the old rule planner would NOT pick for a .py-path question."""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def run(
        self,
        *,
        question: str,
        project: ProjectRef,
        user_id: str,
        chat_id: str,
    ) -> HermesLoopRunResult:
        self.calls.append(
            {
                "question": question,
                "project_id": project.project_id,
                "user_id": user_id,
                "chat_id": chat_id,
            }
        )
        citation = {
            "id": "ev-auth-1",
            "kind": "code",
            "source_uri": "src/demo.py",
            "summary": "authorized evidence hit",
        }
        envelope = {
            "ok": True,
            "tool_name": "projectlens_authorized_evidence",
            "summary": "authorized inventory for the project",
            "citations": [citation],
            "evidence_refs": [citation],
            "unknowns": [],
            "audit_ref": {"allow_apply": False},
            "tool_result_id": "tr-1",
        }
        return HermesLoopRunResult(
            final_response="基于 authorized_evidence：项目可读源已列出。",
            tool_calls=(
                HermesLoopToolCall(
                    name="projectlens_authorized_evidence",
                    arguments={"limit": 5},
                    envelope=envelope,
                ),
            ),
            ok=True,
        )


class _FakeUnavailableRunner:
    def run(
        self,
        *,
        question: str,
        project: ProjectRef,
        user_id: str,
        chat_id: str,
    ) -> HermesLoopRunResult:
        del question, project, user_id, chat_id
        return HermesLoopRunResult(
            final_response="",
            ok=False,
            error="Hermes unavailable: missing API key",
        )


@pytest.mark.asyncio
async def test_bridge_uses_injected_runner_not_rule_plan() -> None:
    assert not hasattr(hermes_tool_loop_mod, "_plan")
    assert not hasattr(FeishuHermesToolLoopBridge, "_plan")
    source = inspect.getsource(hermes_tool_loop_mod)
    assert "def _plan(" not in source
    assert "_needs_graph" not in source
    assert "_FILE_PATH_RE" not in source

    sink = InMemoryEventSink()
    bus = LifecycleBus(event_sink=sink)
    fake = _FakeRunnerSelectingAuthorizedEvidence()
    bridge = FeishuHermesToolLoopBridge(
        tool_service=_FakeToolService(),  # type: ignore[arg-type]
        config=ProjectLensPluginConfig(),
        loop_runner=fake,
        lifecycle=bus,
    )
    # Old rule planner would choose search_context + read_project_file for this text.
    question = "order_service.py create_order coupon relation?"
    project = ProjectRef(tenant_id="demo", project_id="payment")

    result = await bridge.answer(
        project=project,
        question=question,
        user_id="user-1",
        chat_id="chat-1",
    )

    assert fake.calls and fake.calls[0]["question"] == question
    assert result.ok is True
    assert bridge.last_tool_names == ("projectlens_authorized_evidence",)
    assert "projectlens_search_context" not in bridge.last_tool_names
    assert "projectlens_read_project_file" not in bridge.last_tool_names
    envelope = result.envelope
    assert envelope["ok"] is True
    assert envelope["citations"]
    assert envelope["evidence_refs"]
    assert envelope["tool_calls"]
    assert envelope["tool_calls"][0]["name"] == "projectlens_authorized_evidence"
    assert envelope["audit_ref"]["allow_apply"] is False
    assert envelope["audit_ref"]["mode"] == "feishu_hermes_llm_tool_loop"
    assert envelope["audit_ref"]["loop_id"]
    assert envelope["audit_ref"]["trace_id"]
    assert result.loop_id is not None
    assert result.trace_id is not None
    assert "authorized_evidence" in envelope["answer_summary"] or "authorized" in envelope[
        "answer_summary"
    ].casefold()

    loop_events = bus.for_run(result.loop_id)
    types = [event.type for event in loop_events]
    assert LifecycleEventType.TOOL_REQUESTED in types
    assert LifecycleEventType.TOOL_COMPLETED in types
    assert LifecycleEventType.ANSWER_COMPOSED in types
    durable = sink.for_run(result.loop_id)
    assert durable
    assert any(event.type.value == "tool_started" for event in durable)
    assert any(event.type.value == "tool_completed" for event in durable)
    assert all(
        event.payload.get("allow_apply") is False
        for event in durable
        if "allow_apply" in event.payload
    )


@pytest.mark.asyncio
async def test_bridge_reports_explicit_unavailable_without_rule_fallback() -> None:
    sink = InMemoryEventSink()
    bus = LifecycleBus(event_sink=sink)
    bridge = FeishuHermesToolLoopBridge(
        tool_service=_FakeToolService(),  # type: ignore[arg-type]
        config=ProjectLensPluginConfig(),
        loop_runner=_FakeUnavailableRunner(),
        lifecycle=bus,
    )
    result = await bridge.answer(
        project=ProjectRef(tenant_id="demo", project_id="payment"),
        question="order_service.py create_order coupon?",
        user_id="user-1",
        chat_id="chat-1",
    )
    assert result.ok is False
    assert result.envelope["ok"] is False
    assert result.envelope["audit_ref"]["allow_apply"] is False
    assert result.envelope["audit_ref"]["hermes_ok"] is False
    assert result.envelope["audit_ref"]["loop_id"]
    assert "Hermes" in result.envelope["answer_summary"]
    assert "missing API key" in " ".join(result.envelope["unknowns"])
    assert bridge.last_tool_names == ()
    assert result.loop_id is not None
    # Still emits bookend lifecycle even when no tools ran.
    assert any(
        event.type == LifecycleEventType.ANSWER_COMPOSED for event in bus.for_run(result.loop_id)
    )


def test_hermes_loop_scratchpad_entry_is_compact() -> None:
    entry = hermes_loop_scratchpad_entry(
        {
            "audit_ref": {
                "loop_id": "11111111-1111-1111-1111-111111111111",
                "trace_id": "22222222-2222-2222-2222-222222222222",
                "tool_names": ["projectlens_authorized_evidence"],
                "hermes_ok": True,
                "allow_apply": False,
            }
        }
    )
    assert entry["loop_id"] == "11111111-1111-1111-1111-111111111111"
    assert entry["allow_apply"] is False
    assert entry["tool_names"] == ["projectlens_authorized_evidence"]
    UUID(entry["loop_id"])


def test_hermes_tool_envelope_is_evidence_card_bounded() -> None:
    full_body = "SECRET FULL BODY " + ("x" * 5_000)
    envelope = _compact_tool_envelope(
        {
            "ok": True,
            "tool_name": "projectlens_read_project_file",
            "tool_result_id": "tr-bounded",
            "summary": "bounded summary",
            "citations": [
                {
                    "id": "ev-1",
                    "source_uri": "docs/design.md",
                    "revision": "r7",
                    "content_hash": "sha256:abc",
                    "summary": "design summary",
                    "content": full_body,
                },
                {
                    "id": "ev-1-duplicate",
                    "source_uri": "docs/design.md",
                    "revision": "r7",
                    "summary": "duplicate",
                },
            ],
            "evidence_refs": [],
            "result": {"content": full_body, "path": "docs/design.md"},
        }
    )
    serialized = str(envelope)
    assert full_body not in serialized
    assert envelope["citations"][0]["id"] == "ev-1"
    assert envelope["evidence_cards"][0]["evidence_id"] == "ev-1"
    assert envelope["evidence_cards"][0]["source_key"] == "docs/design.md"
    assert envelope["evidence_cards"][0]["revision"] == "r7"
    assert envelope["evidence_cards"][0]["content_hash"] == "sha256:abc"
    assert len(envelope["citations"]) == 1
    assert envelope["result"]["path"] == "docs/design.md"


def test_hermes_detail_preview_is_bounded_and_other_content_is_omitted() -> None:
    body = "a" * 2_000
    detail = _compact_tool_envelope(
        {
            "ok": True,
            "tool_name": "projectlens_read_project_file",
            "internal_tool_name": "read_project_file",
            "tool_result_id": "tr-detail",
            "result": {"content": body, "nested": {"text": body}},
        }
    )
    assert len(detail["result"]["content"]) == 1_201
    assert len(detail["result"]["nested"]["text"]) == 1_201
    assert detail["result"]["nested"]["text"] != body


def test_extract_tool_calls_from_messages_keeps_envelopes() -> None:
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {
                        "name": "projectlens_search_context",
                        "arguments": '{"query": "coupon", "limit": 3}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": json_dumps_envelope(),
        },
    ]
    calls = extract_tool_calls_from_messages(messages)
    assert len(calls) == 1
    assert calls[0].name == "projectlens_search_context"
    assert calls[0].arguments["query"] == "coupon"
    assert calls[0].envelope["ok"] is True
    assert calls[0].envelope["citations"]


def test_real_runner_passes_explicit_model_endpoint_and_api_key(
    monkeypatch,  # noqa: ANN001
) -> None:
    captured: dict = {}

    class FakeAIAgent:
        def __init__(self, **kwargs):  # noqa: ANN003
            captured.update(kwargs)
            captured["questions"] = []

        def run_conversation(self, question: str) -> dict:
            captured["questions"].append(question)
            captured["question"] = question
            return {"final_response": "ok", "messages": []}

    fake_module = ModuleType("run_agent")
    fake_module.AIAgent = FakeAIAgent  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "run_agent", fake_module)

    monkeypatch.setattr(
        "project_lens.integrations.feishu.hermes_loop_runner.assert_external_calls_allowed",
        lambda *args, **kwargs: None,
    )

    runner = HermesAIAgentLoopRunner(
        client=object(),
        config=ProjectLensPluginConfig(),
        provider="openai",
        model="gpt-test",
        base_url="https://example.test/v1",
        api_key="test-key",
    )
    runner._registered = True

    result = runner.run(
        question="Explain this project",
        project=ProjectRef(tenant_id="demo", project_id="payment"),
        user_id="user-1",
        chat_id="chat-1",
    )

    assert result.ok is False
    assert result.error_code == "HERMES_NO_EVIDENCE"
    assert "Hermes completed without citation-ready project evidence" in (
        result.error or ""
    )
    assert captured["provider"] == "openai"
    assert captured["model"] == "gpt-test"
    assert captured["base_url"] == "https://example.test/v1"
    assert captured["api_key"] == "test-key"
    assert captured["enabled_toolsets"] == ["projectlens"]
    assert captured["questions"][0] == "Explain this project"


def test_real_runner_appends_projectlens_context_to_ephemeral_prompt(
    monkeypatch,  # noqa: ANN001
) -> None:
    captured: dict = {}

    class FakeAIAgent:
        def __init__(self, **kwargs):  # noqa: ANN003
            captured.update(kwargs)

        def run_conversation(self, question: str) -> dict:
            return {"final_response": question, "messages": []}

    fake_module = ModuleType("run_agent")
    fake_module.AIAgent = FakeAIAgent  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "run_agent", fake_module)

    monkeypatch.setattr(
        "project_lens.integrations.feishu.hermes_loop_runner.assert_external_calls_allowed",
        lambda *args, **kwargs: None,
    )

    runner = HermesAIAgentLoopRunner(
        client=object(),
        config=ProjectLensPluginConfig(),
        api_key="test-key",
    )
    runner._registered = True
    result = runner.run(
        question="What is the owner?",
        project=ProjectRef(tenant_id="demo", project_id="payment"),
        user_id="user-1",
        chat_id="chat-1",
        context="approved_project_memories: owner=Ada",
    )

    assert result.ok is False
    assert result.error_code == "HERMES_NO_EVIDENCE"
    assert "Hermes completed without citation-ready project evidence" in (
        result.error or ""
    )
    assert "approved_project_memories: owner=Ada" in captured["ephemeral_system_prompt"]


def test_real_runner_recovers_from_empty_authorized_evidence_with_readme(
    monkeypatch,  # noqa: ANN001
) -> None:
    evidence_id = "11111111-1111-1111-1111-111111111111"

    class FakeAIAgent:
        def __init__(self, **kwargs):  # noqa: ANN003
            self.calls = 0

        def run_conversation(self, question: str) -> dict:
            self.calls += 1
            if self.calls == 1:
                return {
                    "final_response": "项目资料不足。",
                    "messages": [
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "empty-evidence",
                                    "function": {
                                        "name": "projectlens_authorized_evidence",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": "empty-evidence",
                            "content": '{"ok": true, "citations": [], "evidence_refs": []}',
                        },
                    ],
                }
            assert "README.md" in question
            return {
                "final_response": (
                    '{"facts":[{"text":"当前项目名为 DeepSeek Harness",'
                    '"citations":["11111111-1111-1111-1111-111111111111"]}]}'
                ),
                "messages": [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "readme",
                                "function": {
                                    "name": "projectlens_read_project_file",
                                    "arguments": '{"path":"README.md"}',
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "readme",
                        "content": (
                            '{"ok": true, "citations": [{"id": "'
                            + evidence_id
                            + '", "kind": "document", "source_uri": "repository:README.md", '
                            '"summary": "DeepSeek Harness"}], "evidence_refs": [{"id": "'
                            + evidence_id
                            + '", "kind": "document", "source_uri": "repository:README.md", '
                            '"summary": "DeepSeek Harness"}]}'
                        ),
                    },
                ],
            }

    fake_module = ModuleType("run_agent")
    fake_module.AIAgent = FakeAIAgent  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "run_agent", fake_module)
    monkeypatch.setattr(
        "project_lens.integrations.feishu.hermes_loop_runner.assert_external_calls_allowed",
        lambda *args, **kwargs: None,
    )
    runner = HermesAIAgentLoopRunner(client=object(), config=ProjectLensPluginConfig())
    runner._registered = True

    result = runner.run(
        question="现在接的项目叫什么？",
        project=ProjectRef(tenant_id="demo", project_id="deepseek-harness"),
        user_id="user-1",
        chat_id="chat-1",
    )

    assert result.ok is True
    assert result.verified_answer is not None
    assert result.verified_answer.facts
    assert "DeepSeek Harness" in result.verified_answer.facts[0].text
    assert [call.name for call in result.tool_calls] == [
        "projectlens_authorized_evidence",
        "projectlens_read_project_file",
    ]


def test_real_runner_marks_model_gateway_errors_as_model_failure(
    monkeypatch,  # noqa: ANN001
) -> None:
    class FakeAIAgent:
        def __init__(self, **kwargs):  # noqa: ANN003
            pass

        def run_conversation(self, question: str) -> dict:
            return {"failed": True, "error": "HTTP 502 bad gateway", "messages": []}

    fake_module = ModuleType("run_agent")
    fake_module.AIAgent = FakeAIAgent  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "run_agent", fake_module)
    monkeypatch.setattr(
        "project_lens.integrations.feishu.hermes_loop_runner.assert_external_calls_allowed",
        lambda *args, **kwargs: None,
    )
    runner = HermesAIAgentLoopRunner(client=object(), config=ProjectLensPluginConfig())
    runner._registered = True

    result = runner.run(
        question="介绍项目",
        project=ProjectRef(tenant_id="demo", project_id="payment"),
        user_id="user-1",
        chat_id="chat-1",
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error_code == "HERMES_MODEL_FAILED"
    assert "[HERMES_MODEL_FAILED@hermes.model]" in result.error
    assert "模型调用失败" in result.error


def test_real_runner_classifies_model_exception_as_model_failure(
    monkeypatch,  # noqa: ANN001
) -> None:
    class FakeAIAgent:
        def __init__(self, **kwargs):  # noqa: ANN003
            pass

        def run_conversation(self, question: str) -> dict:
            raise RuntimeError("HTTP 429 rate limit")

    fake_module = ModuleType("run_agent")
    fake_module.AIAgent = FakeAIAgent  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "run_agent", fake_module)
    monkeypatch.setattr(
        "project_lens.integrations.feishu.hermes_loop_runner.assert_external_calls_allowed",
        lambda *args, **kwargs: None,
    )
    runner = HermesAIAgentLoopRunner(client=object(), config=ProjectLensPluginConfig())
    runner._registered = True

    result = runner.run(
        question="介绍项目",
        project=ProjectRef(tenant_id="demo", project_id="payment"),
        user_id="user-1",
        chat_id="chat-1",
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error_code == "HERMES_MODEL_FAILED"
    assert "[HERMES_MODEL_FAILED@hermes.model]" in result.error
    assert "模型调用失败" in result.error


def test_real_runner_recovers_once_after_transient_provider_failure(
    monkeypatch,  # noqa: ANN001
) -> None:
    captured_prompts: list[str] = []
    evidence_id = "11111111-1111-1111-1111-111111111111"

    class FakeAIAgent:
        calls = 0

        def __init__(self, **kwargs):  # noqa: ANN003
            captured_prompts.append(str(kwargs.get("ephemeral_system_prompt") or ""))

        def run_conversation(self, question: str) -> dict:
            FakeAIAgent.calls += 1
            if FakeAIAgent.calls == 1:
                raise RuntimeError("HTTP 502 bad gateway")
            return {
                "final_response": (
                    '{"facts":[{"text":"owner is Ada",'
                    '"citations":["' + evidence_id + '"]}]}'
                ),
                "messages": [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {
                                    "name": "projectlens_search_context",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call-1",
                        "content": (
                            '{"ok":true,"citations":[{"id":"'
                            + evidence_id
                            + '","source_uri":"README.md","summary":"owner is Ada"}]}'
                        ),
                    },
                ],
            }

    fake_module = ModuleType("run_agent")
    fake_module.AIAgent = FakeAIAgent  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "run_agent", fake_module)
    monkeypatch.setattr(
        "project_lens.integrations.feishu.hermes_loop_runner.assert_external_calls_allowed",
        lambda *args, **kwargs: None,
    )
    runner = HermesAIAgentLoopRunner(
        client=object(),
        config=ProjectLensPluginConfig(),
        context_governor=ContextGovernor(
            ContextGovernorConfig(
                model_context_limit_tokens=10_000,
                soft_limit_ratio=0.45,
                summarize_ratio=0.60,
                prune_ratio=0.75,
                rotate_ratio=0.85,
                hard_limit_ratio=0.95,
                output_reserve_tokens=100,
                tool_schema_tokens=50,
                provider_overhead_tokens=25,
                max_recovery_retries=1,
            )
        ),
    )
    runner._registered = True
    context = "## Confirmed Facts\n- owner is Ada\n" + ("duplicate noise\n" * 300)

    result = runner.run(
        question="owner?",
        project=ProjectRef(tenant_id="demo", project_id="payment"),
        user_id="user-1",
        chat_id="chat-1",
        context=context,
    )

    assert result.ok is True
    assert FakeAIAgent.calls == 2
    assert len(captured_prompts) == 2
    assert captured_prompts[0].count("duplicate noise") > captured_prompts[1].count("duplicate noise")
    assert result.context_budget is not None
    assert result.context_budget["recovery_attempts"] == 1
    assert result.context_budget["recovery_action"] == "prune_and_retry"


def test_non_recoverable_auth_failure_is_not_retried() -> None:
    assert _is_recoverable_hermes_failure("HTTP 401 invalid api key") is False
    assert _is_recoverable_hermes_failure("HTTP 400 invalid request") is False
    assert _is_recoverable_hermes_failure("HTTP 502 bad gateway") is True
    assert _is_recoverable_hermes_failure("context length exceeded") is True


def test_real_runner_repr_does_not_expose_api_key() -> None:
    runner = HermesAIAgentLoopRunner(
        client=object(),
        config=ProjectLensPluginConfig(),
        api_key="secret-that-must-not-leak",
    )

    assert "secret-that-must-not-leak" not in repr(runner)


def test_real_runner_rebinds_inprocess_tools_after_aiagent_plugin_overwrite(
    monkeypatch,  # noqa: ANN001
) -> None:
    """AIAgent plugin discovery registers HTTP ApiClient handlers; Feishu must rebind."""

    rebound: list[bool] = []

    class FakeAIAgent:
        def __init__(self, **kwargs):  # noqa: ANN003
            del kwargs

        def run_conversation(self, question: str) -> dict:
            del question
            return {
                "final_response": "ok",
                "messages": [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {
                                    "name": "projectlens_search_context",
                                    "arguments": '{"query":"status"}',
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "c1",
                        "name": "projectlens_search_context",
                        "content": json_dumps_envelope(),
                    },
                ],
            }

    class FakeInProcessClient:
        def list_tools(self) -> dict:
            return {"ok": True, "tools": []}

        def call_tool(self, payload: dict) -> dict:
            del payload
            return {"ok": True}

    fake_module = ModuleType("run_agent")
    fake_module.AIAgent = FakeAIAgent  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "run_agent", fake_module)
    monkeypatch.setattr(
        "project_lens.integrations.feishu.hermes_loop_runner.assert_external_calls_allowed",
        lambda *args, **kwargs: None,
    )

    runner = HermesAIAgentLoopRunner(
        client=FakeInProcessClient(),
        config=ProjectLensPluginConfig(),
    )
    runner._registered = True

    def _force_rebind(*, force: bool = False) -> None:
        rebound.append(force)

    monkeypatch.setattr(runner, "_ensure_tools_registered", _force_rebind)

    result = runner.run(
        question="项目状态",
        project=ProjectRef(tenant_id="demo", project_id="projectlens"),
        user_id="ou_test",
        chat_id="chat-1",
    )

    assert result.ok is True
    assert True in rebound
    assert rebound[-1] is True


def json_dumps_envelope() -> str:
    import json

    return json.dumps(
        {
            "ok": True,
            "tool_name": "projectlens_search_context",
            "summary": "hit",
            "citations": [
                {
                    "id": "c1",
                    "kind": "code",
                    "source_uri": "src/a.py",
                    "summary": "a",
                }
            ],
            "evidence_refs": [
                {
                    "id": "c1",
                    "kind": "code",
                    "source_uri": "src/a.py",
                    "summary": "a",
                }
            ],
        },
        ensure_ascii=False,
    )
