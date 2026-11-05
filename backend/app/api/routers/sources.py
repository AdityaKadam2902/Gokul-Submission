"""Sources router: registration, listing, detail, update, deletion + ingestion.

Contracts follow ARCHITECTURE §6.2; error payloads are normalised by the
app-level exception handler. `approved_live` policy state cannot be set
through this API (see docs/SOURCE_REGISTER.md §3 for the manual procedure).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from backend.app.api.serializers import to_snapshot_head, to_snapshot_read, to_source_read
from backend.app.core.exceptions import PracticeSignalError
from backend.app.core.logging import correlation_id
from backend.app.core.logging import get_logger
from backend.app.db.base import get_db
from backend.app.ingest.pipeline import run_ingestion
from backend.app.ingest.snapshot_store import latest_snapshot
from backend.app.models import (
    IngestionResult,
    SnapshotRead,
    SourceCreate,
    SourceDetailRead,
    SourcePatch,
    SourceRead,
)
from backend.app.sources import registry

logger = get_logger(__name__)

router = APIRouter(prefix="/api")


@router.post("/sources", response_model=SourceRead, status_code=201, tags=["sources"])
def create_source(payload: SourceCreate, db: Session = Depends(get_db)) -> dict:
    source = registry.create_source(db, payload)
    db.commit()
    logger.info("api_source_created id=%d correlation=%s", source.id, correlation_id.get())
    return to_source_read(source)


@router.get("/sources", response_model=list[SourceRead], tags=["sources"])
def list_sources(db: Session = Depends(get_db)) -> list[dict]:
    return [to_source_read(source) for source in registry.list_sources(db)]


@router.get("/sources/{source_id}", response_model=SourceDetailRead, tags=["sources"])
def get_source(source_id: int, db: Session = Depends(get_db)) -> dict:
    source = registry.get_source_or_404(db, source_id)
    data = to_source_read(source)
    data["snapshots"] = [to_snapshot_head(s) for s in source.snapshots]
    return data


@router.patch("/sources/{source_id}", response_model=SourceRead, tags=["sources"])
def update_source(source_id: int, payload: SourcePatch, db: Session = Depends(get_db)) -> dict:
    source = registry.update_source(db, source_id, payload)
    db.commit()
    return to_source_read(source)


@router.delete("/sources/{source_id}", status_code=204, tags=["sources"])
def delete_source(source_id: int, db: Session = Depends(get_db)) -> Response:
    registry.delete_source(db, source_id)
    db.commit()
    return Response(status_code=204)


@router.post("/ingestions/{source_id}", response_model=IngestionResult, tags=["ingestions"])
def run_ingestion_endpoint(source_id: int, db: Session = Depends(get_db)) -> dict:
    source = registry.get_source_or_404(db, source_id)
    outcome = run_ingestion(db, source)
    db.commit()
    message = (
        "content unchanged: existing snapshot reused (hash short-circuit)"
        if outcome.unchanged
        else "snapshot created and persisted"
    )
    logger.info(
        "ingestion_api source_id=%d unchanged=%s snapshot_id=%s",
        source_id, outcome.unchanged, outcome.snapshot_id,
    )
    return {
        "snapshot_id": outcome.snapshot_id,
        "unchanged": outcome.unchanged,
        "previous_snapshot_id": outcome.previous_snapshot_id,
        "content_hash": outcome.content_hash,
        "message": message,
    }
