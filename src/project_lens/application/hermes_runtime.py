"""The sole production orchestration path for Hermes project questions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from project_lens.application.genai_trace_service import GenAITraceService
from project_lens.application.run_service import RunService
from project_lens.domain.identity import ActorContext
from project_lens.domain.models import AgentRun, ProjectAnswer, ProjectRef
from project_lens.integrations.feishu.hermes_context import (
    HermesProjectContext,
    build_hermes_project_context,
)
from project_lens.integrations.feishu.hermes_tool_loop import (
    FeishuHermesToolLoopBridge,
    FeishuHermesToolLoopResult,
)
from project_lens.project_space.policies import (
    EffectiveAccessScope,
    effective_scope_to_audit_dict,
)
from project_lens.workflow.context_snapshot import (
    ContextSnapshotService,
    MemoryCandidate,
)

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class HermesExecutionResult:
    run: AgentRun
    answer: ProjectAnswer | None
    loop_id: UUID
    tool_names: tuple[str, ...]
    envelope: dict[str, object]
    context: HermesProjectContext

    @property
    def ok(self) -> bool:
        return self.answer is not None and self.run.status.value == "completed"


@dataclass(frozen=True)
class PendingHermesExecution:
    """A bound Hermes run that is safe to hand to background execution."""

    run: AgentRun
    project: ProjectRef
    actor: ActorContext
    scope: EffectiveAccessScope
    question: str
    entry_mode: str
    loop_id: UUID
    context: HermesProjectContext
    context_snapshot_id: UUID | None = None


class HermesRuntimeService:
    """Create the real AgentRun before delegating decision-making to Hermes."""

    def __init__(
        self,
        *,
        run_service: RunService,
        bridge: FeishuHermesToolLoopBridge,
        context_snapshots: ContextSnapshotService | None = None,
        genai_trace_service: GenAITraceService | None = None,
        tool_catalog_provider: Any | None = None,
    ) -> None:
        self._run_service = run_service
        self._bridge = bridge
        self._context_snapshots = context_snapshots
        self._genai_trace_service = genai_trace_service
        self._tool_catalog_provider = tool_catalog_provider

    @property
    def run_service(self) -> RunService:
        return self._run_service

    async def execute(
        self,
        *,
        project: ProjectRef,
        actor: ActorContext,
        scope: EffectiveAccessScope,
        question: str,
        entry_mode: str,
        context_snapshot_id: UUID | None = None,
        context: HermesProjectContext | None = None,
        memory_candidates: tuple[MemoryCandidate, ...] = (),
    ) -> HermesExecutionResult:
        pending = self.prepare(
            project=project,
            actor=actor,
            scope=scope,
            question=question,
            entry_mode=entry_mode,
            context_snapshot_id=context_snapshot_id,
            context=context,
            memory_candidates=memory_candidates,
        )
        return await self.execute_prepared(pending)

    def prepare(
        self,
        *,
        project: ProjectRef,
        actor: ActorContext,
        scope: EffectiveAccessScope,
        question: str,
        entry_mode: str,
        context_snapshot_id: UUID | None = None,
        context: HermesProjectContext | None = None,
        memory_candidates: tuple[MemoryCandidate, ...] = (),
    ) -> PendingHermesExecution:
        """Validate and persist the run before the Hermes tool loop starts."""

        self._assert_binding(project=project, actor=actor, scope=scope)
        normalized_question = question.strip()
        if not normalized_question:
            raise ValueError("question must not be empty")

        runtime_access = effective_scope_to_audit_dict(scope)
        hermes_context = context or build_hermes_project_context(
            session=None,
            runtime_access=runtime_access,
        )
        if self._context_snapshots is not None and memory_candidates:
            frozen = self._context_snapshots.freeze_and_start(
                project=project,
                scope=scope,
                candidates=memory_candidates,
            )
            snapshot = frozen.snapshot
            context_snapshot_id = snapshot.snapshot_id
            context = build_hermes_project_context(
                session=None,
                memories=frozen.memories,
                runtime_access=runtime_access,
            )
            hermes_context = context
            hermes_context.audit_refs.update({
                "context_snapshot_id": str(snapshot.snapshot_id),
                "renderer_version": snapshot.renderer_version,
                "snapshot_memory_ids": [str(item) for item in snapshot.included_memory_ids],
            })
        context_hash = sha256(hermes_context.text.encode("utf-8")).hexdigest()
        loop_id = uuid4()
        run = self._run_service.create(
            project=project,
            user_id=actor.actor_id,
            channel_id=actor.chat_id,
            question=normalized_question,
            runtime_access=runtime_access,
            entry_mode=entry_mode,
            runtime="hermes",
            hermes_loop_id=loop_id,
            context_snapshot_id=context_snapshot_id,
            context_hash=context_hash,
        )
        return PendingHermesExecution(
            run=run,
            project=project,
            actor=actor,
            scope=scope,
            question=normalized_question,
            entry_mode=entry_mode,
            loop_id=loop_id,
            context=hermes_context,
            context_snapshot_id=context_snapshot_id,
        )

    async def execute_prepared(
        self,
        pending: PendingHermesExecution,
    ) -> HermesExecutionResult:
        """Run a previously-created Hermes execution without making a second run."""

        run = pending.run
        loop_id = pending.loop_id
        result: FeishuHermesToolLoopResult
        try:
            result = await self._bridge.answer(
                project=pending.project,
                question=pending.question,
                user_id=pending.actor.actor_id,
                chat_id=pending.actor.chat_id,
                context=pending.context,
                run_id=run.id,
                loop_id=loop_id,
                trace_id=run.trace_id,
            )
            error = None
            answer = result.verified_answer if result.ok else None
            if answer is None:
                audit = (
                    result.envelope.get("audit_ref")
                    if isinstance(result.envelope.get("audit_ref"), dict)
                    else {}
                )
                raw_error = str(audit.get("error") or "").strip()
                if not raw_error:
                    raw_error = "Hermes tool loop failed verification"
                from project_lens.integrations.feishu.hermes_errors import (
                    classify_hermes_failure,
                )

                error = classify_hermes_failure(raw_error).format_run_error()
            completed = await self._run_service.complete_external(
                run.id,
                answer=answer,
                error=error,
                agent_mode="hermes",
                hermes_loop_id=loop_id,
                metadata={
                    "entry_mode": pending.entry_mode,
                    "hermes_loop_id": str(loop_id),
                    "tool_names": list(result.tool_names),
                    "context_hash": run.context_hash,
                    "context_snapshot_id": (
                        str(pending.context_snapshot_id)
                        if pending.context_snapshot_id is not None
                        else None
                    ),
                },
            )
        except Exception as exc:  # noqa: BLE001
            from project_lens.integrations.feishu.hermes_errors import (
                PROJECTLENS_RUNTIME,
                classify_hermes_failure,
            )

            runtime_error = classify_hermes_failure(
                f"Hermes runtime failed: {exc}",
                default_code=PROJECTLENS_RUNTIME,
            ).format_run_error()
            completed = await self._run_service.complete_external(
                run.id,
                answer=None,
                error=runtime_error,
                agent_mode="hermes",
                hermes_loop_id=loop_id,
                metadata={
                    "entry_mode": pending.entry_mode,
                    "hermes_loop_id": str(loop_id),
                },
            )
            result = FeishuHermesToolLoopResult(
                ok=False,
                envelope={
                    "ok": False,
                    "audit_ref": {
                        "error": runtime_error,
                        "error_code": PROJECTLENS_RUNTIME,
                        "error_stage": "projectlens.runtime",
                    },
                },
                loop_id=loop_id,
                trace_id=run.trace_id,
            )
        if completed is None:
            raise RuntimeError(f"Hermes run {run.id} disappeared before completion")
        self._archive_genai_trace(pending=pending, completed=completed, result=result)
        return HermesExecutionResult(
            run=completed,
            answer=completed.answer,
            loop_id=loop_id,
            tool_names=result.tool_names,
            envelope=result.envelope,
            context=pending.context,
        )

    def _archive_genai_trace(
        self,
        *,
        pending: PendingHermesExecution,
        completed: AgentRun,
        result: FeishuHermesToolLoopResult,
    ) -> None:
        """Persist GenAI trace json (primary body) while AgentRun remains dual-write."""

        if self._genai_trace_service is None:
            return
        tools: tuple[dict[str, Any], ...] = ()
        if self._tool_catalog_provider is not None:
            try:
                catalog = self._tool_catalog_provider.list_tools()
                raw_tools = catalog.get("tools") if isinstance(catalog, dict) else None
                if isinstance(raw_tools, list):
                    tools = tuple(
                        item for item in raw_tools if isinstance(item, dict)
                    )
            except Exception:  # noqa: BLE001
                _LOG.exception("failed to collect tool catalog for GenAI trace")
        try:
            self._genai_trace_service.record_hermes_cycle(
                run=completed,
                question=pending.question,
                context_text=pending.context.text if pending.context is not None else None,
                messages=getattr(result, "messages", ()) or (),
                tool_calls=getattr(result, "tool_calls", ()) or (),
                final_response=(
                    getattr(result, "final_response", None)
                    or str((result.envelope or {}).get("answer_summary") or "")
                ),
                tool_catalog=tools,
                runtime_extras={
                    "hermes_ok": bool(result.ok),
                    "entry_mode": pending.entry_mode,
                    "loop_id": str(pending.loop_id),
                },
                started_at=pending.run.created_at,
                ended_at=completed.updated_at,
            )
            self._genai_trace_service.apply_retention()
        except Exception:  # noqa: BLE001
            # Trace archival must not block the user-facing answer path.
            _LOG.exception(
                "GenAI trace archival failed for run_id=%s trace_id=%s",
                completed.id,
                completed.trace_id,
            )

    @staticmethod
    def _assert_binding(
        *,
        project: ProjectRef,
        actor: ActorContext,
        scope: EffectiveAccessScope,
    ) -> None:
        if not actor.authenticated:
            raise PermissionError("Hermes runtime requires an authenticated actor")
        if actor.tenant_key != project.tenant_id:
            raise PermissionError("actor tenant does not match project")
        if scope.project != project:
            raise PermissionError("effective access scope does not match project")
        if scope.actor_id != actor.actor_id or scope.chat_id != actor.chat_id:
            raise PermissionError("effective access scope is not bound to this actor/chat")
        if scope.chat_type != actor.chat_type:
            raise PermissionError("effective access scope chat type does not match actor")
        if scope.identity_source != actor.source:
            raise PermissionError("effective access scope identity source does not match actor")
