"""Facts router: list and inspect extracted candidate facts and verified evidence (ARCHITECTURE §6.2).

Guarantees:
    - All AI-extracted facts remain candidate / unapproved in M2.
    - Full provenance: every fact includes raw_value, normalized_value,
      evidence quotation, Python-calculated offsets, model tag and prompt version.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.api.serializers import to_fact_read
from backend.app.core.exceptions import PracticeSignalError
from backend.app.db.base import get_db
from backend.app.db.orm import Fact
from backend.app.models import FactRead

router = APIRouter(prefix="/api")


class FactNotFound(PracticeSignalError):
    http_status = 404
    error_code = "fact_not_found"


@router.get("/facts", response_model=list[FactRead], tags=["facts"])
def list_facts(
    status: str | None = Query(default=None, description="Filter by fact status (candidate|needs_review|rejected|reviewed)"),
    field_key: str | None = Query(default=None, description="Filter by field key"),
    snapshot_id: int | None = Query(default=None, description="Filter by snapshot id"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[dict]:
    query = select(Fact)
    if status is not None:
        query = query.where(Fact.status == status)
    if field_key is not None:
        query = query.where(Fact.field_key == field_key)
    if snapshot_id is not None:
        query = query.where(Fact.snapshot_id == snapshot_id)

    query = query.order_by(Fact.id.desc()).offset(offset).limit(limit)
    facts = db.scalars(query).all()
    return [to_fact_read(f) for f in facts]


@router.get("/facts/{fact_id}", response_model=FactRead, tags=["facts"])
def get_fact(fact_id: int, db: Session = Depends(get_db)) -> dict:
    fact = db.get(Fact, fact_id)
    if fact is None:
        raise FactNotFound(f"No fact found with id {fact_id}", details={"fact_id": fact_id})
    return to_fact_read(fact)
