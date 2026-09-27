"""Feishu IM history client — list chat messages / get one message body (S08)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlencode

from project_lens.application.group_message_gateway import (
    GroupChatHistoryPage,
    GroupChatMessage,
)
from project_lens.integrations.feishu.http_adapter import (
    FeishuHttpTransport,
    FeishuTenantTokenProvider,
    UrllibFeishuTransport,
)

# Feishu OpenAPI codes that mean group history cannot be used (permissions / bot / mode).
_UNAVAILABLE_CODES = frozenset(
    {
        230002,  # bot not in chat / no permission
        231203,  # confidential mode
        99991663,
        99991672,
        99991668,
    }
)


class FeishuMessagesClient:
    """GET /open-apis/im/v1/messages — chat history + single-message body."""

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

    def list_chat_history(
        self,
        *,
        chat_id: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        page_size: int = 5,
        page_token: str | None = None,
    ) -> GroupChatHistoryPage:
        cid = chat_id.strip()
        if not cid:
            return GroupChatHistoryPage(
                available=False,
                unavailable_reason="chat_id is required",
            )
        params: dict[str, str] = {
            "container_id_type": "chat",
            "container_id": cid,
            "page_size": str(max(1, min(int(page_size), 50))),
            "sort_type": "ByCreateTimeDesc",
        }
        if start_time is not None:
            params["start_time"] = str(int(start_time.astimezone(timezone.utc).timestamp()))
        if end_time is not None:
            params["end_time"] = str(int(end_time.astimezone(timezone.utc).timestamp()))
        if page_token and page_token.strip():
            params["page_token"] = page_token.strip()

        try:
            payload = self._get_json(f"/open-apis/im/v1/messages?{urlencode(params)}")
        except FeishuMessagesApiError as exc:
            if exc.code in _UNAVAILABLE_CODES or exc.permission_like:
                return GroupChatHistoryPage(
                    available=False,
                    unavailable_reason=exc.user_message(),
                )
            return GroupChatHistoryPage(
                available=False,
                unavailable_reason=exc.user_message(),
            )

        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        raw_items = data.get("items") if isinstance(data.get("items"), list) else []
        messages: list[GroupChatMessage] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            if _is_non_citable_message(raw):
                continue
            parsed = _parse_message(raw)
            if parsed is not None:
                messages.append(parsed)
        next_token = str(data.get("page_token") or "").strip() or None
        return GroupChatHistoryPage(
            items=tuple(messages),
            has_more=bool(data.get("has_more")),
            next_page_token=next_token if data.get("has_more") else None,
            available=True,
        )

    def get_message_text(self, message_id: str) -> str | None:
        mid = message_id.strip()
        if not mid:
            return None
        try:
            payload = self._get_json(
                f"/open-apis/im/v1/messages/{quote(mid, safe='')}"
            )
        except FeishuMessagesApiError:
            return None
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        # Single-message API may nest under data.items[0] or data.message.
        if isinstance(data.get("items"), list) and data["items"]:
            first = data["items"][0]
            if isinstance(first, dict):
                parsed = _parse_message(first)
                return parsed.text if parsed else None
        if isinstance(data.get("message"), dict):
            parsed = _parse_message(data["message"])
            return parsed.text if parsed else None
        # Some responses put fields at data root.
        if data.get("message_id") or data.get("body"):
            parsed = _parse_message(data)
            return parsed.text if parsed else None
        return None

    def _get_json(self, path: str) -> dict[str, Any]:
        last_payload: object = None
        last_status = 0
        for attempt in range(self._max_retries + 1):
            token = self._token_provider.get()
            status_code, payload = self._transport.request(
                method="GET",
                url=f"{self._base_url}{path}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
            )
            last_status = status_code
            last_payload = payload
            code = int(payload.get("code", -1)) if isinstance(payload, dict) else -1
            if status_code < 400 and code == 0:
                return payload if isinstance(payload, dict) else {"code": 0, "data": {}}
            if status_code not in {429, 500, 502, 503, 504} or attempt >= self._max_retries:
                break
            time.sleep(0.2 * (2**attempt))
        raise FeishuMessagesApiError(
            status_code=last_status,
            payload=last_payload if isinstance(last_payload, dict) else {"msg": last_payload},
        )


class FeishuMessagesApiError(RuntimeError):
    """Feishu IM messages API non-success response."""

    def __init__(self, *, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self.payload = payload
        self.code = int(payload.get("code", -1) or -1)
        self.msg = str(payload.get("msg") or payload.get("message") or "unknown")
        lower = self.msg.lower()
        self.permission_like = (
            self.code in _UNAVAILABLE_CODES
            or "permission" in lower
            or "scope" in lower
            or "权限" in self.msg
            or "no access" in lower
        )
        super().__init__(self.user_message())

    def user_message(self) -> str:
        return f"feishu_im_unavailable code={self.code} msg={self.msg}"


# Interactive cards / shares are not useful as citation bodies for fine-select.
_NON_CITABLE_MSG_TYPES = frozenset(
    {
        "interactive",
        "share_chat",
        "share_user",
        "system",
    }
)


def _is_non_citable_message(raw: dict[str, Any]) -> bool:
    msg_type = str(raw.get("msg_type") or "").strip().lower()
    return msg_type in _NON_CITABLE_MSG_TYPES


def _parse_message(raw: dict[str, Any]) -> GroupChatMessage | None:
    message_id = str(raw.get("message_id") or "").strip()
    if not message_id:
        return None
    occurred_at = _parse_create_time(raw.get("create_time"))
    if occurred_at is None:
        return None
    text = _extract_body_text(raw)
    if not text.strip():
        msg_type = str(raw.get("msg_type") or "").strip() or "unknown"
        text = f"[{msg_type}]"
    sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
    sender_id = str(
        sender.get("id") or sender.get("sender_id") or ""
    ).strip() or None
    return GroupChatMessage(
        message_id=message_id,
        occurred_at=occurred_at,
        text=text,
        sender_id=sender_id,
    )


def _parse_create_time(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        # Feishu returns millisecond unix timestamp as string.
        raw = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if raw > 10_000_000_000:
        raw = raw // 1000
    return datetime.fromtimestamp(raw, tz=timezone.utc)


def _extract_body_text(raw: dict[str, Any]) -> str:
    body = raw.get("body") if isinstance(raw.get("body"), dict) else {}
    content = body.get("content")
    if content is None:
        return ""
    if isinstance(content, dict):
        return _text_from_content_obj(content)
    text = str(content).strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(parsed, dict):
        return _text_from_content_obj(parsed)
    return text


def _text_from_content_obj(obj: dict[str, Any]) -> str:
    for key in ("text", "content", "title"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    # post / rich text: flatten string leaves
    chunks: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, str):
            if node.strip():
                chunks.append(node.strip())
        elif isinstance(node, dict):
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(obj)
    return " ".join(chunks)
