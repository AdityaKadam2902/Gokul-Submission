"""Healthcare practice lifecycle intelligence router (ARCHITECTURE §6.1, M3.7).

Endpoints for:
    - Listing detected practice lifecycle events (closures, relocations, corporate acquisitions).
    - Detailed single lifecycle event inspection with verbatim evidence and snapshots.
    - Organisation-level and branch-specific lifecycle histories.
    - Narrowly scoped lifecycle detection scanning.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from backend.app.core.exceptions import PracticeSignalError
from backend.app.core.logging import get_logger
from backend.app.db.base import get_db
from backend.app.db.orm import (
    Organisation,
    PracticeLifecycleEvent as PracticeLifecycleEventORM,
    Source,
)
from backend.app.intelligence.practice_lifecycle_detector import detect_practice_lifecycle_events
from backend.app.intelligence.schemas import (
    LifecycleScanResult,
    PracticeLifecycleEventRead,
    TemporalPrecision,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/api/lifecycle", tags=["lifecycle"])


class LifecycleEventNotFound(PracticeSignalError):
    http_status = 404
    error_code = "lifecycle_event_not_found"


class OrganisationNotFound(PracticeSignalError):
    http_status = 404
    error_code = "organisation_not_found"


class SourceNotFound(PracticeSignalError):
    http_status = 404
    error_code = "source_not_found"


class LifecycleScanRequest(BaseModel):
    source_id: int | None = None
    organisation_id: int | None = None


def _to_lifecycle_event_read(evt: PracticeLifecycleEventORM) -> PracticeLifecycleEventRead:
    meta = evt.metadata_json or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}

    precision_val = TemporalPrecision.UNKNOWN
    if evt.date_precision:
        try:
            precision_val = TemporalPrecision(evt.date_precision)
        except Exception:
            precision_val = TemporalPrecision.UNKNOWN

    return PracticeLifecycleEventRead(
        id=evt.id,
        organisation_id=evt.organisation_id,
        organisation_name=evt.organisation.name if evt.organisation else None,
        branch_locality=evt.branch_locality,
        is_branch_level=evt.is_branch_level,
        event_type=evt.event_type,  # type: ignore[arg-type]
        status=evt.status,  # type: ignore[arg-type]
        source_id=evt.source_id,
        source_url=evt.source.url if evt.source else None,
        snapshot_id=evt.snapshot_id,
        evidence_id=evt.evidence_id,
        evidence_quote=evt.evidence_quote,
        effective_date=evt.effective_date,
        date_precision=precision_val,
        old_value=evt.old_value,
        new_value=evt.new_value,
        acquiring_organisation_id=evt.acquiring_organisation_id,
        acquiring_organisation_name=evt.acquiring_organisation.name if evt.acquiring_organisation else None,
        acquiring_entity_name=evt.acquiring_entity_name,
        rationale=evt.rationale,
        metadata_json=meta,
        review_status=evt.review_status,
        reviewed_by=evt.reviewed_by,
        reviewed_at=evt.reviewed_at,
        idempotency_key=evt.idempotency_key,
        detected_at=evt.detected_at,
        created_at=evt.created_at,
        updated_at=evt.updated_at,
    )


@router.get("/events", response_model=list[PracticeLifecycleEventRead])
def list_lifecycle_events(
    organisation_id: int | None = Query(default=None, description="Filter by organisation ID"),
    source_id: int | None = Query(default=None, description="Filter by source ID"),
    event_type: str | None = Query(default=None, description="Filter by event type (permanent_closure, relocation, etc.)"),
    status: str | None = Query(default=None, description="Filter by confidence status"),
    review_status: str | None = Query(default=None, description="Filter by review status (pending, accepted, rejected)"),
    is_branch_level: bool | None = Query(default=None, description="Filter branch vs organisation events"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db),
) -> list[PracticeLifecycleEventRead]:
    """List detected practice lifecycle events with rich filters and pagination."""
    q = select(PracticeLifecycleEventORM).order_by(desc(PracticeLifecycleEventORM.id))

    if organisation_id is not None:
        q = q.where(PracticeLifecycleEventORM.organisation_id == organisation_id)
    if source_id is not None:
        q = q.where(PracticeLifecycleEventORM.source_id == source_id)
    if event_type:
        q = q.where(PracticeLifecycleEventORM.event_type == event_type)
    if status:
        q = q.where(PracticeLifecycleEventORM.status == status)
    if review_status:
        q = q.where(PracticeLifecycleEventORM.review_status == review_status)
    if is_branch_level is not None:
        q = q.where(PracticeLifecycleEventORM.is_branch_level == is_branch_level)

    events = session.execute(q.limit(limit).offset(offset)).scalars().all()
    return [_to_lifecycle_event_read(e) for e in events]


@router.get("/events/{id}", response_model=PracticeLifecycleEventRead)
def get_lifecycle_event(
    id: int,
    session: Session = Depends(get_db),
) -> PracticeLifecycleEventRead:
    """Retrieve detailed lifecycle event information including citation quote and metadata."""
    evt = session.get(PracticeLifecycleEventORM, id)
    if not evt:
        raise LifecycleEventNotFound(message=f"PracticeLifecycleEvent id={id} does not exist", details={"id": id})
    return _to_lifecycle_event_read(evt)


@router.get("/organisations/{org_id}/history", response_model=list[PracticeLifecycleEventRead])
def get_organisation_lifecycle_history(
    org_id: int,
    session: Session = Depends(get_db),
) -> list[PracticeLifecycleEventRead]:
    """Retrieve complete chronological lifecycle event history for a practice organisation."""
    org = session.get(Organisation, org_id)
    if not org:
        raise OrganisationNotFound(message=f"Organisation id={org_id} does not exist", details={"org_id": org_id})

    q = (
        select(PracticeLifecycleEventORM)
        .where(PracticeLifecycleEventORM.organisation_id == org_id)
        .order_by(desc(PracticeLifecycleEventORM.created_at), desc(PracticeLifecycleEventORM.id))
    )
    events = session.execute(q).scalars().all()
    return [_to_lifecycle_event_read(e) for e in events]


@router.get("/organisations/{org_id}/branches/{branch_locality}/history", response_model=list[PracticeLifecycleEventRead])
def get_branch_lifecycle_history(
    org_id: int,
    branch_locality: str,
    session: Session = Depends(get_db),
) -> list[PracticeLifecycleEventRead]:
    """Retrieve branch-specific lifecycle history for a physical clinic location."""
    org = session.get(Organisation, org_id)
    if not org:
        raise OrganisationNotFound(message=f"Organisation id={org_id} does not exist", details={"org_id": org_id})

    q = (
        select(PracticeLifecycleEventORM)
        .where(
            PracticeLifecycleEventORM.organisation_id == org_id,
            PracticeLifecycleEventORM.branch_locality.ilike(branch_locality),
        )
        .order_by(desc(PracticeLifecycleEventORM.created_at), desc(PracticeLifecycleEventORM.id))
    )
    events = session.execute(q).scalars().all()
    return [_to_lifecycle_event_read(e) for e in events]


@router.post("/scan", response_model=LifecycleScanResult)
def scan_lifecycle_events(
    payload: LifecycleScanRequest | None = None,
    session: Session = Depends(get_db),
) -> LifecycleScanResult:
    """Trigger a deterministic practice lifecycle scan for a source or organisation."""
    src_id = payload.source_id if payload else None
    org_id = payload.organisation_id if payload else None

    if src_id is not None:
        src = session.get(Source, src_id)
        if not src:
            raise SourceNotFound(message=f"Source id={src_id} does not exist", details={"source_id": src_id})

    if org_id is not None:
        org = session.get(Organisation, org_id)
        if not org:
            raise OrganisationNotFound(message=f"Organisation id={org_id} does not exist", details={"organisation_id": org_id})

    existing_count_before = session.execute(select(PracticeLifecycleEventORM)).scalars().all()
    events = detect_practice_lifecycle_events(session, source_id=src_id, organisation_id=org_id)

    by_type = Counter(e.event_type for e in events)
    by_status = Counter(e.status for e in events)

    return LifecycleScanResult(
        source_id=src_id,
        organisation_id=org_id,
        events_detected=len(events),
        by_type=dict(by_type),
        by_status=dict(by_status),
        new_events_count=len(events),
        existing_events_count=len(existing_count_before),
    )
