"""Feishu event handling service."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
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
    render_about_bot_card,
    render_answer_card,
    render_collaboration_gate_card,
    render_failure_card,
    render_feishu_doc_sync_status_card,
    render_memory_decision_card,
    render_run_detail_card,
    render_risk_feedback_result_card,
)
from project_lens.integrations.feishu.commands import rewrite_feishu_message
from project_lens.integrations.feishu.idempotency import EventDeduplicator
from project_lens.integrations.feishu.identity import FeishuRunContext
from project_lens.integrations.feishu.hermes_tool_loop import (
    FeishuHermesToolLoopBridge,
    hermes_loop_scratchpad_entry,
)
from project_lens.integrations.feishu.hermes_context import build_hermes_project_context
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

    def to_response(self) -> dict[str, str]:
        if self.challenge is not None and self.status == "verified":
            return {"challenge": self.challenge}
        response: dict[str, str] = {"status": self.status}
        if self.run_id:
            response["run_id"] = self.run_id
        if self.proposal_id:
            response["proposal_id"] = self.proposal_id
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
        self._followup = FollowupRewriter()
        self._ingress = FeishuIngressRouter()

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

        session = self._conversation.get_or_create(
            tenant_id=context.project.tenant_id,
            chat_id=message.chat_id,
            user_id=context.user_id,
            project=context.project,
        )
        message_access = self._resolve_message_access(
            context,
            message_id=message.message_id,
        )
        session, _runtime_access = self._attach_runtime_access(session, message_access)
        question_for_run, followup_rewrite = self._conversation.prepare_question(session, text)
        ingress = self._ingress.decide(
            text,
            session=session,
            followup_rewrite=followup_rewrite,
        )
        if followup_rewrite is None:
            text_for_rewrite = (
                strip_project_debug_slash(text)
                if ingress.kind == FeishuIngressKind.DEBUG_PROJECT_COMMAND
                else text
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
            try:
                pending = self._prepare_hermes_execution(
                    project=context.project,
                    actor=context.actor,
                    scope=message_access.effective_scope,
                    question=question_for_run,
                    entry_mode=ingress.entry_mode or "natural_project_question",
                    session=session,
                )
            except PermissionError as exc:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
                ) from exc
            background_tasks.add_task(
                self._execute_prepared_hermes_tool_loop_and_reply,
                pending,
                message.chat_id,
                session.session_id,
                text,
                audit_rewrite,
            )
            return FeishuCallbackResult(status="accepted", run_id=str(pending.run.id))

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

        # Non-project chitchat → collaboration gate (no forced Skill AgentRun).
        if ingress.kind == FeishuIngressKind.NON_PROJECT_CHITCHAT:
            self._conversation.record_turn(
                session,
                user_id=context.user_id,
                text=text,
                rewritten_question=None,
            )
            background_tasks.add_task(
                self._reply_collaboration_gate,
                context.project,
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
            return self._start_question_from_card(
                tenant_key=tenant_key.strip(),
                chat_id=chat_id,
                user_id=operator_open_id,
                text=question,
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
        chat_type: str = "group",
    ) -> FeishuCallbackResult:
        if chat_type not in {"p2p", "group"}:
            chat_type = "group"
        try:
            context: FeishuRunContext = self._identity_mapper.resolve(
                tenant_key=tenant_key,
                chat_id=chat_id,
                user_id=user_id,
                chat_type=chat_type,  # type: ignore[arg-type]
            )
        except PermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

        session = self._conversation.get_or_create(
            tenant_id=context.project.tenant_id,
            chat_id=chat_id,
            user_id=context.user_id,
            project=context.project,
        )
        message_access = self._resolve_message_access(context)
        session, _runtime_access = self._attach_runtime_access(session, message_access)
        question_for_run, followup_rewrite = self._conversation.prepare_question(session, text)
        ingress = self._ingress.decide(
            text,
            session=session,
            followup_rewrite=followup_rewrite,
        )
        if followup_rewrite is None:
            text_for_rewrite = (
                strip_project_debug_slash(text)
                if ingress.kind == FeishuIngressKind.DEBUG_PROJECT_COMMAND
                else text
            )
            question_for_run = rewrite_feishu_message(text_for_rewrite)
        audit_rewrite = None if question_for_run == text else question_for_run

        role_view = self._try_role_view_switch(
            session=session,
            text=text,
            user_id=context.user_id,
            chat_id=chat_id,
            background_tasks=background_tasks,
        )
        if role_view is not None:
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

        pending = self._prepare_hermes_execution(
            project=context.project,
            actor=context.actor,
            scope=message_access.effective_scope,
            question=question_for_run,
            entry_mode=ingress.entry_mode or "feishu_card_ask",
            session=session,
        )
        background_tasks.add_task(
            self._execute_prepared_hermes_tool_loop_and_reply,
            pending,
            chat_id,
            session.session_id,
            text,
            audit_rewrite,
        )
        return FeishuCallbackResult(status="accepted", run_id=str(pending.run.id))

    def _prepare_hermes_execution(
        self,
        *,
        project: ProjectRef,
        actor: ActorContext,
        scope: EffectiveAccessScope,
        question: str,
        entry_mode: str,
        session: ConversationSession | None,
    ) -> PendingHermesExecution:
        runtime = self._hermes_runtime
        if runtime is None:
            raise RuntimeError("Hermes runtime is not configured")
        handoff = None
        if (
            session is not None
            and session.session_state is not None
            and session.session_state.rotation_pending
        ):
            handoff, session = self._conversation.build_handoff(session)
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
            handoff=handoff,
            runtime_access={
                "tenant_id": scope.project.tenant_id,
                "project_id": scope.project.project_id,
                "actor_id": scope.actor_id,
                "chat_id": scope.chat_id,
                "role": scope.role.value,
                "visibility_level": scope.visibility_level.value,
                "policy_version": scope.policy_version,
            },
        )
        return runtime.prepare(
            project=project,
            actor=actor,
            scope=scope,
            question=question,
            entry_mode=entry_mode,
            context=context,
        )

    async def _execute_prepared_hermes_tool_loop_and_reply(
        self,
        pending: PendingHermesExecution,
        chat_id: str,
        session_id: UUID | None = None,
        raw_text: str | None = None,
        followup_rewrite: str | None = None,
    ) -> None:
        runtime = self._hermes_runtime
        if runtime is None:
            raise RuntimeError("Hermes runtime is not configured")
        execution = await runtime.execute_prepared(pending)
        executed = execution.run
        session = self._conversation.store.get(session_id) if session_id is not None else None
        if session is not None:
            session = self._conversation.record_turn(
                session,
                user_id=pending.actor.actor_id,
                text=raw_text or pending.question,
                rewritten_question=followup_rewrite,
                run_id=executed.id,
                answer=execution.answer,
            )
            session = self._conversation.attach_hermes_tool_loop(
                session, hermes_loop_scratchpad_entry(execution.envelope)
            )
            audit_ref = execution.envelope.get("audit_ref")
            context_budget = (
                audit_ref.get("context_budget")
                if isinstance(audit_ref, dict)
                else None
            )
            if (
                isinstance(context_budget, dict)
                and context_budget.get("rotation_recommended") is True
            ):
                self._conversation.mark_rotation_pending(session, pending=True)
        await self._render_hermes_execution(
            chat_id=chat_id,
            execution=execution,
        )

    async def _render_hermes_execution(
        self,
        *,
        chat_id: str,
        execution: Any,
    ) -> None:
        executed = execution.run
        if not execution.ok:
            await self._messenger.post_card(chat_id, render_failure_card(executed))
            return
        if executed.answer is None:
            await self._messenger.post_card(chat_id, render_failure_card(executed))
            return
        memory_proposal = None
        if self._memory_gateway is not None:
            candidate = propose_memory_from_answer(
                executed.answer,
                proposed_by=executed.user_id,
            )
            if candidate is not None:
                try:
                    memory_proposal = self._memory_gateway.create_memory_proposal(candidate)
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
                except ValueError:
                    memory_proposal = None
        await self._messenger.post_card(
            chat_id,
            render_answer_card(
                executed,
                executed.answer,
                memory_proposal=memory_proposal,
            ),
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
        await self._messenger.post_card(
            chat_id,
            render_answer_card(run, run.answer, audience=audience),
        )

    async def _reply_collaboration_gate(
        self,
        project: ProjectRef,
        chat_id: str,
        user_text: str,
    ) -> None:
        await self._messenger.post_card(
            chat_id,
            render_collaboration_gate_card(user_text=user_text, project=project),
        )

    async def _reply_about_bot(self, chat_id: str, user_text: str) -> None:
        await self._messenger.post_card(
            chat_id,
            render_about_bot_card(user_text=user_text),
        )

    async def _post_memory_decision(
        self,
        chat_id: str,
        proposal: MemoryProposal,
        memory: ProjectMemory | None,
    ) -> None:
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
        await self._messenger.post_card(
            chat_id,
            render_feishu_doc_sync_status_card(
                project,
                statuses,
                read_error=read_error,
            ),
        )
