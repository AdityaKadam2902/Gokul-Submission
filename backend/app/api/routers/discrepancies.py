"""Discrepancies router: cross-source discrepancy detection and inspection (ARCHITECTURE §6.1, DECISION_LOG D41).

Guarantees:
    - Evidence traceability: discrepancies preserve references to both facts, sources, and verified quotes.
    - No auto-resolution: PracticeSignal flags review opportunities; it never automatically selects a winning source.
    - Deterministic detection: strictly evaluates eligible distinct-source facts for confirmed organisations.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.api.serializers import to_discrepancy_detail_read, to_discrepancy_read
from backend.app.core.exceptions import PracticeSignalError
from backend.app.core.logging import get_logger
from backend.app.db.base import get_db
from backend.app.db.orm import Discrepancy as DiscrepancyORM, Organisation
from backend.app.intelligence.discrepancy_detector import scan_organisation_discrepancies
from backend.app.models import (
    DiscrepancyDetailRead,
    DiscrepancyRead,
    DiscrepancyScanResultRead,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["discrepancies"])


class DiscrepancyNotFound(PracticeSignalError):
    http_status = 404
    error_code = "discrepancy_not_found"


class OrganisationNotFound(PracticeSignalError):
    http_status = 404
    error_code = "organisation_not_found"


@router.get("/discrepancies", response_model=list[DiscrepancyRead])
def list_discrepancies(
    organisation_id: int | None = Query(default=None, description="Filter by organisation ID"),
    conflict_type: str | None = Query(default=None, description="Filter by conflict type"),
    status: str | None = Query(default=None, description="Filter by status"),
    field_key: str | None = Query(default=None, description="Filter by field key"),
    limit: int = Query(default=50, ge=1, le=200, description="Page limit"),
    offset: int = Query(default=0, ge=0, description="Page offset"),
    db: Session = Depends(get_db),
) -> list[dict]:
    """List detected cross-source discrepancies with optional filtering."""
    query = select(DiscrepancyORM)

    if organisation_id is not None:
        query = query.where(DiscrepancyORM.organisation_id == organisation_id)
    if conflict_type:
        query = query.where(DiscrepancyORM.conflict_type == conflict_type)
    if status:
        query = query.where(DiscrepancyORM.status == status)
    if field_key:
        query = query.where(DiscrepancyORM.field_key == field_key)

    query = query.order_by(DiscrepancyORM.detected_at.desc(), DiscrepancyORM.id.asc())
    query = query.limit(limit).offset(offset)

    results = db.scalars(query).all()
    return [to_discrepancy_read(d) for d in results]


@router.get("/discrepancies/{discrepancy_id}", response_model=DiscrepancyDetailRead)
def get_discrepancy(
    discrepancy_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """Retrieve detailed discrepancy record with both conflicting facts, sources, and evidence."""
    discrepancy = db.get(DiscrepancyORM, discrepancy_id)
    if not discrepancy:
        raise DiscrepancyNotFound(
            f"Discrepancy {discrepancy_id} not found",
            details={"discrepancy_id": discrepancy_id},
        )

    return to_discrepancy_detail_read(discrepancy)


@router.post("/discrepancies/scan/{organisation_id}", response_model=DiscrepancyScanResultRead)
def trigger_organisation_scan(
    organisation_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """Trigger discrepancy detection scan across all confirmed sources for an organisation."""
    org = db.get(Organisation, organisation_id)
    if not org:
        raise OrganisationNotFound(
            f"Organisation {organisation_id} not found",
            details={"organisation_id": organisation_id},
        )

    scan_res = scan_organisation_discrepancies(db, organisation_id)
    db.commit()

    return {
        "organisation_id": scan_res.organisation_id,
        "total_facts_scanned": scan_res.total_facts_scanned,
        "comparisons_evaluated": scan_res.comparisons_evaluated,
        "discrepancies_detected": scan_res.discrepancies_detected,
        "discrepancies": [to_discrepancy_read(d) for d in scan_res.discrepancies],
    }
