"""Health router: app/DB status; Ollama is configured-but-unused in M1.

ARCHITECTURE §6.2 requires the health endpoint to report app, DB, Ollama
and robots-tool status. M1 performs no network probes whatsoever — the
Ollama entry reflects configuration only (local adapter lands in M2) and
the robots tool reports stdlib availability. This keeps /api/health safe,
instant and dependency-free.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.app import __version__
from backend.app.ai.client import get_ollama_client
from backend.app.core.config import get_settings
from backend.app.db.base import get_db
from backend.app.models import HealthStatus

import urllib.robotparser  # stdlib — reported for ARCHITECTURE §6.2 parity

router = APIRouter(prefix="/api")


@router.get("/health", response_model=HealthStatus, tags=["health"])
def health(db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    database_ok = True
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        database_ok = False

    ollama_client = get_ollama_client()
    ollama_probe = ollama_client.check_health()
    ollama_ok = ollama_probe.get("available", False) and ollama_probe.get("model_present", False)
    if ollama_ok:
        ollama_msg = f"connected ({settings.ollama_url}) model={settings.ollama_model} available (not used in M1; active in M2)"
    elif ollama_probe.get("available", False):
        ollama_msg = f"connected ({settings.ollama_url}) model={settings.ollama_model} NOT FOUND (not used in M1; active in M2)"
    else:
        ollama_msg = f"unreachable ({settings.ollama_url}): {ollama_probe.get('error', 'offline')} (not used in M1; active in M2)"

    overall_status = "ok" if (database_ok and ollama_ok) else "degraded"

    return {
        "status": overall_status,
        "app_ok": True,
        "database_ok": database_ok,
        "database_path": str(settings.database_file),
        "ollama": ollama_msg,
        "ollama_ok": ollama_ok,
        "robots_tool": "available (urllib.robotparser, stdlib) — used only after a future manual source approval",
        "version": __version__,
    }
