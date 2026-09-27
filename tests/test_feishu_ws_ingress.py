"""Unit tests for Feishu WS → local event payload shaping (no live WS)."""

from __future__ import annotations

from types import SimpleNamespace

from project_lens.integrations.feishu.ws_ingress import (
    _card_action_to_payload,
    _message_event_to_payload,
)


def test_message_event_to_payload_shape() -> None:
    data = SimpleNamespace(
        schema="2.0",
        header=SimpleNamespace(
            event_id="evt-1",
            event_type="im.message.receive_v1",
            tenant_key="tenant-a",
        ),
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id="ou_x", user_id="u_x"),
                sender_type="user",
                tenant_key="tenant-a",
            ),
            message=SimpleNamespace(
                message_id="om_1",
                chat_id="oc_1",
                chat_type="group",
                message_type="text",
                content='{"text":"介绍一下这个项目"}',
            ),
        ),
    )
    payload = _message_event_to_payload(data, token="tok")
    assert payload["token"] == "tok"
    assert payload["header"]["tenant_key"] == "tenant-a"
    assert payload["event"]["message"]["chat_id"] == "oc_1"
    assert payload["event"]["sender"]["sender_id"]["open_id"] == "ou_x"


def test_card_action_to_payload_shape() -> None:
    data = SimpleNamespace(
        schema="2.0",
        header=SimpleNamespace(
            event_id="evt-card",
            event_type="card.action.trigger",
            tenant_key="tenant-a",
        ),
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id="ou_x", user_id="u_x", tenant_key="tenant-a"),
            action=SimpleNamespace(
                tag="button",
                value={"action": "context_more_history", "session_id": "s1"},
            ),
            context=SimpleNamespace(open_chat_id="oc_1", open_message_id="om_c"),
            token="card-tok",
        ),
    )
    payload = _card_action_to_payload(data, token="tok")
    assert payload["header"]["event_type"] == "card.action.trigger"
    assert payload["event"]["action"]["value"]["action"] == "context_more_history"
    assert payload["event"]["context"]["open_chat_id"] == "oc_1"


def test_card_action_forwards_date_picker_option() -> None:
    """date_picker callbacks carry the chosen day on action.option (+ timezone)."""

    data = SimpleNamespace(
        schema="2.0",
        header=SimpleNamespace(
            event_id="evt-date",
            event_type="card.action.trigger",
            tenant_key="tenant-a",
        ),
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id="ou_x", user_id="u_x", tenant_key="tenant-a"),
            action=SimpleNamespace(
                tag="date_picker",
                value={"action": "context_set_date_start", "session_id": "s1"},
                option="2026-09-20 +0800",
                timezone="Asia/Shanghai",
            ),
            context=SimpleNamespace(open_chat_id="oc_1", open_message_id="om_c"),
            token="card-tok",
        ),
    )
    payload = _card_action_to_payload(data, token="tok")
    action = payload["event"]["action"]
    assert action["tag"] == "date_picker"
    assert action["option"] == "2026-09-20 +0800"
    assert action["timezone"] == "Asia/Shanghai"
    assert action["value"]["action"] == "context_set_date_start"


def test_card_action_response_from_api_maps_card_and_toast() -> None:
    from project_lens.integrations.feishu.ws_ingress import _card_action_response_from_api

    response = _card_action_response_from_api(
        {
            "status": "accepted",
            "card": {"type": "raw", "data": {"header": {"title": {"content": "x"}}}},
            "toast": {"type": "info", "content": "已勾选去掉"},
        }
    )
    assert response.card is not None
    assert response.card.type == "raw"
    assert response.card.data["header"]["title"]["content"] == "x"
    assert response.toast is not None
    assert response.toast.content == "已勾选去掉"
