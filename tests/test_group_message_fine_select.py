"""S08: Feishu group history fine-select + body retrieval."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from project_lens.application.conversation_service import ConversationService
from project_lens.application.group_message_gateway import (
    GroupChatHistoryPage,
    GroupChatMessage,
)
from project_lens.domain.conversation import (
    CitationSourceKind,
    PendingItemMark,
    group_messages_to_history_candidates,
)
from project_lens.domain.models import ProjectRef
from project_lens.integrations.feishu.cards import render_context_more_history_card
from project_lens.integrations.feishu.hermes_context import build_hermes_project_context
from project_lens.integrations.feishu.messages_client import FeishuMessagesClient


def _project() -> ProjectRef:
    return ProjectRef(
        tenant_id="demo",
        project_id="payment",
        service="order-service",
        environment="production",
    )


class _FakeGroupHistory:
    def __init__(
        self,
        *,
        page: GroupChatHistoryPage | None = None,
        bodies: dict[str, str] | None = None,
    ) -> None:
        self._page = page or GroupChatHistoryPage(available=True, items=())
        self._bodies = bodies or {}
        self.list_calls: list[dict[str, object]] = []

    def list_chat_history(
        self,
        *,
        chat_id: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        page_size: int = 5,
        page_token: str | None = None,
    ) -> GroupChatHistoryPage:
        self.list_calls.append(
            {
                "chat_id": chat_id,
                "start_time": start_time,
                "end_time": end_time,
                "page_size": page_size,
                "page_token": page_token,
            }
        )
        return self._page

    def get_message_text(self, message_id: str) -> str | None:
        return self._bodies.get(message_id)


class _RecordingTransport:
    def __init__(self, responses: list[tuple[int, dict]]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, object]] = []

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None = None,
    ) -> tuple[int, dict]:
        self.requests.append(
            {"method": method, "url": url, "headers": headers, "body": body}
        )
        if not self._responses:
            return 500, {"code": 1, "msg": "no fixture"}
        return self._responses.pop(0)


def test_list_group_history_passes_date_window_and_maps_candidates() -> None:
    stamp = datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc)
    gateway = _FakeGroupHistory(
        page=GroupChatHistoryPage(
            available=True,
            items=(
                GroupChatMessage(
                    message_id="om_1",
                    occurred_at=stamp,
                    text="群里说支付超时要排查",
                    sender_id="ou_x",
                ),
            ),
            has_more=True,
            next_page_token="tok-2",
        )
    )
    service = ConversationService(group_history=gateway)
    session = service.get_or_create(
        tenant_id="demo",
        chat_id="oc_chat",
        user_id="user-1",
        project=_project(),
    )
    session = service.set_date_window(
        session,
        start=datetime(2026, 9, 21, tzinfo=timezone.utc),
        end=datetime(2026, 9, 23, tzinfo=timezone.utc),
    )
    window = session.date_window
    assert window is not None
    session = service.refresh_pending_send(session, question="超时原因？")

    result = service.list_group_history(session, page_token=None)
    assert result.available is True
    assert len(result.candidates) == 1
    assert result.candidates[0].message_id == "om_1"
    assert result.candidates[0].source_kind == CitationSourceKind.GROUP_MESSAGE.value
    assert result.has_more is True
    assert result.next_page_token == "tok-2"
    assert gateway.list_calls[0]["start_time"] == window.start
    assert gateway.list_calls[0]["end_time"] == window.end


def test_list_group_history_unavailable_keeps_session_side_usable() -> None:
    gateway = _FakeGroupHistory(
        page=GroupChatHistoryPage(
            available=False,
            unavailable_reason="feishu_im_unavailable code=230002 msg=bot not in chat",
        )
    )
    service = ConversationService(group_history=gateway)
    session = service.get_or_create(
        tenant_id="demo",
        chat_id="oc_chat",
        user_id="user-1",
        project=_project(),
    )
    session = service.record_turn(
        session,
        user_id="user-1",
        text="seed",
        rewritten_question=None,
        run_id=uuid4(),
    )
    session = service.refresh_pending_send(session, question="q")
    group = service.list_group_history(session)
    assert group.available is False
    page, total = service.list_history_candidates(session)
    assert total >= 1
    assert len(page) >= 1


def test_fine_select_group_message_lands_in_pending_and_preview() -> None:
    stamp = datetime(2026, 9, 22, 9, 30, tzinfo=timezone.utc)
    service = ConversationService()
    session = service.get_or_create(
        tenant_id="demo",
        chat_id="oc_chat",
        user_id="user-1",
        project=_project(),
    )
    session = service.refresh_pending_send(session, question="结合群聊讨论回答")
    session = service.fine_select_group_message(
        session,
        message_id="om_pay_timeout",
        occurred_at=stamp,
        short_title="支付超时讨论",
        body_text="昨天线上支付超时，怀疑是渠道回调延迟。",
    )
    assert any(
        item.source_kind == CitationSourceKind.GROUP_MESSAGE
        for item in session.citations
    )
    assert session.pending_send is not None
    citation_items = [
        item
        for item in session.pending_send.items
        if item.mark == PendingItemMark.CITATION
    ]
    assert len(citation_items) == 1
    assert citation_items[0].short_title == "支付超时讨论"

    ctx = build_hermes_project_context(session=session, use_pending_send=True)
    assert "支付超时讨论" in ctx.text
    assert "昨天线上支付超时" not in ctx.text  # metadata only in default context


def test_get_citation_body_prefers_snapshot_then_feishu_refetch() -> None:
    gateway = _FakeGroupHistory(
        bodies={"om_refetch": "从飞书现拉的正文"},
    )
    service = ConversationService(group_history=gateway)
    project = _project()
    session = service.get_or_create(
        tenant_id="demo",
        chat_id="oc_chat",
        user_id="user-1",
        project=project,
    )
    session = service.refresh_pending_send(session, question="q")
    session = service.fine_select_group_message(
        session,
        message_id="om_refetch",
        occurred_at=datetime.now(timezone.utc),
        short_title="无快照",
        body_text=None,
    )
    citation_id = session.citations[0].citation_id
    # Clear snapshot to force gateway fetch.
    cleared = session.citations[0].model_copy(update={"body_snapshot": None})
    session = service.store.upsert(
        session.model_copy(update={"citations": (cleared,)})
    )

    payload = service.get_citation_body(
        citation_id=citation_id,
        tenant_id="demo",
        chat_id="oc_chat",
        user_id="user-1",
        project=project,
    )
    assert payload is not None
    assert payload["body_available"] is True
    assert payload["body"] == "从飞书现拉的正文"
    # Snapshot persisted for next call.
    reloaded = service.find_citation(
        citation_id=citation_id,
        tenant_id="demo",
        chat_id="oc_chat",
        user_id="user-1",
        project=project,
    )
    assert reloaded is not None
    assert reloaded.body_snapshot == "从飞书现拉的正文"


def test_more_history_card_shows_group_select_actions() -> None:
    stamp = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
    candidates = group_messages_to_history_candidates(
        (
            GroupChatMessage(
                message_id="om_card",
                occurred_at=stamp,
                text="卡片选用按钮应出现",
            ),
        ),
        session=ConversationService()
        .get_or_create(
            tenant_id="demo",
            chat_id="c",
            user_id="u",
            project=_project(),
        ),
    )
    card = render_context_more_history_card(
        question="q",
        session_id=uuid4(),
        candidates=(),
        token_estimate=1,
        offset=0,
        total=0,
        group_candidates=candidates,
        group_available=True,
        group_has_more=True,
        group_next_page_token="next",
        group_page_stack=("",),
        group_item_offset=5,
    )
    payload = str(card)
    assert "群聊发言（6-6）" in payload
    assert "6. [可选]" in payload
    assert "context_select_group_message" in payload
    assert "om_card" in payload
    assert "群聊下一页" in payload
    assert "群聊上一页" in payload
    assert "群聊首页" not in payload
    assert "S08" not in payload


def test_feishu_messages_client_skips_interactive_cards() -> None:
    class _Token:
        def get(self) -> str:
            return "t"

    stamp_ms = str(int(datetime(2026, 9, 22, tzinfo=timezone.utc).timestamp() * 1000))
    transport = _RecordingTransport(
        [
            (
                200,
                {
                    "code": 0,
                    "data": {
                        "has_more": False,
                        "items": [
                            {
                                "message_id": "om_card",
                                "create_time": stamp_ms,
                                "msg_type": "interactive",
                                "body": {
                                    "content": json.dumps(
                                        {"title": "ProjectLens 选择更多历史"},
                                        ensure_ascii=False,
                                    )
                                },
                                "sender": {"id": "bot"},
                            },
                            {
                                "message_id": "om_text",
                                "create_time": stamp_ms,
                                "msg_type": "text",
                                "body": {"content": '{"text":"真人发言"}'},
                                "sender": {"id": "ou_1"},
                            },
                        ],
                    },
                },
            )
        ]
    )
    client = FeishuMessagesClient(
        token_provider=_Token(),  # type: ignore[arg-type]
        transport=transport,
    )
    page = client.list_chat_history(chat_id="oc_1", page_size=5)
    assert page.available is True
    assert [item.message_id for item in page.items] == ["om_text"]
    assert page.items[0].text == "真人发言"

    class _Token:
        def get(self) -> str:
            return "t"

    transport = _RecordingTransport(
        [
            (
                200,
                {
                    "code": 0,
                    "data": {
                        "has_more": False,
                        "items": [
                            {
                                "message_id": "om_x",
                                "create_time": str(
                                    int(
                                        datetime(2026, 9, 22, tzinfo=timezone.utc).timestamp()
                                        * 1000
                                    )
                                ),
                                "msg_type": "text",
                                "body": {"content": '{"text":"hello group"}'},
                                "sender": {"id": "ou_1"},
                            }
                        ],
                    },
                },
            )
        ]
    )
    client = FeishuMessagesClient(
        token_provider=_Token(),  # type: ignore[arg-type]
        transport=transport,
    )
    page = client.list_chat_history(chat_id="oc_1", page_size=5)
    assert page.available is True
    assert len(page.items) == 1
    assert page.items[0].text == "hello group"
    assert "container_id=oc_1" in str(transport.requests[0]["url"])

    denied = _RecordingTransport(
        [(200, {"code": 230002, "msg": "bot not in chat"})]
    )
    client2 = FeishuMessagesClient(
        token_provider=_Token(),  # type: ignore[arg-type]
        transport=denied,
    )
    bad = client2.list_chat_history(chat_id="oc_1")
    assert bad.available is False
    assert "230002" in (bad.unavailable_reason or "")


def test_preview_equals_send_after_group_fine_select() -> None:
    service = ConversationService()
    session = service.get_or_create(
        tenant_id="demo",
        chat_id="oc_chat",
        user_id="user-1",
        project=_project(),
    )
    session = service.record_turn(
        session,
        user_id="user-1",
        text="先问一轮",
        rewritten_question=None,
        run_id=uuid4(),
    )
    # Align turn time so default item exists.
    turn = session.recent_turns[-1].model_copy(
        update={"created_at": datetime(2026, 9, 20, tzinfo=timezone.utc)}
    )
    session = service.store.upsert(
        session.model_copy(update={"recent_turns": (turn,) + session.recent_turns[:-1]})
    )
    session = service.refresh_pending_send(session, question="再问")
    before_ids = {item.item_id for item in session.pending_send.items}  # type: ignore[union-attr]
    session = service.fine_select_group_message(
        session,
        message_id="om_eq",
        occurred_at=datetime(2026, 9, 21, tzinfo=timezone.utc),
        short_title="群聊补充",
        body_text="补充材料",
    )
    after_ids = {item.item_id for item in session.pending_send.items}  # type: ignore[union-attr]
    assert after_ids - before_ids
    ctx = build_hermes_project_context(session=session, use_pending_send=True)
    assert "群聊补充" in ctx.text
    # Excluding the citation removes it from send fragment.
    citation_item = next(
        item
        for item in session.pending_send.items  # type: ignore[union-attr]
        if item.mark == PendingItemMark.CITATION
    )
    session = service.exclude_from_pending_send(session, (citation_item.item_id,))
    ctx2 = build_hermes_project_context(session=session, use_pending_send=True)
    assert "群聊补充" not in ctx2.text
