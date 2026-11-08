"""Organisations router: organisation registry, multi-source links, match candidates, and observation heartbeats (ARCHITECTURE §6.1, D39, D40).

Guarantees:
    - Conservative resolution: all automated match suggestions require operator review.
    - Zero auto-merging: merging endpoints are strictly forbidden in M3.1.
    - Observation heartbeats: chronological ingestion history is queryable per source.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.api.serializers import (
    to_match_candidate_read,
    to_organisation_link_read,
    to_organisation_read,
    to_source_observation_read,
)
from backend.app.core.exceptions import PracticeSignalError
from backend.app.core.logging import get_logger
from backend.app.db.base import get_db
from backend.app.db.orm import (
    Fact,
    Organisation,
    OrganisationMatchCandidate,
    OrganisationSourceLink,
    Source,
    SourceObservation,
)
from backend.app.intelligence.entity_resolver import propose_match_candidate
from backend.app.intelligence.observation_tracker import list_observations
from backend.app.models import (
    OrganisationCreate,
    OrganisationDetailRead,
    OrganisationLinkCreate,
    OrganisationLinkRead,
    OrganisationLinkUpdate,
    OrganisationMatchCandidateRead,
    OrganisationRead,
    SourceObservationRead,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/api")


class OrganisationNotFound(PracticeSignalError):
    http_status = 404
    error_code = "organisation_not_found"


class SourceNotFound(PracticeSignalError):
    http_status = 404
    error_code = "source_not_found"


class OrganisationLinkNotFound(PracticeSignalError):
    http_status = 404
    error_code = "organisation_link_not_found"


class LinkVerificationError(PracticeSignalError):
    http_status = 422
    error_code = "link_verification_error"


class OrganisationSlugConflict(PracticeSignalError):
    http_status = 409
    error_code = "organisation_slug_conflict"


class SourceLinkConflict(PracticeSignalError):
    http_status = 409
    error_code = "source_link_conflict"


# =============================================================================
# ORGANISATION CRUD & QUERIES
# =============================================================================

@router.get("/organisations", response_model=list[OrganisationRead], tags=["organisations"])
def list_organisations(
    slug: str | None = Query(default=None, description="Filter by unique URL-safe slug"),
    postcode: str | None = Query(default=None, description="Filter by 4-digit postcode"),
    state: str | None = Query(default=None, description="Filter by Australian state (VIC, NSW, etc.)"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Query healthcare practice organisations with deterministic filtering."""
    query = select(Organisation)
    if slug:
        query = query.where(Organisation.slug == slug)
    if postcode:
        query = query.where(Organisation.postcode == postcode)
    if state:
        query = query.where(Organisation.state == state.upper())

    query = query.order_by(Organisation.id.asc()).offset(skip).limit(limit)
    orgs = db.scalars(query).all()
    return [to_organisation_read(org) for org in orgs]


@router.post("/organisations", response_model=OrganisationRead, status_code=201, tags=["organisations"])
def create_organisation(
    payload: OrganisationCreate,
    db: Session = Depends(get_db),
) -> dict:
    """Create a new healthcare practice organisation in the canonical registry."""
    existing = db.scalar(select(Organisation).where(Organisation.slug == payload.slug))
    if existing:
        raise OrganisationSlugConflict(f"Organisation with slug '{payload.slug}' already exists")

    now = datetime.now(UTC)
    org = Organisation(
        name=payload.name,
        slug=payload.slug,
        primary_domain=payload.primary_domain,
        corporate_parent_id=payload.corporate_parent_id,
        phone=payload.phone,
        address=payload.address,
        suburb=payload.suburb,
        postcode=payload.postcode,
        state=payload.state.upper(),
        status=payload.status,
        created_at=now,
        updated_at=now,
    )
    db.add(org)
    db.commit()
    logger.info("api_organisation_created id=%d slug=%s", org.id, org.slug)
    return to_organisation_read(org)


@router.get("/organisations/{org_id}", response_model=OrganisationDetailRead, tags=["organisations"])
def get_organisation(
    org_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """Get full organisation profile including linked sources and active facts count."""
    org = db.get(Organisation, org_id)
    if not org:
        raise OrganisationNotFound(f"Organisation {org_id} not found")

    data = to_organisation_read(org)
    data["source_links"] = [to_organisation_link_read(link) for link in org.source_links]

    # Count active/candidate facts mapped to this organisation
    facts_count = db.scalar(
        select(func.count(Fact.id)).where(Fact.organisation_id == org_id)
    ) or 0
    data["active_facts_count"] = facts_count
    return data


# =============================================================================
# MULTI-SOURCE LINKING (D40)
# =============================================================================

@router.get("/organisations/{org_id}/sources", response_model=list[OrganisationLinkRead], tags=["organisations"])
def list_organisation_sources(
    org_id: int,
    db: Session = Depends(get_db),
) -> list[dict]:
    """List all registered sources linked to this practice organisation."""
    org = db.get(Organisation, org_id)
    if not org:
        raise OrganisationNotFound(f"Organisation {org_id} not found")

    return [to_organisation_link_read(link) for link in org.source_links]


@router.post("/organisations/{org_id}/sources", response_model=OrganisationLinkRead, status_code=201, tags=["organisations"])
def link_source_to_organisation(
    org_id: int,
    payload: OrganisationLinkCreate,
    db: Session = Depends(get_db),
) -> dict:
    """Link a source to an organisation with a specific relationship type."""
    org = db.get(Organisation, org_id)
    if not org:
        raise OrganisationNotFound(f"Organisation {org_id} not found")

    source = db.get(Source, payload.source_id)
    if not source:
        raise SourceNotFound(f"Source {payload.source_id} not found")

    # Check for existing link
    existing = db.scalar(
        select(OrganisationSourceLink).where(
            OrganisationSourceLink.organisation_id == org_id,
            OrganisationSourceLink.source_id == payload.source_id,
        )
    )
    if existing:
        raise SourceLinkConflict(f"Source {payload.source_id} is already linked to organisation {org_id}")

    # Enforce that confirmed links require an identified operator
    if payload.verification_status == "confirmed" and not payload.verified_by:
        raise LinkVerificationError("verified_by operator identity is required when creating a confirmed source link")

    now = datetime.now(UTC)
    link = OrganisationSourceLink(
        organisation_id=org_id,
        source_id=payload.source_id,
        relationship_type=payload.relationship_type,
        scope_path=payload.scope_path,
        verification_status=payload.verification_status,
        verified_by=payload.verified_by,
        verified_at=now if payload.verification_status == "confirmed" else None,
        created_at=now,
    )
    db.add(link)
    db.commit()
    logger.info("api_source_linked org_id=%d source_id=%d rel=%s", org_id, payload.source_id, payload.relationship_type)
    return to_organisation_link_read(link)


@router.patch("/organisations/{org_id}/sources/{link_id}", response_model=OrganisationLinkRead, tags=["organisations"])
def update_organisation_source_link(
    org_id: int,
    link_id: int,
    payload: OrganisationLinkUpdate,
    db: Session = Depends(get_db),
) -> dict:
    """Deliberate operator operation to transition source link verification status (suggested -> confirmed | rejected)."""
    org = db.get(Organisation, org_id)
    if not org:
        raise OrganisationNotFound(f"Organisation {org_id} not found")

    link = db.get(OrganisationSourceLink, link_id)
    if not link or link.organisation_id != org_id:
        raise OrganisationLinkNotFound(f"Source link {link_id} not found for organisation {org_id}")

    # Enforce verified_by when confirming or rejecting
    effective_verifier = payload.verified_by or link.verified_by
    if payload.verification_status in ("confirmed", "rejected") and not effective_verifier:
        raise LinkVerificationError(f"verified_by operator identity is required to set status '{payload.verification_status}'")

    now = datetime.now(UTC)
    link.verification_status = payload.verification_status
    if payload.verification_status in ("confirmed", "rejected"):
        link.verified_by = effective_verifier
        link.verified_at = now
    else:  # returned to suggested
        link.verified_at = None

    if payload.relationship_type is not None:
        link.relationship_type = payload.relationship_type
    if payload.scope_path is not None:
        link.scope_path = payload.scope_path

    db.commit()
    logger.info(
        "api_source_link_updated org_id=%d link_id=%d status=%s verifier=%s",
        org_id, link_id, payload.verification_status, link.verified_by,
    )
    return to_organisation_link_read(link)


# =============================================================================
# ENTITY RESOLUTION MATCH CANDIDATES (D39)
# =============================================================================

@router.get("/match-candidates", response_model=list[OrganisationMatchCandidateRead], tags=["entity_resolution"])
def list_match_candidates(
    status: str | None = Query(default=None, description="Filter by review status (suggested|needs_review|approved|rejected)"),
    tier: str | None = Query(default=None, description="Filter by match tier (strong_match_suggestion|possible_match|insufficient_evidence)"),
    source_id: int | None = Query(default=None, description="Filter by source id"),
    organisation_id: int | None = Query(default=None, description="Filter by organisation id"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Query conservative entity resolution match suggestions."""
    query = select(OrganisationMatchCandidate)
    if status:
        query = query.where(OrganisationMatchCandidate.status == status)
    if tier:
        query = query.where(OrganisationMatchCandidate.match_tier == tier)
    if source_id:
        query = query.where(OrganisationMatchCandidate.source_id == source_id)
    if organisation_id:
        query = query.where(OrganisationMatchCandidate.suggested_organisation_id == organisation_id)

    query = query.order_by(OrganisationMatchCandidate.id.desc()).offset(skip).limit(limit)
    candidates = db.scalars(query).all()
    return [to_match_candidate_read(c) for c in candidates]


@router.post("/match-candidates/scan/{source_id}", response_model=list[OrganisationMatchCandidateRead], tags=["entity_resolution"])
def scan_source_matches(
    source_id: int,
    db: Session = Depends(get_db),
) -> list[dict]:
    """Scan a source against known practice organisations and generate match candidates."""
    source = db.get(Source, source_id)
    if not source:
        raise SourceNotFound(f"Source {source_id} not found")

    organisations = db.scalars(select(Organisation)).all()
    created_or_found: list[OrganisationMatchCandidate] = []
    for org in organisations:
        candidate = propose_match_candidate(db, source_id, org.id)
        if candidate:
            created_or_found.append(candidate)

    db.commit()
    return [to_match_candidate_read(c) for c in created_or_found]


# =============================================================================
# SOURCE OBSERVATION HEARTBEATS (D40)
# =============================================================================

@router.get("/sources/{source_id}/observations", response_model=list[SourceObservationRead], tags=["observations"])
def get_source_observations(
    source_id: int,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Retrieve chronological observation heartbeat history for a registered source."""
    source = db.get(Source, source_id)
    if not source:
        raise SourceNotFound(f"Source {source_id} not found")

    query = (
        select(SourceObservation)
        .where(SourceObservation.source_id == source_id)
        .order_by(SourceObservation.observation_sequence.asc())
        .offset(skip)
        .limit(limit)
    )
    observations = db.scalars(query).all()
    return [to_source_observation_read(obs) for obs in observations]
