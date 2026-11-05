"""Snapshots router: read-only snapshot detail (immutability: no update APIs)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.app.api.serializers import to_snapshot_read
from backend.app.core.exceptions import SnapshotNotFound, SourceNotFound
from backend.app.core.logging import get_logger, safe_preview
from backend.app.db.base import get_db
from backend.app.db.orm import PageSnapshot, Source
from backend.app.models import SnapshotRead

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
