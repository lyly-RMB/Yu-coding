from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from project_lens.application.knowledge_gap_service import KnowledgeGapService
from project_lens.application.knowledge_gap_store import InMemoryKnowledgeGapStore, SQLiteKnowledgeGapStore
from project_lens.application.knowledge_gap_digest import (
    KnowledgeGapDigestService,
    render_knowledge_gap_digest,
    render_owner_knowledge_gap_digest,
)
from project_lens.domain.knowledge_gap import (
    GapActionStatus,
    GapActionType,
    GapSeverity,
    KnowledgeGapCandidate,
    KnowledgeGapCaseType,
    KnowledgeGapStatus,
    stable_knowledge_gap_key,
)
from project_lens.domain.models import ProjectRef
from project_lens.persistence.sqlite import SQLiteDatabase


NOW = datetime(2026, 9, 23, 9, tzinfo=timezone.utc)
PROJECT = ProjectRef(tenant_id="tenant", project_id="payment")


def candidate(*, question: str = "Is member tier rule active?", evidence_id=None, severity=GapSeverity.MEDIUM):
    return KnowledgeGapCandidate(
        project=PROJECT,
        type=KnowledgeGapCaseType.CONFLICTING_SOURCES,
        title="Member tier rule conflict",
        question=question,
        description="Formal sources disagree.",
        fact_type="member_tier_rule",
        target_ref="requirement:REQ-42",
        severity=severity,
        evidence_ids=(evidence_id or uuid4(),),
        conflicting_source_ids=("minutes-1",),
    )


def observe(service, proposed, *, authorized=None, now=NOW):
    return service.observe(
        proposed,
        authorized_project=PROJECT,
        authorized_evidence_ids=frozenset({authorized or proposed.evidence_ids[0]}),
        visibility_scope="project:payment:read",
        created_by="agent",
        now=now,
    )


def test_stable_key_ignores_calendar_date_and_keeps_project_and_target() -> None:
    first = candidate(question="Is member tier rule active on 2026-09-23?")
    second = candidate(question="Is member tier rule active on 2026-09-24?")
    assert stable_knowledge_gap_key(first) == stable_knowledge_gap_key(second)
    assert stable_knowledge_gap_key(first) != stable_knowledge_gap_key(first.model_copy(update={"project": ProjectRef(tenant_id="tenant", project_id="crm")}))


def test_repeated_observations_aggregate_and_never_lower_severity() -> None:
    store = InMemoryKnowledgeGapStore()
    service = KnowledgeGapService(store)
    first = candidate(severity=GapSeverity.HIGH)
    created = observe(service, first)
    second = candidate(question="Is member tier rule active?", severity=GapSeverity.LOW, evidence_id=uuid4())

    updated = observe(service, second, now=NOW.replace(hour=10))

    assert updated.id == created.id
    assert updated.occurrence_count == 2
    assert updated.affected_question_count == 1
    assert updated.severity == GapSeverity.HIGH
    assert len(updated.evidence_ids) == 2
    assert len(store.observations_for_gap(updated.id)) == 2


def test_authorization_is_checked_and_non_governance_candidates_are_not_persisted() -> None:
    store = InMemoryKnowledgeGapStore()
    service = KnowledgeGapService(store)
    proposed = candidate()
    with pytest.raises(ValueError, match="authorized set"):
        service.observe(
            proposed,
            authorized_project=PROJECT,
            authorized_evidence_ids=frozenset(),
            visibility_scope="project:payment:read",
            created_by="agent",
        )
    restricted = proposed.model_copy(update={"type": KnowledgeGapCaseType.ACCESS_LIMITED})
    assert observe(service, restricted) is None
    assert store.list_for_project(PROJECT) == ()


def test_state_machine_requires_check_before_resolution_and_reopens() -> None:
    store = InMemoryKnowledgeGapStore()
    service = KnowledgeGapService(store)
    case = observe(service, candidate())
    assert case is not None
    with pytest.raises(ValueError, match="invalid"):
        service.transition(case.id, KnowledgeGapStatus.RESOLVED, actor_id="owner", reason="done")
    opened = service.transition(case.id, KnowledgeGapStatus.OPEN, actor_id="reviewer", reason="validated candidate", now=NOW)
    acknowledged = service.transition(opened.id, KnowledgeGapStatus.ACKNOWLEDGED, actor_id="owner", reason="seen", now=NOW)
    assigned = service.transition(acknowledged.id, KnowledgeGapStatus.ASSIGNED, actor_id="lead", reason="owner identified", now=NOW)
    pending = service.transition(assigned.id, KnowledgeGapStatus.RESOLVED_PENDING_CHECK, actor_id="owner", reason="source added", now=NOW)
    resolved = service.transition(pending.id, KnowledgeGapStatus.RESOLVED, actor_id="checker", reason="query now succeeds", now=NOW)
    reopened = observe(service, candidate(), now=NOW.replace(hour=11))
    assert resolved.status == KnowledgeGapStatus.RESOLVED
    assert reopened.status == KnowledgeGapStatus.REOPENED
    assert reopened.resolved_at is None


def test_sqlite_roundtrip_and_project_isolation(tmp_path) -> None:
    db = SQLiteDatabase(str(tmp_path / "gaps.db"))
    store = SQLiteKnowledgeGapStore(db)
    service = KnowledgeGapService(store)
    case = observe(service, candidate())
    assert case is not None
    assert store.get(case.id) == case
    assert store.get_by_key(PROJECT, case.canonical_key) == case
    assert store.list_for_project(ProjectRef(tenant_id="tenant", project_id="other")) == ()
    assert len(store.observations_for_gap(case.id)) == 1
    updated = service.transition(case.id, KnowledgeGapStatus.OPEN, actor_id="reviewer", reason="confirmed", now=NOW)
    assert updated.status == KnowledgeGapStatus.OPEN
    assert len(store.observations_for_gap(case.id)) == 1
    assert len(store.events_for_gap(case.id)) == 1


def test_existing_knowledge_gap_report_is_persisted_only_with_authorized_evidence() -> None:
    from project_lens.domain.models import KnowledgeGap, KnowledgeGapReport, KnowledgeGapType

    allowed, forbidden = uuid4(), uuid4()
    report = KnowledgeGapReport(
        project=PROJECT,
        gaps=(
            KnowledgeGap(
                type=KnowledgeGapType.OWNER,
                title="Owner missing",
                description="No owner metadata found.",
                source_signal="missing_owner",
                evidence_ids=(allowed, forbidden),
                target_ref="service:orders",
            ),
        ),
    )
    store = InMemoryKnowledgeGapStore()
    cases = KnowledgeGapService(store).observe_report(
        report,
        authorized_project=PROJECT,
        authorized_evidence_ids=frozenset({allowed}),
        visibility_scope="project:payment:read",
        now=NOW,
    )
    assert len(cases) == 1
    assert cases[0].type == KnowledgeGapCaseType.MISSING_OWNER
    assert cases[0].evidence_ids == (allowed,)

    report_without_authorized_anchor = report.model_copy(update={
        "gaps": (report.gaps[0].model_copy(update={"evidence_ids": (forbidden,)}),)
    })
    assert KnowledgeGapService(InMemoryKnowledgeGapStore()).observe_report(
        report_without_authorized_anchor,
        authorized_project=PROJECT,
        authorized_evidence_ids=frozenset({allowed}),
        visibility_scope="project:payment:read",
    ) == ()


def test_daily_digest_aggregates_lifecycle_and_is_idempotent() -> None:
    store = InMemoryKnowledgeGapStore()
    service = KnowledgeGapService(store)
    daily = KnowledgeGapDigestService(store, store)
    first = observe(service, candidate(severity=GapSeverity.HIGH), now=NOW)
    assert first is not None
    opened = service.transition(first.id, KnowledgeGapStatus.OPEN, actor_id="reviewer", reason="validated", now=NOW)
    other = candidate(question="Will the export pass QA?", evidence_id=uuid4())
    second = observe(service, other, now=NOW.replace(hour=12))
    assert second is not None
    service.transition(second.id, KnowledgeGapStatus.OPEN, actor_id="reviewer", reason="validated", now=NOW)
    acknowledged = service.transition(opened.id, KnowledgeGapStatus.ACKNOWLEDGED, actor_id="owner", reason="seen", now=NOW)
    assigned = service.transition(acknowledged.id, KnowledgeGapStatus.ASSIGNED, actor_id="lead", reason="owner identified", now=NOW)
    pending = service.transition(assigned.id, KnowledgeGapStatus.RESOLVED_PENDING_CHECK, actor_id="owner", reason="source added", now=NOW)
    service.transition(pending.id, KnowledgeGapStatus.RESOLVED, actor_id="checker", reason="verified", now=NOW.replace(hour=14))

    digest = daily.build(PROJECT, period_start=NOW, period_end=NOW.replace(day=24), now=NOW.replace(hour=17))
    again = daily.build(PROJECT, period_start=NOW, period_end=NOW.replace(day=24), now=NOW.replace(hour=18))

    assert digest.summary_hash == again.summary_hash
    assert len(digest.new_cases) == 2
    assert len(digest.ongoing_cases) == 1
    assert len(digest.high_impact_cases) == 0
    assert len(digest.resolved_cases) == 1
    assert store.get_digest_run(f"tenant:payment:{NOW.isoformat()}:{NOW.replace(day=24).isoformat()}:project") is not None
    card = render_knowledge_gap_digest(digest)
    assert "每日知识缺口摘要" in str(card)
    assert "持续未解决：1" in str(card)


def test_daily_digest_same_period_updates_summary_hash_without_duplicate_run() -> None:
    store = InMemoryKnowledgeGapStore()
    service = KnowledgeGapService(store)
    daily = KnowledgeGapDigestService(store, store)
    start, end = NOW, NOW.replace(day=24)
    case = observe(service, candidate(), now=NOW)
    assert case is not None
    case = service.transition(case.id, KnowledgeGapStatus.OPEN, actor_id="reviewer", reason="confirmed", now=NOW)
    daily.build(PROJECT, period_start=start, period_end=end, now=NOW)
    changed = case.model_copy(update={"title": "Changed summary content"})
    store.save_case(changed)
    changed_digest = daily.build(PROJECT, period_start=start, period_end=end, now=NOW)
    run = store.get_digest_run(
        f"tenant:payment:{start.isoformat()}:{end.isoformat()}:project"
    )
    assert changed_digest.summary_hash == run.summary_hash
    assert changed_digest.summary_hash != ""
    assert run.status == "generated"
    assert len(store._digest_runs) == 1


def test_digest_delivery_state_refreshes_only_when_content_changes() -> None:
    store = InMemoryKnowledgeGapStore()
    service = KnowledgeGapService(store)
    daily = KnowledgeGapDigestService(store, store)
    start, end = NOW, NOW.replace(day=24)
    case = observe(service, candidate(), now=NOW)
    assert case is not None
    case = service.transition(case.id, KnowledgeGapStatus.OPEN, actor_id="reviewer", reason="confirmed", now=NOW)
    digest = daily.build(PROJECT, period_start=start, period_end=end, now=NOW)
    sent = daily.mark_delivery(digest, recipient_ids=("owner",), status="sent", now=NOW)
    same = daily.build(PROJECT, period_start=start, period_end=end, now=NOW)
    same_run = store.get_digest_run(sent.digest_key)
    assert same.summary_hash == digest.summary_hash
    assert same_run.id == sent.id
    assert same_run.status == "sent"
    assert same_run.recipient_ids == ("owner",)

    store.save_case(case.model_copy(update={"title": "Updated title"}))
    changed = daily.build(PROJECT, period_start=start, period_end=end, now=NOW)
    changed_run = store.get_digest_run(sent.digest_key)
    assert changed.summary_hash != digest.summary_hash
    assert changed_run.id == sent.id
    assert changed_run.status == "generated"
    assert changed_run.recipient_ids == ()
    assert changed_run.sent_at is None
    with pytest.raises(ValueError, match="content changed"):
        daily.mark_delivery(digest, recipient_ids=("stale",), status="sent", now=NOW)


def test_digest_hides_cases_with_any_unauthorized_evidence() -> None:
    store = InMemoryKnowledgeGapStore()
    gap_service = KnowledgeGapService(store)
    digest_service = KnowledgeGapDigestService(store, store)
    visible, hidden = uuid4(), uuid4()
    case = observe(gap_service, candidate(evidence_id=visible), now=NOW)
    assert case is not None
    gap_service.transition(case.id, KnowledgeGapStatus.OPEN, actor_id="reviewer", reason="confirmed", now=NOW)
    repeated = candidate(question="Is member tier rule active?", evidence_id=hidden)
    gap_service.observe(
        repeated,
        authorized_project=PROJECT,
        authorized_evidence_ids=frozenset({hidden}),
        visibility_scope="restricted",
        created_by="agent",
        now=NOW,
    )
    digest = digest_service.build(
        PROJECT,
        period_start=NOW,
        period_end=NOW.replace(day=24),
        authorized_evidence_ids=frozenset({visible}),
        now=NOW,
    )
    assert digest.new_cases == ()
    assert digest.ongoing_cases == ()


def test_digest_bundle_scopes_owner_views_to_authorized_owner_ids() -> None:
    store = InMemoryKnowledgeGapStore()
    service = KnowledgeGapService(store)
    daily = KnowledgeGapDigestService(store, store)
    owner_case = candidate().model_copy(update={"suggested_owner_ids": ("qa-owner",)})
    other_case = candidate(question="Will export pass QA?", evidence_id=uuid4()).model_copy(
        update={"suggested_owner_ids": ("product-owner",)}
    )
    first = observe(service, owner_case, now=NOW)
    second = observe(service, other_case, now=NOW)
    assert first is not None and second is not None
    service.transition(first.id, KnowledgeGapStatus.OPEN, actor_id="reviewer", reason="confirmed", now=NOW)
    service.transition(second.id, KnowledgeGapStatus.OPEN, actor_id="reviewer", reason="confirmed", now=NOW)
    bundle = daily.build_bundle(
        PROJECT,
        period_start=NOW,
        period_end=NOW.replace(day=24),
        authorized_evidence_ids=frozenset((*first.evidence_ids, *second.evidence_ids)),
        authorized_owner_ids=frozenset({"qa-owner"}),
        now=NOW,
    )
    assert [section.owner_id for section in bundle.owner_digests] == ["qa-owner"]
    section = bundle.owner_digests[0]
    assert {item.title for item in section.ongoing_cases} == {first.title}
    card = str(render_owner_knowledge_gap_digest(bundle.project_digest, section))
    assert first.title in card
    assert "Will export pass QA?" not in card


def test_sqlite_digest_run_survives_store_recreation(tmp_path) -> None:
    db = SQLiteDatabase(str(tmp_path / "digest.db"))
    store = SQLiteKnowledgeGapStore(db)
    service = KnowledgeGapService(store)
    digest_service = KnowledgeGapDigestService(store, store)
    observe(service, candidate(), now=NOW)
    digest_service.build(PROJECT, period_start=NOW, period_end=NOW.replace(day=24), now=NOW)
    second_store = SQLiteKnowledgeGapStore(db)
    assert second_store.get_digest_run(
        f"tenant:payment:{NOW.isoformat()}:{NOW.replace(day=24).isoformat()}:project"
    ) is not None


def test_digest_notification_claim_is_idempotent_in_memory_and_sqlite(tmp_path) -> None:
    gap_id = uuid4()
    key = f"{gap_id}:owner:knowledge_gap_digest:2026-09-23"
    memory = InMemoryKnowledgeGapStore()
    assert memory.claim_digest_notification(key, gap_id=gap_id, project=PROJECT, recipient_id="owner", event_type="knowledge_gap_digest", period_key="2026-09-23", at=NOW)
    assert not memory.claim_digest_notification(key, gap_id=gap_id, project=PROJECT, recipient_id="owner", event_type="knowledge_gap_digest", period_key="2026-09-23", at=NOW)

    sqlite = SQLiteKnowledgeGapStore(SQLiteDatabase(str(tmp_path / "notifications.db")))
    assert sqlite.claim_digest_notification(key, gap_id=gap_id, project=PROJECT, recipient_id="owner", event_type="knowledge_gap_digest", period_key="2026-09-23", at=NOW)
    assert not sqlite.claim_digest_notification(key, gap_id=gap_id, project=PROJECT, recipient_id="owner", event_type="knowledge_gap_digest", period_key="2026-09-23", at=NOW)


def test_gap_actions_are_persisted_and_can_complete() -> None:
    store = InMemoryKnowledgeGapStore()
    service = KnowledgeGapService(store)
    case = observe(service, candidate())
    assert case is not None
    action = service.propose_action(case.id, action_type=GapActionType.ADD_SOURCE, proposed_by="owner", description="Attach QA acceptance record", target_owner_id="qa-owner", now=NOW)
    assert store.actions_for_gap(case.id) == (action,)
    completed = service.update_action(action.id, GapActionStatus.COMPLETED, now=NOW)
    assert completed.status == GapActionStatus.COMPLETED
    assert completed.completed_at == NOW
