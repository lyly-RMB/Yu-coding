"""Controlled, idempotent delivery runner for daily knowledge-gap digests."""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Protocol
from zoneinfo import ZoneInfo

from project_lens.application.knowledge_gap_digest import (
    KnowledgeGapDigestService,
    render_knowledge_gap_digest,
    render_owner_knowledge_gap_digest,
)
from project_lens.domain.models import ProjectRef

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DigestRecipients:
    project_chat_id: str
    owner_open_ids: dict[str, str]


class DigestContextResolver(Protocol):
    def __call__(self, project: ProjectRef, period_start: datetime, period_end: datetime) -> tuple[frozenset, frozenset[str], DigestRecipients]: ...


class DigestSender(Protocol):
    async def post_card(self, chat_id: str, card: dict[str, object]) -> None: ...
    async def post_user_card(self, open_id: str, card: dict[str, object]) -> None: ...


@dataclass(frozen=True)
class DigestRunResult:
    project_id: str
    period_start: datetime
    period_end: datetime
    dry_run: bool
    sent: int
    failed: int
    skipped: int
    errors: tuple[str, ...] = ()


class KnowledgeGapDigestRunner:
    def __init__(
        self,
        *,
        service: KnowledgeGapDigestService,
        projects: tuple[ProjectRef, ...],
        resolve_context: DigestContextResolver,
        sender: DigestSender,
        timezone_name: str = "UTC",
        enabled: bool = False,
        interval_seconds: int = 86_400,
    ) -> None:
        self._service = service
        self._projects = projects
        self._resolve_context = resolve_context
        self._sender = sender
        self._zone = ZoneInfo(timezone_name)
        self._enabled = enabled
        self._interval_seconds = max(60, int(interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_results: tuple[DigestRunResult, ...] = ()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def last_results(self) -> tuple[DigestRunResult, ...]:
        return self._last_results

    def start(self) -> None:
        if not self._enabled or self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="knowledge-gap-digest", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 1.0) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def run_once(self, *, now: datetime | None = None, dry_run: bool = False) -> tuple[DigestRunResult, ...]:
        end_local = (now or datetime.now(timezone.utc)).astimezone(self._zone).replace(hour=0, minute=0, second=0, microsecond=0)
        start_local = end_local - timedelta(days=1)
        start, end = start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)
        results = tuple(asyncio.run(self._run_project(project, start, end, dry_run=dry_run)) for project in self._projects)
        self._last_results = results
        return results

    async def _run_project(self, project: ProjectRef, start: datetime, end: datetime, *, dry_run: bool) -> DigestRunResult:
        try:
            evidence_ids, owner_ids, recipients = self._resolve_context(project, start, end)
            bundle = self._service.build_bundle(project, period_start=start, period_end=end, authorized_evidence_ids=evidence_ids, authorized_owner_ids=owner_ids)
            if dry_run:
                return DigestRunResult(project.project_id, start, end, True, 0, 0, 0)
            sent = failed = skipped = 0
            recipients_sent: list[str] = []
            if recipients.project_chat_id and self._service.get_run(bundle.project_digest) is not None:
                claimable = self._claim_items(bundle.project_digest, recipients.project_chat_id, bundle.project_digest.new_cases + bundle.project_digest.ongoing_cases + bundle.project_digest.reopened_cases + bundle.project_digest.resolved_cases, start)
                if not claimable:
                    skipped += 1
                else:
                    try:
                        await self._sender.post_card(recipients.project_chat_id, render_knowledge_gap_digest(bundle.project_digest))
                        sent += 1; recipients_sent.append(recipients.project_chat_id)
                    except Exception as exc:  # noqa: BLE001
                        failed += 1
                        logger.exception("knowledge-gap project digest delivery failed")
            for owner_digest in bundle.owner_digests:
                open_id = recipients.owner_open_ids.get(owner_digest.owner_id)
                if not open_id:
                    skipped += 1
                    continue
                if not self._claim_items(bundle.project_digest, open_id, owner_digest.new_cases + owner_digest.ongoing_cases + owner_digest.reopened_cases, start):
                    skipped += 1
                    continue
                try:
                    await self._sender.post_user_card(open_id, render_owner_knowledge_gap_digest(bundle.project_digest, owner_digest))
                    sent += 1; recipients_sent.append(open_id)
                except Exception:  # noqa: BLE001
                    failed += 1
                    logger.exception("knowledge-gap owner digest delivery failed")
            self._service.mark_delivery(bundle.project_digest, recipient_ids=tuple(recipients_sent), status="sent" if failed == 0 else "failed")
            return DigestRunResult(project.project_id, start, end, False, sent, failed, skipped)
        except Exception as exc:  # noqa: BLE001
            logger.exception("knowledge-gap digest generation failed project=%s", project.project_id)
            return DigestRunResult(project.project_id, start, end, dry_run, 0, 1, 0, (str(exc)[:500],))

    def _claim_items(self, digest, recipient_id: str, items, period_start: datetime) -> bool:
        claimed = False
        period_key = period_start.date().isoformat()
        for item in items:
            key = f"knowledge-gap-digest:{digest.project.tenant_id}:{digest.project.project_id}:{period_key}:{recipient_id}:{item.gap_id}"
            if self._service.claim_notification(
                key, gap_id=item.gap_id, project=digest.project, recipient_id=recipient_id,
                event_type="knowledge_gap_digest", period_key=period_key, at=datetime.now(timezone.utc),
            ):
                claimed = True
        return claimed

    def _run_loop(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                self.run_once()
            except Exception:  # noqa: BLE001
                logger.exception("knowledge-gap digest runner crashed")
