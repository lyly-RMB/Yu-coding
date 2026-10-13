"""Provider-neutral runtime types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, Sequence


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class FinishReason(StrEnum):
    STOP = "stop"
    MAX_ITERATIONS = "max_iterations"
    MAX_TOKENS = "max_tokens"
    STALLED = "stalled"
    CANCELED = "canceled"
    ERROR = "error"


class ToolCategory(StrEnum):
    READ = "read"
    WRITE = "write"
    AGENT = "agent"


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    category: ToolCategory
    required_permission: str

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(frozen=True)
class Message:
    role: Role
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class ModelResponse:
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    usage: dict[str, int] = field(default_factory=dict)


class ModelProvider(Protocol):
    """Chat/tools provider used by ToolRegistry-backed runtimes.

    Distinct from ``workflow.providers.ModelProvider``, which completes from a
    ``ContextPrompt`` for ProjectWorkflow harness runs.
    """

    async def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> ModelResponse: ...


@dataclass(frozen=True)
class ToolResult:
    tool_call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class RuntimeResult:
    messages: tuple[Message, ...]
    final_text: str | None
    finish_reason: FinishReason
    iterations: int
    total_tokens: int

