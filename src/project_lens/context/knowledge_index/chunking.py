"""Stable chunk construction and provenance projection."""

from __future__ import annotations

import re
from collections.abc import Iterable

from project_lens.context.chunking import split_text
from project_lens.context.knowledge_index.models import (
    IndexRecordStatus,
    KnowledgeChunk,
    KnowledgeDocument,
    content_hash,
    stable_chunk_id,
)

_NON_INDEXABLE_STATUSES = {
    IndexRecordStatus.PENDING.value,
    IndexRecordStatus.REJECTED.value,
    IndexRecordStatus.REVOKED.value,
    IndexRecordStatus.EXPIRED.value,
}


class KnowledgeChunkBuilder:
    """Build deterministic chunks without making authorization decisions."""

    def __init__(
        self,
        *,
        chunker_version: str = "v1",
        max_chars: int = 1_200,
        overlap_chars: int = 150,
    ) -> None:
        self.chunker_version = chunker_version
        self.max_chars = max(100, int(max_chars))
        self.overlap_chars = max(0, min(int(overlap_chars), self.max_chars // 2))

    def build(self, document: KnowledgeDocument) -> tuple[KnowledgeChunk, ...]:
        if not document.indexable or document.status.casefold() in _NON_INDEXABLE_STATUSES:
            return ()
        pieces = _split_document_text(document.text, max_chars=self.max_chars, overlap_chars=self.overlap_chars)
        if not pieces:
            return ()
        total = len(pieces)
        return tuple(
            self._make_chunk(document, text, index, total, title_path)
            for index, (text, title_path) in enumerate(pieces)
        )

    def build_many(self, documents: Iterable[KnowledgeDocument]) -> tuple[KnowledgeChunk, ...]:
        chunks: list[KnowledgeChunk] = []
        for document in documents:
            chunks.extend(self.build(document))
        return tuple(chunks)

    def _make_chunk(
        self,
        document: KnowledgeDocument,
        text: str,
        index: int,
        total: int,
        title_path: tuple[str, ...],
    ) -> KnowledgeChunk:
        digest = content_hash(text)
        metadata = dict(document.metadata)
        metadata.update(
            {
                "document_id": document.id,
                "source_system": document.source_system,
                "source_id": document.source_id,
                "source_record_key": document.source_record_key,
                "evidence_ids": list(document.evidence_ids),
                "fact_key": document.fact_key,
                "subject": document.subject,
                "revision": document.revision,
                "valid_from": document.valid_from.isoformat() if document.valid_from else None,
                "valid_to": document.valid_to.isoformat() if document.valid_to else None,
                "derived": document.kind.value == "obsidian_wiki",
                "historical": document.kind.value in {"episode", "memory_observation"},
            }
        )
        return KnowledgeChunk(
            chunk_id=stable_chunk_id(
                tenant_id=document.tenant_id,
                project_id=document.project_id,
                kind=document.kind,
                object_id=document.object_id,
                revision=document.revision,
                chunker_version=self.chunker_version,
                chunk_index=index,
                chunk_content_hash=digest,
            ),
            document_id=document.id,
            object_kind=document.kind,
            object_id=document.object_id,
            tenant_id=document.tenant_id,
            project_id=document.project_id,
            source_id=document.source_id,
            revision=document.revision,
            title_path=title_path,
            text=text,
            content_hash=digest,
            chunker_version=self.chunker_version,
            chunk_index=index,
            chunk_count=total,
            status=document.status,
            access_scope=document.access_scope,
            visibility_scope=document.visibility_scope,
            indexable=document.indexable,
            metadata=metadata,
        )


def _split_document_text(
    text: str,
    *,
    max_chars: int,
    overlap_chars: int,
) -> list[tuple[str, tuple[str, ...]]]:
    """Split text while carrying the nearest Markdown heading path."""

    if not text.strip():
        return []
    headings: list[tuple[int, tuple[str, ...]]] = []
    stack: list[str] = []
    for line_number, line in enumerate(text.splitlines()):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if not match:
            continue
        depth = len(match.group(1))
        stack[:] = stack[: depth - 1]
        stack.append(match.group(2))
        headings.append((line_number, tuple(stack)))

    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    plain_chunks = split_text(text, max_chars=max_chars, overlap_chars=overlap_chars)
    if not headings:
        return [(chunk, ()) for chunk in plain_chunks]

    results: list[tuple[str, tuple[str, ...]]] = []
    cursor = 0
    for chunk in plain_chunks:
        position = text.find(chunk, cursor)
        if position < 0:
            position = cursor
        line_number = text[:position].count("\n")
        title_path = ()
        chunk_line_end = line_number + chunk.count("\n")
        for heading_line, heading_path in headings:
            if heading_line <= line_number:
                title_path = heading_path
            elif heading_line <= chunk_line_end:
                title_path = heading_path
            else:
                break
        results.append((chunk, title_path))
        cursor = max(cursor, position + len(chunk))
    return results


__all__ = ["KnowledgeChunkBuilder"]
