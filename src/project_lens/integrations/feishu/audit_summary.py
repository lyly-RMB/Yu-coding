"""Safe Feishu audit summary built from run/event/adapter audit_refs only.

Never queries EvidenceIndex. Never includes full prompts, evidence bodies,
API keys, tokens, or endpoints.
"""

from __future__ import annotations

from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict

from project_lens.domain.models import AgentRun, ProjectAnswer
from project_lens.runtime.events import AgentEvent, AgentEventType

UNKNOWN = "unknown"
OMITTED = "omitted"

_SECRET_HINTS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "password",
    "secret",
    "token",
    "private_key",
    "access_key",
    "client_secret",
    "signing_secret",
)


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FeishuAuditSummary(FrozenModel):
    skill: str = UNKNOWN
    project_scope: str = UNKNOWN
    evidence_count: str = UNKNOWN
    graph_path_count: str = UNKNOWN
    memory_count: str = UNKNOWN
    ops_signal_count: str = UNKNOWN
    provider: str = UNKNOWN
    model_name: str = UNKNOWN
    retry_count: str = UNKNOWN
    timeout_status: str = UNKNOWN
    usage_summary: str = UNKNOWN
    allow_apply: str = "False"
    trace_id: str = UNKNOWN
    agent_mode: str = UNKNOWN
    read_tool_names: str = UNKNOWN
    skill_guide: str = UNKNOWN
    evidence_ids_preview: str = UNKNOWN
    technical_summary_preview: str = UNKNOWN
    live_effective: str = UNKNOWN
    fallback_from: str = UNKNOWN

    def to_markdown(self) -> str:
        """Legacy full audit block; prefer to_debug_markdown for default cards."""

        return self.to_debug_markdown()

    def to_debug_markdown(self) -> str:
        """Bottom-of-card debug zone — Skill / provider / usage / trace only."""

        lines = [
            "**调试信息**",
            f"- trace_id: {self.trace_id}",
            f"- agent_mode: {self.agent_mode}",
            f"- 内部路由：{self.skill}",
            f"- skill_guide: {self.skill_guide}",
            f"- evidence_count: {self.evidence_count}",
            f"- evidence_ids: {self.evidence_ids_preview}",
            f"- graph_path_count: {self.graph_path_count}",
            f"- memory_count: {self.memory_count}",
            f"- ops_signal_count: {self.ops_signal_count}",
            f"- provider: {self.provider}",
            f"- model_name: {self.model_name}",
            f"- live_effective: {self.live_effective}",
            f"- fallback_from: {self.fallback_from}",
            f"- read_tools: {self.read_tool_names}",
            f"- retry_count: {self.retry_count}",
            f"- timeout: {self.timeout_status}",
            f"- usage: {self.usage_summary}",
            f"- allow_apply: {self.allow_apply}",
        ]
        if self.technical_summary_preview not in {UNKNOWN, OMITTED, ""}:
            lines.append(f"- technical_summary: {self.technical_summary_preview}")
        lines.extend(
            [
                "- 本区只展示安全 audit refs，不含完整 prompt / Evidence 正文 / 密钥。",
                "- Skill / SkillGuide / tool trail 是内部策略，不是用户主概念。",
            ]
        )
        return "\n".join(lines)


def build_feishu_audit_summary(
    run: AgentRun,
    answer: ProjectAnswer,
    *,
    events: tuple[AgentEvent, ...] = (),
    model_adapter_refs: Mapping[str, Any] | None = None,
    context_pack_refs: Mapping[str, Any] | None = None,
    context_prompt_refs: Mapping[str, Any] | None = None,
) -> FeishuAuditSummary:
    analyzing = _analyzing_payload(events)
    adapter_refs = _strip_secrets(dict(model_adapter_refs or {}))
    if not adapter_refs and analyzing:
        raw = analyzing.get("model_adapter")
        if isinstance(raw, Mapping):
            adapter_refs = _strip_secrets(dict(raw))
    pack_refs = _strip_secrets(dict(context_pack_refs or {}))
    if not pack_refs and analyzing:
        raw = analyzing.get("context_pack")
        if isinstance(raw, Mapping):
            pack_refs = _strip_secrets(dict(raw))
    prompt_refs = _strip_secrets(dict(context_prompt_refs or {}))
    if not prompt_refs and analyzing:
        raw = analyzing.get("context_prompt")
        if isinstance(raw, Mapping):
            prompt_refs = _strip_secrets(dict(raw))
    if not prompt_refs:
        prompt_refs = _prompt_refs_from_events(events)

    project = answer.project
    scope_parts = [
        f"tenant={project.tenant_id}",
        f"project={project.project_id}",
    ]
    if project.service:
        scope_parts.append(f"service={project.service}")
    if project.environment:
        scope_parts.append(f"env={project.environment}")
    access_scope = pack_refs.get("access_scope") or prompt_refs.get("access_scope")
    if access_scope:
        scope_parts.append(f"access_scope={_safe_scalar(access_scope)}")

    usage_summary = _format_usage(adapter_refs.get("usage"))

    timeout_raw = adapter_refs.get("timed_out")
    if timeout_raw is True:
        timeout_status = "timed_out"
    elif timeout_raw is False:
        timeout_status = "ok"
    else:
        timeout_status = UNKNOWN

    model_name = _safe_scalar(adapter_refs.get("model_name"))
    if model_name in {UNKNOWN, OMITTED}:
        model_name = _model_name_from_notes(adapter_refs.get("notes"))

    agent_mode = _safe_scalar(
        run.runtime
        or analyzing.get("agent_mode")
        or _lifecycle_field(events, "agent_mode")
    )
    read_tool_names = _format_tool_names(
        analyzing.get("tool_names"),
        (analyzing.get("read_audit") or {}).get("read_tool_names")
        if isinstance(analyzing.get("read_audit"), Mapping)
        else None,
        _tool_names_from_events(events),
    )
    skill_guide = _safe_scalar(
        analyzing.get("skill_guide") or _lifecycle_field(events, "skill_guide")
    )
    evidence_ids_preview = _evidence_ids_preview(answer)
    tech_preview = (answer.technical_summary or "").strip()
    if len(tech_preview) > 180:
        tech_preview = tech_preview[:179] + "…"

    live_effective = _safe_scalar(
        adapter_refs.get("live_effective")
        if adapter_refs.get("live_effective") is not None
        else adapter_refs.get("live")
    )
    fallback_from = _note_field(adapter_refs.get("notes"), "fallback_from")
    if fallback_from in {UNKNOWN, OMITTED}:
        fallback_from = _safe_scalar(adapter_refs.get("fallback_from"))

    return FeishuAuditSummary(
        skill=_safe_scalar(
            answer.skill
            or pack_refs.get("skill")
            or prompt_refs.get("skill")
            or analyzing.get("skill")
        ),
        project_scope="；".join(scope_parts) if scope_parts else UNKNOWN,
        evidence_count=_count_field(
            analyzing.get("evidence_count"),
            pack_refs.get("evidence_ids"),
            prompt_refs.get("evidence_ids"),
            fallback_len=len(answer.evidence),
        ),
        graph_path_count=_count_field(analyzing.get("graph_path_count")),
        memory_count=_count_field(
            analyzing.get("memory_count"),
            pack_refs.get("memory_ids"),
            prompt_refs.get("memory_ids"),
        ),
        ops_signal_count=_count_field(analyzing.get("ops_signal_count")),
        provider=_safe_scalar(adapter_refs.get("provider")),
        model_name=model_name,
        retry_count=_count_field(adapter_refs.get("retries")),
        timeout_status=timeout_status,
        usage_summary=usage_summary,
        # Always False on Feishu cards — never advertise Apply.
        allow_apply="False",
        trace_id=str(run.trace_id) if run.trace_id else UNKNOWN,
        agent_mode=agent_mode,
        read_tool_names=read_tool_names,
        skill_guide=skill_guide,
        evidence_ids_preview=evidence_ids_preview,
        technical_summary_preview=tech_preview or UNKNOWN,
        live_effective=live_effective,
        fallback_from=fallback_from,
    )


def _evidence_ids_preview(answer: ProjectAnswer) -> str:
    ids = [str(item.id) for item in answer.evidence[:5]]
    if not ids:
        return UNKNOWN
    preview = ", ".join(ids)
    if len(answer.evidence) > 5:
        preview += f" …(+{len(answer.evidence) - 5})"
    return preview


def _analyzing_payload(events: tuple[AgentEvent, ...]) -> dict[str, Any]:
    for event in reversed(events):
        payload = event.payload or {}
        if payload.get("status") == "analyzing":
            return dict(payload)
    return {}


def _lifecycle_field(events: tuple[AgentEvent, ...], key: str) -> Any:
    for event in reversed(events):
        payload = event.payload or {}
        if key in payload:
            return payload.get(key)
        if event.type == AgentEventType.LIFECYCLE and key in payload:
            return payload.get(key)
    return None


def _tool_names_from_events(events: tuple[AgentEvent, ...]) -> list[str]:
    names: list[str] = []
    for event in events:
        payload = event.payload or {}
        if event.type == AgentEventType.TOOL_COMPLETED:
            tool = payload.get("tool")
            if tool:
                names.append(str(tool))
            continue
        if event.type == AgentEventType.LIFECYCLE and payload.get("lifecycle") == "tool.completed":
            tool = payload.get("tool")
            if tool:
                names.append(str(tool))
    return names


def _format_tool_names(*candidates: Any) -> str:
    for value in candidates:
        if value is None:
            continue
        if isinstance(value, str) and value.strip():
            return _safe_scalar(value)
        if isinstance(value, (list, tuple)) and value:
            joined = ", ".join(str(item) for item in value[:12])
            return _safe_scalar(joined)
    return UNKNOWN


def _prompt_refs_from_events(events: tuple[AgentEvent, ...]) -> dict[str, Any]:
    for event in reversed(events):
        payload = event.payload or {}
        if payload.get("status") == "analyzing":
            raw = payload.get("context_prompt")
            if isinstance(raw, Mapping):
                return _strip_secrets(dict(raw))
        if event.type == AgentEventType.LIFECYCLE:
            raw = payload.get("context_prompt")
            if isinstance(raw, Mapping):
                return _strip_secrets(dict(raw))
    return {}


def _count_field(*candidates: Any, fallback_len: int | None = None) -> str:
    for value in candidates:
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            return str(len(value))
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(int(value))
        if isinstance(value, str) and value.strip().isdigit():
            return value.strip()
    if fallback_len is not None:
        return str(fallback_len)
    return UNKNOWN


def _format_usage(usage: Any) -> str:
    if not isinstance(usage, Mapping) or not usage:
        return UNKNOWN
    prompt_tokens = usage.get("prompt_tokens", UNKNOWN)
    completion_tokens = usage.get("completion_tokens", UNKNOWN)
    total_tokens = usage.get("total_tokens", UNKNOWN)
    return (
        f"prompt={_safe_scalar(prompt_tokens)}; "
        f"completion={_safe_scalar(completion_tokens)}; "
        f"total={_safe_scalar(total_tokens)}"
    )


def _model_name_from_notes(notes: Any) -> str:
    if not isinstance(notes, (list, tuple)):
        return UNKNOWN
    for note in notes:
        text = str(note)
        if text.startswith("model_name="):
            return _safe_scalar(text.split("=", 1)[1])
    return UNKNOWN


def _note_field(notes: Any, key: str) -> str:
    prefix = f"{key}="
    if not isinstance(notes, (list, tuple)):
        return UNKNOWN
    for note in notes:
        text = str(note)
        if text.startswith(prefix):
            return _safe_scalar(text[len(prefix) :])
    return UNKNOWN


def _safe_scalar(value: Any) -> str:
    if value is None:
        return UNKNOWN
    text = str(value).strip()
    if not text:
        return UNKNOWN
    lowered = text.lower()
    if any(hint in lowered for hint in _SECRET_HINTS):
        return OMITTED
    if len(text) > 200:
        return OMITTED
    return text


def _strip_secrets(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in payload.items():
        key_l = str(key).lower()
        if any(hint in key_l for hint in _SECRET_HINTS):
            continue
        if isinstance(value, Mapping):
            cleaned[key] = _strip_secrets(dict(value))
        elif isinstance(value, list):
            cleaned[key] = [
                _strip_secrets(item) if isinstance(item, Mapping) else item
                for item in value
                if not (
                    isinstance(item, str)
                    and any(hint in item.lower() for hint in _SECRET_HINTS)
                )
            ]
        else:
            cleaned[key] = value
    return cleaned
