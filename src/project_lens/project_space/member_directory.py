"""Project member directory — roles come from manifest, never from the model."""

from __future__ import annotations

from dataclasses import dataclass

from project_lens.domain.models import ProjectRef
from project_lens.project_space.policies import (
    DEFAULT_READ_TOOLS,
    AnswerDepth,
    ProjectMemberRolePolicy,
    RoleKind,
    VisibilityLevel,
)


@dataclass(frozen=True)
class ProjectMember:
    actor_id: str
    roles: tuple[RoleKind, ...]


def resolve_member_roles(
    members: tuple[ProjectMember, ...],
    actor_id: str,
) -> tuple[RoleKind, ...]:
    """Return declared roles for an actor, or empty when unknown."""

    for member in members:
        if member.actor_id == actor_id:
            return member.roles
    return ()


def primary_role(roles: tuple[RoleKind, ...]) -> RoleKind | None:
    """MVP uses the first declared role as the active policy role."""

    return roles[0] if roles else None


def role_template_policy(
    *,
    actor_id: str,
    project: ProjectRef,
    role: RoleKind,
    file_allowlist: tuple[str, ...],
    public_sources: tuple[str, ...],
) -> ProjectMemberRolePolicy:
    """Expand a RoleKind into a concrete ACL policy for manifest members."""

    if role is RoleKind.GUEST:
        return ProjectMemberRolePolicy(
            actor_id=actor_id,
            project=project,
            role=RoleKind.GUEST,
            readable_sources=public_sources,
            allowed_tools=_guest_tools(public_sources),
            answer_depth=AnswerDepth.BRIEF,
            answer_style="team",
            visibility_level=VisibilityLevel.TEAM_SHARED,
        )

    if role is RoleKind.DEVELOPER:
        return ProjectMemberRolePolicy(
            actor_id=actor_id,
            project=project,
            role=role,
            readable_sources=file_allowlist,
            allowed_tools=DEFAULT_READ_TOOLS,
            answer_depth=AnswerDepth.DETAILED,
            answer_style="technical",
            visibility_level=VisibilityLevel.TEAM_SHARED,
        )

    if role is RoleKind.QA:
        return ProjectMemberRolePolicy(
            actor_id=actor_id,
            project=project,
            role=role,
            readable_sources=_prefer_prefixes(
                file_allowlist,
                ("tests/", "knowledge/", "docs/", "src/"),
            ),
            allowed_tools=DEFAULT_READ_TOOLS,
            answer_depth=AnswerDepth.BALANCED,
            answer_style="qa",
            visibility_level=VisibilityLevel.TEAM_SHARED,
        )

    if role is RoleKind.PRODUCT:
        return ProjectMemberRolePolicy(
            actor_id=actor_id,
            project=project,
            role=role,
            readable_sources=_prefer_prefixes(
                file_allowlist,
                ("knowledge/", "docs/", "tests/"),
            ),
            allowed_tools=("search_context", "list_knowledge_gaps"),
            answer_depth=AnswerDepth.BALANCED,
            answer_style="business",
            visibility_level=VisibilityLevel.TEAM_SHARED,
        )

    if role is RoleKind.MANAGER:
        return ProjectMemberRolePolicy(
            actor_id=actor_id,
            project=project,
            role=role,
            readable_sources=_prefer_prefixes(
                file_allowlist,
                ("knowledge/", "docs/", "tests/", "src/"),
            ),
            allowed_tools=("search_context", "list_knowledge_gaps"),
            answer_depth=AnswerDepth.BRIEF,
            answer_style="executive",
            visibility_level=VisibilityLevel.TEAM_SHARED,
        )

    if role is RoleKind.OPS:
        return ProjectMemberRolePolicy(
            actor_id=actor_id,
            project=project,
            role=role,
            readable_sources=file_allowlist,
            allowed_tools=DEFAULT_READ_TOOLS,
            answer_depth=AnswerDepth.DETAILED,
            answer_style="ops",
            visibility_level=VisibilityLevel.TEAM_SHARED,
        )

    # onboarding and any future non-guest roles: public-leaning brief view
    return ProjectMemberRolePolicy(
        actor_id=actor_id,
        project=project,
        role=role,
        readable_sources=public_sources or _prefer_prefixes(file_allowlist, ("knowledge/", "docs/")),
        allowed_tools=("search_context", "list_knowledge_gaps"),
        answer_depth=AnswerDepth.BRIEF,
        answer_style="onboarding",
        visibility_level=VisibilityLevel.TEAM_SHARED,
    )


def merge_role_policies(
    *,
    from_members: tuple[ProjectMemberRolePolicy, ...],
    explicit: tuple[ProjectMemberRolePolicy, ...],
) -> tuple[ProjectMemberRolePolicy, ...]:
    """Explicit per-actor role_policies win over member-template policies."""

    by_actor: dict[str, ProjectMemberRolePolicy] = {
        policy.actor_id: policy for policy in from_members
    }
    for policy in explicit:
        by_actor[policy.actor_id] = policy
    return tuple(by_actor.values())


def _guest_tools(public_sources: tuple[str, ...]) -> tuple[str, ...]:
    if not public_sources:
        return ()
    return ("search_context",)


def _prefer_prefixes(
    allowlist: tuple[str, ...],
    preferred: tuple[str, ...],
) -> tuple[str, ...]:
    selected = tuple(item for item in preferred if item in allowlist)
    return selected or allowlist
