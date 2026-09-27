"""Hermes LLM/tool-loop runner for FeishuHermesToolLoopBridge.

Production path uses Hermes ``AIAgent`` with ``enabled_toolsets=["projectlens"]``.
Tests inject a Fake ``HermesLoopRunner``. There is no rule-planner fallback.

Does not modify Hermes core. Does not touch EvidenceIndex / GraphStore /
filesystem — tools go through ProjectLens Tool Envelope only.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import sys
import threading
from pathlib import Path
from typing import Any, Protocol

from project_lens.config import assert_external_calls_allowed
from project_lens.context.governor import ContextGovernor
from project_lens.domain.models import ProjectRef
from project_lens.integrations.feishu.hermes_errors import (
    HERMES_INVALID_RESULT,
    HERMES_NO_EVIDENCE,
    HERMES_UNAVAILABLE,
    classify_hermes_failure,
)
from project_lens.integrations.hermes_plugin.config import ProjectLensPluginConfig
from project_lens.integrations.hermes_plugin.tools import (
    FORMAL_TOOL_NAMES,
    MEMORY_TOOL_NAMES,
    HISTORY_TOOL_NAMES,
    TOOLSET_NAME,
    build_openai_tool_schema,
    resolve_tool_catalog,
)


@dataclass(frozen=True)
class ToolLoopRequestContext:
    tenant_id: str
    project_id: str
    user_id: str
    chat_id: str


_REQUEST_CTX: ContextVar[ToolLoopRequestContext | None] = ContextVar(
    "project_lens_feishu_hermes_loop_request",
    default=None,
)
_REGISTER_LOCK = threading.Lock()


@dataclass
class HermesLoopToolCall:
    name: str
    arguments: dict[str, Any]
    envelope: dict[str, Any]


@dataclass
class HermesLoopRunResult:
    final_response: str
    tool_calls: tuple[HermesLoopToolCall, ...] = ()
    messages: tuple[Any, ...] = ()
    ok: bool = True
    error: str | None = None
    error_code: str | None = None
    error_stage: str | None = None
    answer_draft: Any | None = None
    verified_answer: Any | None = None
    context_budget: dict[str, Any] | None = None


class HermesLoopRunner(Protocol):
    def run(
        self,
        *,
        question: str,
        project: ProjectRef,
        user_id: str,
        chat_id: str,
        context: str | None = None,
    ) -> HermesLoopRunResult: ...


def _base_system_prompt() -> str:
    return (
        "You answer questions about a software project. Use only the projectlens_* read-only tools. "
        "Cite evidence from tool results. Never invent facts. Never apply, PR, deploy, rollback, or restart. "
        "For project identity or introduction, prefer projectlens_read_project_file on README.md."
    )


def _repair_answer_draft(*, agent: Any, raw_response: str) -> str:
    """Ask Hermes once to reformat its own answer without reopening research."""

    excerpt = raw_response.strip()[:4_000]
    prompt = (
        "Return only a valid JSON AnswerDraft object. Do not call tools, "
        "do not add facts, and do not change the meaning. Preserve citations "
        "only when they are present in the previous answer. Use empty arrays "
        "for fields you cannot support. Previous answer:\n"
        f"{excerpt}"
    )
    try:
        repaired = agent.run_conversation(prompt)
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(repaired, dict):
        return ""
    return str(repaired.get("final_response") or "").strip()


@dataclass
class HermesAIAgentLoopRunner:
    """Real Hermes AIAgent loop locked to the projectlens toolset."""

    client: Any
    config: ProjectLensPluginConfig
    hermes_repo: str = ""
    provider: str = "openai"
    model: str = "gpt-5.4-mini"
    base_url: str | None = None
    api_key: str | None = field(default=None, repr=False)
    max_iterations: int = 8
    context_governor: ContextGovernor = field(default_factory=ContextGovernor)
    _registered: bool = field(default=False, init=False, repr=False)

    def run(
        self,
        *,
        question: str,
        project: ProjectRef,
        user_id: str,
        chat_id: str,
        context: str | None = None,
    ) -> HermesLoopRunResult:
        assert_external_calls_allowed("hermes")
        q = question.strip()
        if not q:
            return _failure_result("empty question")

        base_system_prompt = _base_system_prompt()
        prepared_context = self.context_governor.prepare(
            context_text=context or "", question=q, system_prompt=base_system_prompt
        )
        budget = prepared_context.report.as_dict()
        if prepared_context.report.hard_blocked:
            return HermesLoopRunResult(
                final_response="", ok=False,
                error="上下文预算仍超出 ProjectLens 硬上限，已阻止发送原始请求；请开启新会话或先缩小查询范围。",
                context_budget=budget,
            )

        try:
            self._ensure_hermes_importable()
            self._ensure_tools_registered()
            from run_agent import AIAgent
        except Exception as exc:  # noqa: BLE001
            return _failure_result(
                f"Hermes unavailable: {exc}",
                default_code=HERMES_UNAVAILABLE,
                context_budget=budget,
            )

        token = _REQUEST_CTX.set(
            ToolLoopRequestContext(
                tenant_id=project.tenant_id,
                project_id=project.project_id,
                user_id=user_id,
                chat_id=chat_id,
            )
        )
        try:
            agent = AIAgent(
                base_url=self.base_url or None,
                api_key=self.api_key or None,
                provider=self.provider or None,
                model=self.model,
                quiet_mode=True,
                skip_memory=True,
                skip_context_files=True,
                enabled_toolsets=[TOOLSET_NAME],
                max_iterations=self.max_iterations,
                ephemeral_system_prompt=(
                    base_system_prompt
                    + (f"\n\n{prepared_context.text}" if prepared_context.text else "")
                ),
                user_id=user_id,
                chat_id=chat_id,
                platform="feishu",
            )
            raw = agent.run_conversation(q)
        except Exception as exc:  # noqa: BLE001
            return _failure_result(f"Hermes agent loop failed: {exc}", context_budget=budget)
        finally:
            _REQUEST_CTX.reset(token)

        if not isinstance(raw, dict):
            return _failure_result(
                "Hermes returned a non-dict conversation result",
                final_response=str(raw or ""),
                default_code=HERMES_INVALID_RESULT, context_budget=budget,
            )

        messages = tuple(raw.get("messages") or ())
        budget = self.context_governor.observe_tool_loop(
            prepared_context, messages=messages
        ).as_dict()
        tool_calls = extract_tool_calls_from_messages(messages)
        final_response = str(raw.get("final_response") or "").strip()
        failed = bool(raw.get("failed") or raw.get("error"))
        if failed:
            return _failure_result(
                str(raw.get("error") or "Hermes model call failed"),
                final_response=final_response,
                tool_calls=tool_calls,
                messages=messages, context_budget=budget,
            )
        from project_lens.agent.hermes_answer_parser import (
            parse_hermes_answer,
            parse_structured_hermes_answer,
        )
        from project_lens.agent.hermes_answer import project_answer_from_hermes

        if _needs_evidence_recovery(tool_calls=tool_calls, final_response=final_response):
            repair_raw = _run_evidence_recovery(agent=agent, question=q)
            if isinstance(repair_raw, dict):
                repair_messages = tuple(repair_raw.get("messages") or ())
                messages = messages + repair_messages
                tool_calls = extract_tool_calls_from_messages(messages)
                repaired_response = str(repair_raw.get("final_response") or "").strip()
                if repaired_response:
                    final_response = repaired_response
                if repair_raw.get("failed") or repair_raw.get("error"):
                    failed = True
                    recovery_error = str(
                        repair_raw.get("error") or "Hermes evidence recovery failed"
                    )
                else:
                    recovery_error = ""
            else:
                recovery_error = ""
        else:
            recovery_error = ""

        if failed:
            error = recovery_error or str(raw.get("error") or "Hermes model call failed")
            return _failure_result(
                error,
                final_response=final_response,
                tool_calls=tool_calls,
                messages=messages, context_budget=budget,
            )

        if not _has_citation_ready_evidence(tool_calls):
            return _failure_result(
                "Hermes completed without citation-ready project evidence",
                final_response=final_response,
                tool_calls=tool_calls,
                messages=messages,
                default_code=HERMES_NO_EVIDENCE, context_budget=budget,
            )

        answer_draft = parse_structured_hermes_answer(final_response)
        if answer_draft is None:
            repaired_response = _repair_answer_draft(
                agent=agent,
                raw_response=final_response,
            )
            repaired_draft = parse_structured_hermes_answer(repaired_response)
            if repaired_draft is not None:
                final_response = repaired_response
                answer_draft = repaired_draft
        if answer_draft is None:
            answer_draft = parse_hermes_answer(final_response)
        verified_answer = project_answer_from_hermes(
            project=project,
            draft=answer_draft,
            tool_calls=tool_calls,
        )

        return HermesLoopRunResult(
            final_response=final_response,
            tool_calls=tool_calls,
            messages=messages,
            ok=True,
            error=None,
            answer_draft=answer_draft,
            verified_answer=verified_answer,
            context_budget=budget,
        )

    def _ensure_hermes_importable(self) -> None:
        repo = (self.hermes_repo or "").strip()
        if not repo:
            return
        path = str(Path(repo).resolve())
        if path not in sys.path:
            sys.path.insert(0, path)

    def _ensure_tools_registered(self) -> None:
        if self._registered:
            return
        with _REGISTER_LOCK:
            if self._registered:
                return
            from tools.registry import registry

            catalog = resolve_tool_catalog(self.client)
            formal = [
                item
                for item in catalog
                if isinstance(item, dict)
                and item.get("name")
                in (set(FORMAL_TOOL_NAMES) | set(MEMORY_TOOL_NAMES) | set(HISTORY_TOOL_NAMES))
            ]
            if not set(FORMAL_TOOL_NAMES).issubset({item["name"] for item in formal}):
                raise RuntimeError(
                    "formal projectlens_* catalog incomplete for Hermes loop registration"
                )

            ctx = _HermesRegistryToolContext(registry=registry)
            for spec in formal:
                name = str(spec["name"])
                description = str(spec.get("description") or name)
                schema = build_openai_tool_schema(spec)
                handler = _make_request_scoped_handler(
                    name,
                    client=self.client,
                    config=self.config,
                )
                ctx.register_tool(
                    name=name,
                    toolset=TOOLSET_NAME,
                    schema=schema,
                    handler=handler,
                    description=description,
                    override=True,
                )
            registered = set(registry.get_tool_names_for_toolset(TOOLSET_NAME))
            required_names = set(FORMAL_TOOL_NAMES)
            if not required_names.issubset(registered):
                missing = sorted(required_names - registered)
                raise RuntimeError(
                    f"failed to register projectlens tools: missing={missing}"
                )
            self._registered = True


def _needs_evidence_recovery(
    *, tool_calls: tuple[HermesLoopToolCall, ...], final_response: str
) -> bool:
    if not tool_calls:
        return True
    if not _has_citation_ready_evidence(tool_calls):
        return True
    return not final_response.strip()


def _has_citation_ready_evidence(tool_calls: tuple[HermesLoopToolCall, ...]) -> bool:
    for call in tool_calls:
        if call.envelope.get("ok") is not True:
            continue
        if call.envelope.get("citations") or call.envelope.get("evidence_refs"):
            return True
    return False


def _run_evidence_recovery(*, agent: Any, question: str) -> dict[str, Any] | None:
    prompt = (
        f"The question is: {question}\n"
        "Continue the same read-only investigation. The previous tool result was "
        "empty, failed, or did not provide citation-ready evidence. Do not answer "
        "yet. For project identity or introduction, call projectlens_read_project_file "
        "on README.md first; if that path is unavailable, call "
        "projectlens_list_project_files for README/docs and then read one public file. "
        "Use projectlens_search_context only as a supplement. Do not call "
        "projectlens_authorized_evidence again unless needed."
    )
    try:
        response = agent.run_conversation(prompt)
    except Exception as exc:  # noqa: BLE001
        return {
            "failed": True,
            "error": classify_hermes_failure(str(exc)).format_run_error(),
        }
    return (
        response
        if isinstance(response, dict)
        else {
            "failed": True,
            "error": classify_hermes_failure(
                "Hermes recovery returned an invalid response",
                default_code=HERMES_INVALID_RESULT,
            ).format_run_error(),
        }
    )


def _failure_result(
    error: str,
    *,
    final_response: str = "",
    tool_calls: tuple[HermesLoopToolCall, ...] = (),
    messages: tuple[Any, ...] = (),
    default_code: str | None = None,
    context_budget: dict[str, Any] | None = None,
) -> HermesLoopRunResult:
    info = classify_hermes_failure(error, default_code=default_code)
    return HermesLoopRunResult(
        final_response=final_response,
        tool_calls=tool_calls,
        messages=messages,
        ok=False,
        error=info.format_run_error(),
        error_code=info.code,
        error_stage=info.stage,
        context_budget=context_budget,
    )


class _HermesRegistryToolContext:
    """Minimal PluginContext-like wrapper over Hermes tools.registry."""

    def __init__(self, *, registry: Any) -> None:
        self._registry = registry

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
        self._registry.register(
            name=name,
            toolset=toolset,
            schema=schema,
            handler=handler,
            check_fn=check_fn,
            requires_env=requires_env,
            is_async=is_async,
            description=description,
            emoji=emoji,
            override=override,
        )


def _make_request_scoped_handler(
    tool_name: str,
    *,
    client: Any,
    config: ProjectLensPluginConfig,
) -> Any:
    from project_lens.integrations.hermes_plugin.tools import _make_tool_handler

    base = _make_tool_handler(tool_name, client=client, config=config)

    def _handler(args: dict[str, Any] | str | None = None, **kwargs: Any) -> str:
        ctx = _REQUEST_CTX.get()
        if ctx is not None:
            kwargs.setdefault("tenant_id", ctx.tenant_id)
            kwargs.setdefault("project_id", ctx.project_id)
            kwargs.setdefault("user_id", ctx.user_id)
            kwargs.setdefault("chat_id", ctx.chat_id)
        return base(args, **kwargs)

    _handler.__name__ = tool_name
    _handler.__qualname__ = tool_name
    return _handler


def extract_tool_calls_from_messages(
    messages: tuple[Any, ...] | list[Any],
) -> tuple[HermesLoopToolCall, ...]:
    """Parse Hermes/OpenAI-style messages into ProjectLens tool call records."""

    pending: dict[str, tuple[str, dict[str, Any]]] = {}
    records: list[HermesLoopToolCall] = []

    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                call_id = str(call.get("id") or "")
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                name = str(function.get("name") or call.get("name") or "")
                arguments = _parse_arguments(function.get("arguments") or call.get("arguments"))
                if call_id and name:
                    pending[call_id] = (name, arguments)
        elif role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            name = str(message.get("name") or "")
            arguments: dict[str, Any] = {}
            if call_id and call_id in pending:
                name, arguments = pending.pop(call_id)
            envelope = _decode_envelope(message.get("content"))
            if not name:
                name = str(envelope.get("tool_name") or "")
            if name:
                records.append(
                    HermesLoopToolCall(name=name, arguments=arguments, envelope=envelope)
                )
    return tuple(records)


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}
        if isinstance(decoded, dict):
            return decoded
    return {}


def _decode_envelope(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {"ok": False, "error": raw}
        if isinstance(decoded, dict):
            return decoded
    return {"ok": False, "error": f"unexpected tool result type: {type(raw).__name__}"}
