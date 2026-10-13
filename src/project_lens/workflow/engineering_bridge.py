"""Bridge incident/traceback answers to ProjectEngineeringSkill (read-only)."""

from __future__ import annotations

from pathlib import Path

from project_lens.context.retrieval.exact import TracebackHint, matching_traceback_frame, parse_traceback
from project_lens.domain.models import ActionProposal, Evidence, EvidenceType, ProjectAnswer
from project_lens.workflow.engineering_skill import EngineeringProposal, ProjectEngineeringSkill
from project_lens.workflow.skills import ProjectSkill


def maybe_attach_engineering_proposal(
    answer: ProjectAnswer,
    *,
    question: str,
    skill: ProjectSkill,
    evidence: tuple[Evidence, ...],
    engineering_skill: ProjectEngineeringSkill | None,
) -> ProjectAnswer:
    """For incident/traceback runs, attach a validated read-only EngineeringProposal."""

    if engineering_skill is None:
        return answer
    if skill != ProjectSkill.INCIDENT_DIAGNOSIS:
        return answer
    frames, _exception = parse_traceback(question)
    if not frames:
        return answer
    relative_path = resolve_engineering_relative_path(frames, evidence)
    if relative_path is None:
        return answer
    try:
        proposal = engineering_skill.propose_from_traceback(
            project=answer.project,
            traceback=question,
            relative_path=relative_path,
            test_commands=('python -c "print(\'ok\')"',),
        )
    except (OSError, PermissionError, ValueError):
        return answer
    return attach_engineering_action(answer, proposal)


def attach_engineering_action(
    answer: ProjectAnswer,
    proposal: EngineeringProposal,
) -> ProjectAnswer:
    actions = tuple(
        [
            *answer.recommended_actions,
            proposal.action,
        ]
    )
    return answer.model_copy(update={"recommended_actions": actions})


def resolve_engineering_relative_path(
    frames: tuple[TracebackHint, ...],
    evidence: tuple[Evidence, ...],
) -> str | None:
    for item in evidence:
        if item.type != EvidenceType.CODE:
            continue
        frame = matching_traceback_frame(item, frames)
        if frame is None:
            continue
        file_name = str(item.metadata.get("file") or Path(frame.file).name).replace("\\", "/")
        return _normalize_src_path(file_name)
    if frames:
        return _normalize_src_path(Path(frames[0].file).name)
    return None


def engineering_action_from_answer(answer: ProjectAnswer) -> ActionProposal | None:
    return next(
        (
            action
            for action in answer.recommended_actions
            if action.tool_name == "engineering_proposal"
        ),
        None,
    )


def _normalize_src_path(file_name: str) -> str:
    cleaned = file_name.lstrip("./")
    if cleaned.startswith("src/"):
        return cleaned
    return f"src/{Path(cleaned).name}"
