"""S04: three card entries — direct answer / edit / more history."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from project_lens.application.conversation_service import ConversationService
from project_lens.domain.conversation import (
    CitationEntry,
    CitationSourceKind,
    DateWindow,
    PendingItemMark,
    pending_item_from_default,
)
from project_lens.domain.models import ProjectRef
from project_lens.integrations.feishu.cards import (
    render_context_edit_card,
    render_context_more_history_card,
    render_context_preview_card,
)
from project_lens.integrations.feishu.hermes_context import build_hermes_project_context


def _project() -> ProjectRef:
    return ProjectRef(
        tenant_id="demo",
        project_id="payment",
        service="order-service",
        environment="production",
    )


def _seed_turns(service: ConversationService, *, count: int = 3, chat_id: str = "chat-s04"):
    session = service.get_or_create(
        tenant_id="demo",
        chat_id=chat_id,
        user_id="user-1",
        project=_project(),
    )
    base = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
    for index in range(count):
        session = service.record_turn(
            session,
            user_id="user-1",
            text=f"seed-question-{index}",
            rewritten_question=None,
            run_id=uuid4(),
        )
        turns = list(session.recent_turns)
        turns[-1] = turns[-1].model_copy(update={"created_at": base + timedelta(hours=index)})
        # Keep citation timestamps aligned with turn times for candidate sorting.
        citations = list(session.citations)
        citations[-1] = citations[-1].model_copy(
            update={"occurred_at": base + timedelta(hours=index)}
        )
        session = session.model_copy(
            update={"recent_turns": tuple(turns), "citations": tuple(citations)}
        )
        session = service.store.upsert(session)
    return session


def test_preview_card_wires_real_three_entry_actions() -> None:
    card = render_context_preview_card(
        question="q",
        session_id=uuid4(),
        items=(),
        token_estimate=12,
    )
    payload = str(card)
    assert "context_direct_answer" in payload
    assert "context_edit" in payload
    assert "context_more_history" in payload
    assert "context_edit_placeholder" not in payload
    assert "context_more_history_placeholder" not in payload


def test_edit_exclude_default_then_send_omits_item() -> None:
    service = ConversationService()
    session = _seed_turns(service, count=3)
    session = service.refresh_pending_send(session, question="部署？")
    assert session.pending_send is not None
    victim = session.pending_send.items[1]
    victim_text = victim.user_question or victim.label

    session = service.exclude_from_pending_send(session, (victim.item_id,))
    assert victim.item_id not in {item.item_id for item in session.pending_send.items}

    edit = render_context_edit_card(
        question=session.pending_send.question,
        session_id=session.session_id,
        items=session.pending_send.items,
        token_estimate=session.pending_send.token_estimate,
        restorable_defaults=tuple(
            pending_item_from_default(d)
            for d in service.list_default_context_items(session)
            if pending_item_from_default(d).item_id == victim.item_id
        ),
    )
    edit_body = str(edit)
    assert "恢复·" in edit_body or "已去掉的默认项" in edit_body
    assert "确定" in edit_body
    assert "context_confirm_edit" in edit_body
    assert "去掉·" in edit_body or "context_exclude_item" in edit_body

    context = build_hermes_project_context(session=session, use_pending_send=True)
    assert victim.item_id not in context.audit_refs["pending_item_ids"]
    assert victim_text not in context.text


def test_edit_card_wires_confirm_before_preview() -> None:
    """Edit card must expose 「确定」and staged selection styling."""

    card = render_context_edit_card(
        question="q",
        session_id=uuid4(),
        items=(),
        token_estimate=0,
    )
    body = str(card)
    assert "确定" in body
    assert "context_confirm_edit" in body
    assert "未点确定前不生效" in body


def test_edit_card_shows_summary_and_recent_turn_details() -> None:
    summary = type(
        "Item",
        (),
        {
            "mark": "default",
            "source_kind": "summary",
            "occurred_at": None,
            "label": "最近压缩（3轮）",
            "item_id": "summary:latest",
            "user_question": "发布影响？\n支付超时如何排查？",
            "answer_summary": "涉及支付和订单服务；优先检查连接池。",
        },
    )()
    recent = type(
        "Item",
        (),
        {
            "mark": "default",
            "source_kind": "recent_turn",
            "occurred_at": None,
            "label": "支付超时问题排查",
            "item_id": "turn:recent",
            "user_question": "支付超时可能是什么原因？",
            "answer_summary": "检查连接池、网关和重试策略。",
        },
    )()
    card = render_context_edit_card(
        question="下一步怎么处理？",
        session_id=uuid4(),
        items=(summary, recent),
        token_estimate=42,
    )
    body = str(card)
    assert "会话摘要" in body
    assert "覆盖 2 轮" in body
    assert "最近对话" in body
    assert "用户：支付超时可能是什么原因？" in body
    assert "助手：检查连接池、网关和重试策略。" in body


def test_more_history_card_shows_question_and_answer_summary() -> None:
    candidate = type(
        "Candidate",
        (),
        {
            "occurred_at": None,
            "short_title": "支付超时问题排查",
            "source_kind": "turn",
            "already_in_pending": False,
            "citation_id": "citation-1",
            "user_question": "支付超时可能是什么原因？",
            "answer_summary": "检查连接池、网关和重试策略。",
        },
    )()
    card = render_context_more_history_card(
        question="下一步怎么处理？",
        session_id=uuid4(),
        candidates=(candidate,),
        token_estimate=42,
        offset=0,
        total=1,
    )
    body = str(card)
    assert "用户：支付超时可能是什么原因？" in body
    assert "助手：检查连接池、网关和重试策略。" in body


def test_history_candidates_include_compressed_turn_details() -> None:
    service = ConversationService()
    session = _seed_turns(service, count=1, chat_id="chat-compressed-history")
    turn = session.recent_turns[0]
    record = session.summary.compressed_turn_records[0] if session.summary.compressed_turn_records else None
    if record is None:
        from project_lens.domain.conversation import CompressedTurnRecord

        record = CompressedTurnRecord(
            user_question="历史中的支付超时问题？",
            answer_summary="检查连接池和重试策略。",
            run_id=turn.run_id,
            occurred_at=turn.created_at,
        )
        session = session.model_copy(
            update={
                "recent_turns": (),
                "summary": session.summary.model_copy(
                    update={"compressed_turn_records": (record,)}
                ),
            }
        )
        service.store.upsert(session)
    candidates, total = service.list_history_candidates(session)
    assert total >= 1
    candidate = next(item for item in candidates if item.run_id == turn.run_id)
    assert candidate.user_question == "历史中的支付超时问题？"
    assert candidate.answer_summary == "检查连接池和重试策略。"


def test_edit_card_selected_exclude_uses_danger_style() -> None:
    item = type(
        "Item",
        (),
        {
            "mark": "default",
            "occurred_at": None,
            "label": "seed-question-0",
            "item_id": "turn:1",
        },
    )()
    card = render_context_edit_card(
        question="q",
        session_id=uuid4(),
        items=(item,),
        token_estimate=1,
        staged_exclude_ids=("turn:1",),
    )
    body = str(card)
    assert "已选去掉·" in body
    assert "danger" in body
    assert "将去掉" in body


def test_edit_restore_excluded_default() -> None:
    service = ConversationService()
    session = _seed_turns(service, count=2, chat_id="chat-s04-restore")
    session = service.refresh_pending_send(session, question="q")
    victim = session.pending_send.items[0]  # type: ignore[union-attr]
    session = service.exclude_from_pending_send(session, (victim.item_id,))
    assert len(session.pending_send.items) == 1  # type: ignore[union-attr]
    session = service.restore_to_pending_send(session, (victim.item_id,))
    assert victim.item_id in {item.item_id for item in session.pending_send.items}  # type: ignore[union-attr]
    assert len(session.pending_send.items) == 2  # type: ignore[union-attr]


def test_context_edit_stage_toggle_then_apply() -> None:
    service = ConversationService()
    session = _seed_turns(service, count=3, chat_id="chat-s04-stage")
    session = service.refresh_pending_send(session, question="部署？")
    assert session.pending_send is not None
    victim = session.pending_send.items[1]
    before = {item.item_id for item in session.pending_send.items}

    session = service.toggle_context_edit_exclude(session, victim.item_id)
    assert victim.item_id in service.get_context_edit_stage(session)["exclude_ids"]
    # Pending unchanged until apply.
    assert {item.item_id for item in session.pending_send.items} == before

    session = service.apply_context_edit_stage(session)
    assert victim.item_id not in {item.item_id for item in session.pending_send.items}
    assert service.get_context_edit_stage(session)["exclude_ids"] == ()


def test_more_history_select_lands_in_citation_pending_and_preview() -> None:
    service = ConversationService()
    session = _seed_turns(service, count=2, chat_id="chat-s04-hist")
    extra = CitationEntry(
        source_kind=CitationSourceKind.HISTORY,
        occurred_at=datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc),
        short_title="旧讨论：灰度方案",
        body_snapshot="灰度方案细节……",
    )
    session = service.append_citation(session, extra)
    session = service.refresh_pending_send(session, question="继续灰度")

    page, total = service.list_history_candidates(session, offset=0, page_size=5)
    assert total >= 3  # 2 turn citations + 1 history
    assert any(c.citation_id == extra.citation_id for c in page)

    session = service.fine_select_history(session, extra.citation_id)
    assert session.pending_send is not None
    citation_ids = {
        item.citation_id for item in session.pending_send.items if item.citation_id
    }
    assert extra.citation_id in citation_ids
    assert any(item.mark == PendingItemMark.CITATION for item in session.pending_send.items)

    preview = render_context_preview_card(
        question=session.pending_send.question,
        session_id=session.session_id,
        items=session.pending_send.items,
        token_estimate=session.pending_send.token_estimate,
    )
    assert "灰度方案" in str(preview)
    assert "[引用]" in str(preview) or "引用" in str(preview)

    context = build_hermes_project_context(session=session, use_pending_send=True)
    assert f"citation_id={extra.citation_id}" in context.text


def test_more_history_respects_date_window_when_set() -> None:
    service = ConversationService()
    session = _seed_turns(service, count=2, chat_id="chat-s04-window")
    old = CitationEntry(
        source_kind=CitationSourceKind.HISTORY,
        occurred_at=datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc),
        short_title="窗外旧项",
        body_snapshot="should be filtered",
    )
    session = service.append_citation(session, old)
    session = session.model_copy(
        update={
            "date_window": DateWindow(
                start=datetime(2026, 9, 19, 0, 0, tzinfo=timezone.utc),
                end=None,
            )
        }
    )
    session = service.store.upsert(session)
    session = service.refresh_pending_send(session, question="q")
    page, total = service.list_history_candidates(session)
    assert all(c.citation_id != old.citation_id for c in page)
    assert total == len(session.citations) - 1


def test_more_history_card_pagination_buttons() -> None:
    candidates = []
    for index in range(3):
        candidates.append(
            type(
                "C",
                (),
                {
                    "citation_id": f"cid-{index}",
                    "occurred_at": datetime(2026, 9, 20, tzinfo=timezone.utc),
                    "short_title": f"title-{index}",
                    "source_kind": "history",
                    "already_in_pending": False,
                },
            )()
        )
    card = render_context_more_history_card(
        question="q",
        session_id=uuid4(),
        candidates=tuple(candidates),
        token_estimate=10,
        offset=0,
        total=8,
        page_size=5,
    )
    payload = str(card)
    assert "context_select_history" in payload
    assert "会话下一页" in payload
    assert "群聊发言" in payload
