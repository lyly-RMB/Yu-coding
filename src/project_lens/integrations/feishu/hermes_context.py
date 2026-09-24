"""ProjectLens context rendered for the Hermes tool loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from project_lens.domain.conversation import ConversationSession
from project_lens.domain.session_state import HandoffSummary, render_session_state_markdown
from project_lens.domain.memory import ProjectMemory
from project_lens.context.memory_retrieval import (
    MemorySummaryEntry,
    render_memory_summary,
)


@dataclass(frozen=True)
class HermesProjectContext:
    text: str
    audit_refs: dict[str, Any]
    renderer_version: str = "hermes_context.v1"


def build_hermes_project_context(
    *,
    session: ConversationSession | None,
    memories: tuple[ProjectMemory, ...] = (),
    memory_summary: tuple[MemorySummaryEntry, ...] = (),
    runtime_access: dict[str, object] | None = None,
    handoff: HandoffSummary | None = None,
) -> HermesProjectContext:
    """Render bounded session/project memory already filtered by the caller."""

    lines = [
        "ProjectLens context is authoritative only within the supplied scope.",
        "Use ProjectLens tools for fresh evidence; do not treat guesses as facts.",
        "Never reveal content outside the current project or access scope.",
    ]
    if runtime_access:
        safe_access = {
            key: runtime_access[key]
            for key in (
                "tenant_id", "project_id", "chat_id", "actor_id", "role",
                "visibility_level", "policy_version",
            )
            if key in runtime_access
        }
        lines.append("runtime_access=" + repr(safe_access))
    if session is None:
        lines.append("session=(none)")
    else:
        summary = session.summary
        lines.extend((
            f"session_id={session.session_id}",
            f"active_skill={summary.active_skill or ''}",
            f"session_intent={summary.session_intent or ''}",
        ))
        if summary.unknowns:
            lines.append("session_unknowns=" + "; ".join(summary.unknowns[:12]))
        if summary.next_actions:
            lines.append("session_next_actions=" + "; ".join(summary.next_actions[:12]))
        if summary.last_assistant_summary:
            lines.append("last_assistant_summary=" + summary.last_assistant_summary)
        if session.session_state is not None:
            state = session.session_state
            lines.extend(
                (
                    f"session_summary_version={state.summary_version}",
                    f"session_last_compressed_turn_id={state.last_compressed_turn_id or ''}",
                    f"session_last_handoff_id={state.last_handoff_id or ''}",
                    f"session_rotation_pending={state.rotation_pending}",
                    "structured_session_state:",
                    render_session_state_markdown(state),
                )
            )
        for index, turn in enumerate(session.recent_turns[-6:], start=1):
            lines.append(f"recent_turn[{index}] user_id={turn.user_id} text={turn.text}")
        if session.task_scratchpad:
            scratch = session.task_scratchpad
            lines.append("task_state=" + repr({
                key: scratch[key]
                for key in ("phase", "plan", "files_read", "test_results")
                if key in scratch
            }))
    if handoff is not None:
        lines.extend(("handoff_summary:", handoff.markdown))
    if memory_summary:
        lines.append(render_memory_summary(memory_summary))
    elif memories:
        lines.append("approved_project_memories:")
        for memory in memories[:40]:
            evidence_ids = ",".join(str(item) for item in memory.evidence_ids)
            lines.append(
                f"memory_id={memory.id} type={memory.memory_type.value} "
                f"approved_by={memory.approved_by} evidence_ids={evidence_ids} "
                f"text={memory.text}"
            )
    else:
        lines.append("approved_project_memories=(none)")
    return HermesProjectContext(
        text="\n".join(lines),
        audit_refs={
            "session_id": str(session.session_id) if session is not None else None,
            "recent_turn_count": len(session.recent_turns) if session is not None else 0,
            "memory_ids": (
                [] if memory_summary else [str(item.id) for item in memories]
            ),
            "memory_count": len(memories),
            "memory_summary_topics": len(memory_summary),
            "runtime_access_keys": sorted(runtime_access or {}),
            "renderer_version": "hermes_context.v1",
            "summary_version": (
                session.session_state.summary_version
                if session is not None and session.session_state is not None
                else 0
            ),
            "last_compressed_turn_id": (
                session.session_state.last_compressed_turn_id
                if session is not None and session.session_state is not None
                else None
            ),
            "last_handoff_id": (
                session.session_state.last_handoff_id
                if session is not None and session.session_state is not None
                else None
            ),
            "handoff_id": handoff.handoff_id if handoff is not None else None,
            "handoff_summary_version": (
                handoff.summary_version if handoff is not None else None
            ),
        },
        renderer_version="hermes_context.v1",
    )
