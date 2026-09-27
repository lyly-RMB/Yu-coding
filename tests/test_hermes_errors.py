"""Unit tests for Hermes/Feishu structured error bubbling."""

from __future__ import annotations

from uuid import uuid4

from project_lens.integrations.feishu.hermes_errors import (
    HERMES_AGENT_LOOP_FAILED,
    HERMES_MODEL_FAILED,
    HERMES_NO_EVIDENCE,
    HERMES_UNAVAILABLE,
    classify_hermes_failure,
    format_failure_for_feishu,
    format_unexpected_for_feishu,
)


def test_classify_model_gateway() -> None:
    info = classify_hermes_failure("HTTP 502 from gateway")
    assert info.code == HERMES_MODEL_FAILED
    assert info.stage == "hermes.model"
    assert "模型调用失败" in info.format_run_error()


def test_classify_agent_loop_keeps_detail() -> None:
    info = classify_hermes_failure("Hermes agent loop failed: boom")
    assert info.code == HERMES_AGENT_LOOP_FAILED
    assert info.stage == "hermes.agent_loop"
    assert "boom" in info.format_run_error()


def test_classify_unavailable_and_no_evidence() -> None:
    assert (
        classify_hermes_failure("Hermes unavailable: ImportError").code
        == HERMES_UNAVAILABLE
    )
    assert (
        classify_hermes_failure(
            "Hermes completed without citation-ready project evidence"
        ).code
        == HERMES_NO_EVIDENCE
    )


def test_format_failure_includes_code_stage_run_id() -> None:
    run_id = uuid4()
    text = format_failure_for_feishu(
        error="Hermes agent loop failed: boom",
        run_id=run_id,
    )
    assert "HERMES_AGENT_LOOP_FAILED" in text
    assert "hermes.agent_loop" in text
    assert str(run_id) in text
    assert "boom" in text


def test_format_unexpected_bubbles_exception() -> None:
    text = format_unexpected_for_feishu(RuntimeError("outer boom"), run_id="r1")
    assert "PROJECTLENS_UNEXPECTED" in text
    assert "feishu.outer" in text
    assert "outer boom" in text
