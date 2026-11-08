"""Fact ownership mapper using confirmed source links and heading context (ARCHITECTURE §6.1, D47).

Responsibility:
    Assigns canonical organisation_id to candidate and confirmed facts based on
    confirmed organisation source links and deterministic heading context.

Rules (DECISION_LOG D47):
    1. Single-practice sources: if a source has exactly one confirmed link as an
       official_practice_site or branch_page, facts belong directly to that organisation
       unless enclosed by an explicit corporate heading.
    2. Multi-branch pages: facts are mapped to an organisation by resolving the fact's
       evidence start_offset against enclosing headings (matching branch name, suburb, or postcode).
    3. Corporate & ambiguous headings: corporate-wide or ambiguous sections (e.g. 'Careers',
       'About Us', 'Billing Policy') leave organisation_id as NULL with
       needs_review_reason = 'heading_scope_corporate_unassigned'.
    4. Missing heading context: facts lacking evidence or preceding headings leave
       organisation_id as NULL with needs_review_reason = 'missing_heading_context'.
    5. Conflicting branch evidence: headings matching multiple candidate branch organisations
       leave organisation_id as NULL with needs_review_reason = 'conflicting_branch_evidence'.
    6. Domain-only isolation: parent corporate domain alone NEVER assigns ownership.
    7. Immutability / Non-overwrite: existing valid organisation_id assignments are never
       overwritten without explicit instruction.
    8. Unresolved reason persistence: all unassigned reasons are persisted to the database.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.logging import get_logger
from backend.app.db.orm import (
    Fact,
    Organisation as OrganisationORM,
    OrganisationSourceLink as OrganisationSourceLinkORM,
    PageSnapshot,
)

logger = get_logger(__name__)

CORPORATE_HEADING_KEYWORDS = {
    "careers",
    "career opportunities",
    "about us",
    "our team",
    "head office",
    "corporate",
    "executive",
    "billing policy",
    "privacy policy",
    "terms of use",
    "contact us",
    "national network",
}


def assign_fact_ownership(
    session: Session,
    fact: Fact,
    *,
    candidate_org_links: list[OrganisationSourceLinkORM] | None = None,
    overwrite_existing: bool = False,
) -> int | None:
    """Assign organisation_id to a Fact based on confirmed links and heading context.

    Returns the assigned organisation_id or None if unassigned / ambiguous.
    Persists assignment and unresolved needs_review_reason to the database session.
    """
    if fact.organisation_id is not None and not overwrite_existing:
        # Validate that existing organisation_id exists
        existing_org = session.get(OrganisationORM, fact.organisation_id)
        if existing_org is None:
            raise ValueError(f"Fact references non-existent organisation_id {fact.organisation_id}")
        return fact.organisation_id

    # 1. Retrieve snapshot and source
    snapshot = fact.snapshot or session.get(PageSnapshot, fact.snapshot_id)
    if snapshot is None:
        logger.warning("fact_ownership_failed reason=no_snapshot fact_id=%s", fact.id)
        fact.organisation_id = None
        fact.needs_review_reason = "missing_snapshot"
        session.flush()
        return None

    source = snapshot.source
    if source is None:
        logger.warning("fact_ownership_failed reason=no_source snapshot_id=%d", snapshot.id)
        fact.organisation_id = None
        fact.needs_review_reason = "missing_source"
        session.flush()
        return None

    # 2. Check for corporate heading context first — corporate sections NEVER assign branch ownership
    evidence = fact.evidence
    headings = snapshot.headings or []
    if evidence is not None and evidence.start_offset is not None and headings:
        preceding = [h for h in headings if isinstance(h, dict) and h.get("start_offset", 0) <= evidence.start_offset]
        if preceding:
            preceding_sorted = sorted(preceding, key=lambda h: h.get("start_offset", 0), reverse=True)
            nearest_heading = preceding_sorted[0]
            heading_text = (nearest_heading.get("text") or "").strip().lower()
            is_corporate = any(kw in heading_text for kw in CORPORATE_HEADING_KEYWORDS)
            if is_corporate:
                fact.organisation_id = None
                fact.needs_review_reason = "heading_scope_corporate_unassigned"
                session.flush()
                return None

    # 3. Retrieve confirmed organisation source links
    if candidate_org_links is not None:
        links = candidate_org_links
    else:
        links = list(
            session.scalars(
                select(OrganisationSourceLinkORM).where(
                    OrganisationSourceLinkORM.source_id == source.id,
                    OrganisationSourceLinkORM.verification_status == "confirmed",
                )
            ).all()
        )

    if not links:
        logger.debug(
            "fact_ownership_unassigned reason=no_confirmed_links fact_id=%s source_id=%d",
            fact.id, source.id,
        )
        fact.organisation_id = None
        fact.needs_review_reason = "no_confirmed_organisation_links"
        session.flush()
        return None

    # 4. Case A: Exactly 1 confirmed link
    if len(links) == 1:
        link = links[0]
        # Corporate parent alone cannot assign branch ownership without heading/branch proof
        if link.relationship_type == "corporate_parent":
            fact.organisation_id = None
            fact.needs_review_reason = "heading_scope_corporate_unassigned"
            session.flush()
            return None

        if link.relationship_type in ("official_practice_site", "official_site", "branch_page"):
            fact.organisation_id = link.organisation_id
            fact.needs_review_reason = None
            session.flush()
            logger.debug(
                "fact_ownership_assigned mode=single_link fact_id=%s org_id=%d",
                fact.id, link.organisation_id,
            )
            return link.organisation_id

    # 5. Case B: Multi-branch page or multiple confirmed links -> Heading Context Resolution
    if evidence is None or evidence.start_offset is None:
        fact.organisation_id = None
        fact.needs_review_reason = "missing_heading_context"
        session.flush()
        return None

    preceding = [h for h in headings if isinstance(h, dict) and h.get("start_offset", 0) <= evidence.start_offset]
    if not preceding:
        fact.organisation_id = None
        fact.needs_review_reason = "missing_heading_context"
        session.flush()
        return None

    preceding_sorted = sorted(preceding, key=lambda h: h.get("start_offset", 0), reverse=True)
    nearest_heading = preceding_sorted[0]
    heading_text = (nearest_heading.get("text") or "").strip().lower()

    is_corporate = any(kw in heading_text for kw in CORPORATE_HEADING_KEYWORDS)

    matching_org_ids: list[int] = []
    for link in links:
        org = link.organisation or session.get(OrganisationORM, link.organisation_id)
        if not org:
            continue

        org_name = org.name.lower()
        suburb = (org.suburb or "").lower()
        postcode = (org.postcode or "").strip()
        slug = (org.slug or "").lower()

        name_match = org_name in heading_text or any(part in heading_text for part in org_name.split() if len(part) > 4)
        suburb_match = bool(suburb and suburb in heading_text)
        postcode_match = bool(postcode and postcode in heading_text)
        slug_match = bool(slug and slug in heading_text)

        if (suburb_match or postcode_match or slug_match) or (name_match and not is_corporate):
            matching_org_ids.append(org.id)

    # If multiple candidate branches match the same heading -> conflicting branch evidence
    if len(matching_org_ids) > 1:
        fact.organisation_id = None
        fact.needs_review_reason = "conflicting_branch_evidence"
        session.flush()
        logger.debug(
            "fact_ownership_unassigned mode=conflicting_branch_evidence fact_id=%s matched_count=%d heading=%s",
            fact.id, len(matching_org_ids), heading_text,
        )
        return None

    # If exactly one branch organisation matches the heading context
    if len(matching_org_ids) == 1:
        assigned_id = matching_org_ids[0]
        fact.organisation_id = assigned_id
        fact.needs_review_reason = None
        session.flush()
        logger.debug(
            "fact_ownership_assigned mode=heading_context fact_id=%s org_id=%d heading=%s",
            fact.id, assigned_id, heading_text,
        )
        return assigned_id

    # Ambiguous heading or corporate-wide heading without unambiguous branch match
    fact.organisation_id = None
    fact.needs_review_reason = "heading_scope_corporate_unassigned"
    session.flush()
    logger.debug(
        "fact_ownership_unassigned mode=ambiguous_heading fact_id=%s matched_count=%d heading=%s",
        fact.id, len(matching_org_ids), heading_text,
    )
    return None


def assign_snapshot_facts_ownership(session: Session, snapshot_id: int) -> int:
    """Batch assign organisation ownership to all facts belonging to a snapshot.

    Returns the number of facts successfully assigned an organisation_id.
    """
    facts = list(session.scalars(select(Fact).where(Fact.snapshot_id == snapshot_id)).all())
    assigned_count = 0
    for fact in facts:
        res = assign_fact_ownership(session, fact)
        if res is not None:
            assigned_count += 1
    session.flush()
    return assigned_count
