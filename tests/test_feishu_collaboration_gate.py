"""Non-project collaboration gate and ask_question card actions."""

from __future__ import annotations

from fastapi.testclient import TestClient

from project_lens.domain.models import ProjectRef
from project_lens.integrations.feishu.cards import (
    render_about_bot_card,
    render_answer_card,
    render_collaboration_gate_card,
)
from project_lens.integrations.feishu.intent import (
    is_bot_meta_question,
    is_project_related_message,
)
from project_lens.main import create_app
from tests.test_feishu_answer_views import _answer, _evidence, _run
from tests.test_feishu_memory_approval import LOCAL_TOKEN, _configure, _message_payload


def test_intent_gate_separates_chitchat_from_project() -> None:
    assert is_project_related_message("你好") is False
    assert is_project_related_message("在吗") is False
    assert is_project_related_message("谢谢") is False
    assert is_project_related_message("午饭吃什么") is False

    assert is_project_related_message("介绍一下这个项目") is True
    assert is_project_related_message("项目地图") is True
    assert is_project_related_message("最近故障") is True
    assert is_project_related_message("帮我看看这个接口") is True
    assert is_project_related_message("create_order 这个函数怎么实现的？") is True
    # Follow-up phrases are not project signals while skill routing is paused.
    assert is_project_related_message("展开技术细节") is False


def test_bot_meta_questions_are_not_project_analysis() -> None:
    assert is_bot_meta_question("你用的是什么模型") is True
    assert is_bot_meta_question("@_user_1 你用的是什么模型") is True
    assert is_bot_meta_question("你是谁") is True
    assert is_bot_meta_question("你能做什么") is True
    assert is_project_related_message("你用的是什么模型") is False
    assert is_project_related_message("@_user_1 你用的是什么模型") is False


def test_about_bot_card_answers_model_without_skill() -> None:
    card = render_about_bot_card(user_text="你用的是什么模型")
    assert card["header"]["title"]["content"] == "ProjectLens 关于我"
    joined = "\n".join(item.get("content", "") for item in card["elements"] if "content" in item)
    assert "provider=" in joined or "表达层" in joined
    assert "当前 Skill" not in joined
    assert "置信度" not in joined
    assert "不会去搜项目 Evidence" in joined or "表达增强" in joined


def test_collaboration_gate_card_has_shortcuts() -> None:
    card = render_collaboration_gate_card(
        user_text="你好",
        project=ProjectRef(
            tenant_id="demo",
            project_id="payment",
            service="order-service",
        ),
    )
    assert card["header"]["title"]["content"] == "ProjectLens 协作入口"
    joined = "\n".join(item.get("content", "") for item in card["elements"] if "content" in item)
    assert "不太像当前项目问题" in joined
    assert "介绍一下这个项目" in joined
    actions = [item for item in card["elements"] if item.get("tag") == "action"]
    assert actions
    labels = [btn["text"]["content"] for btn in actions[0]["actions"]]
    assert "介绍项目" in labels
    assert "项目地图" in labels
    assert all(btn["value"]["action"] == "ask_question" for btn in actions[0]["actions"])


def test_answer_card_includes_follow_up_action_buttons() -> None:
    evidence = (_evidence(),)
    answer = _answer(skill="architecture", evidence=evidence)
    card = render_answer_card(_run("项目地图"), answer)
    actions = [item for item in card["elements"] if item.get("tag") == "action"]
    assert actions
    labels = [
        btn["text"]["content"]
        for group in actions
        for btn in group.get("actions") or []
    ]
    assert "给技术看的版本" in labels
    assert "查看证据" in labels
    assert "给产品/业务看的版本" in labels
    assert "给测试看的版本" in labels


def test_feishu_chitchat_returns_collaboration_gate_without_run() -> None:
    app = create_app()
    _configure(app)
    client = TestClient(app)
    response = client.post(
        "/api/v1/feishu/events",
        json=_message_payload(event_id="collab-hi", text="你好"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert "run_id" not in body
    texts = [
        item
        for item in app.state.feishu_messenger.messages
        if item.message_type == "text"
    ]
    assert texts
    assert "协作入口" in texts[-1].content["text"] or "ProjectLens" in texts[-1].content["text"]


def test_feishu_model_question_returns_about_bot_without_run() -> None:
    app = create_app()
    _configure(app)
    client = TestClient(app)
    response = client.post(
        "/api/v1/feishu/events",
        json=_message_payload(
            event_id="about-model",
            text="@_user_1 你用的是什么模型",
        ),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert "run_id" not in body
    texts = [
        item
        for item in app.state.feishu_messenger.messages
        if item.message_type == "text"
    ]
    assert texts
    card_text = texts[-1].content["text"]
    assert "关于我" in card_text or "ProjectLens" in card_text
    assert "当前 Skill" not in card_text
    assert "收集项目证据" not in card_text
    progress = [
        item
        for item in app.state.feishu_messenger.messages
        if item.message_type == "text"
        and "收集项目证据" in str(item.content.get("text") or "")
    ]
    assert not progress


def test_ask_question_card_action_starts_project_run() -> None:
    app = create_app()
    _configure(app)
    client = TestClient(app)
    # Seed session with a prior project turn so follow-ups keep context.
    first = client.post(
        "/api/v1/feishu/events",
        json=_message_payload(event_id="ask-q-seed", text="介绍一下这个项目"),
    )
    assert first.status_code == 200
    assert "run_id" in first.json()

    payload = {
        "schema": "2.0",
        "header": {
            "event_id": "ask-q-tech",
            "event_type": "card.action.trigger",
            "tenant_key": "demo",
        },
        "event": {
            "operator": {"open_id": "feishu-user-1", "user_id": "feishu-user-1"},
            "action": {
                "tag": "button",
                "value": {
                    "action": "ask_question",
                    "question": "展开技术细节",
                },
            },
            "context": {"open_chat_id": "chat-1"},
        },
        "token": LOCAL_TOKEN,
    }
    response = client.post("/api/v1/feishu/events", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert "run_id" in body
