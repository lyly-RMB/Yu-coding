"""Index Feishu documents into DOCUMENT evidence with revision provenance."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from project_lens.context.chunking import split_text
from project_lens.context.indexing.common import content_hash
from project_lens.context.sources.feishu_docs import FeishuDocumentRecord, FeishuDocumentSource
from project_lens.domain.models import Evidence, EvidenceType, ProjectRef, SourceRef


class FeishuDocumentIndexer:
    def __init__(self, source: FeishuDocumentSource | None = None) -> None:
        self._source = source or FeishuDocumentSource()

    def index_file(
        self,
        path: Path,
        *,
        project: ProjectRef | None = None,
    ) -> list[Evidence]:
        payload: dict[str, Any] | list[Any] = json.loads(path.read_text(encoding="utf-8"))
        records_raw = payload if isinstance(payload, list) else payload.get("documents", [])
        if not isinstance(records_raw, list):
            raise ValueError("feishu documents file must contain a list or a documents list")
        records = [FeishuDocumentRecord.model_validate(item) for item in records_raw]
        return self.index(records, project=project)

    def index(
        self,
        records: Iterable[FeishuDocumentRecord],
        *,
        project: ProjectRef | None = None,
    ) -> list[Evidence]:
        evidence: list[Evidence] = []
        for record in records:
            try:
                normalized = self._source.normalize(record)
            except ValueError:
                continue
            if project is not None:
                if (
                    normalized.tenant_id != project.tenant_id
                    or normalized.project_id != project.project_id
                ):
                    continue
                item_project = project
            else:
                item_project = ProjectRef(
                    tenant_id=normalized.tenant_id,
                    project_id=normalized.project_id,
                )
            chunks = split_text(normalized.content)
            for index, chunk in enumerate(chunks):
                content = chunk
                evidence.append(
                    Evidence(
                        type=EvidenceType.DOCUMENT,
                        project=item_project,
                        source=SourceRef(
                            system="feishu_doc",
                            source_id=f"{normalized.doc_token}#chunk-{index}",
                            url=normalized.doc_url,
                        ),
                        content=content,
                        observed_at=normalized.updated_at,
                        access_scope=normalized.access_scope,
                        content_hash=content_hash(
                            f"{normalized.doc_token}:{normalized.revision}:{content}"
                        ),
                        metadata={
                            "doc_token": normalized.doc_token,
                            "source_ref": f"feishu_doc:{normalized.doc_token}",
                            "revision": normalized.revision,
                            "title": normalized.title,
                            "owner_user_id": normalized.owner_user_id,
                            "chunk_index": index,
                            "total_chunks": len(chunks),
                        },
                    )
                )
        return evidence
