"""S07: session-level date window coarse filter."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from project_lens.application.conversation_service import ConversationService
from project_lens.domain.conversation import (
    CitationEntry,
    CitationSourceKind,
    DateWindow,
    format_date_window_label,
)
from project_lens.domain.models import ProjectRef
from project_lens.integrations.feishu.cards import (
    render_context_date_window_card,
    render_context_preview_card,
)
from project_lens.integrations.feishu.hermes_context import build_hermes_project_context
from project_lens.integrations.feishu.service import _parse_feishu_date_option


def _project() -> ProjectRef:
    return ProjectRef(
        tenant_id="demo",
        project_id="payment",
        service="order-service",
        environment="production",
    )


def _seed_turns(
    service: ConversationService,
    *,
    count: int = 3,
    chat_id: str = "chat-s07",
    base: datetime | None = None,
) -> object:
    session = service.get_or_create(
        tenant_id="demo",
        chat_id=chat_id,
        user_id="user-1",
        project=_project(),
    )
    stamp = base or datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    for index in range(count):
        # Pin created_at by recording then rewriting via model_copy on store.
        session = service.record_turn(
            session,
            user_id="user-1",
            text=f"turn-{index}",
            rewritten_question=None,
            run_id=uuid4(),
        )
        turns = list(session.recent_turns)
        turns[-1] = turns[-1].model_copy(
            update={"created_at": stamp + timedelta(days=index)}
        )
        session = session.model_copy(update={"recent_turns": tuple(turns)})
        session = service.store.upsert(session)
    return session


def test_set_window_filters_preview_and_send() -> None:
    service = ConversationService()
    session = _seed_turns(service, count=3, chat_id="chat-filter")
    # turns at 9/20, 9/21, 9/22
    session = service.refresh_pending_send(session, question="q")
    assert len(session.pending_send.items) == 3

    session = service.set_date_window(
        session,
        start=datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc),
        end=None,
    )
    assert session.date_window is not None
    labels = [item.label for item in session.pending_send.items]
    assert all("turn-0" not in label for label in labels)
    assert any("turn-1" in label for label in labels)
    assert any("turn-2" in label for label in labels)

    context = build_hermes_project_context(session=session, use_pending_send=True)
    assert "turn-0" not in context.text
    assert "turn-1" in context.text


def test_clear_window_restores_whole_pool() -> None:
    service = ConversationService()
    session = _seed_turns(service, count=3, chat_id="chat-clear")
    session = service.refresh_pending_send(session, question="q")
    session = service.set_date_window(
        session,
        start=datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc),
    )
    assert len(session.pending_send.items) == 1

    session = service.clear_date_window(session)
    assert session.date_window is None
    assert len(session.pending_send.items) == 3
    assert "整池" in format_date_window_label(None)


def test_fork_lines_have_independent_date_windows() -> None:
    service = ConversationService()
    parent = _seed_turns(service, count=2, chat_id="chat-fork-window")
    parent = service.refresh_pending_send(parent, question="q")
    parent = service.set_date_window(
        parent,
        start=datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc),
        end=datetime(2026, 9, 21, 23, 59, tzinfo=timezone.utc),
    )
    child = service.fork_session(parent)
    assert child.date_window is not None
    assert child.date_window.start == parent.date_window.start

    child = service.set_date_window(
        child,
        start=datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc),
        end=None,
    )
    parent_reloaded = service.store.get(parent.session_id)
    assert parent_reloaded is not None
    assert parent_reloaded.date_window is not None
    assert parent_reloaded.date_window.end is not None
    assert child.date_window.end is None
    assert child.date_window.start != parent_reloaded.date_window.start or (
        parent_reloaded.date_window.end is not None
    )


def test_outside_citation_dropped_from_pending_on_window() -> None:
    service = ConversationService()
    session = _seed_turns(service, count=1, chat_id="chat-cite-window")
    old = CitationEntry(
        source_kind=CitationSourceKind.HISTORY,
        occurred_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        short_title="窗外引用",
        body_snapshot="old body",
    )
    session = service.append_citation(session, old)
    session = service.refresh_pending_send(
        session, question="q", selected_citation_ids=(old.citation_id,)
    )
    assert any(item.citation_id == old.citation_id for item in session.pending_send.items)

    session = service.set_date_window(
        session,
        start=datetime(2026, 9, 19, 0, 0, tzinfo=timezone.utc),
    )
    assert all(item.citation_id != old.citation_id for item in session.pending_send.items)


def test_active_date_window_for_search_default() -> None:
    service = ConversationService()
    session = _seed_turns(service, count=1, chat_id="chat-search-window")
    window = DateWindow(
        start=datetime(2026, 9, 10, tzinfo=timezone.utc),
        end=datetime(2026, 9, 15, tzinfo=timezone.utc),
    )
    session = service.set_date_window(
        session, start=window.start, end=window.end, refresh_pending=False
    )
    resolved = service.active_date_window(
        tenant_id="demo",
        chat_id="chat-search-window",
        user_id="user-1",
        project=_project(),
    )
    assert resolved is not None
    assert resolved.start == window.start
    assert resolved.end == window.end


def test_date_window_cards_expose_actions() -> None:
    sid = uuid4()
    preview = render_context_preview_card(
        question="q",
        session_id=sid,
        items=(),
        token_estimate=0,
        date_window_label="2026-09-20 → …",
    )
    payload = str(preview)
    assert "设置日期窗" in payload
    assert "清空日期窗" in payload
    assert "context_open_date_window" in payload
    assert "日期窗" in payload

    card = render_context_date_window_card(
        question="q",
        session_id=sid,
        date_window_label="未开窗（整池默认带入，不做剔除非今日）",
    )
    body = str(card)
    assert "context_set_date_start" in body
    assert "context_set_date_end" in body
    assert "date_picker" in body
    assert "确定" in body
    assert "context_confirm_date_window" in body


def test_parse_feishu_date_option() -> None:
    start = _parse_feishu_date_option("2026-09-20 +0800", end=False)
    end = _parse_feishu_date_option("2026-09-21", end=True)
    assert start is not None
    assert end is not None
    assert start.hour == 0
    assert end.hour == 23
    assert _parse_feishu_date_option("", end=False) is None
