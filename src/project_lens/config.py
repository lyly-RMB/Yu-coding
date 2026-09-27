"""Application configuration."""

from __future__ import annotations

import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PROJECT_LENS_",
        extra="ignore",
    )

    env: str = "development"
    log_level: str = "INFO"
    api_prefix: str = "/api/v1"
    database_path: str = "project_lens.db"
    # ConversationSession backend: "sqlite" (default) or "memory" fallback.
    conversation_store: str = "sqlite"
    # ContextPrompt ModelProvider: stub (default) | openai | internal | local.
    # Live network / local inference stays disabled in the harness cut.
    model_provider: str = "stub"
    model_name: str = "stub-v1"
    model_timeout_seconds: float = 30.0
    model_max_retries: int = 1
    model_max_tokens: int = 2048
    model_temperature: float = 0.0
    # Safe live LLM (Phase 3): default off; requires api_key + provider support.
    model_live: bool = False
    model_fallback_to_stub: bool = True
    model_openai_base_url: str | None = None
    model_openai_model: str | None = None
    model_openai_api_key: str | None = None
    model_internal_endpoint: str | None = None
    model_local_path: str | None = None
    # Optional semantic memory retrieval. Disabled unless explicitly configured
    # and external calls are allowed. BM25 remains the safe default.
    embedding_provider: str = "none"
    embedding_model: str | None = None
    embedding_api_key: str | None = None
    embedding_base_url: str | None = None
    embedding_timeout_seconds: float = 10.0
    # Knowledge retrieval remains lexical by default. Hybrid/vector can be
    # enabled explicitly after the embedding endpoint is configured.
    knowledge_retrieval_mode: str = "lexical"
    knowledge_vector_enabled: bool = False
    knowledge_embedding_model: str | None = None
    knowledge_embedding_version: str | None = None
    knowledge_chunker_version: str = "v1"
    knowledge_index_version: str = "v1"
    knowledge_max_candidates: int = 200
    knowledge_max_results: int = 8
    knowledge_vector_timeout_seconds: float = 10.0
    knowledge_vector_fallback: bool = True
    # Phase 0 isolation: pytest / explicit test harness must never hit live services.
    test_mode: bool = False
    allow_external_calls: bool = False
    service_token: str | None = None
    # MCP stdio has no HTTP middleware. When configured, these deployment-owned
    # values bind all MCP calls to one trusted service actor; tool arguments
    # must never be treated as actor identity.
    mcp_trusted_tenant_id: str | None = None
    mcp_trusted_actor_id: str | None = None
    mcp_trusted_chat_id: str | None = None
    mcp_trusted_chat_type: str = "group"
    feishu_verification_token: str = "project-lens-local-token"
    feishu_signing_secret: str | None = None
    feishu_app_id: str | None = None
    feishu_app_secret: str | None = None
    feishu_api_base_url: str = "https://open.feishu.cn"
    # Wiki writes are disabled by default. A configured space is still inert
    # until this flag, credentials, and an explicit review approval are present.
    feishu_wiki_publish_enabled: bool = False
    feishu_wiki_space_id: str | None = None
    feishu_wiki_parent_node_token: str | None = None
    github_api_base_url: str = "https://api.github.com"
    github_token: str | None = None
    feishu_project_bindings: str = ""
    local_project_registry: str = ""
    # Show Skill / provider / usage / trace at card bottom as debug (not first screen).
    feishu_show_debug_audit: bool = True
    # Document sync ops: off by default so local tests and chat paths stay isolated.
    feishu_doc_sync_on_startup: bool = False
    feishu_doc_sync_interval_seconds: int = 0
    # Production project questions have exactly one decision runtime. Legacy
    # workflow/read-agent factories are test/evaluation-only and are not
    # configurable through production environment variables.
    # `main.create_app()` always binds production questions to Hermes. Keep a
    # string here so isolated legacy regression fixtures can still construct
    # their own explicit RunService without loading a separate settings model.
    agent_mode: str = "hermes"
    # Legacy ProjectSkill routing / followup / card branching. Kept in code but
    # paused by default for the Hermes-only path; set true to re-enable.
    project_skill_routing_enabled: bool = False
    # Expose approval-gated Hermes proposal/validation tools only when explicitly enabled.
    feishu_hermes_advanced_tools: bool = False
    # Optional background Hermes risk review; disabled by default.
    feishu_hermes_risk_review_enabled: bool = False
    feishu_hermes_risk_review_interval_seconds: int = 0
    # Engineering execution is a separate opt-in and remains disabled by default.
    feishu_hermes_execution_enabled: bool = False
    # Local Hermes agent checkout (sys.path) for in-process AIAgent. Empty = rely on PYTHONPATH.
    hermes_repo: str = ""
    feishu_hermes_provider: str = "openai"
    feishu_hermes_model: str = "gpt-5.4-mini"
    # Optional Hermes-specific credentials. When omitted, reuse the safe live
    # OpenAI-compatible endpoint configured for ProjectLens.
    feishu_hermes_base_url: str | None = None
    feishu_hermes_api_key: str | None = None
    # ProjectLens owns the business context budget; Hermes remains the model
    # and tool-loop executor. Keep this budget below provider maximums.
    context_governor_enabled: bool = True
    context_model_limit_tokens: int = 128_000
    context_soft_limit_ratio: float = 0.45
    context_summarize_ratio: float = 0.60
    context_prune_ratio: float = 0.75
    context_rotate_ratio: float = 0.85
    context_hard_limit_ratio: float = 0.95
    context_output_reserve_tokens: int = 12_000
    context_tool_schema_tokens: int = 2_500
    context_provider_overhead_tokens: int = 1_000
    context_max_recovery_retry: int = 1
    # Optional directory of ProjectSpace JSON files (relative to repo root).
    project_spaces_dir: str = "config/projects"
    # Connector freshness window: answers may warn when last_success_at is older.
    connector_freshness_max_age_seconds: int = 86_400
    # Obsidian is a disabled-by-default local export target. When enabled,
    # the Vault root must be inside this deployment-owned allowed root.
    obsidian_enabled: bool = False
    obsidian_vault_root: str = ""
    obsidian_allowed_root: str = ""
    obsidian_project_subdir: str = "projectlens"
    obsidian_export_mode: str = "reviewed"
    obsidian_max_file_bytes: int = 1_048_576
    obsidian_git_enabled: bool = False
    obsidian_operations_enabled: bool = False
    obsidian_inbox_enabled: bool = False
    # GenAI trace archives (OpenTelemetry GenAI instruction cycles).
    # json under data/traces replaces AgentRun table body; SQLite keeps path index.
    genai_trace_dir: str = "data/traces"
    genai_trace_retention_days: int = 30


settings = Settings()


def is_pytest_running() -> bool:
    """True while a pytest test body (or setup) is active."""

    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def isolation_active(cfg: Settings | None = None) -> bool:
    """Test harness isolation: explicit test_mode or an active pytest test."""

    current = cfg if cfg is not None else settings
    return bool(current.test_mode) or is_pytest_running()


def effective_model_live(cfg: Settings | None = None) -> bool:
    """Live LLM flag after Phase 0 isolation guards."""

    current = cfg if cfg is not None else settings
    if isolation_active(current):
        return False
    return bool(current.model_live)


def external_calls_allowed(cfg: Settings | None = None) -> bool:
    """Whether Feishu HTTP / Hermes live / GitHub outbound may run."""

    current = cfg if cfg is not None else settings
    if isolation_active(current):
        return False
    return bool(current.allow_external_calls)


def assert_external_calls_allowed(service: str, cfg: Settings | None = None) -> None:
    """Hard-stop before any named external network client runs."""

    if external_calls_allowed(cfg):
        return
    raise RuntimeError(
        f"{service} external call blocked: test_mode/pytest active or "
        "allow_external_calls=false"
    )


def allow_demo_binding_fallback(cfg: Settings | None = None) -> bool:
    """Demo chat-1 fallback only for local development outside test isolation."""

    current = cfg if cfg is not None else settings
    if isolation_active(current):
        return False
    return current.env.strip().lower() == "development"
