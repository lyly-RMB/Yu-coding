"""ProjectLens context rendered for the Hermes tool loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from project_lens.domain.conversation import (
    ConversationSession,
    format_citation_ledger_for_context,
    format_pending_items_for_hermes,
    latest_compressed_turn_records,
)


from project_lens.domain.session_state import HandoffSummary, render_session_state_markdown
from project_lens.domain.memory import ProjectMemory
from project_lens.context.memory_retrieval import (
    MemorySummaryEntry,
    render_memory_summary,
)


_VIRTUAL_ROLE_LABELS = {
    "virtual-product": "产品",
    "virtual-developer": "研发",
    "virtual-qa": "测试",
    "virtual-manager": "管理者",
    "virtual-ops": "运维",
}


def _role_label(user_id: str) -> str:
    return _VIRTUAL_ROLE_LABELS.get(user_id, user_id)


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
    use_pending_send: bool | None = None,
    handoff: HandoffSummary | None = None,
) -> HermesProjectContext:
    """Render bounded session/project memory already filtered by the caller.

    When ``use_pending_send`` is True (or None and session.pending_send is set),
    conversation history is **only** the pending-send set (preview == send).
    Legacy full-session dump remains for callers that have not refreshed pending.
    """

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

    pending_mode = False
    if session is None:
        lines.append("session=(none)")
    else:
        pending_mode = (
            use_pending_send is True
            or (use_pending_send is None and session.pending_send is not None)
        )
        if use_pending_send is True and session.pending_send is None:
            raise ValueError(
                "use_pending_send=True requires session.pending_send; "
                "call refresh_pending_send first"
            )
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
            lines.extend((
                f"session_summary_version={state.summary_version}",
                f"session_last_compressed_turn_id={state.last_compressed_turn_id or ''}",
                f"session_last_handoff_id={state.last_handoff_id or ''}",
                f"session_rotation_pending={state.rotation_pending}",
                "structured_session_state:",
                render_session_state_markdown(state),
            ))

        if pending_mode and session.pending_send is not None:
            # Hard commitment: no recent_turns / compressed / ledger outside this set.
            lines.append(format_pending_items_for_hermes(session.pending_send.items))
        else:
            for index, turn in enumerate(session.recent_turns, start=1):
                lines.append(
                    f"recent_turn[{index}] role={_role_label(turn.user_id)} "
                    f"user_id={turn.user_id} text={turn.text} "
                    f"evidence_ids={','.join(turn.evidence_ids)}"
                )
            if session.summary.compressed_turn_records:
                lines.append("compressed_turn_summaries:")
                for index, record in enumerate(
                    latest_compressed_turn_records(session.summary),
                    start=1,
                ):
                    evidence = ",".join(record.evidence_ids[:12])
                    lines.append(
                        f"compressed_turn[{index}] time="
                        f"{record.occurred_at.isoformat()} "
                        f"role={_role_label(str(record.run_id or ''))} "
                        f"question={record.user_question[:200]} "
                        f"answer={record.answer_summary[:200]} "
                        f"evidence_ids={evidence}"
                    )
            # Citation ledger: metadata only; bodies via projectlens_get_citation_body.
            lines.append(format_citation_ledger_for_context(session.citations))

        pending_question = (session.pending_send.question if session.pending_send else "")
        if any(term in pending_question for term in ("总结", "讨论", "已确认", "待确认")):
            lines.append(
                "summary_output_format=如果用户要求多角色讨论总结，将产品、研发、测试三段写入完整回答正文(answer_text)，并区分已确认与待确认；不要把角色段落当作 facts 或 citations。普通问题保持自然回答，不要强行套用三段格式。只输出一份回答正文，不要追加技术视图或角色视图。"
            )

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

    # Summaries must preserve the evidence ids carried by prior turns instead
    # of becoming uncited prose when the user asks to summarize the discussion.
    lines.append(
        "citation_inheritance_rule=when summarizing prior turns, reuse their evidence_ids and cite the corresponding sources; do not output citation-free conclusions if prior evidence exists."
    )

    pending_ids: list[str] = []
    pending_count = 0
    token_estimate = 0
    if session is not None and session.pending_send is not None and pending_mode:
        pending_ids = [item.item_id for item in session.pending_send.items]
        pending_count = len(session.pending_send.items)
        token_estimate = session.pending_send.token_estimate

    return HermesProjectContext(
        text="\n".join(lines),
        audit_refs={
            "session_id": str(session.session_id) if session is not None else None,
            "recent_turn_count": len(session.recent_turns) if session is not None else 0,
            "citation_count": len(session.citations) if session is not None else 0,
            "pending_send_mode": pending_mode,
            "pending_item_ids": pending_ids,
            "pending_item_count": pending_count,
            "pending_token_estimate": token_estimate,
            "memory_ids": (
                [] if memory_summary else [str(item.id) for item in memories]
            ),
            "memory_count": len(memories),
            "memory_summary_topics": len(memory_summary),
            "runtime_access_keys": sorted(runtime_access or {}),
            "renderer_version": "hermes_context.v2",
            "summary_version": (
                session.session_state.summary_version
                if session is not None and session.session_state is not None else 0
            ),
            "last_compressed_turn_id": (
                session.session_state.last_compressed_turn_id
                if session is not None and session.session_state is not None else None
            ),
            "last_handoff_id": (
                session.session_state.last_handoff_id
                if session is not None and session.session_state is not None else None
            ),
            "handoff_id": handoff.handoff_id if handoff is not None else None,
            "handoff_summary_version": handoff.summary_version if handoff is not None else None,
        },
        renderer_version="hermes_context.v2",
    )
