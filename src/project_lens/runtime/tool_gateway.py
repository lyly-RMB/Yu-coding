"""Policy-aware tool gateway for Project Engineering tools."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from project_lens.runtime.patch_plan import FilePatch, PatchPlan
from project_lens.runtime.policy import EngineeringPolicy, RiskClass
from project_lens.runtime.tool_specs import (
    ToolSpec,
    assert_tool_callable,
    get_tool_spec,
    list_tool_specs,
)
from project_lens.runtime.types import ToolCategory
from project_lens.runtime.worktree import IsolatedWorktree, WorktreeValidationResult


@dataclass(frozen=True)
class ToolAuditEvent:
    id: UUID
    tool_name: str
    risk_class: RiskClass
    arguments: dict[str, Any]
    result: str
    ok: bool


@dataclass
class ToolGateway:
    policy: EngineeringPolicy
    audit_events: list[ToolAuditEvent] = field(default_factory=list)

    def list_specs(self) -> tuple[ToolSpec, ...]:
        return list_tool_specs()

    def get_spec(self, tool_name: str) -> ToolSpec:
        return get_tool_spec(tool_name)

    def read_project_file(self, relative_path: str) -> str:
        assert_tool_callable("read_project_file", allow_apply=self.policy.allow_apply)
        path = self.policy.resolve_path(relative_path)
        content = path.read_text(encoding="utf-8")
        self._audit("read_project_file", RiskClass.READ, {"path": relative_path}, content, True)
        return content

    def list_project_files(self, *, prefix: str = "src/", limit: int = 50) -> list[str]:
        assert_tool_callable("list_project_files", allow_apply=self.policy.allow_apply)
        cleaned = self._normalize_prefix(prefix)
        files = self._iter_allowlisted_files(cleaned, limit=max(1, min(limit, 200)))
        self._audit(
            "list_project_files",
            RiskClass.READ,
            {"prefix": cleaned, "file_count": len(files)},
            "\n".join(files[:40]),
            True,
        )
        return files

    def _normalize_prefix(self, prefix: str) -> str:
        cleaned = prefix.replace("\\", "/").lstrip("./") or "src/"
        if not cleaned.endswith("/"):
            cleaned = f"{cleaned}/"
        # Must stay inside allowlist (directory or file prefix).
        self.policy.resolve_path(cleaned.rstrip("/") or cleaned)
        root = self.policy.project_root.resolve()
        base = (self.policy.project_root / cleaned).resolve()
        if not str(base).startswith(str(root)):
            raise PermissionError(f"path escapes project root: {prefix}")
        return cleaned

    def _iter_allowlisted_files(self, prefix: str, *, limit: int) -> list[str]:
        root = self.policy.project_root
        base = (root / prefix).resolve()
        files: list[str] = []
        if not base.is_dir():
            return files
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            try:
                self.policy.resolve_path(rel)
            except PermissionError:
                continue
            files.append(rel)
            if len(files) >= limit:
                break
        return files

    def create_patch_plan(
        self,
        *,
        title: str,
        rationale: str,
        patches: list[dict[str, str]],
        test_commands: list[str] | None = None,
    ) -> PatchPlan:
        assert_tool_callable("create_patch_plan", allow_apply=self.policy.allow_apply)
        plan = PatchPlan(
            title=title,
            rationale=rationale,
            patches=tuple(
                FilePatch(
                    path=item["path"],
                    old_text=item.get("old_text", ""),
                    new_text=item["new_text"],
                )
                for item in patches
            ),
            test_commands=tuple(test_commands or ["pytest -q"]),
        )
        for path in plan.affected_paths():
            self.policy.resolve_path(path)
        self._audit(
            "create_patch_plan",
            RiskClass.VALIDATE,
            {"title": title, "paths": list(plan.affected_paths())},
            plan.title,
            True,
        )
        return plan

    def validate_patch_plan(self, plan: PatchPlan) -> WorktreeValidationResult:
        assert_tool_callable("validate_patch_plan", allow_apply=self.policy.allow_apply)
        result = IsolatedWorktree(self.policy).validate_patch_plan(plan)
        self._audit(
            "validate_patch_plan",
            RiskClass.VALIDATE,
            {"paths": list(plan.affected_paths())},
            json.dumps(
                {
                    "test_passed": result.test_passed,
                    "diff": result.diff_text[:500],
                }
            ),
            result.test_passed,
        )
        return result

    def apply_patch_plan(self, plan: PatchPlan) -> str:
        from project_lens.application.approval_harness import refuse_engineering_apply

        arguments = {"paths": list(plan.affected_paths())}
        try:
            assert_tool_callable("apply_patch_plan", allow_apply=self.policy.allow_apply)
            self.policy.assert_can_apply()
            refuse_engineering_apply(
                reason="engineering apply requires approval harness rollback plan"
            )
        except PermissionError as exc:
            self._audit(
                "apply_patch_plan",
                RiskClass.APPLY,
                arguments,
                str(exc),
                False,
            )
            raise

    def _audit(
        self,
        tool_name: str,
        risk_class: RiskClass,
        arguments: dict[str, Any],
        result: str,
        ok: bool,
    ) -> None:
        self.audit_events.append(
            ToolAuditEvent(
                id=uuid4(),
                tool_name=tool_name,
                risk_class=risk_class,
                arguments=arguments,
                result=result[:2_000],
                ok=ok,
            )
        )

    def tool_category(self, risk_class: RiskClass) -> ToolCategory:
        if risk_class == RiskClass.APPLY:
            return ToolCategory.WRITE
        return ToolCategory.READ
