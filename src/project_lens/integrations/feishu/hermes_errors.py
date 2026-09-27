"""Structured Hermes/Feishu failure codes for locating bugs in the IM path.

Error strings posted to Feishu use a stable prefix:

    [CODE@stage] human message

so operators can tell whether the break was import, model gateway, evidence,
verify demotion, ProjectLens runtime, or Feishu delivery — without reading logs.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
from uuid import UUID

# --- stable error codes (mirror HTTP ask vocabulary where possible) ---

HERMES_UNAVAILABLE = "HERMES_UNAVAILABLE"
HERMES_EMPTY_QUESTION = "HERMES_EMPTY_QUESTION"
HERMES_MODEL_FAILED = "HERMES_MODEL_FAILED"
HERMES_AGENT_LOOP_FAILED = "HERMES_AGENT_LOOP_FAILED"
HERMES_INVALID_RESULT = "HERMES_INVALID_RESULT"
HERMES_NO_EVIDENCE = "HERMES_NO_EVIDENCE"
HERMES_PARSE_FAILED = "HERMES_PARSE_FAILED"
HERMES_VERIFY_DEMOTED = "HERMES_VERIFY_DEMOTED"
HERMES_INSUFFICIENT_EVIDENCE = "HERMES_INSUFFICIENT_EVIDENCE"
PROJECTLENS_RUNTIME = "PROJECTLENS_RUNTIME"
FEISHU_DELIVERY_FAILED = "FEISHU_DELIVERY_FAILED"
PROJECTLENS_UNEXPECTED = "PROJECTLENS_UNEXPECTED"

# --- stages (where in the pipeline it failed) ---

STAGE_IMPORT = "hermes.import"
STAGE_QUESTION = "hermes.question"
STAGE_MODEL = "hermes.model"
STAGE_AGENT_LOOP = "hermes.agent_loop"
STAGE_RESULT = "hermes.result"
STAGE_EVIDENCE = "hermes.evidence"
STAGE_PARSE = "hermes.parse"
STAGE_VERIFY = "hermes.verify"
STAGE_RUNTIME = "projectlens.runtime"
STAGE_DELIVERY = "feishu.delivery"
STAGE_OUTER = "feishu.outer"

_ERROR_PREFIX_RE = re.compile(
    r"^\[(?P<code>[A-Z0-9_]+)@(?P<stage>[a-z0-9_.]+)\]\s*(?P<message>.*)$",
    re.DOTALL,
)

_HINTS: dict[str, str] = {
    HERMES_UNAVAILABLE: "定位：Hermes 安装路径 / AIAgent 导入失败，检查 hermes_repo 与依赖。",
    HERMES_EMPTY_QUESTION: "定位：问题为空，检查飞书卡片提交的 question。",
    HERMES_MODEL_FAILED: "定位：上游模型/网关不可用（429/5xx/rate limit），检查 Hermes 凭证与模型服务。",
    HERMES_AGENT_LOOP_FAILED: "定位：Hermes run_conversation 抛错（非典型网关），查 agent loop 日志。",
    HERMES_INVALID_RESULT: "定位：Hermes 返回了非 dict 结果，查 AIAgent.run_conversation 输出。",
    HERMES_NO_EVIDENCE: "定位：工具循环结束但无 citation-ready Evidence，查 projectlens_* 工具调用与权限。",
    HERMES_PARSE_FAILED: "定位：Hermes 终稿无法解析为 AnswerDraft，查 final_response / repair 路径。",
    HERMES_VERIFY_DEMOTED: "定位：有草稿但 Evidence 引用校验未通过，查 verifier / citations。",
    HERMES_INSUFFICIENT_EVIDENCE: "定位：资料不足，查索引同步与检索权限，非模型崩溃。",
    PROJECTLENS_RUNTIME: "定位：HermesRuntimeService.execute_prepared 异常，查 bridge / complete_external。",
    FEISHU_DELIVERY_FAILED: "定位：回答已生成但飞书 post_text/post_card 失败，查 tenant token 与 IM API。",
    PROJECTLENS_UNEXPECTED: "定位：飞书后台任务未捕获的外层异常，查 service 日志栈。",
}


@dataclass(frozen=True)
class HermesErrorInfo:
    code: str
    stage: str
    message: str

    def format_run_error(self) -> str:
        detail = (self.message or "").strip() or self.code
        return f"[{self.code}@{self.stage}] {detail}"


def classify_hermes_failure(raw: str, *, default_code: str | None = None) -> HermesErrorInfo:
    """Map a raw exception/error string into a stable code + stage."""

    text = (raw or "").strip() or "unknown error"
    existing = parse_hermes_error(text)
    if existing is not None:
        return existing

    lowered = text.casefold()

    if "empty question" in lowered:
        return HermesErrorInfo(HERMES_EMPTY_QUESTION, STAGE_QUESTION, text)

    if "hermes unavailable" in lowered or "failed to register projectlens" in lowered:
        return HermesErrorInfo(HERMES_UNAVAILABLE, STAGE_IMPORT, text)

    if "without citation-ready" in lowered or "citation-ready project evidence" in lowered:
        return HermesErrorInfo(HERMES_NO_EVIDENCE, STAGE_EVIDENCE, text)

    if "non-dict" in lowered or "invalid response" in lowered:
        return HermesErrorInfo(HERMES_INVALID_RESULT, STAGE_RESULT, text)

    if "hermes runtime failed" in lowered:
        return HermesErrorInfo(PROJECTLENS_RUNTIME, STAGE_RUNTIME, text)

    model_markers = (
        "502",
        "429",
        "503",
        "504",
        "rate limit",
        "gateway",
        "模型调用失败",
        "model call",
        "openai",
        "anthropic",
        "http error",
        "status_code",
        "status code",
    )
    if any(marker in lowered for marker in model_markers):
        # Keep the historical Chinese prefix for log/grep continuity.
        message = text if "模型调用失败" in text else f"模型调用失败：{text}"
        return HermesErrorInfo(HERMES_MODEL_FAILED, STAGE_MODEL, message)

    if "agent loop failed" in lowered or "evidence recovery failed" in lowered:
        return HermesErrorInfo(HERMES_AGENT_LOOP_FAILED, STAGE_AGENT_LOOP, text)

    if default_code == HERMES_MODEL_FAILED:
        message = text if "模型调用失败" in text else f"模型调用失败：{text}"
        return HermesErrorInfo(HERMES_MODEL_FAILED, STAGE_MODEL, message)

    if default_code:
        stage = {
            HERMES_UNAVAILABLE: STAGE_IMPORT,
            HERMES_NO_EVIDENCE: STAGE_EVIDENCE,
            HERMES_INVALID_RESULT: STAGE_RESULT,
            HERMES_AGENT_LOOP_FAILED: STAGE_AGENT_LOOP,
            PROJECTLENS_RUNTIME: STAGE_RUNTIME,
            PROJECTLENS_UNEXPECTED: STAGE_OUTER,
            FEISHU_DELIVERY_FAILED: STAGE_DELIVERY,
        }.get(default_code, STAGE_AGENT_LOOP)
        return HermesErrorInfo(default_code, stage, text)

    return HermesErrorInfo(HERMES_AGENT_LOOP_FAILED, STAGE_AGENT_LOOP, text)


def parse_hermes_error(raw: str) -> HermesErrorInfo | None:
    match = _ERROR_PREFIX_RE.match((raw or "").strip())
    if match is None:
        return None
    return HermesErrorInfo(
        code=match.group("code"),
        stage=match.group("stage"),
        message=(match.group("message") or "").strip(),
    )


def classify_degraded_answer(answer: Any) -> HermesErrorInfo:
    """Soft success path: COMPLETED run but not a Hermes-verified conclusion."""

    from project_lens.integrations.feishu.views import (
        _DEGRADED_SUMMARY_MARKERS,
        _DEGRADED_UNKNOWN_MARKERS,
        is_evidence_insufficient,
    )

    summary = f"{getattr(answer, 'business_summary', '')} {getattr(answer, 'conclusion', '')}"
    unknowns_blob = " ".join(getattr(answer, "unknowns", ()) or ())

    if "Hermes 返回了未结构化文本" in summary:
        return HermesErrorInfo(
            HERMES_PARSE_FAILED,
            STAGE_PARSE,
            "Hermes 返回了未结构化文本，未能解析为 AnswerDraft。",
        )
    if "Hermes 未返回最终答案" in summary or "Hermes 未返回最终答案" in unknowns_blob:
        return HermesErrorInfo(
            HERMES_PARSE_FAILED,
            STAGE_PARSE,
            "Hermes 未返回最终答案。",
        )
    if "尚未完成 Evidence 引用校验" in unknowns_blob:
        return HermesErrorInfo(
            HERMES_VERIFY_DEMOTED,
            STAGE_VERIFY,
            "尚未完成 Evidence 引用校验。",
        )
    if is_evidence_insufficient(answer) or any(
        marker in summary for marker in _DEGRADED_SUMMARY_MARKERS
    ):
        return HermesErrorInfo(
            HERMES_INSUFFICIENT_EVIDENCE,
            STAGE_EVIDENCE,
            "当前资料不足以形成带引用结论。",
        )
    if any(marker in unknowns_blob for marker in _DEGRADED_UNKNOWN_MARKERS):
        return HermesErrorInfo(
            HERMES_VERIFY_DEMOTED,
            STAGE_VERIFY,
            "调查结束，但没有形成可展示的结论。",
        )
    return HermesErrorInfo(
        HERMES_VERIFY_DEMOTED,
        STAGE_VERIFY,
        "没有通过引用校验的可展示结论，或当前资料不足。",
    )


def format_failure_for_feishu(
    *,
    error: str | None,
    run_id: UUID | str | None = None,
) -> str:
    """Plain-text failure body for Feishu IM (hard failure path)."""

    info = classify_hermes_failure(error or "")
    return _compose_feishu_error_text(
        info=info,
        run_id=run_id,
        heading_kind="failure",
    )


def format_degraded_banner_for_feishu(
    answer: Any,
    *,
    run_id: UUID | str | None = None,
) -> str:
    """Banner prepended to soft-degraded answers."""

    info = classify_degraded_answer(answer)
    return _compose_feishu_error_text(
        info=info,
        run_id=run_id,
        heading_kind="degraded",
    )


def format_unexpected_for_feishu(
    exc: BaseException,
    *,
    run_id: UUID | str | None = None,
) -> str:
    info = HermesErrorInfo(
        PROJECTLENS_UNEXPECTED,
        STAGE_OUTER,
        f"{type(exc).__name__}: {exc}",
    )
    return _compose_feishu_error_text(
        info=info,
        run_id=run_id,
        heading_kind="failure",
    )


def format_delivery_failure_for_feishu(
    exc: BaseException,
    *,
    run_id: UUID | str | None = None,
) -> str:
    info = HermesErrorInfo(
        FEISHU_DELIVERY_FAILED,
        STAGE_DELIVERY,
        f"回答已生成，但发送到飞书失败：{type(exc).__name__}: {exc}",
    )
    return _compose_feishu_error_text(
        info=info,
        run_id=run_id,
        heading_kind="failure",
    )


def _compose_feishu_error_text(
    *,
    info: HermesErrorInfo,
    run_id: UUID | str | None,
    heading_kind: str,
) -> str:
    if heading_kind == "degraded":
        heading = "【降级标注】非 Hermes 完整结论（软降级）"
    elif info.code == HERMES_MODEL_FAILED:
        heading = "【错误】模型调用失败（非 Hermes 完整结论）"
    else:
        heading = "【错误】ProjectLens / Hermes 处理失败"

    hint = _HINTS.get(info.code, "定位：查服务日志中同 run_id 的栈与 tool 轨迹。")
    lines = [
        heading,
        f"错误码：{info.code}",
        f"阶段：{info.stage}",
    ]
    if run_id is not None:
        lines.append(f"run_id：{run_id}")
    detail = (info.message or "").strip()
    if detail:
        lines.append(f"原因：{detail[:400]}")
    lines.append(hint)
    lines.append("你可以稍后重试，或者把问题缩小到某个服务、文件或接口。")
    return "\n".join(lines)
