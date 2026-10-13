"""Knowledge-gap persistence ports and SQLite/in-memory adapters."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from threading import RLock
from typing import Protocol
from typing import Literal
from uuid import UUID

from project_lens.domain.knowledge_gap import (
    KnowledgeGapCase,
    KnowledgeGapObservation,
    KnowledgeGapStateEvent,
    KnowledgeGapAction,
    KnowledgeGapRecheck,
    KnowledgeGapRecheckJob,
)
from project_lens.domain.models import ProjectRef


class KnowledgeGapStore(Protocol):
    def get(self, gap_id: UUID) -> KnowledgeGapCase | None: ...
    def get_by_key(self, project: ProjectRef, canonical_key: str) -> KnowledgeGapCase | None: ...
    def list_for_project(self, project: ProjectRef) -> tuple[KnowledgeGapCase, ...]: ...
    def record(self, case: KnowledgeGapCase, observation: KnowledgeGapObservation) -> None: ...
    def save_case(self, case: KnowledgeGapCase) -> None: ...
    def append_state_event(self, event: KnowledgeGapStateEvent) -> None: ...
    def observations_for_gap(self, gap_id: UUID) -> tuple[KnowledgeGapObservation, ...]: ...
    def events_for_gap(self, gap_id: UUID) -> tuple[KnowledgeGapStateEvent, ...]: ...
    def actions_for_gap(self, gap_id: UUID) -> tuple[KnowledgeGapAction, ...]: ...
    def save_action(self, action: KnowledgeGapAction) -> None: ...
    def get_action(self, action_id: UUID) -> KnowledgeGapAction | None: ...
    def save_recheck(self, recheck: KnowledgeGapRecheck) -> None: ...
    def rechecks_for_gap(self, gap_id: UUID) -> tuple[KnowledgeGapRecheck, ...]: ...
    def enqueue_recheck_job(self, job: KnowledgeGapRecheckJob) -> KnowledgeGapRecheckJob | None: ...
    def list_recheck_jobs(self, project: ProjectRef, *, retry_failed: bool = True) -> tuple[KnowledgeGapRecheckJob, ...]: ...
    def update_recheck_job(self, job: KnowledgeGapRecheckJob) -> None: ...
    def record_digest_run(self, run) -> object: ...
    def get_digest_run(self, digest_key: str): ...
    def refresh_digest_run(self, run): ...
    def transition_digest_run(self, digest_key: str, *, expected_summary_hash: str, status: Literal["pending", "sent", "failed"], recipient_ids: tuple[str, ...], at: datetime): ...
    def claim_digest_notification(self, notification_key: str, *, gap_id: UUID, project: ProjectRef, recipient_id: str, event_type: str, period_key: str, at: datetime) -> bool: ...


class SQLiteGapDatabase(Protocol):
    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor: ...
    def query_one(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Row | None: ...
    def query_all(self, sql: str, parameters: tuple[object, ...] = ()) -> list[sqlite3.Row]: ...
    def run_migrations(self, component: str, migrations: dict[int, object]) -> None: ...


class InMemoryKnowledgeGapStore:
    def __init__(self) -> None:
        self._cases: dict[UUID, KnowledgeGapCase] = {}
        self._observations: list[KnowledgeGapObservation] = []
        self._events: list[KnowledgeGapStateEvent] = []
        self._actions: dict[UUID, KnowledgeGapAction] = {}
        self._rechecks: list[KnowledgeGapRecheck] = []
        self._recheck_jobs: dict[str, KnowledgeGapRecheckJob] = {}
        self._digest_runs: dict[str, object] = {}
        self._digest_notifications: set[str] = set()
        self._lock = RLock()

    def get(self, gap_id: UUID) -> KnowledgeGapCase | None:
        return self._cases.get(gap_id)

    def get_by_key(self, project: ProjectRef, canonical_key: str) -> KnowledgeGapCase | None:
        return next((case for case in self._cases.values() if _same_project(case.project, project) and case.canonical_key == canonical_key), None)

    def list_for_project(self, project: ProjectRef) -> tuple[KnowledgeGapCase, ...]:
        return tuple(sorted((case for case in self._cases.values() if _same_project(case.project, project)), key=lambda c: (c.created_at, str(c.id))))

    def record(self, case: KnowledgeGapCase, observation: KnowledgeGapObservation) -> None:
        existing = self.get_by_key(case.project, case.canonical_key)
        if existing is not None and existing.id != case.id:
            # Keep identity stable when callers concurrently observed the same key.
            case = case.model_copy(update={"id": existing.id})
            observation = observation.model_copy(update={"gap_id": existing.id})
        if observation.id in {item.id for item in self._observations}:
            return
        self._cases[case.id] = case
        self._observations.append(observation)

    def save_case(self, case: KnowledgeGapCase) -> None:
        self._cases[case.id] = case

    def append_state_event(self, event: KnowledgeGapStateEvent) -> None:
        self._events.append(event)

    def observations_for_gap(self, gap_id: UUID) -> tuple[KnowledgeGapObservation, ...]:
        return tuple(item for item in self._observations if item.gap_id == gap_id)

    def events_for_gap(self, gap_id: UUID) -> tuple[KnowledgeGapStateEvent, ...]:
        return tuple(item for item in self._events if item.gap_id == gap_id)

    def actions_for_gap(self, gap_id: UUID) -> tuple[KnowledgeGapAction, ...]:
        return tuple(item for item in self._actions.values() if item.gap_id == gap_id)

    def save_action(self, action: KnowledgeGapAction) -> None:
        self._actions[action.id] = action

    def get_action(self, action_id: UUID) -> KnowledgeGapAction | None:
        return self._actions.get(action_id)

    def save_recheck(self, recheck: KnowledgeGapRecheck) -> None:
        self._rechecks.append(recheck)

    def rechecks_for_gap(self, gap_id: UUID) -> tuple[KnowledgeGapRecheck, ...]:
        return tuple(item for item in self._rechecks if item.gap_id == gap_id)

    def enqueue_recheck_job(self, job: KnowledgeGapRecheckJob) -> KnowledgeGapRecheckJob | None:
        with self._lock:
            if job.idempotency_key in self._recheck_jobs:
                return None
            self._recheck_jobs[job.idempotency_key] = job
            return job

    def list_recheck_jobs(self, project: ProjectRef, *, retry_failed: bool = True) -> tuple[KnowledgeGapRecheckJob, ...]:
        statuses = {"pending", "failed"} if retry_failed else {"pending"}
        return tuple(job for job in self._recheck_jobs.values() if _same_project(job.project, project) and job.status in statuses)

    def update_recheck_job(self, job: KnowledgeGapRecheckJob) -> None:
        with self._lock:
            self._recheck_jobs[job.idempotency_key] = job

    def record_digest_run(self, run):
        return self._digest_runs.setdefault(run.digest_key, run)

    def get_digest_run(self, digest_key: str):
        return self._digest_runs.get(digest_key)

    def update_digest_run(self, run):
        return self.refresh_digest_run(run)

    def refresh_digest_run(self, run):
        with self._lock:
            current = self._digest_runs.get(run.digest_key)
            if current is not None:
                run = _merge_digest_run(current, run)
            self._digest_runs[run.digest_key] = run
            return run

    def transition_digest_run(self, digest_key: str, *, expected_summary_hash: str, status: Literal["pending", "sent", "failed"], recipient_ids: tuple[str, ...], at: datetime):
        with self._lock:
            current = self._digest_runs.get(digest_key)
            if current is None:
                raise KeyError(f"digest run not found: {digest_key}")
            updated = _transition_digest(current, expected_summary_hash, status, recipient_ids, at)
            self._digest_runs[digest_key] = updated
            return updated

    def claim_digest_notification(self, notification_key: str, *, gap_id: UUID, project: ProjectRef, recipient_id: str, event_type: str, period_key: str, at: datetime) -> bool:
        with self._lock:
            if notification_key in self._digest_notifications:
                return False
            self._digest_notifications.add(notification_key)
            return True

class SQLiteKnowledgeGapStore:
    def __init__(self, database: SQLiteGapDatabase) -> None:
        self._database = database
        database.run_migrations("knowledge_gap", {1: _create_schema, 2: _create_recheck_schema, 3: _create_recheck_job_schema})

    def get(self, gap_id: UUID) -> KnowledgeGapCase | None:
        row = self._database.query_one("SELECT payload FROM knowledge_gap_cases WHERE id = ?", (str(gap_id),))
        return KnowledgeGapCase.model_validate_json(row["payload"]) if row else None

    def get_by_key(self, project: ProjectRef, canonical_key: str) -> KnowledgeGapCase | None:
        row = self._database.query_one(
            "SELECT payload FROM knowledge_gap_cases WHERE tenant_id = ? AND project_id = ? AND canonical_key = ?",
            (project.tenant_id, project.project_id, canonical_key),
        )
        return KnowledgeGapCase.model_validate_json(row["payload"]) if row else None

    def list_for_project(self, project: ProjectRef) -> tuple[KnowledgeGapCase, ...]:
        rows = self._database.query_all(
            "SELECT payload FROM knowledge_gap_cases WHERE tenant_id = ? AND project_id = ?",
            (project.tenant_id, project.project_id),
        )
        cases = [KnowledgeGapCase.model_validate_json(row["payload"]) for row in rows]
        return tuple(sorted((c for c in cases if _same_project(c.project, project)), key=lambda c: (c.created_at, str(c.id))))

    def record(self, case: KnowledgeGapCase, observation: KnowledgeGapObservation) -> None:
        with self._database.transaction() as connection:  # type: ignore[attr-defined]
            case = self._upsert_case(connection, case)
            observation = observation.model_copy(update={"gap_id": case.id})
            connection.execute(
                "INSERT INTO knowledge_gap_observations (id, gap_id, tenant_id, project_id, observed_at, question_hash, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(observation.id), str(observation.gap_id), observation.project.tenant_id, observation.project.project_id,
                 observation.observed_at.isoformat(), observation.question_hash, observation.model_dump_json()),
            )

    def save_case(self, case: KnowledgeGapCase) -> None:
        with self._database.transaction() as connection:  # type: ignore[attr-defined]
            self._upsert_case(connection, case)

    @staticmethod
    def _upsert_case(connection: sqlite3.Connection, case: KnowledgeGapCase) -> KnowledgeGapCase:
        existing = connection.execute(
            "SELECT id FROM knowledge_gap_cases WHERE tenant_id = ? AND project_id = ? AND canonical_key = ?",
            (case.project.tenant_id, case.project.project_id, case.canonical_key),
        ).fetchone()
        if existing is not None:
            case = case.model_copy(update={"id": UUID(existing["id"])})
        connection.execute(
            "INSERT INTO knowledge_gap_cases (id, tenant_id, project_id, canonical_key, payload) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(tenant_id, project_id, canonical_key) DO UPDATE SET payload = excluded.payload",
            (str(case.id), case.project.tenant_id, case.project.project_id, case.canonical_key, case.model_dump_json()),
        )
        return case

    def append_state_event(self, event: KnowledgeGapStateEvent) -> None:
        self._database.execute(
            "INSERT INTO knowledge_gap_state_events (id, gap_id, payload) VALUES (?, ?, ?)",
            (str(event.id), str(event.gap_id), event.model_dump_json()),
        )

    def observations_for_gap(self, gap_id: UUID) -> tuple[KnowledgeGapObservation, ...]:
        rows = self._database.query_all("SELECT payload FROM knowledge_gap_observations WHERE gap_id = ? ORDER BY observed_at, id", (str(gap_id),))
        return tuple(KnowledgeGapObservation.model_validate_json(row["payload"]) for row in rows)

    def events_for_gap(self, gap_id: UUID) -> tuple[KnowledgeGapStateEvent, ...]:
        rows = self._database.query_all("SELECT payload FROM knowledge_gap_state_events WHERE gap_id = ? ORDER BY sequence", (str(gap_id),))
        return tuple(KnowledgeGapStateEvent.model_validate_json(row["payload"]) for row in rows)

    def actions_for_gap(self, gap_id: UUID) -> tuple[KnowledgeGapAction, ...]:
        rows = self._database.query_all("SELECT payload FROM knowledge_gap_actions WHERE gap_id = ? ORDER BY created_at, id", (str(gap_id),))
        return tuple(KnowledgeGapAction.model_validate_json(row["payload"]) for row in rows)

    def save_action(self, action: KnowledgeGapAction) -> None:
        self._database.execute(
            "INSERT INTO knowledge_gap_actions (id, gap_id, created_at, payload) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
            (str(action.id), str(action.gap_id), action.created_at.isoformat(), action.model_dump_json()),
        )

    def get_action(self, action_id: UUID) -> KnowledgeGapAction | None:
        row = self._database.query_one("SELECT payload FROM knowledge_gap_actions WHERE id = ?", (str(action_id),))
        return KnowledgeGapAction.model_validate_json(row["payload"]) if row else None

    def save_recheck(self, recheck: KnowledgeGapRecheck) -> None:
        self._database.execute(
            "INSERT INTO knowledge_gap_rechecks (id, gap_id, created_at, payload) VALUES (?, ?, ?, ?)",
            (str(recheck.id), str(recheck.gap_id), recheck.created_at.isoformat(), recheck.model_dump_json()),
        )

    def rechecks_for_gap(self, gap_id: UUID) -> tuple[KnowledgeGapRecheck, ...]:
        rows = self._database.query_all("SELECT payload FROM knowledge_gap_rechecks WHERE gap_id = ? ORDER BY created_at", (str(gap_id),))
        return tuple(KnowledgeGapRecheck.model_validate_json(row["payload"]) for row in rows)

    def enqueue_recheck_job(self, job: KnowledgeGapRecheckJob) -> KnowledgeGapRecheckJob | None:
        cursor = self._database.execute(
            "INSERT OR IGNORE INTO knowledge_gap_recheck_jobs "
            "(idempotency_key, tenant_id, project_id, status, created_at, updated_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job.idempotency_key, job.project.tenant_id, job.project.project_id, job.status,
             job.created_at.isoformat(), job.updated_at.isoformat(), job.model_dump_json()),
        )
        return job if cursor.rowcount == 1 else None

    def list_recheck_jobs(self, project: ProjectRef, *, retry_failed: bool = True) -> tuple[KnowledgeGapRecheckJob, ...]:
        statuses = ("pending", "failed") if retry_failed else ("pending",)
        rows = self._database.query_all(
            "SELECT payload FROM knowledge_gap_recheck_jobs WHERE tenant_id = ? AND project_id = ? AND status IN (?, ?) ORDER BY created_at",
            (project.tenant_id, project.project_id, statuses[0], statuses[-1]),
        )
        return tuple(job for row in rows if _same_project(
            (job := KnowledgeGapRecheckJob.model_validate_json(row["payload"])).project, project
        ))

    def update_recheck_job(self, job: KnowledgeGapRecheckJob) -> None:
        self._database.execute(
            "UPDATE knowledge_gap_recheck_jobs SET status = ?, updated_at = ?, payload = ? WHERE idempotency_key = ?",
            (job.status, job.updated_at.isoformat(), job.model_dump_json(), job.idempotency_key),
        )

    def record_digest_run(self, run):
        self._database.execute(
            "INSERT OR IGNORE INTO knowledge_gap_digest_runs "
            "(digest_key, tenant_id, project_id, period_start, period_end, summary_hash, status, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run.digest_key, run.project.tenant_id, run.project.project_id,
             run.period_start.isoformat(), run.period_end.isoformat(), run.summary_hash,
             run.status, run.model_dump_json()),
        )
        return self.get_digest_run(run.digest_key)

    def get_digest_run(self, digest_key: str):
        from project_lens.application.knowledge_gap_digest import KnowledgeGapDigestRun

        row = self._database.query_one(
            "SELECT payload FROM knowledge_gap_digest_runs WHERE digest_key = ?", (digest_key,)
        )
        return KnowledgeGapDigestRun.model_validate_json(row["payload"]) if row else None

    def update_digest_run(self, run):
        return self.refresh_digest_run(run)

    def refresh_digest_run(self, run):
        with self._database.transaction() as connection:  # type: ignore[attr-defined]
            row = connection.execute("SELECT payload FROM knowledge_gap_digest_runs WHERE digest_key = ?", (run.digest_key,)).fetchone()
            if row is not None:
                from project_lens.application.knowledge_gap_digest import KnowledgeGapDigestRun

                current = KnowledgeGapDigestRun.model_validate_json(row["payload"])
                run = _merge_digest_run(current, run)
            connection.execute(
                "INSERT INTO knowledge_gap_digest_runs (digest_key, tenant_id, project_id, period_start, period_end, summary_hash, status, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(digest_key) DO UPDATE SET summary_hash=excluded.summary_hash, status=excluded.status, payload=excluded.payload",
                (run.digest_key, run.project.tenant_id, run.project.project_id, run.period_start.isoformat(), run.period_end.isoformat(), run.summary_hash, run.status, run.model_dump_json()),
            )
        return run

    def transition_digest_run(self, digest_key: str, *, expected_summary_hash: str, status: Literal["pending", "sent", "failed"], recipient_ids: tuple[str, ...], at: datetime):
        from project_lens.application.knowledge_gap_digest import KnowledgeGapDigestRun

        with self._database.transaction() as connection:  # type: ignore[attr-defined]
            row = connection.execute("SELECT payload FROM knowledge_gap_digest_runs WHERE digest_key = ?", (digest_key,)).fetchone()
            if row is None:
                raise KeyError(f"digest run not found: {digest_key}")
            current = KnowledgeGapDigestRun.model_validate_json(row["payload"])
            updated = _transition_digest(current, expected_summary_hash, status, recipient_ids, at)
            cursor = connection.execute(
                "UPDATE knowledge_gap_digest_runs SET status = ?, payload = ? WHERE digest_key = ? AND summary_hash = ?",
                (updated.status, updated.model_dump_json(), digest_key, expected_summary_hash),
            )
            if cursor.rowcount != 1:
                raise ValueError("digest content changed during delivery")
        return updated

    def claim_digest_notification(self, notification_key: str, *, gap_id: UUID, project: ProjectRef, recipient_id: str, event_type: str, period_key: str, at: datetime) -> bool:
        cursor = self._database.execute(
            "INSERT OR IGNORE INTO knowledge_gap_notifications "
            "(notification_key, gap_id, tenant_id, project_id, recipient_id, event_type, period_key, claimed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (notification_key, str(gap_id), project.tenant_id, project.project_id, recipient_id, event_type, period_key, at.isoformat()),
        )
        return cursor.rowcount == 1

def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE knowledge_gap_cases (
            id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            canonical_key TEXT NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY (tenant_id, project_id, canonical_key)
        );
        CREATE UNIQUE INDEX idx_knowledge_gap_case_id ON knowledge_gap_cases (id);
        CREATE INDEX idx_knowledge_gap_cases_project ON knowledge_gap_cases (tenant_id, project_id);
        CREATE TABLE knowledge_gap_observations (
            id TEXT PRIMARY KEY,
            gap_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            question_hash TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE INDEX idx_knowledge_gap_observations_gap ON knowledge_gap_observations (gap_id, observed_at);
        CREATE TABLE knowledge_gap_state_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            gap_id TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE TABLE knowledge_gap_actions (
            id TEXT PRIMARY KEY,
            gap_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE INDEX idx_knowledge_gap_actions_gap ON knowledge_gap_actions (gap_id, created_at);
        CREATE TABLE knowledge_gap_digest_runs (
            digest_key TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            summary_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE INDEX idx_knowledge_gap_digest_runs_project
            ON knowledge_gap_digest_runs (tenant_id, project_id, period_start);
        CREATE TABLE knowledge_gap_notifications (
            notification_key TEXT PRIMARY KEY,
            gap_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            recipient_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            period_key TEXT NOT NULL,
            claimed_at TEXT NOT NULL
        );
        CREATE INDEX idx_knowledge_gap_notifications_project
            ON knowledge_gap_notifications (tenant_id, project_id, period_key);
    """)


def _create_recheck_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE knowledge_gap_rechecks (
            id TEXT PRIMARY KEY,
            gap_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            payload TEXT NOT NULL
        )
    """)
    connection.execute("CREATE INDEX idx_knowledge_gap_rechecks_gap ON knowledge_gap_rechecks (gap_id, created_at)")


def _create_recheck_job_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE knowledge_gap_recheck_jobs (
            idempotency_key TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            payload TEXT NOT NULL
        )
    """)
    connection.execute("CREATE INDEX idx_knowledge_gap_recheck_jobs_project ON knowledge_gap_recheck_jobs (tenant_id, project_id, status, created_at)")


def _same_project(left: ProjectRef, right: ProjectRef) -> bool:
    return (
        left.tenant_id == right.tenant_id and left.project_id == right.project_id
        and (right.service is None or left.service == right.service)
        and (right.environment is None or left.environment == right.environment)
    )


def _merge_digest_run(current, incoming):
    if current.summary_hash == incoming.summary_hash:
        return incoming.model_copy(update={"id": current.id, "status": current.status, "audience_scope": current.audience_scope, "recipient_ids": current.recipient_ids, "sent_at": current.sent_at})
    return incoming.model_copy(update={"id": current.id})


def _transition_digest(current, expected_summary_hash, status, recipient_ids, at):
    if current.summary_hash != expected_summary_hash:
        raise ValueError("digest content changed during delivery")
    return current.model_copy(update={"status": status, "recipient_ids": tuple(sorted(set(recipient_ids))), "sent_at": at if status == "sent" else current.sent_at})
