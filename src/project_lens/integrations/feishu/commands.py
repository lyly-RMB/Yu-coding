"""Feishu project command shortcuts.

This module only rewrites short Feishu messages into stable project questions.
It does not choose privileged tools, bypass workflow skill routing, or access knowledge stores.
"""

from __future__ import annotations

from enum import StrEnum


class FeishuProjectCommand(StrEnum):
    RESET_CONVERSATION = "reset_conversation"
    CONTEXT_MANAGER = "context_manager"
    PROJECT_INTRO = "project_intro"
    PROJECT_MAP = "project_map"
    RECENT_CHANGES = "recent_changes"
    RECENT_INCIDENTS = "recent_incidents"
    KNOWLEDGE_GAPS = "knowledge_gaps"
    OWNER_LOOKUP = "owner_lookup"
    DOC_SYNC_STATUS = "doc_sync_status"
    FREE_QUESTION = "free_question"


_PROJECT_INTRO_TERMS = (
    "介绍一下这个项目",
    "介绍这个项目",
    "介绍一下项目",
    "这个项目是做什么的",
    "项目是做什么的",
    "这个项目有哪些核心模块",
    "有哪些核心模块",
    "这个项目有哪些服务",
    "这个项目有哪些文档",
    "项目简介",
    "项目介绍",
)
_PROJECT_MAP_TERMS = ("项目地图", "项目架构", "架构地图", "服务地图")
_RECENT_CHANGE_TERMS = ("最近变更", "版本影响", "上线影响", "发布影响")
_RECENT_INCIDENT_TERMS = ("最近故障", "故障复盘", "最近事故", "线上故障")
_KNOWLEDGE_GAP_TERMS = ("知识库缺什么", "知识缺口", "资料缺口", "缺少什么资料")
_OWNER_TERMS = ("负责人是谁", "谁负责", "owner是谁", "owner 是谁")
_DOC_SYNC_STATUS_TERMS = (
    "文档同步状态",
    "知识库最近更新了什么",
    "知识库最近更新",
    "文档最近同步",
    "同步状态",
)
_RESET_CONVERSATION_TERMS = (
    "/reset",
    "/clear",
    "清空对话",
    "清空聊天记录",
    "重开对话",
    "重新开始对话",
)
_CONTEXT_MANAGER_PREFIXES = ("/context", "上下文管理", "打开上下文管理", "管理上下文")


def classify_feishu_command(text: str) -> FeishuProjectCommand:
    """Classify concise Feishu project shortcuts without replacing workflow routing."""
    normalized = _normalize(text)
    if not normalized:
        return FeishuProjectCommand.FREE_QUESTION
    if _matches(normalized, _RESET_CONVERSATION_TERMS):
        return FeishuProjectCommand.RESET_CONVERSATION
    if is_context_manager_command(text):
        return FeishuProjectCommand.CONTEXT_MANAGER
    if _matches(normalized, _DOC_SYNC_STATUS_TERMS):
        return FeishuProjectCommand.DOC_SYNC_STATUS
    if _matches(normalized, _PROJECT_INTRO_TERMS):
        return FeishuProjectCommand.PROJECT_INTRO
    if _matches(normalized, _PROJECT_MAP_TERMS):
        return FeishuProjectCommand.PROJECT_MAP
    if _matches(normalized, _RECENT_CHANGE_TERMS):
        return FeishuProjectCommand.RECENT_CHANGES
    if _matches(normalized, _RECENT_INCIDENT_TERMS):
        return FeishuProjectCommand.RECENT_INCIDENTS
    if _matches(normalized, _KNOWLEDGE_GAP_TERMS):
        return FeishuProjectCommand.KNOWLEDGE_GAPS
    if _matches(normalized, _OWNER_TERMS):
        return FeishuProjectCommand.OWNER_LOOKUP
    return FeishuProjectCommand.FREE_QUESTION


def rewrite_command_to_question(
    command: FeishuProjectCommand,
    text: str,
) -> str:
    """Rewrite a shortcut into a stable read-only project question."""
    original = text.strip()
    if command == FeishuProjectCommand.PROJECT_INTRO:
        return (
            "请介绍一下这个项目：项目定位、核心服务、关键入口、主要文档、"
            "负责人、最近变更、风险和知识缺口。"
        )
    if command == FeishuProjectCommand.PROJECT_MAP:
        return "请解释这个项目的架构、服务、入口、依赖和风险，形成项目地图。"
    if command == FeishuProjectCommand.RECENT_CHANGES:
        return "这个项目最近有哪些版本、发布、变更、commit，以及它们可能影响了什么？"
    if command == FeishuProjectCommand.RECENT_INCIDENTS:
        return "这个项目最近有哪些故障、事故或报错，它们的影响范围和处理建议是什么？"
    if command == FeishuProjectCommand.KNOWLEDGE_GAPS:
        return "项目知识库缺什么资料？请列出缺失证据、未知项和建议补充的团队文档。"
    if command == FeishuProjectCommand.OWNER_LOOKUP:
        return "这个项目的负责人是谁？如果证据不足，请说明缺少哪些负责人资料。"
    if command == FeishuProjectCommand.DOC_SYNC_STATUS:
        return "请展示这个项目的 Feishu 文档同步状态与最近更新的知识库文档。"
    if command == FeishuProjectCommand.RESET_CONVERSATION:
        return ""
    return original


def is_context_manager_command(text: str) -> bool:
    """Whether the user explicitly requested one-shot context editing."""

    normalized = text.strip().casefold()
    return any(
        normalized == prefix.casefold()
        or normalized.startswith(prefix.casefold() + " ")
        or normalized.startswith(prefix.casefold() + ":")
        or normalized.startswith(prefix.casefold() + "\uff1a")
        for prefix in _CONTEXT_MANAGER_PREFIXES
    )


def strip_context_manager_prefix(text: str) -> str:
    """Remove a context-manager command prefix from the actual question."""

    stripped = text.strip()
    lowered = stripped.casefold()
    for prefix in _CONTEXT_MANAGER_PREFIXES:
        marker = prefix.casefold()
        if lowered == marker:
            return ""
        for separator in (" ", ":", "\uff1a"):
            if lowered.startswith(marker + separator):
                return stripped[len(prefix) + len(separator) :].strip()
    return stripped


def rewrite_feishu_message(text: str) -> str:
    """Return the original message for free questions, or a rewritten project question."""
    command = classify_feishu_command(text)
    return rewrite_command_to_question(command, text)


def is_doc_sync_status_command(text: str) -> bool:
    return classify_feishu_command(text) == FeishuProjectCommand.DOC_SYNC_STATUS


def _normalize(text: str) -> str:
    return "".join(text.casefold().strip().split())


def _matches(normalized: str, terms: tuple[str, ...]) -> bool:
    return any(_normalize(term) in normalized for term in terms)
