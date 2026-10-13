"""One-shot Project Agent ask use case for Hermes/MCP Kernel interface."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from project_lens.application.answer_envelope import (
    project_answer_to_envelope,
    recoverable_error,
)
from project_lens.application.hermes_runtime import HermesRuntimeService
from project_lens.domain.identity import ActorContext
from project_lens.domain.models import ProjectRef, RunStatus
from project_lens.application.knowledge_gap_service import KnowledgeGapService
from project_lens.domain.knowledge_gap import KnowledgeGapCandidate, KnowledgeGapCaseType, GapSeverity
from project_lens.application.knowledge_gap_digest import KnowledgeGapDigestService, render_owner_knowledge_gap_digest, render_knowledge_gap_digest
from project_lens.project_space.policies import (
    ProjectRuntimeContextResolver,
)
from project_lens.project_space.registry import ProjectRegistry

# Conservative write/apply intents — avoid matching read-only docs about deploy.
_WRITE_INTENT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"帮我部署",
        r"立刻部署",
        r"立即部署",
        r"执行部署",
        r"执行回滚",
        r"帮我回滚",
        r"立刻回滚",
        r"重启服务",
        r"帮我重启",
        r"立刻重启",
        r"创建\s*pr\b",
        r"提\s*pr\b",
        r"提交\s*pr\b",
        r"直接改代码",
        r"帮我改代码",
        r"apply\s+(the\s+)?patch",
        r"\bapply\s+fix\b",
        r"\bdeploy\s+(now|to|production|prod)\b",
        r"\brollback\s+(now|production|prod|the)\b",
        r"\brestart\s+(the\s+)?(service|server|pod|container)\b",
        r"\bcreate\s+(a\s+)?pr\b",
        r"\bopen\s+(a\s+)?pull\s+request\b",
    )
)


@dataclass(frozen=True)
class ProjectAgentAskRequest:
    question: str
    project: ProjectRef
    user_id: str = "mcp-user"
    channel_id: str | None = None
    chat_type: str = "group"
    identity_source: str = "member_directory"
    audience: str = "team"
    mode: str = "read_only"
    format: str = "concise"
    actor: ActorContext | None = None


class ProjectAgentAskService:
    """Thin adapter: gates -> HermesRuntimeService -> compact envelope.

    Does not query EvidenceIndex / graph / ops stores directly.
    Does not invent facts.
    """

    def __init__(
        self,
        *,
        hermes_runtime: HermesRuntimeService,
        project_registry: ProjectRegistry,
        runtime_context_resolver: ProjectRuntimeContextResolver | None = None,
        knowledge_gap_service: KnowledgeGapService | None = None,
        knowledge_gap_digest_service: KnowledgeGapDigestService | None = None,
        feishu_messenger: Any | None = None,
    ) -> None:
        self._hermes_runtime = hermes_runtime
        self._registry = project_registry
        self._resolver = runtime_context_resolver or ProjectRuntimeContextResolver(
            project_registry=project_registry,
        )
        self._knowledge_gap_service = knowledge_gap_service
        self._knowledge_gap_digest = knowledge_gap_digest_service
        self._feishu_messenger = feishu_messenger

    def configure_gap_delivery(self, *, digest_service: KnowledgeGapDigestService, messenger: Any) -> None:
        self._knowledge_gap_digest = digest_service
        self._feishu_messenger = messenger

    @property
    def run_service(self):  # noqa: ANN201 - preserves role-view adapter compatibility
        return self._hermes_runtime.run_service

    @property
    def project_registry(self) -> ProjectRegistry:
        return self._registry

    async def ask(self, request: ProjectAgentAskRequest) -> dict[str, Any]:
        question = request.question.strip()
        if not question:
            return recoverable_error(
                error_code="QUESTION_EMPTY",
                message="question must not be empty",
                retryable=True,
                agent_recovery_hint="Provide a non-empty project question.",
                project=request.project,
            )

        if request.mode not in {"read_only", "readonly", "read"}:
            return recoverable_error(
                error_code="WRITE_ACTION_DENIED",
                message="ProjectLens ask API only supports mode=read_only.",
                retryable=False,
                agent_recovery_hint=(
                    "Retry with mode=read_only. Apply / PR / deploy / rollback / restart "
                    "are permanently disabled."
                ),
                project=request.project,
                allow_apply=False,
            )

        if detects_write_intent(question):
            return recoverable_error(
                error_code="WRITE_ACTION_DENIED",
                message=(
                    "Write/apply/deploy/rollback/restart requests are rejected. "
                    "ProjectLens remains read-only."
                ),
                retryable=False,
                agent_recovery_hint=(
                    "Ask a read-only investigation question instead "
                    "(architecture, files, owners, gaps, change impact). "
                    "Do not request Apply / PR / deploy / rollback / restart."
                ),
                project=request.project,
                allow_apply=False,
                extras={
                    "unknowns": [
                        "该请求属于写操作意图，已拒绝执行；未运行项目调查。"
                    ],
                    "facts": [],
                    "inferences": [],
                    "next_actions": [
                        {
                            "title": "改为只读追问",
                            "description": "说明要了解的模块、入口、变更或缺口，而不是要求系统改代码或部署。",
                            "requires_approval": False,
                        }
                    ],
                },
            )

        space = self._registry.get(request.project.tenant_id, request.project.project_id)
        if space is None:
            return recoverable_error(
                error_code="PROJECT_NOT_FOUND",
                message=(
                    f"ProjectSpace {request.project.tenant_id}/{request.project.project_id} "
                    "is not registered."
                ),
                retryable=False,
                agent_recovery_hint=(
                    "Ask the user which registered ProjectLens project to use, "
                    "or register a ProjectSpace before retrying."
                ),
                project=request.project,
            )

        # Prefer registry ProjectRef defaults (service/env) when caller omitted them.
        project = ProjectRef(
            tenant_id=space.tenant_id,
            project_id=space.project_id,
            service=request.project.service or space.project.service,
            environment=request.project.environment or space.project.environment,
        )

        actor = request.actor
        if actor is None:
            return recoverable_error(
                error_code="TRUSTED_ACTOR_REQUIRED",
                message="ProjectLens ask requires a trusted actor context.",
                retryable=False,
                agent_recovery_hint=(
                    "Call through a trusted HTTP or Feishu ingress that binds "
                    "identity before invoking the Hermes runtime."
                ),
                project=project,
            )
        if actor.tenant_key != project.tenant_id:
            return recoverable_error(
                error_code="ACCESS_DENIED",
                message="trusted actor tenant does not match the requested project",
                retryable=False,
                agent_recovery_hint="Use a trusted actor for the project tenant.",
                project=project,
            )
        chat_id = actor.chat_id
        try:
            resolved = self._resolver.resolve(
                tenant_id=project.tenant_id,
                project_id=project.project_id,
                chat_id=chat_id,
                user_id=actor.actor_id,
                chat_type=actor.chat_type,
                identity_source=actor.source,
            )
        except PermissionError as exc:
            return recoverable_error(
                error_code="ACCESS_DENIED",
                message=str(exc),
                retryable=False,
                agent_recovery_hint=(
                    "Use a project member account or ask the project admin to grant access."
                ),
                project=project,
            )

        execution = await self._hermes_runtime.execute(
            project=project,
            actor=actor,
            scope=resolved.effective_scope,
            question=question,
            entry_mode="http_ask",
        )
        executed = execution.run
        if executed is None:
            return recoverable_error(
                error_code="RUN_NOT_FOUND",
                message="Hermes run disappeared after create",
                retryable=True,
                agent_recovery_hint="Retry the ask request.",
                project=project,
            )

        events = self._hermes_runtime.run_service.events(executed.id)
        if executed.status == RunStatus.FAILED or executed.answer is None:
            return recoverable_error(
                error_code="HERMES_UNAVAILABLE",
                message=executed.error or "Hermes failed without a verified answer",
                retryable=True,
                agent_recovery_hint=(
                    "Retry later, or ask a narrower read-only question with file/module hints. "
                    "ProjectLens did not fall back to a legacy runtime."
                ),
                project=project,
                extras={
                    "run_id": str(executed.id),
                    "trace_id": str(executed.trace_id),
                    "unknowns": [executed.error or "investigation failed"],
                },
            )

        envelope = project_answer_to_envelope(
            run=executed,
            answer=executed.answer,
            events=events,
            tool_names=execution.tool_names or None,
            audience=request.audience,
            format=request.format,
        )
        await self._persist_answer_gaps(
            executed.answer,
            project=project,
            actor=actor,
            scope=resolved.effective_scope,
            chat_id=chat_id,
            tool_gaps=_extract_tool_gaps(execution.envelope),
        )
        return envelope

    async def _persist_answer_gaps(self, answer, *, project: ProjectRef, actor: ActorContext, scope, chat_id: str | None, tool_gaps: tuple[dict[str, Any], ...] = ()) -> None:
        if self._knowledge_gap_service is None:
            return
        evidence_ids = tuple(item.evidence_ids for item in answer.claims)
        authorized = frozenset(item for group in evidence_ids for item in group)
        created = []
        gap_inputs = [
            (text, KnowledgeGapCaseType.MISSING_SOURCE)
            for text in getattr(answer, "unknowns", ())
        ] + [
            (text, KnowledgeGapCaseType.CONFLICTING_SOURCES)
            for text in getattr(answer, "conflicts", ())
        ]
        for gap in tool_gaps:
            title = str(gap.get("title") or "").strip()
            description = str(gap.get("description") or title).strip()
            if title:
                gap_inputs.append((title, KnowledgeGapCaseType.MISSING_SOURCE, description))
        # Hermes may render the knowledge-gap report into answer_text without
        # preserving the raw tool envelope. Keep the report durable so Digest
        # can still notify the configured project owner.
        if not tool_gaps and not gap_inputs and _looks_like_gap_report(answer):
            gap_inputs.append((
                "项目知识缺口报告待负责人补充资料",
                KnowledgeGapCaseType.MISSING_SOURCE,
                answer.answer_text[:4000] or "当前授权范围内发现资料缺口。",
            ))
        for item in gap_inputs:
            if len(item) == 3:
                text, gap_type, description = item
            else:
                text, gap_type = item
                description = text
            if not text or (not authorized and gap_type != KnowledgeGapCaseType.MISSING_SOURCE):
                continue
            candidate = KnowledgeGapCandidate(project=project, type=gap_type, title=text[:300], question=text[:2000], description=description[:4000], severity=GapSeverity.MEDIUM, evidence_ids=tuple(authorized), evidence_status=("authorized" if authorized else "no_authorized_evidence"), suggested_owner_ids=(("u-project-owner",) if not authorized else ()))
            try:
                created.append(self._knowledge_gap_service.observe(candidate, authorized_project=project, authorized_evidence_ids=authorized, visibility_scope=scope.scope_id if hasattr(scope, "scope_id") else "project:read", created_by=actor.actor_id))
            except ValueError:
                continue
        if not created or self._feishu_messenger is None:
            return
        now = datetime.now(timezone.utc)
        digest = self._knowledge_gap_digest.build(project, period_start=now.replace(hour=0, minute=0, second=0, microsecond=0), period_end=now, authorized_evidence_ids=authorized)
        if chat_id:
            key = f"{project.project_id}:group:knowledge_gap_digest:{digest.digest_date.isoformat()}"
            if self._knowledge_gap_service.claim_notification(key, gap_id=created[0].id, project=project, recipient_id=chat_id, event_type="knowledge_gap_digest", period_key=digest.digest_date.isoformat(), at=now):
                await self._feishu_messenger.post_card(chat_id, render_knowledge_gap_digest(digest))
        for owner in sorted({item for case in created if case for item in case.suggested_owner_ids}):
            owner_digest = self._knowledge_gap_digest.build_bundle(project, period_start=digest.period_start, period_end=digest.period_end, authorized_evidence_ids=authorized, authorized_owner_ids=frozenset({owner}), now=now).owner_digests[0]
            key = f"{created[0].id}:{owner}:knowledge_gap_digest:{digest.digest_date.isoformat()}"
            if self._knowledge_gap_service.claim_notification(key, gap_id=created[0].id, project=project, recipient_id=owner, event_type="knowledge_gap_digest", period_key=digest.digest_date.isoformat(), at=now):
                await self._feishu_messenger.post_user_card(owner, render_owner_knowledge_gap_digest(digest, owner_digest))


def detects_write_intent(question: str) -> bool:
    normalized = " ".join(question.strip().split())
    if not normalized:
        return False
    return any(pattern.search(normalized) for pattern in _WRITE_INTENT_PATTERNS)


def _extract_tool_gaps(envelope: dict[str, Any] | None) -> tuple[dict[str, Any], ...]:
    """Extract structured list_knowledge_gaps output from a Hermes envelope."""
    found: list[dict[str, Any]] = []
    def visit(value: Any) -> None:
        if isinstance(value, dict):
            gaps = value.get("gaps")
            if isinstance(gaps, list):
                found.extend(item for item in gaps if isinstance(item, dict))
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str) and '"gaps"' in value:
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError):
                return
            visit(parsed)
    import json
    visit(envelope or {})
    unique: dict[str, dict[str, Any]] = {}
    for item in found:
        key = str(item.get("title") or item.get("description") or "").strip()
        if key:
            unique[key] = item
    return tuple(unique.values())


def _looks_like_gap_report(answer: Any) -> bool:
    text = str(getattr(answer, "answer_text", "") or "")
    return any(marker in text for marker in ("知识缺口", "缺少资料", "缺失证据", "项目知识库缺失"))
