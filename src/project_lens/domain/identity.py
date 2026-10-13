"""Trusted actor identity — never accept client-claimed role/tenant as authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ActorSource = Literal["feishu_event", "feishu_virtual_test", "service_token", "test_fixture"]
ChatType = Literal["p2p", "group"]


@dataclass(frozen=True)
class ActorContext:
    """Immutable identity bound at Feishu ingress or a trusted service boundary.

    HTTP request bodies must not invent or elevate this object.
    """

    tenant_key: str
    actor_id: str
    chat_id: str
    chat_type: ChatType
    source: ActorSource
    authenticated: bool
    physical_actor_id: str | None = None

    def __post_init__(self) -> None:
        if not self.tenant_key.strip():
            raise ValueError("tenant_key is required")
        if not self.actor_id.strip():
            raise ValueError("actor_id is required")
        if not self.chat_id.strip():
            raise ValueError("chat_id is required")
        if self.chat_type not in {"p2p", "group"}:
            raise ValueError(f"unsupported chat_type: {self.chat_type}")
        if self.source not in {"feishu_event", "feishu_virtual_test", "service_token", "test_fixture"}:
            raise ValueError(f"unsupported actor source: {self.source}")
        if self.source == "feishu_virtual_test" and not (self.physical_actor_id or "").strip():
            raise ValueError("virtual Feishu actor requires a physical actor id")
