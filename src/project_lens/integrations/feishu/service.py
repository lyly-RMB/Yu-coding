"""Feishu event handling service."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import BackgroundTasks, HTTPException, status
from pydantic import ValidationError

from project_lens.application.conversation_service import ConversationService
from project_lens.application.hermes_runtime import (
    HermesRuntimeService,
    PendingHermesExecution,
)
from project_lens.application.message_access_context import MessageAccessContext
from project_lens.application.feishu_doc_sync_status import FeishuDocSyncStatusStore
from project_lens.application.memory_service import propose_memory_from_answer
from project_lens.application.run_service import RunService
from project_lens.application.project_agent_run_detail import (
    ProjectAgentRunDetailRequest,
    ProjectAgentRunDetailService,
)
from project_lens.application.risk_engine import RiskEngine
from project_lens.application.risk_feedback_service import (
    RiskFeedbackService,
    feedback_idempotency_key,
    parse_risk_feedback_text,
)
from project_lens.application.risk_notification_service import RiskNotificationService
from project_lens.application.knowledge_gap_service import KnowledgeGapService
from project_lens.application.knowledge_gap_recheck import KnowledgeGapRecheckService
from project_lens.domain.knowledge_gap import GapActionType, KnowledgeGapStatus
from project_lens.context.memory_store import MemoryStore
from project_lens.context.memory_retrieval import build_memory_summary
from project_lens.context.memory_authorization import authorize_memory_candidates
from project_lens.domain.conversation import ConversationSession
from project_lens.domain.feishu_doc_sync import FeishuDocSyncStatus
from project_lens.domain.memory import MemoryProposal, ProjectMemory
from project_lens.domain.identity import ActorContext
from project_lens.domain.models import ProjectRef, RunStatus
from project_lens.integrations.feishu.adapter import FeishuMessenger
from project_lens.integrations.feishu.audiences import AnswerAudience, detect_audience_switch
from project_lens.integrations.feishu.cards import (
    card_payload_as_text,
    format_answer_as_text,
    format_failure_as_text,
    render_about_bot_card,
    render_answer_card,
    render_context_date_window_card,
    render_context_edit_card,
    render_context_more_history_card,
    render_context_preview_card,
    render_feishu_doc_sync_status_card,
    render_memory_decision_card,
    render_run_detail_card,
    render_risk_feedback_result_card,
    decode_group_page_stack,
)
from project_lens.integrations.feishu.commands import (
    FeishuProjectCommand,
    classify_feishu_command,
    is_context_manager_command,
    rewrite_feishu_message,
    strip_context_manager_prefix,
)
from project_lens.integrations.feishu.idempotency import EventDeduplicator
from project_lens.integrations.feishu.identity import FeishuRunContext
from project_lens.integrations.feishu.hermes_tool_loop import (
    FeishuHermesToolLoopBridge,
    hermes_loop_scratchpad_entry,
)
from project_lens.integrations.feishu.hermes_context import build_hermes_project_context
from project_lens.integrations.feishu.virtual_members import VirtualMemberController
from project_lens.integrations.feishu.ingress import (
    FeishuIngressKind,
    FeishuIngressRouter,
    strip_project_debug_slash,
)
from project_lens.integrations.feishu.models import FeishuEventCallback
from project_lens.integrations.feishu.security import FeishuRequestVerifier
from project_lens.project_space.policies import EffectiveAccessScope, ProjectRuntimeContextResolver
from project_lens.runtime.lifecycle import LifecycleBus, LifecycleEventType
from project_lens.runtime.memory_approval_gateway import MemoryApprovalGateway
from project_lens.workflow.followup import FollowupRewriter
from project_lens.workflow.task_scratchpad import scratchpad_from_sync_statuses

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeishuCallbackResult:
    status: str
    run_id: str | None = None
    challenge: str | None = None
    proposal_id: str | None = None
    card: dict[str, Any] | None = None
    toast_type: str | None = None
    toast_content: str | None = None
    session_id: str | None = None
    pending: bool = False

    def to_response(self) -> dict[str, Any]:
        if self.challenge is not None and self.status == "verified":
            return {"challenge": self.challenge}
        response: dict[str, Any] = {"status": self.status}
        if self.run_id:
            response["run_id"] = self.run_id
        if self.session_id:
            response["session_id"] = self.session_id
        if self.pending:
            response["pending"] = True
        if self.proposal_id:
            response["proposal_id"] = self.proposal_id
        if self.card is not None:
            response["card"] = {"type": "raw", "data": self.card}
        if self.toast_content:
            response["toast"] = {
                "type": self.toast_type or "info",
                "content": self.toast_content[:100],
            }
        return response


class FeishuEventService:
    def __init__(
        self,
        *,
        run_service: RunService,
        verifier: FeishuRequestVerifier,
        deduplicator: EventDeduplicator,
        identity_mapper: object,
        messenger: FeishuMessenger,
        memory_store: MemoryStore | None = None,
        memory_approval_gateway: MemoryApprovalGateway | None = None,
        sync_status_store: FeishuDocSyncStatusStore | None = None,
        conversation_service: ConversationService | None = None,
        lifecycle: LifecycleBus | None = None,
        runtime_context_resolver: ProjectRuntimeContextResolver | None = None,
        hermes_tool_loop_bridge: FeishuHermesToolLoopBridge | None = None,
        hermes_runtime: HermesRuntimeService | None = None,
        run_detail_service: ProjectAgentRunDetailService | None = None,
        risk_feedback_service: RiskFeedbackService | None = None,
        risk_engine: RiskEngine | None = None,
        risk_notification_service: RiskNotificationService | None = None,
        knowledge_gap_service: KnowledgeGapService | None = None,
        knowledge_gap_recheck_service: KnowledgeGapRecheckService | None = None,
        virtual_members: VirtualMemberController | None = None,
        context_preview_enabled: bool = True,
    ) -> None:
        self._run_service = run_service
        self._verifier = verifier
        self._deduplicator = deduplicator
        self._identity_mapper = identity_mapper
        self._messenger = messenger
        self._memory_store = memory_store
        if memory_approval_gateway is not None:
            self._memory_gateway = memory_approval_gateway
        elif memory_store is not None:
            self._memory_gateway = MemoryApprovalGateway(memory_store)
        else:
            self._memory_gateway = None
        self._sync_status_store = sync_status_store
        self._lifecycle = lifecycle or LifecycleBus()
        self._conversation = conversation_service or ConversationService(
            lifecycle=self._lifecycle
        )
        self._runtime_context_resolver = runtime_context_resolver
        self._hermes_tool_loop_bridge = hermes_tool_loop_bridge
        self._hermes_runtime = hermes_runtime or (
            HermesRuntimeService(run_service=run_service, bridge=hermes_tool_loop_bridge)
            if hermes_tool_loop_bridge is not None
            else None
        )
        self._run_detail_service = run_detail_service
        self._risk_feedback = risk_feedback_service
        self._risk_engine = risk_engine
        self._risk_notifications = risk_notification_service
        self._knowledge_gaps = knowledge_gap_service
        self._knowledge_gap_rechecks = knowledge_gap_recheck_service
        self._virtual_members = virtual_members or VirtualMemberController()
        self._virtual_members = virtual_members
        self._context_preview_enabled = bool(context_preview_enabled)
        self._followup = FollowupRewriter()
        self._ingress = FeishuIngressRouter()
        # Each conversation has one live answer generation.  A newer message
        # advances the generation and makes all older background jobs stale.
        self._latest_answer_generation: dict[str, int] = {}
        # Serialize Hermes replies per Feishu chat. Events can arrive faster
        # than the model responds; queueing prevents overlapping runs and
        # keeps the visible conversation order deterministic.
        self._chat_answer_locks: dict[str, asyncio.Lock] = {}

    def _begin_latest_answer(self, chat_id: str) -> int:
        generation = self._latest_answer_generation.get(chat_id, 0) + 1
        self._latest_answer_generation[chat_id] = generation
        return generation

    def _is_latest_answer(self, chat_id: str, generation: int) -> bool:
        return self._latest_answer_generation.get(chat_id) == generation

    def _run_detail(self, request: ProjectAgentRunDetailRequest) -> dict[str, Any]:
        if self._run_detail_service is None:
            raise RuntimeError("run_detail_service is not configured")
        return self._run_detail_service.run_detail(request)

    def handle_callback(
        self,
        *,
        body: bytes,
        timestamp: str | None,
        nonce: str | None,
        signature: str | None,
        background_tasks: BackgroundTasks,
    ) -> FeishuCallbackResult:
        signature_required = self._verifier.requires_signature(
            timestamp=timestamp,
            nonce=nonce,
            signature=signature,
        )
        if signature_required and not self._verifier.verify_signature(
            body=body,
            timestamp=timestamp,
            nonce=nonce,
            signature=signature,
        ):
            logger.warning("feishu signature verification failed")
            raise HTTPException(status_code=401, detail="invalid Feishu request signature")
        try:
            decoded_body = self._verifier.decode_body(body)
            raw_payload: dict[str, Any] = json.loads(decoded_body)
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid Feishu callback payload",
            ) from exc

        if raw_payload.get("type") == "url_verification":
            if not signature_required and not self._verifier.verify_token(
                str(raw_payload.get("token") or "")
            ):
                raise HTTPException(status_code=401, detail="invalid Feishu verification token")
            challenge = str(raw_payload.get("challenge") or "")
            if not challenge:
                raise HTTPException(status_code=400, detail="missing Feishu challenge")
            return FeishuCallbackResult(status="verified", challenge=challenge)

        header = raw_payload.get("header") or {}
        event_type = str(header.get("event_type") or "")
        if event_type == "card.action.trigger":
            if (
                self._verifier._signing_secret
                and not signature_required
            ):
                raise HTTPException(status_code=401, detail="missing Feishu request signature")
            if not signature_required and not self._verifier.verify_token(
                str(raw_payload.get("token") or "")
            ):
                raise HTTPException(status_code=401, detail="invalid Feishu verification token")
            return self._handle_card_action(raw_payload, background_tasks=background_tasks)

        try:
            payload = FeishuEventCallback.model_validate(raw_payload)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid Feishu callback payload",
            ) from exc
        if (
            self._verifier._signing_secret
            and not signature_required
            and not payload.is_url_verification
        ):
            raise HTTPException(status_code=401, detail="missing Feishu request signature")
        if not signature_required and not self._verifier.verify_token(payload.token):
            logger.warning("feishu verification token rejected")
            raise HTTPException(status_code=401, detail="invalid Feishu verification token")
        if payload.is_url_verification:
            logger.info("feishu url verification received")
            return FeishuCallbackResult(status="verified", challenge=payload.challenge)
        if payload.header is None or payload.event is None:
            raise HTTPException(status_code=400, detail="missing Feishu event payload")
        if not self._deduplicator.mark_seen(payload.header.event_id):
            return FeishuCallbackResult(status="duplicate")
        if payload.header.event_type != "im.message.receive_v1":
            logger.info("feishu event ignored: type=%s", payload.header.event_type)
            return FeishuCallbackResult(status="ignored")

        message = payload.event.message
        text = message.text()
        logger.info(
            "feishu message event tenant_key=%s chat_id=%s text_len=%d",
            payload.header.tenant_key,
            message.chat_id,
            len(text),
        )
        if not text:
            return FeishuCallbackResult(status="ignored")

        natural_feedback = parse_risk_feedback_text(text)
        if natural_feedback is not None and self._risk_feedback is not None:
            risk_id, feedback_action, reason = natural_feedback
            actor_id = str(payload.event.sender.sender_id.open_id or "").strip()
            if not actor_id:
                raise HTTPException(status_code=400, detail="missing Feishu sender open_id")
            try:
                result = self._risk_feedback.submit(
                    risk_id=risk_id,
                    actor_id=actor_id,
                    action=feedback_action,
                    idempotency_key=feedback_idempotency_key(
                        event_id=payload.header.event_id,
                        risk_id=risk_id,
                        actor_id=actor_id,
                    ),
                    reason=reason,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="risk not found") from exc
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            finding = self._risk_engine.get(risk_id) if self._risk_engine else None
            if finding is not None:
                background_tasks.add_task(
                    self._messenger.post_user_card,
                    actor_id,
                    render_risk_feedback_result_card(
                        finding,
                        action=result.feedback.action.value,
                        duplicate=result.duplicate,
                    ),
                )
                if (
                    result.feedback.action.value == "request_help"
                    and self._risk_notifications is not None
                ):
                    background_tasks.add_task(
                        self._risk_notifications.request_help,
                        finding,
                        actor_id=actor_id,
                    )
            return FeishuCallbackResult(status="accepted")

        tenant_key = payload.header.tenant_key.strip()
        if not tenant_key:
            raise HTTPException(status_code=400, detail="missing Feishu tenant_key")

        open_id = payload.event.sender.sender_id.open_id
        if not open_id:
            raise HTTPException(status_code=400, detail="missing Feishu sender open_id")

        chat_id = message.chat_id.strip()
        if not chat_id:
            raise HTTPException(status_code=400, detail="missing Feishu chat_id")

        chat_type = message.chat_type
        if chat_type not in {"p2p", "group"}:
            raise HTTPException(status_code=400, detail="missing or invalid Feishu chat_type")

        try:
            context: FeishuRunContext = self._identity_mapper.resolve(
                tenant_key=tenant_key,
                chat_id=chat_id,
                user_id=open_id,
                chat_type=chat_type,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

        sim = self._virtual_members.parse(text)
        if sim is not None:
            action, payload = sim
            if action in {"help", "list", "off", "whoami", "invalid"}:
                background_tasks.add_task(self._messenger.post_text, chat_id, self._virtual_members.handle(physical_id=open_id, chat_id=chat_id, command=sim))
                return FeishuCallbackResult(status="accepted")
            response = self._virtual_members.handle(physical_id=open_id, chat_id=chat_id, command=sim)
            effective = self._virtual_members.effective_actor(physical_id=open_id, chat_id=chat_id)
            if effective and payload:
                text = payload
            else:
                background_tasks.add_task(self._messenger.post_text, chat_id, response)
                return FeishuCallbackResult(status="accepted")

        # Virtual members change ACL / role view only. Conversation continuity must
        # stay on the real Feishu sender so /sim product → developer → qa → plain
        # "summarize the discussion" share one session (PL-FEISHU-001).
        physical_user_id = open_id
        effective_actor_id = self._virtual_members.effective_actor(physical_id=open_id, chat_id=chat_id)
        if effective_actor_id:
            context = FeishuRunContext(
                project=context.project,
                user_id=effective_actor_id,
                channel_id=context.channel_id,
                actor=type(context.actor)(
                    tenant_key=context.actor.tenant_key,
                    actor_id=effective_actor_id,
                    chat_id=context.actor.chat_id,
                    chat_type=context.actor.chat_type,
                    source="test_fixture",
                    authenticated=True,
                ),
            )

        session = self._conversation.get_or_create(
            tenant_id=context.project.tenant_id,
            chat_id=message.chat_id,
            user_id=physical_user_id,
            project=context.project,
        )
        message_access = self._resolve_message_access(
            context,
            message_id=message.message_id,
        )
        session, _runtime_access = self._attach_runtime_access(session, message_access)
        context_requested = is_context_manager_command(text)
        routed_text = strip_context_manager_prefix(text) if context_requested else text
        question_for_run, followup_rewrite = self._conversation.prepare_question(
            session, routed_text
        )
        ingress = self._ingress.decide(
            routed_text,
            session=session,
            followup_rewrite=followup_rewrite,
        )
        if classify_feishu_command(text) == FeishuProjectCommand.RESET_CONVERSATION:
            self._latest_answer_generation[chat_id] = (
                self._latest_answer_generation.get(chat_id, 0) + 1
            )
            self._conversation.store.delete(session)
            background_tasks.add_task(
                self._messenger.post_text,
                chat_id,
                "当前对话已清空，下一条消息将开启新对话。",
            )
            return FeishuCallbackResult(status="accepted")
        if followup_rewrite is None:
            text_for_rewrite = (
                strip_project_debug_slash(text)
                if ingress.kind == FeishuIngressKind.DEBUG_PROJECT_COMMAND
                else routed_text
            )
            question_for_run = rewrite_feishu_message(text_for_rewrite)
        audit_rewrite = None if question_for_run == text else question_for_run

        # RoleView switch: re-render the same ProjectAnswer — no new fact generation.
        role_view = self._try_role_view_switch(
            session=session,
            text=text,
            user_id=context.user_id,
            chat_id=message.chat_id,
            background_tasks=background_tasks,
        )
        if role_view is not None:
            return role_view

        # Sync-status shortcut (command or follow-up): no AgentRun; still update session.
        if (
            ingress.kind == FeishuIngressKind.DOC_SYNC_STATUS
            or self._followup.is_sync_status_followup(text)
        ):
            sync_statuses: tuple[FeishuDocSyncStatus, ...] = ()
            if self._sync_status_store is not None:
                try:
                    sync_statuses = self._sync_status_store.list_for_project(context.project)
                except Exception:  # noqa: BLE001 - status read must not break chat routing
                    logger.exception("feishu doc sync status read failed during session update")
                    sync_statuses = ()
            self._conversation.record_turn(
                session,
                user_id=context.user_id,
                text=text,
                rewritten_question=audit_rewrite,
                task_state=scratchpad_from_sync_statuses(sync_statuses),
            )
            background_tasks.add_task(
                self._reply_doc_sync_status,
                context.project,
                message.chat_id,
            )
            return FeishuCallbackResult(status="accepted")

        if ingress.creates_project_run:
            return self._dispatch_project_question(
                session=session,
                context=context,
                message_access=message_access,
                chat_id=message.chat_id,
                question=routed_text,
                question_for_run=question_for_run,
                background_tasks=background_tasks,
                force_context_preview=context_requested,
            )

        # Bot/meta questions (e.g. 你用的是什么模型) — never project Skill analysis.
        if ingress.kind == FeishuIngressKind.BOT_META:
            self._conversation.record_turn(
                session,
                user_id=context.user_id,
                text=text,
                rewritten_question=None,
            )
            background_tasks.add_task(
                self._reply_about_bot,
                message.chat_id,
                text,
            )
            return FeishuCallbackResult(status="accepted")

        raise RuntimeError(f"unsupported Feishu ingress: {ingress.kind}")

    def _resolve_message_access(
        self,
        context: FeishuRunContext,
        *,
        message_id: str | None = None,
    ) -> MessageAccessContext:
        if self._runtime_context_resolver is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="runtime context resolver is not configured",
            )
        try:
            return MessageAccessContext.resolve_for_actor(
                actor=context.actor,
                project_id=context.project.project_id,
                tenant_id=context.project.tenant_id,
                resolver=self._runtime_context_resolver,
                message_id=message_id,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    def _attach_runtime_access(
        self,
        session: ConversationSession,
        message_access: MessageAccessContext,
    ) -> tuple[ConversationSession, dict[str, object]]:
        """Persist per-message scope audit bounds; do not reuse across speakers."""

        runtime_access = message_access.runtime_access_audit()
        saved = self._conversation.attach_runtime_access(session, runtime_access)
        return saved, runtime_access

    def _handle_card_action(
        self,
        raw_payload: dict[str, Any],
        *,
        background_tasks: BackgroundTasks,
    ) -> FeishuCallbackResult:
        header = raw_payload.get("header") or {}
        event_id = str(header.get("event_id") or "")
        tenant_key = str(header.get("tenant_key") or "")
        if not event_id:
            raise HTTPException(status_code=400, detail="missing Feishu card event id")
        if not self._deduplicator.mark_seen(event_id):
            return FeishuCallbackResult(status="duplicate")

        event = raw_payload.get("event") or {}
        action = event.get("action") or {}
        value = action.get("value") or {}
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = {}
        action_name = str(value.get("action") or "")

        operator = event.get("operator") or {}
        decided_by = str(
            operator.get("user_id")
            or operator.get("open_id")
            or "feishu-user"
        )
        context = event.get("context") or {}
        chat_id = str(
            context.get("open_chat_id")
            or context.get("chat_id")
            or event.get("open_chat_id")
            or ""
        )

        if action_name == "ask_question":
            question = str(value.get("question") or "").strip()
            if not question or not chat_id:
                return FeishuCallbackResult(status="ignored")
            operator_open_id = str(operator.get("open_id") or "").strip()
            if not operator_open_id:
                raise HTTPException(status_code=400, detail="missing Feishu operator open_id")
            if not tenant_key.strip():
                raise HTTPException(status_code=400, detail="missing Feishu tenant_key")
            result = self._start_question_from_card(
                tenant_key=tenant_key.strip(),
                chat_id=chat_id,
                user_id=operator_open_id,
                text=question,
                background_tasks=background_tasks,
                from_card=True,
            )
            return FeishuCallbackResult(
                status=result.status,
                run_id=result.run_id,
                session_id=result.session_id,
                pending=result.pending,
                card=(
                    self._context_preview_card_payload(session)
                    if result.session_id
                    and (session := self._conversation.store.get(UUID(result.session_id))) is not None
                    else None
                ),
            )

        if action_name == "context_direct_answer":
            return self._handle_context_direct_answer(
                value=value,
                tenant_key=tenant_key.strip(),
                chat_id=chat_id,
                operator=operator,
                context=context,
                background_tasks=background_tasks,
            )

        if action_name == "context_exclude_item":
            return self._handle_context_exclude_item(
                value=value,
                chat_id=chat_id,
                background_tasks=background_tasks,
            )

        if action_name == "context_edit":
            return self._handle_context_edit(
                value=value,
                chat_id=chat_id,
                background_tasks=background_tasks,
            )

        if action_name == "context_more_history":
            return self._handle_context_more_history(
                value=value,
                chat_id=chat_id,
                background_tasks=background_tasks,
            )

        if action_name == "context_confirm_edit":
            return self._handle_context_confirm_edit(
                value=value,
                chat_id=chat_id,
                background_tasks=background_tasks,
            )

        if action_name == "context_back_preview":
            return self._handle_context_back_preview(
                value=value,
                chat_id=chat_id,
                background_tasks=background_tasks,
            )

        if action_name == "context_include_citation":
            return self._handle_context_include_citation(
                value=value,
                chat_id=chat_id,
                background_tasks=background_tasks,
            )

        if action_name == "context_select_history":
            return self._handle_context_select_history(
                value=value,
                chat_id=chat_id,
                background_tasks=background_tasks,
            )

        if action_name == "context_select_group_message":
            return self._handle_context_select_group_message(
                value=value,
                chat_id=chat_id,
                background_tasks=background_tasks,
            )

        if action_name == "context_restore_item":
            return self._handle_context_restore_item(
                value=value,
                chat_id=chat_id,
                background_tasks=background_tasks,
            )

        if action_name == "context_fork_branch":
            return self._handle_context_fork_branch(
                value=value,
                chat_id=chat_id,
                background_tasks=background_tasks,
            )

        if action_name == "context_switch_branch":
            return self._handle_context_switch_branch(
                value=value,
                chat_id=chat_id,
                background_tasks=background_tasks,
            )

        if action_name == "context_delete_branch":
            return self._handle_context_delete_branch(
                value=value,
                chat_id=chat_id,
                background_tasks=background_tasks,
            )

        if action_name == "context_open_date_window":
            return self._handle_context_open_date_window(
                value=value,
                chat_id=chat_id,
                background_tasks=background_tasks,
            )

        if action_name in {"context_set_date_start", "context_set_date_end"}:
            option = str(action.get("option") or value.get("option") or "").strip()
            return self._handle_context_set_date_bound(
                value=value,
                bound="start" if action_name.endswith("start") else "end",
                option=option,
                chat_id=chat_id,
                background_tasks=background_tasks,
            )

        if action_name == "context_confirm_date_window":
            return self._handle_context_confirm_date_window(
                value=value,
                chat_id=chat_id,
                background_tasks=background_tasks,
            )

        if action_name == "context_clear_date_window":
            return self._handle_context_clear_date_window(
                value=value,
                chat_id=chat_id,
                background_tasks=background_tasks,
            )

        if action_name in {"context_edit_placeholder", "context_more_history_placeholder"}:
            # Compat for stale cards still showing S03 placeholders.
            if action_name == "context_edit_placeholder":
                return self._handle_context_edit(
                    value={**value, "action": "context_edit"},
                    chat_id=chat_id,
                    background_tasks=background_tasks,
                )
            return self._handle_context_more_history(
                value={**value, "action": "context_more_history", "offset": "0"},
                chat_id=chat_id,
                background_tasks=background_tasks,
            )

        if action_name == "projectlens_run_detail":
            run_id_raw = str(value.get("run_id") or "").strip()
            operator_open_id = str(operator.get("open_id") or "").strip()
            if not run_id_raw or not operator_open_id or not chat_id or not tenant_key.strip():
                return FeishuCallbackResult(status="ignored")
            try:
                run_id = UUID(run_id_raw)
            except ValueError:
                raise HTTPException(status_code=400, detail="invalid run_id")
            try:
                actor_context = self._identity_mapper.resolve(
                    tenant_key=tenant_key.strip(),
                    chat_id=chat_id,
                    user_id=operator_open_id,
                    chat_type=str(context.get("chat_type") or "group"),
                )
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            detail = self._run_detail(
                ProjectAgentRunDetailRequest(
                    run_id=run_id,
                    actor=actor_context.actor,
                    audience="team",
                )
            )
            background_tasks.add_task(
                self._messenger.post_card,
                chat_id,
                render_run_detail_card(detail),
            )
            return FeishuCallbackResult(status="accepted", run_id=run_id_raw)

        if action_name.startswith("knowledge_gap_"):
            if self._knowledge_gaps is None:
                return FeishuCallbackResult(status="ignored")
            gap_id_raw = str(value.get("gap_id") or "").strip()
            operator_open_id = str(operator.get("open_id") or "").strip()
            if not gap_id_raw or not operator_open_id:
                raise HTTPException(status_code=400, detail="knowledge-gap action requires gap_id and operator open_id")
            try:
                gap_id = UUID(gap_id_raw)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="invalid gap_id") from exc
            case = self._knowledge_gaps.get_case(gap_id)
            if case is None:
                raise HTTPException(status_code=404, detail="knowledge gap not found")
            if not chat_id or not tenant_key:
                raise HTTPException(status_code=400, detail="knowledge-gap action requires Feishu tenant and chat context")
            try:
                actor_context = self._identity_mapper.resolve(
                    tenant_key=tenant_key,
                    chat_id=chat_id,
                    user_id=operator_open_id,
                    chat_type=str(context.get("chat_type") or "group"),
                )
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail="actor is not authorized for this project") from exc
            if actor_context.project != case.project:
                raise HTTPException(status_code=404, detail="knowledge gap not found")
            try:
                if action_name == "knowledge_gap_accept":
                    result = self._knowledge_gaps.propose_action(gap_id, action_type=GapActionType.ADD_SOURCE, proposed_by=operator_open_id, description="Feishu owner accepted knowledge gap")
                elif action_name == "knowledge_gap_recheck":
                    if self._knowledge_gap_rechecks is None:
                        raise HTTPException(status_code=503, detail="knowledge-gap recheck is not configured")
                    access = self._resolve_message_access(actor_context)
                    if case.status in {KnowledgeGapStatus.WAITING_FOR_SOURCE, KnowledgeGapStatus.REOPENED}:
                        case = self._knowledge_gaps.transition(
                            gap_id, KnowledgeGapStatus.RESOLVED_PENDING_CHECK,
                            actor_id=operator_open_id,
                            reason="Authorized Feishu operator requested recheck",
                        )
                    if case.status not in {KnowledgeGapStatus.RESOLVED_PENDING_CHECK, KnowledgeGapStatus.RESOLVED}:
                        raise HTTPException(status_code=409, detail="knowledge gap must be waiting for source before recheck")
                    background_tasks.add_task(
                        self._run_knowledge_gap_recheck,
                        gap_id,
                        actor_context.actor,
                        access.effective_scope,
                        case.project,
                        chat_id,
                    )
                elif action_name == "knowledge_gap_recheck_source_updates":
                    if self._knowledge_gap_rechecks is None:
                        raise HTTPException(status_code=503, detail="knowledge-gap recheck is not configured")
                    access = self._resolve_message_access(actor_context)
                    background_tasks.add_task(
                        self._run_source_update_rechecks,
                        actor_context.actor,
                        access.effective_scope,
                        case.project,
                        chat_id,
                    )
                    return FeishuCallbackResult(status="accepted")
                elif action_name == "knowledge_gap_snooze":
                    result = self._knowledge_gaps.transition(gap_id, KnowledgeGapStatus.SNOOZED, actor_id=operator_open_id, reason="Snoozed from Feishu card")
                elif action_name == "knowledge_gap_dismiss":
                    result = self._knowledge_gaps.transition(gap_id, KnowledgeGapStatus.DISMISSED, actor_id=operator_open_id, reason="Dismissed from Feishu card")
                else:
                    return FeishuCallbackResult(status="ignored")
            except (KeyError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if chat_id:
                background_tasks.add_task(self._messenger.post_user_card, chat_id, {"msg_type": "interactive", "card": {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": f"知识缺口已更新：`{gap_id}`\n操作：{action_name}"}]}}})
            return FeishuCallbackResult(status="accepted")

        if action_name.startswith("risk_"):
            if self._risk_feedback is None or self._risk_engine is None:
                return FeishuCallbackResult(status="ignored")
            risk_id = str(value.get("risk_id") or "").strip()
            operator_open_id = str(operator.get("open_id") or "").strip()
            if not risk_id or not operator_open_id:
                raise HTTPException(
                    status_code=400,
                    detail="risk feedback requires risk_id and operator open_id",
                )
            raw_action = action_name.removeprefix("risk_")
            reason = str(value.get("reason") or "").strip()
            try:
                result = self._risk_feedback.submit(
                    risk_id=risk_id,
                    actor_id=operator_open_id,
                    action=raw_action,
                    idempotency_key=feedback_idempotency_key(
                        event_id=event_id,
                        risk_id=risk_id,
                        actor_id=operator_open_id,
                    ),
                    reason=reason,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="risk not found") from exc
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            finding = self._risk_engine.get(risk_id)
            if finding is not None:
                background_tasks.add_task(
                    self._messenger.post_user_card,
                    operator_open_id,
                    render_risk_feedback_result_card(
                        finding,
                        action=result.feedback.action.value,
                        duplicate=result.duplicate,
                    ),
                )
                if (
                    result.feedback.action.value == "request_help"
                    and self._risk_notifications is not None
                ):
                    background_tasks.add_task(
                        self._risk_notifications.request_help,
                        finding,
                        actor_id=operator_open_id,
                    )
            return FeishuCallbackResult(status="accepted")

        if action_name not in {"memory_approve", "memory_reject"}:
            return FeishuCallbackResult(status="ignored")
        proposal_id_raw = str(value.get("proposal_id") or "")
        if not proposal_id_raw:
            return FeishuCallbackResult(status="ignored")
        if self._memory_gateway is None:
            return FeishuCallbackResult(status="ignored")

        try:
            proposal_id = UUID(proposal_id_raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid proposal_id") from exc

        try:
            proposal, memory = self._memory_gateway.decide_memory_proposal(
                proposal_id,
                approved=action_name == "memory_approve",
                decided_by=decided_by,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="proposal not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        self._lifecycle.emit(
            LifecycleEventType.APPROVAL_DECIDED,
            project=proposal.project,
            payload={
                "proposal_id": str(proposal.id),
                "decision": proposal.status,
                "decided_by": decided_by,
                "memory_gateway": self._memory_gateway.audit_summary(),
            },
        )
        if proposal.status == "approved":
            self._lifecycle.emit(
                LifecycleEventType.MEMORY_APPROVED,
                project=proposal.project,
                payload={
                    "proposal_id": str(proposal.id),
                    "memory_id": str(memory.id) if memory is not None else None,
                    "decided_by": decided_by,
                },
            )
        elif proposal.status == "rejected":
            self._lifecycle.emit(
                LifecycleEventType.MEMORY_REJECTED,
                project=proposal.project,
                payload={
                    "proposal_id": str(proposal.id),
                    "decided_by": decided_by,
                },
            )

        if chat_id:
            background_tasks.add_task(
                self._post_memory_decision,
                chat_id,
                proposal,
                memory,
            )
        return FeishuCallbackResult(status="accepted", proposal_id=str(proposal.id))

    def _start_question_from_card(
        self,
        *,
        tenant_key: str,
        chat_id: str,
        user_id: str,
        text: str,
        background_tasks: BackgroundTasks,
        from_card: bool = False,
        chat_type: str = "group",
    ) -> FeishuCallbackResult:
        if chat_type not in {"p2p", "group"}:
            chat_type = "group"
        physical_user_id = user_id
        try:
            context: FeishuRunContext = self._identity_mapper.resolve(
                tenant_key=tenant_key,
                chat_id=chat_id,
                user_id=user_id,
                chat_type=chat_type,  # type: ignore[arg-type]
            )
        except PermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

        effective_actor_id = self._virtual_members.effective_actor(
            physical_id=physical_user_id, chat_id=chat_id
        )
        if effective_actor_id:
            context = FeishuRunContext(
                project=context.project,
                user_id=effective_actor_id,
                channel_id=context.channel_id,
                actor=type(context.actor)(
                    tenant_key=context.actor.tenant_key,
                    actor_id=effective_actor_id,
                    chat_id=context.actor.chat_id,
                    chat_type=context.actor.chat_type,
                    source="test_fixture",
                    authenticated=True,
                ),
            )

        session = self._conversation.get_or_create(
            tenant_id=context.project.tenant_id,
            chat_id=chat_id,
            user_id=physical_user_id,
            project=context.project,
        )
        message_access = self._resolve_message_access(context)
        session, _runtime_access = self._attach_runtime_access(session, message_access)
        context_requested = is_context_manager_command(text)
        routed_text = strip_context_manager_prefix(text) if context_requested else text
        question_for_run, followup_rewrite = self._conversation.prepare_question(
            session, routed_text
        )
        ingress = self._ingress.decide(
            routed_text,
            session=session,
            followup_rewrite=followup_rewrite,
        )
        if followup_rewrite is None:
            text_for_rewrite = (
                strip_project_debug_slash(text)
                if ingress.kind == FeishuIngressKind.DEBUG_PROJECT_COMMAND
                else routed_text
            )
            question_for_run = rewrite_feishu_message(text_for_rewrite)
        role_view = self._try_role_view_switch(
            session=session,
            text=text,
            user_id=context.user_id,
            chat_id=chat_id,
            background_tasks=background_tasks,
        )
        if role_view is not None and not from_card:
            return role_view

        if (
            ingress.kind == FeishuIngressKind.DOC_SYNC_STATUS
            or self._followup.is_sync_status_followup(text)
        ):
            background_tasks.add_task(
                self._reply_doc_sync_status,
                context.project,
                chat_id,
            )
            return FeishuCallbackResult(status="accepted")

        return self._dispatch_project_question(
            session=session,
            context=context,
            message_access=message_access,
            chat_id=chat_id,
            question=routed_text,
            question_for_run=question_for_run,
            background_tasks=background_tasks,
            force_context_preview=context_requested,
        )

    def _dispatch_project_question(
        self,
        *,
        session: ConversationSession,
        context: FeishuRunContext,
        message_access: MessageAccessContext,
        chat_id: str,
        question: str,
        question_for_run: str,
        background_tasks: BackgroundTasks,
        force_context_preview: bool = False,
    ) -> FeishuCallbackResult:
        generation = self._begin_latest_answer(chat_id)

        session = self._conversation.refresh_pending_send(
            session,
            question=question,
            question_for_run=question_for_run,
        )
        pending_state = session.pending_send
        assert pending_state is not None
        if self._context_preview_enabled or force_context_preview:
            background_tasks.add_task(
                self._messenger.post_card,
                chat_id,
                self._context_preview_card_payload(session),
            )
            return FeishuCallbackResult(
                status="accepted", session_id=str(session.session_id), pending=True
            )

        run_question = pending_state.question_for_run or pending_state.question
        if not run_question.strip():
            return FeishuCallbackResult(status="ignored")
        try:
            pending = self._prepare_hermes_execution(
                project=context.project,
                actor=context.actor,
                scope=message_access.effective_scope,
                question=run_question,
                entry_mode="feishu_context_preview_bypass",
                session=session,
                use_pending_send=False,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # Ack first so Feishu is not silent while Hermes (often 30s–minutes) runs.
        # Keep ack + Hermes in ONE background task: Starlette runs BackgroundTasks
        # sequentially, so a hung/slow ack must not delay or skip Hermes.
        background_tasks.add_task(
            self._ack_and_execute_prepared_hermes_tool_loop_and_reply,
            pending,
            chat_id,
            generation,
            session.session_id,
            pending_state.question or run_question,
            None if run_question == pending_state.question else run_question,
        )
        return FeishuCallbackResult(
            status="accepted",
            run_id=str(pending.run.id),
            session_id=str(session.session_id),
        )

    def _handle_context_direct_answer(
        self,
        *,
        value: dict[str, Any],
        tenant_key: str,
        chat_id: str,
        operator: dict[str, Any],
        context: dict[str, Any],
        background_tasks: BackgroundTasks,
    ) -> FeishuCallbackResult:
        """Confirm preview: assemble Hermes from pending-send only."""

        session_id_raw = str(value.get("session_id") or "").strip()
        operator_open_id = str(operator.get("open_id") or "").strip()
        if not session_id_raw or not operator_open_id or not chat_id or not tenant_key:
            return FeishuCallbackResult(status="ignored")
        try:
            session_id = UUID(session_id_raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid session_id") from None

        session = self._conversation.store.get(session_id)
        if session is None or session.pending_send is None:
            raise HTTPException(
                status_code=409,
                detail="pending_send missing; @ again to refresh preview",
            )
        if session.write_stopped:
            raise HTTPException(
                status_code=409,
                detail="session write_stopped; switch to the active branch",
            )
        pending_state = session.pending_send
        question = pending_state.question_for_run or pending_state.question
        if not question.strip():
            return FeishuCallbackResult(status="ignored")

        chat_type = str(context.get("chat_type") or "group")
        if chat_type not in {"p2p", "group"}:
            chat_type = "group"
        try:
            run_context: FeishuRunContext = self._identity_mapper.resolve(
                tenant_key=tenant_key,
                chat_id=chat_id,
                user_id=operator_open_id,
                chat_type=chat_type,  # type: ignore[arg-type]
            )
        except PermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

        message_access = self._resolve_message_access(run_context)
        session, _runtime_access = self._attach_runtime_access(session, message_access)
        try:
            pending = self._prepare_hermes_execution(
                project=run_context.project,
                actor=run_context.actor,
                scope=message_access.effective_scope,
                question=question,
                entry_mode="feishu_context_preview_direct",
                session=session,
                use_pending_send=False,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        generation = self._begin_latest_answer(chat_id)
        background_tasks.add_task(
            self._execute_prepared_hermes_tool_loop_and_reply,
            pending,
            chat_id,
            generation,
            session.session_id,
            pending_state.question or question,
            None if question == pending_state.question else question,
        )
        return FeishuCallbackResult(status="accepted", run_id=str(pending.run.id))

    def _handle_context_exclude_item(
        self,
        *,
        value: dict[str, Any],
        chat_id: str,
        background_tasks: BackgroundTasks,
    ) -> FeishuCallbackResult:
        """Toggle staged exclude on edit card, or immediately drop on preview."""

        session_id_raw = str(value.get("session_id") or "").strip()
        item_id = str(value.get("item_id") or "").strip()
        return_to = str(value.get("return_to") or "preview").strip() or "preview"
        if not session_id_raw or not item_id or not chat_id:
            return FeishuCallbackResult(status="ignored")
        try:
            session_id = UUID(session_id_raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid session_id") from None
        session = self._conversation.store.get(session_id)
        if session is None or session.pending_send is None:
            raise HTTPException(
                status_code=409,
                detail="pending_send missing; @ again to refresh preview",
            )
        if return_to == "edit":
            session = self._conversation.toggle_context_edit_exclude(session, item_id)
            staged = self._conversation.get_context_edit_stage(session)
            selected = item_id in staged["exclude_ids"]
            return FeishuCallbackResult(
                status="accepted",
                card=self._context_edit_card_payload(session),
                toast_type="info",
                toast_content="已勾选去掉" if selected else "已取消去掉",
            )
        session = self._conversation.exclude_from_pending_send(session, (item_id,))
        return FeishuCallbackResult(
            status="accepted",
            card=self._context_preview_card_payload(session),
            toast_type="info",
            toast_content="已从本次上下文去掉",
        )

    def _handle_context_edit(
        self,
        *,
        value: dict[str, Any],
        chat_id: str,
        background_tasks: BackgroundTasks,
    ) -> FeishuCallbackResult:
        """Open edit card: clear prior stage, show toggleable selections."""

        session = self._require_pending_session(value)
        if session is None or not chat_id:
            return FeishuCallbackResult(status="ignored")
        session = self._conversation.clear_context_edit_stage(session)
        return FeishuCallbackResult(
            status="accepted",
            card=self._context_edit_card_payload(session),
        )

    def _handle_context_more_history(
        self,
        *,
        value: dict[str, Any],
        chat_id: str,
        background_tasks: BackgroundTasks,
    ) -> FeishuCallbackResult:
        """Paginated session + group history; date_window reused when set (S07/S08)."""

        session = self._require_pending_session(value)
        if session is None or not chat_id:
            return FeishuCallbackResult(status="ignored")
        offset, group_page_token, group_page_stack, group_item_offset = (
            self._parse_more_history_paging(value)
        )
        self._post_context_more_history_card(
            session,
            chat_id,
            background_tasks,
            offset=offset,
            group_page_token=group_page_token,
            group_page_stack=group_page_stack,
            group_item_offset=group_item_offset,
        )
        return FeishuCallbackResult(status="accepted")

    def _handle_context_confirm_edit(
        self,
        *,
        value: dict[str, Any],
        chat_id: str,
        background_tasks: BackgroundTasks,
    ) -> FeishuCallbackResult:
        """Apply staged edit toggles, then replace card with updated preview."""

        session = self._require_pending_session(value)
        if session is None or not chat_id:
            return FeishuCallbackResult(status="ignored")
        session = self._conversation.apply_context_edit_stage(session)
        return FeishuCallbackResult(
            status="accepted",
            card=self._context_preview_card_payload(session),
            toast_type="success",
            toast_content="已更新本次上下文",
        )

    def _handle_context_back_preview(
        self,
        *,
        value: dict[str, Any],
        chat_id: str,
        background_tasks: BackgroundTasks,
    ) -> FeishuCallbackResult:
        session = self._require_pending_session(value)
        if session is None or not chat_id:
            return FeishuCallbackResult(status="ignored")
        session = self._conversation.clear_context_edit_stage(session)
        return FeishuCallbackResult(
            status="accepted",
            card=self._context_preview_card_payload(session),
            toast_type="info",
            toast_content="已返回预览（未应用勾选）",
        )

    def _handle_context_include_citation(
        self,
        *,
        value: dict[str, Any],
        chat_id: str,
        background_tasks: BackgroundTasks,
    ) -> FeishuCallbackResult:
        """Toggle staged join on edit card, or immediately join otherwise."""

        session = self._require_pending_session(value)
        citation_id = str(value.get("citation_id") or "").strip()
        return_to = str(value.get("return_to") or "preview").strip() or "preview"
        if session is None or not citation_id or not chat_id:
            return FeishuCallbackResult(status="ignored")
        if return_to == "edit":
            session = self._conversation.toggle_context_edit_join(session, citation_id)
            staged = self._conversation.get_context_edit_stage(session)
            selected = citation_id in staged["join_citation_ids"]
            return FeishuCallbackResult(
                status="accepted",
                card=self._context_edit_card_payload(session),
                toast_type="info",
                toast_content="已勾选加入" if selected else "已取消加入",
            )
        try:
            session = self._conversation.fine_select_history(session, citation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FeishuCallbackResult(
            status="accepted",
            card=self._context_preview_card_payload(session),
            toast_type="info",
            toast_content="已加入引用",
        )

    def _handle_context_select_history(
        self,
        *,
        value: dict[str, Any],
        chat_id: str,
        background_tasks: BackgroundTasks,
    ) -> FeishuCallbackResult:
        """Fine-select from「更多历史」→ citation stays on ledger, lands in pending."""

        session = self._require_pending_session(value)
        citation_id = str(value.get("citation_id") or "").strip()
        if session is None or not citation_id or not chat_id:
            return FeishuCallbackResult(status="ignored")
        offset, group_page_token, group_page_stack, group_item_offset = (
            self._parse_more_history_paging(value)
        )
        try:
            session = self._conversation.fine_select_history(session, citation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        # Keep more-history open for multi-select; preview only after 「返回预览」/「确定」.
        self._post_context_more_history_card(
            session,
            chat_id,
            background_tasks,
            offset=offset,
            group_page_token=group_page_token,
            group_page_stack=group_page_stack,
            group_item_offset=group_item_offset,
        )
        return FeishuCallbackResult(status="accepted")

    def _handle_context_select_group_message(
        self,
        *,
        value: dict[str, Any],
        chat_id: str,
        background_tasks: BackgroundTasks,
    ) -> FeishuCallbackResult:
        """Fine-select a Feishu group message → citation + pending + refresh preview."""

        session = self._require_pending_session(value)
        message_id = str(value.get("message_id") or "").strip()
        if session is None or not message_id or not chat_id:
            return FeishuCallbackResult(status="ignored")
        offset, group_page_token, group_page_stack, group_item_offset = (
            self._parse_more_history_paging(value)
        )
        occurred_raw = str(value.get("occurred_at") or "").strip()
        occurred_at = None
        if occurred_raw:
            try:
                occurred_at = datetime.fromisoformat(occurred_raw.replace("Z", "+00:00"))
            except ValueError:
                occurred_at = None
        try:
            session = self._conversation.fine_select_group_message(
                session,
                message_id=message_id,
                occurred_at=occurred_at,
                short_title=str(value.get("short_title") or "").strip() or None,
                body_text=str(value.get("body_text") or "") or None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Multi-select on this card only; user returns via 「返回预览」for the updated preview.
        self._post_context_more_history_card(
            session,
            chat_id,
            background_tasks,
            offset=offset,
            group_page_token=group_page_token,
            group_page_stack=group_page_stack,
            group_item_offset=group_item_offset,
        )
        return FeishuCallbackResult(status="accepted")

    def _handle_context_restore_item(
        self,
        *,
        value: dict[str, Any],
        chat_id: str,
        background_tasks: BackgroundTasks,
    ) -> FeishuCallbackResult:
        """Toggle staged restore on edit card, or immediately restore on preview."""

        session = self._require_pending_session(value)
        item_id = str(value.get("item_id") or "").strip()
        return_to = str(value.get("return_to") or "edit").strip() or "edit"
        if session is None or not item_id or not chat_id:
            return FeishuCallbackResult(status="ignored")
        if return_to == "edit":
            session = self._conversation.toggle_context_edit_restore(session, item_id)
            staged = self._conversation.get_context_edit_stage(session)
            selected = item_id in staged["restore_ids"]
            return FeishuCallbackResult(
                status="accepted",
                card=self._context_edit_card_payload(session),
                toast_type="info",
                toast_content="已勾选恢复" if selected else "已取消恢复",
            )
        session = self._conversation.restore_to_pending_send(session, (item_id,))
        return FeishuCallbackResult(
            status="accepted",
            card=self._context_preview_card_payload(session),
            toast_type="info",
            toast_content="已恢复到本次上下文",
        )

    def _handle_context_fork_branch(
        self,
        *,
        value: dict[str, Any],
        chat_id: str,
        background_tasks: BackgroundTasks,
    ) -> FeishuCallbackResult:
        """新建分支：复制当前线状态，原线停写，绑定切到新线；原卡片原地刷新。"""

        session = self._require_pending_session(value)
        if session is None or not chat_id:
            return FeishuCallbackResult(status="ignored")
        try:
            child = self._conversation.fork_session(session)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return FeishuCallbackResult(
            status="accepted",
            card=self._context_preview_card_payload(child),
            toast_type="success",
            toast_content=f"已新建分支 {child.branch_name}",
        )

    def _handle_context_switch_branch(
        self,
        *,
        value: dict[str, Any],
        chat_id: str,
        background_tasks: BackgroundTasks,
    ) -> FeishuCallbackResult:
        """切换已有分支：发送只写入选中线；原卡片原地刷新。"""

        session = self._require_pending_session(value, require_writable=False)
        if session is None or not chat_id:
            return FeishuCallbackResult(status="ignored")
        target_raw = str(value.get("target_session_id") or "").strip()
        if not target_raw:
            return FeishuCallbackResult(status="ignored")
        try:
            target_id = UUID(target_raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid target_session_id") from None
        try:
            target = self._conversation.switch_session(session, target_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if target.pending_send is None:
            # Stale line without preview: rebuild from its own pool using last question if any.
            question = ""
            target = self._conversation.refresh_pending_send(target, question=question or "（切换分支）")
        return FeishuCallbackResult(
            status="accepted",
            card=self._context_preview_card_payload(target),
            toast_type="info",
            toast_content=f"已切换到 {target.branch_name}",
        )

    def _handle_context_delete_branch(
        self,
        *,
        value: dict[str, Any],
        chat_id: str,
        background_tasks: BackgroundTasks,
    ) -> FeishuCallbackResult:
        """删除指定分支（默认当前线），绑定切到剩余兄弟线；原卡片原地刷新。"""

        session = self._require_pending_session(value, require_writable=False)
        if session is None or not chat_id:
            return FeishuCallbackResult(status="ignored")
        target_raw = str(value.get("target_session_id") or session.session_id).strip()
        try:
            target_id = UUID(target_raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid target_session_id") from None
        deleted = self._conversation.store.get(target_id)
        deleted_name = deleted.branch_name if deleted is not None else "?"
        try:
            survivor = self._conversation.delete_session(session, target_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if survivor.pending_send is None:
            survivor = self._conversation.refresh_pending_send(
                survivor, question="（删除分支后）"
            )
        return FeishuCallbackResult(
            status="accepted",
            card=self._context_preview_card_payload(survivor),
            toast_type="info",
            toast_content=f"已删除分支 {deleted_name}，当前 {survivor.branch_name}",
        )

    def _handle_context_open_date_window(
        self,
        *,
        value: dict[str, Any],
        chat_id: str,
        background_tasks: BackgroundTasks,
    ) -> FeishuCallbackResult:
        session = self._require_pending_session(value)
        if session is None or not chat_id:
            return FeishuCallbackResult(status="ignored")
        self._post_context_date_window_card(session, chat_id, background_tasks)
        return FeishuCallbackResult(status="accepted")

    def _handle_context_set_date_bound(
        self,
        *,
        value: dict[str, Any],
        bound: str,
        option: str,
        chat_id: str,
        background_tasks: BackgroundTasks,
    ) -> FeishuCallbackResult:
        """Persist one bound silently — do not post a new card (stay on coarse filter)."""

        session = self._require_pending_session(value)
        if session is None or not chat_id:
            return FeishuCallbackResult(status="ignored")
        parsed = _parse_feishu_date_option(option, end=(bound == "end"))
        if parsed is None:
            raise HTTPException(status_code=400, detail="invalid date_picker option")
        current = session.date_window
        start = current.start if current is not None else None
        end = current.end if current is not None else None
        if bound == "start":
            start = parsed
        else:
            end = parsed
        if start is None and end is None:
            raise HTTPException(status_code=400, detail="date window requires a bound")
        if start is not None and end is not None and start > end:
            raise HTTPException(status_code=400, detail="start must not be later than end")
        try:
            self._conversation.set_date_window(session, start=start, end=end)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Intentionally no post_card: date_picker stays on the same coarse-filter
        # card so the user can set both bounds, then tap 「确定」.
        return FeishuCallbackResult(status="accepted")

    def _handle_context_confirm_date_window(
        self,
        *,
        value: dict[str, Any],
        chat_id: str,
        background_tasks: BackgroundTasks,
    ) -> FeishuCallbackResult:
        """After coarse filter, open fine-select only (no extra preview card)."""

        session = self._require_pending_session(value)
        if session is None or not chat_id:
            return FeishuCallbackResult(status="ignored")
        if session.pending_send is not None:
            session = self._conversation.refresh_pending_send(
                session,
                question=session.pending_send.question,
                question_for_run=session.pending_send.question_for_run,
                date_window=session.date_window,
            )
        self._post_context_more_history_card(
            session,
            chat_id,
            background_tasks,
            offset=0,
            group_page_token=None,
        )
        return FeishuCallbackResult(status="accepted")

    def _handle_context_clear_date_window(
        self,
        *,
        value: dict[str, Any],
        chat_id: str,
        background_tasks: BackgroundTasks,
    ) -> FeishuCallbackResult:
        session = self._require_pending_session(value)
        if session is None or not chat_id:
            return FeishuCallbackResult(status="ignored")
        session = self._conversation.clear_date_window(session)
        return_to = str(value.get("return_to") or "preview").strip()
        if return_to == "date_window":
            self._post_context_date_window_card(session, chat_id, background_tasks)
            return FeishuCallbackResult(status="accepted")
        self._post_context_preview_card(session, chat_id, background_tasks)
        return FeishuCallbackResult(status="accepted")

    def _require_pending_session(
        self,
        value: dict[str, Any],
        *,
        require_writable: bool = True,
    ) -> ConversationSession | None:
        session_id_raw = str(value.get("session_id") or "").strip()
        if not session_id_raw:
            return None
        try:
            session_id = UUID(session_id_raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid session_id") from None
        session = self._conversation.store.get(session_id)
        if session is None or session.pending_send is None:
            raise HTTPException(
                status_code=409,
                detail="pending_send missing; @ again to refresh preview",
            )
        if require_writable and session.write_stopped:
            raise HTTPException(
                status_code=409,
                detail="session write_stopped; switch to the active branch",
            )
        return session

    def _joinable_citations(self, session: ConversationSession) -> tuple:
        pending_ids: set[str] = set()
        if session.pending_send is not None:
            pending_ids = {
                item.citation_id
                for item in session.pending_send.items
                if item.citation_id
            }
        return tuple(
            entry
            for entry in session.citations
            if entry.citation_id not in pending_ids
        )

    def _restorable_defaults(self, session: ConversationSession) -> tuple:
        from project_lens.domain.conversation import pending_item_from_default

        pending_ids = {
            item.item_id for item in (session.pending_send.items if session.pending_send else ())
        }
        restored: list = []
        for default in self._conversation.list_default_context_items(
            session, date_window=session.date_window
        ):
            item = pending_item_from_default(default)
            if item.item_id not in pending_ids:
                restored.append(item)
        return tuple(restored)

    def _post_context_preview_card(
        self,
        session: ConversationSession,
        chat_id: str,
        background_tasks: BackgroundTasks,
    ) -> None:
        pending_state = session.pending_send
        assert pending_state is not None
        background_tasks.add_task(
            self._messenger.post_card,
            chat_id,
            self._context_preview_card_payload(session),
        )

    def _context_preview_card_payload(
        self,
        session: ConversationSession,
    ) -> dict[str, object]:
        pending_state = session.pending_send
        assert pending_state is not None
        branches = self._conversation.list_branches(session)
        return render_context_preview_card(
            question=pending_state.question,
            session_id=session.session_id,
            items=pending_state.items,
            token_estimate=pending_state.token_estimate,
            branches=branches,
            active_branch_name=session.branch_name,
            date_window_label=self._conversation.date_window_label(session),
        )

    def _post_context_date_window_card(
        self,
        session: ConversationSession,
        chat_id: str,
        background_tasks: BackgroundTasks,
    ) -> None:
        pending_state = session.pending_send
        assert pending_state is not None
        background_tasks.add_task(
            self._messenger.post_card,
            chat_id,
            render_context_date_window_card(
                question=pending_state.question,
                session_id=session.session_id,
                date_window_label=self._conversation.date_window_label(session),
                token_estimate=pending_state.token_estimate,
            ),
        )

    def _post_context_edit_card(
        self,
        session: ConversationSession,
        chat_id: str,
        background_tasks: BackgroundTasks,
    ) -> None:
        background_tasks.add_task(
            self._messenger.post_card,
            chat_id,
            self._context_edit_card_payload(session),
        )

    def _context_edit_card_payload(
        self,
        session: ConversationSession,
    ) -> dict[str, object]:
        pending_state = session.pending_send
        assert pending_state is not None
        stage = self._conversation.get_context_edit_stage(session)
        return render_context_edit_card(
            question=pending_state.question,
            session_id=session.session_id,
            items=pending_state.items,
            token_estimate=pending_state.token_estimate,
            joinable_citations=self._joinable_citations(session),
            restorable_defaults=self._restorable_defaults(session),
            staged_exclude_ids=stage["exclude_ids"],
            staged_restore_ids=stage["restore_ids"],
            staged_join_citation_ids=stage["join_citation_ids"],
        )

    def _post_context_more_history_card(
        self,
        session: ConversationSession,
        chat_id: str,
        background_tasks: BackgroundTasks,
        *,
        offset: int = 0,
        group_page_token: str | None = None,
        group_page_stack: tuple[str, ...] = (),
        group_item_offset: int = 0,
    ) -> None:
        from project_lens.domain.conversation import HISTORY_CANDIDATE_PAGE_SIZE

        pending_state = session.pending_send
        assert pending_state is not None
        page, total = self._conversation.list_history_candidates(
            session, offset=offset, page_size=HISTORY_CANDIDATE_PAGE_SIZE
        )
        group = self._conversation.list_group_history(
            session,
            page_size=HISTORY_CANDIDATE_PAGE_SIZE,
            page_token=group_page_token,
        )
        background_tasks.add_task(
            self._messenger.post_card,
            chat_id,
            render_context_more_history_card(
                question=pending_state.question,
                session_id=session.session_id,
                candidates=page,
                token_estimate=pending_state.token_estimate,
                offset=offset,
                total=total,
                page_size=HISTORY_CANDIDATE_PAGE_SIZE,
                group_candidates=group.candidates,
                group_available=group.available,
                group_unavailable_reason=group.unavailable_reason,
                group_page_token=group_page_token,
                group_page_stack=group_page_stack,
                group_item_offset=group_item_offset,
                group_has_more=group.has_more,
                group_next_page_token=group.next_page_token,
            ),
        )

    def _parse_more_history_paging(
        self, value: dict[str, Any]
    ) -> tuple[int, str | None, tuple[str, ...], int]:
        """Read session offset + group cursor stack from a card action value."""

        try:
            offset = int(str(value.get("offset") or "0"))
        except ValueError:
            offset = 0
        try:
            group_item_offset = int(str(value.get("group_item_offset") or "0"))
        except ValueError:
            group_item_offset = 0
        group_page_token = str(value.get("group_page_token") or "").strip() or None
        group_page_stack = decode_group_page_stack(value.get("group_page_stack"))
        return max(0, offset), group_page_token, group_page_stack, max(0, group_item_offset)

    def _prepare_hermes_execution(
        self,
        *,
        project: ProjectRef,
        actor: ActorContext,
        scope: EffectiveAccessScope,
        question: str,
        entry_mode: str,
        session: ConversationSession | None,
        use_pending_send: bool = True,
    ) -> PendingHermesExecution:
        runtime = self._hermes_runtime
        if runtime is None:
            raise RuntimeError("Hermes runtime is not configured")
        memories: tuple[ProjectMemory, ...] = ()
        if session is not None and self._memory_store is not None:
            memories = authorize_memory_candidates(
                self._memory_store.list_memories(session.project),
                project=project,
                access_scope=scope,
            )
        context = build_hermes_project_context(
            session=session,
            memories=memories,
            memory_summary=build_memory_summary(memories),
            runtime_access={
                "tenant_id": scope.project.tenant_id,
                "project_id": scope.project.project_id,
                "actor_id": scope.actor_id,
                "chat_id": scope.chat_id,
                "role": scope.role.value,
                "visibility_level": scope.visibility_level.value,
                "policy_version": scope.policy_version,
            },
            use_pending_send=use_pending_send if session is not None else False,
        )
        return runtime.prepare(
            project=project,
            actor=actor,
            scope=scope,
            question=question,
            entry_mode=entry_mode,
            context=context,
        )

    async def _ack_and_execute_prepared_hermes_tool_loop_and_reply(
        self,
        pending: PendingHermesExecution,
        chat_id: str,
        generation: int,
        session_id: UUID | None = None,
        raw_text: str | None = None,
        followup_rewrite: str | None = None,
    ) -> None:
        """Post a short ack, then always continue into the Hermes tool loop."""

        lock = self._chat_answer_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            try:
                await self._messenger.post_text(chat_id, "收到，正在回答…")
            except Exception:  # noqa: BLE001 - ack is best-effort; Hermes must still run
                logger.exception(
                    "feishu ack post failed chat_id=%s run_id=%s",
                    chat_id,
                    pending.run.id,
                )
            await self._execute_prepared_hermes_tool_loop_and_reply(
                pending,
                chat_id,
                generation,
                session_id,
                raw_text,
                followup_rewrite,
            )

    async def _execute_prepared_hermes_tool_loop_and_reply(
        self,
        pending: PendingHermesExecution,
        chat_id: str,
        generation: int,
        session_id: UUID | None = None,
        raw_text: str | None = None,
        followup_rewrite: str | None = None,
    ) -> None:
        """Run Hermes, post the IM reply, then best-effort persist the turn.

        Feishu delivery must not depend on session bookkeeping. Previously
        ``record_turn`` → ``attach_hermes_tool_loop`` ran before ``post_text``;
        if attach/memory side-effects raised, the preview already showed
        ``回复：…`` while the group never got a bot message.
        """

        runtime = self._hermes_runtime
        if runtime is None:
            raise RuntimeError("Hermes runtime is not configured")
        try:
            execution = await runtime.execute_prepared(pending)
        except Exception as exc:  # noqa: BLE001 - background task must still notify the chat
            logger.exception(
                "hermes execute_prepared failed chat_id=%s run_id=%s",
                chat_id,
                pending.run.id,
            )
            from project_lens.integrations.feishu.hermes_errors import (
                format_unexpected_for_feishu,
            )

            await self._safe_post_text(
                chat_id,
                format_unexpected_for_feishu(exc, run_id=pending.run.id),
            )
            return

        # Deliver to Feishu first — do not let session writes skip the IM reply.
        try:
            await self._render_hermes_execution(
                chat_id=chat_id,
                execution=execution,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "feishu hermes reply failed chat_id=%s run_id=%s",
                chat_id,
                execution.run.id,
            )
            from project_lens.integrations.feishu.hermes_errors import (
                format_delivery_failure_for_feishu,
            )

            await self._safe_post_text(
                chat_id,
                format_delivery_failure_for_feishu(exc, run_id=execution.run.id),
            )

        try:
            self._persist_hermes_turn(
                pending=pending,
                execution=execution,
                session_id=session_id,
                raw_text=raw_text,
                followup_rewrite=followup_rewrite,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "persist hermes turn failed after feishu reply chat_id=%s run_id=%s",
                chat_id,
                execution.run.id,
            )

    def _persist_hermes_turn(
        self,
        *,
        pending: PendingHermesExecution,
        execution: Any,
        session_id: UUID | None,
        raw_text: str | None,
        followup_rewrite: str | None,
    ) -> None:
        if session_id is None:
            return
        session = self._conversation.store.get(session_id)
        if session is None:
            return
        session = self._conversation.record_turn(
            session,
            user_id=pending.actor.actor_id,
            text=raw_text or pending.question,
            rewritten_question=followup_rewrite,
            run_id=execution.run.id,
            answer=execution.answer,
        )
        try:
            session = self._conversation.attach_hermes_tool_loop(
                session, hermes_loop_scratchpad_entry(execution.envelope)
            )
            audit_ref = execution.envelope.get("audit_ref")
            context_budget = audit_ref.get("context_budget") if isinstance(audit_ref, dict) else None
            if isinstance(context_budget, dict) and context_budget.get("rotation_recommended") is True:
                self._conversation.mark_rotation_pending(session, pending=True)
        except Exception:  # noqa: BLE001 - audit pointer must not undo a delivered reply
            logger.exception(
                "attach_hermes_tool_loop failed session_id=%s run_id=%s",
                session_id,
                execution.run.id,
            )

    async def _safe_post_text(self, chat_id: str, text: str) -> None:
        try:
            await self._messenger.post_text(chat_id, text)
        except Exception:  # noqa: BLE001
            logger.exception("feishu post_text failed chat_id=%s", chat_id)

    async def _render_hermes_execution(
        self,
        *,
        chat_id: str,
        execution: Any,
    ) -> None:
        executed = execution.run
        if not execution.ok:
            await self._messenger.post_text(chat_id, format_failure_as_text(executed))
            logger.info(
                "feishu hermes failure reply posted chat_id=%s run_id=%s",
                chat_id,
                executed.id,
            )
            return
        if executed.answer is None:
            await self._messenger.post_text(chat_id, format_failure_as_text(executed))
            logger.info(
                "feishu hermes empty-answer reply posted chat_id=%s run_id=%s",
                chat_id,
                executed.id,
            )
            return
        memory_proposal = None
        if self._memory_gateway is not None:
            try:
                candidate = propose_memory_from_answer(
                    executed.answer,
                    proposed_by=executed.user_id,
                )
                if candidate is not None:
                    memory_proposal = self._memory_gateway.create_memory_proposal(
                        candidate
                    )
                    self._lifecycle.emit(
                        LifecycleEventType.MEMORY_PROPOSED,
                        run_id=executed.id,
                        trace_id=executed.trace_id,
                        project=executed.project,
                        payload={
                            "proposal_id": str(memory_proposal.id),
                            "proposed_by": memory_proposal.proposed_by,
                            "source": "hermes",
                        },
                    )
                    self._lifecycle.emit(
                        LifecycleEventType.APPROVAL_REQUESTED,
                        run_id=executed.id,
                        trace_id=executed.trace_id,
                        project=executed.project,
                        payload={
                            "proposal_id": str(memory_proposal.id),
                            "kind": "memory_proposal",
                            "allowed_approvers": list(memory_proposal.allowed_approvers),
                        },
                    )
            except Exception:  # noqa: BLE001 - never block the group reply
                logger.exception(
                    "memory proposal after hermes failed run_id=%s",
                    executed.id,
                )
                memory_proposal = None
        # Successful Hermes answers always use the ProjectLens interactive card.
        # Keeping one renderer here preserves the white card layout and its
        # citations/actions even when no memory approval is needed.
        card = render_answer_card(
            executed,
            executed.answer,
            memory_proposal=memory_proposal,
            show_debug_audit=False,
        )
        if getattr(executed, "entry_mode", "") == "feishu_context_preview_direct":
            body = (executed.answer.answer_text or executed.answer.business_summary or executed.answer.conclusion).strip()
            await self._safe_post_text(chat_id, body or format_failure_as_text(executed))
            return
        try:
            await self._messenger.post_card(chat_id, card)
        except Exception:  # noqa: BLE001 - preserve answer delivery if card formatting/API fails
            logger.exception(
                "feishu answer card delivery failed; falling back to text chat_id=%s run_id=%s",
                chat_id,
                executed.id,
            )
            body = (executed.answer.answer_text or executed.answer.business_summary or executed.answer.conclusion).strip()
            if body:
                await self._safe_post_text(chat_id, body)
            else:
                await self._safe_post_text(chat_id, format_failure_as_text(executed))
            return
        logger.info(
            "feishu hermes answer card posted chat_id=%s run_id=%s proposal_id=%s",
            chat_id,
            executed.id,
            memory_proposal.id if memory_proposal is not None else None,
        )

    def _try_role_view_switch(
        self,
        *,
        session: ConversationSession,
        text: str,
        user_id: str,
        chat_id: str,
        background_tasks: BackgroundTasks,
    ) -> FeishuCallbackResult | None:
        audience = detect_audience_switch(text)
        if audience is None:
            return None
        if session.last_run_id is None:
            return None
        prior = self._run_service.get(session.last_run_id)
        if (
            prior is None
            or prior.status != RunStatus.COMPLETED
            or prior.answer is None
        ):
            return None
        self._conversation.record_turn(
            session,
            user_id=user_id,
            text=text,
            rewritten_question=f"role_view:{audience.value}",
            run_id=prior.id,
            answer=prior.answer,
        )
        background_tasks.add_task(
            self._reply_role_view,
            prior.id,
            chat_id,
            audience,
        )
        return FeishuCallbackResult(status="accepted", run_id=str(prior.id))

    async def _reply_role_view(
        self,
        run_id: UUID,
        chat_id: str,
        audience: AnswerAudience,
    ) -> None:
        run = self._run_service.get(run_id)
        if run is None or run.answer is None:
            return
        await self._messenger.post_text(
            chat_id,
            format_answer_as_text(run, run.answer, audience=audience),
        )

    async def _run_knowledge_gap_recheck(self, gap_id, actor, scope, project, reply_chat_id: str) -> None:
        try:
            result = await self._knowledge_gap_rechecks.recheck(
                gap_id,
                actor=actor,
                scope=scope,
                authorized_project=project,
                trigger="feishu_card_action",
            )
            message = f"知识缺口复查完成：`{gap_id}`\n结果：{result.audit.outcome}\n状态：{result.case.status.value}"
        except Exception:
            logger.exception("Feishu knowledge-gap recheck failed gap_id=%s", gap_id)
            message = f"知识缺口复查失败：`{gap_id}`，状态保持待复查，请稍后重试。"
        await self._messenger.post_user_card(
            reply_chat_id,
            {"msg_type": "interactive", "card": {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": message}]}}},
        )

    async def _run_source_update_rechecks(self, actor, scope, project, reply_chat_id: str) -> None:
        try:
            results = await self._knowledge_gap_rechecks.dispatch_source_jobs(
                project=project, actor=actor, scope=scope,
            )
            message = f"资料更新复查完成：{len(results)} 条缺口已复查。"
        except Exception:
            logger.exception("Feishu source-update recheck dispatch failed project=%s", project.project_id)
            message = "资料更新复查未能完成；任务保留，可在授权后重试。"
        await self._messenger.post_user_card(
            reply_chat_id,
            {"msg_type": "interactive", "card": {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": message}]}}},
        )

    async def _reply_about_bot(self, chat_id: str, user_text: str) -> None:
        card = render_about_bot_card(user_text=user_text)
        await self._messenger.post_text(chat_id, card_payload_as_text(card))

    async def _post_memory_decision(
        self,
        chat_id: str,
        proposal: MemoryProposal,
        memory: ProjectMemory | None,
    ) -> None:
        # Memory approve/reject still needs interactive buttons.
        await self._messenger.post_card(
            chat_id,
            render_memory_decision_card(proposal, memory=memory),
        )

    async def _reply_doc_sync_status(self, project: ProjectRef, chat_id: str) -> None:
        read_error: str | None = None
        statuses: tuple[FeishuDocSyncStatus, ...] = ()
        try:
            if self._sync_status_store is None:
                read_error = "同步状态存储未配置；普通问答仍可继续使用。"
            else:
                statuses = self._sync_status_store.list_for_project(project)
        except Exception as exc:  # noqa: BLE001 - status read must not break chat UX
            logger.exception("feishu doc sync status card failed")
            read_error = f"读取同步状态失败：{exc}"
            statuses = ()
        card = render_feishu_doc_sync_status_card(
            project,
            statuses,
            read_error=read_error,
        )
        await self._messenger.post_card(chat_id, card)


def _parse_feishu_date_option(option: str, *, end: bool) -> datetime | None:
    """Parse Feishu date_picker option (``YYYY-MM-DD`` or ``YYYY-MM-DD +0800``)."""

    raw = (option or "").strip()
    if not raw:
        return None
    # Drop trailing timezone offset like "+0800" / "Asia/Shanghai" when present.
    token = raw.split()[0]
    try:
        if "T" in token or " " in raw and ":" in token:
            parsed = datetime.fromisoformat(token.replace("Z", "+00:00"))
        else:
            parsed = datetime.fromisoformat(token)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if end and len(token) <= 10:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    elif not end and len(token) <= 10:
        parsed = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
    return parsed.astimezone(timezone.utc)
