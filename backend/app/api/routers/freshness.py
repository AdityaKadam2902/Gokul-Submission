"""Information freshness & monitoring policy API router (ARCHITECTURE §6.1, D46).

Guarantees:
    - 4-Dimensional Freshness: Distinguishes observation, content, fact, and evidence verification currency.
    - No crawl/fact confusion: A recent crawl does NOT make an undated fact fresh.
    - Deterministic evaluation: Computes classifications against explicit policy thresholds.
    - Read-only queries: Freshness queries never mutate stored facts or observations.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.app.api.serializers import (
    to_fact_freshness_read,
    to_freshness_policy_read,
    to_organisation_freshness_summary_read,
    to_source_content_freshness_read,
    to_source_observation_freshness_read,
)
from backend.app.core.exceptions import PracticeSignalError
from backend.app.core.logging import get_logger
from backend.app.db.base import get_db
from backend.app.db.orm import Fact, Organisation, Source
from backend.app.intelligence.freshness_engine import (
    DEFAULT_FRESHNESS_POLICY,
    evaluate_fact_freshness,
    evaluate_organisation_freshness_summary,
    evaluate_source_content_freshness,
    evaluate_source_observation_freshness,
)
from backend.app.intelligence.schemas import FreshnessPolicy
from backend.app.models import (
    FactFreshnessRead,
    FreshnessEvaluateRequest,
    FreshnessPolicyRead,
    OrganisationFreshnessSummaryRead,
    SourceContentFreshnessRead,
    SourceObservationFreshnessRead,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/api/freshness", tags=["freshness"])


class SourceNotFound(PracticeSignalError):
    http_status = 404
    error_code = "source_not_found"


class FactNotFound(PracticeSignalError):
    http_status = 404
    error_code = "fact_not_found"


class OrganisationNotFound(PracticeSignalError):
    http_status = 404
    error_code = "organisation_not_found"


@router.get("/policy", response_model=FreshnessPolicyRead)
def get_freshness_policy() -> dict:
    """Inspect active monitoring policy configuration and category-specific thresholds."""
    return to_freshness_policy_read(DEFAULT_FRESHNESS_POLICY)


@router.get("/source/{source_id}", response_model=SourceObservationFreshnessRead)
def get_source_freshness(
    source_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """Evaluate observation freshness for a registered source."""
    source = db.get(Source, source_id)
    if not source:
        raise SourceNotFound(f"Source with id {source_id} does not exist")

    result = evaluate_source_observation_freshness(session=db, source_id=source_id)
    return to_source_observation_freshness_read(result)


@router.get("/source/{source_id}/content", response_model=SourceContentFreshnessRead)
def get_source_content_freshness_endpoint(
    source_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """Evaluate content observation freshness for a registered source."""
    source = db.get(Source, source_id)
    if not source:
        raise SourceNotFound(f"Source with id {source_id} does not exist")

    result = evaluate_source_content_freshness(session=db, source_id=source_id)
    return to_source_content_freshness_read(result)


@router.get("/fact/{fact_id}", response_model=FactFreshnessRead)
def get_fact_freshness(
    fact_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """Evaluate 4-dimensional freshness for a specific stored fact."""
    fact = db.get(Fact, fact_id)
    if not fact:
        raise FactNotFound(f"Fact with id {fact_id} does not exist")

    result = evaluate_fact_freshness(session=db, fact_id=fact_id)
    return to_fact_freshness_read(result)


@router.get("/organisation/{organisation_id}", response_model=OrganisationFreshnessSummaryRead)
def get_organisation_freshness(
    organisation_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """Evaluate freshness metrics across all confirmed sources and linked facts for an organisation."""
    org = db.get(Organisation, organisation_id)
    if not org:
        raise OrganisationNotFound(f"Organisation with id {organisation_id} does not exist")

    summary = evaluate_organisation_freshness_summary(session=db, organisation_id=organisation_id)
    return to_organisation_freshness_summary_read(summary)


@router.post("/evaluate", response_model=dict[str, Any])
def evaluate_freshness(
    req: FreshnessEvaluateRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Execute dynamic freshness evaluation with optional reference-time injection and threshold overrides."""
    # Build custom policy if overrides supplied
    pol = DEFAULT_FRESHNESS_POLICY
    if req.category_thresholds_override or req.source_observation_threshold_override:
        thresholds = dict(DEFAULT_FRESHNESS_POLICY.category_thresholds_days)
        if req.category_thresholds_override:
            thresholds.update(req.category_thresholds_override)
        obs_thresh = req.source_observation_threshold_override or (
            req.category_thresholds_override.get("source_observation")
            if req.category_thresholds_override else None
        ) or (
            req.category_thresholds_override.get("operational")
            if req.category_thresholds_override else None
        ) or DEFAULT_FRESHNESS_POLICY.source_observation_threshold_days

        pol = FreshnessPolicy(
            policy_id=req.policy_id or "custom_override",
            version="1.0.0-override",
            description="Custom overridden freshness policy",
            source_observation_threshold_days=obs_thresh,
            category_thresholds_days=thresholds,
            aging_threshold_ratio=DEFAULT_FRESHNESS_POLICY.aging_threshold_ratio,
        )

    ref_time: datetime | None = None
    if req.reference_time:
        try:
            ref_time = datetime.fromisoformat(req.reference_time)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid ISO reference_time: {exc}")

    if req.fact_id is not None:
        fact = db.get(Fact, req.fact_id)
        if not fact:
            raise FactNotFound(f"Fact with id {req.fact_id} does not exist")
        res = evaluate_fact_freshness(session=db, fact_id=req.fact_id, policy=pol, reference_time=ref_time)
        return {"type": "fact", "data": to_fact_freshness_read(res)}

    if req.source_id is not None:
        source = db.get(Source, req.source_id)
        if not source:
            raise SourceNotFound(f"Source with id {req.source_id} does not exist")
        res = evaluate_source_observation_freshness(session=db, source_id=req.source_id, policy=pol, reference_time=ref_time)
        return {"type": "source", "data": to_source_observation_freshness_read(res)}

    if req.organisation_id is not None:
        org = db.get(Organisation, req.organisation_id)
        if not org:
            raise OrganisationNotFound(f"Organisation with id {req.organisation_id} does not exist")
        summary = evaluate_organisation_freshness_summary(session=db, organisation_id=req.organisation_id, policy=pol, reference_time=ref_time)
        return {"type": "organisation", "data": to_organisation_freshness_summary_read(summary)}

    raise HTTPException(status_code=400, detail="Must provide at least one of: fact_id, source_id, organisation_id")
