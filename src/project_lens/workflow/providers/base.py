"""ContextPrompt-only ModelProvider contracts.

Distinct from runtime.types.ModelProvider (chat/tools).
Harness providers never accept raw Feishu text or EvidenceIndex access.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from project_lens.workflow.context_prompt import ContextPrompt


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProviderKind(StrEnum):
    STUB = "stub"
    OPENAI = "openai"
    INTERNAL = "internal"
    LOCAL = "local"


class ProviderStatus(StrEnum):
    OK = "ok"
    RESERVED = "reserved"
    ERROR = "error"
    TIMEOUT = "timeout"
    FALLBACK = "fallback"


class ProviderUsage(FrozenModel):
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


class ProviderAttempt(FrozenModel):
    attempt: int = Field(ge=1)
    ok: bool
    timed_out: bool = False
    error: str | None = None
    latency_ms: float = Field(default=0.0, ge=0.0)


class ProviderResult(FrozenModel):
    """Outcome of one provider.complete call (before adapter retry aggregation)."""

    provider: str = Field(min_length=1, max_length=100)
    kind: ProviderKind
    status: ProviderStatus = ProviderStatus.OK
    content: str | None = None
    usage: ProviderUsage = Field(default_factory=ProviderUsage)
    allow_apply: bool = False
    notes: tuple[str, ...] = ()
    live: bool = False

    def audit_refs(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "kind": self.kind.value,
            "status": self.status.value,
            "usage": self.usage.as_dict(),
            "allow_apply": False,
            "live": self.live,
            "notes": list(self.notes),
            "has_content": self.content is not None,
        }


class ProviderRuntimeConfig(FrozenModel):
    kind: ProviderKind = ProviderKind.STUB
    model_name: str = "stub-v1"
    timeout_seconds: float = Field(default=30.0, gt=0.0, le=600.0)
    max_retries: int = Field(default=1, ge=0, le=5)
    max_tokens: int = Field(default=2048, ge=1, le=128_000)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    # Requested live for OpenAI Safe Live (Phase 3). Other kinds are coerced off.
    live: bool = False
    fallback_to_stub: bool = True
    openai_base_url: str | None = None
    openai_model: str | None = None
    internal_endpoint: str | None = None
    local_model_path: str | None = None


class ProviderConfigError(ValueError):
    """Invalid provider runtime configuration."""


def validate_provider_config(config: ProviderRuntimeConfig) -> ProviderRuntimeConfig:
    """Validate provider config ranges. Does not perform network I/O."""

    if config.timeout_seconds <= 0 or config.timeout_seconds > 600:
        raise ProviderConfigError("model_timeout_seconds must be in (0, 600]")
    if config.max_retries < 0 or config.max_retries > 5:
        raise ProviderConfigError("model_max_retries must be in [0, 5]")
    if config.max_tokens < 1 or config.max_tokens > 128_000:
        raise ProviderConfigError("model_max_tokens must be in [1, 128000]")
    if config.temperature < 0.0 or config.temperature > 2.0:
        raise ProviderConfigError("model_temperature must be in [0.0, 2.0]")
    if not config.model_name.strip():
        raise ProviderConfigError("model_name must not be empty")
    # Phase 3: only OpenAI may request live; internal/local/stub stay offline.
    if config.live and config.kind != ProviderKind.OPENAI:
        return config.model_copy(update={"live": False})
    return config


def assert_live_disabled(*, live: bool, provider_name: str) -> None:
    """Hard stop before any network or weight-loading call."""

    if live:
        raise RuntimeError(
            f"{provider_name} refused live call; this provider is offline-only "
            "(Safe Live is OpenAI-only in Phase 3)"
        )


class ModelProvider(Protocol):
    """Complete from a ContextPrompt only. Must not call Feishu or EvidenceIndex."""

    name: str
    kind: ProviderKind

    async def complete(self, prompt: ContextPrompt) -> ProviderResult: ...


def assert_prompt_safe(prompt: ContextPrompt) -> tuple[str, ...]:
    """Shared L0-L5 / allow_apply gate for every provider."""

    if prompt.audit_refs.get("allow_apply") is True:
        raise ValueError("ModelProvider refuses allow_apply=True in prompt audit_refs")
    rendered = prompt.as_text()
    if "allow_apply=True" in rendered:
        raise ValueError("ModelProvider refuses allow_apply=True in prompt text")
    layers = tuple(item.layer for item in prompt.sections)
    if layers != ("L0", "L1", "L2", "L3", "L4", "L5"):
        raise ValueError(f"ModelProvider requires L0-L5 sections, got {layers}")
    l0 = next(item for item in prompt.sections if item.layer == "L0")
    if "tool_boundary=" not in l0.content or "allow_apply=False" not in l0.content:
        raise ValueError("ModelProvider requires L0 policy anchors")
    return layers


def estimate_prompt_tokens(prompt: ContextPrompt) -> int:
    text = prompt.as_text()
    return max(1, len(text) // 4)
