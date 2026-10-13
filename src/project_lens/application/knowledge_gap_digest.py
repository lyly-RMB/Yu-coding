"""Project-scoped daily knowledge-gap digest generation and idempotent records."""

from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
import json
from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import Field

from project_lens.application.knowledge_gap_store import KnowledgeGapStore
from project_lens.domain.knowledge_gap import (
    FrozenModel,
    GapSeverity,
    KnowledgeGapCase,
    KnowledgeGapStatus,
)
from project_lens.domain.models import ProjectRef


class KnowledgeGapDigestItem(FrozenModel):
    gap_id: UUID
    title: str
    question: str
    description: str
    severity: GapSeverity
    status: KnowledgeGapStatus
    impact: str = ""
    target_ref: str = ""
    suggested_source_type: str = ""
    suggested_owner_ids: tuple[str, ...] = ()
    evidence_ids: tuple[UUID, ...] = ()
    evidence_status: str = "authorized"
    occurrence_count: int = Field(ge=1)
    last_seen_at: datetime


class DailyKnowledgeGapDigest(FrozenModel):
    project: ProjectRef
    period_start: datetime
    period_end: datetime
    digest_date: date
    new_cases: tuple[KnowledgeGapDigestItem, ...] = ()
    ongoing_cases: tuple[KnowledgeGapDigestItem, ...] = ()
    high_impact_cases: tuple[KnowledgeGapDigestItem, ...] = ()
    reopened_cases: tuple[KnowledgeGapDigestItem, ...] = ()
    resolved_cases: tuple[KnowledgeGapDigestItem, ...] = ()
    summary_hash: str = Field(min_length=64, max_length=64)


class OwnerKnowledgeGapDigest(FrozenModel):
    owner_id: str = Field(min_length=1, max_length=200)
    new_cases: tuple[KnowledgeGapDigestItem, ...] = ()
    ongoing_cases: tuple[KnowledgeGapDigestItem, ...] = ()
    high_impact_cases: tuple[KnowledgeGapDigestItem, ...] = ()
    reopened_cases: tuple[KnowledgeGapDigestItem, ...] = ()


class DailyKnowledgeGapDigestBundle(FrozenModel):
    project_digest: DailyKnowledgeGapDigest
    owner_digests: tuple[OwnerKnowledgeGapDigest, ...] = ()


class KnowledgeGapDigestRun(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    digest_key: str = Field(min_length=1, max_length=500)
    project: ProjectRef
    period_start: datetime
    period_end: datetime
    summary_hash: str = Field(min_length=64, max_length=64)
    status: Literal["generated", "pending", "sent", "failed"] = "generated"
    audience_scope: str = "project"
    recipient_ids: tuple[str, ...] = ()
    sent_at: datetime | None = None
    generated_at: datetime


class KnowledgeGapDigestRunStore(Protocol):
    def get_digest_run(self, digest_key: str) -> KnowledgeGapDigestRun | None: ...
    def refresh_digest_run(self, run: KnowledgeGapDigestRun) -> KnowledgeGapDigestRun: ...
    def transition_digest_run(
        self,
        digest_key: str,
        *,
        expected_summary_hash: str,
        status: Literal["pending", "sent", "failed"],
        recipient_ids: tuple[str, ...],
        at: datetime,
    ) -> KnowledgeGapDigestRun: ...


class KnowledgeGapDigestService:
    def __init__(self, gap_store: KnowledgeGapStore, run_store: KnowledgeGapDigestRunStore) -> None:
        self._gap_store = gap_store
        self._run_store = run_store

    def get_run(self, digest: DailyKnowledgeGapDigest) -> KnowledgeGapDigestRun | None:
        return self._run_store.get_digest_run(_digest_key(digest.project, digest.period_start, digest.period_end))

    def claim_notification(self, notification_key: str, *, gap_id: UUID, project: ProjectRef, recipient_id: str, event_type: str, period_key: str, at: datetime) -> bool:
        return self._gap_store.claim_digest_notification(
            notification_key, gap_id=gap_id, project=project,
            recipient_id=recipient_id, event_type=event_type,
            period_key=period_key, at=_utc(at),
        )

    def build_bundle(
        self,
        project: ProjectRef,
        *,
        period_start: datetime,
        period_end: datetime,
        authorized_evidence_ids: frozenset[UUID],
        authorized_owner_ids: frozenset[str],
        now: datetime | None = None,
    ) -> DailyKnowledgeGapDigestBundle:
        """Build owner views only for explicitly authorized recipients."""

        if not authorized_owner_ids:
            raise ValueError("at least one authorized owner is required")
        project_digest = self.build(
            project,
            period_start=period_start,
            period_end=period_end,
            authorized_evidence_ids=authorized_evidence_ids,
            now=now,
        )
        items = (
            *project_digest.new_cases,
            *project_digest.ongoing_cases,
            *project_digest.reopened_cases,
            *project_digest.resolved_cases,
        )
        owners = sorted({owner for item in items for owner in item.suggested_owner_ids} & authorized_owner_ids)
        sections = tuple(
            OwnerKnowledgeGapDigest(
                owner_id=owner,
                new_cases=tuple(item for item in project_digest.new_cases if owner in item.suggested_owner_ids),
                ongoing_cases=tuple(item for item in project_digest.ongoing_cases if owner in item.suggested_owner_ids),
                high_impact_cases=tuple(item for item in project_digest.high_impact_cases if owner in item.suggested_owner_ids),
                reopened_cases=tuple(item for item in project_digest.reopened_cases if owner in item.suggested_owner_ids),
            )
            for owner in owners
        )
        return DailyKnowledgeGapDigestBundle(project_digest=project_digest, owner_digests=sections)

    def build(
        self,
        project: ProjectRef,
        *,
        period_start: datetime,
        period_end: datetime,
        authorized_evidence_ids: frozenset[UUID] | None = None,
        now: datetime | None = None,
    ) -> DailyKnowledgeGapDigest:
        start, end = _utc(period_start), _utc(period_end)
        if end <= start:
            raise ValueError("digest period_end must be after period_start")
        generated_at = _utc(now or datetime.now(timezone.utc))
        cases = self._gap_store.list_for_project(project)
        if authorized_evidence_ids is not None:
            cases = tuple(
                case for case in cases
                if not case.evidence_ids or set(case.evidence_ids).issubset(authorized_evidence_ids)
            )
        ongoing_states = {
            KnowledgeGapStatus.OPEN,
            KnowledgeGapStatus.ACKNOWLEDGED,
            KnowledgeGapStatus.ASSIGNED,
            KnowledgeGapStatus.WAITING_FOR_SOURCE,
            KnowledgeGapStatus.SNOOZED,
        }
        new = [case for case in cases if _within(case.first_detected_at, start, end)]
        reopened = [case for case in cases if case.status == KnowledgeGapStatus.REOPENED and _within(case.last_seen_at, start, end)]
        resolved = [case for case in cases if case.status == KnowledgeGapStatus.RESOLVED and case.resolved_at is not None and _within(case.resolved_at, start, end)]
        ongoing = [case for case in cases if case.status in ongoing_states]
        high_impact = [case for case in ongoing if case.severity in {GapSeverity.HIGH, GapSeverity.CRITICAL}]
        result = {
            "project": project.model_dump(mode="json"),
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "new_cases": [_case_payload(item) for item in new],
            "ongoing_cases": [_case_payload(item) for item in ongoing],
            "high_impact_cases": [_case_payload(item) for item in high_impact],
            "reopened_cases": [_case_payload(item) for item in reopened],
            "resolved_cases": [_case_payload(item) for item in resolved],
        }
        digest_hash = sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        digest = DailyKnowledgeGapDigest(
            project=project,
            period_start=start,
            period_end=end,
            digest_date=end.date(),
            new_cases=tuple(_item(case) for case in new),
            ongoing_cases=tuple(_item(case) for case in ongoing),
            high_impact_cases=tuple(_item(case) for case in high_impact),
            reopened_cases=tuple(_item(case) for case in reopened),
            resolved_cases=tuple(_item(case) for case in resolved),
            summary_hash=digest_hash,
        )
        key = _digest_key(project, start, end)
        run = KnowledgeGapDigestRun(
            id=uuid4(),
            digest_key=key,
            project=project,
            period_start=start,
            period_end=end,
            summary_hash=digest_hash,
            generated_at=generated_at,
        )
        self._run_store.refresh_digest_run(run)
        return digest

    def mark_delivery(
        self,
        digest: DailyKnowledgeGapDigest,
        *,
        recipient_ids: tuple[str, ...],
        status: str,
        now: datetime | None = None,
    ) -> KnowledgeGapDigestRun:
        key = _digest_key(digest.project, digest.period_start, digest.period_end)
        return self._run_store.transition_digest_run(
            key,
            expected_summary_hash=digest.summary_hash,
            status=status,
            recipient_ids=recipient_ids,
            at=_utc(now or datetime.now(timezone.utc)),
        )


def render_knowledge_gap_digest(digest: DailyKnowledgeGapDigest) -> dict[str, object]:
    lines = [
        f"项目：{digest.project.project_id}",
        f"周期：{digest.period_start.isoformat()} 至 {digest.period_end.isoformat()}",
        f"新增：{len(digest.new_cases)} | 持续未解决：{len(digest.ongoing_cases)} | 高影响：{len(digest.high_impact_cases)} | 重新打开：{len(digest.reopened_cases)} | 已关闭：{len(digest.resolved_cases)}",
    ]
    all_items = []
    for label, items in (
        ("新增", digest.new_cases),
        ("持续未解决", digest.ongoing_cases),
        ("高影响", digest.high_impact_cases),
        ("重新打开", digest.reopened_cases),
        ("已验证关闭", digest.resolved_cases),
    ):
        if not items:
            continue
        lines.extend(("", f"{label}："))
        for item in items[:20]:
            all_items.append(item)
            owner = f"；建议负责人：{', '.join(item.suggested_owner_ids)}" if item.suggested_owner_ids else ""
            impact = f"；影响：{item.impact}" if item.impact else ""
            lines.append(f"- [{item.severity.value}] {item.title}（出现 {item.occurrence_count} 次）{impact}{owner}")
    elements: list[dict[str, object]] = [{"tag": "markdown", "content": "\n".join(lines)}]
    for item in all_items[:10]:
        elements.append({
            "tag": "action",
            "actions": [
                {"tag": "button", "text": {"tag": "plain_text", "content": "认领"}, "type": "primary", "value": {"action": "knowledge_gap_accept", "gap_id": str(item.gap_id)}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "请求复查"}, "value": {"action": "knowledge_gap_recheck", "gap_id": str(item.gap_id)}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "暂缓"}, "value": {"action": "knowledge_gap_snooze", "gap_id": str(item.gap_id)}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "误报"}, "value": {"action": "knowledge_gap_dismiss", "gap_id": str(item.gap_id)}},
            ],
        })
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "orange", "title": {"tag": "plain_text", "content": "ProjectLens 每日知识缺口摘要"}},
        "elements": elements,
    }


def render_owner_knowledge_gap_digest(
    digest: DailyKnowledgeGapDigest,
    owner_digest: OwnerKnowledgeGapDigest,
) -> dict[str, object]:
    """Render a recipient-scoped card containing only that owner's assigned suggestions."""

    lines = [
        f"项目：{digest.project.project_id}",
        f"周期：{digest.period_start.isoformat()} 至 {digest.period_end.isoformat()}",
    ]
    for label, items in (
        ("新增", owner_digest.new_cases),
        ("持续未解决", owner_digest.ongoing_cases),
        ("高影响", owner_digest.high_impact_cases),
        ("重新打开", owner_digest.reopened_cases),
    ):
        if not items:
            continue
        lines.extend(("", f"{label}："))
        for item in items[:20]:
            impact = f"；影响：{item.impact}" if item.impact else ""
            lines.extend((
                f"### {item.title}",
                f"- 严重程度：{item.severity.value}；状态：{item.status.value}；出现次数：{item.occurrence_count}",
                f"- 原始问题：{item.question}",
                f"- 缺口说明：{item.description}",
                f"- 证据状态：{'已授权证据' if item.evidence_status == 'authorized' else '当前没有可用的授权证据'}",
            ))
            if item.evidence_ids:
                lines.append(f"- 关联证据数：{len(item.evidence_ids)}")
            if item.suggested_source_type:
                lines.append(f"- 建议补充资料类型：{item.suggested_source_type}")
            if impact:
                lines.append(f"- 影响：{item.impact}")
            if item.target_ref:
                lines.append(f"- 关联对象：{item.target_ref}")
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "orange", "title": {"tag": "plain_text", "content": "分配给你的知识缺口"}},
        "elements": [{"tag": "markdown", "content": "\n".join(lines)}],
    }


def _item(case: KnowledgeGapCase) -> KnowledgeGapDigestItem:
    return KnowledgeGapDigestItem(
        gap_id=case.id, title=case.title, question=case.question,
        description=case.description, severity=case.severity, status=case.status,
        impact=case.impact, target_ref=case.target_ref,
        suggested_source_type=case.suggested_source_type,
        suggested_owner_ids=case.suggested_owner_ids, evidence_ids=case.evidence_ids,
        evidence_status=case.evidence_status,
        occurrence_count=case.occurrence_count, last_seen_at=case.last_seen_at,
    )


def _case_payload(case: KnowledgeGapCase) -> dict[str, object]:
    return _item(case).model_dump(mode="json") | {"status": case.status.value}


def _within(value: datetime, start: datetime, end: datetime) -> bool:
    value = _utc(value)
    return start <= value < end


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _digest_key(project: ProjectRef, start: datetime, end: datetime) -> str:
    return f"{project.tenant_id}:{project.project_id}:{start.isoformat()}:{end.isoformat()}:project"
