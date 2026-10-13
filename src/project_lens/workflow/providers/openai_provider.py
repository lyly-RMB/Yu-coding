"""OpenAI-compatible Chat Completions provider (reserved offline or safe live)."""

from __future__ import annotations

import json
from typing import Any

from project_lens.workflow.context_prompt import ContextPrompt
from project_lens.workflow.providers.base import (
    ProviderKind,
    ProviderResult,
    ProviderStatus,
    ProviderUsage,
    assert_prompt_safe,
    estimate_prompt_tokens,
)
from project_lens.workflow.providers.transport import ChatTransport, UrllibChatTransport

_EXPRESSION_SYSTEM = (
    "You are a read-only ProjectLens expression assistant. "
    "Respond with a single JSON object only. "
    "Keys: answer_text (the complete natural-language answer), "
    "facts (array of {text, citations}), unknowns (array of strings), "
    "hypotheses (array of strings), allow_apply (must be false). "
    "Do not invent facts without evidence_ids from the prompt. "
    "Never suggest Apply, PR, deploy, rollback, or memory writes."
)

# This provider remains available for isolated/offline workflow experiments.
# Production Feishu/API/MCP questions use HermesRuntimeService.


class OpenAIProvider:
    name = "openai.v1"
    kind = ProviderKind.OPENAI

    def __init__(
        self,
        *,
        model_name: str = "gpt-4o-mini",
        base_url: str | None = None,
        live: bool = False,
        api_key: str | None = None,
        transport: ChatTransport | None = None,
        timeout_seconds: float = 30.0,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> None:
        self._model_name = model_name
        self._base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self._live = live
        self._api_key = api_key
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._max_tokens = max_tokens
        self._temperature = temperature

    async def complete(self, prompt: ContextPrompt) -> ProviderResult:
        assert_prompt_safe(prompt)
        prompt_tokens = estimate_prompt_tokens(prompt)
        if not self._live:
            return ProviderResult(
                provider=self.name,
                kind=self.kind,
                status=ProviderStatus.RESERVED,
                content=None,
                usage=ProviderUsage(prompt_tokens=prompt_tokens, total_tokens=prompt_tokens),
                allow_apply=False,
                live=False,
                notes=(
                    "OpenAI provider offline (live=false); no network call",
                    f"model_name={self._model_name}",
                    f"base_url_configured={bool(self._base_url)}",
                ),
            )
        if not self._api_key:
            return ProviderResult(
                provider=self.name,
                kind=self.kind,
                status=ProviderStatus.RESERVED,
                content=None,
                usage=ProviderUsage(prompt_tokens=prompt_tokens, total_tokens=prompt_tokens),
                allow_apply=False,
                live=False,
                notes=(
                    "live requested but api_key missing; no network call",
                    f"model_name={self._model_name}",
                ),
            )

        transport = self._transport or UrllibChatTransport()
        body = {
            "model": self._model_name,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            "response_format": {"type": "json_object"},
            "messages": _messages_from_prompt(prompt),
        }
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        # Keep sync transport off the event loop via asyncio in adapter wait_for;
        # transport itself is sync for simple urllib/mock injection.
        import asyncio

        response = await asyncio.to_thread(
            transport.post_json,
            url,
            headers=headers,
            body=body,
            timeout_seconds=self._timeout_seconds,
        )
        content, usage = _extract_chat_completion(response, prompt_tokens=prompt_tokens)
        return ProviderResult(
            provider=self.name,
            kind=self.kind,
            status=ProviderStatus.OK,
            content=content,
            usage=usage,
            allow_apply=False,
            live=True,
            notes=(
                "OpenAI live completion ok",
                f"model_name={self._model_name}",
                f"content_chars={len(content or '')}",
            ),
        )


def _messages_from_prompt(prompt: ContextPrompt) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [{"role": "system", "content": _EXPRESSION_SYSTEM}]
    for item in prompt.messages:
        role = item.role
        if role not in {"system", "user", "assistant"}:
            continue
        content = item.content.strip()
        if not content:
            continue
        messages.append({"role": role, "content": content})
    return messages


def _extract_chat_completion(
    response: dict[str, Any],
    *,
    prompt_tokens: int,
) -> tuple[str | None, ProviderUsage]:
    choices = response.get("choices")
    content: str | None = None
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict):
            raw = message.get("content")
            if isinstance(raw, str):
                content = raw
            elif raw is not None:
                content = json.dumps(raw, ensure_ascii=False)
    usage_raw = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    usage = ProviderUsage(
        prompt_tokens=int(usage_raw.get("prompt_tokens") or prompt_tokens),
        completion_tokens=int(usage_raw.get("completion_tokens") or 0),
        total_tokens=int(
            usage_raw.get("total_tokens")
            or (
                int(usage_raw.get("prompt_tokens") or prompt_tokens)
                + int(usage_raw.get("completion_tokens") or 0)
            )
        ),
    )
    return content, usage
