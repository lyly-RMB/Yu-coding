"""Optional Feishu WebSocket long-connection bridge."""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from threading import Thread
from typing import Any

from fastapi import BackgroundTasks

from project_lens.integrations.feishu.security import build_signature

logger = logging.getLogger(__name__)


def signed_callback_auth(
    *,
    body: bytes,
    signing_secret: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Build timestamp/nonce/signature for in-process Feishu callback dispatch."""

    if not signing_secret:
        return None, None, None
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex[:16]
    signature = build_signature(
        body=body,
        timestamp=timestamp,
        nonce=nonce,
        signing_secret=signing_secret,
    )
    return timestamp, nonce, signature


class FeishuLongConnection:
    def __init__(self, *, app_id: str, app_secret: str, event_service: Any, enabled: bool = True) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._event_service = event_service
        self._enabled = enabled and bool(app_id and app_secret)
        self._thread: Thread | None = None
        self._client = None
        self._loop = None
        self._status = "disabled" if not self._enabled else "stopped"
        self._last_connected_at: datetime | None = None
        self._last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def status(self) -> str:
        return self._status

    @property
    def last_connected_at(self) -> datetime | None:
        return self._last_connected_at

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def start(self) -> None:
        if not self._enabled or self._thread is not None:
            return
        self._status = "starting"
        self._thread = Thread(target=self._run, name="feishu-long-connection", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._enabled:
            return
        self._status = "stopping"
        loop = self._loop
        thread = self._thread
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread.is_alive():
            thread.join(timeout=10)
        self._thread = None
        self._loop = None
        self._status = "stopped"

    def _run(self) -> None:
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            import lark_oapi.ws.client as ws_client
            from lark_oapi.ws.client import Client
            from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
        except ImportError:
            logger.exception("lark_oapi is required for Feishu long connection")
            self._last_error = "lark_oapi is not installed"
            self._status = "error"
            return

        ws_client.loop = loop

        def on_card(event):
            from lark_oapi.event.callback.model.p2_card_action_trigger import (
                CallBackCard,
                CallBackToast,
                P2CardActionTriggerResponse,
            )
            raw = _card_event_to_payload(event)
            tasks = BackgroundTasks()
            try:
                result = self._event_service._handle_card_action(
                    raw, background_tasks=tasks
                )
            except Exception:
                logger.exception("Feishu long-connection card action failed")
                return P2CardActionTriggerResponse()

            # Card actions need their response synchronously. The previous
            # implementation scheduled all work and returned an empty response,
            # so Feishu never received the replacement edit/preview card.
            response = P2CardActionTriggerResponse()
            if result.card is not None:
                response.card = CallBackCard(
                    {"type": "raw", "data": result.card}
                )
            if result.toast_content:
                response.toast = CallBackToast(
                    {
                        "type": result.toast_type or "info",
                        "content": result.toast_content[:100],
                    }
                )

            async def dispatch_tasks() -> None:
                try:
                    await tasks()
                except Exception:
                    logger.exception("Feishu long-connection card background task failed")

            loop.create_task(dispatch_tasks())
            return response

        def on_message(event: Any) -> None:
            from project_lens.integrations.feishu.ws_ingress import _message_event_to_payload

            async def dispatch() -> None:
                tasks = BackgroundTasks()
                try:
                    raw = _message_event_to_payload(
                        event,
                        token=self._event_service._verifier._verification_token,
                    )
                    body = json.dumps(raw, ensure_ascii=False).encode("utf-8")
                    # In-process WS must sign like HTTP ingress when Encrypt Key
                    # is configured; otherwise handle_callback rejects with 401
                    # and the group sees silence.
                    timestamp, nonce, signature = signed_callback_auth(
                        body=body,
                        signing_secret=self._event_service._verifier._signing_secret,
                    )
                    self._event_service.handle_callback(
                        body=body,
                        timestamp=timestamp,
                        nonce=nonce,
                        signature=signature,
                        background_tasks=tasks,
                    )
                    await tasks()
                except Exception:
                    logger.exception("Feishu long-connection message handling failed")

            loop.create_task(dispatch())

        handler = (
            EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(on_message)
            .register_p2_card_action_trigger(on_card)
            .build()
        )
        try:
            client = Client(app_id=self._app_id, app_secret=self._app_secret, event_handler=handler)
            self._client = client
            loop.run_until_complete(client._connect())
            self._last_connected_at = datetime.now(timezone.utc)
            self._status = "connected"
            loop.create_task(client._ping_loop())
            loop.run_forever()
        except Exception as exc:
            logger.exception("Feishu long connection stopped")
            self._last_error = str(exc)[:500]
            self._status = "error"
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()


def _card_event_to_payload(event: Any) -> dict[str, object]:
    header = getattr(event, "header", None)
    data = getattr(event, "event", None)
    operator = getattr(data, "operator", None)
    action = getattr(data, "action", None)
    context = getattr(data, "context", None)
    return {
        "header": {
            "event_id": getattr(header, "event_id", "") or "",
            "tenant_key": getattr(header, "tenant_key", "") or "",
            "event_type": "card.action.trigger",
        },
        "event": {
            "operator": {
                "open_id": getattr(operator, "open_id", "") or "",
                "user_id": getattr(operator, "user_id", "") or "",
            },
            "action": {"value": getattr(action, "value", {}) or {}},
            "context": {
                "open_chat_id": getattr(context, "open_chat_id", "") or "",
                "chat_id": getattr(context, "open_chat_id", "") or "",
            },
        },
    }
