"""Request-level context budgeting for the ProjectLens/Hermes boundary.

This module deliberately stays outside Hermes internals.  It estimates the
request before the provider call, applies deterministic pruning when the
business budget is reached, and records a compact audit report after the tool
loop completes.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import sha256
import json
import math
from typing import Any, Iterable, Literal


GovernorAction = Literal[
    "disabled",
    "normal",
    "deduplicate",
    "summarize",
    "prune",
    "rotate",
    "blocked",
]

CONTEXT_RENDERER_VERSION = "context_governor.v1"


@dataclass(frozen=True)
class ContextSection:
    """Stable, inspectable partition of the materialized context."""

    layer: str
    title: str
    content: str


@dataclass(frozen=True)
class ContextSnapshot:
    """Immutable context materialization shared by preview, send, and audit."""

    snapshot_id: str
    context_hash: str
    renderer_version: str
    text: str
    sections: tuple[ContextSection, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "context_hash": self.context_hash,
            "renderer_version": self.renderer_version,
            "section_order": [item.title for item in self.sections],
            "section_count": len(self.sections),
        }


@dataclass(frozen=True)
class ContextGovernorConfig:
    """Business context budget, intentionally lower than provider maximums."""

    enabled: bool = True
    model_context_limit_tokens: int = 128_000
    soft_limit_ratio: float = 0.45
    summarize_ratio: float = 0.60
    prune_ratio: float = 0.75
    rotate_ratio: float = 0.85
    hard_limit_ratio: float = 0.95
    output_reserve_tokens: int = 12_000
    tool_schema_tokens: int = 2_500
    provider_overhead_tokens: int = 1_000
    chars_per_token: float = 4.0
    max_recovery_retries: int = 2

    def __post_init__(self) -> None:
        if self.model_context_limit_tokens < 1_000:
            raise ValueError("model_context_limit_tokens must be at least 1000")
        ratios = (
            self.soft_limit_ratio,
            self.summarize_ratio,
            self.prune_ratio,
            self.rotate_ratio,
            self.hard_limit_ratio,
        )
        if any(ratio <= 0 or ratio >= 1 for ratio in ratios):
            raise ValueError("context ratios must be between 0 and 1")
        if list(ratios) != sorted(ratios):
            raise ValueError("context ratios must be monotonic")
        if self.output_reserve_tokens < 0 or self.tool_schema_tokens < 0:
            raise ValueError("reserved token counts must be non-negative")
        if self.provider_overhead_tokens < 0 or self.max_recovery_retries < 0:
            raise ValueError("overhead and retry counts must be non-negative")
        if self.chars_per_token <= 0:
            raise ValueError("chars_per_token must be positive")


@dataclass(frozen=True)
class TokenBudgetReport:
    """Serializable request budget and compression audit."""

    context_limit_tokens: int
    history_tokens: int
    system_prompt_tokens: int
    question_tokens: int
    tool_schema_tokens: int
    current_tool_result_tokens: int
    output_reserve_tokens: int
    provider_overhead_tokens: int
    estimated_input_tokens: int
    post_tool_loop_tokens: int | None
    utilization_ratio: float
    post_utilization_ratio: float | None
    action: GovernorAction
    original_context_tokens: int
    final_context_tokens: int
    pruned: bool = False
    rotation_recommended: bool = False
    hard_blocked: bool = False
    removed_items: tuple[str, ...] = ()
    snapshot_id: str = ""
    context_hash: str = ""
    renderer_version: str = CONTEXT_RENDERER_VERSION
    section_order: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "context_limit_tokens": self.context_limit_tokens,
            "history_tokens": self.history_tokens,
            "system_prompt_tokens": self.system_prompt_tokens,
            "question_tokens": self.question_tokens,
            "tool_schema_tokens": self.tool_schema_tokens,
            "current_tool_result_tokens": self.current_tool_result_tokens,
            "output_reserve_tokens": self.output_reserve_tokens,
            "provider_overhead_tokens": self.provider_overhead_tokens,
            "estimated_input_tokens": self.estimated_input_tokens,
            "post_tool_loop_tokens": self.post_tool_loop_tokens,
            "utilization_ratio": round(self.utilization_ratio, 6),
            "post_utilization_ratio": (
                round(self.post_utilization_ratio, 6)
                if self.post_utilization_ratio is not None
                else None
            ),
            "action": self.action,
            "original_context_tokens": self.original_context_tokens,
            "final_context_tokens": self.final_context_tokens,
            "pruned": self.pruned,
            "rotation_recommended": self.rotation_recommended,
            "hard_blocked": self.hard_blocked,
            "removed_items": list(self.removed_items),
            "snapshot_id": self.snapshot_id,
            "context_hash": self.context_hash,
            "renderer_version": self.renderer_version,
            "section_order": list(self.section_order),
        }


@dataclass(frozen=True)
class PreparedContext:
    """Prepared context plus the immutable budget decision for one request."""

    text: str
    report: TokenBudgetReport
    snapshot: ContextSnapshot


@dataclass
class ContextGovernor:
    """Deterministic context governor used immediately before Hermes calls."""

    config: ContextGovernorConfig = field(default_factory=ContextGovernorConfig)

    def estimate_tokens(self, value: Any) -> int:
        """Estimate tokens without adding a tokenizer dependency to the app."""

        if value is None:
            return 0
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
        return max(0, math.ceil(len(value) / self.config.chars_per_token))

    def prepare(
        self,
        *,
        context_text: str,
        question: str,
        system_prompt: str,
        tool_schema_tokens: int | None = None,
        force_prune: bool = False,
    ) -> PreparedContext:
        """Prune context before sending it; never return an over-budget payload."""

        original = context_text or ""
        schema_tokens = (
            self.config.tool_schema_tokens
            if tool_schema_tokens is None
            else max(0, int(tool_schema_tokens))
        )
        original_context_tokens = self.estimate_tokens(original)
        initial = self._estimate_request(
            context_text=original,
            question=question,
            system_prompt=system_prompt,
            tool_schema_tokens=schema_tokens,
            tool_result_tokens=0,
        )
        if not self.config.enabled:
            snapshot = _build_snapshot(
                original,
                question=question,
                system_prompt=system_prompt,
                tool_schema_tokens=schema_tokens,
            )
            return PreparedContext(
                text=original,
                report=self._report(
                    context_text=original,
                    original_context_tokens=original_context_tokens,
                    estimate=initial,
                    action="disabled",
                    system_prompt_tokens=self.estimate_tokens(system_prompt),
                    question_tokens=self.estimate_tokens(question),
                    tool_schema_tokens=schema_tokens,
                    snapshot=snapshot,
                ),
                snapshot=snapshot,
            )

        ratio = initial / self.config.model_context_limit_tokens
        action: GovernorAction = "normal"
        prepared = original
        removed: list[str] = []
        if force_prune:
            prepared, recovery_dedup = _deduplicate_lines(prepared)
            prepared, recovery_prune = _compact_context_lines(
                prepared,
                aggressive=True,
            )
            removed.extend(recovery_dedup)
            removed.extend(recovery_prune)
            action = "prune"
        if ratio >= self.config.soft_limit_ratio:
            prepared, dedupe_removed = _deduplicate_lines(prepared)
            removed.extend(dedupe_removed)
            action = "deduplicate"
        if ratio >= self.config.summarize_ratio:
            prepared, summary_removed = _compact_context_lines(
                prepared,
                aggressive=False,
            )
            removed.extend(summary_removed)
            action = "summarize"
        if ratio >= self.config.prune_ratio:
            prepared, prune_removed = _compact_context_lines(
                prepared,
                aggressive=True,
            )
            removed.extend(prune_removed)
            action = "prune"

        estimate = self._estimate_request(
            context_text=prepared,
            question=question,
            system_prompt=system_prompt,
            tool_schema_tokens=schema_tokens,
            tool_result_tokens=0,
        )
        hard_limit = int(
            self.config.model_context_limit_tokens * self.config.hard_limit_ratio
        )
        if estimate > hard_limit:
            available = max(
                0,
                hard_limit
                - self.estimate_tokens(system_prompt)
                - self.estimate_tokens(question)
                - schema_tokens
                - self.config.output_reserve_tokens
                - self.config.provider_overhead_tokens,
            )
            prepared, clipped = _clip_context_to_tokens(
                prepared,
                max_tokens=available,
                chars_per_token=self.config.chars_per_token,
            )
            removed.extend(clipped)
            estimate = self._estimate_request(
                context_text=prepared,
                question=question,
                system_prompt=system_prompt,
                tool_schema_tokens=schema_tokens,
                tool_result_tokens=0,
            )
            action = "rotate" if estimate <= hard_limit else "blocked"

        final_ratio = estimate / self.config.model_context_limit_tokens
        blocked = estimate > hard_limit
        rotation = ratio >= self.config.rotate_ratio or final_ratio >= self.config.rotate_ratio
        if blocked:
            action = "blocked"
        elif rotation and action == "normal":
            action = "rotate"
        snapshot = _build_snapshot(
            prepared,
            question=question,
            system_prompt=system_prompt,
            tool_schema_tokens=schema_tokens,
        )
        report = self._report(
            context_text=prepared,
            original_context_tokens=original_context_tokens,
            estimate=estimate,
            action=action,
            removed_items=tuple(dict.fromkeys(removed)),
            rotation_recommended=rotation,
            hard_blocked=blocked,
            system_prompt_tokens=self.estimate_tokens(system_prompt),
            question_tokens=self.estimate_tokens(question),
            tool_schema_tokens=schema_tokens,
            snapshot=snapshot,
        )
        return PreparedContext(text=prepared, report=report, snapshot=snapshot)

    def observe_tool_loop(
        self,
        prepared: PreparedContext,
        *,
        messages: Iterable[Any],
    ) -> TokenBudgetReport:
        """Add post-call transcript/tool-result cost to the preflight report."""

        tool_result_tokens = self.estimate_tokens(
            [
                message
                for message in messages
                if isinstance(message, dict) and message.get("role") == "tool"
            ]
        )
        post_tokens = prepared.report.estimated_input_tokens + tool_result_tokens
        post_ratio = post_tokens / self.config.model_context_limit_tokens
        return replace(
            prepared.report,
            current_tool_result_tokens=tool_result_tokens,
            post_tool_loop_tokens=post_tokens,
            post_utilization_ratio=post_ratio,
        )

    def _estimate_request(
        self,
        *,
        context_text: str,
        question: str,
        system_prompt: str,
        tool_schema_tokens: int,
        tool_result_tokens: int,
    ) -> int:
        return (
            self.estimate_tokens(context_text)
            + self.estimate_tokens(system_prompt)
            + self.estimate_tokens(question)
            + tool_schema_tokens
            + tool_result_tokens
            + self.config.output_reserve_tokens
            + self.config.provider_overhead_tokens
        )

    def _report(
        self,
        *,
        context_text: str,
        original_context_tokens: int,
        estimate: int,
        action: GovernorAction,
        removed_items: tuple[str, ...] = (),
        rotation_recommended: bool = False,
        hard_blocked: bool = False,
        system_prompt_tokens: int = 0,
        question_tokens: int = 0,
        tool_schema_tokens: int | None = None,
        snapshot: ContextSnapshot | None = None,
    ) -> TokenBudgetReport:
        resolved_snapshot = snapshot or _build_snapshot(
            context_text,
            question="",
            system_prompt="",
            tool_schema_tokens=(
                self.config.tool_schema_tokens
                if tool_schema_tokens is None
                else tool_schema_tokens
            ),
        )
        return TokenBudgetReport(
            context_limit_tokens=self.config.model_context_limit_tokens,
            history_tokens=self.estimate_tokens(context_text),
            system_prompt_tokens=system_prompt_tokens,
            question_tokens=question_tokens,
            tool_schema_tokens=(
                self.config.tool_schema_tokens
                if tool_schema_tokens is None
                else tool_schema_tokens
            ),
            current_tool_result_tokens=0,
            output_reserve_tokens=self.config.output_reserve_tokens,
            provider_overhead_tokens=self.config.provider_overhead_tokens,
            estimated_input_tokens=estimate,
            post_tool_loop_tokens=None,
            utilization_ratio=estimate / self.config.model_context_limit_tokens,
            post_utilization_ratio=None,
            action=action,
            original_context_tokens=original_context_tokens,
            final_context_tokens=self.estimate_tokens(context_text),
            pruned=bool(removed_items) or action in {"summarize", "prune", "rotate", "blocked"},
            rotation_recommended=rotation_recommended,
            hard_blocked=hard_blocked,
            removed_items=removed_items,
            snapshot_id=resolved_snapshot.snapshot_id,
            context_hash=resolved_snapshot.context_hash,
            renderer_version=resolved_snapshot.renderer_version,
            section_order=tuple(item.title for item in resolved_snapshot.sections),
        )


def _build_snapshot(
    text: str,
    *,
    question: str,
    system_prompt: str,
    tool_schema_tokens: int,
) -> ContextSnapshot:
    context_hash = sha256(text.encode("utf-8")).hexdigest()
    identity = json.dumps(
        {
            "context": text,
            "question": question,
            "system_prompt": system_prompt,
            "tool_schema_tokens": tool_schema_tokens,
            "renderer_version": CONTEXT_RENDERER_VERSION,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    snapshot_id = sha256(identity.encode("utf-8")).hexdigest()[:24]
    return ContextSnapshot(
        snapshot_id=snapshot_id,
        context_hash=context_hash,
        renderer_version=CONTEXT_RENDERER_VERSION,
        text=text,
        sections=_partition_context(text),
    )


def _partition_context(text: str) -> tuple[ContextSection, ...]:
    """Parse stable headings without changing the materialized text."""

    lines = text.splitlines()
    if not lines:
        return ()
    sections: list[ContextSection] = []
    current_title = "context"
    current_lines: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if current_lines:
                sections.append(
                    ContextSection(
                        layer=_section_layer(current_title),
                        title=current_title,
                        content="\n".join(current_lines).strip(),
                    )
                )
            current_title = line[3:].strip() or "context"
            current_lines = []
        else:
            current_lines.append(line)
    if current_lines:
        sections.append(
            ContextSection(
                layer=_section_layer(current_title),
                title=current_title,
                content="\n".join(current_lines).strip(),
            )
        )
    return tuple(sections)


def _section_layer(title: str) -> str:
    normalized = title.casefold()
    if "intent" in normalized or "state" in normalized:
        return "L1"
    if "fact" in normalized or "decision" in normalized or "question" in normalized:
        return "L2"
    if "evidence" in normalized or "artifact" in normalized:
        return "L3"
    if "next" in normalized or "compression" in normalized:
        return "L4"
    return "L5"


def _deduplicate_lines(text: str) -> tuple[str, list[str]]:
    lines = text.splitlines()
    seen: set[str] = set()
    kept: list[str] = []
    removed: list[str] = []
    for line in lines:
        normalized = " ".join(line.split())
        if normalized and normalized in seen:
            removed.append("duplicate_line")
            continue
        if normalized:
            seen.add(normalized)
        kept.append(line)
    return "\n".join(kept), removed


def _compact_context_lines(
    text: str,
    *,
    aggressive: bool,
) -> tuple[str, list[str]]:
    lines = text.splitlines()
    if not lines:
        return text, []
    anchors: list[str] = []
    references: list[str] = []
    recent: list[str] = []
    headings: list[str] = []
    structured: list[str] = []
    current_section = ""
    structured_sections = {
        "session intent",
        "current state",
        "confirmed facts",
        "decisions",
        "open questions",
        "blocked items",
        "evidence references",
        "files and artifacts",
        "next steps",
        "compression metadata",
    }
    section_counts: dict[str, int] = {}
    section_limit = 4 if aggressive else 10
    for line in lines:
        if line.startswith("## "):
            current_section = line[3:].strip().casefold()
            headings.append(line)
            continue
        lowered = line.casefold()
        if current_section in structured_sections:
            count = section_counts.get(current_section, 0)
            if count < section_limit:
                structured.append(line)
                section_counts[current_section] = count + 1
            continue
        if any(
            marker in lowered
            for marker in (
                "runtime_access=",
                "session_id=",
                "active_skill=",
                "session_intent=",
                "session_unknowns=",
                "session_next_actions=",
                "last_assistant_summary=",
                "task_state=",
                "approved_project_memories:",
                "compression_",
            )
        ):
            anchors.append(line)
        elif any(
            marker in lowered
            for marker in (
                "memory_id=",
                "evidence_id=",
                "revision=",
                "content_hash=",
                "source_key=",
                "file_path=",
            )
        ):
            references.append(line)
        elif "recent_turn[" in lowered:
            recent.append(line)

    if aggressive:
        selected = headings + structured + anchors[:40] + references[:40] + recent[-8:]
    else:
        selected = headings + structured + anchors[:80] + references[:80] + recent[-16:]
    selected = list(dict.fromkeys(selected))
    if len(selected) >= len(lines):
        return text, []
    if not selected:
        selected = lines[:4] + lines[-4:]
    return "\n".join(selected), ["old_context_lines", "full_tool_result_lines"]


def _clip_context_to_tokens(
    text: str,
    *,
    max_tokens: int,
    chars_per_token: float,
) -> tuple[str, list[str]]:
    max_chars = max(0, int(max_tokens * chars_per_token))
    if len(text) <= max_chars:
        return text, []
    if max_chars <= 32:
        return "", ["context_payload"]
    clipped = text[: max_chars - 1].rstrip() + "…"
    return clipped, ["context_payload_tail"]
