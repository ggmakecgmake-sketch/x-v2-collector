"""
Health & metrics API (FastAPI) for the x-v2-collector.
"""
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import FastAPI, APIRouter
from fastapi.responses import JSONResponse

from src.config import settings

router = APIRouter()

# Mutable state populated by main.py
_state: Dict[str, Any] = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "last_fetch": None,
    "last_engine": None,
    "tweets_this_hour": 0,
    "tweets_total": 0,
    "errors_total": 0,
    "current_engine": "twikit",
    "healthy": False,
}


def update_state(key: str, value: Any):
    _state[key] = value


@router.get("/health")
async def health():
    """Service health check."""
    status = "healthy" if _state.get("healthy") else "degraded"
    code = 200 if _state.get("healthy") else 503
    return JSONResponse(
        status_code=code,
        content={
            "status": status,
            "service": settings.app_name,
            "version": "0.1.0",
            "healthy": _state.get("healthy"),
            "current_engine": _state.get("current_engine"),
            "last_fetch": _state.get("last_fetch"),
            "started_at": _state.get("started_at"),
            "tweets_total": _state.get("tweets_total"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


@router.get("/metrics")
async def metrics():
    """Operational metrics."""
    return {
        "tweets_this_hour": _state.get("tweets_this_hour"),
        "tweets_total": _state.get("tweets_total"),
        "errors_total": _state.get("errors_total"),
        "current_engine": _state.get("current_engine"),
        "last_engine": _state.get("last_engine"),
        "healthy": _state.get("healthy"),
    }
