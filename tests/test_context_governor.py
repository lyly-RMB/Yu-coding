from __future__ import annotations

from dataclasses import FrozenInstanceError

from project_lens.context.governor import ContextGovernor, ContextGovernorConfig


def _config(**overrides: object) -> ContextGovernorConfig:
    values: dict[str, object] = {
        "model_context_limit_tokens": 2_000,
        "output_reserve_tokens": 100,
        "tool_schema_tokens": 50,
        "provider_overhead_tokens": 25,
        "chars_per_token": 4.0,
    }
    values.update(overrides)
    return ContextGovernorConfig(**values)


def test_governor_estimates_components_and_keeps_snapshot_stable() -> None:
    governor = ContextGovernor(_config())
    prepared = governor.prepare(
        context_text=(
            "session_intent=trace payment failure\n"
            "evidence_id=ev-1 revision=7 content_hash=hash-1\n"
        ),
        question="what happened?",
        system_prompt="read-only project agent",
    )

    report = prepared.report
    assert report.history_tokens > 0
    assert report.system_prompt_tokens > 0
    assert report.question_tokens > 0
    assert report.tool_schema_tokens == 50
    assert report.output_reserve_tokens == 100
    assert len(report.snapshot_id) == 24
    assert report.snapshot_id == prepared.report.snapshot_id
    assert prepared.snapshot.snapshot_id == report.snapshot_id
    assert prepared.snapshot.text == prepared.text
    assert report.context_hash == prepared.snapshot.context_hash
    assert report.renderer_version == "context_governor.v1"
    assert report.section_order == ("context",)


def test_governor_snapshot_is_immutable_and_changes_with_request_identity() -> None:
    governor = ContextGovernor(_config())
    first = governor.prepare(
        context_text="## Confirmed Facts\n- PAY-42",
        question="status?",
        system_prompt="system",
    )
    second = governor.prepare(
        context_text="## Confirmed Facts\n- PAY-42",
        question="next step?",
        system_prompt="system",
    )
    assert first.snapshot.snapshot_id != second.snapshot.snapshot_id
    assert first.report.context_hash == second.report.context_hash
    assert [item.title for item in first.snapshot.sections] == ["Confirmed Facts"]
    try:
        first.snapshot.text = "changed"  # type: ignore[misc]
    except FrozenInstanceError:
        pass
    else:
        raise AssertionError("ContextSnapshot must be immutable")


def test_governor_prunes_repeated_and_old_lines_before_hard_limit() -> None:
    governor = ContextGovernor(_config())
    context = "\n".join(
        [
            "session_intent=trace payment failure",
            "active_skill=incident_diagnosis",
            "evidence_id=ev-1 revision=7 content_hash=hash-1",
            *("old transcript line that can be removed" for _ in range(600)),
            "session_intent=trace payment failure",
        ]
    )

    prepared = governor.prepare(
        context_text=context,
        question="next step?",
        system_prompt="read-only project agent",
    )

    assert prepared.report.pruned is True
    assert prepared.report.removed_items
    assert prepared.report.final_context_tokens < prepared.report.original_context_tokens
    assert prepared.report.estimated_input_tokens <= int(2_000 * 0.95)
    assert "session_intent=trace payment failure" in prepared.text
    assert "evidence_id=ev-1" in prepared.text


def test_governor_observes_tool_result_tokens_without_dumping_content() -> None:
    governor = ContextGovernor(_config())
    prepared = governor.prepare(
        context_text="session_intent=trace payment failure",
        question="status?",
        system_prompt="read-only project agent",
    )
    report = governor.observe_tool_loop(
        prepared,
        messages=[
            {"role": "assistant", "content": "tool call"},
            {"role": "tool", "content": {"evidence_id": "ev-1", "summary": "ok"}},
        ],
    )

    assert report.current_tool_result_tokens > 0
    assert report.post_tool_loop_tokens is not None
    assert report.post_tool_loop_tokens > report.estimated_input_tokens
    assert report.post_utilization_ratio is not None


def test_governor_preserves_structured_summary_facts_during_aggressive_prune() -> None:
    governor = ContextGovernor(
        _config(
            model_context_limit_tokens=2_000,
            soft_limit_ratio=0.10,
            summarize_ratio=0.15,
            prune_ratio=0.20,
            rotate_ratio=0.30,
        )
    )
    context = "\n".join(
        [
            "## Session Intent",
            "完成支付链路排障",
            "## Confirmed Facts",
            "- 错误码是 PAY-42 [evidence: ev-1]",
            "- 文件是 src/payments.py [evidence: ev-2]",
            "## Decisions",
            "- 先修复重试策略 (status=confirmed)",
            *(["duplicate noise"] * 400),
        ]
    )
    prepared = governor.prepare(context_text=context, question="下一步?", system_prompt="system")
    assert "PAY-42" in prepared.text
    assert "src/payments.py" in prepared.text
    assert "先修复重试策略" in prepared.text


def test_governor_blocks_when_fixed_reserves_alone_exceed_hard_limit() -> None:
    governor = ContextGovernor(
        _config(
            model_context_limit_tokens=1_000,
            output_reserve_tokens=900,
            tool_schema_tokens=100,
            provider_overhead_tokens=100,
        )
    )
    prepared = governor.prepare(
        context_text="session_intent=important",
        question="status?",
        system_prompt="read-only project agent",
    )

    assert prepared.report.hard_blocked is True
    assert prepared.report.action == "blocked"
