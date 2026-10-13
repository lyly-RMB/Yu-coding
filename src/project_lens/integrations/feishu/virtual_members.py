"""Test-only virtual actor switching for Feishu scenario replay."""
from __future__ import annotations

from dataclasses import dataclass
import json


@dataclass(frozen=True)
class VirtualMember:
    alias: str
    actor_id: str


class VirtualMemberController:
    def __init__(self, *, enabled: bool = False, operators: str = "", profiles: str = "") -> None:
        self.enabled = enabled
        self.operators = {x.strip() for x in operators.split(",") if x.strip()}
        data = {"product": "virtual-product", "developer": "virtual-developer", "qa": "virtual-qa", "manager": "virtual-manager", "ops": "virtual-ops"}
        if profiles.strip():
            data.update(json.loads(profiles))
        self.members = {str(k).casefold(): VirtualMember(str(k), str(v)) for k, v in data.items()}
        self.active: dict[tuple[str, str], VirtualMember] = {}

    def parse(self, text: str) -> tuple[str, str | None] | None:
        raw = text.strip()
        lowered = raw.casefold()
        prefix = "/pl-sim" if lowered.startswith("/pl-sim") else "/sim" if lowered.startswith("/sim") else None
        if prefix is None:
            return None
        parts = raw[len(prefix):].strip().split(maxsplit=2)
        if not parts:
            return ("help", None)
        if parts[0].casefold() == "as" and len(parts) >= 2:
            return ("as", parts[1].casefold())
        if parts[0].casefold() in {"off", "clear"}:
            return ("off", None)
        if parts[0].casefold() in {"list", "help"}:
            return (parts[0].casefold(), None)
        if parts[0].casefold() == "whoami":
            return ("whoami", None)
        if len(parts) < 2:
            return ("invalid", None)
        return (parts[0].casefold(), " ".join(parts[1:]).strip() or None)

    def handle(self, *, physical_id: str | None = None, chat_id: str, command: tuple[str, str | None] | None = None, operator_id: str | None = None, action: tuple[str, str | None] | None = None) -> str:
        physical_id = physical_id or operator_id or ""
        command = action or command or ("help", None)
        if not self.enabled:
            return "测试身份模拟未启用。"
        if physical_id not in self.operators:
            return "没有权限：当前真实账号不在测试身份白名单中。"
        action, value = command
        key = (physical_id, chat_id)
        if action == "list":
            return "可用身份：" + ", ".join(sorted(self.members))
        if action == "help":
            return "用法：/sim <product|developer|qa|manager|ops> <消息>；/sim off；/sim whoami"
        if action == "off":
            self.active.pop(key, None)
            return "已恢复真实身份。"
        if action == "whoami":
            member = self.active.get(key)
            return "当前身份：" + (member.alias if member else "真实飞书账号")
        member = self.members.get(value if action == "as" else action)
        if member is None or not value:
            return "未知身份或消息为空。"
        self.active[key] = member
        if action == "as":
            return f"已切换为测试身份：{member.alias}（{member.actor_id}）。"
        return member.actor_id

    def effective_actor(self, *, physical_id: str | None = None, chat_id: str, operator_id: str | None = None) -> str | None:
        physical_id = physical_id or operator_id or ""
        member = self.active.get((physical_id, chat_id))
        return member.actor_id if self.enabled and member else None

    def command(self, text: str) -> tuple[str, str | None] | None:
        return self.parse(text)

    def active_actor(self, *, operator_id: str, chat_id: str) -> str | None:
        return self.effective_actor(physical_id=operator_id, chat_id=chat_id)
