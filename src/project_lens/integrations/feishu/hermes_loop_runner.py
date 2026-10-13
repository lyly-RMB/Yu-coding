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
import re
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
    HERMES_PARSE_FAILED,
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

_CONTEXT_EVIDENCE_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_CONTEXT_EVIDENCE_FIELD_RE = re.compile(r"\bevidence_ids=([^\r\n]+)")


@dataclass
class HermesLoopToolCall:
    name: str
    arguments: dict[str, Any]
    envelope: dict[str, Any]


def _add_inherited_context_evidence(
    *, context_text: str, tool_calls: tuple[HermesLoopToolCall, ...]
) -> tuple[HermesLoopToolCall, ...]:
    existing: set[str] = set()
    for call in tool_calls:
        for ref in (call.envelope or {}).get("evidence_refs") or (call.envelope or {}).get("citations") or ():
            if isinstance(ref, dict) and ref.get("id"):
                existing.add(str(ref["id"]))
    inherited: list[dict[str, object]] = []
    scoped_ids = [
        evidence_id
        for field in _CONTEXT_EVIDENCE_FIELD_RE.findall(context_text or "")
        for evidence_id in _CONTEXT_EVIDENCE_RE.findall(field)
    ]
    for raw in dict.fromkeys(scoped_ids):
        if raw in existing:
            continue
        inherited.append({
            "id": raw,
            "kind": "document",
            "source_uri": f"conversation:evidence/{raw}",
            "summary": "继承自前序角色讨论的证据",
        })
    if not inherited:
        return tool_calls
    synthetic = HermesLoopToolCall(
        name="projectlens_inherited_context_evidence",
        arguments={"count": len(inherited)},
        envelope={
            "ok": True,
            "tool_name": "projectlens_inherited_context_evidence",
            "citations": inherited,
            "evidence_refs": inherited,
            "summary": "inherited evidence from prior conversation turns",
        },
    )
    return (*tool_calls, synthetic)


@dataclass
class HermesLoopRunResult:
    final_response: str
    answer_text: str = ""
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
        "For project identity or introduction, prefer projectlens_read_project_file on docs/projectlens-product-document.md. "
        "README.md may be unavailable in restricted ProjectSpace scopes. "
        "After completing tool calls, return ONLY one valid JSON object, with no Markdown fences. "
        "Put the complete user-facing response in answer_text. Put evidence data separately in facts, inferences, "
        "unknowns, next_actions, and citations; each fact must have a text field and citations must reference tool evidence. "
        "For a multi-role discussion summary, format answer_text as 产品、研发、测试 sections when requested. "
        "Do not put presentation sections into the evidence fields, and do not compress answer_text to one sentence. "
        "Return one complete answer only; do not append technical/audience view duplicates. "
        "Do not finish with a natural-language answer."
    )


def _repair_answer_draft(*, agent: Any, raw_response: str) -> str:
    """Ask Hermes once to reformat its own answer without reopening research."""

    excerpt = raw_response.strip()[:4_000]
    prompt = (
        "Return ONLY a valid JSON object, with no Markdown fences. Do not call tools, "
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
    # Keep Feishu replies bounded when a tool call or provider retry stalls.
    max_iterations: int = 1
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

        recovery_attempts = 0
        recovery_action = "none"
        recovery_trigger = ""
        token = _REQUEST_CTX.set(
            ToolLoopRequestContext(
                tenant_id=project.tenant_id,
                project_id=project.project_id,
                user_id=user_id,
                chat_id=chat_id,
            )
        )
        try:
            while True:
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
                # AIAgent/plugin discovery re-registers projectlens_* with the
                # HTTP ApiClient (often a wrong/self port). Feishu loop must keep
                # the in-process Tool Envelope client so tools do not 10061.
                if hasattr(self.client, "call_tool"):
                    self._ensure_tools_registered(force=True)
                try:
                    raw = agent.run_conversation(q)
                except Exception as exc:  # noqa: BLE001
                    failure = f"Hermes agent loop failed: {exc}"
                    if recovery_attempts >= self.context_governor.config.max_recovery_retries or not _is_recoverable_hermes_failure(failure):
                        return _failure_result(failure, context_budget=_recovery_budget_audit(budget, attempts=recovery_attempts, action=recovery_action, first_error=recovery_trigger or failure))
                    recovery_attempts += 1
                    recovery_action = "prune_and_retry"
                    recovery_trigger = failure
                    prepared_context = self.context_governor.prepare(
                        context_text=prepared_context.text, question=q,
                        system_prompt=base_system_prompt, force_prune=True,
                    )
                    budget = _recovery_budget_audit(prepared_context.report.as_dict(), attempts=recovery_attempts, action=recovery_action, first_error=recovery_trigger)
                    continue
                if isinstance(raw, dict) and bool(raw.get("failed") or raw.get("error")):
                    failure = str(raw.get("error") or "Hermes model call failed")
                    if recovery_attempts < self.context_governor.config.max_recovery_retries and _is_recoverable_hermes_failure(failure):
                        recovery_attempts += 1
                        recovery_action = "prune_and_retry"
                        recovery_trigger = failure
                        prepared_context = self.context_governor.prepare(
                            context_text=prepared_context.text, question=q,
                            system_prompt=base_system_prompt, force_prune=True,
                        )
                        budget = _recovery_budget_audit(prepared_context.report.as_dict(), attempts=recovery_attempts, action=recovery_action, first_error=recovery_trigger)
                        continue
                break
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
        if recovery_attempts:
            budget = _recovery_budget_audit(budget, attempts=recovery_attempts, action=recovery_action, first_error=recovery_trigger)
        tool_calls = extract_tool_calls_from_messages(messages)
        # Carry evidence IDs from selected prior conversation turns into the
        # current verification ledger so summary answers can cite inherited
        # sources even when Hermes does not repeat the retrieval tool call.
        tool_calls = _add_inherited_context_evidence(
            context_text=prepared_context.text,
            tool_calls=tool_calls,
        )
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
            extract_hermes_answer_text,
            parse_structured_hermes_answer,
        )
        from project_lens.agent.hermes_answer import project_answer_from_hermes

        if _needs_evidence_recovery(tool_calls=tool_calls, final_response=final_response):
            repair_raw = _run_evidence_recovery(agent=agent, question=q)
            if isinstance(repair_raw, dict):
                repair_messages = tuple(repair_raw.get("messages") or ())
                messages = messages + repair_messages
                tool_calls = extract_tool_calls_from_messages(messages)
                tool_calls = _add_inherited_context_evidence(
                    context_text=prepared_context.text,
                    tool_calls=tool_calls,
                )
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

        answer_text = extract_hermes_answer_text(final_response)
        answer_draft = parse_structured_hermes_answer(final_response)
        if answer_draft is None:
            repaired_response = _repair_answer_draft(
                agent=agent,
                raw_response=final_response,
            )
            repaired_draft = parse_structured_hermes_answer(repaired_response)
            if repaired_draft is not None:
                final_response = repaired_response
                answer_text = extract_hermes_answer_text(repaired_response) or answer_text
                answer_draft = repaired_draft
        if answer_draft is None:
            # Backward-compatible provider path: retain a natural-language
            # answer when citation-ready tool evidence exists. The legacy
            # parser marks it unknown and never promotes prose to a fact.
            from project_lens.agent.hermes_answer_parser import parse_hermes_answer

            answer_draft = parse_hermes_answer(final_response)
        verified_answer = project_answer_from_hermes(
            project=project,
            draft=answer_draft,
            tool_calls=tool_calls,
            answer_text=answer_text,
        )

        return HermesLoopRunResult(
            final_response=final_response,
            answer_text=answer_text,
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

    def _ensure_tools_registered(self, *, force: bool = False) -> None:
        if self._registered and not force:
            return
        with _REGISTER_LOCK:
            if self._registered and not force:
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
        "on docs/projectlens-product-document.md first; if that path is unavailable, call "
        "projectlens_list_project_files for README.md or docs/ and then read one public file. "
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


def _is_recoverable_hermes_failure(error: str) -> bool:
    """Recognize transient provider, malformed-stream, or budget failures."""

    lowered = error.casefold()
    if any(
        marker in lowered
        for marker in (
            "401",
            "402",
            "403",
            "unauthorized",
            "forbidden",
            "invalid api key",
            "authentication",
            "invalid request",
            "bad request",
        )
    ):
        return False
    return any(
        marker in lowered
        for marker in (
            "context length",
            "context window",
            "maximum context",
            "max tokens",
            "too many tokens",
            "token limit",
            "429",
            "500",
            "502",
            "503",
            "504",
            "timeout",
            "timed out",
            "connection",
            "temporarily unavailable",
            "rate limit",
            "gateway",
            "overloaded",
            # Some OpenAI-compatible gateways occasionally return a valid
            # response followed by an extra JSON/SSE fragment. Hermes reports
            # this as json.JSONDecodeError("Extra data"); retrying the same
            # request is safe because no tool call has been executed yet.
            "extra data",
            "malformed streaming",
            "invalid json",
        )
    )


def _recovery_budget_audit(
    budget: dict[str, Any],
    *,
    attempts: int,
    action: str,
    first_error: str,
) -> dict[str, Any]:
    updated = dict(budget)
    updated["recovery_attempts"] = attempts
    updated["recovery_action"] = action
    updated["recovery_trigger"] = classify_hermes_failure(first_error).format_run_error()
    return updated


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
        raw = base(args, **kwargs)
        # Tool bodies are retrievable by their result/evidence pointer; do not
        # spend the Hermes transcript budget repeating large source documents.
        if isinstance(raw, dict):
            return json.dumps(_compact_tool_envelope(raw), ensure_ascii=False)
        if isinstance(raw, str):
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                return raw
            if isinstance(decoded, dict):
                return json.dumps(_compact_tool_envelope(decoded), ensure_ascii=False)
        return str(raw)

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


def _compact_tool_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """Keep Hermes transcripts bounded while preserving evidence provenance."""

    result_id = str(envelope.get("tool_result_id") or "")
    references = [
        ref
        for key in ("citations", "evidence_refs")
        for ref in envelope.get(key) or []
        if isinstance(ref, dict) and (ref.get("id") or ref.get("evidence_id"))
    ]
    compact_references = _dedupe_references(references)
    compact: dict[str, Any] = {
        key: envelope[key]
        for key in (
            "ok", "tool_name", "internal_tool_name", "tool_result_id", "project",
            "summary", "unknowns", "conflicts", "error_code", "message",
            "retryable", "agent_recovery_hint",
        )
        if key in envelope
    }
    compact["citations"] = compact_references
    compact["evidence_refs"] = compact_references
    compact["evidence_cards"] = _dedupe_cards([_evidence_card(ref) for ref in compact_references])
    compact["audit_ref"] = _compact_audit_ref(envelope.get("audit_ref"))
    compact["visibility_scope"] = _compact_visibility(envelope.get("visibility_scope"))
    if result_id:
        compact["detail_pointer"] = f"projectlens://tool-result/{result_id}"
    if isinstance(envelope.get("result"), dict):
        compact["result"] = _compact_payload(
            envelope["result"],
            tool_result_id=result_id,
            allow_detail_preview=str(envelope.get("internal_tool_name") or "")
            in {"read_project_file", "read_wiki_page", "get_memory_detail", "get_run_detail"},
        )
    return compact


def _evidence_card(reference: dict[str, Any]) -> dict[str, Any]:
    evidence_id = str(reference.get("id") or reference.get("evidence_id") or "")
    source_key = str(
        reference.get("source_uri")
        or reference.get("source_key")
        or reference.get("source")
        or ""
    )
    return {
        "evidence_id": evidence_id,
        "source_key": source_key,
        "revision": reference.get("revision"),
        "content_hash": reference.get("content_hash"),
        "source_type": reference.get("kind"),
        "authority": reference.get("authority") or "unknown",
        "status": reference.get("status") or "unknown",
        "short_summary": str(reference.get("summary") or "").strip()[:240],
        "key_facts": [str(item)[:240] for item in (reference.get("key_facts") or [])[:8]],
        "conflicts": [str(item)[:240] for item in (reference.get("conflicts") or [])[:8]],
        "detail_pointer": f"projectlens://evidence/{evidence_id}",
    }


def _compact_reference(reference: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "id", "evidence_id", "kind", "source_uri", "source_key", "summary",
        "revision", "content_hash", "authority", "status", "key_facts", "conflicts",
    )
    compact = {
        key: reference[key]
        for key in allowed
        if key in reference and key not in {"key_facts", "conflicts"}
    }
    for key in ("key_facts", "conflicts"):
        values = reference.get(key)
        if isinstance(values, list):
            compact[key] = [str(item)[:240] for item in values[:8]]
    return compact


def _dedupe_references(references: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str | None]] = set()
    result: list[dict[str, Any]] = []
    for reference in references:
        compact = _compact_reference(reference)
        identity = str(
            compact.get("source_uri")
            or compact.get("source_key")
            or compact.get("id")
            or compact.get("evidence_id")
            or ""
        )
        revision = compact.get("revision")
        key = (identity, str(revision) if revision is not None else None)
        if key in seen:
            continue
        seen.add(key)
        result.append(compact)
        if len(result) >= 24:
            break
    return result


def _dedupe_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str | None]] = set()
    result: list[dict[str, Any]] = []
    for card in cards[:24]:
        key = (str(card.get("source_key") or card.get("evidence_id") or ""), card.get("revision"))
        if key in seen:
            continue
        seen.add(key)
        result.append(card)
    return result


def _compact_payload(
    value: Any,
    *,
    tool_result_id: str,
    allow_detail_preview: bool = False,
    depth: int = 0,
) -> Any:
    if depth > 3:
        return "[detail omitted]"
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).casefold()
            if normalized in {"content", "raw", "body", "text"} and isinstance(item, str):
                if allow_detail_preview and depth <= 1:
                    compact[key] = item[:1_200].rstrip() + ("…" if len(item) > 1_200 else "")
                continue
            if normalized in {"observations", "events"} and isinstance(item, list):
                compact[key] = {"count": len(item), "detail_pointer": _pointer(tool_result_id)}
                continue
            compact[key] = _compact_payload(
                item,
                tool_result_id=tool_result_id,
                allow_detail_preview=allow_detail_preview,
                depth=depth + 1,
            )
        if tool_result_id and any(name in compact for name in ("memory_id", "run_id", "path")):
            compact.setdefault("detail_pointer", _pointer(tool_result_id))
        return compact
    if isinstance(value, list):
        return [
            _compact_payload(
                item,
                tool_result_id=tool_result_id,
                allow_detail_preview=allow_detail_preview,
                depth=depth + 1,
            )
            for item in value[:24]
        ]
    if isinstance(value, str):
        return value if len(value) <= 240 else value[:239].rstrip() + "…"
    return value


def _pointer(tool_result_id: str) -> str | None:
    return f"projectlens://tool-result/{tool_result_id}" if tool_result_id else None


def _compact_audit_ref(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"allow_apply": False}
    allowed = {
        "tool_name", "internal_tool_name", "project_id", "tenant_id", "policy_version",
        "role", "read_tool_calls", "read_tool_names", "allow_apply", "derived", "retrieval_mode",
    }
    result = {key: value[key] for key in allowed if key in value}
    result["allow_apply"] = False
    return result


def _compact_visibility(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        key: value[key]
        for key in (
            "tenant_id", "project_id", "actor_id", "chat_id", "role",
            "visibility_level", "policy_version",
        )
        if key in value
    }
