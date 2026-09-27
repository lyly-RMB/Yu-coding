"""Build, persist, and expire GenAI instruction-cycle traces."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from project_lens.domain.genai_trace import GenAITrace, GenAITraceIndexRecord, TraceEntry
from project_lens.domain.models import AgentRun, ProjectRef, utc_now
from project_lens.persistence.genai_trace_store import GenAITraceStore

DEFAULT_GENAI_TRACE_RETENTION_DAYS = 30


class GenAITraceService:
    """Application facade for GenAI trace archival (replaces AgentRun table body)."""

    def __init__(
        self,
        store: GenAITraceStore,
        *,
        retention_days: int = DEFAULT_GENAI_TRACE_RETENTION_DAYS,
    ) -> None:
        self._store = store
        self._retention_days = max(1, int(retention_days))

    @property
    def retention_days(self) -> int:
        return self._retention_days

    @property
    def store(self) -> GenAITraceStore:
        return self._store

    def record_hermes_cycle(
        self,
        *,
        run: AgentRun,
        question: str,
        context_text: str | None,
        messages: tuple[Any, ...] | list[Any],
        tool_calls: tuple[Any, ...] | list[Any],
        final_response: str,
        tool_catalog: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
        runtime_extras: dict[str, Any] | None = None,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
    ) -> GenAITraceIndexRecord:
        """Archive one Hermes instruction cycle and return the index row."""

        trace = build_genai_trace(
            run=run,
            question=question,
            context_text=context_text,
            messages=messages,
            tool_calls=tool_calls,
            final_response=final_response,
            tool_catalog=tool_catalog,
            runtime_extras=runtime_extras,
            started_at=started_at,
            ended_at=ended_at,
        )
        return self._store.write(trace)

    def get_by_run_id(self, run_id: UUID) -> GenAITrace | None:
        return self._store.get_by_run_id(run_id)

    def get_by_trace_id(self, trace_id: UUID) -> GenAITrace | None:
        return self._store.get_by_trace_id(trace_id)

    def find_paths_by_project(
        self,
        project: ProjectRef,
        *,
        limit: int = 100,
    ) -> tuple[str, ...]:
        return self._store.find_paths_by_project(project, limit=limit)

    def list_index_for_project(
        self,
        project: ProjectRef,
        *,
        limit: int = 100,
    ) -> tuple[GenAITraceIndexRecord, ...]:
        return self._store.list_index_for_project(project, limit=limit)

    def apply_retention(self, *, now: datetime | None = None) -> int:
        """Delete traces older than retention_days. Returns removed count."""

        current = now or utc_now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        cutoff = current - timedelta(days=self._retention_days)
        return self._store.prune_before(cutoff=cutoff)


def build_genai_trace(
    *,
    run: AgentRun,
    question: str,
    context_text: str | None,
    messages: tuple[Any, ...] | list[Any],
    tool_calls: tuple[Any, ...] | list[Any],
    final_response: str,
    tool_catalog: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
    runtime_extras: dict[str, Any] | None = None,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> GenAITrace:
    """Stamp every item and assemble a GenAITrace ready for disk."""

    start = started_at or run.created_at or utc_now()
    end = ended_at or utc_now()
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    if end < start:
        end = start

    entries: list[TraceEntry] = [
        TraceEntry(
            kind="trace_boundary",
            timestamp=start,
            payload={"boundary": "start", "trace_id": str(run.trace_id)},
        ),
        TraceEntry(
            kind="user_instruction",
            timestamp=start,
            payload={"content": question},
        ),
    ]

    stamped_messages: list[dict[str, Any]] = []
    cursor = start
    if context_text and context_text.strip():
        cursor = _step(cursor)
        system_message = {
            "role": "system",
            "content": context_text,
            "timestamp": cursor.isoformat(),
            "source": "hermes_project_context",
        }
        stamped_messages.append(system_message)
        entries.append(
            TraceEntry(kind="message", timestamp=cursor, payload=dict(system_message))
        )

    for raw in messages:
        cursor = _step(cursor)
        stamped = _stamp_message(raw, timestamp=cursor)
        stamped_messages.append(stamped)
        entries.append(
            TraceEntry(kind="message", timestamp=cursor, payload=dict(stamped))
        )

    stamped_tool_results: list[dict[str, Any]] = []
    for index, call in enumerate(tool_calls):
        started = _step(cursor)
        finished = _step(started)
        cursor = finished
        name = str(getattr(call, "name", "") or "")
        arguments = dict(getattr(call, "arguments", None) or {})
        envelope = dict(getattr(call, "envelope", None) or {})
        body = _tool_result_body(envelope)
        item = {
            "index": index,
            "tool_name": name,
            "arguments": arguments,
            "ok": envelope.get("ok") is True,
            "body": body,
            "started_at": started.isoformat(),
            "ended_at": finished.isoformat(),
            "timestamp": finished.isoformat(),
        }
        stamped_tool_results.append(item)
        entries.append(
            TraceEntry(kind="tool_result", timestamp=finished, payload=dict(item))
        )

    stamped_replies: list[dict[str, Any]] = []
    reply_ts = _step(cursor)
    cursor = reply_ts
    final_text = (final_response or "").strip()
    if final_text:
        reply = {
            "kind": "final",
            "content": final_text,
            "timestamp": reply_ts.isoformat(),
        }
        stamped_replies.append(reply)
        entries.append(
            TraceEntry(kind="ai_reply", timestamp=reply_ts, payload=dict(reply))
        )

    runtime_ts = end if end >= cursor else _step(cursor)
    runtime_state: dict[str, Any] = {
        "timestamp": runtime_ts.isoformat(),
        "status": run.status.value if hasattr(run.status, "value") else str(run.status),
        "runtime": run.runtime,
        "entry_mode": run.entry_mode,
        "error": run.error,
        "hermes_loop_id": str(run.hermes_loop_id) if run.hermes_loop_id else None,
        "tool_count": len(stamped_tool_results),
        "message_count": len(stamped_messages),
    }
    if runtime_extras:
        runtime_state.update(runtime_extras)
        runtime_state["timestamp"] = runtime_ts.isoformat()

    entries.append(
        TraceEntry(kind="runtime_state", timestamp=runtime_ts, payload=dict(runtime_state))
    )
    entries.append(
        TraceEntry(
            kind="trace_boundary",
            timestamp=runtime_ts,
            payload={"boundary": "end", "trace_id": str(run.trace_id)},
        )
    )

    tools = tuple(
        {
            "name": str(item.get("name") or ""),
            "description": str(item.get("description") or ""),
        }
        for item in tool_catalog
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    )

    return GenAITrace(
        trace_id=run.trace_id,
        run_id=run.id,
        tenant_id=run.project.tenant_id,
        project_id=run.project.project_id,
        started_at=start,
        ended_at=runtime_ts,
        user_instruction=question,
        messages=tuple(stamped_messages),
        ai_replies=tuple(stamped_replies),
        tool_results=tuple(stamped_tool_results),
        tools=tools,
        runtime_state=runtime_state,
        entries=tuple(entries),
    )


def _step(moment: datetime) -> datetime:
    return moment + timedelta(microseconds=1_000)


def _stamp_message(raw: Any, *, timestamp: datetime) -> dict[str, Any]:
    if isinstance(raw, dict):
        stamped = dict(raw)
    else:
        stamped = {"role": "unknown", "content": str(raw)}
    stamped["timestamp"] = timestamp.isoformat()
    return stamped


def _tool_result_body(envelope: dict[str, Any]) -> Any:
    """Prefer full tool payload; fall back to summary / error text."""

    for key in ("result", "data", "content", "payload"):
        if key in envelope and envelope[key] is not None:
            return envelope[key]
    if envelope.get("ok") is True:
        return {
            "summary": envelope.get("summary"),
            "citations": envelope.get("citations"),
            "tool_result_id": envelope.get("tool_result_id"),
            "unknowns": envelope.get("unknowns"),
        }
    return {
        "error": envelope.get("error") or envelope.get("message"),
        "agent_recovery_hint": envelope.get("agent_recovery_hint"),
        "error_code": envelope.get("error_code"),
        "summary": envelope.get("summary"),
    }
