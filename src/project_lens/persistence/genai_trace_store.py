"""File + SQLite index store for GenAI trace json archives."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from project_lens.domain.genai_trace import GenAITrace, GenAITraceIndexRecord
from project_lens.domain.models import ProjectRef
from project_lens.persistence.sqlite import SQLiteDatabase

_SCHEMA_COMPONENT = "genai_traces"
_SCHEMA_VERSION = 1


def _ensure_schema(database: SQLiteDatabase) -> None:
    def _v1(connection) -> None:  # noqa: ANN001
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS genai_traces (
                trace_id TEXT PRIMARY KEY,
                run_id TEXT,
                tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL,
                file_path TEXT NOT NULL,
                indexed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_genai_traces_project_started
                ON genai_traces (tenant_id, project_id, started_at);
            CREATE INDEX IF NOT EXISTS idx_genai_traces_run
                ON genai_traces (run_id);
            """
        )

    database.run_migrations(_SCHEMA_COMPONENT, {_SCHEMA_VERSION: _v1})


class GenAITraceStore:
    """Write GenAI traces under data/traces and keep a SQLite path index."""

    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        root_dir: str | Path,
    ) -> None:
        self._database = database
        self._root = Path(root_dir)
        _ensure_schema(database)

    @property
    def root_dir(self) -> Path:
        return self._root

    def write(self, trace: GenAITrace) -> GenAITraceIndexRecord:
        """Persist one validated trace json and upsert its index row."""

        self._assert_all_timestamps(trace)
        relative = self._relative_path(trace)
        absolute = self._root / relative
        absolute.parent.mkdir(parents=True, exist_ok=True)
        payload = json.loads(trace.model_dump_json())
        absolute.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        record = GenAITraceIndexRecord(
            trace_id=trace.trace_id,
            run_id=trace.run_id,
            tenant_id=trace.tenant_id,
            project_id=trace.project_id,
            started_at=trace.started_at,
            ended_at=trace.ended_at,
            file_path=relative.as_posix(),
        )
        self._database.execute(
            """
            INSERT OR REPLACE INTO genai_traces (
                trace_id, run_id, tenant_id, project_id,
                started_at, ended_at, file_path, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(record.trace_id),
                str(record.run_id) if record.run_id is not None else None,
                record.tenant_id,
                record.project_id,
                record.started_at.isoformat(),
                record.ended_at.isoformat(),
                record.file_path,
                record.indexed_at.isoformat(),
            ),
        )
        return record

    def get_by_trace_id(self, trace_id: UUID) -> GenAITrace | None:
        row = self._database.query_one(
            "SELECT file_path FROM genai_traces WHERE trace_id = ?",
            (str(trace_id),),
        )
        if row is None:
            return None
        return self._load_file(str(row["file_path"]))

    def get_by_run_id(self, run_id: UUID) -> GenAITrace | None:
        row = self._database.query_one(
            "SELECT file_path FROM genai_traces WHERE run_id = ? ORDER BY started_at DESC LIMIT 1",
            (str(run_id),),
        )
        if row is None:
            return None
        return self._load_file(str(row["file_path"]))

    def list_index_for_project(
        self,
        project: ProjectRef,
        *,
        limit: int = 100,
    ) -> tuple[GenAITraceIndexRecord, ...]:
        bounded = max(1, int(limit))
        rows = self._database.query_all(
            """
            SELECT trace_id, run_id, tenant_id, project_id, started_at, ended_at,
                   file_path, indexed_at
            FROM genai_traces
            WHERE tenant_id = ? AND project_id = ?
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (project.tenant_id, project.project_id, bounded),
        )
        return tuple(self._row_to_index(row) for row in rows)

    def find_paths_by_project(
        self,
        project: ProjectRef,
        *,
        limit: int = 100,
    ) -> tuple[str, ...]:
        return tuple(item.file_path for item in self.list_index_for_project(project, limit=limit))

    def prune_before(self, *, cutoff: datetime) -> int:
        """Delete index rows and json files older than cutoff. Returns removed count."""

        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        rows = self._database.query_all(
            "SELECT trace_id, file_path FROM genai_traces WHERE ended_at < ?",
            (cutoff.isoformat(),),
        )
        removed = 0
        for row in rows:
            path = self._root / str(row["file_path"])
            try:
                if path.is_file():
                    path.unlink()
            except OSError:
                pass
            self._database.execute(
                "DELETE FROM genai_traces WHERE trace_id = ?",
                (str(row["trace_id"]),),
            )
            removed += 1
        return removed

    def _load_file(self, relative_path: str) -> GenAITrace | None:
        path = self._root / relative_path
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return GenAITrace.model_validate(payload)
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def _relative_path(self, trace: GenAITrace) -> Path:
        started = trace.started_at.astimezone(timezone.utc)
        stamp = started.strftime("%Y%m%dT%H%M%SZ")
        return Path(trace.tenant_id) / trace.project_id / f"{stamp}_{trace.trace_id}.json"

    @staticmethod
    def _assert_all_timestamps(trace: GenAITrace) -> None:
        if trace.started_at is None or trace.ended_at is None:
            raise ValueError("trace started_at and ended_at are required")
        for entry in trace.entries:
            if getattr(entry, "timestamp", None) is None:
                raise ValueError("trace entry timestamp is required; refusing to write")
        for collection_name in ("messages", "ai_replies", "tool_results"):
            for item in getattr(trace, collection_name):
                if not isinstance(item, dict) or item.get("timestamp") is None:
                    raise ValueError(
                        f"{collection_name} entry missing timestamp; refusing to write"
                    )
        if trace.runtime_state and trace.runtime_state.get("timestamp") is None:
            raise ValueError("runtime_state timestamp is required; refusing to write")

    @staticmethod
    def _row_to_index(row) -> GenAITraceIndexRecord:  # noqa: ANN001
        run_raw = row["run_id"]
        return GenAITraceIndexRecord(
            trace_id=UUID(str(row["trace_id"])),
            run_id=UUID(str(run_raw)) if run_raw else None,
            tenant_id=str(row["tenant_id"]),
            project_id=str(row["project_id"]),
            started_at=_parse_dt(str(row["started_at"])),
            ended_at=_parse_dt(str(row["ended_at"])),
            file_path=str(row["file_path"]),
            indexed_at=_parse_dt(str(row["indexed_at"])),
        )


def _parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
