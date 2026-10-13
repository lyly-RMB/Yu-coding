"""RoleView / AudienceView renderer for Feishu cards.

Presentation only: reorders and summarizes ProjectAnswer for an audience.
Never queries EvidenceIndex / graph / ops stores. Never invents facts.
"""

from __future__ import annotations

import re

from project_lens.application.audience_views import AudienceAnswer
from project_lens.domain.models import AgentRun, ClaimType, ProjectAnswer
from project_lens.integrations.feishu.audiences import AnswerAudience, audience_label
from project_lens.integrations.feishu.views import (
    AnswerView,
    confidence_status_label,
    human_evidence_label,
)


def render_role_view_elements(
    run: AgentRun,
    answer: ProjectAnswer,
    *,
    view: AnswerView,
    audience: AnswerAudience,
) -> list[dict[str, object]]:
    """Build markdown elements for one audience. No new facts."""

    if audience == AnswerAudience.DEBUG:
        return _debug_elements(run, answer)
    if audience == AnswerAudience.EVIDENCE:
        return _evidence_elements(run, answer)
    if audience == AnswerAudience.TECHNICAL:
        return _technical_elements(run, answer)
    if audience == AnswerAudience.BUSINESS:
        return _business_elements(run, answer)
    if audience == AnswerAudience.QA:
        return _qa_elements(run, answer)
    if audience == AnswerAudience.MANAGER:
        return _manager_elements(run, answer)
    if audience == AnswerAudience.ONBOARDING:
        return _onboarding_elements(run, answer)
    return _team_elements(run, answer, view=view)


def render_audience_view_elements(
    run: AgentRun,
    view: AudienceAnswer,
) -> list[dict[str, object]]:
    """Render a concise answer body; sources and runtime details are opt-in views."""

    elements = [
        _markdown(f"### {view.title}"),
        _markdown(f"**你问的是**\n{_short_question(run.question)}"),
        _markdown(view.conclusion),
    ]
    for section in view.sections:
        if section.items:
            elements.append(
                _markdown(
                    f"**{section.title}**\n" + "\n".join(f"- {item}" for item in section.items)
                )
            )
    return elements


def _audience_answer_detail_lines(view: AudienceAnswer, *, limit: int = 5) -> list[str]:
    """Keep the default card conversational instead of exposing a fixed report schema."""

    lines: list[str] = []
    seen: set[str] = {view.conclusion.strip()}
    for section in view.sections:
        for item in section.items:
            text = item.strip()
            if text and text not in seen:
                lines.append(f"- {text}")
                seen.add(text)
            if len(lines) >= limit:
                return lines
    for item in view.unknowns:
        text = item.strip()
        if text and text not in seen:
            lines.append(f"- 仍待确认：{text}")
            seen.add(text)
        if len(lines) >= limit:
            break
    return lines


def source_summary_line(answer: ProjectAnswer) -> str:
    """State which sources the answer bound — or that it bound none."""

    by_id = {item.id: item for item in answer.evidence}
    labels: list[str] = []
    seen: set[str] = set()
    for claim in answer.claims:
        for evidence_id in claim.evidence_ids:
            item = by_id.get(evidence_id)
            if item is None:
                continue
            label = human_evidence_label(item)
            if not label or label in seen:
                continue
            seen.add(label)
            labels.append(f"- {label}")
            if len(labels) >= 8:
                break
        if len(labels) >= 8:
            break
    for citation in answer.citations:
        item = by_id.get(citation.id)
        label = (
            human_evidence_label(item)
            if item is not None
            else citation.source_uri
        )
        if label and label not in seen:
            seen.add(label)
            labels.append(f"- {label}")
        if len(labels) >= 8:
            break
    if labels:
        return "引用资料：\n" + "\n".join(labels)
    return "引用资料：没有"


def cited_fact_lines(answer: ProjectAnswer, *, limit: int = 5) -> list[str]:
    lines: list[str] = []
    for claim in answer.claims:
        if claim.type != ClaimType.FACT:
            continue
        lines.append(f"- {claim.text}")
        if len(lines) >= limit:
            break
    return lines


def inference_lines(answer: ProjectAnswer, *, limit: int = 3) -> list[str]:
    lines: list[str] = []
    for claim in answer.claims:
        if claim.type != ClaimType.INFERENCE:
            continue
        lines.append(f"- {claim.text}")
        if len(lines) >= limit:
            break
    return lines


def technical_anchor_lines(answer: ProjectAnswer, *, limit: int = 8) -> list[str]:
    """Paths / symbols / commits already present on evidence or claim text."""

    seen: set[str] = set()
    lines: list[str] = []
    for item in answer.evidence:
        meta = item.metadata or {}
        for key in ("path", "file_path", "symbol", "function", "route", "commit", "sha"):
            value = meta.get(key)
            if not value:
                continue
            text = str(value).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            label = {
                "path": "文件",
                "file_path": "文件",
                "symbol": "符号",
                "function": "函数",
                "route": "接口",
                "commit": "commit",
                "sha": "commit",
            }.get(key, "锚点")
            lines.append(f"- {label}：{text[:120]}")
            if len(lines) >= limit:
                return lines
        label = human_evidence_label(item)
        if label and label not in seen and (
            "/" in label or label.endswith((".py", ".md", ".json"))
        ):
            seen.add(label)
            lines.append(f"- 来源：{label[:120]}")
            if len(lines) >= limit:
                return lines
    # Claim text may mention files already verified in the answer body.
    path_re = re.compile(
        r"(?:src/|tests/|knowledge/|docs/)?[\w./-]+\.(?:py|md|json|yml|yaml)\b",
        re.IGNORECASE,
    )
    for claim in answer.claims:
        if claim.type != ClaimType.FACT:
            continue
        for match in path_re.findall(claim.text):
            if match in seen:
                continue
            seen.add(match)
            lines.append(f"- 文件：{match}")
            if len(lines) >= limit:
                return lines
    return lines


def action_lines(answer: ProjectAnswer, *, limit: int = 5) -> list[str]:
    lines: list[str] = []
    index = 1
    for item in answer.recommended_actions:
        if item.tool_name == "engineering_proposal":
            continue
        suffix = "（需要审批）" if item.requires_approval else ""
        lines.append(f"{index}. {item.title}{suffix}")
        index += 1
        if len(lines) >= limit:
            break
    if not lines:
        lines = [
            "1. 需要时切换「给技术看的版本」或「给产品/业务看的版本」",
            "2. 点击「查看证据」核验来源",
            "3. 资料不足时询问「知识库缺什么」",
        ]
    return lines


def _markdown(content: str) -> dict[str, object]:
    return {"tag": "markdown", "content": content}


def _short_question(question: str, *, limit: int = 200) -> str:
    text = " ".join(question.strip().split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _status_line(answer: ProjectAnswer) -> str:
    return confidence_status_label(
        answer.confidence, has_evidence=bool(answer.evidence)
    )


def _answer_body(answer: ProjectAnswer) -> str:
    return (answer.answer_text or answer.business_summary or answer.conclusion or "").strip()


def _impact_lines(answer: ProjectAnswer) -> list[str]:
    """Audience-facing impact lines derived only from existing summaries/claims."""

    lines: list[str] = []
    summary = _answer_body(answer)
    if summary:
        lines.append(f"- {summary[:220]}")
    for claim in answer.claims:
        if claim.type != ClaimType.FACT:
            continue
        if any(
            marker in claim.text
            for marker in ("影响", "风险", "入口", "服务", "用户", "业务", "发布")
        ):
            lines.append(f"- {claim.text}")
        if len(lines) >= 4:
            break
    if len(lines) < 2:
        for claim in answer.claims:
            if claim.type == ClaimType.FACT:
                text = f"- {claim.text}"
                if text not in lines:
                    lines.append(text)
            if len(lines) >= 3:
                break
    if not lines:
        lines.append("- 当前结论见上方一句话；需要更多细节可继续追问。")
    return lines[:4]


def _team_elements(
    run: AgentRun,
    answer: ProjectAnswer,
    *,
    view: AnswerView,
) -> list[dict[str, object]]:
    del view  # question type already selected the card title
    elements = [
        _markdown(f"**你问的是**\n{_short_question(run.question)}"),
        _markdown(f"**回答**\n{_answer_body(answer) or '（暂无结论，见未知项）'}"),
        _markdown(f"**当前状态**\n{_status_line(answer)}"),
    ]
    if not answer.answer_text:
        elements.append(
            _markdown("**对团队意味着什么**\n" + "\n".join(_impact_lines(answer)))
        )
        confirmed = cited_fact_lines(answer)
        if confirmed:
            elements.append(_markdown("**已确认**\n" + "\n".join(confirmed)))
        else:
            elements.append(_markdown("**已确认**\n- 暂无确认事实。"))
    if answer.unknowns:
        elements.append(
            _markdown(
                "**当前未知**\n" + "\n".join(f"- {item}" for item in answer.unknowns[:6])
            )
        )
    else:
        elements.append(_markdown("**当前未知**\n- 暂无额外未知项。"))
    elements.append(_markdown("**建议下一步**\n" + "\n".join(action_lines(answer))))
    elements.append(_markdown(f"**来源摘要**\n{source_summary_line(answer)}"))
    elements.append(_markdown(f"**当前视图**\n{audience_label(AnswerAudience.TEAM)}"))
    return elements


def _technical_elements(run: AgentRun, answer: ProjectAnswer) -> list[dict[str, object]]:
    elements = [
        _markdown(f"**你问的是**\n{_short_question(run.question)}"),
        _markdown(f"**回答**\n{_answer_body(answer) or '（暂无结论）'}"),
        _markdown(f"**当前状态**\n{_status_line(answer)}"),
        _markdown(f"**当前视图**\n{audience_label(AnswerAudience.TECHNICAL)}"),
    ]
    anchors = technical_anchor_lines(answer)
    elements.append(
        _markdown(
            "**相关文件 / 函数 / 接口 / commit**\n"
            + ("\n".join(anchors) if anchors else "- 当前答案未携带可展示的技术锚点。")
        )
    )
    confirmed = cited_fact_lines(answer)
    elements.append(
        _markdown(
            "**已确认事实**\n"
            + ("\n".join(confirmed) if confirmed else "- 暂无确认事实。")
        )
    )
    inferences = inference_lines(answer)
    if inferences:
        elements.append(_markdown("**技术推断 / 假设**\n" + "\n".join(inferences)))
    if answer.unknowns:
        elements.append(
            _markdown(
                "**可能原因 / 未知**\n"
                + "\n".join(f"- {item}" for item in answer.unknowns[:6])
            )
        )
    elements.append(
        _markdown(
            "**排查与验证建议**\n"
            + "\n".join(action_lines(answer))
            + "\n- 只读边界：不执行 Apply / PR / deploy / rollback / restart"
        )
    )
    elements.append(_markdown(f"**来源摘要**\n{source_summary_line(answer)}"))
    return elements


def _business_elements(run: AgentRun, answer: ProjectAnswer) -> list[dict[str, object]]:
    plain = _answer_body(answer) or "（暂无业务结论）"
    elements = [
        _markdown(f"**你问的是**\n{_short_question(run.question)}"),
        _markdown(f"**回答**\n{plain}"),
        _markdown(f"**当前视图**\n{audience_label(AnswerAudience.BUSINESS)}"),
        _markdown("**这对业务/用户意味着什么**\n" + "\n".join(_impact_lines(answer))),
        _markdown(
            "**工程当前进度**\n"
            f"- 回答状态：{_status_line(answer)}\n"
            f"- 已引用资料：{len(answer.evidence)} 条"
        ),
    ]
    confirmed = cited_fact_lines(answer, limit=4)
    elements.append(
        _markdown(
            "**已经确认**\n"
            + ("\n".join(confirmed) if confirmed else "- 暂无确认项。")
        )
    )
    if answer.unknowns:
        elements.append(
            _markdown(
                "**仍未知 / 需要业务配合**\n"
                + "\n".join(f"- {item}" for item in answer.unknowns[:6])
            )
        )
    elements.append(
        _markdown(
            "**建议业务/产品下一步**\n" + "\n".join(action_lines(answer))
        )
    )
    elements.append(_markdown(f"**来源摘要**\n{source_summary_line(answer)}"))
    return elements


def _qa_elements(run: AgentRun, answer: ProjectAnswer) -> list[dict[str, object]]:
    elements = [
        _markdown(f"**你问的是**\n{_short_question(run.question)}"),
        _markdown(f"**回答**\n{_answer_body(answer) or '（暂无结论）'}"),
        _markdown(f"**当前视图**\n{audience_label(AnswerAudience.QA)}"),
    ]
    impact = [
        f"- {claim.text}"
        for claim in answer.claims
        if claim.type == ClaimType.FACT
        and any(m in claim.text for m in ("影响", "入口", "场景", "服务", "风险"))
    ][:5]
    if not impact:
        impact = cited_fact_lines(answer, limit=4)
    elements.append(
        _markdown(
            "**影响场景 / 回归范围**\n"
            + ("\n".join(impact) if impact else "- 暂无影响场景要点。")
        )
    )
    elements.append(
        _markdown(
            "**建议验证**\n"
            + "\n".join(action_lines(answer))
            + "\n- 验收时核对：已确认事实是否仍成立，未知项是否关闭"
        )
    )
    if answer.unknowns:
        elements.append(
            _markdown(
                "**未关闭风险**\n"
                + "\n".join(f"- {item}" for item in answer.unknowns[:6])
            )
        )
    elements.append(_markdown(f"**来源摘要**\n{source_summary_line(answer)}"))
    return elements


def _manager_elements(run: AgentRun, answer: ProjectAnswer) -> list[dict[str, object]]:
    elements = [
        _markdown(f"**你问的是**\n{_short_question(run.question)}"),
        _markdown(f"**当前状态**\n{_status_line(answer)}"),
        _markdown(f"**回答**\n{_answer_body(answer) or '（暂无结论）'}"),
        _markdown(f"**当前视图**\n{audience_label(AnswerAudience.MANAGER)}"),
        _markdown("**影响摘要**\n" + "\n".join(_impact_lines(answer))),
    ]
    owner_lines = [
        f"- {claim.text}"
        for claim in answer.claims
        if claim.type == ClaimType.FACT
        and any(m in claim.text for m in ("负责人", "owner", "负责"))
    ][:3]
    if owner_lines:
        elements.append(_markdown("**负责人 / 责任线索**\n" + "\n".join(owner_lines)))
    if answer.unknowns:
        elements.append(
            _markdown(
                "**阻塞 / 风险 / 决策点**\n"
                + "\n".join(f"- {item}" for item in answer.unknowns[:6])
            )
        )
    elements.append(_markdown("**下一步检查点**\n" + "\n".join(action_lines(answer))))
    elements.append(_markdown(f"**来源摘要**\n{source_summary_line(answer)}"))
    return elements


def _onboarding_elements(run: AgentRun, answer: ProjectAnswer) -> list[dict[str, object]]:
    elements = [
        _markdown(f"**你问的是**\n{_short_question(run.question)}"),
        _markdown(f"**回答**\n{_answer_body(answer) or '（暂无定位）'}"),
        _markdown(f"**当前视图**\n{audience_label(AnswerAudience.ONBOARDING)}"),
    ]
    facts = cited_fact_lines(answer, limit=5)
    elements.append(
        _markdown(
            "**建议先理解**\n"
            + ("\n".join(facts) if facts else "- 暂无要点，先看来源摘要或继续追问。")
        )
    )
    anchors = technical_anchor_lines(answer, limit=5)
    if anchors:
        elements.append(_markdown("**入口 / 关键资料**\n" + "\n".join(anchors)))
    elements.append(
        _markdown(
            "**推荐下一步阅读**\n" + "\n".join(action_lines(answer))
        )
    )
    elements.append(_markdown(f"**来源摘要**\n{source_summary_line(answer)}"))
    return elements


def _evidence_elements(run: AgentRun, answer: ProjectAnswer) -> list[dict[str, object]]:
    elements = [
        _markdown(f"**你问的是**\n{_short_question(run.question)}"),
        _markdown(f"**当前视图**\n{audience_label(AnswerAudience.EVIDENCE)}"),
        _markdown(f"**回答**\n{_answer_body(answer) or '（暂无结论）'}"),
    ]
    if not answer.evidence:
        elements.append(_markdown("**本次参考来源**\n- 当前答案没有可展示的 Evidence。"))
        return elements
    lines = [f"- {human_evidence_label(item)}" for item in answer.evidence[:12]]
    elements.append(_markdown("**本次参考来源**\n" + "\n".join(lines)))
    elements.append(
        _markdown(
            f"**来源数量**\n共 {len(answer.evidence)} 条。"
            " Evidence ID / provider / tool 细节见调试视图。"
        )
    )
    return elements


def _debug_elements(run: AgentRun, answer: ProjectAnswer) -> list[dict[str, object]]:
    evid_ids = ", ".join(str(item.id) for item in answer.evidence[:5]) or "(none)"
    return [
        _markdown(f"**你问的是**\n{_short_question(run.question)}"),
        _markdown(f"**当前视图**\n{audience_label(AnswerAudience.DEBUG)}"),
        _markdown(
            "**调试摘要**\n"
            f"- skill: {answer.skill}\n"
            f"- confidence: {answer.confidence}\n"
            f"- evidence_count: {len(answer.evidence)}\n"
            f"- claim_count: {len(answer.claims)}\n"
            f"- evidence_ids: {evid_ids}\n"
            f"- technical_summary: {(answer.technical_summary or '')[:240]}\n"
            f"- trace_id: {run.trace_id}\n"
            "- 完整 audit 见卡片底部调试区（若开启）。"
        ),
    ]
