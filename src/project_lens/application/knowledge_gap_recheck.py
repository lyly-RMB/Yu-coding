"""Revalidate knowledge gaps through a caller-authorized verifier."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

from project_lens.application.knowledge_gap_service import KnowledgeGapService
from project_lens.application.hermes_runtime import HermesRuntimeService
from project_lens.domain.identity import ActorContext
from project_lens.domain.knowledge_gap import KnowledgeGapRecheck, KnowledgeGapStatus
from project_lens.domain.knowledge_gap import KnowledgeGapRecheckJob
from project_lens.domain.models import ProjectRef
from project_lens.project_space.policies import EffectiveAccessScope


@dataclass(frozen=True)
class RecheckEvidence:
    evidence_id: UUID


@dataclass(frozen=True)
class RecheckVerification:
    answered: bool
    complete: bool
    evidence: tuple[RecheckEvidence, ...] = ()
    conflict: bool = False
    summary: str = ""


class KnowledgeGapVerifier(Protocol):
    async def verify(self, *, question: str, project: ProjectRef, actor: ActorContext, scope: EffectiveAccessScope) -> RecheckVerification: ...


class HermesKnowledgeGapVerifier:
    def __init__(self, runtime: HermesRuntimeService) -> None:
        self._runtime = runtime

    async def verify(self, *, question: str, project: ProjectRef, actor: ActorContext, scope: EffectiveAccessScope) -> RecheckVerification:
        result = await self._runtime.execute(
            project=project,
            actor=actor,
            scope=scope,
            question=question,
            entry_mode="knowledge_gap_recheck",
        )
        answer = result.answer
        if answer is None:
            return RecheckVerification(answered=False, complete=False)
        evidence = tuple(RecheckEvidence(item.id) for item in answer.evidence)
        factual_claims = tuple(claim for claim in answer.claims if claim.type.value == "fact")
        has_cited_claim = any(claim.evidence_ids for claim in factual_claims)
        conflict_markers = ("冲突", "不一致", "conflict", "contradict", "disagree")
        conflict = any(any(marker in item.casefold() for marker in conflict_markers) for item in answer.unknowns)
        complete = not answer.unknowns and not conflict and bool(factual_claims) and has_cited_claim
        return RecheckVerification(
            answered=bool(factual_claims),
            complete=complete,
            evidence=evidence,
            conflict=conflict,
            summary=answer.conclusion or answer.business_summary,
        )


@dataclass(frozen=True)
class KnowledgeGapRecheckResult:
    case: object
    audit: KnowledgeGapRecheck


class KnowledgeGapRecheckService:
    def __init__(self, gaps: KnowledgeGapService, verifier: KnowledgeGapVerifier) -> None:
        self._gaps = gaps
        self._verifier = verifier

    async def recheck(
        self,
        gap_id: UUID,
        *,
        actor: ActorContext,
        scope: EffectiveAccessScope,
        authorized_project: ProjectRef,
        trigger: str,
        now: datetime | None = None,
    ) -> KnowledgeGapRecheckResult:
        case = self._gaps.get_case(gap_id)
        if case is None or not _same_project(case.project, authorized_project):
            raise KeyError(f"knowledge gap not found: {gap_id}")
        checked_at = _utc(now or datetime.now(timezone.utc))
        old_status = case.status
        if old_status not in {KnowledgeGapStatus.RESOLVED_PENDING_CHECK, KnowledgeGapStatus.RESOLVED, KnowledgeGapStatus.WAITING_FOR_SOURCE, KnowledgeGapStatus.REOPENED}:
            raise ValueError(f"knowledge gap status is not recheckable: {old_status.value}")
        if actor.actor_id != scope.actor_id or actor.chat_id != scope.chat_id or actor.chat_type != scope.chat_type:
            raise PermissionError("recheck scope is not bound to the current actor and chat")
        if actor.tenant_key != authorized_project.tenant_id or scope.project != authorized_project:
            raise PermissionError("recheck scope does not match the authorized project")

        try:
            verification = await self._verifier.verify(question=case.question, project=authorized_project, actor=actor, scope=scope)
        except Exception:
            verification = None

        evidence_ids = tuple(dict.fromkeys(item.evidence_id for item in verification.evidence)) if verification else ()
        if verification and verification.conflict:
            new_status = KnowledgeGapStatus.REOPENED
            outcome = "conflict_found"
        elif verification and verification.answered and verification.complete and evidence_ids:
            new_status = KnowledgeGapStatus.RESOLVED
            outcome = "verified_answer"
        elif old_status == KnowledgeGapStatus.RESOLVED:
            new_status = KnowledgeGapStatus.REOPENED
            outcome = "gap_still_present_or_verification_failed"
        else:
            new_status = KnowledgeGapStatus.RESOLVED_PENDING_CHECK
            outcome = "still_blocked" if verification else "verification_failed"

        if new_status != old_status:
            case = self._gaps.transition(
                gap_id,
                new_status,
                actor_id=actor.actor_id,
                reason=f"Knowledge-gap recheck: {outcome}",
                now=checked_at,
            )
        audit = KnowledgeGapRecheck(
            gap_id=gap_id,
            project=authorized_project,
            trigger=trigger,
            actor_id=actor.actor_id,
            old_status=old_status,
            new_status=new_status,
            evidence_ids=evidence_ids,
            outcome=outcome,
            created_at=checked_at,
        )
        self._gaps.record_recheck(audit)
        return KnowledgeGapRecheckResult(case=case, audit=audit)

    async def dispatch_source_jobs(
        self, *, project: ProjectRef, actor: ActorContext, scope: EffectiveAccessScope,
        now: datetime | None = None,
    ) -> tuple[KnowledgeGapRecheckResult, ...]:
        """Run persisted source-update jobs only under the caller's current scope."""
        if actor.actor_id != scope.actor_id or actor.chat_id != scope.chat_id or actor.chat_type != scope.chat_type:
            raise PermissionError("recheck scope is not bound to the current actor and chat")
        if actor.tenant_key != project.tenant_id or scope.project != project:
            raise PermissionError("recheck scope does not match the authorized project")
        results: list[KnowledgeGapRecheckResult] = []
        for job in self._gaps.list_recheck_jobs(project):
            case = self._gaps.get_case(job.gap_id)
            if case is None or not _same_project(case.project, project):
                self._gaps.update_recheck_job(job.model_copy(update={
                    "status": "failed", "attempts": job.attempts + 1,
                    "last_error": "gap is no longer available in this project",
                    "updated_at": _utc(now or datetime.now(timezone.utc)),
                }))
                continue
            if case.status not in {KnowledgeGapStatus.WAITING_FOR_SOURCE, KnowledgeGapStatus.RESOLVED_PENDING_CHECK, KnowledgeGapStatus.RESOLVED, KnowledgeGapStatus.REOPENED}:
                # Keep the job pending until an owner requests verification.
                continue
            started = job.model_copy(update={"status": "running", "attempts": job.attempts + 1, "last_error": None, "updated_at": _utc(now or datetime.now(timezone.utc))})
            self._gaps.update_recheck_job(started)
            try:
                result = await self.recheck(
                    job.gap_id, actor=actor, scope=scope, authorized_project=project,
                    trigger=f"source_update:{job.source_ref}@{job.revision}", now=now,
                )
            except Exception as exc:
                self._gaps.update_recheck_job(started.model_copy(update={
                    "status": "failed", "last_error": str(exc)[:500],
                    "updated_at": _utc(now or datetime.now(timezone.utc)),
                }))
                continue
            if result.audit.outcome == "verification_failed":
                self._gaps.update_recheck_job(started.model_copy(update={
                    "status": "failed", "last_error": "verification failed",
                    "updated_at": _utc(now or datetime.now(timezone.utc)),
                }))
                continue
            self._gaps.update_recheck_job(started.model_copy(update={"status": "completed", "updated_at": _utc(now or datetime.now(timezone.utc))}))
            results.append(result)
        return tuple(results)


def _same_project(left: ProjectRef, right: ProjectRef) -> bool:
    return (
        left.tenant_id == right.tenant_id and left.project_id == right.project_id
        and (right.service is None or left.service == right.service)
        and (right.environment is None or left.environment == right.environment)
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
