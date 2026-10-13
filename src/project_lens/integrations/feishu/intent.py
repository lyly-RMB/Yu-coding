"""Feishu intent helpers for bot/meta questions.

Does not query EvidenceIndex / graph / ops stores.
Project vs chitchat gating was removed; natural messages enter project runs.
"""

from __future__ import annotations

import re

_MENTION_TOKEN = re.compile(r"@_user_\d+")

_BOT_META_MODEL_TERMS = (
    "什么模型",
    "用的什么模型",
    "用的是什么模型",
    "哪个模型",
    "哪款模型",
    "模型是什么",
    "what model",
    "which model",
    "你用什么llm",
    "用的什么llm",
)

_BOT_META_ABOUT_TERMS = (
    "你是谁",
    "你是什么",
    "你能做什么",
    "你会什么",
    "你是干什么的",
    "介绍一下你自己",
    "介绍下你自己",
    "what can you do",
    "who are you",
    "你有什么能力",
)


def strip_feishu_mentions(text: str) -> str:
    """Remove Feishu @_user_N mention tokens from message text."""

    cleaned = _MENTION_TOKEN.sub(" ", text)
    return " ".join(cleaned.split()).strip()


def is_bot_meta_question(text: str) -> bool:
    """True for questions about ProjectLens itself (model / capabilities)."""

    normalized = _normalize(strip_feishu_mentions(text))
    if not normalized:
        return False
    if any(_normalize(term) in normalized for term in _BOT_META_MODEL_TERMS):
        return True
    return any(_normalize(term) in normalized for term in _BOT_META_ABOUT_TERMS)


def is_model_meta_question(text: str) -> bool:
    normalized = _normalize(strip_feishu_mentions(text))
    return any(_normalize(term) in normalized for term in _BOT_META_MODEL_TERMS)


def _normalize(text: str) -> str:
    return "".join(text.casefold().strip().split())
