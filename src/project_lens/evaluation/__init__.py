"""ProjectLens evaluation helpers.

Heavy harness integrations are loaded lazily so lightweight offline metrics
can run without importing the application configuration stack.
"""

from importlib import import_module

from project_lens.evaluation.retrieval import mean_reciprocal_rank, recall_at_k
from project_lens.evaluation.memory_retrieval import (
    HistoryRetrievalCase,
    MemoryRetrievalCase,
    evaluate_history_retrieval,
    evaluate_memory_retrieval,
)
from project_lens.evaluation.context_compression import (
    CompressionCase,
    CompressionEvaluationReport,
    CompressionProbe,
    evaluate_compression,
    load_compression_cases,
)

__all__ = [
    "HarnessProbeReport",
    "HistoryRetrievalCase",
    "MemoryRetrievalCase",
    "ReplayStep",
    "mean_reciprocal_rank",
    "recall_at_k",
    "replay_harness_conversation",
    "evaluate_history_retrieval",
    "evaluate_memory_retrieval",
    "run_harness_probes",
    "CompressionCase",
    "CompressionEvaluationReport",
    "CompressionProbe",
    "evaluate_compression",
    "load_compression_cases",
]


_LAZY_EXPORTS = {
    "HarnessProbeReport": ("project_lens.evaluation.harness_probes", "HarnessProbeReport"),
    "run_harness_probes": ("project_lens.evaluation.harness_probes", "run_harness_probes"),
    "ReplayStep": ("project_lens.evaluation.harness_replay", "ReplayStep"),
    "replay_harness_conversation": ("project_lens.evaluation.harness_replay", "replay_harness_conversation"),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
