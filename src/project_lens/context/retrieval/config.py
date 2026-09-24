"""Configuration contract for the unified knowledge retrieval pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from project_lens.config import Settings

RetrievalMode = Literal["lexical", "vector", "hybrid"]

DEFAULT_MAX_CANDIDATES = 200
DEFAULT_MAX_RESULTS = 8
MAX_CANDIDATES_LIMIT = 1_000
MAX_RESULTS_LIMIT = 100


@dataclass(frozen=True)
class RetrievalConfig:
    """Validated, bounded retrieval policy used by later index/search stages."""

    mode: RetrievalMode = "lexical"
    vector_enabled: bool = False
    embedding_provider: str = "none"
    embedding_model: str | None = None
    embedding_version: str | None = None
    embedding_credentials_configured: bool = False
    chunker_version: str = "v1"
    index_version: str = "v1"
    max_candidates: int = DEFAULT_MAX_CANDIDATES
    max_results: int = DEFAULT_MAX_RESULTS
    vector_timeout_seconds: float = 10.0
    vector_fallback: bool = True

    @property
    def vector_ready(self) -> bool:
        """Whether vector retrieval may be selected without external side effects."""

        return bool(
            self.vector_enabled
            and self.embedding_provider.casefold() != "none"
            and self.embedding_model
            and self.embedding_version
            and self.embedding_credentials_configured
        )

    @property
    def effective_mode(self) -> RetrievalMode:
        """Return a safe mode when vector prerequisites are incomplete."""

        if self.mode == "lexical":
            return "lexical"
        if self.vector_ready:
            return self.mode
        if self.vector_fallback:
            return "lexical"
        raise ValueError(
            "Vector retrieval is enabled but embedding provider/model/version "
            "configuration is incomplete"
        )

    def for_query(self, *, requested_mode: str | None = None) -> "RetrievalConfig":
        """Return this policy with an optional validated per-query mode override."""

        mode = normalize_mode(requested_mode if requested_mode is not None else self.mode)
        return RetrievalConfig(
            mode=mode,
            vector_enabled=self.vector_enabled,
            embedding_provider=self.embedding_provider,
            embedding_model=self.embedding_model,
            embedding_version=self.embedding_version,
            embedding_credentials_configured=self.embedding_credentials_configured,
            chunker_version=self.chunker_version,
            index_version=self.index_version,
            max_candidates=self.max_candidates,
            max_results=self.max_results,
            vector_timeout_seconds=self.vector_timeout_seconds,
            vector_fallback=self.vector_fallback,
        )


def normalize_mode(value: str) -> RetrievalMode:
    """Validate the public retrieval mode vocabulary."""

    normalized = value.strip().casefold()
    if normalized not in {"lexical", "vector", "hybrid"}:
        raise ValueError(
            f"Unsupported knowledge retrieval mode {value!r}; "
            "expected lexical, vector, or hybrid"
        )
    return normalized  # type: ignore[return-value]


def bounded_limit(value: int, *, default: int, maximum: int) -> int:
    """Keep operator-provided limits positive and within service hard caps."""

    if value <= 0:
        return default
    return min(value, maximum)


def retrieval_config_from_settings(settings: Settings) -> RetrievalConfig:
    """Build the bounded retrieval contract without creating any provider/client."""

    mode = normalize_mode(settings.knowledge_retrieval_mode)
    max_candidates = bounded_limit(
        settings.knowledge_max_candidates,
        default=DEFAULT_MAX_CANDIDATES,
        maximum=MAX_CANDIDATES_LIMIT,
    )
    max_results = bounded_limit(
        settings.knowledge_max_results,
        default=DEFAULT_MAX_RESULTS,
        maximum=MAX_RESULTS_LIMIT,
    )
    if max_results > max_candidates:
        max_results = min(DEFAULT_MAX_RESULTS, max_candidates)

    provider = settings.embedding_provider.strip().casefold() or "none"
    model = (
        (settings.knowledge_embedding_model or "").strip()
        or (settings.embedding_model or "").strip()
        or None
    )
    version = (settings.knowledge_embedding_version or "").strip() or None
    chunker_version = settings.knowledge_chunker_version.strip() or "v1"
    index_version = settings.knowledge_index_version.strip() or "v1"
    timeout = max(float(settings.knowledge_vector_timeout_seconds), 0.1)
    credentials_configured = (
        provider != "openai"
        or bool(
            (settings.embedding_api_key or "").strip()
            or (settings.model_openai_api_key or "").strip()
        )
    )

    return RetrievalConfig(
        mode=mode,
        vector_enabled=bool(settings.knowledge_vector_enabled),
        embedding_provider=provider,
        embedding_model=model,
        embedding_version=version,
        embedding_credentials_configured=credentials_configured,
        chunker_version=chunker_version,
        index_version=index_version,
        max_candidates=max_candidates,
        max_results=max_results,
        vector_timeout_seconds=timeout,
        vector_fallback=bool(settings.knowledge_vector_fallback),
    )
