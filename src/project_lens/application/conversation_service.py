"""Conversation session lifecycle for multi-turn Feishu collaboration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from project_lens.application.group_message_gateway import GroupChatHistoryGateway
from project_lens.context.conversation_store import ConversationStore, InMemoryConversationStore
from project_lens.domain.conversation import (
    DEFAULT_RECENT_TURN_LIMIT,
    DEFAULT_RECENT_TURN_TOKEN_BUDGET,
    DEFAULT_SESSION_TTL,
    HISTORY_CANDIDATE_PAGE_SIZE,
    CitationEntry,
    CitationSourceKind,
    ConversationSession,
    ConversationTurn,
    DateWindow,
    DefaultContextItem,
    GroupHistoryListResult,
    HistoryCandidate,
    PendingSendState,
    SessionBranchInfo,
    build_pending_send_items,
    empty_summary,
    enumerate_default_context_items,
    estimate_pending_token_budget,
    exclude_pending_item_ids,
    format_citation_ledger_for_context,
    format_date_window_label,
    group_messages_to_history_candidates,
    list_session_history_candidates,
    make_short_title,
    pending_item_from_citation,
    pending_item_from_default,
    sort_pending_items,
    split_recent_turns_for_compression,
)
from project_lens.domain.models import ProjectAnswer, ProjectRef
from project_lens.domain.session_state import (
    ProjectSessionState,
    SessionArtifactRef,
    SessionDecision,
    SessionEvidenceRef,
    SessionFactRef,
    build_handoff_summary,
    render_session_state_markdown,
)
from project_lens.runtime.lifecycle import LifecycleBus, LifecycleEventType
from project_lens.workflow.context_compression import (
    compress_overflow_into_summary,
    compress_scratchpad,
    extract_pinned_from_scratchpad,
    merge_answer_into_summary,
    merge_pinned_ids,
    pin_prior_traceback,
)
from project_lens.workflow.context_pack import TaskScratchpad, task_scratchpad_from_session
from project_lens.workflow.followup import FollowupRewriter
from project_lens.workflow.task_scratchpad import (
    merge_scratchpads,
    scratchpad_from_answer,
    scratchpad_to_session_dict,
)


class ConversationService:
    """Owns L1 recent turns, citation ledger, and L2 summary. Never writes ProjectMemory."""

    def __init__(
        self,
        store: ConversationStore | None = None,
        *,
        rewriter: FollowupRewriter | None = None,
        recent_turn_limit: int = DEFAULT_RECENT_TURN_LIMIT,
        recent_turn_token_budget: int | None = DEFAULT_RECENT_TURN_TOKEN_BUDGET,
        session_ttl: timedelta = DEFAULT_SESSION_TTL,
        lifecycle: LifecycleBus | None = None,
        group_history: GroupChatHistoryGateway | None = None,
    ) -> None:
        self._store = store or InMemoryConversationStore()
        self._rewriter = rewriter or FollowupRewriter()
        self._recent_turn_limit = max(2, recent_turn_limit)
        self._recent_turn_token_budget = (
            None
            if recent_turn_token_budget is None
            else max(1, int(recent_turn_token_budget))
        )
        self._session_ttl = session_ttl
        self._lifecycle = lifecycle or LifecycleBus()
        self._group_history = group_history

    def configure_group_history(
        self, gateway: GroupChatHistoryGateway | None
    ) -> None:
        """Wire Feishu IM history client after create_app assembly."""

        self._group_history = gateway

    @property
    def store(self) -> ConversationStore:
        return self._store

    def get_or_create(
        self,
        *,
        tenant_id: str,
        chat_id: str,
        user_id: str,
        project: ProjectRef,
    ) -> ConversationSession:
        existing = self._store.get_by_binding(
            tenant_id=tenant_id,
            chat_id=chat_id,
            user_id=user_id,
            project=project,
        )
        if existing is not None:
            self._emit_session_event(
                LifecycleEventType.SESSION_LOADED,
                existing,
                extra={"recovered": True},
            )
            return existing
        session = ConversationSession(
            tenant_id=tenant_id,
            chat_id=chat_id,
            user_id=user_id,
            project=project,
            summary=empty_summary(project),
            branch_name="A",
            write_stopped=False,
            session_state=ProjectSessionState(
                session_id=UUID(int=0), project=project, actor_id=user_id
            ),
            expires_at=datetime.now(timezone.utc) + self._session_ttl,
        )
        session = session.model_copy(update={
            "session_state": ProjectSessionState(
                session_id=session.session_id, project=project, actor_id=user_id
            )
        })
        saved = self._store.upsert(session)
        self._store.set_active_session(saved)
        self._emit_session_event(LifecycleEventType.SESSION_CREATED, saved)
        self._emit_session_event(LifecycleEventType.SESSION_SAVED, saved)
        return saved

    def list_branches(
        self,
        session: ConversationSession,
    ) -> tuple[SessionBranchInfo, ...]:
        """Sibling lines under the same Feishu binding (for card switch UI)."""

        siblings = self._store.list_by_binding(
            tenant_id=session.tenant_id,
            chat_id=session.chat_id,
            user_id=session.user_id,
            project=session.project,
        )
        active = self._store.get_by_binding(
            tenant_id=session.tenant_id,
            chat_id=session.chat_id,
            user_id=session.user_id,
            project=session.project,
        )
        active_id = active.session_id if active is not None else None
        return tuple(
            item.to_branch_info(is_active=item.session_id == active_id)
            for item in siblings
        )

    def fork_session(
        self,
        session: ConversationSession,
        *,
        branch_name: str | None = None,
    ) -> ConversationSession:
        """Copy current line into a new branch; stop writes on the parent.

        Aligns with Hermes ``/branch``: parent freezes at the fork point;
        only the new line continues. Does not call a model to rewrite history.
        """

        self._require_writable(session)
        siblings = self._store.list_by_binding(
            tenant_id=session.tenant_id,
            chat_id=session.chat_id,
            user_id=session.user_id,
            project=session.project,
        )
        name = (branch_name or "").strip() or _next_branch_name(
            item.branch_name for item in siblings
        )
        stopped = session.model_copy(
            update={
                "write_stopped": True,
                "expires_at": datetime.now(timezone.utc) + self._session_ttl,
            }
        )
        self._store.upsert(stopped)
        self._emit_session_event(LifecycleEventType.SESSION_SAVED, stopped)

        child = ConversationSession(
            tenant_id=session.tenant_id,
            chat_id=session.chat_id,
            user_id=session.user_id,
            project=session.project,
            last_run_id=session.last_run_id,
            recent_turns=session.recent_turns,
            citations=session.citations,
            summary=session.summary,
            pending_send=session.pending_send,
            date_window=session.date_window,
            parent_session_id=session.session_id,
            branch_name=name,
            write_stopped=False,
            task_scratchpad=dict(session.task_scratchpad),
            expires_at=datetime.now(timezone.utc) + self._session_ttl,
        )
        saved = self._store.upsert(child)
        self._store.set_active_session(saved)
        self._emit_session_event(LifecycleEventType.SESSION_CREATED, saved)
        self._emit_session_event(LifecycleEventType.SESSION_SAVED, saved)
        return saved

    def switch_session(
        self,
        session: ConversationSession,
        target_session_id: UUID,
    ) -> ConversationSession:
        """Make ``target_session_id`` the active line for this binding."""

        target = self._require_sibling(session, target_session_id)
        if target.write_stopped:
            target = target.model_copy(
                update={
                    "write_stopped": False,
                    "expires_at": datetime.now(timezone.utc) + self._session_ttl,
                }
            )
            self._store.upsert(target)
            self._emit_session_event(LifecycleEventType.SESSION_SAVED, target)
        self._store.set_active_session(target)
        self._emit_session_event(
            LifecycleEventType.SESSION_LOADED,
            target,
            extra={"switched": True},
        )
        return target

    def delete_session(
        self,
        session: ConversationSession,
        target_session_id: UUID | None = None,
    ) -> ConversationSession:
        """Delete a sibling branch and keep the binding on a survivor.

        Defaults to deleting ``session`` itself. Refuses when it would leave
        zero branches under the binding.
        """

        target_id = target_session_id or session.session_id
        target = self._require_sibling(session, target_id)
        siblings = self._store.list_by_binding(
            tenant_id=session.tenant_id,
            chat_id=session.chat_id,
            user_id=session.user_id,
            project=session.project,
        )
        if len(siblings) < 2:
            raise ValueError("cannot delete the only remaining branch")
        deleted_name = target.branch_name
        deleted_id = target.session_id
        self._store.delete(target)

        remaining = self._store.list_by_binding(
            tenant_id=session.tenant_id,
            chat_id=session.chat_id,
            user_id=session.user_id,
            project=session.project,
        )
        if not remaining:
            raise RuntimeError("no sibling left after delete; binding is empty")
        remaining_ids = {item.session_id for item in remaining}
        active = self._store.get_by_binding(
            tenant_id=session.tenant_id,
            chat_id=session.chat_id,
            user_id=session.user_id,
            project=session.project,
        )
        # Legacy binding fallback may rebind a write_stopped parent; always
        # pick an explicit survivor and ensure it can accept writes.
        if active is None or active.session_id not in remaining_ids:
            chosen = next(
                (item for item in remaining if not item.write_stopped),
                remaining[0],
            )
        else:
            chosen = active
        if chosen.write_stopped:
            chosen = chosen.model_copy(
                update={
                    "write_stopped": False,
                    "expires_at": datetime.now(timezone.utc) + self._session_ttl,
                }
            )
            self._store.upsert(chosen)
            self._emit_session_event(LifecycleEventType.SESSION_SAVED, chosen)
        self._store.set_active_session(chosen)
        self._emit_session_event(
            LifecycleEventType.SESSION_LOADED,
            chosen,
            extra={
                "deleted_branch": True,
                "deleted_session_id": str(deleted_id),
                "deleted_branch_name": deleted_name,
            },
        )
        return chosen

    def _require_sibling(
        self,
        session: ConversationSession,
        target_session_id: UUID,
    ) -> ConversationSession:
        target = self._store.get(target_session_id)
        if target is None:
            raise KeyError(f"session not found: {target_session_id}")
        if (
            target.tenant_id != session.tenant_id
            or target.chat_id != session.chat_id
            or target.user_id != session.user_id
            or target.project.tenant_id != session.project.tenant_id
            or target.project.project_id != session.project.project_id
        ):
            raise ValueError("target session is not a sibling under this binding")
        return target

    def prepare_question(
        self,
        session: ConversationSession,
        text: str,
    ) -> tuple[str, str | None]:
        """Return (question_for_run, followup_rewrite_or_none)."""

        from project_lens.workflow.skills import skill_routing_enabled

        if not skill_routing_enabled():
            return (text, None)
        followup = self._rewriter.rewrite(text, session)
        return (followup or text, followup)

    def attach_runtime_access(
        self,
        session: ConversationSession,
        runtime_access: dict[str, object],
    ) -> ConversationSession:
        """Persist EffectiveAccessScope audit bounds on a frozen session."""

        self._require_writable(session)
        merged = {**session.task_scratchpad, "runtime_access": runtime_access}
        updated = session.model_copy(update={"task_scratchpad": merged})
        saved = self._store.upsert(updated)
        self._emit_session_event(LifecycleEventType.SESSION_SAVED, saved)
        return saved

    def record_turn(
        self,
        session: ConversationSession,
        *,
        user_id: str,
        text: str,
        rewritten_question: str | None,
        run_id: UUID | None = None,
        answer: ProjectAnswer | None = None,
        task_state: TaskScratchpad | None = None,
        trace_id: str | None = None,
    ) -> ConversationSession:
        self._require_writable(session)
        turn = ConversationTurn(
            run_id=run_id,
            user_id=user_id,
            text=text,
            rewritten_question=rewritten_question,
            answer_summary=(
                None
                if answer is None
                else (answer.answer_text or answer.business_summary or answer.technical_summary or "")[:4_000]
                or None
            ),
            evidence_ids=(
                ()
                if answer is None
                else tuple(str(item.id) for item in answer.evidence)
            ),
        )
        # Citation must be written before compression so overflowed turns stay retrievable.
        citation = self._turn_citation(
            turn,
            answer=answer,
            trace_id=trace_id,
        )
        citations = session.citations + (citation,)
        state = self._ensure_session_state(session)
        recent = session.recent_turns + (turn,)
        overflow, kept = split_recent_turns_for_compression(
            recent,
            recent_turn_limit=self._recent_turn_limit,
            recent_turn_token_budget=self._recent_turn_token_budget,
        )
        # Pin traceback into L2 immediately so restart/compress cannot drop it.
        summary = pin_prior_traceback(
            session.summary,
            text,
            rewritten_question,
        )
        if overflow:
            state = state.model_copy(
                update={
                    "last_compressed_turn_id": self._turn_key(overflow[-1]),
                    "current_status": "in_progress",
                }
            )
            self._lifecycle.emit(
                LifecycleEventType.COMPRESSION_TRIGGERED,
                run_id=run_id,
                project=session.project,
                payload={
                    "session_id": str(session.session_id),
                    "overflow_count": len(overflow),
                    "recent_turn_limit": self._recent_turn_limit,
                    "cycle_before": summary.compression_cycle,
                },
            )
            summary = compress_overflow_into_summary(summary, overflow)
            self._lifecycle.emit(
                LifecycleEventType.COMPRESSION_COMPLETED,
                run_id=run_id,
                project=session.project,
                payload={
                    "session_id": str(session.session_id),
                    "compression_cycle": summary.compression_cycle,
                    "pinned_evidence_count": len(summary.pinned_ids.evidence_ids),
                    "pinned_file_count": len(summary.pinned_ids.file_paths),
                    "compressed_turns": summary.active_topic.get("compressed_turns"),
                    "compressed_turn_record_count": len(summary.compressed_turn_records),
                },
            )
        if answer is not None:
            summary = merge_answer_into_summary(summary, answer)
        merged_task = compress_scratchpad(
            merge_scratchpads(
                task_scratchpad_from_session(session),
                scratchpad_from_answer(answer),
                task_state,
            )
        )
        if merged_task is not None:
            summary = summary.model_copy(
                update={
                    "pinned_ids": merge_pinned_ids(
                        summary.pinned_ids,
                        extract_pinned_from_scratchpad(merged_task),
                    )
                }
            )
        update: dict[str, object] = {
            "recent_turns": kept,
            "citations": citations,
            "summary": summary,
            "last_run_id": run_id if run_id is not None else session.last_run_id,
            "expires_at": datetime.now(timezone.utc) + self._session_ttl,
        }
        if merged_task is not None:
            scratchpad = scratchpad_to_session_dict(merged_task)
            current_access = session.task_scratchpad.get("runtime_access")
            if current_access is not None:
                scratchpad["runtime_access"] = current_access
            update["task_scratchpad"] = scratchpad
            state = self._merge_task_state(state, merged_task)
        if answer is not None:
            state = self._merge_answer_into_state(state, answer, summary)
        state = self._finalize_state(
            state, summary=summary, session=session, current_turn=turn
        )
        update["session_state"] = state
        updated = session.model_copy(update=update)
        saved = self._store.upsert(updated)
        self._emit_session_event(
            LifecycleEventType.SESSION_SAVED,
            saved,
            run_id=run_id,
        )
        return saved

    def mark_rotation_pending(
        self, session: ConversationSession, *, pending: bool = True
    ) -> ConversationSession:
        state = self._ensure_session_state(session)
        state = state.model_copy(update={
            "rotation_pending": pending,
            "rotation_count": state.rotation_count + (1 if pending else 0),
            "summary_version": state.summary_version + 1,
            "updated_at": datetime.now(timezone.utc),
        })
        state = state.model_copy(update={"summary_markdown": render_session_state_markdown(state)})
        saved = self._store.upsert(session.model_copy(update={"session_state": state}))
        self._emit_session_event(LifecycleEventType.SESSION_SAVED, saved)
        return saved

    def build_handoff(self, session: ConversationSession):
        state = self._ensure_session_state(session)
        handoff = build_handoff_summary(state)
        updated_state = state.model_copy(update={
            "last_handoff_id": handoff.handoff_id,
            "rotation_pending": False,
            "summary_version": state.summary_version + 1,
            "updated_at": datetime.now(timezone.utc),
        })
        updated_state = updated_state.model_copy(
            update={"summary_markdown": render_session_state_markdown(updated_state)}
        )
        saved = self._store.upsert(session.model_copy(update={"session_state": updated_state}))
        return handoff, saved

    @staticmethod
    def _turn_key(turn: ConversationTurn) -> str:
        return str(turn.run_id or turn.created_at.isoformat())

    @staticmethod
    def _ensure_session_state(session: ConversationSession) -> ProjectSessionState:
        return session.session_state or ProjectSessionState(
            session_id=session.session_id, project=session.project, actor_id=session.user_id
        )

    def _merge_answer_into_state(self, state: ProjectSessionState, answer: ProjectAnswer, summary):
        facts = list(state.confirmed_facts)
        refs = list(state.evidence_refs)
        decisions = list(state.decisions)
        for claim in answer.claims:
            candidate = SessionFactRef(
                fact_id=str(claim.id), text=claim.text,
                evidence_ids=tuple(str(item) for item in claim.evidence_ids),
            )
            facts = [item for item in facts if item.fact_id != candidate.fact_id]
            facts.append(candidate)
        for evidence in answer.evidence:
            refs = [item for item in refs if item.evidence_id != str(evidence.id)]
            refs.append(SessionEvidenceRef(
                evidence_id=str(evidence.id),
                source_key=f"{evidence.source.system}:{evidence.source.source_id}",
                revision=str(evidence.metadata.get("revision") or evidence.metadata.get("version") or "") or None,
                content_hash=evidence.content_hash,
                authority=str(evidence.metadata.get("authority") or "approved"),
                status=str(evidence.metadata.get("status") or "current"),
            ))
        for action in answer.recommended_actions:
            decision = SessionDecision(
                decision_id=str(action.id), text=action.title,
                rationale=action.description,
                evidence_ids=tuple(str(item) for item in answer.evidence[:8]),
                status="proposed",
            )
            decisions = [item for item in decisions if item.decision_id != decision.decision_id]
            decisions.append(decision)
        return state.model_copy(update={
            "task_intent": state.task_intent or summary.session_intent or (answer.answer_text or answer.business_summary)[:500],
            "current_status": answer.status or "in_progress",
            "confirmed_facts": tuple(facts[-80:]),
            "evidence_refs": tuple(refs[-80:]),
            "decisions": tuple(decisions[-40:]),
            "open_questions": tuple(dict.fromkeys((*state.open_questions, *answer.unknowns)))[-40:],
        })

    @staticmethod
    def _merge_task_state(state: ProjectSessionState, task_state: TaskScratchpad) -> ProjectSessionState:
        artifacts = list(state.artifact_refs)
        for path in (*task_state.files_read, *task_state.files_changed):
            artifacts = [item for item in artifacts if item.key != path]
            artifacts.append(SessionArtifactRef(kind="file", key=path))
        blocked = tuple(dict.fromkeys((*state.blocked_items, *task_state.failed_attempts)))[-40:]
        return state.model_copy(update={"artifact_refs": tuple(artifacts[-80:]), "blocked_items": blocked})

    @staticmethod
    def _finalize_state(state: ProjectSessionState, *, summary, session: ConversationSession, current_turn: ConversationTurn) -> ProjectSessionState:
        finalized = state.model_copy(update={
            "task_intent": state.task_intent or summary.session_intent,
            "open_questions": tuple(dict.fromkeys((*state.open_questions, *summary.unknowns)))[-40:],
            "artifact_refs": tuple(list(state.artifact_refs) + ([SessionArtifactRef(kind="run", key=str(current_turn.run_id))] if current_turn.run_id else []))[-80:],
            "summary_version": state.summary_version + 1,
            "updated_at": datetime.now(timezone.utc),
        })
        return finalized.model_copy(update={"summary_markdown": render_session_state_markdown(finalized)})

    def append_citation(
        self,
        session: ConversationSession,
        entry: CitationEntry,
    ) -> ConversationSession:
        """Pin a fine-selected history / group-message citation onto the ledger."""

        self._require_writable(session)
        if any(item.citation_id == entry.citation_id for item in session.citations):
            return session
        updated = session.model_copy(
            update={
                "citations": session.citations + (entry,),
                "expires_at": datetime.now(timezone.utc) + self._session_ttl,
            }
        )
        saved = self._store.upsert(updated)
        self._emit_session_event(LifecycleEventType.SESSION_SAVED, saved)
        return saved

    def citation_context_fragment(self, session: ConversationSession) -> str:
        """Default context lines for the citation ledger (metadata only)."""

        return format_citation_ledger_for_context(session.citations)

    def list_default_context_items(
        self,
        session: ConversationSession,
        *,
        date_window: DateWindow | None = None,
    ) -> tuple[DefaultContextItem, ...]:
        """Enumerate default pending items (summary + recent turns).

        ``date_window=None`` means whole pool — no strip-non-today (S02).
        """

        return enumerate_default_context_items(session, date_window=date_window)

    def set_date_window(
        self,
        session: ConversationSession,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        refresh_pending: bool = True,
    ) -> ConversationSession:
        """Write session-level date window and optionally rebuild pending-send."""

        self._require_writable(session)
        window = DateWindow(start=start, end=end)
        updated = session.model_copy(
            update={
                "date_window": window,
                "expires_at": datetime.now(timezone.utc) + self._session_ttl,
            }
        )
        saved = self._store.upsert(updated)
        self._emit_session_event(LifecycleEventType.SESSION_SAVED, saved)
        if refresh_pending and saved.pending_send is not None:
            pending = saved.pending_send
            saved = self.refresh_pending_send(
                saved,
                question=pending.question,
                question_for_run=pending.question_for_run,
                date_window=window,
            )
        return saved

    def clear_date_window(
        self,
        session: ConversationSession,
        *,
        refresh_pending: bool = True,
    ) -> ConversationSession:
        """Clear date window → restore whole-pool default (no strip-non-today)."""

        self._require_writable(session)
        updated = session.model_copy(
            update={
                "date_window": None,
                "expires_at": datetime.now(timezone.utc) + self._session_ttl,
            }
        )
        saved = self._store.upsert(updated)
        self._emit_session_event(LifecycleEventType.SESSION_SAVED, saved)
        if refresh_pending and saved.pending_send is not None:
            pending = saved.pending_send
            saved = self.refresh_pending_send(
                saved,
                question=pending.question,
                question_for_run=pending.question_for_run,
                date_window=None,
            )
        return saved

    def active_date_window(
        self,
        *,
        tenant_id: str,
        chat_id: str,
        user_id: str,
        project: ProjectRef,
    ) -> DateWindow | None:
        """Return the active binding session's date window (for search defaults)."""

        session = self._store.get_by_binding(
            tenant_id=tenant_id,
            chat_id=chat_id,
            user_id=user_id,
            project=project,
        )
        if session is None:
            return None
        return session.date_window

    def date_window_label(self, session: ConversationSession) -> str:
        """Card-facing label for the current date window."""

        return format_date_window_label(session.date_window)

    def refresh_pending_send(
        self,
        session: ConversationSession,
        *,
        question: str,
        question_for_run: str | None = None,
        date_window: DateWindow | None = None,
        selected_citation_ids: tuple[str, ...] | None = None,
    ) -> ConversationSession:
        """Rebuild preview pending-send set from defaults (+ optional citations).

        Hard commitment: this set is what Hermes assembly will consume.
        """

        self._require_writable(session)
        prior_citations: tuple[str, ...] = ()
        if selected_citation_ids is not None:
            prior_citations = selected_citation_ids
        elif session.pending_send is not None:
            prior_citations = tuple(
                item.citation_id
                for item in session.pending_send.items
                if item.citation_id
            )

        window = date_window if date_window is not None else session.date_window
        items = build_pending_send_items(
            session,
            date_window=window,
            selected_citation_ids=prior_citations or None,
        )
        q = question.strip()
        q_run = (question_for_run or question).strip() or q
        state = PendingSendState(
            question=q,
            question_for_run=q_run,
            items=items,
            token_estimate=estimate_pending_token_budget(items),
        )
        updated = session.model_copy(
            update={
                "pending_send": state,
                "expires_at": datetime.now(timezone.utc) + self._session_ttl,
            }
        )
        saved = self._store.upsert(updated)
        self._emit_session_event(LifecycleEventType.SESSION_SAVED, saved)
        return saved

    def exclude_from_pending_send(
        self,
        session: ConversationSession,
        item_ids: tuple[str, ...] | list[str],
    ) -> ConversationSession:
        """Remove items from pending-send, re-sort by time, refresh token budget."""

        self._require_writable(session)
        if session.pending_send is None:
            raise ValueError("pending_send is not set; call refresh_pending_send first")
        items = exclude_pending_item_ids(session.pending_send.items, item_ids)
        state = session.pending_send.model_copy(
            update={
                "items": items,
                "token_estimate": estimate_pending_token_budget(items),
                "refreshed_at": datetime.now(timezone.utc),
            }
        )
        updated = session.model_copy(
            update={
                "pending_send": state,
                "expires_at": datetime.now(timezone.utc) + self._session_ttl,
            }
        )
        saved = self._store.upsert(updated)
        self._emit_session_event(LifecycleEventType.SESSION_SAVED, saved)
        return saved

    _CONTEXT_EDIT_STAGE_KEY = "context_edit_stage"

    def get_context_edit_stage(self, session: ConversationSession) -> dict[str, tuple[str, ...]]:
        """Staged toggles for the edit-context card (applied only on 「确定」)."""

        raw = session.task_scratchpad.get(self._CONTEXT_EDIT_STAGE_KEY) or {}
        if not isinstance(raw, dict):
            raw = {}

        def _ids(key: str) -> tuple[str, ...]:
            values = raw.get(key) or ()
            if not isinstance(values, (list, tuple)):
                return ()
            return tuple(str(item).strip() for item in values if str(item).strip())

        return {
            "exclude_ids": _ids("exclude_ids"),
            "restore_ids": _ids("restore_ids"),
            "join_citation_ids": _ids("join_citation_ids"),
        }

    def clear_context_edit_stage(self, session: ConversationSession) -> ConversationSession:
        scratch = dict(session.task_scratchpad)
        scratch.pop(self._CONTEXT_EDIT_STAGE_KEY, None)
        updated = session.model_copy(
            update={
                "task_scratchpad": scratch,
                "expires_at": datetime.now(timezone.utc) + self._session_ttl,
            }
        )
        return self._store.upsert(updated)

    def _save_context_edit_stage(
        self,
        session: ConversationSession,
        stage: dict[str, tuple[str, ...]],
    ) -> ConversationSession:
        scratch = {
            **session.task_scratchpad,
            self._CONTEXT_EDIT_STAGE_KEY: {
                "exclude_ids": list(stage.get("exclude_ids") or ()),
                "restore_ids": list(stage.get("restore_ids") or ()),
                "join_citation_ids": list(stage.get("join_citation_ids") or ()),
            },
        }
        updated = session.model_copy(
            update={
                "task_scratchpad": scratch,
                "expires_at": datetime.now(timezone.utc) + self._session_ttl,
            }
        )
        return self._store.upsert(updated)

    def toggle_context_edit_exclude(
        self, session: ConversationSession, item_id: str
    ) -> ConversationSession:
        needle = item_id.strip()
        if not needle:
            return session
        stage = self.get_context_edit_stage(session)
        selected = set(stage["exclude_ids"])
        if needle in selected:
            selected.remove(needle)
        else:
            selected.add(needle)
        return self._save_context_edit_stage(
            session, {**stage, "exclude_ids": tuple(sorted(selected))}
        )

    def toggle_context_edit_restore(
        self, session: ConversationSession, item_id: str
    ) -> ConversationSession:
        needle = item_id.strip()
        if not needle:
            return session
        stage = self.get_context_edit_stage(session)
        selected = set(stage["restore_ids"])
        if needle in selected:
            selected.remove(needle)
        else:
            selected.add(needle)
        return self._save_context_edit_stage(
            session, {**stage, "restore_ids": tuple(sorted(selected))}
        )

    def toggle_context_edit_join(
        self, session: ConversationSession, citation_id: str
    ) -> ConversationSession:
        needle = citation_id.strip()
        if not needle:
            return session
        stage = self.get_context_edit_stage(session)
        selected = set(stage["join_citation_ids"])
        if needle in selected:
            selected.remove(needle)
        else:
            selected.add(needle)
        return self._save_context_edit_stage(
            session, {**stage, "join_citation_ids": tuple(sorted(selected))}
        )

    def apply_context_edit_stage(self, session: ConversationSession) -> ConversationSession:
        """Commit staged exclude / restore / join, then clear the stage."""

        self._require_writable(session)
        stage = self.get_context_edit_stage(session)
        if stage["exclude_ids"]:
            session = self.exclude_from_pending_send(session, stage["exclude_ids"])
        if stage["restore_ids"]:
            session = self.restore_to_pending_send(session, stage["restore_ids"])
        if stage["join_citation_ids"]:
            session = self.add_citations_to_pending_send(session, stage["join_citation_ids"])
        return self.clear_context_edit_stage(session)

    def add_citations_to_pending_send(
        self,
        session: ConversationSession,
        citation_ids: tuple[str, ...] | list[str],
    ) -> ConversationSession:
        """Fine-select citations into the pending set (card「选用」/「加入」)."""

        self._require_writable(session)
        if session.pending_send is None:
            raise ValueError("pending_send is not set; call refresh_pending_send first")
        by_id = {item.item_id: item for item in session.pending_send.items}
        wanted = {cid.strip() for cid in citation_ids if cid and str(cid).strip()}
        for entry in session.citations:
            if entry.citation_id not in wanted:
                continue
            item = pending_item_from_citation(entry)
            by_id[item.item_id] = item
        items = sort_pending_items(by_id.values())
        state = session.pending_send.model_copy(
            update={
                "items": items,
                "token_estimate": estimate_pending_token_budget(items),
                "refreshed_at": datetime.now(timezone.utc),
            }
        )
        updated = session.model_copy(
            update={
                "pending_send": state,
                "expires_at": datetime.now(timezone.utc) + self._session_ttl,
            }
        )
        saved = self._store.upsert(updated)
        self._emit_session_event(LifecycleEventType.SESSION_SAVED, saved)
        return saved

    def restore_to_pending_send(
        self,
        session: ConversationSession,
        item_ids: tuple[str, ...] | list[str],
    ) -> ConversationSession:
        """Re-include excluded default rows (or known citations) into pending-send."""

        self._require_writable(session)
        if session.pending_send is None:
            raise ValueError("pending_send is not set; call refresh_pending_send first")
        wanted = {iid.strip() for iid in item_ids if iid and str(iid).strip()}
        if not wanted:
            return session
        by_id = {item.item_id: item for item in session.pending_send.items}
        for default in enumerate_default_context_items(
            session, date_window=session.date_window
        ):
            item = pending_item_from_default(default)
            if item.item_id in wanted:
                by_id[item.item_id] = item
        for entry in session.citations:
            item = pending_item_from_citation(entry)
            if item.item_id in wanted or entry.citation_id in wanted:
                by_id[item.item_id] = item
        items = sort_pending_items(by_id.values())
        state = session.pending_send.model_copy(
            update={
                "items": items,
                "token_estimate": estimate_pending_token_budget(items),
                "refreshed_at": datetime.now(timezone.utc),
            }
        )
        updated = session.model_copy(
            update={
                "pending_send": state,
                "expires_at": datetime.now(timezone.utc) + self._session_ttl,
            }
        )
        saved = self._store.upsert(updated)
        self._emit_session_event(LifecycleEventType.SESSION_SAVED, saved)
        return saved

    def list_history_candidates(
        self,
        session: ConversationSession,
        *,
        offset: int = 0,
        page_size: int = HISTORY_CANDIDATE_PAGE_SIZE,
        date_window: DateWindow | None = None,
    ) -> tuple[tuple[HistoryCandidate, ...], int]:
        """Paginated session-side history for「选择更多历史」."""

        return list_session_history_candidates(
            session,
            date_window=date_window,
            offset=offset,
            page_size=page_size,
        )

    def list_group_history(
        self,
        session: ConversationSession,
        *,
        date_window: DateWindow | None = None,
        page_size: int = HISTORY_CANDIDATE_PAGE_SIZE,
        page_token: str | None = None,
    ) -> GroupHistoryListResult:
        """Fetch Feishu group history for「选择更多历史」; session-side stays independent."""

        window = date_window if date_window is not None else session.date_window
        gateway = self._group_history
        if gateway is None:
            return GroupHistoryListResult(
                available=False,
                unavailable_reason="群聊历史客户端未配置（无飞书凭证或未组装）",
            )
        page = gateway.list_chat_history(
            chat_id=session.chat_id,
            start_time=window.start if window is not None else None,
            end_time=window.end if window is not None else None,
            page_size=page_size,
            page_token=page_token,
        )
        if not page.available:
            return GroupHistoryListResult(
                available=False,
                unavailable_reason=page.unavailable_reason
                or "群聊历史不可用（权限或机器人不在群）",
            )
        candidates = group_messages_to_history_candidates(page.items, session)
        return GroupHistoryListResult(
            candidates=candidates,
            available=True,
            has_more=page.has_more,
            next_page_token=page.next_page_token,
        )

    def list_group_history_stub(
        self,
        session: ConversationSession,
        *,
        date_window: DateWindow | None = None,
    ) -> tuple[HistoryCandidate, ...]:
        """Compat alias — prefer list_group_history."""

        result = self.list_group_history(session, date_window=date_window)
        return result.candidates

    def fine_select_history(
        self,
        session: ConversationSession,
        citation_id: str,
    ) -> ConversationSession:
        """Write citation into pending (already on ledger) and refresh token budget."""

        if session.pending_send is None:
            raise ValueError("pending_send is not set; call refresh_pending_send first")
        needle = citation_id.strip()
        if not needle:
            raise ValueError("citation_id is required")
        if not any(item.citation_id == needle for item in session.citations):
            raise KeyError(f"citation not found: {needle}")
        return self.add_citations_to_pending_send(session, (needle,))

    def fine_select_group_message(
        self,
        session: ConversationSession,
        *,
        message_id: str,
        occurred_at: datetime | None = None,
        short_title: str | None = None,
        body_text: str | None = None,
    ) -> ConversationSession:
        """Fine-select a Feishu group message into citation ledger + pending-send.

        Prefers body snapshot at select time; keeps message_id for later re-fetch.
        """

        if session.pending_send is None:
            raise ValueError("pending_send is not set; call refresh_pending_send first")
        mid = message_id.strip()
        if not mid:
            raise ValueError("message_id is required")

        existing = next(
            (item for item in session.citations if item.message_id == mid),
            None,
        )
        if existing is not None:
            return self.add_citations_to_pending_send(session, (existing.citation_id,))

        body = (body_text or "").strip()
        if not body and self._group_history is not None:
            fetched = self._group_history.get_message_text(mid)
            body = (fetched or "").strip()
        if not body:
            # Still pin with message_id so tools can re-fetch later.
            body_snapshot = None
        else:
            body_snapshot = body[:20_000]

        stamp = occurred_at or datetime.now(timezone.utc)
        title = (short_title or "").strip() or make_short_title(body or mid)
        entry = CitationEntry(
            source_kind=CitationSourceKind.GROUP_MESSAGE,
            occurred_at=stamp.astimezone(timezone.utc),
            short_title=title,
            message_id=mid,
            body_snapshot=body_snapshot,
        )
        session = self.append_citation(session, entry)
        return self.add_citations_to_pending_send(session, (entry.citation_id,))

    def clear_pending_send(self, session: ConversationSession) -> ConversationSession:
        """Drop pending-send after a confirmed send (or cancel)."""

        self._require_writable(session)
        if session.pending_send is None:
            return session
        updated = session.model_copy(
            update={
                "pending_send": None,
                "expires_at": datetime.now(timezone.utc) + self._session_ttl,
            }
        )
        saved = self._store.upsert(updated)
        self._emit_session_event(LifecycleEventType.SESSION_SAVED, saved)
        return saved

    def find_citation(
        self,
        *,
        citation_id: str,
        tenant_id: str,
        chat_id: str,
        user_id: str,
        project: ProjectRef,
    ) -> CitationEntry | None:
        """Resolve a citation on the active binding session after project match."""

        session = self._store.get_by_binding(
            tenant_id=tenant_id,
            chat_id=chat_id,
            user_id=user_id,
            project=project,
        )
        if session is None:
            return None
        if (
            session.project.tenant_id != project.tenant_id
            or session.project.project_id != project.project_id
        ):
            return None
        needle = citation_id.strip()
        for item in session.citations:
            if item.citation_id == needle:
                return item
        return None

    def get_citation_body(
        self,
        *,
        citation_id: str,
        tenant_id: str,
        chat_id: str,
        user_id: str,
        project: ProjectRef,
    ) -> dict[str, object] | None:
        """Return retrievable body payload for one authorized citation, or None."""

        entry = self.find_citation(
            citation_id=citation_id,
            tenant_id=tenant_id,
            chat_id=chat_id,
            user_id=user_id,
            project=project,
        )
        if entry is None:
            return None
        body = (entry.body_snapshot or "").strip()
        if not body and entry.message_id and self._group_history is not None:
            fetched = self._group_history.get_message_text(entry.message_id)
            body = (fetched or "").strip()
            if body:
                # Persist snapshot so later tool calls work without Feishu.
                session = self._store.get_by_binding(
                    tenant_id=tenant_id,
                    chat_id=chat_id,
                    user_id=user_id,
                    project=project,
                )
                if session is not None and not session.write_stopped:
                    updated_citations = tuple(
                        item.model_copy(update={"body_snapshot": body[:20_000]})
                        if item.citation_id == entry.citation_id
                        else item
                        for item in session.citations
                    )
                    saved = self._store.upsert(
                        session.model_copy(update={"citations": updated_citations})
                    )
                    self._emit_session_event(LifecycleEventType.SESSION_SAVED, saved)
                    entry = next(
                        item
                        for item in saved.citations
                        if item.citation_id == entry.citation_id
                    )
        if not body:
            return {
                "citation_id": entry.citation_id,
                "source_kind": entry.source_kind.value,
                "occurred_at": entry.occurred_at.astimezone(timezone.utc).isoformat(),
                "short_title": entry.short_title,
                "body": None,
                "run_id": str(entry.run_id) if entry.run_id is not None else None,
                "trace_id": entry.trace_id,
                "message_id": entry.message_id,
                "evidence_ids": list(entry.evidence_ids),
                "file_refs": list(entry.file_refs),
                "body_available": False,
                "retrieval_hint": (
                    "body_snapshot empty; Feishu re-fetch failed or unavailable "
                    "(check im:message.group_msg / bot-in-chat)"
                    if entry.message_id
                    else "body_snapshot empty; use run_id/trace_id keys"
                ),
            }
        return {
            "citation_id": entry.citation_id,
            "source_kind": entry.source_kind.value,
            "occurred_at": entry.occurred_at.astimezone(timezone.utc).isoformat(),
            "short_title": entry.short_title,
            "body": body,
            "run_id": str(entry.run_id) if entry.run_id is not None else None,
            "trace_id": entry.trace_id,
            "message_id": entry.message_id,
            "evidence_ids": list(entry.evidence_ids),
            "file_refs": list(entry.file_refs),
            "body_available": True,
        }

    def attach_hermes_tool_loop(
        self,
        session: ConversationSession,
        entry: dict[str, object],
        *,
        keep_last: int = 5,
    ) -> ConversationSession:
        """Persist a compact Hermes tool-loop audit pointer on the session scratchpad.

        Does not create an AgentRun. ``loop_id`` is a synthetic key for agent_events.
        """

        self._require_writable(session)
        recent = [
            item
            for item in (session.task_scratchpad.get("hermes_tool_loops") or [])
            if isinstance(item, dict)
        ]
        recent.append(dict(entry))
        recent = recent[-max(1, keep_last) :]
        merged = {
            **session.task_scratchpad,
            "hermes_tool_loop": dict(entry),
            "hermes_tool_loops": recent,
        }
        updated = session.model_copy(update={"task_scratchpad": merged})
        saved = self._store.upsert(updated)
        self._emit_session_event(LifecycleEventType.SESSION_SAVED, saved)
        return saved

    def _require_writable(self, session: ConversationSession) -> None:
        if session.write_stopped:
            raise ValueError(
                "session write_stopped: switch to the active branch before writing"
            )
        active = self._store.get_by_binding(
            tenant_id=session.tenant_id,
            chat_id=session.chat_id,
            user_id=session.user_id,
            project=session.project,
        )
        if active is None or active.session_id != session.session_id:
            raise ValueError(
                "session is not the active branch for this binding; switch first"
            )

    def _turn_citation(
        self,
        turn: ConversationTurn,
        *,
        answer: ProjectAnswer | None,
        trace_id: str | None,
    ) -> CitationEntry:
        evidence_ids: tuple[str, ...] = ()
        if answer is not None:
            evidence_ids = tuple(str(item.id) for item in answer.evidence)
        return CitationEntry(
            source_kind=CitationSourceKind.TURN,
            occurred_at=turn.created_at,
            short_title=make_short_title(turn.text),
            run_id=turn.run_id,
            trace_id=trace_id,
            evidence_ids=evidence_ids,
            body_snapshot=turn.text,
        )

    def _emit_session_event(
        self,
        event_type: LifecycleEventType,
        session: ConversationSession,
        *,
        run_id: UUID | None = None,
        extra: dict[str, object] | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "session_id": str(session.session_id),
            "tenant_id": session.tenant_id,
            "chat_id": session.chat_id,
            "user_id": session.user_id,
            "project_id": session.project.project_id,
            "compression_cycle": session.summary.compression_cycle,
            "recent_turn_count": len(session.recent_turns),
            "citation_count": len(session.citations),
            "has_prior_traceback": bool(
                (session.summary.active_topic or {}).get("prior_traceback")
            ),
            "active_skill": session.summary.active_skill,
            "branch_name": session.branch_name,
            "write_stopped": session.write_stopped,
            "parent_session_id": (
                str(session.parent_session_id)
                if session.parent_session_id is not None
                else None
            ),
        }
        if extra:
            payload.update(extra)
        self._lifecycle.emit(
            event_type,
            run_id=run_id or session.last_run_id,
            project=session.project,
            payload=payload,
        )


def _next_branch_name(existing: object) -> str:
    """Pick the next unused single-letter branch label (A..Z), else branch-N."""

    used = {str(name).strip() for name in existing if str(name).strip()}
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if letter not in used:
            return letter
    return f"branch-{len(used) + 1}"
