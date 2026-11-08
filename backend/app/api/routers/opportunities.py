"""Healthcare workforce opportunities router (ARCHITECTURE §6.1, DECISION_LOG D43).

Guarantees:
    - Conservative lifecycle representation: strictly forbids claiming 'filled' or 'hired'.
    - Complete provenance: every opportunity is linked to organisation, source, and snapshots.
    - Historical preservation: never cascade deletes opportunities upon source or org change.
"""

from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.api.serializers import (
    to_opportunity_detail_read,
    to_opportunity_read,
)
from backend.app.core.exceptions import PracticeSignalError
from backend.app.core.logging import get_logger
from backend.app.db.base import get_db
from backend.app.db.orm import (
    Opportunity as OpportunityORM,
    Organisation,
    Source,
)
from backend.app.intelligence.opportunity_detector import (
    scan_organisation_opportunities,
    scan_source_opportunities,
)
from backend.app.models import (
    OpportunityDetailRead,
    OpportunityRead,
    OpportunityScanResultRead,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["opportunities"])


class OpportunityNotFound(PracticeSignalError):
    http_status = 404
    error_code = "opportunity_not_found"


class SourceNotFound(PracticeSignalError):
    http_status = 404
    error_code = "source_not_found"


class OrganisationNotFound(PracticeSignalError):
    http_status = 404
    error_code = "organisation_not_found"


@router.get("/opportunities", response_model=list[OpportunityRead])
def list_opportunities(
    role_category: str | None = Query(default=None, description="Filter by role category (e.g. gp, nurse)"),
    organisation_id: int | None = Query(default=None, description="Filter by organisation ID"),
    source_id: int | None = Query(default=None, description="Filter by source ID"),
    status: str | None = Query(default=None, description="Filter by lifecycle status"),
    limit: int = Query(default=50, ge=1, le=200, description="Page limit"),
    offset: int = Query(default=0, ge=0, description="Page offset"),
    db: Session = Depends(get_db),
) -> list[dict]:
    """List healthcare practice recruitment opportunities with optional filtering."""
    query = select(OpportunityORM)

    if role_category:
        query = query.where(OpportunityORM.role_category == role_category)
    if organisation_id is not None:
        query = query.where(OpportunityORM.organisation_id == organisation_id)
    if source_id is not None:
        query = query.where(OpportunityORM.source_id == source_id)
    if status:
        query = query.where(OpportunityORM.status == status)

    query = query.order_by(OpportunityORM.id.asc()).offset(offset).limit(limit)
    opps = db.execute(query).scalars().all()
    return [to_opportunity_read(opp) for opp in opps]


@router.get("/opportunities/{opportunity_id}", response_model=OpportunityDetailRead)
def get_opportunity(
    opportunity_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """Retrieve detailed information and provenance for a single opportunity."""
    opp = db.get(OpportunityORM, opportunity_id)
    if not opp:
        raise OpportunityNotFound(
            message=f"Opportunity {opportunity_id} not found",
            details={"opportunity_id": opportunity_id},
        )
    return to_opportunity_detail_read(opp)


@router.post("/opportunities/scan/{source_id}", response_model=OpportunityScanResultRead)
def trigger_source_opportunity_scan(
    source_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """Trigger deterministic opportunity lifecycle scan for a registered source."""
    source = db.get(Source, source_id)
    if not source:
        raise SourceNotFound(
            message=f"Source {source_id} not found",
            details={"source_id": source_id},
        )

    # Initial count
    existing_count = db.execute(
        select(OpportunityORM).where(OpportunityORM.source_id == source_id)
    ).scalars().all()
    initial_ids = {o.id for o in existing_count}

    # Run deterministic scan
    opps = scan_source_opportunities(db, source_id)
    db.commit()

    created_count = sum(1 for o in opps if o.id not in initial_ids)
    updated_count = sum(1 for o in opps if o.id in initial_ids)
    status_counts = dict(Counter(o.status for o in opps))

    return {
        "source_id": source_id,
        "opportunities_found": len(opps),
        "opportunities_created": created_count,
        "opportunities_updated": updated_count,
        "status_counts": status_counts,
        "opportunities": [to_opportunity_read(o) for o in opps],
    }
