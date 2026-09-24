"""Validate, deduplicate, and persist authorized knowledge-gap observations."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from uuid import UUID

from project_lens.application.knowledge_gap_store import KnowledgeGapStore
from project_lens.domain.knowledge_gap import (
    ALLOWED_GAP_TRANSITIONS,
    GapTriggerType,
    KnowledgeGapCandidate,
    KnowledgeGapCase,
    KnowledgeGapObservation,
    KnowledgeGapStateEvent,
    GapActionStatus, KnowledgeGapAction,
    KnowledgeGapStatus,
    normalize_key_text,
    stable_knowledge_gap_key,
)
from project_lens.domain.models import ProjectRef
from project_lens.domain.models import KnowledgeGapReport, KnowledgeGapType


class KnowledgeGapService:
    def __init__(self, store: KnowledgeGapStore) -> None:
        self._store = store

    def observe(
        self,
        candidate: KnowledgeGapCandidate,
        *,
        authorized_project: ProjectRef,
        authorized_evidence_ids: frozenset[UUID],
        visibility_scope: str,
        created_by: str,
        trigger_type: GapTriggerType = GapTriggerType.ANSWER,
        run_id: str | None = None,
        now: datetime | None = None,
    ) -> KnowledgeGapCase | None:
        _require_project(candidate.project, authorized_project)
        if candidate.type.value in {"access_limited", "unclear_question"}:
            return None
        if not candidate.evidence_ids:
            raise ValueError("knowledge-gap candidates require authorized evidence")
        if any(item not in authorized_evidence_ids for item in candidate.evidence_ids):
            raise ValueError("candidate references evidence outside the authorized set")
        observed_at = _utc(now or datetime.now(timezone.utc))
        key = stable_knowledge_gap_key(candidate)
        current = self._store.get_by_key(authorized_project, key)
        if current is None:
            case = KnowledgeGapCase(
                project=authorized_project,
                canonical_key=key,
                type=candidate.type,
                title=candidate.title,
                question=candidate.question,
                description=candidate.description,
                fact_type=candidate.fact_type,
                severity=candidate.severity,
                impact=candidate.impact,
                target_ref=candidate.target_ref,
                evidence_ids=tuple(dict.fromkeys(candidate.evidence_ids)),
                conflicting_source_ids=tuple(dict.fromkeys(candidate.conflicting_source_ids)),
                suggested_source_type=candidate.suggested_source_type,
                suggested_owner_ids=tuple(dict.fromkeys(candidate.suggested_owner_ids)),
                first_detected_at=observed_at,
                last_seen_at=observed_at,
                created_at=observed_at,
                updated_at=observed_at,
            )
        else:
            # An observation is never allowed to lower a case's recorded severity.
            case = current.model_copy(update={
                "last_seen_at": observed_at,
                "updated_at": observed_at,
                "occurrence_count": current.occurrence_count + 1,
                "affected_question_count": current.affected_question_count + (
                    0 if normalize_key_text(current.question) == normalize_key_text(candidate.question) else 1
                ),
                "severity": _max_severity(current.severity, candidate.severity),
                "evidence_ids": tuple(dict.fromkeys((*current.evidence_ids, *candidate.evidence_ids))),
                "conflicting_source_ids": tuple(dict.fromkeys((*current.conflicting_source_ids, *candidate.conflicting_source_ids))),
                "suggested_owner_ids": tuple(dict.fromkeys((*current.suggested_owner_ids, *candidate.suggested_owner_ids))),
                "status": KnowledgeGapStatus.REOPENED if current.status == KnowledgeGapStatus.RESOLVED else current.status,
                "resolved_at": None if current.status == KnowledgeGapStatus.RESOLVED else current.resolved_at,
            })
        observation = KnowledgeGapObservation(
            gap_id=case.id,
            run_id=run_id,
            project=authorized_project,
            observed_at=observed_at,
            trigger_type=trigger_type,
            question_hash=sha256(normalize_key_text(candidate.question).encode("utf-8")).hexdigest(),
            evidence_ids=tuple(dict.fromkeys(candidate.evidence_ids)),
            signal=candidate.type.value,
            visibility_scope=visibility_scope,
            created_by=created_by,
        )
        self._store.record(case, observation)
        return case

    def observe_report(
        self,
        report: KnowledgeGapReport,
        *,
        authorized_project: ProjectRef,
        authorized_evidence_ids: frozenset[UUID],
        visibility_scope: str,
        created_by: str = "rule",
        trigger_type: GapTriggerType = GapTriggerType.SCHEDULED_SCAN,
        now: datetime | None = None,
    ) -> tuple[KnowledgeGapCase, ...]:
        """Persist only report gaps anchored to evidence in the caller's authorized set."""

        _require_project(report.project, authorized_project)
        result: list[KnowledgeGapCase] = []
        for gap in report.gaps:
            evidence_ids = tuple(item for item in gap.evidence_ids if item in authorized_evidence_ids)
            if not evidence_ids:
                continue
            candidate = KnowledgeGapCandidate(
                project=authorized_project,
                type=_case_type_for_report_gap(gap.type),
                title=gap.title,
                question=gap.title,
                description=gap.description,
                fact_type=gap.type.value,
                impact=gap.recommendation,
                target_ref=gap.target_ref or "",
                severity=_severity_from_report(gap.severity.value),
                evidence_ids=evidence_ids,
                suggested_owner_ids=gap.suggested_owners,
            )
            case = self.observe(
                candidate,
                authorized_project=authorized_project,
                authorized_evidence_ids=authorized_evidence_ids,
                visibility_scope=visibility_scope,
                created_by=created_by,
                trigger_type=trigger_type,
                now=now,
            )
            if case is not None:
                result.append(case)
        return tuple(result)

    def transition(
        self,
        gap_id: UUID,
        status: KnowledgeGapStatus,
        *,
        actor_id: str,
        reason: str,
        now: datetime | None = None,
    ) -> KnowledgeGapCase:
        current = self._store.get(gap_id)
        if current is None:
            raise KeyError(f"knowledge gap not found: {gap_id}")
        if status == current.status:
            return current
        if status not in ALLOWED_GAP_TRANSITIONS[current.status]:
            raise ValueError(f"invalid knowledge-gap transition: {current.status.value} -> {status.value}")
        if not reason.strip():
            raise ValueError("state transitions require a reason")
        now = _utc(now or datetime.now(timezone.utc))
        updates = {"status": status, "updated_at": now}
        if status == KnowledgeGapStatus.RESOLVED:
            updates.update({"resolved_at": now, "resolved_by": actor_id, "resolution_note": reason})
        elif current.status == KnowledgeGapStatus.RESOLVED:
            updates.update({"resolved_at": None, "resolved_by": None})
        if status == KnowledgeGapStatus.DISMISSED:
            updates["dismissed_reason"] = reason
        updated = current.model_copy(update=updates)
        self._store.save_case(updated)
        self._store.append_state_event(KnowledgeGapStateEvent(
            gap_id=gap_id, from_status=current.status, to_status=status,
            actor_id=actor_id, occurred_at=now, reason=reason,
        ))
        return updated

    def propose_action(self, gap_id: UUID, *, action_type, proposed_by: str, description: str, target_owner_id: str | None = None, linked_source_ids: tuple[UUID, ...] = (), linked_task_id: str | None = None, now: datetime | None = None) -> KnowledgeGapAction:
        if self._store.get(gap_id) is None:
            raise KeyError(f"knowledge gap not found: {gap_id}")
        action = KnowledgeGapAction(gap_id=gap_id, action_type=action_type, proposed_by=proposed_by, target_owner_id=target_owner_id, description=description, linked_source_ids=linked_source_ids, linked_task_id=linked_task_id, created_at=_utc(now or datetime.now(timezone.utc)))
        self._store.save_action(action)
        return action

    def update_action(self, action_id: UUID, status: GapActionStatus, *, now: datetime | None = None) -> KnowledgeGapAction:
        action = self._store.get_action(action_id)
        if action is None:
            raise KeyError(f"knowledge gap action not found: {action_id}")
        updated = action.model_copy(update={"status": status, "completed_at": _utc(now or datetime.now(timezone.utc)) if status == GapActionStatus.COMPLETED else action.completed_at})
        self._store.save_action(updated)
        return updated


def _require_project(candidate: ProjectRef, authorized: ProjectRef) -> None:
    if candidate.tenant_id != authorized.tenant_id or candidate.project_id != authorized.project_id:
        raise ValueError("candidate project does not match the authorized project")
    if authorized.service is not None and candidate.service not in {None, authorized.service}:
        raise ValueError("candidate service is outside the authorized project scope")
    if authorized.environment is not None and candidate.environment not in {None, authorized.environment}:
        raise ValueError("candidate environment is outside the authorized project scope")


def _max_severity(left, right):
    order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    return left if order[left.value] >= order[right.value] else right


def _severity_from_report(value: str):
    from project_lens.domain.knowledge_gap import GapSeverity

    return GapSeverity(value) if value in {item.value for item in GapSeverity} else GapSeverity.MEDIUM


def _case_type_for_report_gap(value: KnowledgeGapType):
    from project_lens.domain.knowledge_gap import KnowledgeGapCaseType

    mapping = {
        KnowledgeGapType.OWNER: KnowledgeGapCaseType.MISSING_OWNER,
        KnowledgeGapType.DECISION_RECORD: KnowledgeGapCaseType.MISSING_DECISION,
        KnowledgeGapType.TASK_TRACKING: KnowledgeGapCaseType.MISSING_SCHEDULE,
        KnowledgeGapType.ACCESS_LIMITED: KnowledgeGapCaseType.ACCESS_LIMITED,
    }
    return mapping.get(value, KnowledgeGapCaseType.MISSING_SOURCE)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
