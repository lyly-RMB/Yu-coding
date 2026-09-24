"""Unified knowledge index contracts and persistence."""

from project_lens.context.knowledge_index.adapters import (
    EpisodeAdapter,
    EvidenceAdapter,
    MemoryObservationAdapter,
    ObsidianWikiAdapter,
    ProjectMemoryAdapter,
    SourceRecordAdapter,
)
from project_lens.context.knowledge_index.chunking import KnowledgeChunkBuilder
from project_lens.context.knowledge_index.embeddings import (
    EmbeddingBatchResult,
    EmbeddingGenerationError,
    KnowledgeEmbeddingService,
)
from project_lens.context.knowledge_index.indexer import (
    KnowledgeIndexBuildResult,
    KnowledgeIndexBuildService,
)
from project_lens.context.knowledge_index.models import (
    EmbeddingRecord,
    IndexRecordStatus,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIndexJob,
    KnowledgeObjectKind,
    content_hash,
    stable_chunk_id,
    stable_document_id,
)
from project_lens.context.knowledge_index.store import (
    KnowledgeIndexStats,
    SQLiteKnowledgeIndexStore,
)
from project_lens.context.knowledge_index.sync import (
    KnowledgeIndexSyncResult,
    KnowledgeIndexSyncService,
)

__all__ = [
    "EmbeddingRecord",
    "EmbeddingBatchResult",
    "EmbeddingGenerationError",
    "EpisodeAdapter",
    "EvidenceAdapter",
    "IndexRecordStatus",
    "KnowledgeChunk",
    "KnowledgeChunkBuilder",
    "KnowledgeDocument",
    "KnowledgeIndexJob",
    "KnowledgeEmbeddingService",
    "KnowledgeIndexBuildResult",
    "KnowledgeIndexBuildService",
    "KnowledgeIndexStats",
    "KnowledgeIndexSyncResult",
    "KnowledgeIndexSyncService",
    "KnowledgeObjectKind",
    "MemoryObservationAdapter",
    "ObsidianWikiAdapter",
    "ProjectMemoryAdapter",
    "SQLiteKnowledgeIndexStore",
    "SourceRecordAdapter",
    "content_hash",
    "stable_chunk_id",
    "stable_document_id",
]


def __getattr__(name: str):
    if name == "KnowledgeIndexVectorRetriever":
        from project_lens.context.retrieval.knowledge_vector import (
            KnowledgeIndexVectorRetriever,
        )

        return KnowledgeIndexVectorRetriever
    raise AttributeError(name)


__all__.append("KnowledgeIndexVectorRetriever")
