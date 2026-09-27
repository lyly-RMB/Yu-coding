"""ConversationSession stores: in-memory and SQLite-backed."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from uuid import UUID

from pydantic import ValidationError

from project_lens.domain.conversation import ConversationSession
from project_lens.domain.models import ProjectRef
from project_lens.persistence.sqlite import SQLiteDatabase

logger = logging.getLogger(__name__)

_BINDING_KEY = tuple[str, str, str, str, str]


class ConversationStore:
    def get(self, session_id: UUID) -> ConversationSession | None:
        raise NotImplementedError

    def get_by_binding(
        self,
        *,
        tenant_id: str,
        chat_id: str,
        user_id: str,
        project: ProjectRef,
    ) -> ConversationSession | None:
        raise NotImplementedError

    def list_by_binding(
        self,
        *,
        tenant_id: str,
        chat_id: str,
        user_id: str,
        project: ProjectRef,
    ) -> tuple[ConversationSession, ...]:
        raise NotImplementedError

    def set_active_session(self, session: ConversationSession) -> None:
        raise NotImplementedError

    def upsert(self, session: ConversationSession) -> ConversationSession:
        raise NotImplementedError

    def delete(self, session: ConversationSession) -> None:
        """Remove a session row; clear binding if it was the active line."""

        raise NotImplementedError


def _binding_key(
    *,
    tenant_id: str,
    chat_id: str,
    user_id: str,
    project: ProjectRef,
) -> _BINDING_KEY:
    return (
        tenant_id,
        chat_id,
        user_id,
        project.tenant_id,
        project.project_id,
    )


def _session_binding_key(session: ConversationSession) -> _BINDING_KEY:
    return _binding_key(
        tenant_id=session.tenant_id,
        chat_id=session.chat_id,
        user_id=session.user_id,
        project=session.project,
    )


class InMemoryConversationStore(ConversationStore):
    def __init__(self) -> None:
        self._by_id: dict[UUID, ConversationSession] = {}
        self._active_by_binding: dict[_BINDING_KEY, UUID] = {}

    def get(self, session_id: UUID) -> ConversationSession | None:
        session = self._by_id.get(session_id)
        if session is None:
            return None
        if session.expires_at < datetime.now(timezone.utc):
            self._drop(session)
            return None
        return session

    def get_by_binding(
        self,
        *,
        tenant_id: str,
        chat_id: str,
        user_id: str,
        project: ProjectRef,
    ) -> ConversationSession | None:
        key = _binding_key(
            tenant_id=tenant_id,
            chat_id=chat_id,
            user_id=user_id,
            project=project,
        )
        session_id = self._active_by_binding.get(key)
        if session_id is None:
            return None
        session = self.get(session_id)
        if session is None:
            self._active_by_binding.pop(key, None)
            return None
        return session

    def list_by_binding(
        self,
        *,
        tenant_id: str,
        chat_id: str,
        user_id: str,
        project: ProjectRef,
    ) -> tuple[ConversationSession, ...]:
        key = _binding_key(
            tenant_id=tenant_id,
            chat_id=chat_id,
            user_id=user_id,
            project=project,
        )
        rows: list[ConversationSession] = []
        for session in list(self._by_id.values()):
            if _session_binding_key(session) != key:
                continue
            loaded = self.get(session.session_id)
            if loaded is not None:
                rows.append(loaded)
        rows.sort(key=lambda item: item.branch_name)
        return tuple(rows)

    def set_active_session(self, session: ConversationSession) -> None:
        if self.get(session.session_id) is None:
            self._by_id[session.session_id] = session
        self._active_by_binding[_session_binding_key(session)] = session.session_id

    def upsert(self, session: ConversationSession) -> ConversationSession:
        key = _session_binding_key(session)
        self._by_id[session.session_id] = session
        if key not in self._active_by_binding:
            self._active_by_binding[key] = session.session_id
        return session

    def delete(self, session: ConversationSession) -> None:
        self._by_id.pop(session.session_id, None)
        key = _session_binding_key(session)
        current = self._active_by_binding.get(key)
        if current == session.session_id:
            self._active_by_binding.pop(key, None)

    def _drop(self, session: ConversationSession) -> None:
        self.delete(session)


class SQLiteConversationStore(ConversationStore):
    """Durable ConversationSession store. Runtime context only — never ProjectMemory."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._database.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_sessions (
                session_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                project_tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        self._database.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_bindings (
                tenant_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                project_tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                active_session_id TEXT NOT NULL,
                PRIMARY KEY (
                    tenant_id, chat_id, user_id, project_tenant_id, project_id
                )
            )
            """
        )
        self._database.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_conversation_sessions_binding
            ON conversation_sessions (
                tenant_id, chat_id, user_id, project_tenant_id, project_id
            )
            """
        )
        self._database.run_migrations(
            "conversation_sessions",
            {1: _migrate_conversation_sessions_v1},
        )

    def get(self, session_id: UUID) -> ConversationSession | None:
        row = self._database.query_one(
            "SELECT payload FROM conversation_sessions WHERE session_id = ?",
            (str(session_id),),
        )
        if row is None:
            return None
        return self._hydrate_or_drop(
            row["payload"],
            drop_session_id=str(session_id),
        )

    def get_by_binding(
        self,
        *,
        tenant_id: str,
        chat_id: str,
        user_id: str,
        project: ProjectRef,
    ) -> ConversationSession | None:
        row = self._database.query_one(
            """
            SELECT active_session_id FROM conversation_bindings
            WHERE tenant_id = ?
              AND chat_id = ?
              AND user_id = ?
              AND project_tenant_id = ?
              AND project_id = ?
            """,
            (
                tenant_id,
                chat_id,
                user_id,
                project.tenant_id,
                project.project_id,
            ),
        )
        if row is None:
            # Legacy rows written before bindings table: fall back to any session.
            return self._legacy_binding_fallback(
                tenant_id=tenant_id,
                chat_id=chat_id,
                user_id=user_id,
                project=project,
            )
        try:
            active_id = UUID(str(row["active_session_id"]))
        except ValueError:
            return None
        session = self.get(active_id)
        if session is None:
            self._database.execute(
                """
                DELETE FROM conversation_bindings
                WHERE tenant_id = ?
                  AND chat_id = ?
                  AND user_id = ?
                  AND project_tenant_id = ?
                  AND project_id = ?
                """,
                (
                    tenant_id,
                    chat_id,
                    user_id,
                    project.tenant_id,
                    project.project_id,
                ),
            )
            return None
        return session

    def list_by_binding(
        self,
        *,
        tenant_id: str,
        chat_id: str,
        user_id: str,
        project: ProjectRef,
    ) -> tuple[ConversationSession, ...]:
        rows = self._database.query_all(
            """
            SELECT session_id, payload FROM conversation_sessions
            WHERE tenant_id = ?
              AND chat_id = ?
              AND user_id = ?
              AND project_tenant_id = ?
              AND project_id = ?
            """,
            (
                tenant_id,
                chat_id,
                user_id,
                project.tenant_id,
                project.project_id,
            ),
        )
        sessions: list[ConversationSession] = []
        for row in rows:
            session = self._hydrate_or_drop(
                row["payload"],
                drop_session_id=str(row["session_id"]),
            )
            if session is not None:
                sessions.append(session)
        sessions.sort(key=lambda item: item.branch_name)
        return tuple(sessions)

    def set_active_session(self, session: ConversationSession) -> None:
        self._database.execute(
            """
            INSERT INTO conversation_bindings (
                tenant_id,
                chat_id,
                user_id,
                project_tenant_id,
                project_id,
                active_session_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, chat_id, user_id, project_tenant_id, project_id)
            DO UPDATE SET active_session_id = excluded.active_session_id
            """,
            (
                session.tenant_id,
                session.chat_id,
                session.user_id,
                session.project.tenant_id,
                session.project.project_id,
                str(session.session_id),
            ),
        )

    def upsert(self, session: ConversationSession) -> ConversationSession:
        payload = session.model_dump_json()
        self._database.execute(
            """
            INSERT INTO conversation_sessions (
                session_id,
                tenant_id,
                chat_id,
                user_id,
                project_tenant_id,
                project_id,
                expires_at,
                payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                tenant_id = excluded.tenant_id,
                chat_id = excluded.chat_id,
                user_id = excluded.user_id,
                project_tenant_id = excluded.project_tenant_id,
                project_id = excluded.project_id,
                expires_at = excluded.expires_at,
                payload = excluded.payload
            """,
            (
                str(session.session_id),
                session.tenant_id,
                session.chat_id,
                session.user_id,
                session.project.tenant_id,
                session.project.project_id,
                session.expires_at.isoformat(),
                payload,
            ),
        )
        existing = self._database.query_one(
            """
            SELECT active_session_id FROM conversation_bindings
            WHERE tenant_id = ?
              AND chat_id = ?
              AND user_id = ?
              AND project_tenant_id = ?
              AND project_id = ?
            """,
            (
                session.tenant_id,
                session.chat_id,
                session.user_id,
                session.project.tenant_id,
                session.project.project_id,
            ),
        )
        if existing is None:
            self.set_active_session(session)
        return session

    def _legacy_binding_fallback(
        self,
        *,
        tenant_id: str,
        chat_id: str,
        user_id: str,
        project: ProjectRef,
    ) -> ConversationSession | None:
        siblings = self.list_by_binding(
            tenant_id=tenant_id,
            chat_id=chat_id,
            user_id=user_id,
            project=project,
        )
        if not siblings:
            return None
        # Prefer a non-stopped line; otherwise the first sibling.
        chosen = next((item for item in siblings if not item.write_stopped), siblings[0])
        self.set_active_session(chosen)
        return chosen

    def _hydrate_or_drop(
        self,
        payload: str,
        *,
        drop_session_id: str | None = None,
    ) -> ConversationSession | None:
        try:
            session = ConversationSession.model_validate_json(payload)
        except (ValidationError, json.JSONDecodeError, ValueError, TypeError) as exc:
            # Corrupt rows are dropped so Feishu can recreate a clean session.
            logger.warning(
                "dropping corrupt conversation session payload: %s",
                drop_session_id or "unknown",
                exc_info=exc,
            )
            if drop_session_id:
                self._database.execute(
                    "DELETE FROM conversation_sessions WHERE session_id = ?",
                    (drop_session_id,),
                )
            return None
        if session.expires_at < datetime.now(timezone.utc):
            self._drop(session)
            return None
        return session

    def delete(self, session: ConversationSession) -> None:
        self._database.execute(
            "DELETE FROM conversation_sessions WHERE session_id = ?",
            (str(session.session_id),),
        )
        row = self._database.query_one(
            """
            SELECT active_session_id FROM conversation_bindings
            WHERE tenant_id = ?
              AND chat_id = ?
              AND user_id = ?
              AND project_tenant_id = ?
              AND project_id = ?
            """,
            (
                session.tenant_id,
                session.chat_id,
                session.user_id,
                session.project.tenant_id,
                session.project.project_id,
            ),
        )
        if row is not None and str(row["active_session_id"]) == str(session.session_id):
            self._database.execute(
                """
                DELETE FROM conversation_bindings
                WHERE tenant_id = ?
                  AND chat_id = ?
                  AND user_id = ?
                  AND project_tenant_id = ?
                  AND project_id = ?
                """,
                (
                    session.tenant_id,
                    session.chat_id,
                    session.user_id,
                    session.project.tenant_id,
                    session.project.project_id,
                ),
            )

    def _drop(self, session: ConversationSession) -> None:
        self.delete(session)


def _migrate_conversation_sessions_v1(connection: sqlite3.Connection) -> None:
    """Drop per-binding UNIQUE so sibling forks can coexist; backfill bindings."""

    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='conversation_sessions'"
    ).fetchone()
    create_sql = str(row[0]) if row and row[0] else ""
    if "UNIQUE" in create_sql.upper():
        connection.execute(
            """
            CREATE TABLE conversation_sessions_v2 (
                session_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                project_tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO conversation_sessions_v2 (
                session_id, tenant_id, chat_id, user_id,
                project_tenant_id, project_id, expires_at, payload
            )
            SELECT
                session_id, tenant_id, chat_id, user_id,
                project_tenant_id, project_id, expires_at, payload
            FROM conversation_sessions
            """
        )
        connection.execute("DROP TABLE conversation_sessions")
        connection.execute(
            "ALTER TABLE conversation_sessions_v2 RENAME TO conversation_sessions"
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_conversation_sessions_binding
            ON conversation_sessions (
                tenant_id, chat_id, user_id, project_tenant_id, project_id
            )
            """
        )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_bindings (
            tenant_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            project_tenant_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            active_session_id TEXT NOT NULL,
            PRIMARY KEY (
                tenant_id, chat_id, user_id, project_tenant_id, project_id
            )
        )
        """
    )

    missing = connection.execute(
        """
        SELECT s.tenant_id, s.chat_id, s.user_id, s.project_tenant_id, s.project_id,
               s.session_id
        FROM conversation_sessions s
        LEFT JOIN conversation_bindings b
          ON b.tenant_id = s.tenant_id
         AND b.chat_id = s.chat_id
         AND b.user_id = s.user_id
         AND b.project_tenant_id = s.project_tenant_id
         AND b.project_id = s.project_id
        WHERE b.active_session_id IS NULL
        """
    ).fetchall()
    seen: set[tuple[str, str, str, str, str]] = set()
    for item in missing:
        key = (
            str(item["tenant_id"]),
            str(item["chat_id"]),
            str(item["user_id"]),
            str(item["project_tenant_id"]),
            str(item["project_id"]),
        )
        if key in seen:
            continue
        seen.add(key)
        connection.execute(
            """
            INSERT OR IGNORE INTO conversation_bindings (
                tenant_id, chat_id, user_id, project_tenant_id, project_id,
                active_session_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (*key, str(item["session_id"])),
        )
