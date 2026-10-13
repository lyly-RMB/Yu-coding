"""Parse Hermes final text into the shared ProjectLens answer draft contract."""

from __future__ import annotations

from project_lens.agent.draft import AnswerDraft


def extract_hermes_answer_text(text: str) -> str:
    """Extract the complete user-facing answer without turning it into facts."""

    import json
    import re

    raw = text.strip()
    if not raw:
        return ""
    cleaned = raw
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned)
    if fenced:
        cleaned = fenced.group(1).strip()
    try:
        payload = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return _collapse_repeated_paragraphs(raw)
    if not isinstance(payload, dict):
        return _collapse_repeated_paragraphs(raw)
    for key in ("answer_text", "answer_markdown", "answer", "final_answer", "response"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _collapse_repeated_paragraphs(value)
    # Legacy envelopes used business_summary/conclusion as the only prose.
    for key in ("business_summary", "conclusion"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _collapse_repeated_paragraphs(value)
    return ""


def _collapse_repeated_paragraphs(value: str) -> str:
    """Remove only adjacent exact paragraph duplicates from model output."""

    paragraphs = [part.strip() for part in value.strip().split("\n\n")]
    unique: list[str] = []
    for paragraph in paragraphs:
        if paragraph and (not unique or paragraph != unique[-1]):
            unique.append(paragraph)
    if len(unique) % 2 == 0:
        midpoint = len(unique) // 2
        if unique[:midpoint] == unique[midpoint:]:
            unique = unique[:midpoint]
    return "\n\n".join(unique)


def parse_structured_hermes_answer(text: str) -> AnswerDraft | None:
    """Return a draft only when Hermes emitted the JSON AnswerDraft shape."""

    raw = text.strip()
    if not raw:
        return None
    try:
        return AnswerDraft.from_json_text(raw)
    except (ValueError, TypeError):
        return None


def parse_hermes_answer(text: str) -> AnswerDraft:
    """Legacy parser retained for compatibility with older callers/tests.

    Production Hermes uses ``parse_structured_hermes_answer`` plus
    ``extract_hermes_answer_text``. This compatibility function preserves the
    historical summary fields for callers that still consume them.
    """

    raw = text.strip()
    if not raw:
        return AnswerDraft(unknowns=["Hermes 未返回最终答案。"])
    structured = parse_structured_hermes_answer(raw)
    if structured is not None:
        return structured
    return AnswerDraft(
        conclusion=raw[:2_000],
        business_summary=raw[:2_000],
        unknowns=["Hermes 返回了未结构化文本。"],
    )
