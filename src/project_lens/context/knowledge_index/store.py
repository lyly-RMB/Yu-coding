"""SQLite persistence for knowledge documents, chunks, embeddings, and jobs."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from uuid import UUID
from dataclasses import dataclass

from project_lens.context.knowledge_index.models import (
    EmbeddingRecord,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIndexJob,
    KnowledgeObjectKind,
)
from project_lens.persistence.sqlite import SQLiteDatabase


@dataclass(frozen=True)
class KnowledgeIndexStats:
    documents: int
    active_documents: int
    chunks: int
    active_chunks: int
    embeddings: int
    ready_embeddings: int
    pending_jobs: int
    failed_jobs: int


class SQLiteKnowledgeIndexStore:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database
        self._database.run_migrations(
            "knowledge_index",
            {1: _migration_v1, 2: _migration_v2, 3: _migration_v3},
        )

    def upsert_documents(self, documents: Iterable[KnowledgeDocument]) -> int:
        count = 0
        with self._database.transaction() as connection:
            for document in documents:
                connection.execute(
                    """
                    INSERT INTO knowledge_documents (
                        id, tenant_id, project_id, kind, object_id, revision,
                        content_hash, indexable, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        content_hash=excluded.content_hash,
                        indexable=excluded.indexable,
                        payload=excluded.payload
                    """,
                    (
                        document.id,
                        document.tenant_id,
                        document.project_id,
                        document.kind.value,
                        document.object_id,
                        document.revision,
                        document.content_hash,
                        int(document.indexable),
                        document.model_dump_json(),
                    ),
                )
                count += 1
        return count

    def get_document(self, document_id: str) -> KnowledgeDocument | None:
        row = self._database.query_one(
            "SELECT payload FROM knowledge_documents WHERE id = ?", (document_id,)
        )
        return KnowledgeDocument.model_validate_json(row["payload"]) if row else None

    def stats(self, *, tenant_id: str | None = None, project_id: str | None = None) -> KnowledgeIndexStats:
        scope = ""
        params: list[object] = []
        if tenant_id is not None:
            scope += " AND tenant_id = ?"
            params.append(tenant_id)
        if project_id is not None:
            scope += " AND project_id = ?"
            params.append(project_id)
        document_row = self._database.query_one(
            f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN indexable = 1 THEN 1 ELSE 0 END) AS active
            FROM knowledge_documents WHERE 1 = 1 {scope}
            """,
            tuple(params),
        )
        chunk_row = self._database.query_one(
            f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN indexable = 1 THEN 1 ELSE 0 END) AS active
            FROM knowledge_chunks WHERE 1 = 1 {scope}
            """,
            tuple(params),
        )
        embedding_row = self._database.query_one(
            f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status = 'ready' THEN 1 ELSE 0 END) AS ready
            FROM knowledge_embeddings WHERE 1 = 1 {scope}
            """,
            tuple(params),
        )
        job_row = self._database.query_one(
            f"""
            SELECT
              SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
              SUM(CASE WHEN status IN ('failed', 'dead_letter') THEN 1 ELSE 0 END) AS failed
            FROM knowledge_index_jobs WHERE 1 = 1 {scope}
            """,
            tuple(params),
        )
        return KnowledgeIndexStats(
            documents=int(document_row["total"] or 0),
            active_documents=int(document_row["active"] or 0),
            chunks=int(chunk_row["total"] or 0),
            active_chunks=int(chunk_row["active"] or 0),
            embeddings=int(embedding_row["total"] or 0),
            ready_embeddings=int(embedding_row["ready"] or 0),
            pending_jobs=int(job_row["pending"] or 0),
            failed_jobs=int(job_row["failed"] or 0),
        )

    def list_documents(
        self,
        *,
        tenant_id: str,
        project_id: str,
        kind: KnowledgeObjectKind | str | None = None,
        indexable_only: bool = False,
        limit: int = 200,
    ) -> tuple[KnowledgeDocument, ...]:
        clauses = ["tenant_id = ?", "project_id = ?"]
        params: list[object] = [tenant_id, project_id]
        if kind is not None:
            clauses.append("kind = ?")
            params.append(str(kind))
        if indexable_only:
            clauses.append("indexable = 1")
        params.append(max(1, min(int(limit), 1_000)))
        rows = self._database.query_all(
            f"SELECT payload FROM knowledge_documents WHERE {' AND '.join(clauses)} "
            "ORDER BY rowid DESC LIMIT ?",
            tuple(params),
        )
        return tuple(KnowledgeDocument.model_validate_json(row["payload"]) for row in rows)

    def upsert_chunks(self, chunks: Iterable[KnowledgeChunk]) -> int:
        count = 0
        with self._database.transaction() as connection:
            for chunk in chunks:
                connection.execute(
                    """
                    INSERT INTO knowledge_chunks (
                        chunk_id, document_id, tenant_id, project_id, object_kind,
                        object_id, revision, content_hash, chunker_version,
                        indexable, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chunk_id) DO UPDATE SET
                        content_hash=excluded.content_hash,
                        chunker_version=excluded.chunker_version,
                        indexable=excluded.indexable,
                        payload=excluded.payload
                    """,
                    (
                        chunk.chunk_id,
                        chunk.document_id,
                        chunk.tenant_id,
                        chunk.project_id,
                        chunk.object_kind.value,
                        chunk.object_id,
                        chunk.revision,
                        chunk.content_hash,
                        chunk.chunker_version,
                        int(chunk.indexable),
                        chunk.model_dump_json(),
                    ),
                )
                count += 1
        return count

    def replace_document_chunks(
        self,
        document_id: str,
        chunks: Iterable[KnowledgeChunk],
    ) -> int:
        """Deactivate old revisions for a document, then install current chunks."""

        current = tuple(chunks)
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT payload FROM knowledge_chunks WHERE document_id = ?",
                (document_id,),
            ).fetchall()
            for row in rows:
                old = KnowledgeChunk.model_validate_json(row["payload"])
                if old.indexable or old.status == "active":
                    retired = old.model_copy(
                        update={"indexable": False, "status": "superseded"}
                    )
                    connection.execute(
                        """
                        UPDATE knowledge_chunks
                        SET indexable = ?, content_hash = ?, chunker_version = ?,
                            payload = ?
                        WHERE chunk_id = ?
                        """,
                        (
                            0,
                            retired.content_hash,
                            retired.chunker_version,
                            retired.model_dump_json(),
                            retired.chunk_id,
                        ),
                    )
            for chunk in current:
                connection.execute(
                    """
                    INSERT INTO knowledge_chunks (
                        chunk_id, document_id, tenant_id, project_id, object_kind,
                        object_id, revision, content_hash, chunker_version,
                        indexable, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chunk_id) DO UPDATE SET
                        content_hash=excluded.content_hash,
                        chunker_version=excluded.chunker_version,
                        indexable=excluded.indexable,
                        payload=excluded.payload
                    """,
                    (
                        chunk.chunk_id,
                        chunk.document_id,
                        chunk.tenant_id,
                        chunk.project_id,
                        chunk.object_kind.value,
                        chunk.object_id,
                        chunk.revision,
                        chunk.content_hash,
                        chunk.chunker_version,
                        int(chunk.indexable),
                        chunk.model_dump_json(),
                    ),
                )
        return len(current)

    def deactivate_document_chunks(self, document_id: str) -> int:
        """Mark all chunks for a superseded document as non-indexable."""

        changed = 0
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT payload FROM knowledge_chunks WHERE document_id = ?",
                (document_id,),
            ).fetchall()
            for row in rows:
                old = KnowledgeChunk.model_validate_json(row["payload"])
                if old.indexable or old.status == "active":
                    retired = old.model_copy(
                        update={"indexable": False, "status": "superseded"}
                    )
                    connection.execute(
                        """
                        UPDATE knowledge_chunks
                        SET indexable = ?, payload = ?
                        WHERE chunk_id = ?
                        """,
                        (0, retired.model_dump_json(), retired.chunk_id),
                    )
                    changed += 1
        return changed

    def deactivate_document(self, document_id: str) -> int:
        document = self.get_document(document_id)
        if document is None or not document.indexable:
            return 0
        retired = document.model_copy(update={"indexable": False, "status": "superseded"})
        self._database.execute(
            """
            UPDATE knowledge_documents
            SET indexable = ?, payload = ?
            WHERE id = ?
            """,
            (0, retired.model_dump_json(), document_id),
        )
        self.deactivate_document_chunks(document_id)
        return 1

    def list_chunks(
        self,
        *,
        tenant_id: str,
        project_id: str,
        indexable_only: bool = False,
        limit: int = 1_000,
    ) -> tuple[KnowledgeChunk, ...]:
        condition = "AND indexable = 1" if indexable_only else ""
        rows = self._database.query_all(
            f"SELECT payload FROM knowledge_chunks WHERE tenant_id = ? AND project_id = ? "
            f"{condition} ORDER BY rowid DESC LIMIT ?",
            (tenant_id, project_id, max(1, min(int(limit), 10_000))),
        )
        return tuple(KnowledgeChunk.model_validate_json(row["payload"]) for row in rows)

    def put_embedding(self, record: EmbeddingRecord) -> None:
        self._database.execute(
            """
            INSERT INTO knowledge_embeddings (
                chunk_id, tenant_id, project_id, model_version,
                dimensions, content_hash, chunker_version, index_version, status, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                tenant_id, project_id, chunk_id, model_version,
                content_hash, chunker_version, index_version
            ) DO UPDATE SET
                dimensions=excluded.dimensions,
                status=excluded.status,
                payload=excluded.payload
            """,
            (
                record.chunk_id,
                record.tenant_id,
                record.project_id,
                record.model_version,
                record.dimensions,
                record.content_hash,
                record.chunker_version,
                record.index_version,
                record.status,
                record.model_dump_json(),
            ),
        )

    def get_embedding(
        self,
        *,
        tenant_id: str,
        project_id: str,
        chunk_id: str,
        model_version: str,
        content_hash: str,
        chunker_version: str,
        index_version: str = "v1",
    ) -> EmbeddingRecord | None:
        row = self._database.query_one(
            """
            SELECT payload FROM knowledge_embeddings
            WHERE tenant_id = ? AND project_id = ? AND chunk_id = ?
              AND model_version = ? AND content_hash = ? AND chunker_version = ?
              AND index_version = ?
            """,
            (tenant_id, project_id, chunk_id, model_version, content_hash, chunker_version, index_version),
        )
        return EmbeddingRecord.model_validate_json(row["payload"]) if row else None

    def list_chunks_with_embeddings(
        self,
        *,
        tenant_id: str,
        project_id: str,
        model_version: str,
        chunker_version: str | None = None,
        index_version: str = "v1",
        indexable_only: bool = True,
        limit: int = 10_000,
    ) -> tuple[tuple[KnowledgeChunk, EmbeddingRecord], ...]:
        """Return chunks with a ready vector matching the current chunk content."""

        clauses = [
            "c.tenant_id = ?",
            "c.project_id = ?",
            "e.tenant_id = c.tenant_id",
            "e.project_id = c.project_id",
            "e.chunk_id = c.chunk_id",
            "e.model_version = ?",
            "e.index_version = ?",
            "e.chunker_version = c.chunker_version",
            "e.content_hash = c.content_hash",
            "e.status = 'ready'",
        ]
        params: list[object] = [tenant_id, project_id, model_version, index_version]
        if chunker_version is not None:
            clauses.append("c.chunker_version = ?")
            params.append(chunker_version)
        if indexable_only:
            clauses.append("c.indexable = 1")
        params.append(max(1, min(int(limit), 50_000)))
        rows = self._database.query_all(
            f"""
            SELECT c.payload AS chunk_payload, e.payload AS embedding_payload
            FROM knowledge_chunks AS c
            INNER JOIN knowledge_embeddings AS e
                ON e.chunk_id = c.chunk_id
            WHERE {' AND '.join(clauses)}
            ORDER BY c.rowid DESC
            LIMIT ?
            """,
            tuple(params),
        )
        return tuple(
            (
                KnowledgeChunk.model_validate_json(row["chunk_payload"]),
                EmbeddingRecord.model_validate_json(row["embedding_payload"]),
            )
            for row in rows
        )

    def enqueue_job(self, job: KnowledgeIndexJob) -> KnowledgeIndexJob:
        self._database.execute(
            """
            INSERT INTO knowledge_index_jobs (
                id, tenant_id, project_id, object_kind, object_id,
                content_hash, model_version, chunker_version, index_version, status,
                attempt_count, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                tenant_id, project_id, object_kind, object_id,
                content_hash, model_version, chunker_version, index_version
            ) DO UPDATE SET
                status=CASE WHEN knowledge_index_jobs.status = 'completed'
                    THEN knowledge_index_jobs.status ELSE excluded.status END,
                payload=CASE WHEN knowledge_index_jobs.status = 'completed'
                    THEN knowledge_index_jobs.payload ELSE excluded.payload END
            """,
            (
                str(job.id),
                job.tenant_id,
                job.project_id,
                job.object_kind.value,
                job.object_id,
                job.content_hash,
                job.model_version,
                job.chunker_version,
                job.index_version,
                job.status,
                job.attempt_count,
                job.model_dump_json(),
            ),
        )
        row = self._database.query_one(
            """
            SELECT payload FROM knowledge_index_jobs
            WHERE tenant_id = ? AND project_id = ? AND object_kind = ?
                AND object_id = ? AND content_hash = ? AND model_version = ?
              AND chunker_version = ? AND index_version = ?
            """,
            (
                job.tenant_id,
                job.project_id,
                job.object_kind.value,
                job.object_id,
                job.content_hash,
                job.model_version,
                job.chunker_version,
                job.index_version,
            ),
        )
        return KnowledgeIndexJob.model_validate_json(row["payload"]) if row else job

    def list_jobs(self, *, tenant_id: str, project_id: str, status: str | None = None) -> tuple[KnowledgeIndexJob, ...]:
        clauses = ["tenant_id = ?", "project_id = ?"]
        params: list[object] = [tenant_id, project_id]
        if status:
            clauses.append("status = ?")
            params.append(status)
        rows = self._database.query_all(
            f"SELECT payload FROM knowledge_index_jobs WHERE {' AND '.join(clauses)} ORDER BY rowid",
            tuple(params),
        )
        return tuple(KnowledgeIndexJob.model_validate_json(row["payload"]) for row in rows)


def _migration_v1(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS knowledge_documents (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            object_id TEXT NOT NULL,
            revision TEXT,
            content_hash TEXT NOT NULL,
            indexable INTEGER NOT NULL,
            payload TEXT NOT NULL,
            UNIQUE (tenant_id, project_id, kind, object_id, revision)
        );
        CREATE INDEX IF NOT EXISTS idx_knowledge_documents_scope
            ON knowledge_documents (tenant_id, project_id, indexable);
        CREATE TABLE IF NOT EXISTS knowledge_chunks (
            chunk_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            object_kind TEXT NOT NULL,
            object_id TEXT NOT NULL,
            revision TEXT,
            content_hash TEXT NOT NULL,
            chunker_version TEXT NOT NULL,
            indexable INTEGER NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_scope
            ON knowledge_chunks (tenant_id, project_id, indexable);
        CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_document
            ON knowledge_chunks (document_id);
        CREATE TABLE IF NOT EXISTS knowledge_embeddings (
            chunk_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            model_version TEXT NOT NULL,
            dimensions INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            chunker_version TEXT NOT NULL,
            index_version TEXT NOT NULL DEFAULT 'v1',
            status TEXT NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY (
                tenant_id, project_id, chunk_id,
                model_version, content_hash, chunker_version
                , index_version
            )
        );
        CREATE TABLE IF NOT EXISTS knowledge_index_jobs (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            object_kind TEXT NOT NULL,
            object_id TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            model_version TEXT NOT NULL,
            chunker_version TEXT NOT NULL,
            index_version TEXT NOT NULL DEFAULT 'v1',
            status TEXT NOT NULL,
            attempt_count INTEGER NOT NULL,
            payload TEXT NOT NULL,
            UNIQUE (
                tenant_id, project_id, object_kind, object_id,
                content_hash, model_version, chunker_version
                , index_version
            )
        );
        CREATE INDEX IF NOT EXISTS idx_knowledge_jobs_status
            ON knowledge_index_jobs (tenant_id, project_id, status);
        """
    )


def _migration_v2(connection: sqlite3.Connection) -> None:
    for table in ("knowledge_embeddings", "knowledge_index_jobs"):
        columns = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if "index_version" not in columns:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN index_version TEXT NOT NULL DEFAULT 'v1'"
            )


def _migration_v3(connection: sqlite3.Connection) -> None:
    """Add version-aware conflict targets to databases created before v2."""

    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_embeddings_versioned
        ON knowledge_embeddings (
            tenant_id, project_id, chunk_id, model_version,
            content_hash, chunker_version, index_version
        )
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_jobs_versioned
        ON knowledge_index_jobs (
            tenant_id, project_id, object_kind, object_id,
            content_hash, model_version, chunker_version, index_version
        )
        """
    )


__all__ = ["KnowledgeIndexStats", "SQLiteKnowledgeIndexStore"]
