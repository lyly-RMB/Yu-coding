"""Harness boundary probes: approval, memory, ops, evidence, compression, Feishu."""

from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from project_lens.application.conversation_service import ConversationService
from project_lens.application.memory_service import propose_memory_from_answer
from project_lens.context.bootstrap import (
    build_registered_context_engine,
    default_local_project_registrations,
)
from project_lens.context.memory_store import InMemoryMemoryStore
from project_lens.context.models import AccessContext
from project_lens.domain.models import (
    ActionProposal,
    Claim,
    ClaimType,
    Evidence,
    EvidenceGrade,
    EvidenceType,
    ProjectAnswer,
    ProjectRef,
    SourceRef,
)
from project_lens.integrations.feishu.adapter import RecordingFeishuMessenger
from project_lens.integrations.feishu.hermes_tool_loop import FeishuHermesToolLoopResult
from project_lens.integrations.feishu.identity import parse_project_bindings
from project_lens.main import create_app
from project_lens.runtime.policy import EngineeringPolicy, RiskClass
from project_lens.runtime.tool_gateway import ToolGateway
from project_lens.runtime.tool_specs import (
    SurfaceClass,
    ToolLane,
    assert_tool_callable,
    get_tool_spec,
    list_tool_specs,
    tool_policy_hash,
)
from project_lens.workflow.context_compression import pinned_probe_values
from project_lens.workflow.context_pack import TaskScratchpad, task_scratchpad_from_session
from project_lens.workflow.followup import FollowupRewriter

ROOT = Path(__file__).parents[1]
DEMO = ROOT / "examples" / "payment_service"
FEISHU_SERVICE = ROOT / "src" / "project_lens" / "integrations" / "feishu" / "service.py"

TRACEBACK = """Traceback (most recent call last):
  File "order_service.py", line 16, in create_order
    coupon_id = request.coupon.id
AttributeError: 'NoneType' object has no attribute 'id'
"""


def _project() -> ProjectRef:
    return ProjectRef(
        tenant_id="demo",
        project_id="payment",
        service="order-service",
        environment="production",
    )


class _FakeHermesEngineeringBridge:
    async def answer(self, **kwargs):  # noqa: ANN003
        project = kwargs["project"]
        evidence_id = uuid4()
        proposal_id = uuid4()
        file_path = "src/order_service.py"
        evidence = Evidence(
            id=evidence_id,
            type=EvidenceType.CODE,
            project=project,
            source=SourceRef(system="local", source_id="order_service.py"),
            content="create_order",
            observed_at=datetime.now(timezone.utc),
            access_scope="project:payment:read",
            content_hash="1234567890abcdefcc",
            metadata={"file": file_path},
        )
        answer = ProjectAnswer(
            project=project,
            skill="incident_diagnosis",
            confidence=0.7,
            status="identified",
            business_summary="failure",
            technical_summary="null coupon",
            claims=(
                Claim(
                    text="coupon null",
                    type=ClaimType.FACT,
                    evidence_ids=(evidence.id,),
                    grade=EvidenceGrade.B,
                ),
            ),
            evidence=(evidence,),
            recommended_actions=(
                ActionProposal(
                    id=proposal_id,
                    title="fix",
                    tool_name="engineering_proposal",
                    requires_approval=True,
                    arguments={
                        "affected_paths": [file_path],
                        "diff_summary": "+guard",
                        "can_apply": False,
                        "allow_apply": False,
                        "test_passed": True,
                        "explanation": "guard",
                        "failed_attempts": [],
                        "test_commands": ['python -c "print(\'ok\')"'],
                        "patch_plan_text": "Guard optional coupon",
                    },
                ),
            ),
        )
        return FeishuHermesToolLoopResult(
            ok=True,
            envelope={
                "ok": True,
                "audit_ref": {
                    "allow_apply": False,
                    "loop_id": str(kwargs["loop_id"]),
                    "trace_id": str(kwargs["trace_id"]),
                },
            },
            tool_names=("projectlens_search_context",),
            loop_id=kwargs["loop_id"],
            trace_id=kwargs["trace_id"],
            verified_answer=answer,
        )


def _configure_feishu(app) -> None:
    verifier = app.state.feishu_event_service._verifier
    verifier._verification_token = "project-lens-local-token"
    verifier._signing_secret = None
    app.state.feishu_event_service._identity_mapper = parse_project_bindings(
        "",
        default_project=_project(),
        allow_demo_fallback=True,
    )
    app.state.feishu_messenger = RecordingFeishuMessenger()
    app.state.feishu_event_service._messenger = app.state.feishu_messenger
    app.state.feishu_event_service._context_preview_enabled = False
    app.state.feishu_hermes_tool_loop_bridge.answer = _FakeHermesEngineeringBridge().answer
    if getattr(app.state, "hermes_runtime_service", None) is not None:
        app.state.hermes_runtime_service._bridge = app.state.feishu_hermes_tool_loop_bridge


def test_tool_specs_lock_apply_as_human_controlled() -> None:
    apply_tools = list_tool_specs(category=ToolLane.APPLY)
    assert apply_tools
    assert all(item.requires_approval for item in apply_tools)
    assert all(item.surface == SurfaceClass.HUMAN_CONTROLLED for item in apply_tools)
    assert all(item.risk_class == RiskClass.APPLY for item in apply_tools)
    with pytest.raises(PermissionError, match="human-controlled|disabled"):
        assert_tool_callable("apply_patch_plan", allow_apply=False)
    assert_tool_callable("read_project_file", allow_apply=False)
    # Memory decide is APPROVAL-lane (writes ProjectMemory) but not Engineering Apply.
    assert_tool_callable("decide_memory_proposal", allow_apply=False)
    assert_tool_callable("create_memory_proposal", allow_apply=False)
    assert get_tool_spec("validate_patch_plan").category == ToolLane.VALIDATE
    # L0 tool_policy_hash must be stable for a given locked registry.
    assert tool_policy_hash() == tool_policy_hash()
    assert len(tool_policy_hash()) == 16


def test_approval_safety_apply_is_audited_and_blocked() -> None:
    gateway = ToolGateway(EngineeringPolicy(project_root=DEMO, allow_apply=False))
    plan = gateway.create_patch_plan(
        title="noop",
        rationale="boundary",
        patches=[
            {
                "path": "src/order_service.py",
                "old_text": "coupon_id = request.coupon.id",
                "new_text": "coupon_id = request.coupon.id",
            }
        ],
    )
    with pytest.raises(PermissionError):
        gateway.apply_patch_plan(plan)
    denied = [event for event in gateway.audit_events if event.tool_name == "apply_patch_plan"]
    assert denied
    assert denied[-1].ok is False
    assert denied[-1].risk_class == RiskClass.APPLY


def test_memory_boundary_proposal_does_not_write_until_approval() -> None:
    evidence = Evidence(
        type=EvidenceType.DOCUMENT,
        project=_project(),
        source=SourceRef(system="feishu_doc", source_id="docx_payment_owners"),
        content="order-service owner is Ada",
        observed_at=datetime.now(timezone.utc),
        access_scope="project:payment:read",
        content_hash="1234567890abcdefaa",
    )
    answer = ProjectAnswer(
        project=_project(),
        status="identified",
        business_summary="owner known",
        technical_summary="owner known",
        claims=(
            Claim(
                text="order-service owner is Ada",
                type=ClaimType.FACT,
                evidence_ids=(evidence.id,),
                grade=EvidenceGrade.B,
            ),
        ),
        evidence=(evidence,),
    )
    proposal = propose_memory_from_answer(answer, proposed_by="u1")
    assert proposal is not None
    store = InMemoryMemoryStore()
    store.create_proposal(proposal)
    assert store.list_memories(_project()) == ()
    _updated, memory = store.decide_proposal(
        proposal.id,
        approved=True,
        decided_by="approver",
    )
    assert memory is not None
    assert store.list_memories(_project())
    # Reject path never writes.
    rejected = propose_memory_from_answer(answer, proposed_by="u2")
    assert rejected is not None
    # New proposal id required; reuse text is fine for boundary check via second store.
    store2 = InMemoryMemoryStore()
    created = store2.create_proposal(rejected)
    store2.decide_proposal(created.id, approved=False, decided_by="approver")
    assert store2.list_memories(_project()) == ()


def test_ops_boundary_signals_stay_out_of_long_term_index() -> None:
    engine, index = build_registered_context_engine(
        default_local_project_registrations(ROOT)
    )
    assert engine.ops_store.all()
    assert all(item.source.system != "ops_window" for item in index.all())
    access = AccessContext(
        tenant_id="demo",
        user_id="u1",
        permissions=frozenset({"project:payment:read"}),
    )
    from project_lens.domain.ops import OpsQuery

    finding = engine.query_ops(
        OpsQuery(
            project=_project(),
            start=datetime(2026, 7, 20, 9, tzinfo=timezone.utc),
            end=datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
        ),
        access,
    )
    assert finding.evidence
    assert all(item.metadata.get("ephemeral") is True for item in finding.evidence)
    # Ephemeral ops evidence must not be upserted into the durable index.
    durable_ids = {item.id for item in index.all()}
    assert durable_ids.isdisjoint({item.id for item in finding.evidence})


def test_evidence_safety_facts_require_authorized_evidence() -> None:
    with pytest.raises(ValueError, match="evidence"):
        Claim(
            text="unsupported fact",
            type=ClaimType.FACT,
            grade=EvidenceGrade.A,
        )
    evidence = Evidence(
        type=EvidenceType.DOCUMENT,
        project=_project(),
        source=SourceRef(system="local", source_id="x"),
        content="ok",
        observed_at=datetime.now(timezone.utc),
        access_scope="project:payment:read",
        content_hash="1234567890abcdefbb",
    )
    with pytest.raises(ValueError, match="missing evidence"):
        ProjectAnswer(
            project=_project(),
            status="bad",
            business_summary="b",
            technical_summary="t",
            claims=(
                Claim(
                    text="points elsewhere",
                    type=ClaimType.FACT,
                    evidence_ids=(uuid4(),),
                    grade=EvidenceGrade.B,
                ),
            ),
            evidence=(evidence,),
        )


def test_compression_quality_keeps_pinned_ids_after_overflow() -> None:
    service = ConversationService(recent_turn_limit=2)
    project = _project()
    session = service.get_or_create(
        tenant_id="demo",
        chat_id="chat-1",
        user_id="u1",
        project=project,
    )
    evidence_id = uuid4()
    proposal_id = uuid4()
    file_path = "src/order_service.py"
    evidence = Evidence(
        id=evidence_id,
        type=EvidenceType.CODE,
        project=project,
        source=SourceRef(system="local", source_id="order_service.py"),
        content="create_order",
        observed_at=datetime.now(timezone.utc),
        access_scope="project:payment:read",
        content_hash="1234567890abcdefcc",
        metadata={"file": file_path, "commit_sha": "abc1234"},
    )
    answer = ProjectAnswer(
        project=project,
        skill="incident_diagnosis",
        confidence=0.7,
        status="identified",
        business_summary="failure",
        technical_summary="null coupon",
        claims=(
            Claim(
                text="coupon null",
                type=ClaimType.FACT,
                evidence_ids=(evidence.id,),
                grade=EvidenceGrade.B,
            ),
        ),
        evidence=(evidence,),
        recommended_actions=(
            ActionProposal(
                id=proposal_id,
                title="fix",
                tool_name="engineering_proposal",
                requires_approval=True,
                arguments={
                    "affected_paths": [file_path],
                    "diff_summary": "+guard",
                    "can_apply": False,
                    "test_passed": True,
                    "explanation": "guard",
                },
            ),
        ),
    )
    session = service.record_turn(
        session,
        user_id="u1",
        text=TRACEBACK,
        rewritten_question=None,
        run_id=uuid4(),
        answer=answer,
        task_state=TaskScratchpad(files_read=(file_path,), allow_apply=False),
    )
    for index in range(2):
        session = service.record_turn(
            session,
            user_id="u1",
            text=f"ping-{index}",
            rewritten_question=None,
            run_id=uuid4(),
        )
    probe = pinned_probe_values(session.summary.pinned_ids)
    assert str(evidence_id) in probe
    assert str(proposal_id) in probe
    assert file_path in probe
    assert session.summary.compression_cycle >= 1


def test_context_recall_followup_rewrites_with_prior_skill() -> None:
    service = ConversationService()
    session = service.get_or_create(
        tenant_id="demo",
        chat_id="chat-1",
        user_id="u1",
        project=_project(),
    )
    evidence = Evidence(
        type=EvidenceType.CODE,
        project=_project(),
        source=SourceRef(system="local", source_id="x"),
        content="x",
        observed_at=datetime.now(timezone.utc),
        access_scope="project:payment:read",
        content_hash="1234567890abcdefdd",
    )
    session = service.record_turn(
        session,
        user_id="u1",
        text=TRACEBACK,
        rewritten_question=None,
        run_id=uuid4(),
        answer=ProjectAnswer(
            project=_project(),
            skill="incident_diagnosis",
            confidence=0.6,
            status="identified",
            business_summary="incident",
            technical_summary="traceback",
            claims=(
                Claim(
                    text="error",
                    type=ClaimType.FACT,
                    evidence_ids=(evidence.id,),
                    grade=EvidenceGrade.B,
                ),
            ),
            evidence=(evidence,),
        ),
    )
    rewritten = FollowupRewriter().rewrite("那是谁改的？", session)
    assert rewritten is not None
    assert "故障诊断" in rewritten
    assert session.summary.active_skill == "incident_diagnosis"


def test_artifact_trail_keeps_files_read_in_scratchpad() -> None:
    service = ConversationService()
    session = service.get_or_create(
        tenant_id="demo",
        chat_id="chat-1",
        user_id="u1",
        project=_project(),
    )
    path = "src/order_service.py"
    session = service.record_turn(
        session,
        user_id="u1",
        text="怎么修？",
        rewritten_question=None,
        run_id=uuid4(),
        task_state=TaskScratchpad(
            phase="Validate",
            files_read=(path,),
            patch_plan="null guard",
            diff_summary="+guard",
            approval_status="pending",
            allow_apply=False,
        ),
    )
    pad = task_scratchpad_from_session(session)
    assert pad is not None
    assert path in pad.files_read
    assert pad.allow_apply is False
    assert path in session.summary.pinned_ids.file_paths


def test_feishu_adapter_does_not_import_evidence_index() -> None:
    tree = ast.parse(FEISHU_SERVICE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            for alias in node.names:
                imported.add(f"{node.module}.{alias.name}")
    joined = " ".join(sorted(imported))
    assert "EvidenceIndex" not in joined
    assert "evidence_index" not in joined
    assert "project_lens.context.store" not in joined


def test_feishu_engineering_card_never_offers_apply() -> None:
    app = create_app()
    _configure_feishu(app)
    client = TestClient(app)
    response = client.post(
        "/api/v1/feishu/events",
        json={
            "schema": "2.0",
            "token": "project-lens-local-token",
            "header": {
                "event_id": "harness-boundary-apply",
                "event_type": "im.message.receive_v1",
                "tenant_key": "demo",
            },
            "event": {
                "sender": {"sender_id": {"open_id": "feishu-user-1", "user_id": "feishu-user-1"}},
                "message": {
                    "message_id": "message-harness-boundary",
                    "chat_id": "chat-1",
                    "chat_type": "group",
                    "message_type": "text",
                    "content": json.dumps({"text": TRACEBACK}),
                },
            },
        },
    )
    assert response.status_code == 200
    run = app.state.run_service.get(UUID(response.json()["run_id"]))
    assert run is not None and run.answer is not None
    eng = next(
        action
        for action in run.answer.recommended_actions
        if action.tool_name == "engineering_proposal"
    )
    assert eng.arguments.get("can_apply") is False
    card = next(
        message
        for message in app.state.feishu_messenger.messages
        if message.message_type == "interactive"
    )
    card_text = "\n".join(
        element.get("content", "") for element in card.content["elements"]
    )
    assert "不会执行 Apply" in card_text
    assert "立即应用" not in card_text
    # Session L2 summary must not become ProjectMemory.
    session = app.state.conversation_service.get_or_create(
        tenant_id="demo",
        chat_id="chat-1",
        user_id="feishu-user-1",
        project=_project(),
    )
    assert app.state.memory_store.list_memories(_project()) == ()
    assert session.summary.active_skill == "incident_diagnosis"
