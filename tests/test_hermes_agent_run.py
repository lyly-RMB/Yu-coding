from project_lens.application.run_service import InMemoryRunRepository, RunService
from project_lens.domain.models import ProjectAnswer, ProjectRef, RunStatus
import pytest


@pytest.mark.asyncio
async def test_complete_external_persists_verified_hermes_answer() -> None:
    repository = InMemoryRunRepository()
    service = RunService(repository=repository, agent_mode="hermes")
    run = service.create(
        project=ProjectRef(tenant_id="demo", project_id="payment"),
        user_id="user-1",
        question="What is the project owner?",
        entry_mode="natural_project_question",
    )
    answer = ProjectAnswer(
        project=run.project,
        status="unknown",
        business_summary="资料不足",
        technical_summary="资料不足",
        unknowns=("需要更多证据",),
    )

    completed = await service.complete_external(
        run.id,
        answer=answer,
        agent_mode="hermes",
        metadata={"loop_id": "loop-1"},
    )

    assert completed is not None
    assert completed.status == RunStatus.COMPLETED
    assert completed.answer == answer
    assert service.get(run.id).answer == answer  # type: ignore[union-attr]


def test_project_answer_answer_text_is_backward_compatible() -> None:
    answer = ProjectAnswer(
        project=ProjectRef(tenant_id="demo", project_id="payment"),
        status="unknown",
        business_summary="资料不足",
        technical_summary="资料不足",
        answer_text="产品：待确认\n研发：待确认\n测试：待确认",
    )
    restored = ProjectAnswer.model_validate_json(answer.model_dump_json())
    assert restored.answer_text == answer.answer_text

    legacy = answer.model_dump()
    legacy.pop("answer_text")
    assert ProjectAnswer.model_validate(legacy).answer_text == ""
