"""Temporal change events router (ARCHITECTURE §6.1, DECISION_LOG D42).

Guarantees:
    - Historical preservation: Change events are append-only and immutable.
    - Full traceability: Every change references previous/current observations, snapshots, facts, and evidence.
    - No automated correction: Flags detected changes without overwriting or deleting historical facts.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.api.serializers import to_change_event_detail_read, to_change_event_read
from backend.app.core.exceptions import PracticeSignalError
from backend.app.core.logging import get_logger
from backend.app.db.base import get_db
from backend.app.db.orm import ChangeEvent as ChangeEventORM, Organisation, Source
from backend.app.intelligence.change_detector import (
    scan_organisation_changes,
    scan_source_changes,
)
from backend.app.models import (
    ChangeEventDetailRead,
    ChangeEventRead,
    ChangeScanResultRead,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["changes"])


class ChangeEventNotFound(PracticeSignalError):
    http_status = 404
    error_code = "change_event_not_found"


class SourceNotFound(PracticeSignalError):
    http_status = 404
    error_code = "source_not_found"


class OrganisationNotFound(PracticeSignalError):
    http_status = 404
    error_code = "organisation_not_found"


@router.get("/changes", response_model=list[ChangeEventRead])
def list_change_events(
    source_id: int | None = Query(default=None, description="Filter by registered source ID"),
    organisation_id: int | None = Query(default=None, description="Filter by organisation ID"),
    event_type: str | None = Query(default=None, description="Filter by event type"),
    field_key: str | None = Query(default=None, description="Filter by field key"),
    limit: int = Query(default=50, ge=1, le=200, description="Page limit"),
    offset: int = Query(default=0, ge=0, description="Page offset"),
    db: Session = Depends(get_db),
) -> list[dict]:
    """List detected temporal change events with optional filtering."""
    query = select(ChangeEventORM)

    if source_id is not None:
        query = query.where(ChangeEventORM.source_id == source_id)
    if organisation_id is not None:
        query = query.where(ChangeEventORM.organisation_id == organisation_id)
    if event_type:
        query = query.where(ChangeEventORM.event_type == event_type)
    if field_key:
        query = query.where(ChangeEventORM.field_key == field_key)

    query = query.order_by(ChangeEventORM.detected_at.desc(), ChangeEventORM.id.asc())
    query = query.limit(limit).offset(offset)

    results = db.scalars(query).all()
    return [to_change_event_read(e) for e in results]


@router.get("/changes/{change_id}", response_model=ChangeEventDetailRead)
def get_change_event(
    change_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """Retrieve detailed temporal change event with linked facts, snapshots, and sources."""
    event = db.get(ChangeEventORM, change_id)
    if not event:
        raise ChangeEventNotFound(
            f"Change event {change_id} not found",
            details={"change_id": change_id},
        )

    return to_change_event_detail_read(event)


@router.post("/changes/scan/source/{source_id}", response_model=ChangeScanResultRead)
def trigger_source_change_scan(
    source_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """Trigger change detection scan across all observations for a registered source."""
    source = db.get(Source, source_id)
    if not source:
        raise SourceNotFound(
            f"Source {source_id} not found",
            details={"source_id": source_id},
        )

    scan_res = scan_source_changes(db, source_id)
    return {
        "source_id": scan_res.source_id,
        "observations_evaluated": scan_res.observations_evaluated,
        "changes_detected": scan_res.changes_detected,
        "changes": [to_change_event_read(e) for e in scan_res.changes],
    }


@router.post("/changes/scan/organisation/{organisation_id}", response_model=list[ChangeEventRead])
def trigger_organisation_change_scan(
    organisation_id: int,
    db: Session = Depends(get_db),
) -> list[dict]:
    """Trigger change detection across all confirmed sources for an organisation."""
    org = db.get(Organisation, organisation_id)
    if not org:
        raise OrganisationNotFound(
            f"Organisation {organisation_id} not found",
            details={"organisation_id": organisation_id},
        )

    events = scan_organisation_changes(db, organisation_id)
    return [to_change_event_read(e) for e in events]
