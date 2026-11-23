from datetime import datetime, timezone
import math
import re
import time
from typing import Any, Dict, Tuple
from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from app.config import get_settings

router = APIRouter(tags=["Health"])

_URL_USERINFO_RE = re.compile(r"(://)([^@\s/]+)@")


def _redact_sensitive_text(value: Any, max_length: int = 240) -> str:
    """Mask embedded URL credentials and normalize single-line error text for public health endpoints."""
    text = str(value)
    text = _URL_USERINFO_RE.sub(r"\1***@", text)
    text = " ".join(text.splitlines()).strip()
    if len(text) > max_length:
        text = text[: max_length - 3].rstrip() + "..."
    return text


def check_bot_health(request: Request) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Evaluate Discord Bot health status.
    Returns (is_healthy, status_label, details_dict).
    """
    settings = get_settings()
    if not settings.is_discord_bot_configured:
        return True, "disabled", {"configured": False, "status": "disabled"}

    bot = getattr(request.app.state, "discord_bot", None)
    bot_task = getattr(request.app.state, "discord_bot_task", None)
    app_start_time = getattr(request.app.state, "app_start_time", 0.0)

    # 1. Check supervisor task
    if bot_task is None:
        return False, "supervisor_not_started", {
            "configured": True,
            "supervisor_alive": False,
            "ready": False,
            "reason": "Supervisor task has not been created",
        }

    if bot_task.done():
        exc = None
        try:
            exc = bot_task.exception()
        except Exception:
            pass
        return False, "supervisor_dead", {
            "configured": True,
            "supervisor_alive": False,
            "ready": False,
            "reason": (
                f"Supervisor task died: {_redact_sensitive_text(exc)}"
                if exc
                else "Supervisor task terminated"
            ),
        }

    # 2. Check bot ready state
    is_ready = bool(bot and not bot.is_closed() and bot.is_ready())
    if is_ready:
        raw_latency = getattr(bot, "latency", None)
        latency_ms = (
            round(raw_latency * 1000, 1)
            if (
                isinstance(raw_latency, (int, float))
                and not isinstance(raw_latency, bool)
                and math.isfinite(raw_latency)
                and raw_latency >= 0
            )
            else 0.0
        )
        return True, "healthy", {
            "configured": True,
            "supervisor_alive": True,
            "ready": True,
            "closed": False,
            "latency_ms": latency_ms,
            "user": str(bot.user) if (bot and bot.user) else None,
        }

    # 3. Not ready: check startup grace period (20s)
    elapsed_since_start = time.time() - app_start_time if app_start_time else 999.0
    if elapsed_since_start < 20.0:
        return True, "starting", {
            "configured": True,
            "supervisor_alive": True,
            "ready": False,
            "closed": bot.is_closed() if bot else False,
            "status": "connecting",
            "elapsed_seconds": round(elapsed_since_start, 1),
        }

    # 4. Grace period expired and bot is not ready / closed / None
    is_closed = bot.is_closed() if bot else True
    reason = "Bot instance is None" if bot is None else ("Bot is closed" if is_closed else "Bot is not ready")
    details: Dict[str, Any] = {
        "configured": True,
        "supervisor_alive": True,
        "ready": False,
        "closed": is_closed,
        "reason": reason,
    }
    last_error = getattr(request.app.state, "discord_bot_last_error", None)
    if last_error:
        details["last_error"] = _redact_sensitive_text(last_error)
    active_proxy = getattr(request.app.state, "discord_bot_active_proxy", None)
    if active_proxy is not None:
        details["active_proxy"] = _redact_sensitive_text(active_proxy)
    proxy_failures = getattr(request.app.state, "discord_bot_proxy_failures", 0)
    if proxy_failures:
        details["proxy_failures"] = proxy_failures
    return False, "offline", details


@router.get("/healthz")
async def healthcheck(request: Request):
    """Coolify liveness and readiness probe endpoint reporting queue and bot status."""
    queue_service = getattr(request.app.state, "queue_service", None)
    stats = queue_service.stats if queue_service else {}
    is_healthy, _bot_state, bot_details = check_bot_health(request)

    response_payload = {
        "status": "healthy" if is_healthy else "unhealthy",
        "service": "Tradingview_Price_Alert_Notification",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "queue": stats,
        "discord_bot": bot_details,
    }

    if not is_healthy:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=response_payload,
        )

    return response_payload


@router.get("/live")
async def liveness(request: Request):
    """
    High-frequency ping for Docker healthcheck and reverse proxies.
    Returns 200 OK when operational, or 503 Service Unavailable when the Discord bot is offline/dead.
    """
    is_healthy, bot_state, bot_details = check_bot_health(request)
    if not is_healthy:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "degraded", "bot": bot_state, "details": bot_details},
        )
    return {"status": "ok"}
