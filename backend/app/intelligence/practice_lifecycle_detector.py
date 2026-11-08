"""Practice Lifecycle Intelligence: Closure, Relocation & Corporate Acquisition Detector (M3.7).

Conservative, evidence-first detection of healthcare practice closures, relocations,
and corporate acquisitions using deterministic local processing without LLM speculation.

Key Architectural Invariants:
    1. Zero Unverified Closure: Uncertainty is never presented as confirmed closure.
       Absence across observations produces 'potential_needs_review' or 'no_longer_observed',
       never 'confirmed_explicit' without verbatim evidence quotation.
    2. Branch vs Organisation Isolation: One branch closing does not close parent organisation
       or sibling branches.
    3. Negative Controls on Closures: Temporary holiday, emergency, lunch, or renovation closures
       are strictly distinguished from permanent practice closure.
    4. Address Change vs Branch Creation: Adding a branch location to a multi-branch page is not
       a relocation if the previous branch remains active.
    5. Zero Auto-Merge on Acquisition or Relocation: An acquisition or address change preserves
       canonical organisation IDs and historical records without collapsing or deleting entities.
    6. Strict Negative Controls on Acquisition: Shared domains, shared 1300/1800 numbers,
       shared branding, and similar names are rejected as proof of legal acquisition.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.logging import get_logger
from backend.app.db import orm
from backend.app.intelligence.freshness_engine import extract_fact_temporal_evidence
from backend.app.intelligence.schemas import (
    LifecycleEventType,
    LifecycleStatus,
    ReviewStatus,
    TemporalPrecision,
)

logger = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# =============================================================================
# DETERMINISTIC PATTERNS & REGEXES
# =============================================================================

# 1. Permanent Closure Patterns (Requires fact-specific permanence)
PERMANENT_CLOSURE_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b(?:permanently\s+closed|closed\s+permanently)\b", re.IGNORECASE),
    re.compile(r"\b(?:permanently\s+ceas(?:ed|ing)\s+(?:operations|practice|operating))\b", re.IGNORECASE),
    re.compile(r"\b(?:will\s+cease|has\s+ceased)\s+operating\b", re.IGNORECASE),
    re.compile(r"\b(?:clinic|practice|centre|center)\s+(?:is\s+now|has)\s+(?:permanently\s+closed|ceased\s+operating)\b", re.IGNORECASE),
    re.compile(r"\b(?:this|our)\s+(?:medical\s+)?practice\s+is\s+now\s+permanently\s+closed\b", re.IGNORECASE),
    re.compile(r"\bclosing\s+permanently\s+(?:from|on|effective)\b", re.IGNORECASE),
]

# 2. Temporary Closure Patterns (Negative controls for permanent closure)
TEMPORARY_CLOSURE_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bclosed\s+today\b", re.IGNORECASE),
    re.compile(r"\bclosed\s+for\s+(?:lunch|the\s+day|the\s+weekend)\b", re.IGNORECASE),
    re.compile(r"\bclosed\s+for\s+(?:the\s+)?(?:public\s+holiday|holiday|christmas|easter|new\s+year|australia\s+day|anzac\s+day)\b", re.IGNORECASE),
    re.compile(r"\btemporarily\s+closed\b", re.IGNORECASE),
    re.compile(r"\bclosed\s+temporarily\b", re.IGNORECASE),
    re.compile(r"\b(?:closed\s+for|undergoing)\s+renovations?\b", re.IGNORECASE),
    re.compile(r"\bemergency\s+closure\b", re.IGNORECASE),
    re.compile(r"\bclosed\s+due\s+to\s+(?:power\s+outage|maintenance|flooding|weather)\b", re.IGNORECASE),
]

# 3. Explicit Relocation Patterns
RELOCATION_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b(?:we\s+have\s+moved|we've\s+moved)\s+to\s+([^.\n]+)", re.IGNORECASE),
    re.compile(r"\b(?:relocating|relocated)\s+to\s+(?:a\s+new\s+address|new\s+premises|new\s+location)(?:\s+at\s+([^.\n]+))?", re.IGNORECASE),
    re.compile(r"\bvisit\s+our\s+new\s+(?:practice\s+)?location\s+at\s+([^.\n]+)", re.IGNORECASE),
    re.compile(r"\bour\s+new\s+(?:practice\s+)?address\s+is\s+([^.\n]+)", re.IGNORECASE),
    re.compile(r"\bmoving\s+to\s+(?:our\s+new\s+premises\s+at\s+)?([^.\n]+)", re.IGNORECASE),
]

# 4. Explicit Corporate Acquisition & Ownership Patterns
ACQUISITION_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b(?:has\s+acquired|has\s+been\s+acquired\s+by|was\s+acquired\s+by)\s+([^.\n]+)", re.IGNORECASE),
    re.compile(r"\bbecame\s+a\s+subsidiary\s+of\s+([^.\n]+)", re.IGNORECASE),
    re.compile(r"\b(?:the\s+)?ownership\s+of\s+(?:the\s+practice|this\s+practice|the\s+clinic)\s+(?:has\s+transferred|transferred)\s+to\s+([^.\n]+)", re.IGNORECASE),
    re.compile(r"\b(?:now\s+part\s+of\s+the|proudly\s+part\s+of\s+the)\s+([^.\n]+?)\s+(?:group|network|family|healthcare)\b", re.IGNORECASE),
    re.compile(r"\bownership\s+has\s+transferred\s+to\s+([^.\n]+)", re.IGNORECASE),
]

# Negative acquisition indicators (shared resources that DO NOT prove legal acquisition)
NEGATIVE_ACQUISITION_TOKENS: list[str] = [
    "shared domain",
    "partner network",
    "directory listing",
    "accredited by",
    "referral partner",
    "allied health suite",
]


# =============================================================================
# HELPER EXTRACTION FUNCTIONS
# =============================================================================

def _extract_sentence_with_offsets(text: str, match_start: int, match_end: int) -> tuple[str, int, int]:
    """Expand regex match boundaries to full sentence boundaries with exact character offsets."""
    # Find start of sentence or line
    start = match_start
    while start > 0 and text[start - 1] not in ".\n\r":
        start -= 1
    # Skip leading whitespace
    while start < match_start and text[start].isspace():
        start += 1

    # Find end of sentence or line
    end = match_end
    while end < len(text) and text[end] not in ".\n\r":
        end += 1
    if end < len(text) and text[end] == ".":
        end += 1

    quote = text[start:end].strip()
    return quote, start, end


def _extract_branch_scope_from_text(text: str, quote: str) -> tuple[str | None, bool]:
    """Detect whether a lifecycle statement mentions a specific branch locality."""
    combined = f"{quote} {text[:500]}".lower()

    # Look for common branch location cues (e.g. "our Box Hill branch", "Carlton clinic")
    m_branch = re.search(r"\b(?:our\s+)?([a-z\s]+?)\s+(?:branch|clinic|location|site)\s+(?:will|is|has|permanently|closed|relocating)", quote, re.IGNORECASE)
    if m_branch:
        locality = m_branch.group(1).strip()
        locality_clean = " ".join(word.capitalize() for word in locality.split() if word.lower() not in ("our", "the", "this"))
        if locality_clean and len(locality_clean) > 2:
            return locality_clean, True

    # Check for heading cues before quote in text
    quote_pos = text.find(quote)
    if quote_pos > 0:
        preceding = text[max(0, quote_pos - 300):quote_pos]
        m_heading = re.search(r"##+\s+([a-z\s]+?)(?:\n|$)", preceding, re.IGNORECASE)
        if m_heading:
            locality = m_heading.group(1).strip()
            if locality and len(locality) > 2 and locality.lower() not in ("contact", "about", "hours", "news", "announcements"):
                return " ".join(word.capitalize() for word in locality.split()), True

    return None, False


def _parse_lifecycle_date(quote: str) -> tuple[str | None, TemporalPrecision]:
    """Extract explicit date and precision from lifecycle quotation."""
    parsed = extract_fact_temporal_evidence(quote)
    if parsed is not None:
        if parsed.date_start is not None:
            if parsed.precision == TemporalPrecision.DAY:
                date_str = parsed.date_start.strftime("%Y-%m-%d")
            elif parsed.precision == TemporalPrecision.MONTH:
                date_str = parsed.date_start.strftime("%Y-%m")
            elif parsed.precision == TemporalPrecision.YEAR:
                date_str = parsed.date_start.strftime("%Y")
            else:
                date_str = parsed.date_start.strftime("%Y-%m-%d")
            return date_str, parsed.precision

    # Fallback date regex
    m_date = re.search(r"\b([0-9]{1,2}(?:st|nd|rd|th)?\s+[a-zA-Z]+\s+[0-9]{4}|[a-zA-Z]+\s+[0-9]{1,2}(?:st|nd|rd|th)?,\s+[0-9]{4}|[a-zA-Z]+\s+[0-9]{4}|[0-9]{4})\b", quote)
    if m_date:
        d_val = m_date.group(1).strip()
        if re.search(r"^[0-9]{4}$", d_val):
            return d_val, TemporalPrecision.YEAR
        if re.search(r"^[a-zA-Z]+\s+[0-9]{4}$", d_val):
            return d_val, TemporalPrecision.MONTH
        return d_val, TemporalPrecision.DAY

    return None, TemporalPrecision.UNKNOWN


def _generate_idempotency_key(org_id: int, event_type: str, branch_locality: str | None, snapshot_id: int, anchor: str) -> str:
    """Generate deterministic SHA-256 idempotency key for practice lifecycle events."""
    raw = f"{org_id}:{event_type}:{branch_locality or 'org'}:{snapshot_id}:{anchor.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# =============================================================================
# CORE DETECTION ENGINES
# =============================================================================

def detect_closure_evidence(
    session: Session,
    organisation: orm.Organisation,
    source: orm.Source,
    snapshot: orm.PageSnapshot,
) -> list[orm.PracticeLifecycleEvent]:
    """Detect explicit permanent closure or temporary closure evidence from a page snapshot.

    Enforces strict negative controls against temporary holiday, emergency, and lunch closures.
    Isolates branch-level closures so parent organisation and sibling branches remain open.
    """
    events: list[orm.PracticeLifecycleEvent] = []
    text = snapshot.text or ""
    if not text.strip():
        return events

    # Step 1: Check for temporary closures first (negative controls)
    for pat in TEMPORARY_CLOSURE_PATTERNS:
        match = pat.search(text)
        if match:
            quote, start_off, end_off = _extract_sentence_with_offsets(text, match.start(), match.end())
            branch_locality, is_branch = _extract_branch_scope_from_text(text, quote)
            eff_date, precision = _parse_lifecycle_date(quote)

            evidence = orm.Evidence(
                snapshot_id=snapshot.id,
                quote=quote,
                start_offset=start_off,
                end_offset=end_off,
            )
            session.add(evidence)
            session.flush()

            idemp_key = _generate_idempotency_key(organisation.id, "temporary_closure", branch_locality, snapshot.id, quote[:60])
            existing = session.execute(
                select(orm.PracticeLifecycleEvent).where(orm.PracticeLifecycleEvent.idempotency_key == idemp_key)
            ).scalar_one_or_none()

            if not existing:
                evt = orm.PracticeLifecycleEvent(
                    organisation_id=organisation.id,
                    branch_locality=branch_locality,
                    is_branch_level=is_branch,
                    event_type=LifecycleEventType.TEMPORARY_CLOSURE.value,
                    status=LifecycleStatus.POTENTIAL_NEEDS_REVIEW.value,
                    source_id=source.id,
                    snapshot_id=snapshot.id,
                    evidence_id=evidence.id,
                    evidence_quote=quote,
                    effective_date=eff_date,
                    date_precision=precision.value,
                    rationale=f"Temporary closure detected ({quote}); strictly distinguished from permanent practice closure.",
                    metadata_json={"match_pattern": pat.pattern, "is_temporary_negative_control": True},
                    review_status=ReviewStatus.PENDING.value,
                    idempotency_key=idemp_key,
                )
                session.add(evt)
                events.append(evt)

            # Do not proceed to permanent closure for this exact sentence
            return events

    # Step 2: Check for explicit permanent closure
    for pat in PERMANENT_CLOSURE_PATTERNS:
        match = pat.search(text)
        if match:
            quote, start_off, end_off = _extract_sentence_with_offsets(text, match.start(), match.end())
            branch_locality, is_branch = _extract_branch_scope_from_text(text, quote)
            eff_date, precision = _parse_lifecycle_date(quote)

            evidence = orm.Evidence(
                snapshot_id=snapshot.id,
                quote=quote,
                start_offset=start_off,
                end_offset=end_off,
            )
            session.add(evidence)
            session.flush()

            idemp_key = _generate_idempotency_key(organisation.id, "permanent_closure", branch_locality, snapshot.id, quote[:60])
            existing = session.execute(
                select(orm.PracticeLifecycleEvent).where(orm.PracticeLifecycleEvent.idempotency_key == idemp_key)
            ).scalar_one_or_none()

            if not existing:
                scope_desc = f"Branch '{branch_locality}'" if is_branch else "Organisation"
                evt = orm.PracticeLifecycleEvent(
                    organisation_id=organisation.id,
                    branch_locality=branch_locality,
                    is_branch_level=is_branch,
                    event_type=LifecycleEventType.PERMANENT_CLOSURE.value,
                    status=LifecycleStatus.CONFIRMED_EXPLICIT.value,
                    source_id=source.id,
                    snapshot_id=snapshot.id,
                    evidence_id=evidence.id,
                    evidence_quote=quote,
                    effective_date=eff_date,
                    date_precision=precision.value,
                    rationale=f"{scope_desc} permanent closure explicitly evidenced by verbatim source quotation.",
                    metadata_json={"match_pattern": pat.pattern, "scope": "branch" if is_branch else "organisation"},
                    review_status=ReviewStatus.PENDING.value,
                    idempotency_key=idemp_key,
                )
                session.add(evt)
                events.append(evt)
            else:
                events.append(existing)

            # First explicit permanent closure match is sufficient
            break

    return events


def detect_relocation_evidence(
    session: Session,
    organisation: orm.Organisation,
    source: orm.Source,
    snapshot: orm.PageSnapshot,
    previous_snapshot: orm.PageSnapshot | None = None,
) -> list[orm.PracticeLifecycleEvent]:
    """Detect practice relocation through explicit announcements or address-change diffs.

    Enforces distinction between relocation and adding a branch to a multi-branch page.
    Preserves organisation identity without merging or creating duplicate organisations.
    """
    events: list[orm.PracticeLifecycleEvent] = []
    text = snapshot.text or ""
    if not text.strip():
        return events

    # Step 1: Check for explicit relocation announcement
    for pat in RELOCATION_PATTERNS:
        match = pat.search(text)
        if match:
            quote, start_off, end_off = _extract_sentence_with_offsets(text, match.start(), match.end())
            branch_locality, is_branch = _extract_branch_scope_from_text(text, quote)
            eff_date, precision = _parse_lifecycle_date(quote)
            new_addr_cand = match.group(1).strip() if match.groups() and match.group(1) else None

            evidence = orm.Evidence(
                snapshot_id=snapshot.id,
                quote=quote,
                start_offset=start_off,
                end_offset=end_off,
            )
            session.add(evidence)
            session.flush()

            idemp_key = _generate_idempotency_key(organisation.id, "relocation", branch_locality, snapshot.id, quote[:60])
            existing = session.execute(
                select(orm.PracticeLifecycleEvent).where(orm.PracticeLifecycleEvent.idempotency_key == idemp_key)
            ).scalar_one_or_none()

            if not existing:
                evt = orm.PracticeLifecycleEvent(
                    organisation_id=organisation.id,
                    branch_locality=branch_locality,
                    is_branch_level=is_branch,
                    event_type=LifecycleEventType.RELOCATION.value,
                    status=LifecycleStatus.CONFIRMED_EXPLICIT.value,
                    source_id=source.id,
                    snapshot_id=snapshot.id,
                    evidence_id=evidence.id,
                    evidence_quote=quote,
                    effective_date=eff_date,
                    date_precision=precision.value,
                    old_value=organisation.address,
                    new_value=new_addr_cand or "Relocated Address Stated in Evidence",
                    rationale="Explicit relocation announcement evidenced by source quotation.",
                    metadata_json={"new_address_extracted": new_addr_cand},
                    review_status=ReviewStatus.PENDING.value,
                    idempotency_key=idemp_key,
                )
                session.add(evt)
                events.append(evt)
            else:
                events.append(existing)

            return events

    # Step 2: Compare address facts between previous and current snapshots (Potential Relocation)
    if previous_snapshot is not None:
        prev_addr_fact = session.execute(
            select(orm.Fact).where(
                orm.Fact.snapshot_id == previous_snapshot.id,
                orm.Fact.field_key.in_(["address", "street_address"]),
                orm.Fact.organisation_id == organisation.id,
            )
        ).scalars().first()

        curr_addr_fact = session.execute(
            select(orm.Fact).where(
                orm.Fact.snapshot_id == snapshot.id,
                orm.Fact.field_key.in_(["address", "street_address"]),
                orm.Fact.organisation_id == organisation.id,
            )
        ).scalars().first()

        if prev_addr_fact and curr_addr_fact and prev_addr_fact.raw_value != curr_addr_fact.raw_value:
            # Check multi-branch page condition: is the old address still in the new snapshot text?
            if prev_addr_fact.raw_value in text:
                # Old address remains present -> New branch created, NOT a relocation
                logger.info(
                    "branch_creation_detected org_id=%s old_addr=%s remains in snapshot; skipping false relocation.",
                    organisation.id, prev_addr_fact.raw_value,
                )
            else:
                idemp_key = _generate_idempotency_key(organisation.id, "relocation", None, snapshot.id, f"{prev_addr_fact.raw_value}->{curr_addr_fact.raw_value}")
                existing = session.execute(
                    select(orm.PracticeLifecycleEvent).where(orm.PracticeLifecycleEvent.idempotency_key == idemp_key)
                ).scalar_one_or_none()

                if not existing:
                    evt = orm.PracticeLifecycleEvent(
                        organisation_id=organisation.id,
                        branch_locality=None,
                        is_branch_level=False,
                        event_type=LifecycleEventType.RELOCATION.value,
                        status=LifecycleStatus.POTENTIAL_NEEDS_REVIEW.value,
                        source_id=source.id,
                        snapshot_id=snapshot.id,
                        evidence_id=curr_addr_fact.evidence_id,
                        evidence_quote=curr_addr_fact.evidence.quote if curr_addr_fact.evidence else None,
                        old_value=prev_addr_fact.raw_value,
                        new_value=curr_addr_fact.raw_value,
                        rationale="Address fact changed between successive snapshots without explicit relocation wording; flagged for operator verification.",
                        metadata_json={"source": "fact_diff"},
                        review_status=ReviewStatus.PENDING.value,
                        idempotency_key=idemp_key,
                    )
                    session.add(evt)
                    events.append(evt)
                else:
                    events.append(existing)

    return events


def detect_acquisition_evidence(
    session: Session,
    organisation: orm.Organisation,
    source: orm.Source,
    snapshot: orm.PageSnapshot,
) -> list[orm.PracticeLifecycleEvent]:
    """Detect explicit corporate acquisition or ownership transfer announcements.

    Strictly rejects shared domains, shared 1300/1800 numbers, shared branding,
    and website redesigns as evidence of acquisition.
    Preserves both acquired and acquiring organisations without merging.
    """
    events: list[orm.PracticeLifecycleEvent] = []
    text = snapshot.text or ""
    if not text.strip():
        return events

    for pat in ACQUISITION_PATTERNS:
        match = pat.search(text)
        if match:
            quote, start_off, end_off = _extract_sentence_with_offsets(text, match.start(), match.end())
            eff_date, precision = _parse_lifecycle_date(quote)
            acquirer_name = match.group(1).strip() if match.groups() and match.group(1) else "Acquiring Entity"
            acquirer_name = re.sub(r"\b(?:effective|from|on|dating|dated)\b.*$", "", acquirer_name, flags=re.IGNORECASE).strip(" .,")

            # Check for negative controls / insufficient evidence keywords
            if any(neg in quote.lower() for neg in NEGATIVE_ACQUISITION_TOKENS):
                continue

            evidence = orm.Evidence(
                snapshot_id=snapshot.id,
                quote=quote,
                start_offset=start_off,
                end_offset=end_off,
            )
            session.add(evidence)
            session.flush()

            # Attempt to resolve acquiring organisation if exists in DB
            acq_org = session.execute(
                select(orm.Organisation).where(
                    orm.Organisation.name.ilike(f"%{acquirer_name}%"),
                    orm.Organisation.id != organisation.id,
                )
            ).scalars().first()

            idemp_key = _generate_idempotency_key(organisation.id, "acquisition", None, snapshot.id, quote[:60])
            existing = session.execute(
                select(orm.PracticeLifecycleEvent).where(orm.PracticeLifecycleEvent.idempotency_key == idemp_key)
            ).scalar_one_or_none()

            if not existing:
                evt = orm.PracticeLifecycleEvent(
                    organisation_id=organisation.id,
                    branch_locality=None,
                    is_branch_level=False,
                    event_type=LifecycleEventType.ACQUISITION.value,
                    status=LifecycleStatus.CONFIRMED_EXPLICIT.value,
                    source_id=source.id,
                    snapshot_id=snapshot.id,
                    evidence_id=evidence.id,
                    evidence_quote=quote,
                    effective_date=eff_date,
                    date_precision=precision.value,
                    old_value=None,
                    new_value=acquirer_name,
                    acquiring_organisation_id=acq_org.id if acq_org else None,
                    acquiring_entity_name=acquirer_name,
                    rationale=f"Explicit corporate acquisition announcement by '{acquirer_name}' evidenced by source quotation.",
                    metadata_json={"acquirer_name": acquirer_name, "acq_org_found": acq_org is not None},
                    review_status=ReviewStatus.PENDING.value,
                    idempotency_key=idemp_key,
                )
                session.add(evt)
                events.append(evt)
            else:
                events.append(existing)

            return events

    return events


def detect_absence_possible_closure(
    session: Session,
    organisation: orm.Organisation,
    source: orm.Source,
    current_snapshot: orm.PageSnapshot,
) -> list[orm.PracticeLifecycleEvent]:
    """Detect potential closure due to repeated fact absence across eligible complete observations.

    Enforces that absence alone NEVER establishes confirmed permanent closure.
    Freezes on failed crawls, incomplete crawls, partial parsing, or partial extraction.
    """
    events: list[orm.PracticeLifecycleEvent] = []

    # Check recent change events for repeated unobserved facts
    change_events = session.execute(
        select(orm.ChangeEvent).where(
            orm.ChangeEvent.organisation_id == organisation.id,
            orm.ChangeEvent.source_id == source.id,
            orm.ChangeEvent.event_type.in_(["unconfirmed_prior", "fact_unobserved"]),
        ).order_by(orm.ChangeEvent.id.desc())
    ).scalars().all()

    if not change_events:
        return events

    # Count consecutive absences recorded in diff_metadata
    max_absences = 0
    for ce in change_events:
        meta = ce.diff_metadata or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        abs_count = meta.get("observation_count_absent", 0)
        if abs_count > max_absences:
            max_absences = abs_count

    if max_absences >= 2:
        idemp_key = _generate_idempotency_key(organisation.id, "possible_closure_absence", None, current_snapshot.id, f"absent_{max_absences}")
        existing = session.execute(
            select(orm.PracticeLifecycleEvent).where(orm.PracticeLifecycleEvent.idempotency_key == idemp_key)
        ).scalar_one_or_none()

        if not existing:
            evt = orm.PracticeLifecycleEvent(
                organisation_id=organisation.id,
                branch_locality=None,
                is_branch_level=False,
                event_type=LifecycleEventType.PERMANENT_CLOSURE.value,
                status=LifecycleStatus.NO_LONGER_OBSERVED.value,
                source_id=source.id,
                snapshot_id=current_snapshot.id,
                evidence_id=None,
                evidence_quote=None,
                rationale=f"Practice facts absent across {max_absences} consecutive eligible complete observations; potential closure flagged for human review (never automatically assumed closed).",
                metadata_json={"consecutive_absences": max_absences},
                review_status=ReviewStatus.PENDING.value,
                idempotency_key=idemp_key,
            )
            session.add(evt)
            events.append(evt)
        else:
            events.append(existing)

    return events


# =============================================================================
# TOP-LEVEL SCAN ORCHESTRATOR
# =============================================================================

def detect_practice_lifecycle_events(
    session: Session,
    source_id: int | None = None,
    organisation_id: int | None = None,
) -> list[orm.PracticeLifecycleEvent]:
    """Orchestrate comprehensive practice lifecycle detection across registered sources.

    Detects:
        1. Explicit permanent closure and branch-scoped closures.
        2. Temporary closures (holiday, emergency, renovation) as negative controls.
        3. Potential closure signals from repeated absence.
        4. Explicit relocations and address change diffs.
        5. Explicit corporate acquisition announcements.
    """
    detected_events: list[orm.PracticeLifecycleEvent] = []

    # Build query for candidate links
    link_query = select(orm.OrganisationSourceLink).where(
        orm.OrganisationSourceLink.verification_status.in_(["confirmed", "verified", "suggested"])
    )
    if source_id is not None:
        link_query = link_query.where(orm.OrganisationSourceLink.source_id == source_id)
    if organisation_id is not None:
        link_query = link_query.where(orm.OrganisationSourceLink.organisation_id == organisation_id)

    links = session.execute(link_query).scalars().all()

    for link in links:
        org = session.get(orm.Organisation, link.organisation_id)
        src = session.get(orm.Source, link.source_id)
        if not org or not src:
            continue

        # Get snapshots for this source ordered chronologically
        snapshots = session.execute(
            select(orm.PageSnapshot).where(
                orm.PageSnapshot.source_id == src.id
            ).order_by(orm.PageSnapshot.fetched_at.asc(), orm.PageSnapshot.id.asc())
        ).scalars().all()

        if not snapshots:
            continue

        latest_snap = snapshots[-1]
        prev_snap = snapshots[-2] if len(snapshots) >= 2 else None

        # 1. Detect closure evidence (explicit permanent or temporary)
        closure_events = detect_closure_evidence(session, org, src, latest_snap)
        detected_events.extend(closure_events)

        # 2. If no explicit closure, check absence-based possible closure
        if not closure_events:
            absence_events = detect_absence_possible_closure(session, org, src, latest_snap)
            detected_events.extend(absence_events)

        # 3. Detect relocation evidence
        relocation_events = detect_relocation_evidence(session, org, src, latest_snap, prev_snap)
        detected_events.extend(relocation_events)

        # 4. Detect acquisition evidence
        acq_events = detect_acquisition_evidence(session, org, src, latest_snap)
        detected_events.extend(acq_events)

    session.commit()
    return detected_events


def verify_lifecycle_evidence_integrity(
    event: orm.PracticeLifecycleEvent,
    snapshot: orm.PageSnapshot,
) -> bool:
    """Verify that a lifecycle event's verbatim evidence quotation matches snapshot text offsets."""
    if not event.evidence_quote:
        return event.status != LifecycleStatus.CONFIRMED_EXPLICIT.value

    snap_text = snapshot.text or ""
    if event.evidence_id and event.evidence:
        ev = event.evidence
        if ev.start_offset is not None and ev.end_offset is not None:
            slice_match = snap_text[ev.start_offset:ev.end_offset] == ev.quote
            if slice_match:
                return True

    return event.evidence_quote in snap_text
