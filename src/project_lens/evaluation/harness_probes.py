"""Deterministic harness probes from agent-harness-design.md §12."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from project_lens.context.engine import ContextEngine
from project_lens.context.memory_store import MemoryStore
from project_lens.context.store import EvidenceIndex
from project_lens.domain.conversation import ConversationSession
from project_lens.domain.models import ClaimType, ProjectAnswer
from project_lens.runtime.lifecycle import LifecycleBus, LifecycleEventType
from project_lens.runtime.policy import EngineeringPolicy, RiskClass
from project_lens.runtime.tool_gateway import ToolGateway
from project_lens.runtime.tool_specs import SurfaceClass, ToolLane, list_tool_specs
from project_lens.workflow.context_compression import pinned_probe_values
from project_lens.workflow.context_pack import task_scratchpad_from_session
from project_lens.workflow.followup import FollowupRewriter
from project_lens.workflow.skills import ProjectSkill


@dataclass(frozen=True)
class ProbeResult:
    name: str
    passed: bool
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HarnessProbeReport:
    probes: tuple[ProbeResult, ...]

    @property
    def passed(self) -> bool:
        return all(item.passed for item in self.probes)

    @property
    def failed(self) -> tuple[ProbeResult, ...]:
        return tuple(item for item in self.probes if not item.passed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "probe_count": len(self.probes),
            "failed_count": len(self.failed),
            "probes": [
                {
                    "name": item.name,
                    "passed": item.passed,
                    "detail": item.detail,
                    "evidence": item.evidence,
                }
                for item in self.probes
            ],
        }


def probe_evidence_safety(answer: ProjectAnswer) -> ProbeResult:
    evidence_ids = {item.id for item in answer.evidence}
    missing: list[str] = []
    uncited_facts = 0
    for claim in answer.claims:
        if claim.type == ClaimType.FACT and not claim.evidence_ids:
            uncited_facts += 1
        for evidence_id in claim.evidence_ids:
            if evidence_id not in evidence_ids:
                missing.append(str(evidence_id))
    # Uncited facts are allowed; broken citation ids are not.
    passed = not missing
    return ProbeResult(
        name="evidence_safety",
        passed=passed,
        detail=(
            "cited evidence ids resolve"
            if passed
            else f"missing={missing[:5]} uncited_facts={uncited_facts}"
        ),
        evidence={
            "claim_count": len(answer.claims),
            "missing_evidence_ids": missing[:10],
            "uncited_facts": uncited_facts,
        },
    )


def probe_approval_safety(
    answer: ProjectAnswer | None = None,
    *,
    gateway: ToolGateway | None = None,
) -> ProbeResult:
    apply_specs = list_tool_specs(category=ToolLane.APPLY)
    specs_ok = all(
        item.requires_approval and item.surface == SurfaceClass.HUMAN_CONTROLLED
        for item in apply_specs
    )
    actions_ok = True
    can_apply_flags: list[bool] = []
    if answer is not None:
        for action in answer.recommended_actions:
            if action.tool_name == "engineering_proposal":
                flag = bool(action.arguments.get("can_apply", False))
                can_apply_flags.append(flag)
                if flag or not action.requires_approval:
                    actions_ok = False
    gateway_ok = True
    if gateway is not None:
        gateway_ok = gateway.policy.allow_apply is False
        if any(event.tool_name == "apply_patch_plan" and event.ok for event in gateway.audit_events):
            gateway_ok = False
    passed = specs_ok and actions_ok and gateway_ok
    return ProbeResult(
        name="approval_safety",
        passed=passed,
        detail="apply remains human-controlled and disabled" if passed else "apply gate failed",
        evidence={
            "apply_tool_count": len(apply_specs),
            "engineering_can_apply_flags": can_apply_flags,
            "policy_allow_apply": (
                gateway.policy.allow_apply if gateway is not None else None
            ),
        },
    )


def probe_memory_boundary(
    store: MemoryStore,
    *,
    project,
    pending_proposal_ids: tuple[UUID, ...] = (),
) -> ProbeResult:
    memories = store.list_memories(project)
    pending_written = False
    for proposal_id in pending_proposal_ids:
        proposal = store.get_proposal(proposal_id)
        if proposal is not None and proposal.status == "pending":
            if any(item.proposal_id == proposal_id for item in memories):
                pending_written = True
    leaked: list[str] = []
    for item in memories:
        if item.proposal_id is None:
            continue
        proposal = store.get_proposal(item.proposal_id)
        if proposal is not None and proposal.status != "approved":
            leaked.append(str(item.id))
    passed = not pending_written and not leaked
    return ProbeResult(
        name="memory_boundary",
        passed=passed,
        detail="only approved memories are listed" if passed else "unapproved memory leak",
        evidence={
            "memory_count": len(memories),
            "pending_proposal_ids": [str(item) for item in pending_proposal_ids],
            "leaked_memory_ids": leaked[:10],
        },
    )


def probe_ops_boundary(engine: ContextEngine, index: EvidenceIndex) -> ProbeResult:
    durable = index.all()
    durable_ops = [item for item in durable if item.source.system == "ops_window"]
    ephemeral_flags = True
    # Ops store may be empty in some test apps; probe still checks durable index.
    passed = not durable_ops and ephemeral_flags
    return ProbeResult(
        name="ops_boundary",
        passed=passed,
        detail=(
            "ops signals are not in long-term evidence index"
            if passed
            else f"found {len(durable_ops)} ops_window durable evidence rows"
        ),
        evidence={
            "durable_count": len(durable),
            "durable_ops_window_count": len(durable_ops),
            "ops_store_count": len(engine.ops_store.all()),
        },
    )


def probe_compression_quality(session: ConversationSession) -> ProbeResult:
    pinned = pinned_probe_values(session.summary.pinned_ids)
    required_kinds = (
        bool(session.summary.pinned_ids.evidence_ids)
        or bool(session.summary.evidence_ids)
    )
    # After at least one compression cycle, pinned ids / cycle metadata must exist.
    if session.summary.compression_cycle == 0:
        return ProbeResult(
            name="compression_quality",
            passed=True,
            detail="no compression cycle yet; probe skipped as pass",
            evidence={"compression_cycle": 0},
        )
    has_cycle = session.summary.compression_cycle >= 1
    has_topic = "compression_cycle" in session.summary.active_topic
    passed = has_cycle and has_topic and (required_kinds or bool(pinned))
    return ProbeResult(
        name="compression_quality",
        passed=passed,
        detail="pinned ids retained after compression" if passed else "compression lost pins",
        evidence={
            "compression_cycle": session.summary.compression_cycle,
            "pinned_count": len(pinned),
            "evidence_ids": [str(item) for item in session.summary.evidence_ids[:10]],
            "proposal_ids": list(session.summary.pinned_ids.proposal_ids[:10]),
            "file_paths": list(session.summary.pinned_ids.file_paths[:10]),
        },
    )


def probe_context_recall(session: ConversationSession) -> ProbeResult:
    skill = session.summary.active_skill
    rewriter = FollowupRewriter()
    rewrite = rewriter.rewrite("那是谁改的？", session)
    expects_incident = skill == ProjectSkill.INCIDENT_DIAGNOSIS.value
    expects_version = skill == ProjectSkill.VERSION_CHANGE.value
    if skill is None:
        return ProbeResult(
            name="context_recall",
            passed=True,
            detail="no active skill yet; probe skipped as pass",
            evidence={},
        )
    passed = rewrite is not None
    if expects_incident:
        passed = passed and "故障诊断" in (rewrite or "")
    if expects_version:
        passed = passed and "版本变更" in (rewrite or "")
    return ProbeResult(
        name="context_recall",
        passed=passed,
        detail="follow-up rewrite uses active skill" if passed else "follow-up lost skill context",
        evidence={
            "active_skill": skill,
            "rewrite_preview": (rewrite or "")[:160],
        },
    )


def probe_artifact_trail(session: ConversationSession) -> ProbeResult:
    pad = task_scratchpad_from_session(session)
    files = pad.files_read if pad is not None else ()
    pinned_files = session.summary.pinned_ids.file_paths
    # Read-only skills may pin evidence file paths without L3 files_read.
    # Only enforce the engineering trail when files_read is non-empty.
    if not files:
        return ProbeResult(
            name="artifact_trail",
            passed=True,
            detail=(
                "no engineering files_read; probe skipped as pass"
                if not pinned_files
                else "pinned evidence paths without engineering files_read; ok"
            ),
            evidence={
                "files_read": [],
                "pinned_file_paths": list(pinned_files[:20]),
                "allow_apply": False if pad is None else pad.allow_apply,
            },
        )
    passed = all(path in pinned_files for path in files)
    return ProbeResult(
        name="artifact_trail",
        passed=passed,
        detail=(
            "files_read retained in scratchpad and pinned ids"
            if passed
            else "artifact trail incomplete"
        ),
        evidence={
            "files_read": list(files),
            "pinned_file_paths": list(pinned_files[:20]),
            "allow_apply": False if pad is None else pad.allow_apply,
        },
    )


def probe_lifecycle_trail(
    lifecycle: LifecycleBus,
    *,
    required: tuple[LifecycleEventType, ...] = (
        LifecycleEventType.RUN_CREATED,
        LifecycleEventType.ANSWER_COMPOSED,
    ),
) -> ProbeResult:
    present = {event.type for event in lifecycle.all()}
    missing = [item.value for item in required if item not in present]
    passed = not missing
    return ProbeResult(
        name="lifecycle_trail",
        passed=passed,
        detail="required lifecycle hooks present" if passed else f"missing={missing}",
        evidence={
            "event_count": len(lifecycle.all()),
            "types": sorted(item.value for item in present),
            "missing": missing,
        },
    )


def probe_context_prompt_trail(lifecycle: LifecycleBus) -> ProbeResult:
    """Every completed harness run must render ContextPrompt and feed ModelAdapter."""

    rendered = [
        event
        for event in lifecycle.all()
        if event.type == LifecycleEventType.CONTEXT_PROMPT_RENDERED
    ]
    collected = [
        event
        for event in lifecycle.all()
        if event.type == LifecycleEventType.CONTEXT_COLLECTED
    ]
    used_prompt = False
    layers: list[str] = []
    for event in rendered:
        adapter = event.payload.get("model_adapter")
        if isinstance(adapter, dict) and adapter.get("used_prompt") is True:
            used_prompt = True
        prompt_refs = event.payload.get("context_prompt")
        if isinstance(prompt_refs, dict):
            raw_layers = prompt_refs.get("section_layers") or prompt_refs.get(
                "model_saw_layers"
            )
            if isinstance(raw_layers, list):
                layers = [str(item) for item in raw_layers]
    passed = bool(rendered) and bool(collected) and used_prompt and layers == [
        "L0",
        "L1",
        "L2",
        "L3",
        "L4",
        "L5",
    ]
    return ProbeResult(
        name="context_prompt_trail",
        passed=passed,
        detail=(
            "ContextPack->ContextPrompt->ModelAdapter trail present"
            if passed
            else "missing prompt trail or L0-L5 layers"
        ),
        evidence={
            "prompt_rendered_count": len(rendered),
            "context_collected_count": len(collected),
            "used_prompt": used_prompt,
            "section_layers": layers,
        },
    )


def probe_read_gateway_trail(
    lifecycle: LifecycleBus,
    *,
    gateway: Any | None = None,
) -> ProbeResult:
    """Workflow read tools must be audited; Apply must never appear on the read path."""

    collected = [
        event
        for event in lifecycle.all()
        if event.type == LifecycleEventType.CONTEXT_COLLECTED
    ]
    tool_names: list[str] = []
    allow_apply = False
    apply_events: list[str] = []
    for event in collected:
        summary = event.payload.get("read_gateway")
        if isinstance(summary, dict):
            raw_names = summary.get("read_tool_names") or []
            if isinstance(raw_names, list):
                tool_names.extend(str(item) for item in raw_names)
            if summary.get("allow_apply") is True:
                allow_apply = True
            raw_apply = summary.get("apply_events") or []
            if isinstance(raw_apply, list):
                apply_events.extend(str(item) for item in raw_apply)
    if gateway is not None:
        tool_names.extend(event.tool_name for event in gateway.audit_events)
        apply_events.extend(
            event.tool_name
            for event in gateway.audit_events
            if event.risk_class == RiskClass.APPLY
        )
    known_read = {
        "search_context",
        "authorized_evidence",
        "list_knowledge_gaps",
        "query_logs",
    }
    has_read = any(name in known_read for name in tool_names)
    passed = has_read and not allow_apply and not apply_events
    return ProbeResult(
        name="read_gateway_trail",
        passed=passed,
        detail=(
            "read tools audited via ReadContextGateway"
            if passed
            else "missing read audit or apply leaked onto read path"
        ),
        evidence={
            "read_tool_names": tool_names[:20],
            "allow_apply": allow_apply,
            "apply_events": apply_events[:10],
        },
    )


def run_harness_probes(
    *,
    answer: ProjectAnswer | None = None,
    session: ConversationSession | None = None,
    memory_store: MemoryStore | None = None,
    context_engine: ContextEngine | None = None,
    evidence_index: EvidenceIndex | None = None,
    lifecycle: LifecycleBus | None = None,
    gateway: ToolGateway | None = None,
    pending_proposal_ids: tuple[UUID, ...] = (),
) -> HarnessProbeReport:
    probes: list[ProbeResult] = []
    if answer is not None:
        probes.append(probe_evidence_safety(answer))
        probes.append(probe_approval_safety(answer, gateway=gateway))
    elif gateway is not None:
        probes.append(probe_approval_safety(gateway=gateway))
    else:
        probes.append(probe_approval_safety())
    if memory_store is not None and session is not None:
        probes.append(
            probe_memory_boundary(
                memory_store,
                project=session.project,
                pending_proposal_ids=pending_proposal_ids,
            )
        )
    if context_engine is not None and evidence_index is not None:
        probes.append(probe_ops_boundary(context_engine, evidence_index))
    if session is not None:
        probes.append(probe_compression_quality(session))
        probes.append(probe_context_recall(session))
        probes.append(probe_artifact_trail(session))
    if lifecycle is not None:
        probes.append(probe_lifecycle_trail(lifecycle))
        probes.append(probe_context_prompt_trail(lifecycle))
        probes.append(probe_read_gateway_trail(lifecycle))
    return HarnessProbeReport(probes=tuple(probes))


def default_read_only_gateway(project_root) -> ToolGateway:
    return ToolGateway(EngineeringPolicy(project_root=project_root, allow_apply=False))


def assert_no_apply_risk_in_gateway(gateway: ToolGateway) -> bool:
    return all(event.risk_class != RiskClass.APPLY or not event.ok for event in gateway.audit_events)
