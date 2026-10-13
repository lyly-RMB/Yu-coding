"""Map Feishu identities to ProjectLens project and access context."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from project_lens.domain.identity import ActorContext, ChatType
from project_lens.domain.models import ProjectRef


@dataclass(frozen=True)
class FeishuRunContext:
    project: ProjectRef
    user_id: str
    channel_id: str
    actor: ActorContext

    @property
    def tenant_key(self) -> str:
        return self.actor.tenant_key

    @property
    def chat_type(self) -> ChatType:
        return self.actor.chat_type


def build_feishu_actor_context(
    *,
    tenant_key: str,
    actor_id: str,
    chat_id: str,
    chat_type: ChatType,
) -> ActorContext:
    return ActorContext(
        tenant_key=tenant_key,
        actor_id=actor_id,
        chat_id=chat_id,
        chat_type=chat_type,
        source="feishu_event",
        authenticated=True,
    )


class ConfigurableFeishuIdentityMapper:
    """Resolve only explicitly configured tenant/chat bindings."""

    def __init__(
        self,
        *,
        bindings: dict[tuple[str, str], ProjectRef],
        allowed_users: dict[tuple[str, str], frozenset[str]] | None = None,
        owner_open_ids: dict[tuple[ProjectRef, str], str] | None = None,
    ) -> None:
        self._bindings = bindings
        self._allowed_users = allowed_users or {}
        self._project_chat_ids = {project: chat_id for (tenant, chat_id), project in bindings.items()}
        self._owner_open_ids = owner_open_ids or {}

    @property
    def binding_count(self) -> int:
        return len(self._bindings)

    def project_chat_id(self, project: ProjectRef) -> str | None:
        return self._project_chat_ids.get(project)

    def owner_open_id(self, project: ProjectRef, owner_id: str) -> str | None:
        return self._owner_open_ids.get((project, owner_id))

    def resolve(
        self,
        *,
        tenant_key: str,
        chat_id: str,
        user_id: str,
        chat_type: ChatType = "group",
    ) -> FeishuRunContext:
        if not tenant_key.strip():
            raise PermissionError("missing Feishu tenant_key")
        key = (tenant_key, chat_id)
        project = self._bindings.get(key)
        if project is None:
            raise PermissionError("Feishu chat is not mapped to a ProjectLens project")
        users = self._allowed_users.get(key)
        if users is not None and user_id not in users:
            raise PermissionError("Feishu user is not allowed for this project chat")
        # Binding already validated Feishu tenant_key+chat_id. Downstream Hermes /
        # ProjectSpace expect actor.tenant_key == ProjectLens project.tenant_id
        # (may differ via lens_tenant_id).
        actor = build_feishu_actor_context(
            tenant_key=project.tenant_id,
            actor_id=user_id,
            chat_id=chat_id,
            chat_type=chat_type,
        )
        return FeishuRunContext(
            project=project,
            user_id=user_id,
            channel_id=chat_id,
            actor=actor,
        )


def parse_project_bindings(
    raw: str,
    *,
    default_project: ProjectRef,
    registered_projects: Iterable[ProjectRef] | None = None,
    allow_demo_fallback: bool = False,
) -> ConfigurableFeishuIdentityMapper:
    projects = tuple(registered_projects or (default_project,))
    if not raw.strip():
        # Phase 0: empty bindings must not invent demo/chat-1. Demo fallback is
        # only for explicit local development fixture wiring.
        if allow_demo_fallback:
            return ConfigurableFeishuIdentityMapper(
                bindings={(default_project.tenant_id, "chat-1"): default_project}
            )
        return ConfigurableFeishuIdentityMapper(bindings={})
    payload: dict[str, Any] = json.loads(raw)
    bindings: dict[tuple[str, str], ProjectRef] = {}
    allowed_users: dict[tuple[str, str], frozenset[str]] = {}
    owner_open_ids: dict[tuple[ProjectRef, str], str] = {}
    for item in payload.get("bindings", []):
        tenant_key = str(item["tenant_key"])
        chat_id = str(item["chat_id"])
        key = (tenant_key, chat_id)
        if key in bindings:
            raise ValueError("duplicate Feishu tenant/chat binding")
        requested = ProjectRef(
            tenant_id=str(item.get("lens_tenant_id", default_project.tenant_id)),
            project_id=str(item.get("project_id", default_project.project_id)),
            service=_optional_str(item.get("service")),
            environment=_optional_str(item.get("environment")),
        )
        bindings[key] = _resolve_registered_project(requested, projects)
        users = item.get("allowed_users")
        if users is not None:
            if not isinstance(users, list) or not all(isinstance(user, str) for user in users):
                raise ValueError("allowed_users must be a JSON array of user IDs")
            allowed_users[key] = frozenset(users)
        raw_owner_mappings = item.get("owner_mappings") or {}
        if not isinstance(raw_owner_mappings, dict):
            raise ValueError("owner_mappings must be a JSON object")
        for owner_id, open_id in raw_owner_mappings.items():
            if not isinstance(owner_id, str) or not isinstance(open_id, str) or not owner_id.strip() or not open_id.strip():
                raise ValueError("owner_mappings values must be non-empty strings")
            owner_open_ids[(bindings[key], owner_id)] = open_id
    return ConfigurableFeishuIdentityMapper(
        bindings=bindings,
        allowed_users=allowed_users,
        owner_open_ids=owner_open_ids,
    )


def _resolve_registered_project(
    requested: ProjectRef,
    registered_projects: tuple[ProjectRef, ...],
) -> ProjectRef:
    matches = [
        project
        for project in registered_projects
        if project.tenant_id == requested.tenant_id
        and project.project_id == requested.project_id
        and (requested.service is None or project.service == requested.service)
        and (requested.environment is None or project.environment == requested.environment)
    ]
    if not matches:
        raise ValueError("Feishu binding references an unregistered project")
    if len(matches) > 1:
        raise ValueError("Feishu binding project is ambiguous; specify service or environment")
    return matches[0]


def _optional_str(value: object | None) -> str | None:
    if value is None:
        return None
    return str(value)
