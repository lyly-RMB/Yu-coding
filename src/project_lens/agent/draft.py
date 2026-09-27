"""Structured answer draft produced for citation verification."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class DraftFact(BaseModel):
    text: str = Field(min_length=1)
    citations: list[str] = Field(default_factory=list)


class AnswerDraft(BaseModel):
    """LLM / stub planner final structured output before harness verification."""

    facts: list[DraftFact] = Field(default_factory=list)
    inferences: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    impact: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    conclusion: str = ""
    business_summary: str = ""
    technical_summary: str = ""
    policy_used: str = ""
    tools_used: list[str] = Field(default_factory=list)
    stop_reason: str = "stop"

    @classmethod
    def from_json_text(cls, text: str) -> "AnswerDraft":
        import json
        import re

        cleaned = text.strip()
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned)
        if fenced:
            cleaned = fenced.group(1).strip()
        # Tolerate leading prose before the JSON object.
        if not cleaned.startswith("{"):
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start >= 0 and end > start:
                cleaned = cleaned[start : end + 1]
        payload: dict[str, Any] = json.loads(cleaned)
        facts = _facts_from_payload(payload.get("facts"))
        if not facts:
            facts = _facts_from_payload(payload.get("structured_facts"))

        citation_ids = _citation_ids_from_payload(payload.get("citations"))
        answer_text = _first_nonempty_str(
            payload.get("business_summary"),
            payload.get("conclusion"),
            payload.get("answer"),
            payload.get("answer_markdown"),
        )
        # Hermes often emits {answer, citations} instead of AnswerDraft.facts.
        if not facts and answer_text:
            facts = [DraftFact(text=answer_text[:4_000], citations=citation_ids)]
        elif facts and citation_ids:
            # Attach top-level citations when individual facts omitted them.
            enriched: list[DraftFact] = []
            for fact in facts:
                if fact.citations:
                    enriched.append(fact)
                else:
                    enriched.append(
                        DraftFact(text=fact.text, citations=list(citation_ids))
                    )
            facts = enriched

        unknowns = _string_list(
            payload.get("unknowns"),
            payload.get("limitations"),
            payload.get("open_questions"),
        )
        next_actions = _string_list(
            payload.get("next_actions"),
            payload.get("follow_up_questions"),
        )
        business_summary = _first_nonempty_str(
            payload.get("business_summary"),
            answer_text,
        )[:4_000]
        conclusion = _first_nonempty_str(
            payload.get("conclusion"),
            answer_text,
        )[:2_000]
        return cls(
            facts=facts,
            inferences=_string_list(payload.get("inferences")),
            unknowns=unknowns,
            impact=_string_list(payload.get("impact")),
            next_actions=next_actions,
            conclusion=conclusion,
            business_summary=business_summary,
            technical_summary=str(payload.get("technical_summary") or ""),
            policy_used=str(payload.get("policy_used") or ""),
            # Hermes envelopes often use used_tools; AnswerDraft contract is tools_used.
            tools_used=_string_list(
                payload.get("tools_used"),
                payload.get("used_tools"),
            ),
            stop_reason=str(payload.get("stop_reason") or "stop"),
        )


def _facts_from_payload(raw: Any) -> list[DraftFact]:
    if not isinstance(raw, list):
        return []
    facts: list[DraftFact] = []
    for item in raw:
        if isinstance(item, dict):
            text = str(
                item.get("text")
                or item.get("fact")
                or item.get("statement")
                or ""
            ).strip()
            if not text:
                continue
            citations = _citation_ids_from_payload(
                item.get("citations") or item.get("citation_ids")
            )
            facts.append(DraftFact(text=text[:4_000], citations=citations))
        elif isinstance(item, str) and item.strip():
            facts.append(DraftFact(text=item.strip()[:4_000], citations=[]))
    return facts


def _citation_ids_from_payload(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    if not isinstance(raw, list):
        return []
    ids: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            ids.append(item.strip())
            continue
        if isinstance(item, dict):
            for key in ("citation_id", "id", "evidence_id", "evidence_ref"):
                value = item.get(key)
                if value is not None and str(value).strip():
                    ids.append(str(value).strip())
                    break
    # Preserve order, drop empties/dupes.
    return list(dict.fromkeys(ids))


def _string_list(*parts: Any) -> list[str]:
    values: list[str] = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, str) and part.strip():
            values.append(part.strip())
            continue
        if isinstance(part, list):
            for item in part:
                text = str(item).strip()
                if text:
                    values.append(text)
    return list(dict.fromkeys(values))


def _first_nonempty_str(*parts: Any) -> str:
    for part in parts:
        text = str(part or "").strip()
        if text:
            return text
    return ""


def parse_citation_id(raw: str) -> UUID | None:
    try:
        return UUID(str(raw).strip().strip("`\"'"))
    except ValueError:
        return None


def resolve_citation_id(raw: str, evidence_ids: set[UUID]) -> UUID | None:
    """Map a citation token to a ledger Evidence id.

    Accepts full UUIDs and unambiguous 8+ hex prefixes (e.g. prose ``ref `dd3a1aa6```).
    Ambiguous or unknown tokens return None — never guess across multiple matches.
    """

    if not evidence_ids:
        return None
    cleaned = _normalize_citation_token(raw)
    if not cleaned:
        return None
    full = parse_citation_id(cleaned)
    if full is not None:
        return full if full in evidence_ids else None
    hex_body = cleaned.lower().replace("-", "")
    if len(hex_body) < 8 or any(ch not in "0123456789abcdef" for ch in hex_body):
        return None
    prefix = hex_body[:8]
    matches = [
        eid
        for eid in evidence_ids
        if str(eid).replace("-", "").startswith(prefix)
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _normalize_citation_token(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    text = text.strip("`\"'")
    lowered = text.casefold()
    for prefix in ("ref ", "ref:", "evidence_ref=", "evidence_id=", "citation_id="):
        if lowered.startswith(prefix):
            text = text[len(prefix) :].strip().strip("`\"'")
            break
    return text.strip()
