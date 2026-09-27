"""S06: conversation fork persistence (sibling lines + binding → active)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from project_lens.application.conversation_service import ConversationService
from project_lens.context.conversation_store import (
    InMemoryConversationStore,
    SQLiteConversationStore,
)
from project_lens.domain.conversation import (
    CitationEntry,
    CitationSourceKind,
    ConversationSession,
    ConversationTurn,
    DateWindow,
    empty_summary,
)
from project_lens.domain.models import ProjectRef
from project_lens.integrations.feishu.cards import render_context_preview_card
from project_lens.persistence.sqlite import SQLiteDatabase


def _project() -> ProjectRef:
    return ProjectRef(
        tenant_id="demo",
        project_id="payment",
        service="order-service",
        environment="production",
    )


def _seed(service: ConversationService, *, chat_id: str = "chat-fork") -> ConversationSession:
    session = service.get_or_create(
        tenant_id="demo",
        chat_id=chat_id,
        user_id="user-1",
        project=_project(),
    )
    session = service.record_turn(
        session,
        user_id="user-1",
        text="shared-before-fork",
        rewritten_question=None,
        run_id=uuid4(),
    )
    return service.refresh_pending_send(session, question="继续？")


def test_fork_copies_state_and_stops_parent() -> None:
    service = ConversationService()
    parent = _seed(service)
    assert parent.branch_name == "A"
    assert parent.pending_send is not None
    parent_turn_count = len(parent.recent_turns)
    parent_citation_count = len(parent.citations)

    child = service.fork_session(parent)
    assert child.session_id != parent.session_id
    assert child.parent_session_id == parent.session_id
    assert child.branch_name == "B"
    assert child.write_stopped is False
    assert len(child.recent_turns) == parent_turn_count
    assert len(child.citations) == parent_citation_count
    assert child.pending_send is not None
    assert child.pending_send.question == parent.pending_send.question

    reloaded_parent = service.store.get(parent.session_id)
    assert reloaded_parent is not None
    assert reloaded_parent.write_stopped is True

    active = service.get_or_create(
        tenant_id="demo",
        chat_id="chat-fork",
        user_id="user-1",
        project=_project(),
    )
    assert active.session_id == child.session_id


def test_parent_rejects_writes_after_fork_child_accepts() -> None:
    service = ConversationService()
    parent = _seed(service)
    child = service.fork_session(parent)
    frozen = service.store.get(parent.session_id)
    assert frozen is not None

    with pytest.raises(ValueError, match="write_stopped"):
        service.record_turn(
            frozen,
            user_id="user-1",
            text="should-fail-on-A",
            rewritten_question=None,
            run_id=uuid4(),
        )

    child = service.record_turn(
        child,
        user_id="user-1",
        text="only-on-B",
        rewritten_question=None,
        run_id=uuid4(),
    )
    assert any(turn.text == "only-on-B" for turn in child.recent_turns)
    assert all(turn.text != "only-on-B" for turn in (service.store.get(parent.session_id) or parent).recent_turns)


def test_switch_back_to_a_isolates_turns() -> None:
    service = ConversationService()
    parent = _seed(service, chat_id="chat-switch")
    child = service.fork_session(parent)
    child = service.record_turn(
        child,
        user_id="user-1",
        text="b-only",
        rewritten_question=None,
        run_id=uuid4(),
    )

    resumed = service.switch_session(child, parent.session_id)
    assert resumed.session_id == parent.session_id
    assert resumed.write_stopped is False
    resumed = service.record_turn(
        resumed,
        user_id="user-1",
        text="a-only",
        rewritten_question=None,
        run_id=uuid4(),
    )

    line_a = service.store.get(parent.session_id)
    line_b = service.store.get(child.session_id)
    assert line_a is not None and line_b is not None
    assert any(turn.text == "a-only" for turn in line_a.recent_turns)
    assert all(turn.text != "b-only" for turn in line_a.recent_turns)
    assert any(turn.text == "b-only" for turn in line_b.recent_turns)
    assert all(turn.text != "a-only" for turn in line_b.recent_turns)

    with pytest.raises(ValueError, match="not the active branch"):
        service.record_turn(
            line_b,
            user_id="user-1",
            text="stale-b-card",
            rewritten_question=None,
            run_id=uuid4(),
        )


def test_fork_survives_sqlite_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "fork.db"
    store = SQLiteConversationStore(SQLiteDatabase(str(db_path)))
    service = ConversationService(store=store)
    parent = _seed(service, chat_id="chat-persist-fork")
    # Attach date window + citation so copy is observable after restart.
    window = DateWindow(
        start=datetime(2026, 9, 1, tzinfo=timezone.utc),
        end=datetime(2026, 9, 30, tzinfo=timezone.utc),
    )
    parent = parent.model_copy(update={"date_window": window})
    parent = service.store.upsert(parent)
    parent = service.append_citation(
        parent,
        CitationEntry(
            source_kind=CitationSourceKind.HISTORY,
            occurred_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
            short_title="pinned-history",
            body_snapshot="body for citation",
        ),
    )
    child = service.fork_session(parent)
    child_id = child.session_id
    parent_id = parent.session_id

    # Simulate process restart with a fresh store on the same file.
    store2 = SQLiteConversationStore(SQLiteDatabase(str(db_path)))
    service2 = ConversationService(store=store2)
    active = service2.get_or_create(
        tenant_id="demo",
        chat_id="chat-persist-fork",
        user_id="user-1",
        project=_project(),
    )
    assert active.session_id == child_id
    branches = service2.list_branches(active)
    names = {item.branch_name for item in branches}
    assert names == {"A", "B"}
    assert {item.session_id for item in branches} == {parent_id, child_id}

    line_b = service2.store.get(child_id)
    assert line_b is not None
    assert line_b.date_window is not None
    assert line_b.date_window.start == window.start
    assert any(item.short_title == "pinned-history" for item in line_b.citations)

    resumed = service2.switch_session(active, parent_id)
    assert resumed.session_id == parent_id
    resumed = service2.record_turn(
        resumed,
        user_id="user-1",
        text="after-restart-on-A",
        rewritten_question=None,
        run_id=uuid4(),
    )
    assert any(turn.text == "after-restart-on-A" for turn in resumed.recent_turns)
    line_b_again = service2.store.get(child_id)
    assert line_b_again is not None
    assert all(turn.text != "after-restart-on-A" for turn in line_b_again.recent_turns)


def test_upsert_keeps_sibling_sessions() -> None:
    store = SQLiteConversationStore(SQLiteDatabase(":memory:"))
    project = _project()
    a = ConversationSession(
        tenant_id="demo",
        chat_id="chat-siblings",
        user_id="user-1",
        project=project,
        summary=empty_summary(project),
        branch_name="A",
        recent_turns=(
            ConversationTurn(user_id="user-1", text="on-a", rewritten_question=None),
        ),
        expires_at=datetime.now(timezone.utc) + timedelta(days=3),
    )
    store.upsert(a)
    store.set_active_session(a)
    b = ConversationSession(
        tenant_id="demo",
        chat_id="chat-siblings",
        user_id="user-1",
        project=project,
        summary=empty_summary(project),
        branch_name="B",
        parent_session_id=a.session_id,
        recent_turns=(
            ConversationTurn(user_id="user-1", text="on-b", rewritten_question=None),
        ),
        expires_at=datetime.now(timezone.utc) + timedelta(days=3),
    )
    store.upsert(b)
    store.set_active_session(b)
    # Updating B must not delete A.
    b2 = b.model_copy(
        update={
            "recent_turns": b.recent_turns
            + (ConversationTurn(user_id="user-1", text="on-b-2", rewritten_question=None),)
        }
    )
    store.upsert(b2)
    siblings = store.list_by_binding(
        tenant_id="demo",
        chat_id="chat-siblings",
        user_id="user-1",
        project=project,
    )
    assert {item.branch_name for item in siblings} == {"A", "B"}
    assert store.get(a.session_id) is not None


def test_preview_card_exposes_fork_and_switch_actions() -> None:
    from project_lens.domain.conversation import SessionBranchInfo

    branches = (
        SessionBranchInfo(
            session_id=uuid4(),
            branch_name="A",
            write_stopped=True,
            is_active=False,
        ),
        SessionBranchInfo(
            session_id=uuid4(),
            branch_name="B",
            parent_session_id=None,
            write_stopped=False,
            is_active=True,
        ),
    )
    card = render_context_preview_card(
        question="q",
        session_id=branches[1].session_id,
        items=(),
        token_estimate=1,
        branches=branches,
        active_branch_name="B",
    )
    payload = str(card)
    assert "context_fork_branch" in payload
    assert "context_switch_branch" in payload
    assert "context_delete_branch" in payload
    assert "切换·A" in payload
    assert "新建分支" in payload
    assert "删除分支" in payload


def test_preview_card_hides_delete_when_only_one_branch() -> None:
    from project_lens.domain.conversation import SessionBranchInfo

    only = SessionBranchInfo(
        session_id=uuid4(),
        branch_name="A",
        write_stopped=False,
        is_active=True,
    )
    card = render_context_preview_card(
        question="q",
        session_id=only.session_id,
        items=(),
        token_estimate=1,
        branches=(only,),
        active_branch_name="A",
    )
    payload = str(card)
    assert "context_fork_branch" in payload
    assert "context_delete_branch" not in payload
    assert "删除分支" not in payload


def test_delete_session_removes_branch_and_rebinds() -> None:
    service = ConversationService()
    parent = _seed(service, chat_id="chat-delete")
    child = service.fork_session(parent)
    assert child.branch_name == "B"

    survivor = service.delete_session(child)
    assert survivor.session_id == parent.session_id
    assert survivor.write_stopped is False
    assert service.store.get(child.session_id) is None
    active = service.get_or_create(
        tenant_id="demo",
        chat_id="chat-delete",
        user_id="user-1",
        project=_project(),
    )
    assert active.session_id == parent.session_id

    with pytest.raises(ValueError, match="only remaining"):
        service.delete_session(active)


def test_fork_and_switch_re_render_original_card_not_new_message() -> None:
    """新建/切换分支必须通过回调 card= 原地刷新，不得再 post_card 新消息。"""

    import json

    from fastapi.testclient import TestClient

    from project_lens.integrations.feishu.adapter import RecordingFeishuMessenger
    from project_lens.integrations.feishu.identity import parse_project_bindings
    from project_lens.main import create_app

    local_token = "project-lens-local-token"
    app = create_app()
    verifier = app.state.feishu_event_service._verifier
    verifier._verification_token = local_token
    verifier._signing_secret = None
    app.state.feishu_event_service._identity_mapper = parse_project_bindings(
        "", default_project=_project(), allow_demo_fallback=True
    )
    messenger = RecordingFeishuMessenger()
    app.state.feishu_messenger = messenger
    app.state.feishu_event_service._messenger = messenger
    client = TestClient(app)

    preview = client.post(
        "/api/v1/feishu/events",
        json={
            "schema": "2.0",
            "token": local_token,
            "header": {
                "event_id": "fork-preview",
                "event_type": "im.message.receive_v1",
                "tenant_key": "demo",
            },
            "event": {
                "sender": {"sender_id": {"open_id": "u1"}},
                "message": {
                    "message_id": "message-fork-preview",
                    "chat_id": "chat-1",
                    "chat_type": "group",
                    "message_type": "text",
                    "content": json.dumps({"text": "介绍一下项目"}),
                },
            },
        },
    )
    assert preview.status_code == 200
    cards_after_preview = [
        item for item in messenger.messages if item.message_type == "interactive"
    ]
    assert len(cards_after_preview) == 1

    session = app.state.conversation_store.get_by_binding(
        tenant_id="demo",
        chat_id="chat-1",
        user_id="u1",
        project=_project(),
    )
    assert session is not None
    parent_id = session.session_id

    fork = client.post(
        "/api/v1/feishu/events",
        json={
            "schema": "2.0",
            "token": local_token,
            "header": {
                "event_id": "fork-click",
                "event_type": "card.action.trigger",
                "tenant_key": "demo",
            },
            "event": {
                "operator": {"open_id": "u1", "user_id": "u1"},
                "action": {
                    "tag": "button",
                    "value": {
                        "action": "context_fork_branch",
                        "session_id": str(parent_id),
                    },
                },
                "context": {"open_chat_id": "chat-1", "chat_type": "group"},
            },
        },
    )
    assert fork.status_code == 200
    fork_body = fork.json()
    assert fork_body["status"] == "accepted"
    assert fork_body.get("card", {}).get("type") == "raw"
    assert "新建分支" in str(fork_body["card"]["data"]) or "分支" in str(
        fork_body.get("toast", {})
    )
    assert len([item for item in messenger.messages if item.message_type == "interactive"]) == 1

    child = app.state.conversation_store.get_by_binding(
        tenant_id="demo",
        chat_id="chat-1",
        user_id="u1",
        project=_project(),
    )
    assert child is not None
    assert child.session_id != parent_id

    switch = client.post(
        "/api/v1/feishu/events",
        json={
            "schema": "2.0",
            "token": local_token,
            "header": {
                "event_id": "switch-click",
                "event_type": "card.action.trigger",
                "tenant_key": "demo",
            },
            "event": {
                "operator": {"open_id": "u1", "user_id": "u1"},
                "action": {
                    "tag": "button",
                    "value": {
                        "action": "context_switch_branch",
                        "session_id": str(child.session_id),
                        "target_session_id": str(parent_id),
                    },
                },
                "context": {"open_chat_id": "chat-1", "chat_type": "group"},
            },
        },
    )
    assert switch.status_code == 200
    switch_body = switch.json()
    assert switch_body["status"] == "accepted"
    assert switch_body.get("card", {}).get("type") == "raw"
    assert len([item for item in messenger.messages if item.message_type == "interactive"]) == 1


def test_delete_branch_card_action_re_renders_and_drops_session() -> None:
    """删除分支：回调 card= 原地刷新，目标会话从 store 消失，绑定切到剩余线。"""

    import json

    from fastapi.testclient import TestClient

    from project_lens.integrations.feishu.adapter import RecordingFeishuMessenger
    from project_lens.integrations.feishu.identity import parse_project_bindings
    from project_lens.main import create_app

    local_token = "project-lens-local-token"
    app = create_app()
    verifier = app.state.feishu_event_service._verifier
    verifier._verification_token = local_token
    verifier._signing_secret = None
    app.state.feishu_event_service._identity_mapper = parse_project_bindings(
        "", default_project=_project(), allow_demo_fallback=True
    )
    messenger = RecordingFeishuMessenger()
    app.state.feishu_messenger = messenger
    app.state.feishu_event_service._messenger = messenger
    client = TestClient(app)

    preview = client.post(
        "/api/v1/feishu/events",
        json={
            "schema": "2.0",
            "token": local_token,
            "header": {
                "event_id": "delete-preview",
                "event_type": "im.message.receive_v1",
                "tenant_key": "demo",
            },
            "event": {
                "sender": {"sender_id": {"open_id": "u1"}},
                "message": {
                    "message_id": "message-delete-preview",
                    "chat_id": "chat-1",
                    "chat_type": "group",
                    "message_type": "text",
                    "content": json.dumps({"text": "介绍一下项目"}),
                },
            },
        },
    )
    assert preview.status_code == 200

    parent = app.state.conversation_store.get_by_binding(
        tenant_id="demo",
        chat_id="chat-1",
        user_id="u1",
        project=_project(),
    )
    assert parent is not None
    parent_id = parent.session_id

    fork = client.post(
        "/api/v1/feishu/events",
        json={
            "schema": "2.0",
            "token": local_token,
            "header": {
                "event_id": "delete-fork",
                "event_type": "card.action.trigger",
                "tenant_key": "demo",
            },
            "event": {
                "operator": {"open_id": "u1", "user_id": "u1"},
                "action": {
                    "tag": "button",
                    "value": {
                        "action": "context_fork_branch",
                        "session_id": str(parent_id),
                    },
                },
                "context": {"open_chat_id": "chat-1", "chat_type": "group"},
            },
        },
    )
    assert fork.status_code == 200
    child = app.state.conversation_store.get_by_binding(
        tenant_id="demo",
        chat_id="chat-1",
        user_id="u1",
        project=_project(),
    )
    assert child is not None
    assert child.session_id != parent_id
    child_id = child.session_id

    delete = client.post(
        "/api/v1/feishu/events",
        json={
            "schema": "2.0",
            "token": local_token,
            "header": {
                "event_id": "delete-click",
                "event_type": "card.action.trigger",
                "tenant_key": "demo",
            },
            "event": {
                "operator": {"open_id": "u1", "user_id": "u1"},
                "action": {
                    "tag": "button",
                    "value": {
                        "action": "context_delete_branch",
                        "session_id": str(child_id),
                        "target_session_id": str(child_id),
                    },
                },
                "context": {"open_chat_id": "chat-1", "chat_type": "group"},
            },
        },
    )
    assert delete.status_code == 200
    body = delete.json()
    assert body["status"] == "accepted"
    assert body.get("card", {}).get("type") == "raw"
    assert app.state.conversation_store.get(child_id) is None
    active = app.state.conversation_store.get_by_binding(
        tenant_id="demo",
        chat_id="chat-1",
        user_id="u1",
        project=_project(),
    )
    assert active is not None
    assert active.session_id == parent_id
    assert active.write_stopped is False
    assert len([item for item in messenger.messages if item.message_type == "interactive"]) == 1


def test_inmemory_fork_list_and_active() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store)
    parent = _seed(service, chat_id="chat-mem")
    child = service.fork_session(parent)
    branches = service.list_branches(child)
    assert len(branches) == 2
    assert sum(1 for item in branches if item.is_active) == 1
    active = store.get_by_binding(
        tenant_id="demo",
        chat_id="chat-mem",
        user_id="user-1",
        project=_project(),
    )
    assert active is not None
    assert active.session_id == child.session_id
