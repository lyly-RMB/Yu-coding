"""Citation ledger write / metadata context / body retrieval (S01)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from project_lens.application.conversation_service import ConversationService
from project_lens.domain.conversation import (
    CitationEntry,
    CitationSourceKind,
    format_citation_ledger_for_context,
    make_short_title,
)
from project_lens.domain.models import ProjectRef


def _project() -> ProjectRef:
    return ProjectRef(
        tenant_id="demo",
        project_id="payment",
        service="order-service",
        environment="production",
    )


def test_record_turn_appends_citation_before_compression() -> None:
    service = ConversationService(recent_turn_limit=2)
    project = _project()
    session = service.get_or_create(
        tenant_id="demo",
        chat_id="chat-1",
        user_id="user-1",
        project=project,
    )
    full_question = "请核对昨天关于支付回调超时的讨论结论，不要漏掉关键证据编号。"
    for index in range(3):
        text = full_question if index == 0 else f"follow-up-{index}"
        session = service.record_turn(
            session,
            user_id="user-1",
            text=text,
            rewritten_question=None,
            run_id=uuid4(),
        )

    assert len(session.recent_turns) == 2
    assert len(session.citations) == 3
    first = session.citations[0]
    assert first.source_kind == CitationSourceKind.TURN
    assert first.body_snapshot == full_question
    assert first.short_title == make_short_title(full_question)
    assert first.run_id is not None
    # Overflow removed the first turn from L1, but citation ledger still holds it.
    assert full_question not in {turn.text for turn in session.recent_turns}
    assert any(item.body_snapshot == full_question for item in session.citations)


def test_citation_context_fragment_excludes_body() -> None:
    service = ConversationService()
    project = _project()
    session = service.get_or_create(
        tenant_id="demo",
        chat_id="chat-1",
        user_id="user-1",
        project=project,
    )
    # Longer than CITATION_SHORT_TITLE_MAX so short_title is clipped and body differs.
    secret_body = (
        "SECRET_BODY_MARKER_FULL_TEXT_"
        + ("详细讨论内容请勿直接塞进默认上下文。" * 8)
    )
    session = service.record_turn(
        session,
        user_id="user-1",
        text=secret_body,
        rewritten_question=None,
        run_id=uuid4(),
    )
    fragment = service.citation_context_fragment(session)
    assert "citation_id=" in fragment
    assert "title=" in fragment
    assert "time=" in fragment
    assert secret_body not in fragment
    assert "body_snapshot" not in fragment
    assert "SECRET_BODY_MARKER_FULL_TEXT_" in fragment  # appears only as clipped title prefix
    assert fragment.count("SECRET_BODY_MARKER_FULL_TEXT_") == 1
    assert session.citations[0].body_snapshot == secret_body
    assert len(session.citations[0].short_title) < len(secret_body)
    assert secret_body not in format_citation_ledger_for_context(session.citations)


def test_get_citation_body_returns_snapshot_and_fails_when_missing() -> None:
    service = ConversationService()
    project = _project()
    session = service.get_or_create(
        tenant_id="demo",
        chat_id="chat-1",
        user_id="user-1",
        project=project,
    )
    session = service.record_turn(
        session,
        user_id="user-1",
        text="原始用户问题正文",
        rewritten_question=None,
        run_id=uuid4(),
    )
    citation_id = session.citations[0].citation_id
    payload = service.get_citation_body(
        citation_id=citation_id,
        tenant_id="demo",
        chat_id="chat-1",
        user_id="user-1",
        project=project,
    )
    assert payload is not None
    assert payload["body"] == "原始用户问题正文"
    assert payload["body_available"] is True

    missing = service.get_citation_body(
        citation_id="does-not-exist",
        tenant_id="demo",
        chat_id="chat-1",
        user_id="user-1",
        project=project,
    )
    assert missing is None

    # Wrong binding / user cannot read another session's citation.
    cross = service.get_citation_body(
        citation_id=citation_id,
        tenant_id="demo",
        chat_id="other-chat",
        user_id="user-1",
        project=project,
    )
    assert cross is None


def test_group_message_citation_accepts_message_id_or_body_snapshot() -> None:
    with_message = CitationEntry(
        source_kind=CitationSourceKind.GROUP_MESSAGE,
        occurred_at=datetime.now(timezone.utc),
        short_title="群里关于超时的讨论",
        message_id="om_feishu_msg_123",
    )
    assert with_message.message_id == "om_feishu_msg_123"
    assert with_message.body_snapshot is None

    with_body = CitationEntry(
        source_kind=CitationSourceKind.GROUP_MESSAGE,
        occurred_at=datetime.now(timezone.utc),
        short_title="本地快照发言",
        body_snapshot="这是细选时落盘的群聊正文快照",
    )
    assert with_body.body_snapshot is not None

    with pytest.raises(ValueError, match="body_snapshot|retrieval key"):
        CitationEntry(
            source_kind=CitationSourceKind.GROUP_MESSAGE,
            occurred_at=datetime.now(timezone.utc),
            short_title="既无正文也无外键",
        )

    service = ConversationService()
    project = _project()
    session = service.get_or_create(
        tenant_id="demo",
        chat_id="chat-1",
        user_id="user-1",
        project=project,
    )
    updated = service.append_citation(session, with_message)
    assert len(updated.citations) == 1
    assert updated.citations[0].message_id == "om_feishu_msg_123"
