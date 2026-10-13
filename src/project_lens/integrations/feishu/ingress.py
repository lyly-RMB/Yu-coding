"""Feishu ingress routing for ProjectLens project-mode messages.

This module decides *entry mode* only. It does not access EvidenceIndex,
graph stores, files, RoleView, or tool gateways.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from project_lens.domain.conversation import ConversationSession
from project_lens.integrations.feishu.commands import is_doc_sync_status_command
from project_lens.integrations.feishu.intent import is_bot_meta_question


class FeishuIngressKind(StrEnum):
    BOT_META = "bot_meta"
    DOC_SYNC_STATUS = "doc_sync_status"
    PROJECT_QUESTION = "project_question"
    DEBUG_PROJECT_COMMAND = "debug_project_command"


@dataclass(frozen=True)
class FeishuIngressDecision:
    kind: FeishuIngressKind
    entry_mode: str | None

    @property
    def creates_project_run(self) -> bool:
        return self.kind in {
            FeishuIngressKind.PROJECT_QUESTION,
            FeishuIngressKind.DEBUG_PROJECT_COMMAND,
        }


class FeishuIngressRouter:
    """Classify Feishu text before AgentRun creation.

    `/project` remains a debug/smoke entry. Natural project questions are the
    product entry. RoleView is intentionally not part of this decision.
    Non-project chitchat is routed as PROJECT_QUESTION (no collaboration gate).
    """

    def decide(
        self,
        text: str,
        *,
        session: ConversationSession,
        followup_rewrite: str | None,
    ) -> FeishuIngressDecision:
        _ = session  # reserved for future session-aware routing
        stripped = text.strip()
        if _is_project_debug_slash(stripped):
            return FeishuIngressDecision(
                kind=FeishuIngressKind.DEBUG_PROJECT_COMMAND,
                entry_mode="debug_slash",
            )
        if is_doc_sync_status_command(stripped):
            return FeishuIngressDecision(
                kind=FeishuIngressKind.DOC_SYNC_STATUS,
                entry_mode=None,
            )
        if followup_rewrite is None and is_bot_meta_question(stripped):
            return FeishuIngressDecision(
                kind=FeishuIngressKind.BOT_META,
                entry_mode=None,
            )
        return FeishuIngressDecision(
            kind=FeishuIngressKind.PROJECT_QUESTION,
            entry_mode="natural_project_question",
        )


def strip_project_debug_slash(text: str) -> str:
    stripped = text.strip()
    lowered = stripped.casefold()
    if lowered == "/project":
        return ""
    if lowered.startswith("/project "):
        return stripped[len("/project ") :].strip()
    return stripped


def _is_project_debug_slash(text: str) -> bool:
    lowered = text.casefold()
    return lowered == "/project" or lowered.startswith("/project ")
