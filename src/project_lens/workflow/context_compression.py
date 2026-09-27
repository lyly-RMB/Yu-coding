"""Deterministic context compression for L1->L2 and L3 scratchpad trimming.

No LLM calls. Only overflow content is compacted into the existing summary.
Pinned IDs/paths are merged, never dropped for size alone within configured caps.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable
from uuid import UUID

from project_lens.context.retrieval.exact import parse_traceback
from project_lens.domain.conversation import (
    COMPRESSED_ANSWER_TOKEN_BUDGET,
    CompressedTurnRecord,
    ConversationSummary,
    ConversationTurn,
    PinnedIds,
    clip_answer_and_evidence_to_budget,
)
from project_lens.domain.models import ProjectAnswer
from project_lens.workflow.context_pack import TaskScratchpad

# Keep enough traceback for Engineering follow-ups after L1 slides out.
_PRIOR_TRACEBACK_MAX = 4_000

_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_COMMIT_RE = re.compile(r"\b[0-9a-fA-F]{7,40}\b")
_DOC_TOKEN_RE = re.compile(
    r"\b(?:docx?_[A-Za-z0-9_]+|doc_token[=:]([A-Za-z0-9_]+))\b",
    re.IGNORECASE,
)
_FILE_PATH_RE = re.compile(
    r"(?:(?:src|examples|tests)/[\w./\\-]+\.py|[\w./\\-]+\.py)\b",
    re.IGNORECASE,
)
_INCIDENT_RE = re.compile(r"\b(?:INC|incident)[-_]?([A-Za-z0-9]+)\b", re.IGNORECASE)
_TRACEBACK_SYMBOL_RE = re.compile(r"\bin\s+([A-Za-z_][\w]*)\s*$", re.MULTILINE)
_PROPOSAL_REF_RE = re.compile(
    r"\b(?:engineering_proposal|proposal|memory_proposal|memory):([0-9a-fA-F-]{36})\b",
    re.IGNORECASE,
)

_PIN_CAPS = {
    "run_ids": 40,
    "trace_ids": 40,
    "evidence_ids": 80,
    "claim_ids": 80,
    "proposal_ids": 40,
    "memory_ids": 40,
    "incident_ids": 40,
    "commit_shas": 40,
    "doc_tokens": 40,
    "file_paths": 60,
    "symbol_names": 40,
}


def merge_pinned_ids(*parts: PinnedIds | None) -> PinnedIds:
    active = [part for part in parts if part is not None]
    if not active:
        return PinnedIds()
    data: dict[str, tuple[str, ...]] = {
        field: ()
        for field in _PIN_CAPS
    }
    for part in active:
        dumped = part.model_dump()
        for field in _PIN_CAPS:
            data[field] = _dedupe_cap(
                data[field] + tuple(str(item) for item in dumped[field]),
                _PIN_CAPS[field],
            )
    return PinnedIds(**data)


def extract_pinned_from_text(text: str | None) -> PinnedIds:
    if not text:
        return PinnedIds()
    uuids = tuple(_UUID_RE.findall(text))
    uuid_chunks = {part for uuid in uuids for part in uuid.split("-")}
    commits = tuple(
        item
        for item in _COMMIT_RE.findall(text)
        if not _UUID_RE.fullmatch(item)
        and len(item) >= 7
        and item not in uuid_chunks
    )
    doc_tokens: list[str] = []
    for match in _DOC_TOKEN_RE.finditer(text):
        token = match.group(1) or match.group(0)
        doc_tokens.append(token)
    file_paths = tuple(_normalize_path(item) for item in _FILE_PATH_RE.findall(text))
    incidents = tuple(
        f"INC-{match.group(1)}" for match in _INCIDENT_RE.finditer(text)
    )
    symbols = tuple(_TRACEBACK_SYMBOL_RE.findall(text))
    proposals: list[str] = []
    memories: list[str] = []
    for match in _PROPOSAL_REF_RE.finditer(text):
        kind = match.group(0).split(":", 1)[0].lower()
        value = match.group(1)
        if "memory" in kind:
            memories.append(value)
        else:
            proposals.append(value)
    return PinnedIds(
        evidence_ids=uuids,
        commit_shas=_dedupe_cap(commits, _PIN_CAPS["commit_shas"]),
        doc_tokens=_dedupe_cap(doc_tokens, _PIN_CAPS["doc_tokens"]),
        file_paths=_dedupe_cap(file_paths, _PIN_CAPS["file_paths"]),
        incident_ids=_dedupe_cap(incidents, _PIN_CAPS["incident_ids"]),
        symbol_names=_dedupe_cap(symbols, _PIN_CAPS["symbol_names"]),
        proposal_ids=_dedupe_cap(proposals, _PIN_CAPS["proposal_ids"]),
        memory_ids=_dedupe_cap(memories, _PIN_CAPS["memory_ids"]),
    )


def extract_pinned_from_answer(answer: ProjectAnswer | None) -> PinnedIds:
    if answer is None:
        return PinnedIds()
    evidence_ids = tuple(str(item.id) for item in answer.evidence)
    claim_ids = tuple(str(claim.id) for claim in answer.claims)
    proposal_ids: list[str] = []
    file_paths: list[str] = []
    commits: list[str] = []
    incidents: list[str] = []
    doc_tokens: list[str] = []
    symbols: list[str] = []
    for item in answer.evidence:
        meta = item.metadata or {}
        if meta.get("commit") or meta.get("sha") or meta.get("commit_sha"):
            commits.append(
                str(meta.get("commit") or meta.get("sha") or meta.get("commit_sha"))
            )
        if meta.get("file") or meta.get("path"):
            file_paths.append(_normalize_path(str(meta.get("file") or meta.get("path"))))
        if meta.get("symbol") or meta.get("function"):
            symbols.append(str(meta.get("symbol") or meta.get("function")))
        if meta.get("incident_id"):
            incidents.append(str(meta["incident_id"]))
        if meta.get("doc_token"):
            doc_tokens.append(str(meta["doc_token"]))
        source_id = item.source.source_id
        if source_id.startswith("docx_") or source_id.startswith("doc_"):
            doc_tokens.append(source_id)
        text_pins = extract_pinned_from_text(item.content)
        commits.extend(text_pins.commit_shas)
        file_paths.extend(text_pins.file_paths)
        incidents.extend(text_pins.incident_ids)
        symbols.extend(text_pins.symbol_names)
    for action in answer.recommended_actions:
        proposal_ids.append(str(action.id))
        for path in action.arguments.get("affected_paths") or ():
            file_paths.append(_normalize_path(str(path)))
        text_pins = extract_pinned_from_text(str(action.arguments.get("diff_summary") or ""))
        file_paths.extend(text_pins.file_paths)
        text_pins = extract_pinned_from_text(action.description)
        file_paths.extend(text_pins.file_paths)
        commits.extend(text_pins.commit_shas)
    return merge_pinned_ids(
        PinnedIds(
            evidence_ids=evidence_ids,
            claim_ids=claim_ids,
            proposal_ids=tuple(proposal_ids),
            commit_shas=tuple(commits),
            file_paths=tuple(file_paths),
            incident_ids=tuple(incidents),
            doc_tokens=tuple(doc_tokens),
            symbol_names=tuple(symbols),
        ),
        extract_pinned_from_text(answer.business_summary),
        extract_pinned_from_text(answer.technical_summary),
    )


def extract_pinned_from_scratchpad(pad: TaskScratchpad | None) -> PinnedIds:
    if pad is None:
        return PinnedIds()
    proposal_ids: list[str] = []
    if pad.notes.get("engineering_action_id"):
        proposal_ids.append(pad.notes["engineering_action_id"])
    doc_tokens = [
        key.removeprefix("doc:")
        for key in pad.sync_state
        if key.startswith("doc:")
    ]
    return PinnedIds(
        file_paths=_dedupe_cap(
            tuple(_normalize_path(path) for path in pad.files_read + pad.files_changed),
            _PIN_CAPS["file_paths"],
        ),
        proposal_ids=_dedupe_cap(proposal_ids, _PIN_CAPS["proposal_ids"]),
        doc_tokens=_dedupe_cap(doc_tokens, _PIN_CAPS["doc_tokens"]),
    )


def extract_traceback_snippet(text: str | None) -> str | None:
    """Return a bounded traceback/exception snippet suitable for L2 active_topic."""

    if not text or not text.strip():
        return None
    frames, exception = parse_traceback(text)
    if not frames and not exception:
        return None
    return text.strip()[:_PRIOR_TRACEBACK_MAX]


def pin_prior_traceback(
    summary: ConversationSummary,
    *texts: str | None,
) -> ConversationSummary:
    """Promote the newest traceback into L2 so follow-ups survive L1 eviction."""

    snippet: str | None = None
    for text in texts:
        found = extract_traceback_snippet(text)
        if found:
            snippet = found
    if snippet is None:
        return summary
    topic = dict(summary.active_topic)
    topic["prior_traceback"] = snippet
    return summary.model_copy(
        update={
            "active_topic": topic,
            "updated_at": datetime.now(timezone.utc),
        }
    )


def compress_overflow_into_summary(
    summary: ConversationSummary,
    overflow: tuple[ConversationTurn, ...],
) -> ConversationSummary:
    """L1 overflow -> L2 incremental merge with compression_cycle bump.

    Each overflowed turn becomes a ``CompressedTurnRecord``:
    user question (uncapped by the 300-token budget) + answer summary + evidence ids
    where answer+evidence share ``COMPRESSED_ANSWER_TOKEN_BUDGET``.
    """

    if not overflow:
        return summary
    cycle = summary.compression_cycle + 1
    topic = dict(summary.active_topic)
    snippets: list[str] = []
    pinned = summary.pinned_ids
    run_ids: list[str] = []
    new_records: list[CompressedTurnRecord] = []
    for turn in overflow:
        user_question = turn.text
        clipped_answer, clipped_evidence = clip_answer_and_evidence_to_budget(
            turn.answer_summary or "",
            turn.evidence_ids,
            budget=COMPRESSED_ANSWER_TOKEN_BUDGET,
        )
        new_records.append(
            CompressedTurnRecord(
                user_question=user_question,
                answer_summary=clipped_answer,
                evidence_ids=clipped_evidence,
                occurred_at=turn.created_at,
                run_id=turn.run_id,
                compression_cycle=cycle,
            )
        )
        snippet = (turn.rewritten_question or turn.text)[:120]
        snippets.append(snippet)
        if turn.run_id is not None:
            run_ids.append(str(turn.run_id))
        pinned = merge_pinned_ids(
            pinned,
            extract_pinned_from_text(turn.text),
            extract_pinned_from_text(turn.rewritten_question),
            extract_pinned_from_text(turn.answer_summary),
            PinnedIds(
                run_ids=(str(turn.run_id),) if turn.run_id else (),
                evidence_ids=clipped_evidence,
            ),
        )
        # Newest overflow traceback wins so 「怎么修」 survives compress+restart.
        for candidate in (turn.text, turn.rewritten_question):
            found = extract_traceback_snippet(candidate)
            if found:
                topic["prior_traceback"] = found
    topic["compressed_turns"] = str(
        int(topic.get("compressed_turns") or "0") + len(overflow)
    )
    topic["last_compressed"] = " | ".join(snippets)[:280]
    topic["compression_cycle"] = str(cycle)
    topic["last_compressed_run_ids"] = ",".join(run_ids)[:300]
    intent = summary.session_intent
    if intent is None and snippets:
        intent = snippets[0][:200]
    return summary.model_copy(
        update={
            "active_topic": topic,
            "pinned_ids": pinned,
            "compression_cycle": cycle,
            # Sliding window: keep only this cycle's records (do not append forever).
            "compressed_turn_records": tuple(new_records),
            "session_intent": intent,
            "artifact_refs": _merge_artifact_refs(
                summary.artifact_refs,
                f"compression_cycle:{cycle}",
                *(f"run:{run_id}" for run_id in run_ids),
            ),
            "updated_at": datetime.now(timezone.utc),
        }
    )


def merge_answer_into_summary(
    summary: ConversationSummary,
    answer: ProjectAnswer,
) -> ConversationSummary:
    """Deterministic L2 update from a verified ProjectAnswer."""

    topic = dict(summary.active_topic)
    topic["skill"] = answer.skill
    topic["business_summary"] = answer.business_summary[:300]
    if answer.status:
        topic["status"] = answer.status
    claim_ids = tuple(
        dict.fromkeys(summary.verified_claim_ids + tuple(claim.id for claim in answer.claims))
    )[-20:]
    evidence_ids = tuple(
        dict.fromkeys(summary.evidence_ids + tuple(item.id for item in answer.evidence))
    )[-40:]
    unknowns = tuple(dict.fromkeys(summary.unknowns + answer.unknowns))[-20:]
    actions = tuple(
        dict.fromkeys(
            summary.next_actions + tuple(item.title for item in answer.recommended_actions)
        )
    )[:12]
    artifacts = list(summary.artifact_refs)
    for item in answer.evidence[:8]:
        artifacts.append(f"evidence:{item.id}")
    for action in answer.recommended_actions:
        artifacts.append(f"proposal:{action.id}")
        if action.tool_name == "engineering_proposal":
            artifacts.append(f"engineering_proposal:{action.id}")
            topic["engineering_can_apply"] = str(
                bool(action.arguments.get("can_apply", False))
            )
    answer_pins = extract_pinned_from_answer(answer)
    pinned = merge_pinned_ids(
        summary.pinned_ids,
        answer_pins,
        PinnedIds(
            evidence_ids=tuple(str(item) for item in evidence_ids),
            claim_ids=tuple(str(item) for item in claim_ids),
        ),
    )
    intent = summary.session_intent or answer.business_summary[:200]
    return summary.model_copy(
        update={
            "project": answer.project,
            "active_skill": answer.skill,
            "active_topic": topic,
            "verified_claim_ids": claim_ids,
            "evidence_ids": evidence_ids,
            "unknowns": unknowns,
            "next_actions": actions,
            "artifact_refs": tuple(dict.fromkeys(artifacts))[-40:],
            "pinned_ids": pinned,
            "session_intent": intent,
            "last_assistant_summary": (
                f"{answer.skill}: {answer.business_summary}"
            )[:500],
            "updated_at": datetime.now(timezone.utc),
        }
    )


def compress_scratchpad(
    pad: TaskScratchpad | None,
    *,
    max_tool_events: int = 20,
) -> TaskScratchpad | None:
    """Trim L3 raw tool audit noise while keeping pinned file/proposal/sync state."""

    if pad is None:
        return None
    notes = dict(pad.notes)
    overflow = pad.tool_audit_events[:-max_tool_events]
    kept = pad.tool_audit_events[-max_tool_events:]
    if overflow:
        notes["compressed_tool_events"] = str(
            int(notes.get("compressed_tool_events") or "0") + len(overflow)
        )
        notes["last_compressed_tools"] = ",".join(overflow[-5:])[:280]
    # Paths and sync keys stay verbatim; allow_apply forced off.
    return pad.model_copy(
        update={
            "tool_audit_events": kept,
            "notes": notes,
            "allow_apply": False,
            "files_read": _dedupe_cap(pad.files_read, _PIN_CAPS["file_paths"]),
            "files_changed": _dedupe_cap(pad.files_changed, _PIN_CAPS["file_paths"]),
        }
    )


def pinned_probe_values(pinned: PinnedIds) -> set[str]:
    """Flat set used by probe tests to assert retention."""

    values: set[str] = set()
    for field, items in pinned.model_dump().items():
        for item in items:
            values.add(str(item))
            values.add(f"{field}:{item}")
    return values


def _merge_artifact_refs(
    existing: Iterable[str],
    *extra: str,
) -> tuple[str, ...]:
    return tuple(dict.fromkeys([*existing, *extra]))[-40:]


def _dedupe_cap(values: Iterable[str], cap: int) -> tuple[str, ...]:
    ordered = tuple(dict.fromkeys(item for item in values if item))
    if len(ordered) <= cap:
        return ordered
    return ordered[-cap:]


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def as_uuid_tuple(values: Iterable[str]) -> tuple[UUID, ...]:
    result: list[UUID] = []
    for item in values:
        try:
            result.append(UUID(item))
        except ValueError:
            continue
    return tuple(result)
