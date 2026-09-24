"""Conversation session lifecycle for multi-turn Feishu collaboration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from project_lens.context.conversation_store import ConversationStore, InMemoryConversationStore
from project_lens.domain.conversation import (
    DEFAULT_RECENT_TURN_LIMIT,
    DEFAULT_SESSION_TTL,
    ConversationSession,
    ConversationTurn,
    empty_summary,
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
    """Owns L1 recent turns and L2 summary updates. Never writes ProjectMemory."""

    def __init__(
        self,
        store: ConversationStore | None = None,
        *,
        rewriter: FollowupRewriter | None = None,
        recent_turn_limit: int = DEFAULT_RECENT_TURN_LIMIT,
        session_ttl: timedelta = DEFAULT_SESSION_TTL,
        lifecycle: LifecycleBus | None = None,
    ) -> None:
        self._store = store or InMemoryConversationStore()
        self._rewriter = rewriter or FollowupRewriter()
        self._recent_turn_limit = max(2, recent_turn_limit)
        self._session_ttl = session_ttl
        self._lifecycle = lifecycle or LifecycleBus()

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
            session_state=ProjectSessionState(
                session_id=UUID(int=0),
                project=project,
                actor_id=user_id,
            ),
            expires_at=datetime.now(timezone.utc) + self._session_ttl,
        )
        session = session.model_copy(
            update={
                "session_state": ProjectSessionState(
                    session_id=session.session_id,
                    project=project,
                    actor_id=user_id,
                )
            }
        )
        saved = self._store.upsert(session)
        self._emit_session_event(LifecycleEventType.SESSION_CREATED, saved)
        self._emit_session_event(LifecycleEventType.SESSION_SAVED, saved)
        return saved

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
    ) -> ConversationSession:
        turn = ConversationTurn(
            run_id=run_id,
            user_id=user_id,
            text=text,
            rewritten_question=rewritten_question,
        )
        state = self._ensure_session_state(session)
        recent = session.recent_turns + (turn,)
        overflow = recent[: -self._recent_turn_limit]
        kept = recent[-self._recent_turn_limit :]
        # Pin traceback into L2 immediately so restart/compress cannot drop it.
        summary = pin_prior_traceback(
            session.summary,
            text,
            rewritten_question,
        )
        if overflow:
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
            state = state.model_copy(
                update={
                    "last_compressed_turn_id": self._turn_key(overflow[-1]),
                    "current_status": "in_progress",
                }
            )
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
                },
            )
        if answer is not None:
            summary = merge_answer_into_summary(summary, answer)
            state = self._merge_answer_into_state(state, answer, summary)
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
            state = self._merge_task_state(state, merged_task)
        state = self._finalize_state(
            state,
            summary=summary,
            session=session,
            current_turn=turn,
        )
        update: dict[str, object] = {
            "recent_turns": kept,
            "summary": summary,
            "session_state": state,
            "last_run_id": run_id if run_id is not None else session.last_run_id,
            "expires_at": datetime.now(timezone.utc) + self._session_ttl,
        }
        if merged_task is not None:
            scratchpad = scratchpad_to_session_dict(merged_task)
            current_access = session.task_scratchpad.get("runtime_access")
            if current_access is not None:
                scratchpad["runtime_access"] = current_access
            update["task_scratchpad"] = scratchpad
        updated = session.model_copy(update=update)
        saved = self._store.upsert(updated)
        self._emit_session_event(
            LifecycleEventType.SESSION_SAVED,
            saved,
            run_id=run_id,
        )
        return saved

    def mark_rotation_pending(
        self,
        session: ConversationSession,
        *,
        pending: bool = True,
    ) -> ConversationSession:
        """Persist a rotation marker before a new Hermes transcript is created."""

        state = self._ensure_session_state(session).model_copy(
            update={
                "rotation_pending": pending,
                "rotation_count": (
                    self._ensure_session_state(session).rotation_count + (1 if pending else 0)
                ),
                "summary_version": self._ensure_session_state(session).summary_version + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        state = state.model_copy(update={"summary_markdown": render_session_state_markdown(state)})
        saved = self._store.upsert(session.model_copy(update={"session_state": state}))
        self._emit_session_event(
            LifecycleEventType.SESSION_SAVED,
            saved,
            extra={"rotation_pending": pending, "rotation_count": state.rotation_count},
        )
        return saved

    def build_handoff(self, session: ConversationSession):
        """Build a handoff and persist its marker for the next Hermes session."""

        state = self._ensure_session_state(session)
        handoff = build_handoff_summary(state)
        updated_state = state.model_copy(
            update={
                "last_handoff_id": handoff.handoff_id,
                "rotation_pending": False,
                "summary_version": state.summary_version + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        )
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
        if session.session_state is not None:
            return session.session_state
        return ProjectSessionState(
            session_id=session.session_id,
            project=session.project,
            actor_id=session.user_id,
        )

    def _merge_answer_into_state(
        self,
        state: ProjectSessionState,
        answer: ProjectAnswer,
        summary,
    ) -> ProjectSessionState:
        facts = list(state.confirmed_facts)
        refs = list(state.evidence_refs)
        decisions = list(state.decisions)
        for claim in answer.claims:
            candidate = SessionFactRef(
                fact_id=str(claim.id),
                text=claim.text,
                evidence_ids=tuple(str(item) for item in claim.evidence_ids),
            )
            facts = [item for item in facts if item.fact_id != candidate.fact_id]
            facts.append(candidate)
        for evidence in answer.evidence:
            refs = [item for item in refs if item.evidence_id != str(evidence.id)]
            refs.append(
                SessionEvidenceRef(
                    evidence_id=str(evidence.id),
                    source_key=f"{evidence.source.system}:{evidence.source.source_id}",
                    revision=str(evidence.metadata.get("revision") or evidence.metadata.get("version") or "") or None,
                    content_hash=evidence.content_hash,
                    authority=str(evidence.metadata.get("authority") or "approved"),
                    status=str(evidence.metadata.get("status") or "current"),
                )
            )
        for action in answer.recommended_actions:
            decision = SessionDecision(
                decision_id=str(action.id),
                text=action.title,
                rationale=action.description,
                evidence_ids=tuple(str(item) for item in answer.evidence[:8]),
                status="proposed",
            )
            decisions = [item for item in decisions if item.decision_id != decision.decision_id]
            decisions.append(decision)
        status = answer.status or "in_progress"
        return state.model_copy(
            update={
                "task_intent": state.task_intent or summary.session_intent or answer.business_summary[:500],
                "current_status": status,
                "confirmed_facts": tuple(facts[-80:]),
                "evidence_refs": tuple(refs[-80:]),
                "decisions": tuple(decisions[-40:]),
                "open_questions": tuple(dict.fromkeys((*state.open_questions, *answer.unknowns)))[-40:],
            }
        )

    @staticmethod
    def _merge_task_state(state: ProjectSessionState, task_state: TaskScratchpad) -> ProjectSessionState:
        artifacts = list(state.artifact_refs)
        for path in (*task_state.files_read, *task_state.files_changed):
            artifacts = [item for item in artifacts if item.key != path]
            artifacts.append(SessionArtifactRef(kind="file", key=path))
        for name in task_state.notes:
            if name in {"session_id", "runtime_access"}:
                continue
            value = task_state.notes[name]
            artifacts.append(SessionArtifactRef(kind="other", key=name, detail=value))
        blocked = tuple(dict.fromkeys((*state.blocked_items, *task_state.failed_attempts)))[-40:]
        return state.model_copy(update={"artifact_refs": tuple(artifacts[-80:]), "blocked_items": blocked})

    @staticmethod
    def _finalize_state(
        state: ProjectSessionState,
        *,
        summary,
        session: ConversationSession,
        current_turn: ConversationTurn,
    ) -> ProjectSessionState:
        version = state.summary_version + 1
        finalized = state.model_copy(
            update={
                "task_intent": state.task_intent or summary.session_intent,
                "open_questions": tuple(dict.fromkeys((*state.open_questions, *summary.unknowns)))[-40:],
                "artifact_refs": tuple(
                    list(state.artifact_refs)
                    + [SessionArtifactRef(kind="run", key=str(current_turn.run_id))]
                    if current_turn.run_id
                    else list(state.artifact_refs)
                )[-80:],
                "summary_version": version,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        return finalized.model_copy(update={"summary_markdown": render_session_state_markdown(finalized)})

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
            "has_prior_traceback": bool(
                (session.summary.active_topic or {}).get("prior_traceback")
            ),
            "active_skill": session.summary.active_skill,
        }
        if extra:
            payload.update(extra)
        self._lifecycle.emit(
            event_type,
            run_id=run_id or session.last_run_id,
            project=session.project,
            payload=payload,
        )
