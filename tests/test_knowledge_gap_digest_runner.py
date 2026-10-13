from datetime import datetime, timezone
from uuid import uuid4

from project_lens.application.knowledge_gap_digest_runner import (
    DigestRecipients,
    KnowledgeGapDigestRunner,
)
from project_lens.application.knowledge_gap_digest import KnowledgeGapDigestService
from project_lens.application.knowledge_gap_service import KnowledgeGapService
from project_lens.application.knowledge_gap_store import InMemoryKnowledgeGapStore
from project_lens.domain.knowledge_gap import GapSeverity, KnowledgeGapCandidate, KnowledgeGapCaseType, KnowledgeGapStatus
from project_lens.domain.models import ProjectRef


PROJECT = ProjectRef(tenant_id="tenant", project_id="payment")
NOW = datetime(2026, 9, 27, 8, tzinfo=timezone.utc)


class Sender:
    def __init__(self):
        self.cards = []

    async def post_card(self, chat_id, card):
        self.cards.append((chat_id, card))

    async def post_user_card(self, open_id, card):
        self.cards.append((open_id, card))


def test_digest_runner_claims_and_deduplicates_delivery():
    store = InMemoryKnowledgeGapStore()
    gaps = KnowledgeGapService(store)
    evidence_id = uuid4()
    case = gaps.observe(
        KnowledgeGapCandidate(
            project=PROJECT,
            type=KnowledgeGapCaseType.MISSING_SOURCE,
            title="Missing runbook",
            question="Where is the runbook?",
            description="No runbook found",
            severity=GapSeverity.HIGH,
            evidence_ids=(evidence_id,),
            suggested_owner_ids=("u1",),
        ),
        authorized_project=PROJECT,
        authorized_evidence_ids=frozenset({evidence_id}),
        visibility_scope="project:read",
        created_by="test",
        now=datetime(2026, 9, 26, 8, tzinfo=timezone.utc),
    )
    gaps.transition(case.id, KnowledgeGapStatus.OPEN, actor_id="reviewer", reason="confirmed", now=NOW)
    digest = KnowledgeGapDigestService(store, store)
    sender = Sender()
    runner = KnowledgeGapDigestRunner(
        service=digest,
        projects=(PROJECT,),
        resolve_context=lambda project, start, end: (frozenset({evidence_id}), frozenset({"u1"}), DigestRecipients("chat-1", {"u1": "ou-1"})),
        sender=sender,
    )
    first = runner.run_once(now=NOW)
    second = runner.run_once(now=NOW)
    assert first[0].sent == 2
    assert second[0].sent == 0
    assert second[0].skipped == 2
    assert len(sender.cards) == 2


def test_digest_runner_dry_run_does_not_send():
    store = InMemoryKnowledgeGapStore()
    runner = KnowledgeGapDigestRunner(
        service=KnowledgeGapDigestService(store, store),
        projects=(PROJECT,),
        resolve_context=lambda project, start, end: (frozenset(), frozenset({"u1"}), DigestRecipients("chat-1", {"u1": "ou-1"})),
        sender=Sender(),
    )
    result = runner.run_once(now=NOW, dry_run=True)
    assert result[0].dry_run is True
    assert result[0].sent == 0
