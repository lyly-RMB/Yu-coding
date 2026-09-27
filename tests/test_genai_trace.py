"""S05 GenAI trace archival: json under data/traces + SQLite path index."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from project_lens.application.genai_trace_service import (
    DEFAULT_GENAI_TRACE_RETENTION_DAYS,
    GenAITraceService,
    build_genai_trace,
)
from project_lens.application.hermes_runtime import HermesRuntimeService
from project_lens.application.run_service import InMemoryRunRepository, RunService
from project_lens.config import settings
from project_lens.domain.genai_trace import GenAITrace, TraceEntry
from project_lens.domain.identity import ActorContext
from project_lens.domain.models import AgentRun, ProjectAnswer, ProjectRef, RunStatus
from project_lens.integrations.feishu.hermes_context import HermesProjectContext
from project_lens.integrations.feishu.hermes_loop_runner import HermesLoopToolCall
from project_lens.integrations.feishu.hermes_tool_loop import FeishuHermesToolLoopResult
from project_lens.persistence.genai_trace_store import GenAITraceStore
from project_lens.persistence.sqlite import SQLiteDatabase
from project_lens.project_space.policies import (
    AnswerDepth,
    DEFAULT_READ_TOOLS,
    EffectiveAccessScope,
    RoleKind,
    VisibilityLevel,
)


def _project() -> ProjectRef:
    return ProjectRef(tenant_id="demo", project_id="payment")


def _answer(project: ProjectRef | None = None) -> ProjectAnswer:
    return ProjectAnswer(
        project=project or _project(),
        status="answered",
        business_summary="先查回调日志",
        technical_summary="先查回调日志",
        conclusion="先查回调日志",
        unknowns=(),
    )


def _run(*, question: str = "支付回调延迟怎么查？") -> AgentRun:
    project = _project()
    return AgentRun(
        project=project,
        user_id="u1",
        channel_id="chat-1",
        question=question,
        runtime="hermes",
        entry_mode="direct_answer",
        status=RunStatus.COMPLETED,
        answer=_answer(project),
    )


def _tool_call() -> HermesLoopToolCall:
    return HermesLoopToolCall(
        name="projectlens_search_context",
        arguments={"query": "支付回调"},
        envelope={
            "ok": True,
            "summary": "命中回调日志说明",
            "citations": [{"id": "ev-1"}],
            "tool_result_id": "tr-1",
        },
    )


def test_retention_default_is_30_days() -> None:
    assert DEFAULT_GENAI_TRACE_RETENTION_DAYS == 30
    assert settings.genai_trace_retention_days == 30


def test_write_trace_with_tool_call_and_find_by_project(tmp_path: Path) -> None:
    database = SQLiteDatabase(str(tmp_path / "t.db"))
    store = GenAITraceStore(database, root_dir=tmp_path / "traces")
    service = GenAITraceService(store, retention_days=30)
    run = _run()
    started = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    ended = started + timedelta(seconds=5)

    index = service.record_hermes_cycle(
        run=run,
        question=run.question,
        context_text="系统侧：摘要 + 近几轮",
        messages=(
            {"role": "user", "content": run.question},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true}'},
        ),
        tool_calls=(_tool_call(),),
        final_response="先查回调日志",
        tool_catalog=(
            {"name": "projectlens_search_context", "description": "search"},
        ),
        runtime_extras={"hermes_ok": True},
        started_at=started,
        ended_at=ended,
    )

    absolute = (tmp_path / "traces" / index.file_path)
    assert absolute.is_file()
    loaded = service.get_by_run_id(run.id)
    assert loaded is not None
    assert loaded.user_instruction == run.question
    assert loaded.tool_results
    assert loaded.tool_results[0]["body"]["summary"] == "命中回调日志说明"
    assert all(item.get("timestamp") for item in loaded.messages)
    assert all(item.get("timestamp") for item in loaded.tool_results)
    assert loaded.runtime_state.get("timestamp")
    ordered = loaded.ordered_entries()
    assert ordered[0].timestamp <= ordered[-1].timestamp

    paths = service.find_paths_by_project(_project())
    assert index.file_path in paths


def test_missing_timestamp_is_rejected(tmp_path: Path) -> None:
    database = SQLiteDatabase(str(tmp_path / "t.db"))
    store = GenAITraceStore(database, root_dir=tmp_path / "traces")
    run = _run()
    now = datetime(2026, 9, 23, 11, 0, tzinfo=timezone.utc)
    bad = GenAITrace.model_construct(
        schema_version=1,
        trace_id=run.trace_id,
        run_id=run.id,
        tenant_id=run.project.tenant_id,
        project_id=run.project.project_id,
        started_at=now,
        ended_at=now,
        user_instruction=run.question,
        messages=({"role": "user", "content": "x"},),  # no timestamp
        ai_replies=(),
        tool_results=(),
        tools=(),
        runtime_state={"status": "completed", "timestamp": now.isoformat()},
        entries=(
            TraceEntry(kind="user_instruction", timestamp=now, payload={"content": "x"}),
        ),
    )
    with pytest.raises(ValueError, match="timestamp"):
        store.write(bad)


def test_retention_prunes_traces_older_than_30_days(tmp_path: Path) -> None:
    database = SQLiteDatabase(str(tmp_path / "t.db"))
    store = GenAITraceStore(database, root_dir=tmp_path / "traces")
    service = GenAITraceService(store, retention_days=30)
    now = datetime(2026, 9, 23, tzinfo=timezone.utc)
    old_run = _run(question="旧问题")
    old_run = old_run.model_copy(update={"id": uuid4(), "trace_id": uuid4()})
    new_run = _run(question="新问题")
    new_run = new_run.model_copy(update={"id": uuid4(), "trace_id": uuid4()})

    old_index = service.record_hermes_cycle(
        run=old_run,
        question=old_run.question,
        context_text=None,
        messages=({"role": "user", "content": old_run.question},),
        tool_calls=(),
        final_response="旧答",
        started_at=now - timedelta(days=40),
        ended_at=now - timedelta(days=40),
    )
    new_index = service.record_hermes_cycle(
        run=new_run,
        question=new_run.question,
        context_text=None,
        messages=({"role": "user", "content": new_run.question},),
        tool_calls=(),
        final_response="新答",
        started_at=now - timedelta(days=2),
        ended_at=now - timedelta(days=2),
    )

    removed = service.apply_retention(now=now)
    assert removed == 1
    assert not (tmp_path / "traces" / old_index.file_path).exists()
    assert (tmp_path / "traces" / new_index.file_path).is_file()
    assert service.get_by_run_id(old_run.id) is None
    assert service.get_by_run_id(new_run.id) is not None


@pytest.mark.asyncio
async def test_hermes_runtime_archives_genai_trace(tmp_path: Path) -> None:
    database = SQLiteDatabase(str(tmp_path / "t.db"))
    store = GenAITraceStore(database, root_dir=tmp_path / "traces")
    genai = GenAITraceService(store, retention_days=30)
    run_service = RunService(InMemoryRunRepository())

    class _Bridge:
        async def answer(self, **kwargs):  # noqa: ANN003
            project = kwargs["project"]
            return FeishuHermesToolLoopResult(
                ok=True,
                envelope={"ok": True, "answer_summary": "先查回调日志"},
                tool_names=("projectlens_search_context",),
                loop_id=kwargs["loop_id"],
                trace_id=kwargs["trace_id"],
                messages=(
                    {"role": "user", "content": "支付回调延迟怎么查？"},
                    {"role": "assistant", "content": "先查回调日志"},
                ),
                tool_calls=(_tool_call(),),
                final_response="先查回调日志",
                verified_answer=_answer(project),
            )

    class _Tools:
        def list_tools(self) -> dict:
            return {
                "ok": True,
                "tools": [
                    {
                        "name": "projectlens_search_context",
                        "description": "search",
                    }
                ],
            }

    runtime = HermesRuntimeService(
        run_service=run_service,
        bridge=_Bridge(),  # type: ignore[arg-type]
        genai_trace_service=genai,
        tool_catalog_provider=_Tools(),
    )
    project = _project()
    actor = ActorContext(
        actor_id="u1",
        chat_id="chat-1",
        tenant_key="demo",
        chat_type="group",
        source="test_fixture",
        authenticated=True,
    )
    scope = EffectiveAccessScope(
        project=project,
        actor_id="u1",
        chat_id="chat-1",
        role=RoleKind.DEVELOPER,
        readable_sources=("knowledge/",),
        allowed_tools=DEFAULT_READ_TOOLS,
        forbidden_sources=(),
        answer_depth=AnswerDepth.DETAILED,
        answer_style="technical",
        visibility_level=VisibilityLevel.TEAM_SHARED,
        identity_source="test_fixture",
        chat_type="group",
        allow_private_details=False,
    )
    pending = runtime.prepare(
        project=project,
        actor=actor,
        scope=scope,
        question="支付回调延迟怎么查？",
        entry_mode="direct_answer",
        context=HermesProjectContext(text="系统侧上下文", audit_refs={}),
    )
    executed = await runtime.execute_prepared(pending)
    assert executed.run.status == RunStatus.COMPLETED
    trace = genai.get_by_run_id(executed.run.id)
    assert trace is not None
    assert trace.tool_results
    assert "支付回调" in trace.user_instruction
    assert genai.find_paths_by_project(project)


def test_build_genai_trace_stamps_every_collection() -> None:
    run = _run()
    trace = build_genai_trace(
        run=run,
        question=run.question,
        context_text="ctx",
        messages=({"role": "assistant", "content": "hi"},),
        tool_calls=(_tool_call(),),
        final_response="done",
        tool_catalog=({"name": "projectlens_search_context", "description": "d"},),
        started_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
        ended_at=datetime(2026, 9, 23, 0, 0, 1, tzinfo=timezone.utc),
    )
    assert all("timestamp" in item for item in trace.messages)
    assert all("timestamp" in item for item in trace.tool_results)
    assert all("timestamp" in item for item in trace.ai_replies)
    assert "timestamp" in trace.runtime_state
