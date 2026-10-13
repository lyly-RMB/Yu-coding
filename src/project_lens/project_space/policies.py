"""Role-aware ProjectSpace policies.

These models are deliberately project-scoped.  Hermes may drive the agent loop,
but ProjectLens owns what can be read, how much can be shown, and which role
policy is active before answer generation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from collections.abc import Mapping
from typing import TYPE_CHECKING

from project_lens.domain.identity import ChatType
from project_lens.domain.models import ProjectRef

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from project_lens.project_space.models import ProjectSpace
    from project_lens.project_space.registry import ProjectRegistry

POLICY_VERSION = "v1"

DEFAULT_READ_TOOLS: tuple[str, ...] = (
    "search_context",
    "read_project_file",
    "list_knowledge_gaps",
    "search_wiki",
    "read_wiki_page",
)


class RoleKind(StrEnum):
    DEVELOPER = "developer"
    OPS = "ops"
    QA = "qa"
    PRODUCT = "product"
    MANAGER = "manager"
    ONBOARDING = "onboarding"
    GUEST = "guest"


class AnswerDepth(StrEnum):
    BRIEF = "brief"
    BALANCED = "balanced"
    DETAILED = "detailed"


class VisibilityLevel(StrEnum):
    PRIVATE = "private"
    TEAM_SHARED = "team_shared"


@dataclass(frozen=True)
class ProjectMemberRolePolicy:
    actor_id: str
    project: ProjectRef
    role: RoleKind
    readable_sources: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    forbidden_sources: tuple[str, ...] = ()
    answer_depth: AnswerDepth = AnswerDepth.BALANCED
    answer_style: str = "team"
    visibility_level: VisibilityLevel = VisibilityLevel.TEAM_SHARED
    escalation_rules: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChatVisibilityPolicy:
    chat_id: str
    project: ProjectRef
    chat_type: ChatType = "group"
    visibility_level: VisibilityLevel = VisibilityLevel.TEAM_SHARED
    allowed_roles: tuple[RoleKind, ...] = ()
    readable_sources: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    forbidden_sources: tuple[str, ...] = ()
    answer_depth: AnswerDepth | None = None
    answer_style: str | None = None
    allow_private_details: bool = False
    public_sources: tuple[str, ...] = ()
    policy_version: str = POLICY_VERSION


@dataclass(frozen=True)
class EffectiveAccessScope:
    project: ProjectRef
    actor_id: str
    chat_id: str
    role: RoleKind
    readable_sources: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    forbidden_sources: tuple[str, ...]
    answer_depth: AnswerDepth
    answer_style: str
    visibility_level: VisibilityLevel
    identity_source: str = "member_directory"
    policy_version: str = POLICY_VERSION
    chat_type: ChatType = "group"
    allow_private_details: bool = False
    physical_actor_id: str | None = None


def effective_scope_to_audit_dict(scope: EffectiveAccessScope) -> dict[str, object]:
    """Serialize capability bounds for session/lifecycle audit only.

    Never include ProjectSpace, Evidence, prompts, file bodies, or secrets.
    """

    return {
        "tenant_id": scope.project.tenant_id,
        "project_id": scope.project.project_id,
        "actor_id": scope.actor_id,
        **(
            {"physical_actor_id": scope.physical_actor_id}
            if scope.physical_actor_id
            else {}
        ),
        "chat_id": scope.chat_id,
        "role": scope.role.value,
        "visibility_level": scope.visibility_level.value,
        "answer_depth": scope.answer_depth.value,
        "answer_style": scope.answer_style,
        "allowed_tools": list(scope.allowed_tools),
        "readable_sources": list(scope.readable_sources),
        "forbidden_sources": list(scope.forbidden_sources),
        "identity_source": scope.identity_source,
        "policy_version": scope.policy_version,
        "chat_type": scope.chat_type,
        "allow_private_details": scope.allow_private_details,
    }


def effective_scope_from_audit_dict(
    payload: Mapping[str, object],
    *,
    project: ProjectRef,
    actor_id: str,
    chat_id: str,
) -> EffectiveAccessScope:
    """Restore a scope snapshot only after binding it to the current run."""

    if str(payload.get("tenant_id") or "") != project.tenant_id:
        raise ValueError("runtime access tenant does not match current run")
    if str(payload.get("project_id") or "") != project.project_id:
        raise ValueError("runtime access project does not match current run")
    if str(payload.get("actor_id") or "") != actor_id:
        raise ValueError("runtime access actor does not match current run")
    if str(payload.get("chat_id") or "") != chat_id:
        raise ValueError("runtime access chat does not match current run")

    chat_type = str(payload.get("chat_type") or "")
    if chat_type not in {"p2p", "group"}:
        raise ValueError("runtime access chat_type is invalid")

    identity_source = _required_text(payload, "identity_source")
    policy_version = _required_text(payload, "policy_version")
    answer_style = _required_text(payload, "answer_style")
    allow_private_details = payload.get("allow_private_details")
    if not isinstance(allow_private_details, bool):
        raise ValueError("runtime access allow_private_details must be boolean")
    if chat_type == "group" and allow_private_details:
        raise ValueError("group runtime access cannot allow private details")

    try:
        role = RoleKind(_required_text(payload, "role"))
        answer_depth = AnswerDepth(_required_text(payload, "answer_depth"))
        visibility_level = VisibilityLevel(
            _required_text(payload, "visibility_level")
        )
    except ValueError as exc:
        raise ValueError("runtime access enum value is invalid") from exc

    return EffectiveAccessScope(
        project=project,
        actor_id=actor_id,
        chat_id=chat_id,
        role=role,
        readable_sources=_string_tuple(payload, "readable_sources"),
        allowed_tools=_string_tuple(payload, "allowed_tools"),
        forbidden_sources=_string_tuple(payload, "forbidden_sources"),
        answer_depth=answer_depth,
        answer_style=answer_style,
        visibility_level=visibility_level,
        identity_source=identity_source,
        policy_version=policy_version,
        chat_type=chat_type,  # type: ignore[arg-type]
        allow_private_details=allow_private_details,
        physical_actor_id=(str(payload["physical_actor_id"]) if payload.get("physical_actor_id") else None),
    )


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"runtime access {key} must be a non-empty string")
    return value.strip()


def _string_tuple(payload: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"runtime access {key} must be an array")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"runtime access {key} must contain non-empty strings")
    return tuple(str(item).strip() for item in value)


@dataclass(frozen=True)
class ResolvedProjectRuntimeContext:
    project_space: ProjectSpace
    actor_id: str
    channel_id: str
    role_policy: ProjectMemberRolePolicy
    chat_policy: ChatVisibilityPolicy
    effective_scope: EffectiveAccessScope

    @property
    def project(self) -> ProjectRef:
        return self.project_space.project


class ProjectRuntimeContextResolver:
    """Resolve ProjectSpace + user role + chat visibility before agent generation."""

    def __init__(self, *, project_registry: ProjectRegistry) -> None:
        self._project_registry = project_registry

    def resolve(
        self,
        *,
        tenant_id: str,
        project_id: str,
        chat_id: str,
        user_id: str,
        chat_type: ChatType = "group",
        identity_source: str = "member_directory",
    ) -> ResolvedProjectRuntimeContext:
        space = self._project_registry.require(tenant_id, project_id)
        role_policy = space.role_policy_for(user_id) or default_member_role_policy(
            space=space,
            actor_id=user_id,
        )
        if (
            role_policy.role is RoleKind.GUEST
            and not role_policy.readable_sources
            and not role_policy.allowed_tools
        ):
            raise PermissionError(
                "unknown actor has no public project sources; access denied"
            )
        explicit_chat = space.chat_policy_for(chat_id)
        if explicit_chat is not None:
            chat_policy = explicit_chat
            effective_chat_type = explicit_chat.chat_type
        else:
            effective_chat_type = chat_type
            chat_policy = default_chat_visibility_policy(
                space=space,
                chat_id=chat_id,
                chat_type=effective_chat_type,
            )
        if chat_policy.allowed_roles and role_policy.role not in chat_policy.allowed_roles:
            raise PermissionError(
                f"role {role_policy.role.value} is not allowed in chat {chat_id}"
            )
        effective_scope = combine_role_and_chat_policy(
            role_policy=role_policy,
            chat_policy=chat_policy,
            chat_type=effective_chat_type,
            identity_source=identity_source,
        )
        return ResolvedProjectRuntimeContext(
            project_space=space,
            actor_id=user_id,
            channel_id=chat_id,
            role_policy=role_policy,
            chat_policy=chat_policy,
            effective_scope=effective_scope,
        )


def default_member_role_policy(
    *,
    space: ProjectSpace,
    actor_id: str,
) -> ProjectMemberRolePolicy:
    public_sources = space.public_sources
    guest_tools: tuple[str, ...] = ("search_context",) if public_sources else ()
    return ProjectMemberRolePolicy(
        actor_id=actor_id,
        project=space.project,
        role=RoleKind.GUEST,
        readable_sources=public_sources,
        allowed_tools=guest_tools,
        answer_depth=AnswerDepth.BRIEF,
        answer_style="team",
        visibility_level=VisibilityLevel.TEAM_SHARED,
    )


def default_chat_visibility_policy(
    *,
    space: ProjectSpace,
    chat_id: str,
    chat_type: ChatType,
) -> ChatVisibilityPolicy:
    if chat_type == "p2p":
        return ChatVisibilityPolicy(
            chat_id=chat_id,
            project=space.project,
            chat_type="p2p",
            visibility_level=VisibilityLevel.PRIVATE,
            readable_sources=space.file_allowlist,
            allowed_tools=DEFAULT_READ_TOOLS,
            allow_private_details=True,
            policy_version=POLICY_VERSION,
        )
    return ChatVisibilityPolicy(
        chat_id=chat_id,
        project=space.project,
        chat_type="group",
        visibility_level=VisibilityLevel.TEAM_SHARED,
        readable_sources=space.file_allowlist,
        allowed_tools=DEFAULT_READ_TOOLS,
        allow_private_details=False,
        public_sources=space.public_sources,
        policy_version=POLICY_VERSION,
    )


def combine_role_and_chat_policy(
    *,
    role_policy: ProjectMemberRolePolicy,
    chat_policy: ChatVisibilityPolicy,
    chat_type: ChatType,
    identity_source: str = "member_directory",
) -> EffectiveAccessScope:
    if chat_type == "p2p":
        readable_sources = role_policy.readable_sources or chat_policy.readable_sources
        allowed_tools = role_policy.allowed_tools or chat_policy.allowed_tools
        forbidden_sources = _dedupe(
            (*role_policy.forbidden_sources, *chat_policy.forbidden_sources)
        )
        answer_depth = role_policy.answer_depth
        answer_style = role_policy.answer_style
        visibility_level = role_policy.visibility_level
        allow_private_details = (
            chat_policy.allow_private_details
            or role_policy.visibility_level is VisibilityLevel.PRIVATE
        )
    else:
        readable_sources = _intersect_or_inherit(
            role_policy.readable_sources,
            chat_policy.readable_sources,
        )
        allowed_tools = _intersect_or_inherit(
            role_policy.allowed_tools,
            chat_policy.allowed_tools,
        )
        forbidden_sources = _dedupe(
            (*chat_policy.forbidden_sources, *role_policy.forbidden_sources)
        )
        answer_depth = _more_conservative_depth(
            role_policy.answer_depth,
            chat_policy.answer_depth,
        )
        answer_style = role_policy.answer_style
        visibility_level = _more_conservative_visibility(
            role_policy.visibility_level,
            chat_policy.visibility_level,
        )
        if visibility_level is VisibilityLevel.PRIVATE:
            visibility_level = VisibilityLevel.TEAM_SHARED
        allow_private_details = False

    return EffectiveAccessScope(
        project=role_policy.project,
        actor_id=role_policy.actor_id,
        chat_id=chat_policy.chat_id,
        role=role_policy.role,
        readable_sources=readable_sources,
        allowed_tools=allowed_tools,
        forbidden_sources=forbidden_sources,
        answer_depth=answer_depth,
        answer_style=answer_style,
        visibility_level=visibility_level,
        identity_source=identity_source,
        policy_version=chat_policy.policy_version,
        chat_type=chat_type,
        allow_private_details=allow_private_details,
    )


def source_path_allowed(scope: EffectiveAccessScope, path: str) -> bool:
    """Return whether a project-relative path is within readable_sources."""

    normalized = path.strip().replace("\\", "/")
    if not normalized:
        return False
    if any(
        normalized.startswith(prefix) or normalized == prefix.rstrip("/")
        for prefix in scope.forbidden_sources
    ):
        return False
    if not scope.readable_sources:
        return False
    return any(normalized.startswith(prefix) for prefix in scope.readable_sources)


def _intersect_or_inherit(
    primary: tuple[str, ...],
    secondary: tuple[str, ...],
) -> tuple[str, ...]:
    if not primary:
        return secondary
    if not secondary:
        return primary
    allowed = set(secondary)
    return tuple(item for item in primary if item in allowed)


def _dedupe(items: tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return tuple(result)


def _more_conservative_depth(
    role_depth: AnswerDepth,
    chat_depth: AnswerDepth | None,
) -> AnswerDepth:
    if chat_depth is None:
        return role_depth
    rank = {
        AnswerDepth.BRIEF: 0,
        AnswerDepth.BALANCED: 1,
        AnswerDepth.DETAILED: 2,
    }
    return role_depth if rank[role_depth] <= rank[chat_depth] else chat_depth


def _more_conservative_visibility(
    role_visibility: VisibilityLevel,
    chat_visibility: VisibilityLevel,
) -> VisibilityLevel:
    rank = {
        VisibilityLevel.PRIVATE: 0,
        VisibilityLevel.TEAM_SHARED: 1,
    }
    return (
        role_visibility
        if rank[role_visibility] <= rank[chat_visibility]
        else chat_visibility
    )
