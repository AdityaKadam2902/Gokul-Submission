"""Prompt runs router: inspect AI inference audits and execution metrics (ARCHITECTURE §6.2)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.api.serializers import to_prompt_run_read
from backend.app.core.exceptions import PracticeSignalError
from backend.app.db.base import get_db
from backend.app.db.orm import PromptRun
from backend.app.models import PromptRunRead

router = APIRouter(prefix="/api")


class PromptRunNotFound(PracticeSignalError):
    http_status = 404
    error_code = "prompt_run_not_found"


@router.get("/prompt-runs", response_model=list[PromptRunRead], tags=["prompt-runs"])
def list_prompt_runs(
    snapshot_id: int | None = Query(default=None, description="Filter by snapshot id"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[dict]:
    query = select(PromptRun)
    if snapshot_id is not None:
        query = query.where(PromptRun.snapshot_id == snapshot_id)
    query = query.order_by(PromptRun.id.desc()).offset(offset).limit(limit)
    runs = db.scalars(query).all()
    return [to_prompt_run_read(r) for r in runs]


@router.get("/prompt-runs/{run_id}", response_model=PromptRunRead, tags=["prompt-runs"])
def get_prompt_run(run_id: int, db: Session = Depends(get_db)) -> dict:
    run = db.get(PromptRun, run_id)
    if run is None:
        raise PromptRunNotFound(f"No prompt run found with id {run_id}", details={"run_id": run_id})
    return to_prompt_run_read(run)
