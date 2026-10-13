"""Role-aware projections over one verified ProjectAnswer fact core."""

from __future__ import annotations

from dataclasses import dataclass

from project_lens.domain.models import Claim, ClaimType, EvidenceRef, ProjectAnswer
from project_lens.project_space.policies import EffectiveAccessScope, RoleKind


@dataclass(frozen=True)
class AudienceSection:
    title: str
    items: tuple[str, ...]


@dataclass(frozen=True)
class AudienceAnswer:
    role: RoleKind
    view_role: RoleKind
    audience: str
    chat_type: str
    readable_sources: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    title: str
    conclusion: str
    sections: tuple[AudienceSection, ...]
    facts: tuple[Claim, ...]
    inferences: tuple[Claim, ...]
    unknowns: tuple[str, ...]
    impact: tuple[str, ...]
    next_actions: tuple[str, ...]
    citations: tuple[EvidenceRef, ...]
    policy_used: str
    answer_depth: str
    answer_style: str

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role.value,
            "view_role": self.view_role.value,
            "audience": self.audience,
            "chat_type": self.chat_type,
            "readable_sources": list(self.readable_sources),
            "allowed_tools": list(self.allowed_tools),
            "title": self.title,
            "conclusion": self.conclusion,
            "sections": [
                {"title": section.title, "items": list(section.items)}
                for section in self.sections
            ],
            "facts": [_claim_dict(item) for item in self.facts],
            "inferences": [_claim_dict(item) for item in self.inferences],
            "unknowns": list(self.unknowns),
            "impact": list(self.impact),
            "next_actions": list(self.next_actions),
            "citations": [item.model_dump(mode="json") for item in self.citations],
            "policy_used": self.policy_used,
            "answer_depth": self.answer_depth,
            "answer_style": self.answer_style,
        }


def render_audience_view(
    answer: ProjectAnswer,
    *,
    role: RoleKind,
    chat_type: str,
    scope: EffectiveAccessScope,
    audience: str | None = None,
) -> AudienceAnswer:
    """Reorder a verified answer without adding claims, evidence, or unknowns."""

    if answer.project.tenant_id != scope.project.tenant_id or (
        answer.project.project_id != scope.project.project_id
    ):
        raise PermissionError("answer project does not match effective access scope")
    if role != scope.role and audience in {None, "team"}:
        raise PermissionError("automatic audience role must match effective access scope")

    selected_role = role_for_audience(audience, default=role)
    audience_key = (audience or audience_for_role(selected_role)).strip().lower()
    facts = answer.facts or tuple(
        item for item in answer.claims if item.type == ClaimType.FACT
    )
    # Hermes presentation text is not a role-specific fact. Some legacy
    # envelopes repeated the complete answer in ``facts``; suppress that
    # duplicate from audience sections while retaining the verified claim in
    # the ProjectAnswer/evidence payload.
    answer_text = (answer.answer_text or "").strip()
    if answer_text:
        facts = tuple(item for item in facts if item.text.strip() != answer_text)
    inferences = answer.inferences or tuple(
        item for item in answer.claims if item.type == ClaimType.INFERENCE
    )
    citations = answer.citations or _citation_refs(answer)
    impact = answer.impact or _matching_texts(
        facts,
        ("影响", "风险", "用户", "业务", "范围", "impact", "risk"),
    )
    actions = answer.next_actions or tuple(
        item.title for item in answer.recommended_actions if item.title.strip()
    )
    conclusion = answer.answer_text or answer.conclusion or answer.business_summary or (
        facts[0].text if facts else "暂无结论。"
    )

    sections = _sections_for_role(
        selected_role,
        facts=facts,
        impact=impact,
        unknowns=answer.unknowns,
        actions=actions,
        citations=citations,
        audience=audience_key,
    )
    return AudienceAnswer(
        role=role,
        view_role=selected_role,
        audience=audience_key,
        chat_type=chat_type,
        readable_sources=scope.readable_sources,
        allowed_tools=scope.allowed_tools,
        title=_title_for(selected_role, audience_key),
        conclusion=conclusion,
        sections=sections,
        facts=facts,
        inferences=inferences,
        unknowns=answer.unknowns,
        impact=impact,
        next_actions=actions,
        citations=citations,
        policy_used=(
            answer.policy_used
            if answer.policy_used != "unknown"
            else f"{scope.policy_version}:{scope.role.value}:{scope.chat_type}"
        ),
        answer_depth=scope.answer_depth.value,
        answer_style=scope.answer_style,
    )


def render_audience_markdown(view: AudienceAnswer) -> str:
    lines = [f"### {view.title}", f"**结论**\n{view.conclusion}"]
    lines.append("")
    lines.append(
        "**当前身份 / 工具权限**\n"
        f"- 角色：{view.role.value}\n"
        f"- 可读范围：{_scope_list(view.readable_sources)}\n"
        f"- 可用工具：{_scope_list(view.allowed_tools)}"
    )
    for section in view.sections:
        if not section.items:
            continue
        lines.extend(("", f"**{section.title}**"))
        lines.extend(f"- {item}" for item in section.items)
    if view.unknowns and not any(
        section.title in {"阻塞 / 需要业务配合", "缺失覆盖 / 未关闭风险", "风险 / 决策点"}
        for section in view.sections
    ):
        lines.extend(("", "**当前未知**"))
        lines.extend(f"- {item}" for item in view.unknowns)
    lines.extend(
        (
            "",
            f"Audit: role={view.role.value} | audience={view.audience} | "
            f"chat_type={view.chat_type} | "
            f"policy={view.policy_used} | allow_apply=false",
        )
    )
    return "\n".join(lines).strip()


def _scope_list(items: tuple[str, ...]) -> str:
    if not items:
        return "（未限制/未显式配置）"
    return "、".join(items)


def audience_for_role(role: RoleKind) -> str:
    return {
        RoleKind.DEVELOPER: "technical",
        RoleKind.OPS: "ops",
        RoleKind.QA: "qa",
        RoleKind.PRODUCT: "business",
        RoleKind.MANAGER: "manager",
        RoleKind.ONBOARDING: "onboarding",
        RoleKind.GUEST: "team",
    }.get(role, "team")


def role_for_audience(audience: str | None, *, default: RoleKind) -> RoleKind:
    return {
        "technical": RoleKind.DEVELOPER,
        "business": RoleKind.PRODUCT,
        "qa": RoleKind.QA,
        "manager": RoleKind.MANAGER,
        "ops": RoleKind.OPS,
        "onboarding": RoleKind.ONBOARDING,
    }.get((audience or "").strip().lower(), default)


def _sections_for_role(
    role: RoleKind,
    *,
    facts: tuple[Claim, ...],
    impact: tuple[str, ...],
    unknowns: tuple[str, ...],
    actions: tuple[str, ...],
    citations: tuple[EvidenceRef, ...],
    audience: str,
) -> tuple[AudienceSection, ...]:
    if audience in {"evidence", "debug"}:
        return (
            AudienceSection(
                "本次参考来源",
                tuple(f"{item.kind.value}: {item.source_uri}" for item in citations),
            ),
            AudienceSection("已确认事实", tuple(item.text for item in facts)),
            AudienceSection("当前未知", unknowns),
        )
    if role is RoleKind.DEVELOPER:
        return _compact(
            AudienceSection("技术影响", impact or _texts(facts, limit=4)),
            AudienceSection(
                "相关模块 / 文件 / 接口",
                _matching_texts(facts, ("src/", "tests/", "模块", "文件", "接口", ".py", "module")),
            ),
            AudienceSection(
                "PR / CI / 变更线索",
                _matching_texts(facts, ("PR", "CI", "commit", "发布", "变更", "release")),
            ),
            AudienceSection("建议下一步", actions),
        )
    if role is RoleKind.OPS:
        return _compact(
            AudienceSection("当前影响", impact or _texts(facts, limit=4)),
            AudienceSection("时间线 / 最近变更", _matching_texts(facts, ("时间", "最近", "发布", "变更", "commit"))),
            AudienceSection("负责人 / 处置线索", _matching_texts(facts, ("负责人", "owner", "处置", "恢复"))),
            AudienceSection("缓解与下一步", actions),
        )
    if role is RoleKind.QA:
        return _compact(
            AudienceSection("验收标准", _matching_texts(facts, ("验收", "需求", "应该", "必须", "acceptance"))),
            AudienceSection("影响场景 / 回归范围", impact or _matching_texts(facts, ("影响", "场景", "入口", "回归", "测试"))),
            AudienceSection("失败用例 / 风险", _matching_texts(facts, ("失败", "错误", "异常", "风险", "failed"))),
            AudienceSection("缺失覆盖 / 未关闭风险", unknowns),
            AudienceSection("建议验证", actions),
        )
    if role is RoleKind.PRODUCT:
        return _compact(
            AudienceSection("这对业务 / 用户意味着什么", impact or _matching_texts(facts, ("用户", "业务", "影响", "范围"))),
            AudienceSection("需求范围 / 当前进度", _matching_texts(facts, ("需求", "范围", "进度", "状态", "发布"))),
            AudienceSection("阻塞 / 需要业务配合", unknowns),
            AudienceSection("协调下一步", actions),
        )
    if role is RoleKind.MANAGER:
        return _compact(
            AudienceSection("风险等级 / 影响", impact or _matching_texts(facts, ("风险", "严重", "影响", "severity"))),
            AudienceSection("里程碑 / 截止时间", _matching_texts(facts, ("里程碑", "截止", "日期", "发布", "deadline"))),
            AudienceSection("负责人", _matching_texts(facts, ("负责人", "owner", "负责"))),
            AudienceSection("风险 / 决策点", unknowns),
            AudienceSection("需要决策", actions),
        )
    return _compact(
        AudienceSection("对团队意味着什么", impact or _texts(facts, limit=4)),
        AudienceSection("已确认", _texts(facts, limit=5)),
        AudienceSection("当前未知", unknowns),
        AudienceSection("建议下一步", actions),
    )


def _citation_refs(answer: ProjectAnswer) -> tuple[EvidenceRef, ...]:
    used = {eid for claim in answer.claims for eid in claim.evidence_ids}
    return tuple(
        EvidenceRef(
            id=item.id,
            kind=item.type,
            source_uri=f"{item.source.system}:{item.source.source_id}",
            summary="",
        )
        for item in answer.evidence
        if item.id in used
    )


def _matching_texts(claims: tuple[Claim, ...], markers: tuple[str, ...]) -> tuple[str, ...]:
    lowered = tuple(marker.casefold() for marker in markers)
    return tuple(
        item.text
        for item in claims
        if any(marker in item.text.casefold() for marker in lowered)
    )


def _texts(claims: tuple[Claim, ...], *, limit: int) -> tuple[str, ...]:
    return tuple(item.text for item in claims[:limit])


def _compact(*sections: AudienceSection) -> tuple[AudienceSection, ...]:
    return tuple(section for section in sections if section.items)


def _title_for(role: RoleKind, audience: str) -> str:
    if audience == "evidence":
        return "证据视图"
    if audience == "debug":
        return "调试视图"
    return {
        RoleKind.DEVELOPER: "技术视图",
        RoleKind.OPS: "运维视图",
        RoleKind.QA: "测试视图",
        RoleKind.PRODUCT: "业务/产品视图",
        RoleKind.MANAGER: "管理/进度视图",
        RoleKind.ONBOARDING: "新人理解视图",
        RoleKind.GUEST: "团队协作视图",
    }.get(role, "团队协作视图")


def _claim_dict(claim: Claim) -> dict[str, object]:
    return {
        "id": str(claim.id),
        "text": claim.text,
        "type": claim.type.value,
        "evidence_ids": [str(item) for item in claim.evidence_ids],
        "grade": claim.grade.value,
    }
