"""Unit tests for FeishuIngressRouter — entry classification only."""

from __future__ import annotations

from project_lens.domain.conversation import ConversationSession, empty_summary
from project_lens.domain.models import ProjectRef
from project_lens.integrations.feishu.ingress import (
    FeishuIngressKind,
    FeishuIngressRouter,
    strip_project_debug_slash,
)


def _session() -> ConversationSession:
    project = ProjectRef(
        tenant_id="demo",
        project_id="payment",
        service="order-service",
        environment="production",
    )
    return ConversationSession(
        tenant_id=project.tenant_id,
        chat_id="chat-1",
        user_id="u1",
        project=project,
        summary=empty_summary(project),
    )


def _decide(text: str, *, followup_rewrite: str | None = None):
    return FeishuIngressRouter().decide(
        text,
        session=_session(),
        followup_rewrite=followup_rewrite,
    )


def test_bot_meta_ingress() -> None:
    for text in (
        "你用的是什么模型",
        "你是谁",
        "@_user_1 你能做什么",
    ):
        decision = _decide(text)
        assert decision.kind == FeishuIngressKind.BOT_META
        assert decision.entry_mode is None
        assert decision.creates_project_run is False


def test_former_chitchat_enters_project_question_ingress() -> None:
    for text in ("你好", "在吗", "午饭吃什么", "测试 open id"):
        decision = _decide(text)
        assert decision.kind == FeishuIngressKind.PROJECT_QUESTION, text
        assert decision.entry_mode == "natural_project_question", text
        assert decision.creates_project_run is True, text


def test_doc_sync_status_ingress() -> None:
    for text in ("文档同步状态", "同步状态"):
        decision = _decide(text)
        assert decision.kind == FeishuIngressKind.DOC_SYNC_STATUS
        assert decision.entry_mode is None
        assert decision.creates_project_run is False


def test_natural_project_question_ingress() -> None:
    samples = (
        "介绍一下这个项目",
        "项目地图",
        "最近故障",
        "这个订单接口最近谁改过？",
        "支付服务依赖哪些模块？",
        "上线后报错怎么排查？",
        "新人怎么理解这个项目架构？",
        "create_order 为什么会 AttributeError？",
    )
    for text in samples:
        decision = _decide(text)
        assert decision.kind == FeishuIngressKind.PROJECT_QUESTION, text
        assert decision.entry_mode == "natural_project_question", text
        assert decision.creates_project_run is True, text


def test_debug_project_command_ingress() -> None:
    for text in ("/project 介绍一下这个项目", "/project"):
        decision = _decide(text)
        assert decision.kind == FeishuIngressKind.DEBUG_PROJECT_COMMAND, text
        assert decision.entry_mode == "debug_slash", text
        assert decision.creates_project_run is True, text


def test_creates_project_run_only_for_project_paths() -> None:
    assert _decide("介绍一下这个项目").creates_project_run is True
    assert _decide("/project 项目地图").creates_project_run is True
    assert _decide("你好").creates_project_run is True
    assert _decide("你是谁").creates_project_run is False
    assert _decide("同步状态").creates_project_run is False


def test_strip_project_debug_slash() -> None:
    assert strip_project_debug_slash("/project 介绍一下这个项目") == "介绍一下这个项目"
    assert strip_project_debug_slash("/project") == ""
    assert strip_project_debug_slash("介绍一下这个项目") == "介绍一下这个项目"
    assert strip_project_debug_slash("这个订单接口最近谁改过？") == "这个订单接口最近谁改过？"


def test_followup_rewrite_keeps_project_path_over_chitchat_surface() -> None:
    # Ambiguous short text with an active follow-up rewrite stays a project question.
    decision = _decide("继续", followup_rewrite="请继续解释这个项目的架构")
    assert decision.kind == FeishuIngressKind.PROJECT_QUESTION
    assert decision.entry_mode == "natural_project_question"
    assert decision.creates_project_run is True
