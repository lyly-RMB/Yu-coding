"""Feishu callback routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Request

from project_lens.integrations.feishu.service import FeishuEventService

router = APIRouter()
logger = logging.getLogger(__name__)


def get_feishu_service(request: Request) -> FeishuEventService:
    return request.app.state.feishu_event_service


@router.post("/feishu/events", tags=["feishu"])
async def receive_feishu_event(
    request: Request,
    background_tasks: BackgroundTasks,
    service: FeishuEventService = Depends(get_feishu_service),
) -> dict:
    body = await request.body()
    logger.info(
        "feishu callback received bytes=%d has_signature=%s",
        len(body),
        bool(request.headers.get("X-Lark-Signature")),
    )
    result = service.handle_callback(
        body=body,
        timestamp=request.headers.get("X-Lark-Request-Timestamp"),
        nonce=request.headers.get("X-Lark-Request-Nonce"),
        signature=request.headers.get("X-Lark-Signature"),
        background_tasks=background_tasks,
    )
    return result.to_response()
