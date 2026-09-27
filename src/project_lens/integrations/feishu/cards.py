"""Render ProjectLens answers into Feishu card payloads."""

from __future__ import annotations

import json
import re
from datetime import timezone
from uuid import UUID

from project_lens.application.audience_views import render_audience_view
from project_lens.config import settings
from project_lens.domain.feishu_doc_sync import FeishuDocSyncStatus, FeishuDocSyncStatusValue
from project_lens.domain.memory import MemoryProposal, ProjectMemory, memory_type_label
from project_lens.domain.risk import RiskFinding, RiskSeverity
from project_lens.domain.models import (
    ActionProposal,
    AgentRun,
    ClaimType,
    ProjectAnswer,
    ProjectRef,
)
from project_lens.integrations.feishu.adapter import FeishuCard
from project_lens.integrations.feishu.audiences import (
    AnswerAudience,
    audience_label,
    select_answer_audience,
)
from project_lens.integrations.feishu.audit_summary import (
    FeishuAuditSummary,
    build_feishu_audit_summary,
)
from project_lens.integrations.feishu.role_views import render_role_view_elements
from project_lens.integrations.feishu.role_views import render_audience_view_elements
from project_lens.integrations.feishu.views import (
    AnswerView,
    DEGRADED_ANSWER_TITLE,
    answer_view_title,
    confidence_status_label,
    degraded_answer_banner,
    human_evidence_label,
    is_degraded_answer,
    is_evidence_insufficient,
    select_answer_view,
)
from project_lens.workflow.engineering_bridge import engineering_action_from_answer
from project_lens.workflow.skills import (
    ProjectSkill,
    is_project_intro_question,
    skill_routing_enabled,
)
from project_lens.project_space.policies import effective_scope_from_audit_dict


def render_progress_text(stage: str, run: AgentRun) -> str:
    del run  # keep signature for call sites; do not surface run_id on chat progress
    return f"ProjectLens 已收到问题，正在{stage}…"


def render_risk_card(
    finding: RiskFinding,
    *,
    group_safe: bool = False,
) -> dict[str, object]:
    """Render citation-only risk details; never include Evidence bodies."""

    severity_label = {
        RiskSeverity.HIGH: "高",
        RiskSeverity.MEDIUM: "中",
        RiskSeverity.LOW: "低",
    }[finding.severity]
    refs = finding.affected_refs[:4] or (finding.primary_ref,)
    evidence = finding.evidence_ids[:4]
    elements: list[dict[str, object]] = [
        _markdown(
            f"**风险等级**\n{severity_label}\n"
            f"**当前状态**\n{finding.state.value}\n"
            f"**影响对象**\n" + "、".join(refs)
        ),
        _markdown(f"**判断说明**\n{finding.summary}"),
        _markdown(
            "**关键证据引用**\n"
            + "\n".join(f"- Evidence `{item}`" for item in evidence)
        ),
        _markdown(
            f"**检测时间**\n{finding.detected_at.isoformat()}\n"
            f"**数据新鲜度**\n最近确认：{finding.last_seen_at.isoformat()}"
        ),
    ]
    if group_safe:
        elements.append(
            _markdown("详细证据仅在具备权限的私聊中查看；群内不展示来源正文。")
        )
    else:
        elements.extend(_risk_action_rows(finding.risk_id))
    return FeishuCard(
        title=f"ProjectLens 风险提醒 · {finding.title}",
        elements=elements,
    ).to_payload()


def render_risk_feedback_result_card(
    finding: RiskFinding,
    *,
    action: str,
    duplicate: bool,
) -> dict[str, object]:
    status_text = "该操作已处理，本次未重复创建反馈。" if duplicate else "反馈已记录。"
    return FeishuCard(
        title="ProjectLens 风险反馈",
        elements=[
            _markdown(
                f"**风险**\n{finding.title}\n"
                f"**操作**\n{action}\n"
                f"**当前状态**\n{finding.state.value}\n{status_text}"
            )
        ],
    ).to_payload()


def render_answer_card(
    run: AgentRun,
    answer: ProjectAnswer,
    *,
    memory_proposal: MemoryProposal | None = None,
    audit_summary: FeishuAuditSummary | None = None,
    show_debug_audit: bool | None = None,
    audience: AnswerAudience | None = None,
) -> dict[str, object]:
    """User-centered Feishu card. Skill stays in debug zone, not the first screen.

    AnswerView = question type (card title). AnswerAudience = who is reading (RoleView).
    Renderer consumes only AgentRun + ProjectAnswer — no new facts.
    """

    view = select_answer_view(run, answer)
    selected_audience = select_answer_audience(run.question, explicit=audience)
    audience_view = None
    if run.runtime_access is not None and run.channel_id:
        scope = effective_scope_from_audit_dict(
            run.runtime_access,
            project=run.project,
            actor_id=run.user_id,
            chat_id=run.channel_id,
        )
        audience_view = render_audience_view(
            answer,
            role=scope.role,
            chat_type=scope.chat_type,
            scope=scope,
            audience=audience.value if audience is not None else None,
        )
        selected_audience = AnswerAudience(audience_view.audience)
    summary = audit_summary or build_feishu_audit_summary(run, answer)
    debug_on = (
        settings.feishu_show_debug_audit if show_debug_audit is None else show_debug_audit
    )

    # Evidence/debug RoleViews always render from the same answer; otherwise
    # insufficient-evidence stays the actionable TEAM-first fallback.
    if audience_view is not None:
        elements = render_audience_view_elements(run, audience_view)
    elif selected_audience in {AnswerAudience.EVIDENCE, AnswerAudience.DEBUG}:
        elements = render_role_view_elements(
            run, answer, view=view, audience=selected_audience
        )
    elif is_evidence_insufficient(answer):
        elements = _render_insufficient_evidence_elements(run, answer, view)
    else:
        elements = render_role_view_elements(
            run, answer, view=view, audience=selected_audience
        )

    engineering_action = engineering_action_from_answer(answer)
    if engineering_action is not None and selected_audience in {
        AnswerAudience.TECHNICAL,
        AnswerAudience.OPS,
        AnswerAudience.EVIDENCE,
    }:
        evidence_refs = tuple(
            human_evidence_label(item) for item in answer.evidence[:5]
        )
        elements.append(
            _markdown(
                _engineering_proposal_text(
                    engineering_action,
                    evidence_refs=evidence_refs,
                )
            )
        )
    if memory_proposal is not None and selected_audience != AnswerAudience.DEBUG:
        elements.append(_markdown(_memory_proposal_text(memory_proposal)))
        elements.append(_memory_action_buttons(memory_proposal.id))

    if selected_audience not in {AnswerAudience.EVIDENCE, AnswerAudience.DEBUG}:
        follow_ups = (
            _follow_up_questions(answer.skill, question=run.question)
            if skill_routing_enabled()
            else ()
        )
        if follow_ups:
            elements.append(
                _markdown(
                    "**可继续追问**\n"
                    + "\n".join(f"- {item}" for item in follow_ups)
                    + "\n- 给技术看的版本 / 给产品/业务看的版本 / 给测试看的版本 / 查看证据 / 知识库缺什么"
                )
            )
        elements.append(_run_detail_actions(run.id))
        elements.append(_role_view_action_buttons(selected_audience))

    # SkillGuide / provider / tool trail / trace stay in the bottom debug zone only.
    if debug_on and selected_audience != AnswerAudience.DEBUG:
        elements.append(_markdown(summary.to_debug_markdown()))

    degraded = is_degraded_answer(answer)
    if degraded:
        elements = [
            _markdown(degraded_answer_banner(answer, run_id=run.id)),
            *elements,
        ]

    title = answer_view_title(view)
    if degraded:
        title = DEGRADED_ANSWER_TITLE
    elif audience is not None and selected_audience != AnswerAudience.TEAM:
        title = f"{title} · {audience_label(selected_audience)}"

    return FeishuCard(
        title=title,
        elements=elements,
    ).to_payload()


def _render_view_elements(
    run: AgentRun,
    answer: ProjectAnswer,
    view: AnswerView,
) -> list[dict[str, object]]:
    elements: list[dict[str, object]] = [
        _markdown(f"**你问的是**\n{_short_question(run.question)}"),
        _markdown(
            f"**回答状态**\n{confidence_status_label(answer.confidence, has_evidence=bool(answer.evidence))}"
        ),
        _markdown(f"**一句话结论**\n{answer.business_summary or '（暂无业务结论）'}"),
    ]

    if view == AnswerView.PROJECT_OVERVIEW:
        elements.append(_markdown(_project_intro_focus_text(answer)))
    elif view == AnswerView.PROJECT_MAP:
        elements.append(_markdown(_project_map_focus_text(answer)))
    elif view == AnswerView.KNOWLEDGE_GAP:
        elements.append(_markdown(_knowledge_gap_focus_text(answer)))
    elif view == AnswerView.OWNER_LOOKUP:
        elements.append(_markdown(_owner_lookup_focus_text(answer)))
    elif view == AnswerView.CHANGE_IMPACT:
        elements.append(_markdown(_change_impact_focus_text(answer)))
    elif view == AnswerView.INCIDENT_COLLABORATION:
        elements.append(_markdown(_incident_collaboration_focus_text(answer)))
    elif view == AnswerView.CODE_EXPLANATION:
        elements.append(_markdown(_code_explanation_focus_text(answer)))
    elif view == AnswerView.REASON_ANALYSIS:
        elements.append(_markdown(_reason_analysis_focus_text(answer)))
    elif view == AnswerView.ARCHITECTURE_EXPLAIN:
        elements.append(_markdown(_architecture_explain_focus_text(answer)))
    elif view == AnswerView.PROJECT_INVESTIGATION:
        elements.append(_markdown(_general_key_findings(answer)))
    else:
        elements.append(_markdown(_general_key_findings(answer)))

    # Shared key findings when view-specific section did not already list claims.
    if view in {
        AnswerView.GENERAL_REPLY,
        AnswerView.PROJECT_INVESTIGATION,
        AnswerView.CODE_EXPLANATION,
        AnswerView.REASON_ANALYSIS,
        AnswerView.ARCHITECTURE_EXPLAIN,
        AnswerView.CHANGE_IMPACT,
        AnswerView.INCIDENT_COLLABORATION,
    }:
        findings = _key_findings_block(answer, view=view)
        if findings:
            elements.append(_markdown(findings))

    if answer.unknowns and view not in {
        AnswerView.KNOWLEDGE_GAP,
        AnswerView.PROJECT_OVERVIEW,
        AnswerView.PROJECT_MAP,
    }:
        elements.append(
            _markdown(
                "**当前不确定**\n" + "\n".join(f"- {item}" for item in answer.unknowns[:6])
            )
        )

    actions = _recommended_action_lines(answer)
    if actions:
        elements.append(_markdown("**建议下一步**\n" + "\n".join(actions)))

    sources = _reference_sources(answer, limit=5)
    if sources:
        elements.append(_markdown("**参考来源**\n" + "\n".join(sources)))

    # Optional technical details collapsed toward the end (not first screen).
    # Investigation-style cards keep technical_summary in the debug zone only.
    if answer.technical_summary and view in {
        AnswerView.INCIDENT_COLLABORATION,
        AnswerView.CODE_EXPLANATION,
        AnswerView.CHANGE_IMPACT,
        AnswerView.GENERAL_REPLY,
    } and answer.skill != "project_investigation":
        elements.append(_markdown(f"**技术细节**\n{answer.technical_summary}"))

    return elements


def _render_insufficient_evidence_elements(
    run: AgentRun,
    answer: ProjectAnswer,
    view: AnswerView,
) -> list[dict[str, object]]:
    searched = _searched_lines(answer)
    known_lines = [
        f"- 当前项目范围：{_project_scope_text(answer)}",
    ]
    if searched:
        known_lines.extend(searched)
    elif answer.evidence:
        known_lines.append("- 已找到的少量来源：")
        known_lines.extend(f"  - {human_evidence_label(item)}" for item in answer.evidence[:4])
    elif answer.business_summary:
        known_lines.append(f"- 系统已尝试回答：{answer.business_summary[:160]}")
    else:
        known_lines.append("- 当前 ACL 范围内几乎没有可引用资料")

    missing = list(answer.unknowns[:6]) if answer.unknowns else [
        "README 或项目简介",
        "架构文档",
        "服务职责说明",
        "负责人信息",
        "最近变更或任务记录",
    ]
    not_found = _not_found_lines(answer) or [
        "- 没有足够可引用的 Evidence 支撑完整结论",
    ]
    suggestions = _recommended_action_lines(answer) or [
        "1. 同步飞书文档",
        "2. 补充 README / 架构文档",
        "3. 询问「知识库缺什么」",
    ]
    # Ensure numbered suggestions when using defaults already numbered.
    if suggestions and not suggestions[0].lstrip().startswith(("1.", "2.", "-")):
        suggestions = [f"- {item}" for item in suggestions]

    view_hint = {
        AnswerView.PROJECT_OVERVIEW: "项目概览",
        AnswerView.PROJECT_MAP: "项目地图",
        AnswerView.KNOWLEDGE_GAP: "知识缺口",
        AnswerView.CHANGE_IMPACT: "变更影响",
        AnswerView.INCIDENT_COLLABORATION: "故障协作",
        AnswerView.OWNER_LOOKUP: "负责人",
        AnswerView.CODE_EXPLANATION: "代码解释",
        AnswerView.REASON_ANALYSIS: "原因分析",
        AnswerView.ARCHITECTURE_EXPLAIN: "架构说明",
        AnswerView.GENERAL_REPLY: "项目回答",
    }.get(view, "本次问题")

    return [
        _markdown(f"**你问的是**\n{_short_question(run.question)}"),
        _markdown(
            f"**当前资料不足，暂时不能完整回答（{view_hint}）**\n"
            "不是机器人坏了，而是当前可引用资料不够支撑完整结论。"
        ),
        _markdown("**我查了什么**\n" + "\n".join(known_lines)),
        _markdown("**没找到什么**\n" + "\n".join(not_found)),
        _markdown("**还缺什么**\n" + "\n".join(f"- {item}" for item in missing)),
        _markdown("**建议下一步**\n" + "\n".join(suggestions)),
    ]


def _searched_lines(answer: ProjectAnswer) -> list[str]:
    lines: list[str] = []
    tech = answer.technical_summary or ""
    if "tools=" in tech:
        tools = tech.split("tools=", 1)[1].split(";", 1)[0].strip()
        if tools and tools != "no tools used":
            lines.append(f"- 已调用只读工具：{tools}")
    if answer.evidence:
        lines.append("- 已读到的来源：")
        lines.extend(f"  - {human_evidence_label(item)}" for item in answer.evidence[:4])
    for item in answer.unknowns:
        if any(token in item for token in ("已尝试", "已查", "search_context", "工具")):
            lines.append(f"- {item}")
    return lines[:8]


def _not_found_lines(answer: ProjectAnswer) -> list[str]:
    lines: list[str] = []
    for item in answer.unknowns:
        if any(
            token in item
            for token in ("未获得", "缺少", "没有", "无法", "缺什么", "找不到")
        ):
            lines.append(f"- {item}")
    return lines[:6]


def _short_question(question: str, *, limit: int = 200) -> str:
    text = " ".join(question.strip().split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


# Card preview only: keep bot-reply teaser short so Feishu cards stay readable.
_PENDING_ANSWER_PREVIEW_LIMIT = 48
# Feishu interactive markdown treats mid-line ``##`` / fences as structure and
# may blank the whole pending-list element; strip those before the teaser.
_PENDING_MARKDOWN_HEADING = re.compile(r"#{1,6}\s+")
_PENDING_MARKDOWN_FENCE = re.compile(r"`{3,}")


def _pending_answer_preview(
    answer: str,
    *,
    limit: int = _PENDING_ANSWER_PREVIEW_LIMIT,
) -> str:
    """Short plain teaser for pending-send cards (safe for Feishu markdown)."""

    text = _PENDING_MARKDOWN_FENCE.sub("", answer)
    text = _PENDING_MARKDOWN_HEADING.sub("", text)
    return _short_question(text, limit=limit)


def _key_conclusions_block(answer: ProjectAnswer, *, view: AnswerView) -> str | None:
    facts = [
        claim
        for claim in answer.claims
        if claim.type == ClaimType.FACT and not _is_project_map_nav_claim(claim.text)
    ]
    inferences = [claim for claim in answer.claims if claim.type == ClaimType.INFERENCE]
    if not facts and not inferences:
        return None
    heading = {
        AnswerView.REASON_ANALYSIS: "**关键结论（原因）**",
        AnswerView.CODE_EXPLANATION: "**关键结论（代码）**",
        AnswerView.ARCHITECTURE_EXPLAIN: "**关键结论（架构）**",
        AnswerView.CHANGE_IMPACT: "**关键结论（变更）**",
    }.get(view, "**关键结论**")
    lines = [heading]
    for claim in facts[:4]:
        lines.append(f"- {claim.text}")
    for claim in inferences[:2]:
        lines.append(f"- 推断：{claim.text}")
    return "\n".join(lines)


def _key_findings_block(answer: ProjectAnswer, *, view: AnswerView) -> str | None:
    facts = [
        claim
        for claim in answer.claims
        if claim.type == ClaimType.FACT and not _is_project_map_nav_claim(claim.text)
    ]
    inferences = [claim for claim in answer.claims if claim.type == ClaimType.INFERENCE]
    risks = [
        claim
        for claim in answer.claims
        if "风险" in claim.text or claim.text.startswith("风险信号：")
    ]
    if not facts and not inferences and not risks:
        return None
    lines = ["**关键发现**"]
    for claim in facts[:3]:
        lines.append(f"- 事实：{claim.text}")
    for claim in inferences[:2]:
        lines.append(f"- 推断：{claim.text}")
    for claim in risks[:2]:
        lines.append(f"- 风险/缺口：{claim.text}")
    if view == AnswerView.INCIDENT_COLLABORATION:
        lines.append("- 时间接近不等于因果，需继续用 trace / 复现 / commit 验证。")
    return "\n".join(lines)


def _general_key_findings(answer: ProjectAnswer) -> str:
    block = _key_findings_block(answer, view=AnswerView.GENERAL_REPLY)
    if block:
        return block
    return "**关键发现**\n- 当前没有可展示的已验证结论，见未知项与参考来源。"


def _change_impact_focus_text(answer: ProjectAnswer) -> str:
    lines = ["**变更影响报告**"]
    lines.append(f"- 变更摘要：{answer.business_summary or '见下方关键发现'}")
    change_claims = [
        claim.text
        for claim in answer.claims
        if any(
            marker in claim.text
            for marker in ("变更", "commit", "发布", "release", "版本", "影响")
        )
    ]
    if change_claims:
        lines.append("- 相关变更：")
        lines.extend(f"  - {item}" for item in change_claims[:4])
    else:
        lines.append("- 相关变更：当前证据不足，见未知项。")
    lines.append("- 建议先确认受影响服务、接口与回归场景，再讨论修复或回滚。")
    lines.append("- 只读分析：不执行 Apply / PR / deploy / rollback / restart。")
    return "\n".join(lines)


def _incident_collaboration_focus_text(answer: ProjectAnswer) -> str:
    lines = ["**故障协作报告**"]
    lines.append(f"- 当前判断：{answer.business_summary or '见关键发现'}")
    fact_claims = [c for c in answer.claims if c.type == ClaimType.FACT][:3]
    inference_claims = [c for c in answer.claims if c.type == ClaimType.INFERENCE][:3]
    if fact_claims:
        lines.append("- 已确认信号：")
        lines.extend(f"  - {c.text}" for c in fact_claims)
    if inference_claims:
        lines.append("- 可能相关（推断）：")
        lines.extend(f"  - {c.text}" for c in inference_claims)
    timeline = [c.text for c in answer.claims if "ProjectOps timeline correlation" in c.text]
    if timeline:
        lines.append("- ProjectOps 时间线相关性：")
        lines.extend(f"  - {item}" for item in timeline[:3])
        lines.append("  - 只读判断：时间接近不等于因果。")
    projectops = [c.text for c in answer.claims if "ProjectOps 相关性分析" in c.text]
    if projectops:
        lines.append("- ProjectOps 相关性分析：")
        lines.extend(f"  - {item}" for item in projectops[:3])
    ops = [c.text for c in answer.claims if "运维时间窗信号" in c.text]
    if ops:
        lines.append("- 运维信号（时间窗只读）：")
        lines.extend(f"  - {item}" for item in ops[:3])
        lines.append("  - 日志/指标/trace 不写入长期知识库。")
    lines.append("- 可展示 Explain/Propose/Validate 修复提案，但不会执行 Apply。")
    return "\n".join(lines)


def _code_explanation_focus_text(answer: ProjectAnswer) -> str:
    lines = ["**代码解释**"]
    lines.append(f"- 摘要：{answer.business_summary or answer.technical_summary or '见关键发现'}")
    code_claims = [
        c.text
        for c in answer.claims
        if c.type == ClaimType.FACT
    ][:4]
    if code_claims:
        lines.append("- 已核实要点：")
        lines.extend(f"  - {item}" for item in code_claims)
    lines.append("- 事实均引用 Evidence；不会直接改代码。")
    return "\n".join(lines)


def _reason_analysis_focus_text(answer: ProjectAnswer) -> str:
    lines = ["**原因分析**"]
    lines.append(f"- 一句话：{answer.business_summary or '见关键结论'}")
    facts = [c.text for c in answer.claims if c.type == ClaimType.FACT][:4]
    if facts:
        lines.append("- 已核实原因线索：")
        lines.extend(f"  - {item}" for item in facts)
    if answer.unknowns:
        lines.append("- 仍不确定的点见下方「当前不确定」。")
    return "\n".join(lines)


def _architecture_explain_focus_text(answer: ProjectAnswer) -> str:
    lines = ["**架构说明**"]
    lines.append(f"- 摘要：{answer.business_summary or '见关键结论'}")
    facts = [c.text for c in answer.claims if c.type == ClaimType.FACT][:4]
    if facts:
        lines.append("- 架构要点：")
        lines.extend(f"  - {item}" for item in facts)
    return "\n".join(lines)


def _recommended_action_lines(answer: ProjectAnswer) -> list[str]:
    lines: list[str] = []
    index = 1
    for item in answer.recommended_actions:
        if item.tool_name == "engineering_proposal":
            continue
        suffix = "（需要审批）" if item.requires_approval else ""
        lines.append(f"{index}. {item.title}{suffix}")
        index += 1
        if index > 5:
            break
    return lines


def _reference_sources(answer: ProjectAnswer, *, limit: int = 5) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    cap = max(3, min(int(limit), 5))
    for item in answer.evidence:
        label = human_evidence_label(item)
        # Never dump raw evidence UUIDs on the first screen.
        if label in seen:
            continue
        if len(label) >= 32 and all(ch in "0123456789abcdef-" for ch in label.casefold()):
            continue
        seen.add(label)
        lines.append(f"- {label}")
        if len(lines) >= cap:
            break
    return lines


def render_feishu_doc_sync_status_card(
    project: ProjectRef,
    statuses: tuple[FeishuDocSyncStatus, ...],
    *,
    read_error: str | None = None,
) -> dict[str, object]:
    scope = f"tenant={project.tenant_id}；project={project.project_id}"
    if project.service:
        scope += f"；service={project.service}"
    elements: list[dict[str, object]] = [
        _markdown(f"**项目范围**\n{scope}"),
        _markdown("**知识库同步视图**\n展示 Feishu 文档同步成功、跳过、失败与最新 revision。"),
    ]
    if read_error:
        elements.append(_markdown(f"**读取异常**\n{read_error}\n普通问答不受影响。"))
    elif not statuses:
        elements.append(
            _markdown(
                "**同步记录**\n当前还没有同步状态。"
                "可通过 `POST /api/v1/projects/feishu-docs/sync` 触发同步。"
            )
        )
    else:
        success = sum(1 for item in statuses if item.status == FeishuDocSyncStatusValue.SUCCESS)
        skipped = sum(1 for item in statuses if item.status == FeishuDocSyncStatusValue.SKIPPED)
        failed = sum(1 for item in statuses if item.status == FeishuDocSyncStatusValue.FAILED)
        elements.append(
            _markdown(
                "**汇总**\n"
                f"- success: {success}\n"
                f"- skipped: {skipped}\n"
                f"- failed: {failed}"
            )
        )
        lines = [_format_sync_status_line(item) for item in statuses[:10]]
        elements.append(_markdown("**文档明细**\n" + "\n".join(lines)))
        recent = [
            item
            for item in statuses
            if item.status in {FeishuDocSyncStatusValue.SUCCESS, FeishuDocSyncStatusValue.SKIPPED}
        ][:5]
        if recent:
            recent_lines = [
                f"- {item.title or item.doc_token} @ revision={item.revision or '(none)'} "
                f"scope={item.access_scope or '(unknown)'} "
                f"url={item.doc_url or '(none)'} "
                f"({item.last_synced_at.isoformat()})"
                for item in recent
            ]
            elements.append(_markdown("**知识库最近更新**\n" + "\n".join(recent_lines)))
    elements.append(_note("文档同步状态为运维只读视图，不影响普通飞书聊天。"))
    return FeishuCard(title="ProjectLens 文档同步状态", elements=elements).to_payload()


def _format_sync_status_line(item: FeishuDocSyncStatus) -> str:
    title = item.title or "(untitled)"
    revision = item.revision or "(none)"
    owner = item.owner_user_id or "(unknown)"
    scope = item.access_scope or "(unknown)"
    url = item.doc_url or "(none)"
    line = (
        f"- [{item.status.value}] {item.doc_token} | {title} | "
        f"owner={owner} | revision={revision} | "
        f"scope={scope} | url={url} | "
        f"synced_at={item.last_synced_at.isoformat()}"
    )
    if item.status == FeishuDocSyncStatusValue.FAILED:
        last_ok = item.last_success_revision or "(none)"
        line += f" | last_ok={last_ok}"
        if item.error:
            line += f" | error={item.error[:160]}"
    return line


def render_memory_decision_card(
    proposal: MemoryProposal,
    *,
    memory: ProjectMemory | None = None,
) -> dict[str, object]:
    if proposal.status == "approved" and memory is not None:
        evidence = "、".join(str(item) for item in memory.evidence_ids[:4]) or "无"
        body = (
            f"**已沉淀到项目记忆**\n{memory.text}\n"
            f"- 记忆类型：{memory_type_label(memory.memory_type)}\n"
            f"- approved_by: {memory.approved_by}\n"
            f"- 引用证据：{evidence}"
        )
        title = "项目记忆已确认"
    elif proposal.status == "rejected":
        body = (
            f"**已拒绝沉淀**\n{proposal.claim_text}\n"
            f"- 记忆类型：{memory_type_label(proposal.memory_type)}\n"
            "人工确认前不会写入 ProjectMemory。"
        )
        title = "项目记忆未沉淀"
    else:
        body = (
            f"**记忆提案仍待确认**\n{proposal.claim_text}\n"
            f"- 记忆类型：{memory_type_label(proposal.memory_type)}"
        )
        title = "项目记忆待确认"
    return FeishuCard(
        title=title,
        elements=[_markdown(body), _note(f"proposal_id: {proposal.id}")],
    ).to_payload()


def render_failure_card(run: AgentRun) -> dict[str, object]:
    from project_lens.integrations.feishu.hermes_errors import (
        HERMES_MODEL_FAILED,
        classify_hermes_failure,
        format_failure_for_feishu,
    )

    info = classify_hermes_failure(run.error or "")
    heading = (
        "模型调用失败"
        if info.code == HERMES_MODEL_FAILED
        else f"处理失败 · {info.code}"
    )
    body = format_failure_for_feishu(error=run.error, run_id=run.id)
    return FeishuCard(
        title=heading,
        elements=[
            _markdown(body),
            _run_detail_actions(run.id),
        ],
    ).to_payload()


def format_failure_as_text(run: AgentRun) -> str:
    """Plain-text failure reply (no interactive card)."""

    from project_lens.integrations.feishu.hermes_errors import format_failure_for_feishu

    return format_failure_for_feishu(error=run.error, run_id=run.id)


def format_answer_as_text(
    run: AgentRun,
    answer: ProjectAnswer,
    *,
    memory_proposal: MemoryProposal | None = None,
    audience: AnswerAudience | None = None,
) -> str:
    """Render the answer body as plain text for Feishu ``msg_type=text`` replies."""

    card = render_answer_card(
        run,
        answer,
        memory_proposal=None,  # buttons not available in text; note below
        show_debug_audit=False,
        audience=audience,
    )
    chunks = _markdown_chunks_from_card(card)
    title = ""
    header = card.get("header") if isinstance(card.get("header"), dict) else {}
    title_obj = header.get("title") if isinstance(header, dict) else None
    if isinstance(title_obj, dict):
        title = str(title_obj.get("content") or "").strip()
    parts: list[str] = []
    if title:
        parts.append(title)
    parts.extend(chunks)
    if memory_proposal is not None:
        parts.append(
            "（已生成记忆沉淀提案；可在后续管理入口确认，群内文本回复不含审批按钮。）"
        )
    return "\n\n".join(part for part in parts if part).strip() or "（无正文）"


def _markdown_chunks_from_card(card: dict[str, object]) -> list[str]:
    elements = card.get("elements")
    if not isinstance(elements, list):
        return []
    chunks: list[str] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        tag = str(element.get("tag") or "")
        if tag == "markdown":
            content = str(element.get("content") or "").strip()
            if content:
                chunks.append(content)
        elif tag == "div":
            text_obj = element.get("text")
            if isinstance(text_obj, dict):
                content = str(text_obj.get("content") or "").strip()
                if content:
                    chunks.append(content)
    return chunks


def card_payload_as_text(card: dict[str, object]) -> str:
    """Flatten a Feishu card payload into plain text (drops buttons)."""

    chunks = _markdown_chunks_from_card(card)
    header = card.get("header") if isinstance(card.get("header"), dict) else {}
    title = ""
    if isinstance(header, dict):
        title_obj = header.get("title")
        if isinstance(title_obj, dict):
            title = str(title_obj.get("content") or "").strip()
    parts = [title, *chunks] if title else list(chunks)
    return "\n\n".join(part for part in parts if part).strip() or "（无正文）"


def render_run_detail_card(detail: dict[str, object]) -> dict[str, object]:
    if not detail.get("ok"):
        body = (
            "**这次我没能打开运行详情**\n"
            f"- 原因：{detail.get('message') or detail.get('answer_summary') or 'unknown'}"
        )
    else:
        source_summary = [
            str(item) for item in (detail.get("source_summary") or [])  # type: ignore[arg-type]
        ]
        facts = detail.get("facts") or []
        unknowns = [
            str(item) for item in (detail.get("unknowns") or [])  # type: ignore[arg-type]
        ]
        lines = [
            "**运行详情**",
            f"- run_id: {detail.get('run_id')}",
            f"- trace_id: {detail.get('trace_id')}",
            f"- runtime: {detail.get('runtime')}",
            f"- entry_mode: {detail.get('entry_mode')}",
            f"- 验证状态: {detail.get('verification_state')}",
            f"- 已确认事实: {len(facts) if isinstance(facts, list) else 0}",
            f"- 引用数量: {detail.get('citation_count')}",
            "- allow_apply=false",
        ]
        if source_summary:
            lines.append("- 来源摘要：")
            lines.extend(f"  - {item}" for item in source_summary[:6])
        if unknowns:
            lines.append("- 未知项：")
            lines.extend(f"  - {item}" for item in unknowns[:4])
        failure_reason = str(detail.get("failure_reason") or "").strip()
        if failure_reason:
            lines.append(f"- 失败原因：{failure_reason[:300]}")
        body = "\n".join(lines)
    return FeishuCard(
        title="ProjectLens 运行详情",
        elements=[_markdown(body)],
    ).to_payload()


def render_collaboration_gate_card(
    *,
    user_text: str,
    project: ProjectRef | None = None,
) -> dict[str, object]:
    """Clarify non-project messages without forcing a project Skill AgentRun."""

    scope = ""
    if project is not None:
        scope = (
            f"\n当前绑定项目：tenant={project.tenant_id}；"
            f"project={project.project_id}"
        )
        if project.service:
            scope += f"；service={project.service}"

    elements: list[dict[str, object]] = [
        _markdown(f"**你说的是**\n{_short_question(user_text)}"),
        _markdown(
            "**这不太像当前项目问题**\n"
            "我主要帮团队回答项目相关问题，不会把闲聊强行套进项目分析。"
            f"{scope}"
        ),
        _markdown(
            "**你可以这样问**\n"
            "- 介绍一下这个项目\n"
            "- 项目地图\n"
            "- 最近变更 / 最近故障\n"
            "- 知识库缺什么\n"
            "- 负责人是谁\n"
            "- 或直接描述服务、接口、报错、文档"
        ),
        _collaboration_shortcut_buttons(),
    ]
    return FeishuCard(title="ProjectLens 协作入口", elements=elements).to_payload()


def render_context_preview_card(
    *,
    question: str,
    session_id: UUID,
    items: tuple[object, ...] | list[object],
    token_estimate: int,
    branches: tuple[object, ...] | list[object] | None = None,
    active_branch_name: str | None = None,
    date_window_label: str = "未开窗（整池默认带入，不做剔除非今日）",
) -> dict[str, object]:
    """Candidate preview: token budget + time-sorted pending list + three entries.

    Hard commitment: this list is the only conversation history Hermes will see.
    """

    lines: list[str] = []
    for index, raw in enumerate(items, start=1):
        mark = getattr(raw, "mark", None)
        mark_label = "默认"
        mark_value = getattr(mark, "value", mark)
        if str(mark_value) == "citation":
            mark_label = "引用"
        occurred = getattr(raw, "occurred_at", None)
        stamp = occurred.isoformat() if occurred is not None else "(no-time)"
        label = str(getattr(raw, "label", "") or "")
        item_id = str(getattr(raw, "item_id", "") or "")
        answer_raw = str(getattr(raw, "answer_summary", "") or "").strip()
        row = f"{index}. [{mark_label}] {stamp} · {label}"
        if answer_raw:
            preview = _pending_answer_preview(answer_raw)
            row += f"\n   回复：{preview}"
        row += f"\n   id=`{item_id}`"
        lines.append(row)
    body = (
        "\n".join(lines)
        if lines
        else "（待发集合为空：本次不会带入历史轮次/摘要）"
    )
    branch_rows = list(branches or ())
    if active_branch_name:
        branch_status = f"当前分支：`{active_branch_name}`"
    else:
        branch_status = "当前分支：默认线"
    if branch_rows:
        labels: list[str] = []
        for raw in branch_rows:
            name = str(getattr(raw, "branch_name", "") or "")
            stopped = bool(getattr(raw, "write_stopped", False))
            active = bool(getattr(raw, "is_active", False))
            marker = "●" if active else "○"
            frozen = "（已停写）" if stopped else ""
            labels.append(f"{marker} {name}{frozen}")
        branch_status += "\n" + " · ".join(labels)

    elements: list[dict[str, object]] = [
        _markdown(f"**本次问题**\n{_short_question(question)}"),
        _markdown(f"**Token 预算（估算）**\n约 `{token_estimate}` tokens"),
        _markdown(f"**分支**\n{branch_status}"),
        _markdown(f"**日期窗**\n{date_window_label}"),
        _markdown(f"**待发上下文（已按时间排序）**\n{body}"),
        _markdown(
            "超过 8 轮后，最旧的第 1～4 轮会压成摘要并按原时间排在列表最上；"
            "近几轮按时间接在后面。列表上的「回复」仅为短预览，实发会带完整回答摘要。"
            "预览即实发：下面「直接回答」只带列表里的条目与顺序。"
        ),
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "直接回答"},
                    "type": "primary",
                    "value": {
                        "action": "context_direct_answer",
                        "session_id": str(session_id),
                    },
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "编辑上下文"},
                    "type": "default",
                    "value": {
                        "action": "context_edit",
                        "session_id": str(session_id),
                    },
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "选择更多历史"},
                    "type": "default",
                    "value": {
                        "action": "context_more_history",
                        "session_id": str(session_id),
                        "offset": "0",
                    },
                },
            ],
        },
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "设置日期窗"},
                    "type": "default",
                    "value": {
                        "action": "context_open_date_window",
                        "session_id": str(session_id),
                    },
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "清空日期窗"},
                    "type": "default",
                    "value": {
                        "action": "context_clear_date_window",
                        "session_id": str(session_id),
                    },
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "新建分支"},
                    "type": "default",
                    "value": {
                        "action": "context_fork_branch",
                        "session_id": str(session_id),
                    },
                },
                *(
                    [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "删除分支"},
                            "type": "danger",
                            "value": {
                                "action": "context_delete_branch",
                                "session_id": str(session_id),
                                "target_session_id": str(session_id),
                            },
                        }
                    ]
                    if len(branch_rows) >= 2
                    else []
                ),
            ],
        },
    ]
    # Switch buttons for sibling branches (exclude current). Cap at 5.
    switch_actions: list[dict[str, object]] = []
    for raw in branch_rows:
        if bool(getattr(raw, "is_active", False)):
            continue
        target_id = str(getattr(raw, "session_id", "") or "")
        name = str(getattr(raw, "branch_name", "") or "")
        if not target_id or not name:
            continue
        switch_actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": f"切换·{name}"[:20]},
                "type": "default",
                "value": {
                    "action": "context_switch_branch",
                    "session_id": str(session_id),
                    "target_session_id": target_id,
                },
            }
        )
    if switch_actions:
        elements.append({"tag": "action", "actions": switch_actions[:5]})

    # Per-item exclude buttons (quick path on preview). Cap at 5 rows.
    exclude_actions: list[dict[str, object]] = []
    for raw in list(items)[:5]:
        item_id = str(getattr(raw, "item_id", "") or "")
        if not item_id:
            continue
        short = str(getattr(raw, "label", "") or item_id)[:18]
        exclude_actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": f"去掉·{short}"},
                "type": "default",
                "value": {
                    "action": "context_exclude_item",
                    "session_id": str(session_id),
                    "item_id": item_id,
                },
            }
        )
    if exclude_actions:
        elements.append({"tag": "action", "actions": exclude_actions[:5]})

    return FeishuCard(title="ProjectLens 上下文预览", elements=elements).to_payload()


def render_context_edit_card(
    *,
    question: str,
    session_id: UUID,
    items: tuple[object, ...] | list[object],
    token_estimate: int,
    joinable_citations: tuple[object, ...] | list[object] = (),
    restorable_defaults: tuple[object, ...] | list[object] = (),
    staged_exclude_ids: tuple[str, ...] | list[str] | set[str] = (),
    staged_restore_ids: tuple[str, ...] | list[str] | set[str] = (),
    staged_join_citation_ids: tuple[str, ...] | list[str] | set[str] = (),
) -> dict[str, object]:
    """Edit context: toggle selections (color change), apply on 「确定」."""

    staged_exclude = {str(item) for item in staged_exclude_ids}
    staged_restore = {str(item) for item in staged_restore_ids}
    staged_join = {str(item) for item in staged_join_citation_ids}

    pending_lines: list[str] = []
    for index, raw in enumerate(items, start=1):
        mark = getattr(raw, "mark", None)
        mark_value = getattr(mark, "value", mark)
        mark_label = "引用" if str(mark_value) == "citation" else "默认"
        occurred = getattr(raw, "occurred_at", None)
        stamp = occurred.isoformat() if occurred is not None else "(no-time)"
        label = str(getattr(raw, "label", "") or "")
        item_id = str(getattr(raw, "item_id", "") or "")
        flag = "将去掉" if item_id in staged_exclude else "保留"
        pending_lines.append(f"{index}. [{flag}] [{mark_label}] {stamp} · {label}")

    pending_body = (
        "\n".join(pending_lines) if pending_lines else "（当前待发为空）"
    )
    join_lines: list[str] = []
    for raw in joinable_citations:
        stamp_obj = getattr(raw, "occurred_at", None)
        stamp = stamp_obj.isoformat() if stamp_obj is not None else "(no-time)"
        title = str(
            getattr(raw, "short_title", None)
            or getattr(raw, "label", None)
            or ""
        )
        cid = str(getattr(raw, "citation_id", "") or "")
        flag = "将加入" if cid in staged_join else "可选"
        join_lines.append(
            f"- [{flag}] {stamp} · {title}" + (f" (`{cid}`)" if cid else "")
        )
    join_body = (
        "\n".join(join_lines)
        if join_lines
        else "（暂无可加入的引用；可用「选择更多历史」从会话侧挑选）"
    )
    restore_lines: list[str] = []
    for raw in restorable_defaults:
        stamp_obj = getattr(raw, "occurred_at", None)
        stamp = stamp_obj.isoformat() if stamp_obj is not None else "(no-time)"
        label = str(getattr(raw, "label", "") or "")
        item_id = str(getattr(raw, "item_id", "") or "")
        flag = "将恢复" if item_id in staged_restore else "已去掉"
        restore_lines.append(f"- [{flag}] {stamp} · {label}")
    restore_body = (
        "\n".join(restore_lines) if restore_lines else "（无已去掉的默认项）"
    )
    staged_count = len(staged_exclude) + len(staged_restore) + len(staged_join)

    elements: list[dict[str, object]] = [
        _markdown(f"**本次问题**\n{_short_question(question)}"),
        _markdown(f"**Token 预算（估算）**\n约 `{token_estimate}` tokens"),
        _markdown(
            f"**勾选变更（未点确定前不生效）**\n"
            f"已勾选 `{staged_count}` 项。"
            "红色=将去掉，蓝色=将加入/恢复；再点一次可取消勾选。"
        ),
        _markdown(f"**当前待发**\n{pending_body}"),
        _markdown(f"**已去掉的默认项（可恢复）**\n{restore_body}"),
        _markdown(f"**可加入的引用**\n{join_body}"),
        _markdown(
            "勾选完成后点「确定」写回预览；「返回预览」放弃本次勾选。"
            "需要从会话侧或群聊细选时，用「选择更多历史」。"
        ),
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "确定"},
                    "type": "primary",
                    "value": {
                        "action": "context_confirm_edit",
                        "session_id": str(session_id),
                    },
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "选择更多历史"},
                    "type": "default",
                    "value": {
                        "action": "context_more_history",
                        "session_id": str(session_id),
                        "offset": "0",
                    },
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "返回预览"},
                    "type": "default",
                    "value": {
                        "action": "context_back_preview",
                        "session_id": str(session_id),
                    },
                },
            ],
        },
    ]

    exclude_actions: list[dict[str, object]] = []
    for raw in list(items)[:5]:
        item_id = str(getattr(raw, "item_id", "") or "")
        if not item_id:
            continue
        short = str(getattr(raw, "label", "") or item_id)[:16]
        selected = item_id in staged_exclude
        exclude_actions.append(
            {
                "tag": "button",
                "text": {
                    "tag": "plain_text",
                    "content": (f"已选去掉·{short}" if selected else f"去掉·{short}"),
                },
                "type": "danger" if selected else "default",
                "value": {
                    "action": "context_exclude_item",
                    "session_id": str(session_id),
                    "item_id": item_id,
                    "return_to": "edit",
                },
            }
        )
    if exclude_actions:
        elements.append({"tag": "action", "actions": exclude_actions[:5]})

    restore_actions: list[dict[str, object]] = []
    for raw in list(restorable_defaults)[:5]:
        item_id = str(getattr(raw, "item_id", "") or "")
        if not item_id:
            continue
        short = str(getattr(raw, "label", "") or item_id)[:16]
        selected = item_id in staged_restore
        restore_actions.append(
            {
                "tag": "button",
                "text": {
                    "tag": "plain_text",
                    "content": (f"已选恢复·{short}" if selected else f"恢复·{short}"),
                },
                "type": "primary" if selected else "default",
                "value": {
                    "action": "context_restore_item",
                    "session_id": str(session_id),
                    "item_id": item_id,
                    "return_to": "edit",
                },
            }
        )
    if restore_actions:
        elements.append({"tag": "action", "actions": restore_actions[:5]})

    join_actions: list[dict[str, object]] = []
    for raw in list(joinable_citations)[:5]:
        citation_id = str(getattr(raw, "citation_id", "") or "")
        if not citation_id:
            continue
        short = str(getattr(raw, "short_title", "") or citation_id)[:16]
        selected = citation_id in staged_join
        join_actions.append(
            {
                "tag": "button",
                "text": {
                    "tag": "plain_text",
                    "content": (f"已选加入·{short}" if selected else f"加入·{short}"),
                },
                "type": "primary" if selected else "default",
                "value": {
                    "action": "context_include_citation",
                    "session_id": str(session_id),
                    "citation_id": citation_id,
                    "return_to": "edit",
                },
            }
        )
    if join_actions:
        elements.append({"tag": "action", "actions": join_actions[:5]})

    return FeishuCard(title="ProjectLens 编辑上下文", elements=elements).to_payload()


def encode_group_page_stack(stack: tuple[str, ...] | list[str]) -> str:
    """Serialize prior group page-request tokens for card button values."""

    return json.dumps([str(item) for item in stack], ensure_ascii=False)


def decode_group_page_stack(raw: object) -> tuple[str, ...]:
    """Parse group page-token stack from a card action value."""

    if raw is None:
        return ()
    text = str(raw).strip()
    if not text:
        return ()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item) for item in parsed)


def render_context_more_history_card(
    *,
    question: str,
    session_id: UUID,
    candidates: tuple[object, ...] | list[object],
    token_estimate: int,
    offset: int,
    total: int,
    page_size: int = 5,
    group_candidates: tuple[object, ...] | list[object] = (),
    group_available: bool = True,
    group_unavailable_reason: str | None = None,
    group_page_token: str | None = None,
    group_page_stack: tuple[str, ...] | list[str] = (),
    group_item_offset: int = 0,
    group_has_more: bool = False,
    group_next_page_token: str | None = None,
) -> dict[str, object]:
    """Paginated history picker: session-side citations + Feishu group messages."""

    start = max(0, int(offset)) + 1
    end = max(0, int(offset)) + len(list(candidates))
    lines: list[str] = []
    for index, raw in enumerate(candidates, start=start):
        stamp_obj = getattr(raw, "occurred_at", None)
        stamp = stamp_obj.isoformat() if stamp_obj is not None else "(no-time)"
        title = str(getattr(raw, "short_title", "") or "")
        kind = str(getattr(raw, "source_kind", "") or "")
        selected = bool(getattr(raw, "already_in_pending", False))
        flag = "已选" if selected else "可选"
        lines.append(f"{index}. [{flag}] [{kind}] {stamp} · {title}")
    body = "\n".join(lines) if lines else "（当前页无会话侧历史候选）"
    range_label = f"{start}-{end}" if candidates else "0"

    group_list = list(group_candidates)
    group_start = max(0, int(group_item_offset)) + 1
    group_end = max(0, int(group_item_offset)) + len(group_list)
    group_lines: list[str] = []
    for index, raw in enumerate(group_list, start=group_start):
        stamp_obj = getattr(raw, "occurred_at", None)
        stamp = stamp_obj.isoformat() if stamp_obj is not None else "(no-time)"
        title = str(getattr(raw, "short_title", "") or "")
        selected = bool(getattr(raw, "already_in_pending", False))
        flag = "已选" if selected else "可选"
        group_lines.append(f"{index}. [{flag}] {stamp} · {title}")
    if not group_available:
        group_note = (
            "群聊来源不可用："
            + (group_unavailable_reason or "权限不足或机器人不在群；会话侧历史仍可用")
        )
        group_body = group_note
        group_range_label = "不可用"
    elif group_lines:
        group_body = "\n".join(group_lines)
        group_range_label = f"{group_start}-{group_end}"
    else:
        group_body = "（当前页无群聊候选；可设日期窗后重试）"
        group_range_label = "0"

    stack = tuple(str(item) for item in group_page_stack)
    stack_encoded = encode_group_page_stack(stack)
    current_group_token = str(group_page_token or "")
    group_nav_base = {
        "action": "context_more_history",
        "session_id": str(session_id),
        "offset": str(max(0, int(offset))),
    }

    elements: list[dict[str, object]] = [
        _markdown(f"**本次问题**\n{_short_question(question)}"),
        _markdown(f"**Token 预算（当前待发）**\n约 `{token_estimate}` tokens"),
        _markdown(
            f"**会话侧历史（{range_label} / 共 {total}）**\n{body}"
        ),
        _markdown(f"**群聊发言（{group_range_label}）**\n{group_body}"),
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "确定"},
                    "type": "primary",
                    "value": {
                        "action": "context_back_preview",
                        "session_id": str(session_id),
                    },
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "返回预览"},
                    "type": "default",
                    "value": {
                        "action": "context_back_preview",
                        "session_id": str(session_id),
                    },
                },
            ],
        },
    ]

    select_actions: list[dict[str, object]] = []
    for raw in candidates:
        if bool(getattr(raw, "already_in_pending", False)):
            continue
        citation_id = str(getattr(raw, "citation_id", "") or "")
        if not citation_id:
            continue
        short = str(getattr(raw, "short_title", "") or citation_id)[:18]
        select_actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": f"选用·{short}"},
                "type": "default",
                "value": {
                    "action": "context_select_history",
                    "session_id": str(session_id),
                    "citation_id": citation_id,
                    "offset": str(max(0, int(offset))),
                    "group_page_token": current_group_token,
                    "group_page_stack": stack_encoded,
                    "group_item_offset": str(max(0, int(group_item_offset))),
                },
            }
        )
    if select_actions:
        elements.append({"tag": "action", "actions": select_actions[:5]})

    group_actions: list[dict[str, object]] = []
    if group_available:
        for raw in group_list:
            if bool(getattr(raw, "already_in_pending", False)):
                continue
            message_id = str(getattr(raw, "message_id", "") or "")
            if not message_id:
                continue
            short = str(getattr(raw, "short_title", "") or message_id)[:18]
            stamp_obj = getattr(raw, "occurred_at", None)
            stamp = (
                stamp_obj.astimezone(timezone.utc).isoformat()
                if stamp_obj is not None
                else ""
            )
            body_text = str(getattr(raw, "body_text", "") or "")[:500]
            group_actions.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": f"群聊·{short}"},
                    "type": "default",
                    "value": {
                        "action": "context_select_group_message",
                        "session_id": str(session_id),
                        "message_id": message_id,
                        "occurred_at": stamp,
                        "short_title": str(getattr(raw, "short_title", "") or "")[:120],
                        "body_text": body_text,
                        "offset": str(max(0, int(offset))),
                        "group_page_token": current_group_token,
                        "group_page_stack": stack_encoded,
                        "group_item_offset": str(max(0, int(group_item_offset))),
                    },
                }
            )
    if group_actions:
        elements.append({"tag": "action", "actions": group_actions[:5]})

    nav: list[dict[str, object]] = []
    if offset > 0:
        prev_offset = max(0, int(offset) - int(page_size))
        nav.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "会话上一页"},
                "type": "default",
                "value": {
                    **group_nav_base,
                    "offset": str(prev_offset),
                    "group_page_token": current_group_token,
                    "group_page_stack": stack_encoded,
                    "group_item_offset": str(max(0, int(group_item_offset))),
                },
            }
        )
    if offset + page_size < total:
        next_offset = int(offset) + int(page_size)
        nav.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "会话下一页"},
                "type": "default",
                "value": {
                    **group_nav_base,
                    "offset": str(next_offset),
                    "group_page_token": current_group_token,
                    "group_page_stack": stack_encoded,
                    "group_item_offset": str(max(0, int(group_item_offset))),
                },
            }
        )
    if group_available and stack:
        prev_token = stack[-1]
        prev_stack = stack[:-1]
        prev_item_offset = max(0, int(group_item_offset) - int(page_size))
        nav.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "群聊上一页"},
                "type": "default",
                "value": {
                    **group_nav_base,
                    "group_page_token": prev_token,
                    "group_page_stack": encode_group_page_stack(prev_stack),
                    "group_item_offset": str(prev_item_offset),
                },
            }
        )
    if group_available and group_has_more and group_next_page_token:
        next_stack = stack + (current_group_token,)
        next_item_offset = max(0, int(group_item_offset)) + len(group_list)
        nav.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "群聊下一页"},
                "type": "default",
                "value": {
                    **group_nav_base,
                    "group_page_token": str(group_next_page_token),
                    "group_page_stack": encode_group_page_stack(next_stack),
                    "group_item_offset": str(next_item_offset),
                },
            }
        )
    if nav:
        elements.append({"tag": "action", "actions": nav[:5]})

    return FeishuCard(title="ProjectLens 选择更多历史", elements=elements).to_payload()


def render_context_date_window_card(
    *,
    question: str,
    session_id: UUID,
    date_window_label: str,
    token_estimate: int = 0,
) -> dict[str, object]:
    """Session-level date window coarse filter (start/end pickers + clear)."""

    elements: list[dict[str, object]] = [
        _markdown(f"**本次问题**\n{_short_question(question)}"),
        _markdown(f"**当前日期窗**\n{date_window_label}"),
        _markdown(
            "先选开始/结束日期（可只选一侧；选完不会跳转），再点「确定」进入「选择更多历史」细选。"
            "开窗后：窗外项不进入自动带入 / 预览待发 / 「更多历史」默认候选；"
            "也不作为 `projectlens_search_project_history` 的默认 from/to。"
            "窗外记录仍留在库中。清空日期窗即恢复整池默认。"
        ),
        _markdown(f"**Token 预算（当前待发）**\n约 `{token_estimate}` tokens"),
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "date_picker",
                    "placeholder": {"tag": "plain_text", "content": "开始日期"},
                    "value": {
                        "action": "context_set_date_start",
                        "session_id": str(session_id),
                    },
                },
                {
                    "tag": "date_picker",
                    "placeholder": {"tag": "plain_text", "content": "结束日期"},
                    "value": {
                        "action": "context_set_date_end",
                        "session_id": str(session_id),
                    },
                },
            ],
        },
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "确定"},
                    "type": "primary",
                    "value": {
                        "action": "context_confirm_date_window",
                        "session_id": str(session_id),
                    },
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "清空日期窗"},
                    "type": "default",
                    "value": {
                        "action": "context_clear_date_window",
                        "session_id": str(session_id),
                        "return_to": "date_window",
                    },
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "返回预览"},
                    "type": "default",
                    "value": {
                        "action": "context_back_preview",
                        "session_id": str(session_id),
                    },
                },
            ],
        },
    ]
    return FeishuCard(title="ProjectLens 日期窗粗筛", elements=elements).to_payload()


def render_about_bot_card(*, user_text: str) -> dict[str, object]:
    """Answer questions about ProjectLens itself — no project Skill / Evidence path."""

    from project_lens.config import settings
    from project_lens.integrations.feishu.intent import is_model_meta_question

    provider = settings.model_provider
    configured_name = settings.model_openai_model or settings.model_name
    live = bool(settings.model_live)
    if provider == "stub" or not live:
        model_line = (
            f"当前表达层配置：provider=`{provider}`，model=`{configured_name}`；"
            f"live={live}。多数本地/灰发默认走 stub，不会拿项目结论去“猜”。"
        )
    else:
        model_line = (
            f"当前表达层配置：provider=`{provider}`，model=`{configured_name}`；"
            f"live={live}。LLM 只润色表达，不决定事实。"
        )

    if is_model_meta_question(user_text):
        lead = (
            f"**一句话结论**\n{model_line}\n\n"
            "**说明**\n"
            "- 问「用什么模型」属于机器人元信息，不会去搜项目 Evidence\n"
            "- 项目事实仍必须引用文档 / commit / 图谱等证据\n"
            "- 不会执行 Apply / PR / deploy / rollback / restart"
        )
    else:
        lead = (
            "**一句话结论**\n"
            "我是 ProjectLens：帮团队用证据回答项目问题（概览、地图、变更、故障、缺口）。\n\n"
            f"**表达层模型**\n{model_line}\n\n"
            "**边界**\n"
            "- LLM 只做表达增强，不决定事实、不写记忆、不直接调知识库\n"
            "- 不会执行 Apply / PR / deploy / rollback / restart"
        )

    elements: list[dict[str, object]] = [
        _markdown(f"**你问的是**\n{_short_question(user_text)}"),
        _markdown(lead),
        _markdown(
            "**想问项目可以这样说**\n"
            "- 介绍一下这个项目\n"
            "- 项目地图\n"
            "- 最近变更 / 最近故障\n"
            "- 知识库缺什么"
        ),
        _collaboration_shortcut_buttons(),
    ]
    return FeishuCard(title="ProjectLens 关于我", elements=elements).to_payload()


def _ask_question_button(label: str, question: str, *, primary: bool = False) -> dict[str, object]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": "primary" if primary else "default",
        "value": {
            "action": "ask_question",
            "question": question,
        },
    }


def _risk_action_rows(risk_id: str) -> list[dict[str, object]]:
    return [
        {
            "tag": "action",
            "actions": [
                _risk_button("确认风险", "acknowledge", risk_id, primary=True),
                _risk_button("误报", "dismiss", risk_id),
                _risk_button("明天提醒", "snooze", risk_id),
            ],
        },
        {
            "tag": "action",
            "actions": [
                _risk_button("更新进展", "update_progress", risk_id),
                _risk_button("请求协助", "request_help", risk_id),
                _ask_question_button(
                    "为什么判断为风险？",
                    f"为什么风险 {risk_id} 被判断为风险？请只引用我有权限查看的证据。",
                ),
            ],
        },
    ]


def _risk_button(
    label: str,
    action: str,
    risk_id: str,
    *,
    primary: bool = False,
) -> dict[str, object]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": "primary" if primary else "default",
        "value": {
            "action": f"risk_{action}",
            "risk_id": risk_id,
        },
    }


def _role_view_action_buttons(audience: AnswerAudience) -> dict[str, object]:
    """RoleView switches — Feishu ask_question short-circuits to re-render same answer."""

    actions: list[dict[str, object]] = []
    if audience != AnswerAudience.TECHNICAL:
        actions.append(
            _ask_question_button("给技术看的版本", "给技术看的版本", primary=True)
        )
    if audience != AnswerAudience.BUSINESS:
        actions.append(
            _ask_question_button("给产品/业务看的版本", "给产品/业务看的版本")
        )
    if audience != AnswerAudience.QA:
        actions.append(_ask_question_button("给测试看的版本", "给测试看的版本"))
    if audience != AnswerAudience.EVIDENCE:
        actions.append(_ask_question_button("查看证据", "查看证据"))
    if audience != AnswerAudience.TEAM:
        actions.append(_ask_question_button("回到团队视图", "回到团队视图"))
    # Cap Feishu action row length.
    return {"tag": "action", "actions": actions[:4]}


def _run_detail_actions(run_id: UUID) -> dict[str, object]:
    return {
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "查看运行详情"},
                "type": "default",
                "value": {
                    "action": "projectlens_run_detail",
                    "run_id": str(run_id),
                },
            }
        ],
    }


def _collaboration_shortcut_buttons() -> dict[str, object]:
    return {
        "tag": "action",
        "actions": [
            _ask_question_button("介绍项目", "介绍一下这个项目", primary=True),
            _ask_question_button("项目地图", "项目地图"),
            _ask_question_button("最近变更", "最近变更"),
            _ask_question_button("知识库缺什么", "知识库缺什么"),
        ],
    }


def _markdown(content: str) -> dict[str, object]:
    return {"tag": "markdown", "content": content}


def _note(content: str) -> dict[str, object]:
    return {
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": content}],
    }


def _is_project_map_nav_claim(text: str) -> bool:
    """Claims already rendered in intro/map navigation views."""

    prefixes = (
        "项目地图：",
        "核心服务：",
        "关键入口：",
        "上下游依赖：",
        "最近变更：",
        "风险信号：",
        "项目定位：",
        "主要文档：",
    )
    return text.startswith(prefixes) or "负责人可从" in text


def _project_scope_text(answer: ProjectAnswer) -> str:
    project = answer.project
    parts = [f"tenant={project.tenant_id}", f"project={project.project_id}"]
    if project.service:
        parts.append(f"service={project.service}")
    if project.environment:
        parts.append(f"env={project.environment}")
    return "；".join(parts)


def _skill_focus_section(run: AgentRun, answer: ProjectAnswer) -> dict[str, object] | None:
    """Legacy helper kept for unit tests of focus text builders."""

    view = select_answer_view(run, answer)
    if view == AnswerView.PROJECT_OVERVIEW:
        return _markdown(_project_intro_focus_text(answer))
    if view == AnswerView.KNOWLEDGE_GAP:
        return _markdown(_knowledge_gap_focus_text(answer))
    if view == AnswerView.OWNER_LOOKUP:
        return _markdown(_owner_lookup_focus_text(answer))
    if view == AnswerView.PROJECT_MAP:
        return _markdown(_project_map_focus_text(answer))
    if view == AnswerView.CHANGE_IMPACT:
        return _markdown(_change_impact_focus_text(answer))
    if view == AnswerView.INCIDENT_COLLABORATION:
        return _markdown(_incident_collaboration_focus_text(answer))
    return None


def _project_map_focus_text(answer: ProjectAnswer) -> str:
    """Render architecture answers as a project navigation page, not a flat claim list."""

    lines = ["**项目地图**"]
    sections = (
        ("核心服务", ("核心服务：", "项目地图：")),
        ("关键入口", ("关键入口：",)),
        ("上下游", ("上下游依赖：",)),
        ("负责人", ("负责人",)),
        ("最近变更", ("最近变更：",)),
        ("风险/缺口", ("风险信号：",)),
    )
    for label, prefixes in sections:
        matched: list[str] = []
        for claim in answer.claims:
            if any(
                claim.text.startswith(prefix)
                or (prefix == "负责人" and "负责人" in claim.text)
                for prefix in prefixes
            ):
                matched.append(claim.text)
                break
        if matched:
            lines.append(f"- {label}：{matched[0]}")
        else:
            lines.append(f"- {label}：当前证据不足，见未知项。")
    graph_claims = [claim.text for claim in answer.claims if "项目关系图显示" in claim.text]
    if graph_claims:
        lines.append("- 关系路径：")
        lines.extend(f"  - {item}" for item in graph_claims[:3])
    gap_unknowns = [
        item
        for item in answer.unknowns
        if item.startswith("知识缺口") or item.startswith("[")
    ]
    if gap_unknowns:
        lines.append("- 知识缺口：")
        lines.extend(f"  - {item}" for item in gap_unknowns[:4])
    elif answer.unknowns:
        lines.append("- 未解决项：")
        lines.extend(f"  - {item}" for item in answer.unknowns[:3])
    lines.append("- 每条结论引用 Evidence / GraphEvidence；可继续追问负责人、上下游和变更影响。")
    lines.append("- 只读导航：不执行 Apply / PR / deploy / rollback / restart。")
    return "\n".join(lines)


def _project_intro_focus_text(answer: ProjectAnswer) -> str:
    lines = ["**项目概览**"]
    sections = (
        ("项目定位", "项目定位："),
        ("核心服务", "核心服务："),
        ("关键入口", "关键入口："),
        ("主要文档", "主要文档："),
        ("负责人", "负责人"),
        ("最近变更", "最近变更："),
        ("风险/缺口", "风险信号："),
    )
    for label, prefix in sections:
        matched = [
            claim.text
            for claim in answer.claims
            if claim.text.startswith(prefix) or (prefix == "负责人" and "负责人" in claim.text)
        ]
        if matched:
            lines.append(f"- {label}：{matched[0]}")
        else:
            lines.append(f"- {label}：当前证据不足，见未知项。")
    gap_unknowns = [
        item
        for item in answer.unknowns
        if item.startswith("知识缺口") or item.startswith("[")
    ]
    if gap_unknowns:
        lines.append("- 待补资料：")
        lines.extend(f"  - {item}" for item in gap_unknowns[:4])
    lines.append("- 事实均引用 Evidence / GraphEvidence；LLM 仅可做表达增强，不决定事实。")
    lines.append("- 不执行 Apply / PR / deploy / rollback / restart。")
    return "\n".join(lines)


def _knowledge_gap_focus_text(answer: ProjectAnswer) -> str:
    lines = ["**知识缺口报告**"]
    gap_lines = [
        item
        for item in answer.unknowns
        if item.startswith("[") or item.startswith("缺口：")
    ]
    if gap_lines:
        lines.extend(f"- {item}" for item in gap_lines[:8])
    elif answer.unknowns:
        lines.extend(f"- 缺口：{item}" for item in answer.unknowns[:6])
    else:
        lines.append("- 当前 ACL 范围内未发现明确缺口。")
    action_lines: list[str] = []
    for item in answer.recommended_actions[:5]:
        if item.tool_name == "engineering_proposal":
            continue
        line = f"- 建议动作：{item.title}"
        if item.description:
            line = f"{line} — {item.description[:180]}"
        action_lines.append(line)
    lines.extend(action_lines)
    lines.append("- 每条缺口尽量带「为何判断」信号与触发 Evidence；无证据则标未知。")
    lines.append("- 只把有来源和审批确认的信息沉淀为项目记忆；缺口本身不会自动写入 ProjectMemory。")
    return "\n".join(lines)


def _owner_lookup_focus_text(answer: ProjectAnswer) -> str:
    lines = ["**负责人视图**"]
    owner_claims = [
        claim
        for claim in answer.claims
        if "负责人" in claim.text or "owner" in claim.text.lower()
    ]
    if owner_claims:
        lines.extend(f"- {claim.text}" for claim in owner_claims[:5])
    elif answer.unknowns:
        lines.extend(f"- 未知：{item}" for item in answer.unknowns[:4])
    else:
        lines.append("- 当前未找到可展示的负责人结论。")
    owner_evidence = [
        item
        for item in answer.evidence
        if item.metadata.get("owner_user_id")
        or item.metadata.get("owner")
        or "owner" in item.content.lower()
        or "负责人" in item.content
    ]
    if owner_evidence:
        lines.append("- 证据：")
        lines.extend(
            f"  - {item.source.source_id}"
            + (
                f"（owner={item.metadata.get('owner_user_id') or item.metadata.get('owner')}）"
                if item.metadata.get("owner_user_id") or item.metadata.get("owner")
                else ""
            )
            for item in owner_evidence[:4]
        )
    lines.append("- 没有证据时明确标为未知，不会猜测负责人。")
    return "\n".join(lines)


def _follow_up_questions(
    skill: ProjectSkill | str,
    *,
    question: str = "",
) -> tuple[str, ...]:
    if question and is_project_intro_question(question):
        return (
            "项目地图里服务、入口和依赖分别是什么？",
            "最近哪些变更可能影响核心入口？",
            "知识库还缺什么资料？",
        )
    try:
        normalized = ProjectSkill(skill)
    except ValueError:
        normalized = ProjectSkill.PROJECT_KNOWLEDGE
    if normalized == ProjectSkill.ARCHITECTURE:
        return (
            "这个服务的负责人和上下游依赖是谁？",
            "最近哪些提交影响了这些入口？",
            "哪些架构风险还缺证据？",
        )
    if normalized == ProjectSkill.VERSION_CHANGE:
        return (
            "这次变更关联了哪些事故、任务或发布记录？",
            "需要回归哪些入口和场景？",
            "哪些服务或负责人需要一起确认？",
        )
    if normalized == ProjectSkill.INCIDENT_DIAGNOSIS:
        return (
            "影响范围现在是否还在扩大？",
            "有哪些历史事故和本次症状相似？",
            "最小修复或回滚方案需要哪些验证？",
        )
    if normalized == ProjectSkill.CODE_EXPLANATION:
        return (
            "这个函数的输入、输出和异常边界是什么？",
            "如果要修改，需要补哪些测试？",
            "这个实现影响哪些调用路径？",
        )
    return (
        "这个项目的架构、负责人和最近变更是什么？",
        "有哪些未解决风险需要团队补充？",
        "哪些资料应该沉淀进知识库？",
    )


def _claim_type_label(claim_type: ClaimType | str) -> str:
    try:
        normalized = ClaimType(claim_type)
    except ValueError:
        return str(claim_type)
    if normalized == ClaimType.FACT:
        return "事实"
    if normalized == ClaimType.INFERENCE:
        return "推断"
    return "未知"


def _engineering_proposal_text(
    action: ActionProposal,
    *,
    evidence_refs: tuple[str, ...] = (),
) -> str:
    """Read-only Engineering Validate section. Never includes an Apply button."""

    args = action.arguments
    explanation = str(args.get("explanation") or action.description)
    paths = args.get("affected_paths") or []
    if isinstance(paths, list):
        path_text = ", ".join(str(item) for item in paths) or "(none)"
    else:
        path_text = str(paths)
    patch_plan_text = str(args.get("patch_plan_text") or "").strip()
    if not patch_plan_text:
        patch_plan_text = _format_patch_plan_from_args(args.get("patch_plan"))
    diff_summary = str(args.get("diff_summary") or "")[:500]
    test_commands = args.get("test_commands") or []
    if isinstance(test_commands, list):
        commands_text = ", ".join(str(item) for item in test_commands) or "(none)"
    else:
        commands_text = str(test_commands)
    test_passed = bool(args.get("test_passed"))
    test_output = str(args.get("test_output_summary") or "")[:300]
    failed_attempts = args.get("failed_attempts") or []
    if isinstance(failed_attempts, list):
        failed_text = "; ".join(str(item) for item in failed_attempts) or "(none)"
    else:
        failed_text = str(failed_attempts)
    evidence_text = ", ".join(evidence_refs) if evidence_refs else "(见上方证据来源或无)"
    can_apply = bool(args.get("can_apply"))
    allow_apply = bool(args.get("allow_apply"))
    return (
        "**修复提案**\n"
        "- 阶段: Explain → Propose → Validate（只读）\n"
        f"- 问题解释: {explanation}\n"
        f"- 相关 Evidence: {evidence_text}\n"
        f"- affected_paths: {path_text}\n"
        f"- patch plan:\n{patch_plan_text or '(none)'}\n"
        f"- diff 摘要:\n```\n{diff_summary or '(empty)'}\n```\n"
        f"- test commands: {commands_text}\n"
        f"- test result: passed={test_passed}\n"
        f"- test output 摘要: {test_output or '(empty)'}\n"
        f"- failed attempts: {failed_text}\n"
        "- approval requirement: requires_approval=True（需人工审批）\n"
        f"- can_apply={can_apply}\n"
        f"- allow_apply={allow_apply}\n"
        "- 当前仅只读验证，不改主仓库、不创建 PR、不发布/回滚/重启，不会执行 Apply。"
    )


def _format_patch_plan_from_args(raw: object) -> str:
    if not isinstance(raw, dict):
        return ""
    title = str(raw.get("title") or "")
    rationale = str(raw.get("rationale") or "")
    lines = [f"{title}: {rationale}".strip(": ").strip()]
    steps = raw.get("steps") or []
    if isinstance(steps, list):
        for step in steps[:8]:
            if not isinstance(step, dict):
                continue
            path = str(step.get("path") or "")
            old = str(step.get("old_preview") or "")[:80]
            new = str(step.get("new_preview") or "")[:80]
            lines.append(f"- {path}: {old!r} -> {new!r}")
    return "\n".join(line for line in lines if line)[:800]


def _memory_proposal_text(proposal: MemoryProposal) -> str:
    lines = [
        "**建议沉淀为项目记忆**",
        proposal.claim_text,
        f"- 记忆类型：{memory_type_label(proposal.memory_type)}",
    ]
    if proposal.reason.strip():
        lines.append(f"- 为什么建议沉淀：{proposal.reason.strip()}")
    if proposal.evidence_ids:
        lines.append("- 引用了哪些证据：")
        lines.extend(f"  - {item_id}" for item_id in proposal.evidence_ids[:6])
    else:
        lines.append("- 引用了哪些证据：无（提案无效，不应出现）")
    if proposal.replaces_memory_id is not None:
        lines.append(f"- 将替换旧记忆：{proposal.replaces_memory_id}")
    lines.append(f"- proposal_id: {proposal.id}")
    lines.append("- 确认 / 暂不沉淀：人工确认前不会写入正式 ProjectMemory。")
    return "\n".join(lines)


def _memory_action_buttons(proposal_id: UUID) -> dict[str, object]:
    return {
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "确认沉淀"},
                "type": "primary",
                "value": {
                    "action": "memory_approve",
                    "proposal_id": str(proposal_id),
                },
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "暂不沉淀"},
                "type": "default",
                "value": {
                    "action": "memory_reject",
                    "proposal_id": str(proposal_id),
                },
            },
        ],
    }
