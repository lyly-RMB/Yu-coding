"""Operational status endpoint for Feishu deployment checks."""

from __future__ import annotations

from fastapi import APIRouter, Request

from project_lens.config import settings

router = APIRouter()


@router.get("/integrations/feishu/status", tags=["feishu"])
def feishu_status(request: Request) -> dict[str, object]:
    run_service = getattr(request.app.state, "run_service", None)
    hermes_base_url = settings.feishu_hermes_base_url or settings.model_openai_base_url
    hermes_api_key = settings.feishu_hermes_api_key or settings.model_openai_api_key
    return {
        "configured": bool(request.app.state.feishu_credentials_configured),
        "outbound_mode": request.app.state.feishu_outbound_mode,
        "binding_count": request.app.state.feishu_binding_count,
        "webhook_path": "/api/v1/feishu/events",
        "agent_mode": getattr(run_service, "agent_mode", settings.agent_mode),
        "ask_agent_mode": "hermes",
        "model_provider": settings.model_provider,
        "model_live": settings.model_live,
        "model_name": settings.model_name,
        "model_openai_model": settings.model_openai_model,
        "model_fallback_to_stub": settings.model_fallback_to_stub,
        # Never expose API keys — only whether a key is configured locally.
        "model_openai_api_key_configured": bool(
            (settings.model_openai_api_key or "").strip()
        ),
        "hermes_tool_loop_enabled": True,
        "hermes_loop_timeout_seconds": settings.feishu_hermes_loop_timeout_seconds,
        "hermes_bridge_active": (
            getattr(request.app.state, "feishu_hermes_tool_loop_bridge", None) is not None
        ),
        "hermes_provider": settings.feishu_hermes_provider,
        "hermes_model": settings.feishu_hermes_model,
        "hermes_base_url_configured": bool((hermes_base_url or "").strip()),
        "hermes_api_key_configured": bool((hermes_api_key or "").strip()),
        "hermes_advanced_tools_enabled": settings.feishu_hermes_advanced_tools,
        "hermes_risk_review_enabled": settings.feishu_hermes_risk_review_enabled,
        "hermes_risk_review_interval_seconds": settings.feishu_hermes_risk_review_interval_seconds,
        "hermes_risk_scheduler_running": bool(
            getattr(getattr(request.app.state, "hermes_risk_scheduler", None), "running", False)
        ),
        "hermes_risk_review_last_results": [
            {
                "project_id": result.project.project_id,
                "reviewed_risk_ids": list(result.reviewed_risk_ids),
                "skipped_risk_ids": list(result.skipped_risk_ids),
            }
            for result in getattr(
                getattr(request.app.state, "hermes_risk_scheduler", None),
                "last_results",
                (),
            )
        ],
        "service_auth_configured": bool((settings.service_token or "").strip()),
        "knowledge_gap_digest_enabled": settings.feishu_knowledge_gap_digest_enabled,
        "knowledge_gap_digest_interval_seconds": settings.feishu_knowledge_gap_digest_interval_seconds,
        "knowledge_gap_digest_timezone": settings.feishu_knowledge_gap_digest_timezone,
        "knowledge_gap_digest_running": bool(
            getattr(getattr(request.app.state, "knowledge_gap_digest_runner", None), "running", False)
        ),
        "knowledge_gap_digest_last_results": [
            {
                "project_id": result.project_id,
                "period_start": result.period_start.isoformat(),
                "period_end": result.period_end.isoformat(),
                "sent": result.sent,
                "failed": result.failed,
                "skipped": result.skipped,
                "dry_run": result.dry_run,
            }
            for result in getattr(
                getattr(request.app.state, "knowledge_gap_digest_runner", None),
                "last_results",
                (),
            )
            ],
        "long_connection_enabled": bool(
            getattr(getattr(request.app.state, "feishu_long_connection", None), "enabled", False)
        ),
        "long_connection_status": getattr(
            getattr(request.app.state, "feishu_long_connection", None), "status", "unconfigured"
        ),
        "long_connection_last_connected_at": (
            getattr(getattr(request.app.state, "feishu_long_connection", None), "last_connected_at", None).isoformat()
            if getattr(getattr(request.app.state, "feishu_long_connection", None), "last_connected_at", None)
            else None
        ),
        "long_connection_last_error": getattr(
            getattr(request.app.state, "feishu_long_connection", None), "last_error", None
        ),
        "context_preview_enabled": settings.feishu_context_preview_enabled,
    }
