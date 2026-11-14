"""Human Review & Auditable Decision Workflow Router (ARCHITECTURE §6.1, DECISION_LOG D44).

Endpoints:
    - GET /api/reviews/queue: Unified queue across 7 intelligence domains.
    - GET /api/reviews/metrics: Pending, completed, and deferred review counts.
    - GET /api/reviews/{entity_type}/{entity_id}: Detailed inspection context.
    - POST /api/reviews/{entity_type}/{entity_id}/actions: Record review decision.
    - GET /api/reviews/{entity_type}/{entity_id}/history: Specific item decision history.
    - GET /api/reviews/actions: Global audit trail of all review decisions.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.app.api.serializers import to_review_action_read
from backend.app.core.exceptions import PracticeSignalError
from backend.app.core.logging import get_logger
from backend.app.db.base import get_db
from backend.app.intelligence.review_service import (
    get_review_history,
    get_review_item_detail,
    get_review_metrics,
    get_review_queue,
    record_review_action,
)
from backend.app.models import (
    ReviewActionCreate,
    ReviewActionRead,
    ReviewHistoryResponse,
    ReviewItemDetailRead,
    ReviewMetricsRead,
    ReviewQueueResponse,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/api/reviews", tags=["reviews"])


class ReviewItemNotFound(PracticeSignalError):
    http_status = 404
    error_code = "review_item_not_found"


class InvalidReviewAction(PracticeSignalError):
    http_status = 422
    error_code = "invalid_review_action"


@router.get("/queue", response_model=ReviewQueueResponse)
def get_queue(
    entity_type: str | None = Query(None, description="Filter queue by entity type"),
    status: str | None = Query(None, description="Filter queue by status (pending, deferred, completed, all)"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    """Retrieve unified human review queue with grounded evidence and priority."""
    return get_review_queue(
        session=db,
        entity_type=entity_type if isinstance(entity_type, str) else None,
        status=status if isinstance(status, str) else None,
        limit=limit if isinstance(limit, int) else 50,
        offset=offset if isinstance(offset, int) else 0,
    )


@router.get("/metrics", response_model=ReviewMetricsRead)
def get_metrics(db: Session = Depends(get_db)) -> dict:
    """Retrieve review metrics and pending item totals grouped by entity type."""
    return get_review_metrics(session=db)


@router.get("/actions", response_model=ReviewHistoryResponse)
def list_review_actions(
    entity_type: str | None = Query(None, description="Filter history by entity type"),
    entity_id: int | None = Query(None, description="Filter history by entity id"),
    reviewer: str | None = Query(None, description="Filter history by reviewer identifier"),
    action: str | None = Query(None, description="Filter history by action taken"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    """Retrieve global append-only audit trail of human decisions."""
    return get_review_history(
        session=db,
        entity_type=entity_type if isinstance(entity_type, str) else None,
        entity_id=entity_id if isinstance(entity_id, int) else None,
        reviewer=reviewer if isinstance(reviewer, str) else None,
        action=action if isinstance(action, str) else None,
        limit=limit if isinstance(limit, int) else 50,
        offset=offset if isinstance(offset, int) else 0,
    )


@router.get("/{entity_type}/{entity_id}", response_model=ReviewItemDetailRead)
def get_item_detail(
    entity_type: str,
    entity_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """Retrieve comprehensive detail, quotations, and decision history for a reviewable item."""
    try:
        return get_review_item_detail(session=db, entity_type=entity_type, entity_id=entity_id)
    except ValueError as exc:
        raise ReviewItemNotFound(str(exc)) from exc


@router.post("/{entity_type}/{entity_id}/actions", response_model=ReviewActionRead)
def submit_review_action(
    entity_type: str,
    entity_id: int,
    payload: ReviewActionCreate,
    db: Session = Depends(get_db),
) -> dict:
    """Record an immutable, append-only human decision on an intelligence entity."""
    try:
        record = record_review_action(
            session=db,
            entity_type=entity_type,
            entity_id=entity_id,
            action=payload.action,
            reason_code=payload.reason_code,
            reviewer=payload.reviewer,
            notes=payload.notes,
            evidence_id=payload.evidence_id,
            decision_metadata=payload.decision_metadata,
        )
        db.commit()
        return to_review_action_read(record)
    except ValueError as exc:
        err_msg = str(exc)
        if "not found" in err_msg.lower():
            raise ReviewItemNotFound(err_msg) from exc
        raise InvalidReviewAction(err_msg) from exc


@router.get("/{entity_type}/{entity_id}/history", response_model=ReviewHistoryResponse)
def get_item_history(
    entity_type: str,
    entity_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    """Retrieve decision audit history for a specific entity."""
    return get_review_history(
        session=db,
        entity_type=entity_type,
        entity_id=entity_id,
        limit=limit,
        offset=offset,
    )
