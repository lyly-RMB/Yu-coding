"""Multi-turn conversation session contracts for the Agent Harness."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import StrEnum
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from project_lens.domain.models import ProjectRef, utc_now
from project_lens.domain.session_state import ProjectSessionState

DEFAULT_RECENT_TURN_LIMIT = 12
# A turn-count ceiling is only a soft boundary; large answers/evidence can trigger
# compression earlier so the session does not accumulate a few oversized turns.
DEFAULT_RECENT_TURN_TOKEN_BUDGET = 6_000
MIN_RECENT_TURNS_AFTER_COMPRESSION = 6
# Sliding window: compress the oldest batch first, then keep the newest context.
COMPRESS_TURN_START_1BASED = 1
COMPRESS_TURN_END_1BASED = 4
# Answer summary + evidence ids share this budget; user question is excluded.
COMPRESSED_ANSWER_TOKEN_BUDGET = 300
DEFAULT_SESSION_TTL = timedelta(days=7)
CITATION_SHORT_TITLE_MAX = 120


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ConversationTurn(FrozenModel):
    run_id: UUID | None = None
    user_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=20_000)
    rewritten_question: str | None = Field(default=None, max_length=20_000)
    # Populated at record time so sliding-window compression can keep per-turn answer+evidence.
    answer_summary: str | None = Field(default=None, max_length=4_000)
    evidence_ids: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)


class CompressedTurnRecord(FrozenModel):
    """One L1 turn compacted into L2 (user question + budgeted answer/evidence)."""

    user_question: str = Field(min_length=1, max_length=20_000)
    answer_summary: str = Field(default="", max_length=4_000)
    evidence_ids: tuple[str, ...] = ()
    occurred_at: datetime = Field(default_factory=utc_now)
    run_id: UUID | None = None
    # Which compression cycle produced this record (default pool keeps only the latest).
    compression_cycle: int = Field(default=0, ge=0)


class DateWindow(FrozenModel):
    """Optional session-level date filter. None / unset means whole pool (S07)."""

    start: datetime | None = None
    end: datetime | None = None

    @model_validator(mode="after")
    def _require_bound(self) -> DateWindow:
        if self.start is None and self.end is None:
            raise ValueError("DateWindow requires at least one of start/end")
        if (
            self.start is not None
            and self.end is not None
            and self.start.astimezone(timezone.utc) > self.end.astimezone(timezone.utc)
        ):
            raise ValueError("DateWindow.start must not be later than end")
        return self


def format_date_window_label(window: DateWindow | None) -> str:
    """Human-readable date window for Feishu cards."""

    if window is None:
        return "未开窗（整池默认带入，不做剔除非今日）"
    start = (
        window.start.astimezone(timezone.utc).date().isoformat()
        if window.start is not None
        else "…"
    )
    end = (
        window.end.astimezone(timezone.utc).date().isoformat()
        if window.end is not None
        else "…"
    )
    return f"{start} → {end}"


class DefaultContextKind(StrEnum):
    SUMMARY = "summary"
    RECENT_TURN = "recent_turn"


class DefaultContextItem(FrozenModel):
    """One default pending-context row for preview/assembly (S02 enumerate → S03 consume)."""

    kind: DefaultContextKind
    occurred_at: datetime
    label: str = Field(min_length=1, max_length=500)
    run_id: UUID | None = None
    evidence_ids: tuple[str, ...] = ()
    user_question: str | None = Field(default=None, max_length=20_000)
    answer_summary: str | None = Field(default=None, max_length=4_000)
    actor_id: str | None = Field(default=None, max_length=100)


class PendingItemMark(StrEnum):
    """Card mark: default pool vs fine-selected citation."""

    DEFAULT = "default"
    CITATION = "citation"


class PendingSendItem(FrozenModel):
    """One row in the preview pending-send set (must have occurred_at)."""

    item_id: str = Field(min_length=1, max_length=200)
    mark: PendingItemMark
    occurred_at: datetime
    label: str = Field(min_length=1, max_length=500)
    source_kind: str = Field(min_length=1, max_length=40)
    user_question: str | None = Field(default=None, max_length=20_000)
    answer_summary: str | None = Field(default=None, max_length=4_000)
    evidence_ids: tuple[str, ...] = ()
    run_id: UUID | None = None
    citation_id: str | None = Field(default=None, max_length=64)
    short_title: str | None = Field(default=None, max_length=CITATION_SHORT_TITLE_MAX)
    actor_id: str | None = Field(default=None, max_length=100)


class PendingSendState(FrozenModel):
    """Preview == send: question + time-sorted items + token budget."""

    question: str = Field(default="", max_length=20_000)
    question_for_run: str = Field(default="", max_length=20_000)
    items: tuple[PendingSendItem, ...] = ()
    token_estimate: int = Field(default=0, ge=0)
    refreshed_at: datetime = Field(default_factory=utc_now)


class CitationSourceKind(StrEnum):
    """Where a citation ledger entry came from."""

    TURN = "turn"
    GROUP_MESSAGE = "group_message"
    HISTORY = "history"


class CitationEntry(FrozenModel):
    """Pinned retrieval handle written before compression / fine-select.

    Default context carries only citation_id + short_title + occurred_at.
    Full body lives in body_snapshot and/or external keys (run_id / message_id).
    """

    citation_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=64)
    source_kind: CitationSourceKind
    occurred_at: datetime = Field(default_factory=utc_now)
    short_title: str = Field(min_length=1, max_length=CITATION_SHORT_TITLE_MAX)
    run_id: UUID | None = None
    trace_id: str | None = Field(default=None, max_length=200)
    message_id: str | None = Field(default=None, max_length=200)
    evidence_ids: tuple[str, ...] = ()
    file_refs: tuple[str, ...] = ()
    body_snapshot: str | None = Field(default=None, max_length=20_000)

    @model_validator(mode="after")
    def _require_retrievable_body(self) -> CitationEntry:
        if self.body_snapshot:
            return self
        if self.run_id is not None or self.trace_id or self.message_id:
            return self
        raise ValueError(
            "citation must keep a body_snapshot or an external retrieval key "
            "(run_id / trace_id / message_id)"
        )

    def metadata_line(self) -> str:
        """Assemble one context line without body text."""

        stamp = self.occurred_at.astimezone(timezone.utc).isoformat()
        return f"citation_id={self.citation_id} time={stamp} title={self.short_title}"


class PinnedIds(FrozenModel):
    """IDs and paths that must survive L1->L2 compression."""

    run_ids: tuple[str, ...] = ()
    trace_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    claim_ids: tuple[str, ...] = ()
    proposal_ids: tuple[str, ...] = ()
    memory_ids: tuple[str, ...] = ()
    incident_ids: tuple[str, ...] = ()
    commit_shas: tuple[str, ...] = ()
    doc_tokens: tuple[str, ...] = ()
    file_paths: tuple[str, ...] = ()
    symbol_names: tuple[str, ...] = ()


class ConversationSummary(FrozenModel):
    """L2 rolling session summary. Runtime context only — never ProjectMemory."""

    project: ProjectRef
    active_skill: str | None = None
    active_topic: dict[str, str] = Field(default_factory=dict)
    verified_claim_ids: tuple[UUID, ...] = ()
    evidence_ids: tuple[UUID, ...] = ()
    unknowns: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    pinned_ids: PinnedIds = Field(default_factory=PinnedIds)
    compression_cycle: int = 0
    # Latest compression cycle only (sliding window). Older cycles are dropped so
    # the default pending pool cannot grow without bound; citations still retain history.
    compressed_turn_records: tuple[CompressedTurnRecord, ...] = ()
    session_intent: str | None = Field(default=None, max_length=500)
    last_assistant_summary: str | None = Field(default=None, max_length=500)
    updated_at: datetime = Field(default_factory=utc_now)


class HistoryCandidate(FrozenModel):
    """One row on the「选择更多历史」card (session-side or group message)."""

    candidate_id: str = Field(min_length=1, max_length=200)
    citation_id: str | None = Field(default=None, max_length=64)
    run_id: UUID | None = None
    message_id: str | None = Field(default=None, max_length=200)
    occurred_at: datetime
    short_title: str = Field(min_length=1, max_length=CITATION_SHORT_TITLE_MAX)
    source_kind: str = Field(min_length=1, max_length=40)
    already_in_pending: bool = False
    # Fine-select snapshot for group_message (not shown on card by default).
    body_text: str | None = Field(default=None, max_length=20_000)
    user_question: str | None = Field(default=None, max_length=20_000)
    answer_summary: str | None = Field(default=None, max_length=4_000)

    @model_validator(mode="after")
    def _require_select_key(self) -> HistoryCandidate:
        if self.citation_id or self.message_id:
            return self
        raise ValueError("HistoryCandidate needs citation_id and/or message_id")


class GroupHistoryListResult(FrozenModel):
    """Paginated group-chat candidates plus availability for the more-history card."""

    candidates: tuple[HistoryCandidate, ...] = ()
    available: bool = True
    unavailable_reason: str | None = Field(default=None, max_length=500)
    has_more: bool = False
    next_page_token: str | None = Field(default=None, max_length=2000)


# Page size for Feishu「更多历史」lists (action-row button budget).
HISTORY_CANDIDATE_PAGE_SIZE = 5


class SessionBranchInfo(FrozenModel):
    """One sibling line under the same Feishu binding (for card switch UI)."""

    session_id: UUID
    branch_name: str = Field(min_length=1, max_length=80)
    parent_session_id: UUID | None = None
    write_stopped: bool = False
    is_active: bool = False


class ConversationSession(FrozenModel):
    """Harness session spanning Feishu multi-turn collaboration."""

    session_id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(min_length=1, max_length=100)
    chat_id: str = Field(min_length=1, max_length=200)
    user_id: str = Field(min_length=1, max_length=100)
    project: ProjectRef
    last_run_id: UUID | None = None
    recent_turns: tuple[ConversationTurn, ...] = ()
    citations: tuple[CitationEntry, ...] = ()
    summary: ConversationSummary
    # Preview == send (S03). None = legacy path has not refreshed a pending set yet.
    pending_send: PendingSendState | None = None
    # Optional coarse date filter (S07 UI); S04「更多历史」reuses when set.
    date_window: DateWindow | None = None
    # Fork tree (S06): root has parent=None; stopped lines refuse further writes.
    parent_session_id: UUID | None = None
    branch_name: str = Field(default="A", min_length=1, max_length=80)
    write_stopped: bool = False
    session_state: ProjectSessionState | None = None
    # L3 scratchpad reserved for later Engineering/Ops process state.
    task_scratchpad: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc) + DEFAULT_SESSION_TTL
    )

    @property
    def id(self) -> UUID:
        return self.session_id

    @property
    def project_ref(self) -> ProjectRef:
        return self.project

    def to_branch_info(self, *, is_active: bool) -> SessionBranchInfo:
        return SessionBranchInfo(
            session_id=self.session_id,
            branch_name=self.branch_name,
            parent_session_id=self.parent_session_id,
            write_stopped=self.write_stopped,
            is_active=is_active,
        )


ConversationSession.model_rebuild()


def empty_summary(project: ProjectRef) -> ConversationSummary:
    return ConversationSummary(project=project)


def make_short_title(text: str, *, max_len: int = CITATION_SHORT_TITLE_MAX) -> str:
    """Collapse whitespace and clip to a citation short title."""

    collapsed = " ".join(text.split())
    if not collapsed:
        return "untitled"
    if len(collapsed) <= max_len:
        return collapsed
    return collapsed[: max(1, max_len - 1)].rstrip() + "…"


def format_citation_ledger_for_context(
    citations: tuple[CitationEntry, ...] | list[CitationEntry],
) -> str:
    """Default context fragment: metadata only, never body_snapshot."""

    if not citations:
        return "citation_ledger=(empty)"
    lines = ["citation_ledger:"]
    lines.extend(item.metadata_line() for item in citations)
    return "\n".join(lines)


def estimate_text_tokens(text: str) -> int:
    """Cheap token estimate aligned with workflow providers (≈4 chars / token)."""

    stripped = text.strip()
    if not stripped:
        return 0
    return max(1, (len(stripped) + 3) // 4)


def clip_text_to_token_budget(text: str, budget: int) -> str:
    """Clip text so estimate_text_tokens(result) <= budget (budget<=0 → empty)."""

    if budget <= 0 or not text:
        return ""
    if estimate_text_tokens(text) <= budget:
        return text
    # Binary-search character length; estimate is monotonic in len for this helper.
    lo, hi = 0, len(text)
    best = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = text[:mid].rstrip()
        if estimate_text_tokens(candidate) <= budget:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    if best and len(best) < len(text):
        return best.rstrip() + "…"
    return best


def clip_answer_and_evidence_to_budget(
    answer_summary: str,
    evidence_ids: tuple[str, ...] | list[str],
    *,
    budget: int = COMPRESSED_ANSWER_TOKEN_BUDGET,
) -> tuple[str, tuple[str, ...]]:
    """Prefer keeping evidence ids; clip answer summary to remaining token budget."""

    ids = tuple(str(item) for item in evidence_ids if str(item).strip())
    if budget <= 0:
        return "", ()
    kept_ids: list[str] = []
    used = 0
    for item in ids:
        cost = estimate_text_tokens(item if not kept_ids else f",{item}")
        if used + cost > budget:
            break
        kept_ids.append(item)
        used += cost
    remaining = budget - used
    clipped = clip_text_to_token_budget(answer_summary or "", remaining)
    return clipped, tuple(kept_ids)


def split_recent_turns_for_compression(
    recent: tuple[ConversationTurn, ...],
    *,
    recent_turn_limit: int,
    recent_turn_token_budget: int | None = DEFAULT_RECENT_TURN_TOKEN_BUDGET,
    compress_start_1based: int = COMPRESS_TURN_START_1BASED,
    compress_end_1based: int = COMPRESS_TURN_END_1BASED,
) -> tuple[tuple[ConversationTurn, ...], tuple[ConversationTurn, ...]]:
    """Split (to_compress, kept) after appending a turn.

    The default product rule keeps a wider 12-turn window and compresses the
    oldest batch first.  A token budget can trigger the same operation before
    the count limit; at least six recent turns remain when possible.  Smaller
    custom limits keep legacy FIFO overflow semantics for callers/tests that
    intentionally request a compact window.
    """

    if not recent:
        return (), recent
    token_budget = (
        int(recent_turn_token_budget)
        if recent_turn_token_budget is not None
        else None
    )
    total_tokens = sum(
        estimate_text_tokens(turn.text)
        + estimate_text_tokens(turn.answer_summary or "")
        + estimate_text_tokens(",".join(turn.evidence_ids))
        for turn in recent
    )
    count_exceeded = len(recent) > recent_turn_limit
    budget_exceeded = (
        recent_turn_limit >= DEFAULT_RECENT_TURN_LIMIT
        and token_budget is not None
        and total_tokens > token_budget
    )
    if not count_exceeded and not budget_exceeded:
        return (), recent

    # Explicitly small windows retain the original FIFO behavior.
    if recent_turn_limit < DEFAULT_RECENT_TURN_LIMIT:
        return recent[:-recent_turn_limit], recent[-recent_turn_limit:]

    start = compress_start_1based - 1
    end = compress_end_1based
    minimum_keep = min(
        len(recent),
        max(MIN_RECENT_TURNS_AFTER_COMPRESSION, recent_turn_limit // 2),
    )
    batch_size = end - start
    compress_count = max(batch_size if (count_exceeded or budget_exceeded) else 0, len(recent) - recent_turn_limit)
    compress_count = min(compress_count, max(1, len(recent) - minimum_keep))

    if token_budget is not None:
        while (
            compress_count < len(recent) - minimum_keep
            and sum(
                estimate_text_tokens(turn.text)
                + estimate_text_tokens(turn.answer_summary or "")
                + estimate_text_tokens(",".join(turn.evidence_ids))
                for turn in recent[compress_count:]
            )
            > token_budget
        ):
            compress_count += 1

    if compress_count <= 0:
        return (), recent
    return recent[:compress_count], recent[compress_count:]


def _in_date_window(occurred_at: datetime, date_window: DateWindow | None) -> bool:
    if date_window is None:
        return True
    stamp = occurred_at.astimezone(timezone.utc)
    if date_window.start is not None and stamp < date_window.start.astimezone(timezone.utc):
        return False
    if date_window.end is not None and stamp > date_window.end.astimezone(timezone.utc):
        return False
    return True


def latest_compressed_turn_records(
    summary: ConversationSummary,
) -> tuple[CompressedTurnRecord, ...]:
    """Records from the newest compression cycle only (bounded sliding summary)."""

    records = summary.compressed_turn_records
    if not records:
        return ()
    tagged = [item for item in records if int(item.compression_cycle or 0) > 0]
    if tagged:
        latest = max(int(item.compression_cycle) for item in tagged)
        return tuple(item for item in tagged if int(item.compression_cycle) == latest)
    # Legacy payloads without cycle tags: keep only the last compression batch size.
    batch = COMPRESS_TURN_END_1BASED - COMPRESS_TURN_START_1BASED + 1
    return tuple(records[-batch:])


def latest_summary_default_item(
    summary: ConversationSummary,
) -> DefaultContextItem | None:
    """Roll the latest compression cycle into one default-pool summary row."""

    records = latest_compressed_turn_records(summary)
    if not records:
        return None
    questions = [record.user_question for record in records]
    answers = [record.answer_summary for record in records if record.answer_summary]
    evidence_ids = tuple(
        dict.fromkeys(
            evidence_id for record in records for evidence_id in record.evidence_ids
        )
    )
    short_bits = [make_short_title(question, max_len=28) for question in questions[:4]]
    label = f"[summary] 最近压缩（{len(records)}轮）: " + " / ".join(short_bits)
    if len(label) > 500:
        label = label[:497] + "..."
    return DefaultContextItem(
        kind=DefaultContextKind.SUMMARY,
        occurred_at=min(record.occurred_at for record in records),
        label=label,
        run_id=records[-1].run_id,
        evidence_ids=evidence_ids[:40],
        user_question="\n".join(f"- {question}" for question in questions)[:20_000],
        answer_summary="\n".join(answers)[:4_000] if answers else None,
        actor_id="multiple",
    )


def enumerate_default_context_items(
    session: ConversationSession,
    *,
    date_window: DateWindow | None = None,
) -> tuple[DefaultContextItem, ...]:
    """Default pending pool: one latest-summary row + recent turns.

    Older compression cycles are excluded so the default selection stays bounded.
    ``date_window=None`` does not strip by calendar day.
    """

    items: list[DefaultContextItem] = []
    summary_item = latest_summary_default_item(session.summary)
    if summary_item is not None and _in_date_window(
        summary_item.occurred_at, date_window
    ):
        items.append(summary_item)
    for turn in session.recent_turns:
        if not _in_date_window(turn.created_at, date_window):
            continue
        items.append(
            DefaultContextItem(
                kind=DefaultContextKind.RECENT_TURN,
                occurred_at=turn.created_at,
                label=make_short_title(turn.text, max_len=80),
                run_id=turn.run_id,
                evidence_ids=turn.evidence_ids,
                user_question=turn.text,
                answer_summary=turn.answer_summary,
                actor_id=turn.user_id,
            )
        )
    # Preview == send: ascending by occurred_at (oldest / compressed summary first).
    items.sort(key=lambda item: item.occurred_at.astimezone(timezone.utc))
    return tuple(items)


def _pending_item_id_for_default(item: DefaultContextItem) -> str:
    stamp = item.occurred_at.astimezone(timezone.utc).isoformat()
    if item.kind == DefaultContextKind.SUMMARY:
        # One stable row per compression batch (collapsed latest summary).
        return f"summary:latest:{stamp}"
    rid = str(item.run_id) if item.run_id is not None else "norun"
    # Some deployments/tests use a coarse or frozen clock. Include stable
    # content entropy so same-timestamp turns do not collapse into one row.
    entropy = sha256(
        "|".join(
            (
                item.user_question or item.label,
                ",".join(item.evidence_ids),
            )
        ).encode("utf-8")
    ).hexdigest()[:12]
    return f"turn:{rid}:{stamp}:{entropy}"


def pending_item_from_default(item: DefaultContextItem) -> PendingSendItem:
    """Convert a default-pool row into a pending-send item (mark=默认)."""

    source_kind = (
        "summary" if item.kind == DefaultContextKind.SUMMARY else "recent_turn"
    )
    return PendingSendItem(
        item_id=_pending_item_id_for_default(item),
        mark=PendingItemMark.DEFAULT,
        occurred_at=item.occurred_at,
        label=item.label,
        source_kind=source_kind,
        user_question=item.user_question,
        answer_summary=item.answer_summary,
        evidence_ids=item.evidence_ids,
        run_id=item.run_id,
        actor_id=item.actor_id,
    )


def pending_item_from_citation(entry: CitationEntry) -> PendingSendItem:
    """Convert a fine-selected citation into a pending-send item (mark=引用)."""

    return PendingSendItem(
        item_id=f"citation:{entry.citation_id}",
        mark=PendingItemMark.CITATION,
        occurred_at=entry.occurred_at,
        label=entry.short_title,
        source_kind="citation",
        run_id=entry.run_id,
        citation_id=entry.citation_id,
        short_title=entry.short_title,
        evidence_ids=entry.evidence_ids,
    )


def sort_pending_items(
    items: tuple[PendingSendItem, ...] | list[PendingSendItem],
) -> tuple[PendingSendItem, ...]:
    """Ascending by occurred_at (preview order == send order)."""

    return tuple(
        sorted(items, key=lambda item: item.occurred_at.astimezone(timezone.utc))
    )


def build_pending_send_items(
    session: ConversationSession,
    *,
    date_window: DateWindow | None = None,
    selected_citation_ids: tuple[str, ...] | frozenset[str] | None = None,
) -> tuple[PendingSendItem, ...]:
    """Build time-sorted pending set: latest summary + recent turns + optional citations."""

    by_id: dict[str, PendingSendItem] = {}
    for default in enumerate_default_context_items(session, date_window=date_window):
        item = pending_item_from_default(default)
        by_id[item.item_id] = item

    if selected_citation_ids:
        wanted = {cid.strip() for cid in selected_citation_ids if cid and cid.strip()}
        for entry in session.citations:
            if entry.citation_id not in wanted:
                continue
            if not _in_date_window(entry.occurred_at, date_window):
                continue
            item = pending_item_from_citation(entry)
            by_id[item.item_id] = item

    return sort_pending_items(by_id.values())


def exclude_pending_item_ids(
    items: tuple[PendingSendItem, ...] | list[PendingSendItem],
    item_ids: tuple[str, ...] | list[str] | set[str],
) -> tuple[PendingSendItem, ...]:
    """Drop ids then re-sort by time (removed items must not be sent)."""

    drop = {item_id.strip() for item_id in item_ids if item_id and str(item_id).strip()}
    kept = [item for item in items if item.item_id not in drop]
    return sort_pending_items(kept)


def format_pending_items_for_hermes(
    items: tuple[PendingSendItem, ...] | list[PendingSendItem],
) -> str:
    """Sole conversation-history fragment when preview==send is active."""

    if not items:
        return "pending_send=(empty)"
    lines = ["pending_send:"]
    for index, item in enumerate(items, start=1):
        stamp = item.occurred_at.astimezone(timezone.utc).isoformat()
        role = {
            "virtual-product": "产品",
            "virtual-developer": "研发",
            "virtual-qa": "测试",
            "virtual-manager": "管理者",
            "virtual-ops": "运维",
            "multiple": "多角色",
        }.get(item.actor_id or "", item.actor_id or "未知角色")
        mark = "默认" if item.mark == PendingItemMark.DEFAULT else "引用"
        if item.source_kind == "citation":
            title = item.short_title or item.label
            lines.append(
                f"pending[{index}] role={role} mark={mark} time={stamp} "
                f"citation_id={item.citation_id} title={title}"
            )
        elif item.source_kind == "summary":
            evidence = ",".join(item.evidence_ids[:12])
            question = (item.user_question or item.label)[:200]
            answer = (item.answer_summary or "").strip()
            lines.append(
                f"pending[{index}] role={role} mark={mark} time={stamp} kind=summary "
                f"question={question} answer={answer} evidence_ids={evidence}"
            )
        else:
            question = (item.user_question or item.label)[:500]
            answer = (item.answer_summary or "").strip()
            lines.append(
                f"pending[{index}] role={role} mark={mark} time={stamp} "
                f"kind=recent_turn question={question} answer={answer}"
            )
    return "\n".join(lines)


def estimate_pending_token_budget(
    items: tuple[PendingSendItem, ...] | list[PendingSendItem],
) -> int:
    """Estimate tokens for the pending-send Hermes fragment."""

    return estimate_text_tokens(format_pending_items_for_hermes(items))


def list_session_history_candidates(
    session: ConversationSession,
    *,
    date_window: DateWindow | None = None,
    offset: int = 0,
    page_size: int = HISTORY_CANDIDATE_PAGE_SIZE,
) -> tuple[tuple[HistoryCandidate, ...], int]:
    """Session-side history for「选择更多历史」(paginated).

    Uses ``date_window`` when provided, else ``session.date_window``.
    Group-message candidates come from ConversationService.list_group_history (S08).
    Returns ``(page, total_count)``.
    """

    window = date_window if date_window is not None else session.date_window
    pending_citation_ids: set[str] = set()
    if session.pending_send is not None:
        pending_citation_ids = {
            item.citation_id
            for item in session.pending_send.items
            if item.citation_id
        }

    turns_by_run_id = {
        str(turn.run_id): turn
        for turn in session.recent_turns
        if turn.run_id is not None
    }
    compressed_by_run_id = {
        str(record.run_id): record
        for record in latest_compressed_turn_records(session.summary)
        if record.run_id is not None
    }
    rows: list[HistoryCandidate] = []
    for entry in session.citations:
        if not _in_date_window(entry.occurred_at, window):
            continue
        rows.append(
            HistoryCandidate(
                candidate_id=f"citation:{entry.citation_id}",
                citation_id=entry.citation_id,
                run_id=entry.run_id,
                message_id=entry.message_id,
                occurred_at=entry.occurred_at,
                short_title=entry.short_title,
                source_kind=entry.source_kind.value,
                already_in_pending=entry.citation_id in pending_citation_ids,
                user_question=(
                    turns_by_run_id[str(entry.run_id)].text
                    if entry.run_id is not None and str(entry.run_id) in turns_by_run_id
                    else (
                        compressed_by_run_id[str(entry.run_id)].user_question
                        if entry.run_id is not None and str(entry.run_id) in compressed_by_run_id
                        else None
                    )
                ),
                answer_summary=(
                    turns_by_run_id[str(entry.run_id)].answer_summary
                    if entry.run_id is not None and str(entry.run_id) in turns_by_run_id
                    else (
                        compressed_by_run_id[str(entry.run_id)].answer_summary
                        if entry.run_id is not None and str(entry.run_id) in compressed_by_run_id
                        else None
                    )
                ),
            )
        )
    # Newest first for browsing; preview/send still sorts ascending.
    rows.sort(key=lambda item: item.occurred_at.astimezone(timezone.utc), reverse=True)
    total = len(rows)
    start = max(0, int(offset))
    size = max(1, int(page_size))
    page = tuple(rows[start : start + size])
    return page, total


def group_messages_to_history_candidates(
    messages: tuple[Any, ...] | list[Any],
    session: ConversationSession,
) -> tuple[HistoryCandidate, ...]:
    """Map normalized group messages to more-history rows (dedupe via message_id)."""

    pending_citation_ids = set()
    if session.pending_send is not None:
        pending_citation_ids = {
            item.citation_id
            for item in session.pending_send.items
            if item.citation_id
        }
    by_message = {
        entry.message_id: entry
        for entry in session.citations
        if entry.message_id
    }
    rows: list[HistoryCandidate] = []
    for raw in messages:
        message_id = str(getattr(raw, "message_id", "") or "").strip()
        if not message_id:
            continue
        occurred_at = getattr(raw, "occurred_at", None)
        if not isinstance(occurred_at, datetime):
            continue
        text = str(getattr(raw, "text", "") or "")
        existing = by_message.get(message_id)
        if existing is not None:
            rows.append(
                HistoryCandidate(
                    candidate_id=f"group:{message_id}",
                    citation_id=existing.citation_id,
                    message_id=message_id,
                    occurred_at=existing.occurred_at,
                    short_title=existing.short_title,
                    source_kind=CitationSourceKind.GROUP_MESSAGE.value,
                    already_in_pending=existing.citation_id in pending_citation_ids,
                    body_text=existing.body_snapshot or text or None,
                )
            )
            continue
        rows.append(
            HistoryCandidate(
                candidate_id=f"group:{message_id}",
                citation_id=None,
                message_id=message_id,
                occurred_at=occurred_at.astimezone(timezone.utc),
                short_title=make_short_title(text),
                source_kind=CitationSourceKind.GROUP_MESSAGE.value,
                already_in_pending=False,
                body_text=text or None,
            )
        )
    return tuple(rows)
