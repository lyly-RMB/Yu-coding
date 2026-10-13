"""HERMES_* values in .env must reach os.environ for in-process Hermes watchdogs."""

from __future__ import annotations

import os
from pathlib import Path

from project_lens.config import apply_hermes_runtime_env


def test_apply_hermes_runtime_env_sets_missing_keys(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "PROJECT_LENS_ENV=development",
                "HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS=300",
                'HERMES_STREAM_READ_TIMEOUT="300"',
                "HERMES_STREAM_STALE_TIMEOUT=300",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("HERMES_STREAM_READ_TIMEOUT", raising=False)
    monkeypatch.delenv("HERMES_STREAM_STALE_TIMEOUT", raising=False)

    apply_hermes_runtime_env(env_file)

    assert os.environ["HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS"] == "300"
    assert os.environ["HERMES_STREAM_READ_TIMEOUT"] == "300"
    assert os.environ["HERMES_STREAM_STALE_TIMEOUT"] == "300"


def test_apply_hermes_runtime_env_does_not_override_existing(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS=300\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS", "12")

    apply_hermes_runtime_env(env_file)

    assert os.environ["HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS"] == "12"
