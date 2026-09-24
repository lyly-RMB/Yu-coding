"""Structured session state and handoff probes for Phase 3."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from project_lens.application.conversation_service import ConversationService
from project_lens.domain.models import (
    Claim,
    ClaimType,
    Evidence,
    EvidenceGrade,
    EvidenceType,
    ProjectAnswer,
    ProjectRef,
    SourceRef,
)
from project_lens.integrations.feishu.hermes_context import build_hermes_project_context


def _project() -> ProjectRef:
    return ProjectRef(tenant_id="demo", project_id="payment", service="orders")


def _answer() -> ProjectAnswer:
    evidence = Evidence(
        type=EvidenceType.CODE,
        project=_project(),
        source=SourceRef(system="git", source_id="src/orders.py"),
        content="owner=ada",
        observed_at=datetime.now(timezone.utc),
        access_scope="project:payment:read",
        content_hash="sha256:abcdef0123456789",
        metadata={"revision": "commit-1", "authority": "approved"},
    )
    return ProjectAnswer(
        project=_project(),
        skill="project_knowledge",
        confidence=0.9,
        status="confirmed",
        business_summary="订单服务负责人已确认",
        technical_summary="owner=ada",
        claims=(
            Claim(
                text="负责人是 Ada",
                type=ClaimType.FACT,
                evidence_ids=(evidence.id,),
                grade=EvidenceGrade.A,
            ),
        ),
        evidence=(evidence,),
        unknowns=("最近变更时间尚未确认",),
    )


def test_structured_state_retains_facts_evidence_and_version() -> None:
    service = ConversationService(recent_turn_limit=2)
    session = service.get_or_create(
        tenant_id="demo", chat_id="chat-1", user_id="u1", project=_project()
    )
    session = service.record_turn(
        session,
        user_id="u1",
        text="负责人是谁？",
        rewritten_question=None,
        run_id=uuid4(),
        answer=_answer(),
    )
    state = session.session_state
    assert state is not None
    assert state.summary_version >= 1
    assert state.confirmed_facts[0].text == "负责人是 Ada"
    assert state.evidence_refs[0].source_key == "git:src/orders.py"
    assert state.evidence_refs[0].revision == "commit-1"
    assert "## Confirmed Facts" in state.summary_markdown
    assert "最近变更时间尚未确认" in state.summary_markdown


def test_incremental_compression_marks_only_newly_evicted_turn() -> None:
    service = ConversationService(recent_turn_limit=2)
    session = service.get_or_create(
        tenant_id="demo", chat_id="chat-1", user_id="u1", project=_project()
    )
    first_run = uuid4()
    session = service.record_turn(
        session,
        user_id="u1",
        text="first",
        rewritten_question=None,
        run_id=first_run,
    )
    session = service.record_turn(
        session,
        user_id="u1",
        text="second",
        rewritten_question=None,
        run_id=uuid4(),
    )
    session = service.record_turn(
        session,
        user_id="u1",
        text="third",
        rewritten_question=None,
        run_id=uuid4(),
    )
    assert session.session_state is not None
    assert session.session_state.last_compressed_turn_id == str(first_run)
    assert "first" in session.session_state.summary_markdown


def test_handoff_is_bounded_and_updates_marker() -> None:
    service = ConversationService()
    session = service.get_or_create(
        tenant_id="demo", chat_id="chat-1", user_id="u1", project=_project()
    )
    session = service.record_turn(
        session,
        user_id="u1",
        text="需要继续调查",
        rewritten_question=None,
        run_id=uuid4(),
        answer=_answer(),
    )
    handoff, saved = service.build_handoff(session)
    assert handoff.handoff_id
    assert len(handoff.markdown) <= 20_000
    assert saved.session_state is not None
    assert saved.session_state.last_handoff_id == handoff.handoff_id
    assert saved.session_state.rotation_pending is False


def test_rotation_marker_is_persisted_until_handoff_is_built() -> None:
    service = ConversationService()
    session = service.get_or_create(
        tenant_id="demo", chat_id="chat-1", user_id="u1", project=_project()
    )
    pending = service.mark_rotation_pending(session, pending=True)
    assert pending.session_state is not None
    assert pending.session_state.rotation_pending is True
    assert pending.session_state.rotation_count == 1

    handoff, saved = service.build_handoff(pending)
    assert handoff.summary_version >= pending.session_state.summary_version
    assert saved.session_state is not None
    assert saved.session_state.rotation_pending is False
    assert saved.session_state.last_handoff_id == handoff.handoff_id


def test_hermes_context_exposes_state_version_without_raw_transcript() -> None:
    service = ConversationService()
    session = service.get_or_create(
        tenant_id="demo", chat_id="chat-1", user_id="u1", project=_project()
    )
    session = service.record_turn(
        session,
        user_id="u1",
        text="确认负责人",
        rewritten_question=None,
        run_id=uuid4(),
        answer=_answer(),
    )
    rendered = build_hermes_project_context(session=session)
    assert "session_summary_version=" in rendered.text
    assert "负责人是 Ada" in rendered.text
    assert rendered.audit_refs["summary_version"] >= 1
