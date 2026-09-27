"""Verify AnswerDraft into ProjectAnswer — no uncited facts."""

from __future__ import annotations

from project_lens.agent.draft import AnswerDraft, resolve_citation_id
from project_lens.agent.read_tools import InvestigationLedger
from project_lens.domain.models import (
    ActionProposal,
    Claim,
    ClaimType,
    EvidenceGrade,
    EvidenceRef,
    ProjectAnswer,
    ProjectRef,
)


def verify_answer_draft(
    draft: AnswerDraft,
    *,
    project: ProjectRef,
    ledger: InvestigationLedger,
) -> ProjectAnswer:
    evidence = ledger.all_evidence()
    evidence_ids = {item.id for item in evidence}
    claims: list[Claim] = []
    unknowns = list(draft.unknowns)

    for fact in draft.facts:
        cited = []
        for raw in fact.citations:
            eid = resolve_citation_id(raw, evidence_ids)
            if eid is not None and eid not in cited:
                cited.append(eid)
        if cited:
            claims.append(
                Claim(
                    text=fact.text,
                    type=ClaimType.FACT,
                    evidence_ids=tuple(cited),
                    grade=EvidenceGrade.B,
                )
            )
        else:
            # Uncited "fact" must not stay FACT — demote to unknown/hypothesis.
            unknowns.append(f"未通过引用校验的候选结论：{fact.text[:160]}")

    for text in draft.inferences:
        claims.append(
            Claim(
                text=text,
                type=ClaimType.INFERENCE,
                evidence_ids=(),
                grade=EvidenceGrade.C,
            )
        )

    if not claims and not unknowns:
        unknowns.append("调查结束，但没有形成可展示的结论；请补充资料或更具体的文件路径。")

    actions = tuple(
        ActionProposal(title=item[:200], description=item, requires_approval=True)
        for item in draft.next_actions[:5]
    )

    business = draft.business_summary.strip()
    if not business:
        if claims:
            business = claims[0].text
        elif unknowns:
            business = (
                "当前资料不足以形成带引用结论。"
                f"已使用工具：{', '.join(draft.tools_used) or '无'}。"
            )
        else:
            business = "调查完成，暂无结论。"

    technical = draft.technical_summary.strip() or (
        f"hermes tools={list(draft.tools_used)}; "
        f"evidence={len(evidence)}; facts={sum(1 for c in claims if c.type == ClaimType.FACT)}"
    )

    # Keep only evidence referenced by claims (plus a small buffer of tool evidence).
    used_ids = {eid for claim in claims for eid in claim.evidence_ids}
    kept = tuple(item for item in evidence if item.id in used_ids) or evidence[:6]
    facts = tuple(item for item in claims if item.type == ClaimType.FACT)
    inferences = tuple(item for item in claims if item.type == ClaimType.INFERENCE)
    citations = tuple(
        EvidenceRef(
            id=item.id,
            kind=item.type,
            source_uri=f"{item.source.system}:{item.source.source_id}",
            summary=str(
                item.metadata.get("title")
                or item.metadata.get("path")
                or item.metadata.get("file")
                or item.source.source_id
            )[:300],
        )
        for item in kept
        if item.id in used_ids
    )
    conclusion = facts[0].text if facts else (
        "当前资料不足以形成带引用结论。"
        if unknowns
        else "调查完成，暂无已验证事实。"
    )
    impact_markers = ("影响", "风险", "用户", "业务", "范围", "impact", "risk")
    impact = tuple(
        item.text
        for item in facts
        if any(marker in item.text.casefold() for marker in impact_markers)
    )[:8]
    if not impact:
        impact = tuple(item.text for item in facts[:3])
    next_actions = tuple(
        dict.fromkeys(item.strip() for item in draft.next_actions if item.strip())
    )[:5]

    return ProjectAnswer(
        project=project,
        status="identified" if any(c.type == ClaimType.FACT for c in claims) else "unknown",
        skill="project_investigation",
        confidence=0.7 if any(c.type == ClaimType.FACT for c in claims) else 0.25,
        business_summary=business,
        technical_summary=technical,
        conclusion=conclusion,
        claims=tuple(claims),
        facts=facts,
        inferences=inferences,
        evidence=kept,
        impact=impact,
        next_actions=next_actions,
        citations=citations,
        policy_used="verified-ledger-v1",
        recommended_actions=actions,
        unknowns=tuple(dict.fromkeys(unknowns))[:8],
    )
