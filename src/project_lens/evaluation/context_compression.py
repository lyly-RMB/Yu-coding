"""Deterministic quality evaluation for ProjectLens context compression."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Literal

from project_lens.context.governor import ContextGovernor

ProbeKind = Literal[
    "recall",
    "artifact",
    "decision",
    "continuation",
    "citation",
    "negative",
]


@dataclass(frozen=True)
class CompressionProbe:
    probe_id: str
    kind: ProbeKind
    expected: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompressionCase:
    case_id: str
    context_text: str
    question: str
    probes: tuple[CompressionProbe, ...]


@dataclass(frozen=True)
class CompressionProbeResult:
    case_id: str
    probe_id: str
    kind: ProbeKind
    passed: bool
    missing: tuple[str, ...] = ()
    forbidden_present: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompressionCaseResult:
    case_id: str
    original_tokens: int
    final_tokens: int
    token_reduction_ratio: float
    action: str
    hard_blocked: bool
    probes: tuple[CompressionProbeResult, ...]

    @property
    def passed(self) -> bool:
        return not self.hard_blocked and all(item.passed for item in self.probes)


@dataclass(frozen=True)
class CompressionEvaluationReport:
    cases: tuple[CompressionCaseResult, ...]
    dataset_size: int
    minimum_release_cases: int = 50
    min_probe_pass_rate: float = 0.98
    min_token_reduction_ratio: float = 0.40

    @property
    def probe_count(self) -> int:
        return sum(len(item.probes) for item in self.cases)

    @property
    def passed_probe_count(self) -> int:
        return sum(sum(1 for probe in item.probes if probe.passed) for item in self.cases)

    @property
    def probe_pass_rate(self) -> float:
        return self.passed_probe_count / self.probe_count if self.probe_count else 0.0

    @property
    def token_reduction_ratio(self) -> float:
        original = sum(item.original_tokens for item in self.cases)
        final = sum(item.final_tokens for item in self.cases)
        return (original - final) / original if original else 0.0

    @property
    def status(self) -> str:
        if self.dataset_size < self.minimum_release_cases:
            return "insufficient_data"
        if any(item.hard_blocked for item in self.cases):
            return "fail"
        if self.probe_pass_rate < self.min_probe_pass_rate:
            return "fail"
        if self.token_reduction_ratio < self.min_token_reduction_ratio:
            return "fail"
        return "pass"

    def as_dict(self) -> dict[str, Any]:
        by_kind: dict[str, dict[str, int]] = {}
        for case in self.cases:
            for probe in case.probes:
                bucket = by_kind.setdefault(probe.kind, {"passed": 0, "total": 0})
                bucket["total"] += 1
                bucket["passed"] += int(probe.passed)
        return {
            "schema_version": "context-compression-evaluation.v1",
            "status": self.status,
            "dataset_size": self.dataset_size,
            "minimum_release_cases": self.minimum_release_cases,
            "probe_count": self.probe_count,
            "passed_probe_count": self.passed_probe_count,
            "probe_pass_rate": round(self.probe_pass_rate, 6),
            "token_reduction_ratio": round(self.token_reduction_ratio, 6),
            "by_kind": by_kind,
            "cases": [
                {
                    "case_id": item.case_id,
                    "original_tokens": item.original_tokens,
                    "final_tokens": item.final_tokens,
                    "token_reduction_ratio": round(item.token_reduction_ratio, 6),
                    "action": item.action,
                    "hard_blocked": item.hard_blocked,
                    "passed": item.passed,
                    "probes": [
                        {
                            "probe_id": probe.probe_id,
                            "kind": probe.kind,
                            "passed": probe.passed,
                            "missing": list(probe.missing),
                            "forbidden_present": list(probe.forbidden_present),
                        }
                        for probe in item.probes
                    ],
                }
                for item in self.cases
            ],
        }


def evaluate_compression_case(
    case: CompressionCase,
    *,
    governor: ContextGovernor,
) -> CompressionCaseResult:
    prepared = governor.prepare(
        context_text=case.context_text,
        question=case.question,
        system_prompt="ProjectLens read-only system contract",
        force_prune=True,
    )
    original_tokens = governor.estimate_tokens(case.context_text)
    final_tokens = governor.estimate_tokens(prepared.text)
    probes: list[CompressionProbeResult] = []
    for probe in case.probes:
        missing = tuple(item for item in probe.expected if item not in prepared.text)
        forbidden = tuple(item for item in probe.forbidden if item in prepared.text)
        probes.append(
            CompressionProbeResult(
                case_id=case.case_id,
                probe_id=probe.probe_id,
                kind=probe.kind,
                passed=not missing and not forbidden,
                missing=missing,
                forbidden_present=forbidden,
            )
        )
    reduction = (original_tokens - final_tokens) / original_tokens if original_tokens else 0.0
    return CompressionCaseResult(
        case_id=case.case_id,
        original_tokens=original_tokens,
        final_tokens=final_tokens,
        token_reduction_ratio=reduction,
        action=prepared.report.action,
        hard_blocked=prepared.report.hard_blocked,
        probes=tuple(probes),
    )


def evaluate_compression(
    cases: tuple[CompressionCase, ...],
    *,
    governor: ContextGovernor,
    minimum_release_cases: int = 50,
) -> CompressionEvaluationReport:
    results = tuple(evaluate_compression_case(case, governor=governor) for case in cases)
    return CompressionEvaluationReport(
        cases=results,
        dataset_size=len(cases),
        minimum_release_cases=minimum_release_cases,
    )


def load_compression_cases(path: str | Path) -> tuple[CompressionCase, ...]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("compression dataset must be a JSON list")
    cases: list[CompressionCase] = []
    for raw in payload:
        if not isinstance(raw, dict):
            raise ValueError("compression case must be an object")
        probes = tuple(
            CompressionProbe(
                probe_id=str(item["probe_id"]),
                kind=item["kind"],
                expected=tuple(str(value) for value in item.get("expected", [])),
                forbidden=tuple(str(value) for value in item.get("forbidden", [])),
            )
            for item in raw.get("probes", [])
        )
        cases.append(
            CompressionCase(
                case_id=str(raw["case_id"]),
                context_text=str(raw["context_text"]),
                question=str(raw["question"]),
                probes=probes,
            )
        )
    return tuple(cases)


def write_compression_report(report: CompressionEvaluationReport, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
