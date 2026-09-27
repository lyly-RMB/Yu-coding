"""HTTP adapter for Feishu tenant token and message APIs."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from project_lens.config import assert_external_calls_allowed
from project_lens.integrations.feishu.adapter import FeishuMessenger


class FeishuHttpTransport(Protocol):
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None = None,
    ) -> tuple[int, dict[str, Any]]: ...


class UrllibFeishuTransport:
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None = None,
    ) -> tuple[int, dict[str, Any]]:
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"msg": raw}
            return exc.code, payload
        except URLError as exc:
            raise RuntimeError(f"Feishu transport unavailable: {exc.reason}") from exc


@dataclass
class _TokenCache:
    value: str | None = None
    expires_at: float = 0.0


class FeishuTenantTokenProvider:
    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        base_url: str = "https://open.feishu.cn",
        transport: FeishuHttpTransport | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._base_url = base_url.rstrip("/")
        self._transport = transport or UrllibFeishuTransport()
        self._cache = _TokenCache()
        self._lock = Lock()

    def get(self) -> str:
        assert_external_calls_allowed("feishu")
        now = time.time()
        with self._lock:
            if self._cache.value and now < self._cache.expires_at:
                return self._cache.value
            status_code, payload = self._transport.request(
                method="POST",
                url=f"{self._base_url}/open-apis/auth/v3/tenant_access_token/internal",
                headers={"Content-Type": "application/json; charset=utf-8"},
                body=json.dumps(
                    {"app_id": self._app_id, "app_secret": self._app_secret}
                ).encode("utf-8"),
            )
            token = payload.get("tenant_access_token")
            if status_code >= 400 or not token:
                raise RuntimeError(f"Feishu token request failed: {payload.get('msg', payload)}")
            expires_in = int(payload.get("expire", 3600))
            self._cache = _TokenCache(
                value=str(token),
                expires_at=now + max(60, expires_in - 60),
            )
            return str(token)


class HttpFeishuMessenger(FeishuMessenger):
    def __init__(
        self,
        *,
        token_provider: FeishuTenantTokenProvider,
        base_url: str = "https://open.feishu.cn",
        transport: FeishuHttpTransport | None = None,
        max_retries: int = 2,
    ) -> None:
        self._token_provider = token_provider
        self._base_url = base_url.rstrip("/")
        self._transport = transport or UrllibFeishuTransport()
        self._max_retries = max_retries

    async def post_text(self, chat_id: str, text: str) -> None:
        """Send a plain text IM message (not an interactive card)."""

        body = (text or "").strip() or "（空回复）"
        # Feishu text content is a JSON string of {"text": "..."}.
        await asyncio.to_thread(
            self._post_sync,
            chat_id,
            {"text": body[:15_000]},
            "chat_id",
            msg_type="text",
        )

    async def post_card(self, chat_id: str, card: dict[str, Any]) -> None:
        await asyncio.to_thread(
            self._post_sync, chat_id, card, "chat_id", msg_type="interactive"
        )

    async def post_user_card(self, open_id: str, card: dict[str, Any]) -> None:
        await asyncio.to_thread(
            self._post_sync, open_id, card, "open_id", msg_type="interactive"
        )

    def _post_sync(
        self,
        receive_id: str,
        content: dict[str, Any],
        receive_id_type: str,
        *,
        msg_type: str = "interactive",
    ) -> None:
        assert_external_calls_allowed("feishu")
        last_payload: object = None
        for attempt in range(self._max_retries + 1):
            token = self._token_provider.get()
            status_code, payload = self._transport.request(
                method="POST",
                url=(
                    f"{self._base_url}/open-apis/im/v1/messages"
                    f"?receive_id_type={receive_id_type}"
                ),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                body=json.dumps(
                    {
                        "receive_id": receive_id,
                        "msg_type": msg_type,
                        "content": json.dumps(content, ensure_ascii=False),
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
            )
            if status_code < 400 and payload.get("code", 0) == 0:
                return
            last_payload = payload
            if status_code not in {429, 500, 502, 503, 504} or attempt >= self._max_retries:
                break
            time.sleep(0.2 * (2**attempt))
        raise RuntimeError(f"Feishu message request failed: {last_payload}")
