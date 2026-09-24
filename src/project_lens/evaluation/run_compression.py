"""CLI for deterministic context-compression replay evaluation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from project_lens.context.governor import ContextGovernor, ContextGovernorConfig
from project_lens.evaluation.context_compression import (
    evaluate_compression,
    load_compression_cases,
    write_compression_report,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model-limit", type=int, default=128_000)
    parser.add_argument("--minimum-release-cases", type=int, default=50)
    args = parser.parse_args()

    cases = load_compression_cases(args.dataset)
    report = evaluate_compression(
        cases,
        governor=ContextGovernor(
            ContextGovernorConfig(model_context_limit_tokens=args.model_limit)
        ),
        minimum_release_cases=args.minimum_release_cases,
    )
    if args.output:
        write_compression_report(report, args.output)
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
