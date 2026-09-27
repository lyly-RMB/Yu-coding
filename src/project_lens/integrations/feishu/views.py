"""Feishu AnswerView: user-facing presentation layer (not workflow Skill).

Feishu adapters must not query EvidenceIndex / graph / ops stores.
Selection consumes only AgentRun + ProjectAnswer.
"""

from __future__ import annotations

import re
from enum import StrEnum

from project_lens.context.knowledge_gaps import is_knowledge_gap_question
from project_lens.domain.models import AgentRun, ClaimType, ProjectAnswer
from project_lens.workflow.skills import (
    ProjectSkill,
    is_owner_lookup_question,
    is_project_intro_question,
    skill_routing_enabled,
)


class AnswerView(StrEnum):
    PROJECT_OVERVIEW = "project_overview"
    PROJECT_MAP = "project_map"
    CHANGE_IMPACT = "change_impact"
    INCIDENT_COLLABORATION = "incident_collaboration"
    OWNER_LOOKUP = "owner_lookup"
    KNOWLEDGE_GAP = "knowledge_gap"
    CODE_EXPLANATION = "code_explanation"
    REASON_ANALYSIS = "reason_analysis"
    ARCHITECTURE_EXPLAIN = "architecture_explain"
    PROJECT_INVESTIGATION = "project_investigation"
    GENERAL_REPLY = "general_reply"
    DEBUG_AUDIT = "debug_audit"


_VIEW_TITLES: dict[AnswerView, str] = {
    AnswerView.PROJECT_OVERVIEW: "ProjectLens 项目概览",
    AnswerView.PROJECT_MAP: "ProjectLens 项目地图",
    AnswerView.CHANGE_IMPACT: "ProjectLens 变更影响报告",
    AnswerView.INCIDENT_COLLABORATION: "ProjectLens 故障协作报告",
    AnswerView.OWNER_LOOKUP: "ProjectLens 负责人视图",
    AnswerView.KNOWLEDGE_GAP: "ProjectLens 知识缺口报告",
    AnswerView.CODE_EXPLANATION: "ProjectLens 代码解释",
    AnswerView.REASON_ANALYSIS: "ProjectLens 原因分析",
    AnswerView.ARCHITECTURE_EXPLAIN: "ProjectLens 架构说明",
    # Fallback title for investigation when no finer view matches.
    AnswerView.PROJECT_INVESTIGATION: "ProjectLens 项目回答",
    AnswerView.GENERAL_REPLY: "ProjectLens 项目回答",
    AnswerView.DEBUG_AUDIT: "ProjectLens 调试信息",
}

_PATH_HINT = re.compile(
    r"[\w./\\-]+\.(?:py|ts|tsx|js|jsx|go|java|rs|md|yml|yaml|json)\b",
    re.IGNORECASE,
)
_CODE_MARKERS = (
    "create_order",
    "coupon",
    "函数",
    "方法",
    "代码",
    "实现",
    "检查",
    "调用",
    "入口文件",
    "哪个文件",
    "order_service",
)
_REASON_MARKERS = ("为什么", "原因", "为何", "怎么会", "为啥", "为何要")
_ARCH_MARKERS = (
    "架构",
    "readme",
    "项目地图",
    "依赖",
    "对应",
    "服务入口",
    "模块",
    "architecture",
)
_CHANGE_MARKERS = ("谁改", "最近谁", "谁提交", "变更", "commit", "版本")


def answer_view_title(view: AnswerView) -> str:
    return _VIEW_TITLES.get(view, "ProjectLens 项目回答")


DEGRADED_ANSWER_TITLE = "ProjectLens 降级回复（非 Hermes 完整结论）"

_DEGRADED_SUMMARY_MARKERS = (
    "当前资料不足以形成带引用结论",
    "调查完成，暂无结论",
    "Hermes 未返回最终答案",
    "Hermes 返回了未结构化文本",
)
_DEGRADED_UNKNOWN_MARKERS = (
    "调查结束，但没有形成可展示的结论",
    "Hermes 未返回最终答案",
    "尚未完成 Evidence 引用校验",
)


def has_cited_fact(answer: ProjectAnswer) -> bool:
    return any(
        claim.type == ClaimType.FACT and claim.evidence_ids for claim in answer.claims
    )


def is_degraded_answer(answer: ProjectAnswer) -> bool:
    """True when the posted body is a system fallback, not a Hermes verified conclusion.

    Covers verifier stock lines, empty-draft fallbacks, and the insufficient-evidence
    card path — even when they still render under a normal answer title.

    Partial unknowns (owner / README / changelog gaps) must not degrade an answer
    that already has citation-checked FACT claims.
    """

    if has_cited_fact(answer):
        return False
    if is_evidence_insufficient(answer):
        return True
    summary = f"{answer.business_summary} {answer.conclusion}"
    if any(marker in summary for marker in _DEGRADED_SUMMARY_MARKERS):
        return True
    unknowns_blob = " ".join(answer.unknowns)
    return any(marker in unknowns_blob for marker in _DEGRADED_UNKNOWN_MARKERS)


def degraded_answer_banner(
    answer: ProjectAnswer | None = None,
    *,
    run_id: object | None = None,
) -> str:
    if answer is None:
        return (
            "**【降级标注】本条不是 Hermes 完整项目回答**\n"
            "原因：没有通过引用校验的可展示结论，或当前资料不足。"
            "正文为系统兜底文案，请勿当作已核实的项目结论。"
        )
    from project_lens.integrations.feishu.hermes_errors import (
        format_degraded_banner_for_feishu,
    )

    return (
        f"**{format_degraded_banner_for_feishu(answer, run_id=run_id)}**\n"
        "正文为系统兜底文案，请勿当作已核实的项目结论。"
    )


def select_answer_view(run: AgentRun, answer: ProjectAnswer) -> AnswerView:
    """Choose user-facing view from question + answer. Skill stays internal."""

    question = run.question or ""
    if not skill_routing_enabled():
        if answer.skill == "project_investigation":
            return select_investigation_answer_view(run, answer)
        return AnswerView.GENERAL_REPLY

    if is_project_intro_question(question):
        return AnswerView.PROJECT_OVERVIEW
    if is_knowledge_gap_question(question):
        return AnswerView.KNOWLEDGE_GAP
    if is_owner_lookup_question(question):
        return AnswerView.OWNER_LOOKUP

    if answer.skill == "project_investigation":
        return select_investigation_answer_view(run, answer)

    try:
        skill = ProjectSkill(answer.skill)
    except ValueError:
        skill = ProjectSkill.PROJECT_KNOWLEDGE

    if skill == ProjectSkill.ARCHITECTURE:
        return AnswerView.PROJECT_MAP
    if skill == ProjectSkill.VERSION_CHANGE:
        return AnswerView.CHANGE_IMPACT
    if skill == ProjectSkill.INCIDENT_DIAGNOSIS:
        return AnswerView.INCIDENT_COLLABORATION
    if skill == ProjectSkill.CODE_EXPLANATION:
        return AnswerView.CODE_EXPLANATION
    return AnswerView.GENERAL_REPLY


def select_investigation_answer_view(
    run: AgentRun,
    answer: ProjectAnswer,
) -> AnswerView:
    """Map free-question investigation answers to human-facing card types.

    Never default the user title to a generic “项目调查” debug label.
    """

    question = run.question or ""
    normalized = question.casefold()
    summary_blob = f"{answer.business_summary} {answer.technical_summary}".casefold()

    if any(marker in question for marker in _CHANGE_MARKERS) or any(
        marker in normalized for marker in ("who changed", "recent commit")
    ):
        return AnswerView.CHANGE_IMPACT

    if any(marker.casefold() in normalized for marker in _ARCH_MARKERS) or (
        "架构" in summary_blob and "入口" in summary_blob
    ):
        return AnswerView.ARCHITECTURE_EXPLAIN

    if _PATH_HINT.search(question) or any(
        marker.casefold() in normalized for marker in _CODE_MARKERS
    ):
        if any(marker in question for marker in _REASON_MARKERS):
            return AnswerView.REASON_ANALYSIS
        return AnswerView.CODE_EXPLANATION

    if any(marker in question for marker in _REASON_MARKERS):
        return AnswerView.REASON_ANALYSIS

    if skill_routing_enabled() and is_project_intro_question(question):
        return AnswerView.PROJECT_OVERVIEW

    return AnswerView.GENERAL_REPLY


def is_investigation_style(view: AnswerView) -> bool:
    """Views commonly produced by read_agent free questions."""

    return view in {
        AnswerView.GENERAL_REPLY,
        AnswerView.CODE_EXPLANATION,
        AnswerView.REASON_ANALYSIS,
        AnswerView.ARCHITECTURE_EXPLAIN,
        AnswerView.PROJECT_INVESTIGATION,
        AnswerView.CHANGE_IMPACT,
    }


def is_evidence_insufficient(answer: ProjectAnswer) -> bool:
    """True when the card should lead with the insufficient-evidence fallback."""

    if answer.evidence:
        # Investigation may have tool evidence but still no citable facts.
        if has_cited_fact(answer):
            return False
        if answer.confidence > 0.2 and (answer.business_summary or "").strip():
            # Weak but present summary — still show normal layout unless unknowns
            # explicitly say the search failed.
            if not _unknowns_say_search_failed(answer):
                return False
        if _unknowns_say_search_failed(answer):
            return True
        return False
    if has_cited_fact(answer):
        return False
    # Gap reports and follow-up/role views still have useful summaries or unknowns.
    if answer.unknowns and answer.confidence >= 0.2:
        return False
    if answer.business_summary.strip() or answer.technical_summary.strip():
        if _unknowns_say_search_failed(answer):
            return True
        return False
    return True


def _unknowns_say_search_failed(answer: ProjectAnswer) -> bool:
    blob = " ".join(answer.unknowns)
    markers = ("未获得可引用", "缺少可引用", "查了", "已尝试工具", "缺什么", "没有找到")
    return any(marker in blob for marker in markers)


def confidence_status_label(confidence: float, *, has_evidence: bool) -> str:
    """Map internal confidence to a human-readable status (not a raw percent)."""

    if not has_evidence or confidence <= 0:
        return "当前资料不足"
    if confidence < 0.4:
        return "证据较少，仅供参考"
    if confidence <= 0.7:
        return "有部分证据"
    return "证据较充分"


def human_evidence_label(evidence) -> str:
    """Readable reference line; never dumps secrets, UUIDs, or full bodies."""

    meta = evidence.metadata or {}
    for key in ("title", "path", "file_path", "doc_title", "name"):
        value = meta.get(key)
        if value:
            text = str(value).replace("\\", "/")
            # Strip UUID-looking tails from tool-generated source ids.
            if re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                text,
                flags=re.IGNORECASE,
            ):
                continue
            return text[:120]
    source_id = str(evidence.source.source_id)
    if re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        source_id,
        flags=re.IGNORECASE,
    ):
        type_label = evidence.type.value if hasattr(evidence.type, "value") else str(evidence.type)
        return f"{type_label} 资料"
    # Prefer short filenames over opaque tokens.
    if "/" in source_id or "\\" in source_id:
        return source_id.replace("\\", "/").rsplit("/", 1)[-1][:120]
    if source_id.startswith(("list:", "grep:", "search:")):
        return source_id.split(":", 1)[0] + " 结果"
    if source_id.startswith(("docx_", "doc_")):
        title = meta.get("title")
        return str(title)[:120] if title else f"飞书文档（{source_id[:16]}…）"
    type_label = evidence.type.value if hasattr(evidence.type, "value") else str(evidence.type)
    return f"{type_label}：{source_id[:80]}"
