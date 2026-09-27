"""Turn a Hermes tool-loop result into a citation-checked ProjectAnswer."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from hashlib import sha256
from uuid import UUID

from project_lens.agent.draft import AnswerDraft, DraftFact, resolve_citation_id
from project_lens.agent.read_tools import InvestigationLedger
from project_lens.agent.verifier import verify_answer_draft
from project_lens.domain.models import Evidence, EvidenceType, ProjectAnswer, ProjectRef, SourceRef

_FULL_UUID_RE = re.compile(
    r"\b([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\b"
)
_SHORT_REF_RE = re.compile(
    r"(?:ref\s*[`:]?\s*|evidence(?:_ref|_id)?\s*[:=]\s*|`)"
    r"([0-9a-fA-F]{8})(?:-[0-9a-fA-F]{4})?\b",
    re.IGNORECASE,
)
_BACKFILL_CAP = 12
_SEARCH_TOOL_MARKERS = ("search_context",)


def project_answer_from_hermes(
    *, project: ProjectRef, draft: AnswerDraft, tool_calls: tuple[object, ...]
) -> ProjectAnswer:
    """Verify Hermes citations using bounded citation metadata from tool results."""
    ledger = InvestigationLedger()
    ledger_ids: list[UUID] = []
    search_ids: list[UUID] = []
    for call in tool_calls:
        envelope = getattr(call, "envelope", {}) or {}
        name = str(getattr(call, "name", "") or envelope.get("tool_name") or "")
        is_search = any(marker in name for marker in _SEARCH_TOOL_MARKERS)
        for ref in envelope.get("evidence_refs") or envelope.get("citations") or []:
            if not isinstance(ref, dict):
                continue
            evidence = _citation_to_evidence(project, ref)
            if evidence is None:
                continue
            ledger.add_evidence(evidence)
            if evidence.id not in ledger_ids:
                ledger_ids.append(evidence.id)
            if is_search and evidence.id not in search_ids:
                search_ids.append(evidence.id)
    ledger.tool_names.extend(
        str(getattr(call, "name", "")) for call in tool_calls if getattr(call, "name", "")
    )
    enriched = _enrich_draft_citations(
        draft,
        ledger_ids=ledger_ids,
        preferred_ids=search_ids or ledger_ids,
        tool_names=list(ledger.tool_names),
    )
    answer = verify_answer_draft(enriched, project=project, ledger=ledger)
    return answer.model_copy(
        update={"skill": "project_investigation", "policy_used": "hermes-citation-ledger-v1"}
    )


def _enrich_draft_citations(
    draft: AnswerDraft,
    *,
    ledger_ids: list[UUID],
    preferred_ids: list[UUID],
    tool_names: list[str],
) -> AnswerDraft:
    """Fill missing structured citations from prose refs and the tool evidence ledger.

    Hermes often emits a long ``answer_markdown`` while leaving ``citations`` empty.
    Without this step the verifier demotes the whole answer and Feishu marks it degraded.
    """

    if not draft.facts and not ledger_ids:
        return draft
    evidence_set = set(ledger_ids)
    id_order = preferred_ids or ledger_ids
    new_facts: list[DraftFact] = []
    for fact in draft.facts:
        merged = _merge_citation_tokens(
            fact.citations,
            _extract_citation_tokens(fact.text),
            evidence_set=evidence_set,
        )
        # Only auto-attach ledger hits when Hermes omitted structured citations.
        # Explicit but invalid citation ids still demote (do not silently replace).
        if not merged and not fact.citations and fact.text.strip() and id_order:
            merged = [str(eid) for eid in id_order[:_BACKFILL_CAP]]
        new_facts.append(DraftFact(text=fact.text, citations=merged))
    tools_used = list(draft.tools_used)
    if not tools_used and tool_names:
        tools_used = list(dict.fromkeys(name for name in tool_names if name))
    return draft.model_copy(update={"facts": new_facts, "tools_used": tools_used})


def _merge_citation_tokens(
    *groups: list[str],
    evidence_set: set[UUID],
) -> list[str]:
    resolved: list[UUID] = []
    for group in groups:
        for raw in group:
            eid = resolve_citation_id(raw, evidence_set)
            if eid is not None and eid not in resolved:
                resolved.append(eid)
    return [str(eid) for eid in resolved]


def _extract_citation_tokens(text: str) -> list[str]:
    if not text:
        return []
    found: list[str] = []
    for match in _FULL_UUID_RE.finditer(text):
        found.append(match.group(1))
    for match in _SHORT_REF_RE.finditer(text):
        found.append(match.group(1))
    return list(dict.fromkeys(found))


def _citation_to_evidence(project: ProjectRef, ref: dict[str, object]) -> Evidence | None:
    try:
        evidence_id = UUID(str(ref.get("id") or ""))
    except ValueError:
        return None
    source_uri = str(ref.get("source_uri") or "hermes:unknown")[:500]
    system, _, source_id = source_uri.partition(":")
    if not source_id:
        system, source_id = "hermes", source_uri
    summary = str(ref.get("summary") or "citation metadata").strip()[:300] or "citation metadata"
    return Evidence(
        id=evidence_id,
        type=_kind_to_type(str(ref.get("kind") or "document")),
        project=project,
        source=SourceRef(system=system[:50] or "hermes", source_id=source_id[:500]),
        content=summary,
        observed_at=datetime.now(timezone.utc),
        access_scope=f"project:{project.project_id}:read",
        content_hash=sha256(summary.encode("utf-8")).hexdigest(),
        metadata={"citation_only": True, "source_uri": source_uri},
    )


def _kind_to_type(kind: str) -> EvidenceType:
    return {
        "doc": EvidenceType.DOCUMENT,
        "document": EvidenceType.DOCUMENT,
        "code": EvidenceType.CODE,
        "commit": EvidenceType.COMMIT,
        "pull_request": EvidenceType.PULL_REQUEST,
        "pr": EvidenceType.PULL_REQUEST,
        "task": EvidenceType.TASK,
        "incident": EvidenceType.INCIDENT,
        "log": EvidenceType.LOG,
        "metric": EvidenceType.METRIC,
    }.get(kind.casefold(), EvidenceType.DOCUMENT)
