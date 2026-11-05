"""Snapshots router: read-only snapshot detail (immutability: no update APIs)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.api.serializers import to_fact_read, to_prompt_run_read, to_snapshot_read
from backend.app.core.exceptions import SnapshotNotFound, SourceNotFound
from backend.app.core.logging import get_logger, safe_preview
from backend.app.db.base import get_db
from backend.app.db.orm import Fact, PageSnapshot, PromptRun, Source
from backend.app.extraction.candidates import run_extraction_for_snapshot
from backend.app.models import ExtractionResultRead, FactRead, PromptRunRead, SnapshotRead

logger = get_logger(__name__)

router = APIRouter(prefix="/api")


def _get_snapshot_or_404(db: Session, snapshot_id: int) -> PageSnapshot:
    snapshot = db.get(PageSnapshot, snapshot_id)
    if snapshot is None:
        logger.info("api_snapshot_missing id=%d", snapshot_id)
        raise SnapshotNotFound(f"no page snapshot with id {snapshot_id}", details={"snapshot_id": snapshot_id})
    return snapshot


@router.get("/snapshots/{snapshot_id}", response_model=SnapshotRead, tags=["snapshots"])
def get_snapshot(snapshot_id: int, db: Session = Depends(get_db)) -> dict:
    snapshot = _get_snapshot_or_404(db, snapshot_id)
    source = db.get(Source, snapshot.source_id)
    if source is None:
        raise SourceNotFound(f"snapshot {snapshot_id} has no parent source", details={"snapshot_id": snapshot_id})
    logger.info(
        "api_snapshot_read id=%d source_id=%d hash=%s title=%s",
        snapshot.id, snapshot.source_id, snapshot.content_hash[:12],
        safe_preview(snapshot.title or "", 40),
    )
    return to_snapshot_read(snapshot, source)


@router.post("/snapshots/{snapshot_id}/extract", response_model=ExtractionResultRead, tags=["extraction"])
def extract_snapshot(
    snapshot_id: int,
    force: bool = Query(default=False, description="Force re-extraction even if identical hash was previously run"),
    db: Session = Depends(get_db),
) -> dict:
    snapshot = _get_snapshot_or_404(db, snapshot_id)
    summary = run_extraction_for_snapshot(db, snapshot, force=force)
    msg = (
        f"Idempotent reuse of {summary.total_candidates} existing candidates"
        if summary.is_idempotent_skip
        else f"Extracted {summary.total_candidates} candidates ({summary.accepted_candidates} accepted, {summary.needs_review_candidates} needs review, {summary.rejected_candidates} rejected)"
    )
    return {
        "snapshot_id": summary.snapshot_id,
        "prompt_run_id": summary.prompt_run_id,
        "is_idempotent_skip": summary.is_idempotent_skip,
        "parse_ok": summary.parse_ok,
        "total_candidates": summary.total_candidates,
        "accepted_candidates": summary.accepted_candidates,
        "needs_review_candidates": summary.needs_review_candidates,
        "rejected_candidates": summary.rejected_candidates,
        "latency_ms": summary.latency_ms,
        "model_tag": summary.model_tag,
        "prompt_version": summary.prompt_version,
        "message": msg,
    }


@router.get("/snapshots/{snapshot_id}/facts", response_model=list[FactRead], tags=["facts"])
def get_snapshot_facts(snapshot_id: int, db: Session = Depends(get_db)) -> list[dict]:
    snapshot = _get_snapshot_or_404(db, snapshot_id)
    facts = db.scalars(select(Fact).where(Fact.snapshot_id == snapshot.id).order_by(Fact.id.asc())).all()
    return [to_fact_read(f) for f in facts]


@router.get("/snapshots/{snapshot_id}/prompt-runs", response_model=list[PromptRunRead], tags=["prompt-runs"])
def get_snapshot_prompt_runs(snapshot_id: int, db: Session = Depends(get_db)) -> list[dict]:
    snapshot = _get_snapshot_or_404(db, snapshot_id)
    runs = db.scalars(
        select(PromptRun).where(PromptRun.snapshot_id == snapshot.id).order_by(PromptRun.id.desc())
    ).all()
    return [to_prompt_run_read(r) for r in runs]

