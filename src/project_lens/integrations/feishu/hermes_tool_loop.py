"""Feishu -> Hermes AIAgent project tool-loop bridge.

Natural-language project questions optionally enter a real Hermes LLM/tool
loop via ``HermesLoopRunner``. Tools are only the formal projectlens_* set;
execution goes through ProjectLens Tool Envelope (RolePolicy / ChatVisibility
applied before tool work). Tool traces map to LifecycleBus / agent_events via
the real AgentRun id supplied by HermesRuntimeService. No rule planner. No
Hermes core changes.
No direct EvidenceIndex / GraphStore / filesystem access from this adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from project_lens.application.project_agent_tools import ProjectAgentToolService
from project_lens.context.governor import ContextGovernor
from project_lens.domain.models import ProjectRef
from project_lens.integrations.feishu.cards import FeishuCard, _markdown
from project_lens.integrations.feishu.hermes_context import HermesProjectContext
from project_lens.integrations.feishu.hermes_loop_runner import (
    HermesAIAgentLoopRunner,
    HermesLoopRunner,
    HermesLoopRunResult,
    HermesLoopToolCall,
)
from project_lens.integrations.hermes_plugin.config import ProjectLensPluginConfig
from project_lens.integrations.hermes_plugin.render import render_answer_envelope
from project_lens.runtime.lifecycle import LifecycleBus, LifecycleEventType

HERMES_LOOP_MODE = "feishu_hermes_llm_tool_loop"


@dataclass
class FeishuHermesToolLoopResult:
    ok: bool
    envelope: dict[str, Any]
    tool_names: tuple[str, ...] = ()
    output_markdown: str = ""
    loop_id: UUID | None = None
    trace_id: UUID | None = None
    answer_draft: Any | None = None
    verified_answer: Any | None = None
    # GenAI trace inputs (S05): full message chain + tool envelopes with bodies.
    messages: tuple[Any, ...] = ()
    tool_calls: tuple[HermesLoopToolCall, ...] = ()
    final_response: str = ""


class ProjectLensToolClient:
    """In-process adapter around ProjectAgentToolService."""

    def __init__(self, service: ProjectAgentToolService) -> None:
        self._service = service

    def list_tools(self) -> dict[str, Any]:
        return self._service.list_tools()

    def call_tool(self, payload: dict[str, Any]) -> dict[str, Any]:
        from project_lens.application.project_agent_tools import ProjectAgentToolCallRequest

        request = ProjectAgentToolCallRequest(
            tool_name=str(payload["tool_name"]),
            project=ProjectRef.model_validate(payload["project"]),
            user_id=str(payload["user_id"]),
            chat_id=str(payload["chat_id"]),
            arguments=dict(payload.get("arguments") or {}),
        )
        return _run_async(self._service.call_tool(request))


class FeishuHermesToolLoopBridge:
    """Feishu bridge that delegates tool selection to a HermesLoopRunner."""

    def __init__(
        self,
        *,
        tool_service: ProjectAgentToolService,
        config: ProjectLensPluginConfig,
        loop_runner: HermesLoopRunner | None = None,
        lifecycle: LifecycleBus | None = None,
        hermes_repo: str = "",
        hermes_provider: str = "openai",
        hermes_model: str = "gpt-5.4-mini",
        hermes_base_url: str | None = None,
        hermes_api_key: str | None = None,
        context_governor: ContextGovernor | None = None,
    ) -> None:
        self._tool_service = tool_service
        self._config = config
        self._lifecycle = lifecycle
        self._client = ProjectLensToolClient(tool_service)
        self._loop_runner = loop_runner or HermesAIAgentLoopRunner(
            client=self._client,
            config=config,
            hermes_repo=hermes_repo,
            provider=hermes_provider,
            model=hermes_model,
            base_url=hermes_base_url,
            api_key=hermes_api_key,
            context_governor=context_governor or ContextGovernor(),
        )
        self.last_tool_names: tuple[str, ...] = ()
        self.last_tool_calls: tuple[HermesLoopToolCall, ...] = ()
        self.last_envelope: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.last_loop_id: UUID | None = None
        self.last_trace_id: UUID | None = None

    async def answer(
        self,
        *,
        project: ProjectRef,
        question: str,
        user_id: str,
        chat_id: str,
        context: HermesProjectContext | None = None,
        run_id: UUID | None = None,
        loop_id: UUID | None = None,
        trace_id: UUID | None = None,
    ) -> FeishuHermesToolLoopResult:
        loop_id = loop_id or uuid4()
        trace_id = trace_id or uuid4()
        result = await _run_runner_async(
            self._loop_runner,
            question=question,
            project=project,
            user_id=user_id,
            chat_id=chat_id,
            context=context.text if context is not None else None,
        )
        envelope = _build_answer_envelope(
            question=question,
            project=project,
            user_id=user_id,
            chat_id=chat_id,
            result=result,
            loop_id=loop_id,
            trace_id=trace_id,
            context=context,
        )
        await _emit_hermes_tool_audit_events(
            lifecycle=self._lifecycle,
            project=project,
            run_id=run_id or loop_id,
            loop_id=loop_id,
            trace_id=trace_id,
            result=result,
        )
        self.last_tool_calls = result.tool_calls
        self.last_tool_names = tuple(call.name for call in result.tool_calls)
        self.last_envelope = envelope
        self.last_error = result.error
        self.last_loop_id = loop_id
        self.last_trace_id = trace_id
        markdown = render_answer_envelope(envelope, audience="team")
        return FeishuHermesToolLoopResult(
            ok=bool(result.ok),
            envelope=envelope,
            tool_names=self.last_tool_names,
            output_markdown=markdown,
            loop_id=loop_id,
            trace_id=trace_id,
            answer_draft=result.answer_draft,
            verified_answer=result.verified_answer,
            messages=tuple(result.messages or ()),
            tool_calls=tuple(result.tool_calls or ()),
            final_response=str(result.final_response or ""),
        )


def render_hermes_tool_loop_card(markdown: str) -> dict[str, Any]:
    return FeishuCard(
        title="ProjectLens Hermes Tool Loop",
        elements=[_markdown(markdown)],
    ).to_payload()


def hermes_loop_scratchpad_entry(envelope: dict[str, Any]) -> dict[str, Any]:
    """Compact session scratchpad payload for one Hermes tool-loop answer."""

    audit = envelope.get("audit_ref") if isinstance(envelope.get("audit_ref"), dict) else {}
    return {
        "loop_id": audit.get("loop_id"),
        "trace_id": audit.get("trace_id"),
        "tool_names": list(audit.get("tool_names") or []),
        "hermes_ok": bool(audit.get("hermes_ok")),
        "allow_apply": False,
        "mode": HERMES_LOOP_MODE,
    }


async def _emit_hermes_tool_audit_events(
    *,
    lifecycle: LifecycleBus | None,
    project: ProjectRef,
    run_id: UUID,
    loop_id: UUID,
    trace_id: UUID,
    result: HermesLoopRunResult,
) -> None:
    if lifecycle is None:
        return
    for index, call in enumerate(result.tool_calls):
        await lifecycle.emit_async(
            LifecycleEventType.TOOL_REQUESTED,
            run_id=run_id,
            trace_id=trace_id,
            project=project,
            payload={
                "mode": HERMES_LOOP_MODE,
                "tool": call.name,
                "index": index,
                "arguments": dict(call.arguments or {}),
                "allow_apply": False,
            },
        )
        citation_ids = [
            item.get("id")
            for item in _list_of_dicts(call.envelope.get("citations"))
            if item.get("id")
        ]
        await lifecycle.emit_async(
            LifecycleEventType.TOOL_COMPLETED,
            run_id=run_id,
            trace_id=trace_id,
            project=project,
            payload={
                "mode": HERMES_LOOP_MODE,
                "tool": call.name,
                "index": index,
                "ok": call.envelope.get("ok") is True,
                "tool_result_id": call.envelope.get("tool_result_id"),
                "citation_ids": citation_ids,
                "allow_apply": False,
            },
        )
    await lifecycle.emit_async(
        LifecycleEventType.ANSWER_COMPOSED,
        run_id=run_id,
        trace_id=trace_id,
        project=project,
        payload={
            "mode": HERMES_LOOP_MODE,
            "hermes_loop": "completed",
            "hermes_ok": bool(result.ok),
            "tool_count": len(result.tool_calls),
            "allow_apply": False,
            "error": result.error,
        },
    )


def _build_answer_envelope(
    *,
    question: str,
    project: ProjectRef,
    user_id: str,
    chat_id: str,
    result: HermesLoopRunResult,
    loop_id: UUID,
    trace_id: UUID,
    context: HermesProjectContext | None = None,
) -> dict[str, Any]:
    citations: list[dict[str, Any]] = []
    evidence_refs: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    unknowns: list[str] = []
    conflicts: list[str] = []
    next_actions: list[dict[str, Any]] = []
    tool_calls_payload: list[dict[str, Any]] = []

    for call in result.tool_calls:
        tool_calls_payload.append(
            {
                "name": call.name,
                "arguments": call.arguments,
                "ok": call.envelope.get("ok"),
                "tool_result_id": call.envelope.get("tool_result_id"),
            }
        )
        citations.extend(_list_of_dicts(call.envelope.get("citations")))
        evidence_refs.extend(_list_of_dicts(call.envelope.get("evidence_refs")))
        if call.envelope.get("ok") is True:
            summary = str(call.envelope.get("summary") or "").strip()
            if summary:
                citation_ids = [
                    item.get("id")
                    for item in _list_of_dicts(call.envelope.get("citations"))
                    if item.get("id")
                ]
                facts.append({"text": summary, "citations": citation_ids})
            unknowns.extend(
                str(item) for item in call.envelope.get("unknowns") or [] if str(item).strip()
            )
            conflicts.extend(
                str(item) for item in call.envelope.get("conflicts") or [] if str(item).strip()
            )
        else:
            hint = str(call.envelope.get("agent_recovery_hint") or "").strip()
            if hint:
                next_actions.append({"title": hint, "requires_approval": False})
            unknowns.extend(
                str(item) for item in call.envelope.get("unknowns") or [] if str(item).strip()
            )

    if result.ok:
        answer_summary = (
            result.final_response.strip()
            or _compose_summary(question, result.tool_calls)
        )
    else:
        error = (result.error or "Hermes tool loop unavailable").strip()
        answer_summary = (
            f"Hermes LLM/tool loop 不可用，未能回答「{question}」。原因：{error}"
        )
        if error not in unknowns:
            unknowns.append(error)
        next_actions.append(
            {
                "title": "检查 Hermes 安装路径与模型凭证，然后重试。",
                "requires_approval": False,
            }
        )

    audit_ref = {
        "loop_id": str(loop_id),
        "trace_id": str(trace_id),
        "tool_names": [call.name for call in result.tool_calls],
        "allow_apply": False,
        "mode": HERMES_LOOP_MODE,
        "project_id": project.project_id,
        "tenant_id": project.tenant_id,
        "chat_id": chat_id,
        "user_id": user_id,
        "hermes_ok": bool(result.ok),
        "context_budget": result.context_budget,
        "context": context.audit_refs if context is not None else None,
        "answer_draft": result.answer_draft is not None,
    }
    if result.error:
        audit_ref["error"] = result.error
    if result.error_code:
        audit_ref["error_code"] = result.error_code
    if result.error_stage:
        audit_ref["error_stage"] = result.error_stage

    if not evidence_refs and citations:
        evidence_refs = list(citations)

    return {
        "ok": bool(result.ok),
        "answer_text": str(
            getattr(result.verified_answer, "answer_text", "")
            or result.final_response
            or answer_summary
        ).strip(),
        "answer_summary": answer_summary,
        "facts": facts,
        "inferences": [],
        "unknowns": unknowns,
        "conflicts": conflicts,
        "next_actions": next_actions,
        "citations": citations,
        "evidence_refs": evidence_refs,
        "tool_calls": tool_calls_payload,
        "audit_ref": audit_ref,
        "role_views_available": [
            "team",
            "technical",
            "business",
            "qa",
            "manager",
            "evidence",
            "onboarding",
            "debug",
            "map",
        ],
    }


def _compose_summary(question: str, calls: tuple[HermesLoopToolCall, ...]) -> str:
    if not calls:
        return f"Hermes 已处理「{question}」，但未调用只读工具。"
    tool_names = "、".join(call.name for call in calls)
    return f"针对「{question}」已通过 Hermes LLM/tool loop 调查：{tool_names}。"


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


async def _run_runner_async(
    runner: HermesLoopRunner,
    *,
    question: str,
    project: ProjectRef,
    user_id: str,
    chat_id: str,
    context: str | None = None,
) -> HermesLoopRunResult:
    import asyncio
    import inspect

    kwargs: dict[str, Any] = {
        "question": question,
        "project": project,
        "user_id": user_id,
        "chat_id": chat_id,
    }
    if context is not None:
        try:
            supports_context = "context" in inspect.signature(runner.run).parameters
        except (TypeError, ValueError):
            supports_context = False
        if supports_context:
            kwargs["context"] = context

    # A provider/tool-call deadlock must not leave Feishu stuck on
    # "正在回答" forever. The worker thread may finish later, but the user
    # receives a bounded failure response and can retry safely.
    # Qwen tool calls can require a second model turn after the tool result.
    # Keep a bounded response window without aborting a healthy loop early.
    from project_lens.config import settings

    timeout_seconds = max(30.0, float(settings.feishu_hermes_loop_timeout_seconds or 120.0))
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(runner.run, **kwargs),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        return HermesLoopRunResult(
            final_response="",
            ok=False,
            error=(
                f"Hermes 工具循环超过 {int(timeout_seconds)} 秒未完成。"
                "模型基础连接正常，但当前工具调用链未返回；"
                "请缩小问题范围或稍后重试。"
            ),
            error_code="HERMES_TIMEOUT",
            error_stage="hermes.agent_loop",
        )


def _run_async(coro: Any) -> Any:
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    if loop.is_running():
        return _run_in_new_loop(coro)
    return loop.run_until_complete(coro)


def _run_in_new_loop(coro: Any) -> Any:
    import asyncio
    import threading

    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001
            error["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error["error"]
    return result.get("value")
