"""Per-message access binding — never reuse actor/scope across speakers."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace

from project_lens.domain.identity import ActorContext, ChatType
from project_lens.project_space.policies import (
    EffectiveAccessScope,
    ProjectRuntimeContextResolver,
    effective_scope_to_audit_dict,
)


@dataclass(frozen=True)
class MessageAccessContext:
    """Fresh scope for one Feishu message; sessions may not carry this forward."""

    actor: ActorContext
    effective_scope: EffectiveAccessScope
    message_id: str | None = None

    @classmethod
    def resolve_for_actor(
        cls,
        *,
        actor: ActorContext,
        project_id: str,
        resolver: ProjectRuntimeContextResolver,
        message_id: str | None = None,
        tenant_id: str | None = None,
    ) -> MessageAccessContext:
        # Prefer ProjectLens tenant_id from the chat→project binding. Feishu
        # tenant_key is only an identity signal and may differ (e.g. demo vs real).
        resolved = resolver.resolve(
            tenant_id=tenant_id or actor.tenant_key,
            project_id=project_id,
            chat_id=actor.chat_id,
            user_id=actor.actor_id,
            chat_type=actor.chat_type,
            identity_source=actor.source,
        )
        scope = resolved.effective_scope
        if actor.physical_actor_id:
            scope = replace(scope, physical_actor_id=actor.physical_actor_id)
        return cls(
            actor=actor,
            effective_scope=scope,
            message_id=message_id,
        )

    def runtime_access_audit(self) -> dict[str, object]:
        return effective_scope_to_audit_dict(self.effective_scope)

    @property
    def chat_type(self) -> ChatType:
        return self.actor.chat_type
