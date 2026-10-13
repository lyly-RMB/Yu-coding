"""Phase 3 Safe Live LLM provider safety tests (mock transport only)."""

from __future__ import annotations

import json

import pytest

from project_lens.config import Settings
from project_lens.workflow.context_prompt import render_context_prompt
from project_lens.workflow.model_adapter import ProviderModelAdapter
from project_lens.workflow.providers import (
    OpenAIProvider,
    ProviderKind,
    ProviderRuntimeConfig,
    StubProvider,
    create_model_adapter_from_settings,
    provider_config_from_settings,
)
from project_lens.workflow.providers.transport import RecordingChatTransport
from tests.test_context_prompt import _pack


@pytest.mark.asyncio
async def test_live_false_does_not_call_transport() -> None:
    transport = RecordingChatTransport(response={})
    provider = OpenAIProvider(
        live=False,
        api_key="sk-test",
        transport=transport,
    )
    result = await provider.complete(render_context_prompt(_pack()))
    assert result.status.value == "reserved"
    assert result.live is False
    assert transport.calls == []


@pytest.mark.asyncio
async def test_live_true_without_api_key_does_not_network() -> None:
    transport = RecordingChatTransport(response={})
    provider = OpenAIProvider(live=True, api_key=None, transport=transport)
    result = await provider.complete(render_context_prompt(_pack()))
    assert result.status.value == "reserved"
    assert result.live is False
    assert transport.calls == []


@pytest.mark.asyncio
async def test_live_true_with_mock_transport_uses_context_prompt_only() -> None:
    prompt = render_context_prompt(_pack())
    payload = {
        "answer_text": "产品：下单可能受 coupon 为空影响。\n研发：optional coupon 未做空判断。\n测试：边界覆盖待确认。",
        "facts": [],
        "unknowns": [],
        "hypotheses": [],
        "allow_apply": False,
    }
    transport = RecordingChatTransport(
        response={
            "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
        }
    )
    adapter = ProviderModelAdapter(
        OpenAIProvider(
            live=True,
            api_key="sk-test",
            transport=transport,
            model_name="gpt-test",
        ),
        config=ProviderRuntimeConfig(
            kind=ProviderKind.OPENAI,
            live=True,
            model_name="gpt-test",
            fallback_to_stub=True,
        ),
        fallback_provider=StubProvider(),
        live_requested=True,
    )
    result = await adapter.prepare(prompt)
    assert result.status == "ok"
    assert result.live_effective is True
    assert result.usage["total_tokens"] == 140
    assert transport.calls
    body = transport.calls[0]["body"]
    assert body["messages"][0]["role"] == "system"
    assert any(item["role"] == "user" for item in body["messages"])
    assert transport.calls[0]["headers"]["Authorization"] == "***"
    assert "sk-test" not in str(transport.calls)


@pytest.mark.asyncio
async def test_live_failure_falls_back_without_crashing() -> None:
    prompt = render_context_prompt(_pack())
    transport = RecordingChatTransport(error=RuntimeError("network down"))
    adapter = ProviderModelAdapter(
        OpenAIProvider(live=True, api_key="sk-test", transport=transport),
        config=ProviderRuntimeConfig(
            kind=ProviderKind.OPENAI,
            live=True,
            fallback_to_stub=True,
            max_retries=0,
        ),
        fallback_provider=StubProvider(model_name="stub-fallback"),
        live_requested=True,
    )
    result = await adapter.prepare(prompt)
    assert result.fallback_used is True
    assert result.status == "fallback"
    assert result.provider == "stub.v1"
    assert result.allow_apply is False


def test_settings_live_requested_without_key_stays_offline() -> None:
    settings = Settings(
        model_provider="openai",
        model_live=True,
        model_openai_api_key=None,
        model_fallback_to_stub=True,
    )
    config = provider_config_from_settings(settings)
    # Under pytest isolation, effective_model_live() forces live=False.
    assert config.live is False
    adapter = create_model_adapter_from_settings(settings)
    assert adapter.config.live is False
