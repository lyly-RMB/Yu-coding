"""Deterministic answer and citation checks.

The evaluator consumes already-rendered answer records. It deliberately does
not judge wording or semantic completeness; those belong to a later calibrated
LLM or human review layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from project_lens.evaluation.retrieval_cases import RetrievalCase


@dataclass(frozen=True)
class AnswerEvaluation:
    metrics: dict[str, float | int]
    failures: tuple[dict, ...] = ()

    def as_dict(self) -> dict:
        return {"metrics": dict(self.metrics), "failures": list(self.failures)}


def evaluate_answer_citations(
    cases: Iterable[RetrievalCase],
    answers: Iterable[dict],
) -> AnswerEvaluation:
    """Check citation IDs against expected evidence and retrieved scope.

    Each answer record may contain:

    ``claims``: ``[{"evidence_ids": ["..."]}, ...]``
    ``citations``: ``["..."]`` or ``[{"id": "..."}]``
    ``retrieved_object_ids``: IDs that were authorized for this answer.
    """

    case_list = tuple(cases)
    answer_list = tuple(answers)
    by_id = {str(item.get("id")): item for item in answer_list}
    failures: list[dict] = []
    citation_total = 0
    citation_correct = 0
    expected_total = 0
    expected_covered = 0
    unsupported_claims = 0
    abstention_true_positive = 0
    abstention_false_positive = 0
    abstention_false_negative = 0
    conflict_true_positive = 0
    conflict_false_positive = 0
    conflict_false_negative = 0

    for case in case_list:
        answer = by_id.get(case.id, {})
        allowed = set(str(item) for item in answer.get("retrieved_object_ids", ()))
        expected = set(case.expected_evidence_ids) or set(case.relevant_object_ids)
        citations = _citation_ids(answer.get("citations", ()))
        citation_total += len(citations)
        citation_correct += sum(
            int(item in allowed or item in expected) for item in citations
        )
        expected_total += len(expected)
        expected_covered += len(expected & set(citations))

        case_failures: list[str] = []
        if any(item not in allowed and item not in expected for item in citations):
            case_failures.append("citation_outside_expected_or_retrieved_scope")

        # Uncited FACT claims are allowed; do not inflate unsupported_claim_rate.
        abstained = bool(answer.get("abstained", False))
        if case.expected_abstention and abstained:
            abstention_true_positive += 1
        elif not case.expected_abstention and abstained:
            abstention_false_positive += 1
        elif case.expected_abstention and not abstained:
            abstention_false_negative += 1
            case_failures.append("expected_abstention_missing")
        if not case.expected_abstention and abstained:
            case_failures.append("unexpected_abstention")

        conflict_detected = bool(answer.get("conflict_detected", False))
        if case.expected_conflict and conflict_detected:
            conflict_true_positive += 1
        elif not case.expected_conflict and conflict_detected:
            conflict_false_positive += 1
        elif case.expected_conflict and not conflict_detected:
            conflict_false_negative += 1
            case_failures.append("expected_conflict_missing")
        if not case.expected_conflict and conflict_detected:
            case_failures.append("unexpected_conflict")
        if case_failures:
            failures.append({"id": case.id, "reasons": case_failures})

    abstention_precision = _precision(
        abstention_true_positive, abstention_false_positive
    )
    abstention_recall = _recall(
        abstention_true_positive, abstention_false_negative
    )
    conflict_precision = _precision(conflict_true_positive, conflict_false_positive)
    conflict_recall = _recall(conflict_true_positive, conflict_false_negative)
    return AnswerEvaluation(
        metrics={
            "citation_correctness": (
                citation_correct / citation_total if citation_total else 1.0
            ),
            "citation_completeness": (
                expected_covered / expected_total if expected_total else 1.0
            ),
            "unsupported_claim_rate": (
                unsupported_claims / max(1, sum(len(item.get("claims", ())) for item in answer_list))
            ),
            "abstention_precision": abstention_precision,
            "abstention_recall": abstention_recall,
            "conflict_precision": conflict_precision,
            "conflict_recall": conflict_recall,
            "abstention_case_count": sum(
                int(case.expected_abstention) for case in case_list
            ),
            "conflict_case_count": sum(
                int(case.expected_conflict) for case in case_list
            ),
            "answer_case_count": len(case_list),
        },
        failures=tuple(failures),
    )


def _citation_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[str] = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("id")
        if item not in (None, ""):
            result.append(str(item))
    return tuple(dict.fromkeys(result))


def _precision(true_positive: int, false_positive: int) -> float:
    denominator = true_positive + false_positive
    return true_positive / denominator if denominator else 1.0


def _recall(true_positive: int, false_negative: int) -> float:
    denominator = true_positive + false_negative
    return true_positive / denominator if denominator else 1.0


__all__ = ["AnswerEvaluation", "evaluate_answer_citations"]
