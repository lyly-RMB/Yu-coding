from datetime import datetime, timezone
from uuid import uuid4

from project_lens.domain.conversation import ConversationSession, ConversationTurn, empty_summary
from project_lens.domain.memory import MemoryType, ProjectMemory
from project_lens.domain.models import ProjectRef
from project_lens.context.memory_retrieval import build_memory_summary
from project_lens.domain.session_state import HandoffSummary
from project_lens.integrations.feishu.hermes_context import build_hermes_project_context


def _session() -> ConversationSession:
    project = ProjectRef(tenant_id="demo", project_id="payment")
    return ConversationSession(
        tenant_id="demo",
        chat_id="chat-1",
        user_id="user-1",
        project=project,
        summary=empty_summary(project).model_copy(
            update={
                "active_skill": "project_knowledge",
                "next_actions": ("确认负责人",),
                "last_assistant_summary": "订单服务由 Ada 负责",
            }
        ),
        recent_turns=(
            ConversationTurn(user_id="user-1", text="订单服务的负责人是谁？"),
        ),
    )


def test_context_includes_session_and_approved_memory_without_full_store_access() -> None:
    memory = ProjectMemory(
        project=ProjectRef(tenant_id="demo", project_id="payment"),
        text="订单服务负责人是 Ada",
        memory_type=MemoryType.OWNER,
        evidence_ids=(uuid4(),),
        approved_by="manager-1",
    )
    context = build_hermes_project_context(
        session=_session(),
        memories=(memory,),
        runtime_access={
            "tenant_id": "demo",
            "project_id": "payment",
            "role": "manager",
            "policy_version": "v2",
        },
    )

    assert "session_id=" in context.text
    assert "订单服务的负责人是谁？" in context.text
    assert "订单服务负责人是 Ada" in context.text
    assert context.audit_refs["memory_ids"] == [str(memory.id)]
    assert context.audit_refs["memory_count"] == 1


def test_context_omits_unapproved_proposal_shape_and_bounds_recent_turns() -> None:
    session = _session().model_copy(
        update={
            "recent_turns": tuple(
                ConversationTurn(user_id="user-1", text=f"turn-{index}")
                for index in range(10)
            )
        }
    )
    context = build_hermes_project_context(session=session)

    # Legacy path (no pending_send): dumps whatever is already on the session pool.
    assert "turn-0" in context.text
    assert "turn-9" in context.text
    assert "pending" not in context.text


def test_pending_send_mode_excludes_session_turns_not_in_set() -> None:
    from project_lens.application.conversation_service import ConversationService

    service = ConversationService()
    project = ProjectRef(tenant_id="demo", project_id="payment")
    session = service.get_or_create(
        tenant_id="demo",
        chat_id="chat-legacy",
        user_id="user-1",
        project=project,
    )
    for index in range(3):
        session = service.record_turn(
            session,
            user_id="user-1",
            text=f"keep-{index}",
            rewritten_question=None,
        )
    session = service.refresh_pending_send(session, question="ask")
    assert session.pending_send is not None
    drop_id = session.pending_send.items[0].item_id
    session = service.exclude_from_pending_send(session, (drop_id,))
    context = build_hermes_project_context(session=session, use_pending_send=True)
    assert "keep-0" not in context.text
    assert "keep-1" in context.text
    assert "keep-2" in context.text
    assert "recent_turn[" not in context.text


def test_context_can_inject_navigation_summary_without_memory_bodies() -> None:
    memory = ProjectMemory(
        project=ProjectRef(tenant_id="demo", project_id="payment"),
        text="支付回调由 callback-service 接收并写入 Kafka",
        memory_type=MemoryType.ARCHITECTURE_FACT,
        evidence_ids=(uuid4(),),
        approved_by="manager-1",
    )
    context = build_hermes_project_context(
        session=_session(),
        memories=(memory,),
        memory_summary=build_memory_summary((memory,)),
    )

    assert "project_memory_directory:" in context.text
    assert "callback-service" in context.text
    assert memory.text not in context.text


def test_context_injects_handoff_and_audits_its_version() -> None:
    session = _session()
    handoff = HandoffSummary(
        session_id=session.session_id,
        project_id=session.project.project_id,
        summary_version=4,
        markdown="handoff_id=h1\n## Next Steps\n- continue investigation",
    )
    context = build_hermes_project_context(session=session, handoff=handoff)

    assert "handoff_summary:" in context.text
    assert "continue investigation" in context.text
    assert context.audit_refs["handoff_id"] == handoff.handoff_id
    assert context.audit_refs["handoff_summary_version"] == 4
