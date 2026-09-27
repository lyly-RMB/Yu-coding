"""Feishu long-connection (WebSocket) ingress → local ProjectLens event API.

Replaces cloudflared HTTP webhooks for local/dev: Feishu pushes over WS;
this process rebuilds the webhook JSON shape and POSTs to
``/api/v1/feishu/events`` on localhost so preview cards / card actions work
without a public URL.

Requires optional dependency ``lark-oapi``. Do not run Hermes Feishu gateway
at the same time (only one long connection per app).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


class FeishuWsIngress:
    """Own Feishu WS long-connection and forward events to local HTTP callback."""

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        verification_token: str,
        events_url: str = "http://127.0.0.1:8000/api/v1/feishu/events",
        signing_secret: str | None = None,
        domain: str = "https://open.feishu.cn",
        encrypt_key: str = "",
        timeout_seconds: float = 8.0,
    ) -> None:
        self._app_id = app_id.strip()
        self._app_secret = app_secret.strip()
        self._verification_token = verification_token.strip()
        self._events_url = events_url.rstrip("/")
        self._signing_secret = (signing_secret or "").strip() or None
        self._domain = domain.rstrip("/")
        self._encrypt_key = encrypt_key or ""
        self._timeout_seconds = timeout_seconds

    def run_forever(self) -> None:
        """Block on the official Feishu WS client (call from a dedicated process)."""

        try:
            import lark_oapi as lark
            from lark_oapi.event.callback.model.p2_card_action_trigger import (
                P2CardActionTriggerResponse,
            )
            from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
            from lark_oapi.ws import Client as FeishuWSClient
        except ImportError as exc:  # pragma: no cover - env/install issue
            raise RuntimeError(
                "lark-oapi is required for Feishu WS ingress. "
                'Install with: pip install "lark-oapi>=1.4.0"'
            ) from exc

        if not self._app_id or not self._app_secret:
            raise RuntimeError("feishu app_id/app_secret are required for WS ingress")
        if not self._verification_token:
            raise RuntimeError("feishu verification_token is required for WS ingress")

        def on_message(data: Any) -> None:
            try:
                payload = _message_event_to_payload(data, token=self._verification_token)
                self._post_local(payload)
            except Exception:  # noqa: BLE001 - never crash the WS thread
                logger.exception("feishu ws message forward failed")

        def on_card(data: Any) -> Any:
            try:
                payload = _card_action_to_payload(data, token=self._verification_token)
                result = self._post_local(payload)
                return _card_action_response_from_api(result)
            except Exception:  # noqa: BLE001
                logger.exception("feishu ws card-action forward failed")
            return P2CardActionTriggerResponse()

        handler = (
            EventDispatcherHandler.builder(self._encrypt_key, self._verification_token)
            .register_p2_im_message_receive_v1(on_message)
            .register_p2_card_action_trigger(on_card)
            .build()
        )
        # ``channel`` UA tag: Feishu WS must advertise Channel protocol or
        # group @mention events are not pushed (same as Hermes adapter).
        client_kwargs: dict[str, Any] = {
            "app_id": self._app_id,
            "app_secret": self._app_secret,
            "log_level": lark.LogLevel.INFO,
            "event_handler": handler,
            "domain": self._domain,
        }
        try:
            client = FeishuWSClient(**client_kwargs, extra_ua_tags=["channel"])
        except TypeError:
            logger.warning(
                "lark-oapi FeishuWSClient has no extra_ua_tags; "
                "group @mention push may be incomplete"
            )
            client = FeishuWSClient(**client_kwargs)

        logger.info(
            "feishu ws ingress starting domain=%s events_url=%s",
            self._domain,
            self._events_url,
        )
        client.start()

    def _post_local(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self._signing_secret:
            timestamp = str(int(time.time()))
            nonce = uuid.uuid4().hex[:16]
            content = f"{timestamp}{nonce}{self._signing_secret}".encode() + body
            headers.update(
                {
                    "X-Lark-Request-Timestamp": timestamp,
                    "X-Lark-Request-Nonce": nonce,
                    "X-Lark-Signature": hashlib.sha256(content).hexdigest(),
                }
            )
        request = Request(self._events_url, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                status_code = response.status
                raw = response.read().decode("utf-8", errors="replace")
            logger.info(
                "feishu ws forwarded event_type=%s status=%s body=%s",
                (payload.get("header") or {}).get("event_type"),
                status_code,
                raw[:200],
            )
            try:
                parsed = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                parsed = {}
            return parsed if isinstance(parsed, dict) else {}
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            logger.error(
                "feishu ws forward HTTP %s: %s",
                exc.code,
                detail[:500],
            )
            raise
        except URLError as exc:
            logger.error("feishu ws forward unreachable: %s", exc.reason)
            raise


def _card_action_response_from_api(result: dict[str, Any]) -> Any:
    """Map local API JSON into lark-oapi card-action callback response."""

    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        CallBackCard,
        CallBackToast,
        P2CardActionTriggerResponse,
    )

    response = P2CardActionTriggerResponse()
    card = result.get("card")
    if isinstance(card, dict) and card.get("data") is not None:
        response.card = CallBackCard(
            {
                "type": str(card.get("type") or "raw"),
                "data": card.get("data"),
            }
        )
    toast = result.get("toast")
    if isinstance(toast, dict) and str(toast.get("content") or "").strip():
        response.toast = CallBackToast(
            {
                "type": str(toast.get("type") or "info"),
                "content": str(toast.get("content")),
            }
        )
    return response


def _message_event_to_payload(data: Any, *, token: str) -> dict[str, Any]:
    header = getattr(data, "header", None)
    event = getattr(data, "event", None)
    message = getattr(event, "message", None)
    sender = getattr(event, "sender", None)
    sender_id = getattr(sender, "sender_id", None)
    event_id = str(getattr(header, "event_id", None) or f"ws-msg-{uuid.uuid4().hex[:12]}")
    tenant_key = str(
        getattr(header, "tenant_key", None)
        or getattr(sender, "tenant_key", None)
        or ""
    ).strip()
    if not tenant_key:
        raise ValueError("missing tenant_key on Feishu WS message event")
    content = getattr(message, "content", None)
    if content is None:
        content = ""
    elif not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    return {
        "schema": str(getattr(data, "schema", None) or "2.0"),
        "token": token,
        "header": {
            "event_id": event_id,
            "event_type": str(
                getattr(header, "event_type", None) or "im.message.receive_v1"
            ),
            "tenant_key": tenant_key,
        },
        "event": {
            "sender": {
                "sender_id": {
                    "open_id": str(getattr(sender_id, "open_id", None) or ""),
                    "user_id": str(getattr(sender_id, "user_id", None) or "") or None,
                },
                "sender_type": str(getattr(sender, "sender_type", None) or "") or None,
                "tenant_key": tenant_key,
            },
            "message": {
                "message_id": str(getattr(message, "message_id", None) or ""),
                "chat_id": str(getattr(message, "chat_id", None) or ""),
                "chat_type": str(getattr(message, "chat_type", None) or "group"),
                "message_type": str(getattr(message, "message_type", None) or "text"),
                "content": content,
            },
        },
    }


def _card_action_to_payload(data: Any, *, token: str) -> dict[str, Any]:
    header = getattr(data, "header", None)
    event = getattr(data, "event", None)
    operator = getattr(event, "operator", None)
    action = getattr(event, "action", None)
    context = getattr(event, "context", None)
    event_id = str(getattr(header, "event_id", None) or f"ws-card-{uuid.uuid4().hex[:12]}")
    tenant_key = str(
        getattr(header, "tenant_key", None)
        or getattr(operator, "tenant_key", None)
        or ""
    ).strip()
    if not tenant_key:
        raise ValueError("missing tenant_key on Feishu WS card action")
    value = getattr(action, "value", None) or {}
    if not isinstance(value, dict):
        value = {}
    # date_picker / select callbacks put the chosen value on action.option
    # (and timezone). Dropping them makes date-window coarse filter a no-op
    # over WS ingress — option never reaches set_date_window.
    action_body: dict[str, Any] = {
        "tag": str(getattr(action, "tag", None) or "button"),
        "value": value,
    }
    option = getattr(action, "option", None)
    if option is not None and str(option).strip():
        action_body["option"] = str(option).strip()
    timezone = getattr(action, "timezone", None)
    if timezone is not None and str(timezone).strip():
        action_body["timezone"] = str(timezone).strip()
    form_value = getattr(action, "form_value", None)
    if isinstance(form_value, dict) and form_value:
        action_body["form_value"] = form_value
    return {
        "schema": str(getattr(data, "schema", None) or "2.0"),
        "token": token,
        "header": {
            "event_id": event_id,
            "event_type": "card.action.trigger",
            "tenant_key": tenant_key,
        },
        "event": {
            "operator": {
                "open_id": str(getattr(operator, "open_id", None) or ""),
                "user_id": str(getattr(operator, "user_id", None) or ""),
                "tenant_key": tenant_key,
            },
            "action": action_body,
            "context": {
                "open_chat_id": str(getattr(context, "open_chat_id", None) or ""),
                "open_message_id": str(getattr(context, "open_message_id", None) or ""),
            },
            "token": str(getattr(event, "token", None) or ""),
        },
    }


def main() -> None:
    """CLI entry: ``python -m project_lens.integrations.feishu.ws_ingress``."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    from project_lens.config import settings

    ingress = FeishuWsIngress(
        app_id=str(settings.feishu_app_id or ""),
        app_secret=str(settings.feishu_app_secret or ""),
        verification_token=settings.feishu_verification_token,
        signing_secret=settings.feishu_signing_secret,
        events_url="http://127.0.0.1:8000/api/v1/feishu/events",
        domain=settings.feishu_api_base_url,
    )
    ingress.run_forever()


if __name__ == "__main__":
    main()
