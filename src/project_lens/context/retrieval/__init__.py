"""Evidence retrieval channels and fusion."""

from project_lens.context.retrieval.bm25 import BM25Retriever
from project_lens.context.retrieval.config import (
    RetrievalConfig,
    RetrievalMode,
    retrieval_config_from_settings,
)
from project_lens.context.retrieval.exact import ExactCodeRetriever
from project_lens.context.retrieval.fusion import ReciprocalRankFusion
from project_lens.context.retrieval.knowledge_vector import KnowledgeIndexVectorRetriever
from project_lens.context.retrieval.vector import EvidenceVectorRetriever

__all__ = [
    "BM25Retriever",
    "ExactCodeRetriever",
    "EvidenceVectorRetriever",
    "KnowledgeIndexVectorRetriever",
    "ReciprocalRankFusion",
    "RetrievalConfig",
    "RetrievalMode",
    "retrieval_config_from_settings",
]
