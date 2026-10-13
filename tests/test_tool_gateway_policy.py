from pathlib import Path

import pytest

from project_lens.runtime.policy import EngineeringPolicy, RiskClass
from project_lens.runtime.tool_gateway import ToolGateway

DEMO_SRC = Path(__file__).parents[1] / "examples" / "payment_service"


def test_tool_gateway_blocks_paths_outside_allowlist() -> None:
    gateway = ToolGateway(EngineeringPolicy(project_root=DEMO_SRC))
    with pytest.raises(PermissionError, match="not allowed"):
        gateway.read_project_file("../secrets.txt")
    with pytest.raises(PermissionError, match="not allowed"):
        gateway.read_project_file("knowledge/architecture.md")
    with pytest.raises(PermissionError, match="not allowed"):
        gateway.list_project_files(prefix="knowledge/")


def test_tool_gateway_lists_allowlisted_files() -> None:
    gateway = ToolGateway(
        EngineeringPolicy(
            project_root=DEMO_SRC,
            allowed_path_prefixes=("src/", "tests/", "knowledge/"),
        )
    )
    files = gateway.list_project_files(prefix="src/", limit=20)
    assert "src/order_service.py" in files
    names = [event.tool_name for event in gateway.audit_events]
    assert "list_project_files" in names
    assert all(event.risk_class == RiskClass.READ for event in gateway.audit_events)
    assert all(event.risk_class != RiskClass.APPLY for event in gateway.audit_events)


def test_tool_gateway_audits_read_and_validate_steps() -> None:
    gateway = ToolGateway(
        EngineeringPolicy(project_root=DEMO_SRC, allowed_tests=("python",))
    )
    content = gateway.read_project_file("src/order_service.py")
    assert "create_order" in content
    plan = gateway.create_patch_plan(
        title="noop",
        rationale="test",
        patches=[
            {
                "path": "src/order_service.py",
                "old_text": "coupon_id = request.coupon.id",
                "new_text": "coupon_id = request.coupon.id",
            }
        ],
        test_commands=['python -c "print(1)"'],
    )
    result = gateway.validate_patch_plan(plan)
    assert result.test_passed is True
    names = [event.tool_name for event in gateway.audit_events]
    assert "read_project_file" in names
    assert "create_patch_plan" in names
    assert "validate_patch_plan" in names
    assert all(event.risk_class != RiskClass.APPLY for event in gateway.audit_events)


def test_apply_requires_explicit_policy_flag() -> None:
    gateway = ToolGateway(EngineeringPolicy(project_root=DEMO_SRC, allow_apply=False))
    plan = gateway.create_patch_plan(
        title="noop",
        rationale="test",
        patches=[
            {
                "path": "src/order_service.py",
                "old_text": "coupon_id = request.coupon.id",
                "new_text": "coupon_id = request.coupon.id",
            }
        ],
    )
    with pytest.raises(PermissionError, match="approval|disabled"):
        gateway.apply_patch_plan(plan)


def test_validate_uses_isolated_worktree_and_never_marks_apply_risk() -> None:
    gateway = ToolGateway(
        EngineeringPolicy(project_root=DEMO_SRC, allowed_tests=("python",), allow_apply=False)
    )
    plan = gateway.create_patch_plan(
        title="guard",
        rationale="validate only",
        patches=[
            {
                "path": "src/order_service.py",
                "old_text": "coupon_id = request.coupon.id",
                "new_text": "coupon_id = None if request.coupon is None else request.coupon.id",
            }
        ],
        test_commands=['python -c "print(1)"'],
    )
    result = gateway.validate_patch_plan(plan)
    assert result.test_passed is True
    assert "order_service.py" in result.diff_text
    # Original project file must remain untouched after Validate.
    original = (DEMO_SRC / "src" / "order_service.py").read_text(encoding="utf-8")
    assert "coupon_id = request.coupon.id" in original
    assert all(event.risk_class != RiskClass.APPLY for event in gateway.audit_events)
    with pytest.raises(PermissionError):
        gateway.apply_patch_plan(plan)
