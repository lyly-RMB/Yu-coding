"""Create memory proposals from verified answers without Feishu coupling."""

from __future__ import annotations

from datetime import datetime
from collections.abc import Sequence
from uuid import UUID, uuid5
import re

from project_lens.domain.memory import MemoryProposal, MemoryType
from project_lens.domain.memory import Episode
from project_lens.domain.models import ClaimType, ProjectAnswer


def infer_memory_type(claim_text: str, *, skill: str | None = None) -> MemoryType:
    """Heuristic memory taxonomy for proposal routing; never writes memory by itself."""

    normalized = claim_text.casefold()
    if any(term in normalized for term in ("负责人", "owner", "谁负责")):
        return MemoryType.OWNER
    if any(term in normalized for term in ("复盘", "postmortem", "事后总结")):
        return MemoryType.POSTMORTEM
    if any(term in normalized for term in ("runbook", "手册", "操作步骤")):
        return MemoryType.RUNBOOK
    if any(term in normalized for term in ("决策", "decision", "拍板")):
        return MemoryType.DECISION
    if any(term in normalized for term in ("风险", "risk", "边界条件")):
        return MemoryType.RISK
    if any(term in normalized for term in ("业务规则", "business rule", "必须", "不得")):
        return MemoryType.BUSINESS_RULE
    if any(term in normalized for term in ("约定", "convention", "规范")):
        return MemoryType.TEAM_CONVENTION
    if skill == "architecture" or any(
        term in normalized for term in ("架构", "服务", "入口", "依赖", "architecture")
    ):
        return MemoryType.ARCHITECTURE_FACT
    return MemoryType.ARCHITECTURE_FACT


def propose_memory_from_answer(
    answer: ProjectAnswer,
    *,
    proposed_by: str,
    reason: str | None = None,
    allowed_approvers: tuple[str, ...] = (),
    expires_at: datetime | None = None,
    replaces_memory_id: UUID | None = None,
) -> MemoryProposal | None:
    """Return a pending proposal for the first evidence-backed FACT claim, if any."""

    fact = next(
        (
            claim
            for claim in answer.claims
            if claim.type == ClaimType.FACT and claim.evidence_ids
        ),
        None,
    )
    if fact is None:
        return None
    # Hermes may emit a long answer_markdown as one FACT; MemoryProposal caps at 2k.
    claim_text = fact.text.strip()[:2_000]
    if not claim_text:
        return None
    memory_type = infer_memory_type(claim_text, skill=answer.skill)
    resolved_reason = reason or (
        f"已验证 FACT（skill={answer.skill or 'unknown'}），"
        f"建议沉淀为 {memory_type.value}；人工确认前不写入正式 ProjectMemory。"
    )
    payload: dict[str, object] = {
        "project": answer.project,
        "proposed_by": proposed_by,
        "claim_text": claim_text,
        "claim_type": fact.type,
        "memory_type": memory_type,
        "evidence_ids": fact.evidence_ids,
        "reason": resolved_reason,
        "status": "pending",
        "allowed_approvers": allowed_approvers,
    }
    if expires_at is not None:
        payload["expires_at"] = expires_at
    if replaces_memory_id is not None:
        payload["replaces_memory_id"] = replaces_memory_id
    return MemoryProposal.model_validate(payload)


def propose_memory_from_episode(
    episode: Episode,
    *,
    proposed_by: str,
    claim_text: str | None = None,
    reason: str | None = None,
    allowed_approvers: tuple[str, ...] = (),
    expires_at: datetime | None = None,
    replaces_memory_id: UUID | None = None,
) -> MemoryProposal | None:
    """Create a pending candidate from a historical Episode.

    Episode consolidation is intentionally proposal-only. A historical summary
    is eligible only when it has Evidence IDs and non-placeholder content;
    approval is still required before it can become ProjectMemory.
    """

    text = (claim_text or episode.summary).strip()
    if not episode.evidence_ids or not text or _is_placeholder_episode_text(text):
        return None
    text = text[:2_000]
    memory_type = infer_memory_type(text)
    resolved_reason = reason or (
        f"来自历史 Episode {episode.id} 的 Evidence-backed 候选（status={episode.status}）；"
        "仅创建待审批 MemoryProposal，不自动写入 ProjectMemory。"
    )
    payload: dict[str, object] = {
        "project": episode.project,
        "proposed_by": proposed_by,
        "claim_text": text,
        "claim_type": ClaimType.FACT,
        "memory_type": memory_type,
        "evidence_ids": episode.evidence_ids,
        "reason": resolved_reason,
        "status": "pending",
        "allowed_approvers": allowed_approvers,
    }
    if expires_at is not None:
        payload["expires_at"] = expires_at
    if replaces_memory_id is not None:
        payload["replaces_memory_id"] = replaces_memory_id
    return MemoryProposal.model_validate(payload)


def consolidate_episodes(
    episodes: Sequence[Episode],
    *,
    proposed_by: str,
    allowed_approvers: tuple[str, ...] = (),
    expires_at: datetime | None = None,
    similarity_threshold: float = 0.85,
) -> dict[str, object]:
    """Convert a bounded, already-authorized Episode batch into proposals.

    This is deliberately a pure proposal-generation step. Episodes from
    different projects are never compared, duplicate summaries are collapsed,
    and no proposal is persisted or approved here.
    """

    threshold = min(0.99, max(0.5, float(similarity_threshold)))
    grouped: dict[tuple[str, str], list[Episode]] = {}
    skipped: dict[str, int] = {"missing_evidence": 0, "placeholder": 0, "duplicate": 0}
    for episode in episodes:
        if not episode.evidence_ids:
            skipped["missing_evidence"] += 1
            continue
        text = episode.summary.strip()
        if not text or _is_placeholder_episode_text(text):
            skipped["placeholder"] += 1
            continue
        memory_type = infer_memory_type(text)
        key = (episode.project.tenant_id, episode.project.project_id)
        group = grouped.setdefault(key, [])
        normalized = _proposal_normalize(text)
        if any(
            _proposal_similarity(normalized, _proposal_normalize(item.summary)) >= threshold
            for item in group
        ):
            skipped["duplicate"] += 1
            continue
        group.append(episode)

    proposals: list[MemoryProposal] = []
    for project_key in sorted(grouped):
        representatives = sorted(
            grouped[project_key],
            key=lambda item: (-len(item.evidence_ids), -item.ended_at.timestamp(), str(item.id)),
        )
        for episode in representatives:
            proposal = propose_memory_from_episode(
                episode,
                proposed_by=proposed_by,
                allowed_approvers=allowed_approvers,
                expires_at=expires_at,
                reason=(
                    f"批量 consolidation 来源 Episode {episode.id}；"
                    f"项目={episode.project.project_id}，仅生成待审批候选。"
                ),
            )
            if proposal is not None:
                proposals.append(proposal)
    return {
        "proposals": tuple(proposals),
        "proposal_count": len(proposals),
        "input_count": len(episodes),
        "skipped": skipped,
        "similarity_threshold": threshold,
    }


def submit_consolidated_episodes(
    episodes: Sequence[Episode],
    *,
    approval_gateway,
    proposed_by: str,
    allowed_approvers: tuple[str, ...] = (),
    expires_at: datetime | None = None,
    similarity_threshold: float = 0.85,
) -> dict[str, object]:
    """Persist batch consolidation results through the approval gateway.

    The gateway is the only write boundary. Stable proposal IDs make retries
    idempotent while preserving an already-approved proposal's state.
    """

    report = consolidate_episodes(
        episodes,
        proposed_by=proposed_by,
        allowed_approvers=allowed_approvers,
        expires_at=expires_at,
        similarity_threshold=similarity_threshold,
    )
    submitted: list[MemoryProposal] = []
    errors: list[dict[str, str]] = []
    for proposal in report["proposals"]:
        source_episode = next(
            (item for item in episodes if item.project == proposal.project and _proposal_normalize(item.summary) == _proposal_normalize(proposal.claim_text)),
            None,
        )
        if source_episode is not None:
            stable_id = uuid5(source_episode.id, f"memory-proposal:{_proposal_normalize(proposal.claim_text)}")
            proposal = proposal.model_copy(update={"id": stable_id})
        try:
            submitted.append(approval_gateway.create_memory_proposal(proposal))
        except Exception as exc:
            errors.append({"proposal_id": str(proposal.id), "error": str(exc)[:500]})
    return {
        **report,
        "proposals": tuple(submitted),
        "submitted_count": len(submitted),
        "errors": tuple(errors),
    }


def _is_placeholder_episode_text(text: str) -> bool:
    normalized = text.casefold().strip()
    return normalized in {
        "未生成回答",
        "未生成答案",
        "no answer generated",
        "未完成",
    }


def _proposal_normalize(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text.casefold()))


def _proposal_similarity(left: str, right: str) -> float:
    left_tokens, right_tokens = set(left.split()), set(right.split())
    if not left_tokens or not right_tokens:
        return 1.0 if left == right else 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
