"""S02: sliding window (8 turns) + compress oldest turns 1–4 + whole-pool default."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from project_lens.application.conversation_service import ConversationService
from project_lens.domain.conversation import (
    COMPRESSED_ANSWER_TOKEN_BUDGET,
    DEFAULT_RECENT_TURN_LIMIT,
    DefaultContextKind,
    clip_answer_and_evidence_to_budget,
    estimate_text_tokens,
)
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


def _project() -> ProjectRef:
    return ProjectRef(
        tenant_id="demo",
        project_id="payment",
        service="order-service",
        environment="production",
    )


def _answer(*, business_summary: str = "结论摘要", evidence_content: str = "证据正文") -> ProjectAnswer:
    evidence = Evidence(
        type=EvidenceType.DOCUMENT,
        project=_project(),
        source=SourceRef(system="local", source_id="fixture"),
        content=evidence_content,
        observed_at=datetime.now(timezone.utc),
        access_scope="project:payment:read",
        content_hash="1234567890abcdefaa",
    )
    claim = Claim(
        text="fact",
        type=ClaimType.FACT,
        evidence_ids=(evidence.id,),
        grade=EvidenceGrade.B,
    )
    return ProjectAnswer(
        project=_project(),
        skill="incident_diagnosis",
        confidence=0.7,
        status="partial",
        business_summary=business_summary,
        technical_summary="tech",
        claims=(claim,),
        evidence=(evidence,),
    )


def test_default_recent_turn_limit_is_eight() -> None:
    assert DEFAULT_RECENT_TURN_LIMIT == 8


def test_ninth_turn_compresses_oldest_turns_1_through_4() -> None:
    service = ConversationService()
    session = service.get_or_create(
        tenant_id="demo",
        chat_id="chat-1",
        user_id="user-1",
        project=_project(),
    )
    for index in range(9):
        session = service.record_turn(
            session,
            user_id="user-1",
            text=f"question-{index}",
            rewritten_question=None,
            run_id=uuid4(),
            answer=_answer(business_summary=f"answer-{index}"),
        )

    assert session.summary.compression_cycle == 1
    assert session.summary.active_topic.get("compressed_turns") == "4"
    assert len(session.recent_turns) == 5
    kept_texts = [turn.text for turn in session.recent_turns]
    assert kept_texts == [
        "question-4",
        "question-5",
        "question-6",
        "question-7",
        "question-8",
    ]
    records = session.summary.compressed_turn_records
    assert len(records) == 4
    assert all(item.compression_cycle == 1 for item in records)
    assert [item.user_question for item in records] == [
        "question-0",
        "question-1",
        "question-2",
        "question-3",
    ]
    for record in records:
        budget_used = estimate_text_tokens(record.answer_summary) + estimate_text_tokens(
            ",".join(record.evidence_ids) if record.evidence_ids else ""
        )
        # User question is excluded from the 300-token budget.
        assert budget_used <= COMPRESSED_ANSWER_TOKEN_BUDGET
        assert record.answer_summary
        assert record.evidence_ids

    # Default pool = one collapsed latest-summary row + recent turns, time-sorted.
    defaults = service.list_default_context_items(session)
    summary_items = [item for item in defaults if item.kind == DefaultContextKind.SUMMARY]
    recent_items = [item for item in defaults if item.kind == DefaultContextKind.RECENT_TURN]
    assert len(summary_items) == 1
    assert len(recent_items) == 5
    assert summary_items[0].label.startswith("[summary] 最近压缩（4轮）")
    assert "question-0" in (summary_items[0].user_question or "")
    assert "question-3" in (summary_items[0].user_question or "")
    # Oldest compressed summary sits at the top under ascending time order.
    assert defaults[0].kind == DefaultContextKind.SUMMARY
    stamps = [item.occurred_at for item in defaults]
    assert stamps == sorted(stamps)


def test_second_compression_keeps_only_latest_summary_batch() -> None:
    """Sliding window: older summary cycles must not stay in the default pool."""

    service = ConversationService()
    session = service.get_or_create(
        tenant_id="demo",
        chat_id="chat-cycle-2",
        user_id="user-1",
        project=_project(),
    )
    # 9 turns → cycle 1 compresses oldest questions 0–3.
    for index in range(9):
        session = service.record_turn(
            session,
            user_id="user-1",
            text=f"question-{index}",
            rewritten_question=None,
            run_id=uuid4(),
            answer=_answer(business_summary=f"answer-{index}"),
        )
    assert session.summary.compression_cycle == 1
    first_batch = {item.user_question for item in session.summary.compressed_turn_records}
    assert first_batch == {"question-0", "question-1", "question-2", "question-3"}

    # Grow past 8 again → cycle 2 replaces the summary batch.
    for index in range(9, 13):
        session = service.record_turn(
            session,
            user_id="user-1",
            text=f"question-{index}",
            rewritten_question=None,
            run_id=uuid4(),
            answer=_answer(business_summary=f"answer-{index}"),
        )
    assert session.summary.compression_cycle == 2
    records = session.summary.compressed_turn_records
    assert len(records) == 4
    assert all(item.compression_cycle == 2 for item in records)
    assert first_batch.isdisjoint({item.user_question for item in records})
    assert {item.user_question for item in records} == {
        "question-4",
        "question-5",
        "question-6",
        "question-7",
    }

    defaults = service.list_default_context_items(session)
    summary_items = [item for item in defaults if item.kind == DefaultContextKind.SUMMARY]
    assert len(summary_items) == 1
    summary_text = summary_items[0].user_question or ""
    for old in first_batch:
        assert old not in summary_text
    for question in {item.user_question for item in records}:
        assert question in summary_text
    assert defaults[0].kind == DefaultContextKind.SUMMARY


def test_answer_evidence_budget_clips_answer_first() -> None:
    huge_answer = "答" * 2_000
    evidence_ids = tuple(f"ev-{index:04d}-{uuid4()}" for index in range(3))
    clipped, kept_ids = clip_answer_and_evidence_to_budget(
        huge_answer,
        evidence_ids,
        budget=COMPRESSED_ANSWER_TOKEN_BUDGET,
    )
    assert kept_ids == evidence_ids
    assert estimate_text_tokens(clipped) + estimate_text_tokens(",".join(kept_ids)) <= (
        COMPRESSED_ANSWER_TOKEN_BUDGET
    )
    assert len(clipped) < len(huge_answer)


def test_cross_day_turns_stay_in_default_pool_without_date_window() -> None:
    service = ConversationService()
    session = service.get_or_create(
        tenant_id="demo",
        chat_id="chat-1",
        user_id="user-1",
        project=_project(),
    )
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    today = datetime.now(timezone.utc)

    session = service.record_turn(
        session,
        user_id="user-1",
        text="昨夜讨论支付超时",
        rewritten_question=None,
        run_id=uuid4(),
        answer=_answer(business_summary="昨夜结论"),
    )
    # Force created_at on the stored turn (record_turn uses utc_now by default).
    old_turn = session.recent_turns[0].model_copy(update={"created_at": yesterday})
    session = session.model_copy(update={"recent_turns": (old_turn,)})
    session = service.store.upsert(session)

    session = service.record_turn(
        session,
        user_id="user-1",
        text="今早继续追问",
        rewritten_question=None,
        run_id=uuid4(),
        answer=_answer(business_summary="今早结论"),
    )
    # Stamp the second turn as today explicitly.
    turns = (
        session.recent_turns[0],
        session.recent_turns[1].model_copy(update={"created_at": today}),
    )
    session = session.model_copy(update={"recent_turns": turns})
    session = service.store.upsert(session)

    items = service.list_default_context_items(session, date_window=None)
    assert any(item.user_question == "昨夜讨论支付超时" for item in items)
    assert any(item.user_question == "今早继续追问" for item in items)
    assert all(item.kind == DefaultContextKind.RECENT_TURN for item in items)
    assert len(items) == 2
    # Sorted ascending by time: yesterday then today.
    assert items[0].occurred_at <= items[1].occurred_at
    assert items[0].user_question == "昨夜讨论支付超时"
    assert items[1].user_question == "今早继续追问"


def test_compressed_turn_still_retrievable_via_citation_tool() -> None:
    service = ConversationService()
    session = service.get_or_create(
        tenant_id="demo",
        chat_id="chat-1",
        user_id="user-1",
        project=_project(),
    )
    target_body = "第1轮用户原文，压缩后仍须可按引用取回。"
    for index in range(9):
        text = target_body if index == 0 else f"question-{index}"
        session = service.record_turn(
            session,
            user_id="user-1",
            text=text,
            rewritten_question=None,
            run_id=uuid4(),
            answer=_answer(business_summary=f"answer-{index}"),
        )

    assert target_body not in {turn.text for turn in session.recent_turns}
    citation = next(item for item in session.citations if item.body_snapshot == target_body)
    payload = service.get_citation_body(
        citation_id=citation.citation_id,
        tenant_id="demo",
        chat_id="chat-1",
        user_id="user-1",
        project=_project(),
    )
    assert payload is not None
    assert payload["body_available"] is True
    assert payload["body"] == target_body
