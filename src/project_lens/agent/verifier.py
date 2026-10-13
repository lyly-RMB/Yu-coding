"""Verify AnswerDraft into ProjectAnswer — attach citations when present."""

from __future__ import annotations

import re
from uuid import UUID

from project_lens.agent.draft import AnswerDraft, resolve_citation_id
from project_lens.agent.read_tools import InvestigationLedger
from project_lens.domain.models import (
    ActionProposal,
    Claim,
    ClaimType,
    Evidence,
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
    answer_text: str = "",
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
        # Keep the statement even when Hermes did not bind citations.
        claims.append(
            Claim(
                text=fact.text,
                type=ClaimType.FACT,
                evidence_ids=tuple(cited),
                grade=EvidenceGrade.B if cited else EvidenceGrade.C,
            )
        )

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
            business = f"暂无结论。已使用工具：{', '.join(draft.tools_used) or '无'}。"
        else:
            business = "调查完成，暂无结论。"

    technical = draft.technical_summary.strip() or (
        f"hermes tools={list(draft.tools_used)}; "
        f"evidence={len(evidence)}; facts={sum(1 for c in claims if c.type == ClaimType.FACT)}"
    )

    # Prefer claim-bound evidence; otherwise keep a small tool-evidence buffer.
    used_ids = {eid for claim in claims for eid in claim.evidence_ids}
    # Preserve explicit source markers written in Hermes' presentation text
    # as citation refs, without treating the prose itself as a verified fact.
    for evidence_id in _evidence_ids_cited_in_answer(answer_text, evidence):
        used_ids.add(evidence_id)
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
    conclusion = (
        facts[0].text
        if facts
        else (unknowns[0][:200] if unknowns else "调查完成，暂无结论。")
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
        status="identified" if facts else "unknown",
        skill="project_investigation",
        confidence=0.7 if facts else 0.25,
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
        answer_text=answer_text.strip(),
    )


_ANSWER_PATH_RE = re.compile(
    r"(?<![\w.-])(?:[\w.-]+/)+[\w./#-]+|(?<![\w.-])[\w.-]+\.(?:py|md|json|ya?ml)(?:#L\d+(?:-L?\d+)?)?",
    re.IGNORECASE,
)


def _evidence_ids_cited_in_answer(
    answer_text: str, evidence: tuple[Evidence, ...]
) -> set[UUID]:
    if not answer_text:
        return set()
    lowered = answer_text.casefold()
    uuid_tokens = set(re.findall(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        lowered,
    ))
    paths = {
        match.group(0).casefold().split("#", 1)[0].replace("\\", "/").strip("`'")
        for match in _ANSWER_PATH_RE.finditer(lowered)
    }
    cited = set()
    path_matches: dict[str, list[UUID]] = {path: [] for path in paths}
    for item in evidence:
        if str(item.id).casefold() in uuid_tokens:
            cited.add(item.id)
            continue
        source = item.source.source_id.casefold().replace("\\", "/").strip("/")
        if source and source in lowered:
            cited.add(item.id)
            continue
        source_path = source.split(":", 1)[-1].strip("/")
        for path in paths:
            if path == source_path or path.endswith("/" + source_path):
                path_matches[path].append(item.id)
            elif source_path.endswith("/" + path) and len(path) >= 12:
                path_matches[path].append(item.id)
    for matches in path_matches.values():
        if len(matches) == 1:
            cited.add(matches[0])
    return cited
