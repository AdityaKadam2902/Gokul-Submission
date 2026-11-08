"""Deterministic healthcare workforce vacancy lifecycle intelligence engine (ARCHITECTURE §6.1, DECISION_LOG D43).

Responsibility:
    Identifies and tracks publicly advertised healthcare workforce opportunities across
    successive observations of registered practice sources.

Supported Lifecycle States (D43):
    - advertised: Complete, evidence-backed active vacancy posting.
    - changed: Existing vacancy observed with title, remuneration, or description change (same identity).
    - possibly_stale: Vacancy passed an explicitly published closing date or monitoring threshold.
    - unconfirmed_prior: Absent from 1st eligible complete observation (absence count = 1).
    - no_longer_observed: Absent across 2 consecutive eligible complete observations (absence count = 2).
    - explicitly_closed: Source explicitly provides verified verbatim closure evidence quotation.

Strict Prohibitions:
    - Status 'filled' or 'hired' is STRICTLY FORBIDDEN by architecture (D43).
    - Overwriting historical vacancy provenance is strictly forbidden.
    - Cascading deletions are strictly forbidden (historical preservation).

Deterministic Identity Hierarchy:
    1. Explicit published job reference ID (explicit_ad_id).
    2. Stable source-local job slug or URL (source_job_id).
    3. Composite identity: organisation_id, role_category, branch/location, source_id
       (job title and remuneration are strictly omitted to allow title/salary changes without duplicate creation).
    4. Ambiguity preserved across sources and anonymous vacancies (no speculative auto-merging).
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from bs4 import BeautifulSoup
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.core.logging import get_logger
from backend.app.db.orm import (
    ChangeEvent as ChangeEventORM,
    Opportunity as OpportunityORM,
    Organisation,
    OrganisationSourceLink,
    PageSnapshot,
    Source,
    SourceObservation as SourceObservationORM,
)
from backend.app.intelligence.schemas import (
    ExtractionCompleteness,
    ObservationOutcome,
    OpportunityStatus,
    ParsingCompleteness,
    RoleCategory,
)

logger = get_logger(__name__)

# =============================================================================
# REGEX PATTERNS & ROLE CLASSIFICATION
# =============================================================================

REF_PATTERNS = [
    re.compile(r"(?:Ref(?:erence)?|Job\s*Ref(?:erence)?|Job\s*ID|Ad\s*ID|Reference\s*Number)[:\s#]+([A-Za-z0-9\-_/]+)", re.IGNORECASE),
    re.compile(r"\b([A-Z]{2,4}-\d{4}-\d{2,4})\b"),  # e.g. GP-2026-04
]

EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
PHONE_PATTERN = re.compile(r"(?:\+?61\s?|0)[2-478](?:[ -]?[0-9]){8}")

CLOSURE_PATTERNS = [
    re.compile(
        r"([^.<\n\r]*(?:applications?|positions?|roles?|vacanc(?:y|ies))[^.<\n\r]*(?:are\s+now\s+closed|have\s+closed|is\s+closed|no\s+longer\s+accepting)[^.<\n\r]*\.?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"([^.<\n\r]*(?:this\s+position|this\s+role)[^.<\n\r]*(?:has\s+been\s+closed|is\s+now\s+closed|closed)[^.<\n\r]*\.?)",
        re.IGNORECASE,
    ),
]

CLOSING_DATE_PATTERNS = [
    re.compile(r"(?:applications?\s+close|closing\s+date|closes|deadline)[:\s]+([0-9]{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+[0-9]{4})", re.IGNORECASE),
    re.compile(r"(?:applications?\s+close|closing\s+date|closes|deadline)[:\s]+([0-9]{4}-[0-9]{2}-[0-9]{2})", re.IGNORECASE),
    re.compile(r"(?:applications?\s+close|closing\s+date|closes|deadline)[:\s]+([0-9]{1,2}/[0-9]{1,2}/[0-9]{4})", re.IGNORECASE),
]

ROLE_KEYWORDS: dict[RoleCategory, list[str]] = {
    RoleCategory.GP: [
        "general practitioner", "vr gp", "gp", "family physician", "general practice doctor",
        "medical officer", "doctor", "vocational registered general practitioner",
    ],
    RoleCategory.NURSE: [
        "registered nurse", "enrolled nurse", "practice nurse", "nurse practitioner",
        "clinical nurse", "rn", "nurse",
    ],
    RoleCategory.ALLIED_HEALTH: [
        "physiotherapist", "podiatrist", "psychologist", "dietitian", "occupational therapist",
        "speech pathologist", "exercise physiologist", "optometrist", "allied health",
        "audiologist", "chiropractor", "osteopath", "social worker",
    ],
    RoleCategory.ADMINISTRATIVE: [
        "practice manager", "medical receptionist", "receptionist", "clinic coordinator",
        "administration officer", "admin assistant", "practice administrator",
    ],
    RoleCategory.SPECIALIST: [
        "specialist", "dermatologist", "cardiologist", "psychiatrist", "surgeon",
        "paediatrician", "neurologist", "oncologist", "gynaecologist", "endocrinologist",
    ],
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _normalize_text_basic(text: str) -> str:
    """Normalize text for whitespace, unicode normalization, and case-folding."""
    if not text:
        return ""
    norm = unicodedata.normalize("NFKC", text)
    return " ".join(norm.split())


def classify_role_category(role_title: str, wording: str = "") -> RoleCategory:
    """Classify healthcare workforce role title and wording into standardized RoleCategory."""
    combined = f"{role_title} {wording}".lower()
    for cat, keywords in ROLE_KEYWORDS.items():
        for kw in keywords:
            pattern = rf"\b{re.escape(kw)}\b"
            if re.search(pattern, combined):
                return cat
    return RoleCategory.OTHER


def extract_explicit_ad_id(text: str) -> str | None:
    """Extract explicit published job reference ID (e.g. GP-2026-04) if present."""
    if not text:
        return None
    for pat in REF_PATTERNS:
        match = pat.search(text)
        if match:
            ref = match.group(1).strip().rstrip(".,;:")
            if ref:
                return ref
    return None


def extract_explicit_closing_date(text: str) -> tuple[datetime | None, str | None]:
    """Extract explicitly published application closing date if present.

    Missing or ambiguous closing dates (e.g. 'Apply ASAP', 'Immediate start', 'Ongoing')
    return (None, None) and never produce fabricated expiry dates.
    """
    if not text:
        return None, None

    for pat in CLOSING_DATE_PATTERNS:
        match = pat.search(text)
        if match:
            raw_date = match.group(1).strip()
            date_clean = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", raw_date)
            for fmt in ["%d %B %Y", "%d %b %Y", "%Y-%m-%d", "%d/%m/%Y"]:
                try:
                    dt = datetime.strptime(date_clean, fmt).replace(tzinfo=UTC, hour=23, minute=59, second=59)
                    return dt, raw_date
                except ValueError:
                    pass

    return None, None


def evaluate_opportunity_staleness(
    opportunity: OpportunityORM,
    reference_time: datetime | None = None,
) -> str:
    """Evaluate whether an opportunity has passed an explicitly published closing date.

    Expected:
        - Before expiry: returns current status (advertised or changed).
        - After expiry: returns possibly_stale.
        - Expiry alone must NOT produce explicitly_closed.
        - Expiry alone must NOT produce no_longer_observed.
        - Missing or ambiguous closing dates must NOT produce fabricated expiry.
    """
    if opportunity.status in (
        OpportunityStatus.EXPLICITLY_CLOSED.value,
        OpportunityStatus.NO_LONGER_OBSERVED.value,
    ):
        return opportunity.status

    closing_dt, _ = extract_explicit_closing_date(opportunity.verbatim_wording)
    if closing_dt is None:
        return opportunity.status

    ref = reference_time or _utcnow()
    if ref > closing_dt:
        return OpportunityStatus.POSSIBLY_STALE.value

    return opportunity.status


def extract_closure_notices(text_or_html: str, target_text: str | None = None) -> list[dict[str, Any]]:
    """Scan text or HTML for explicit closure evidence statements, referenced ad IDs, and offsets."""
    if not text_or_html:
        return []

    soup = BeautifulSoup(text_or_html, "html.parser")
    text_content = soup.get_text(separator=" ", strip=True)

    notices: list[dict[str, Any]] = []
    search_text = target_text if target_text is not None else text_content

    notice_elements = soup.find_all(class_=re.compile(r"notice|alert|closed|closure", re.I))
    for el in notice_elements:
        el_text = _normalize_text_basic(el.get_text())
        if any(w in el_text.lower() for w in ("closed", "no longer accepting")):
            ref_id = extract_explicit_ad_id(el_text)
            start_off = None
            end_off = None
            if search_text and el_text in search_text:
                start_off = search_text.find(el_text)
                end_off = start_off + len(el_text)
            notices.append({
                "quote": el_text,
                "explicit_ad_id": ref_id,
                "start_offset": start_off,
                "end_offset": end_off,
            })

    for pat in CLOSURE_PATTERNS:
        for match in pat.finditer(text_content):
            quote = _normalize_text_basic(match.group(1))
            if quote and len(quote) > 10:
                ref_id = extract_explicit_ad_id(quote)
                if not any(n["quote"] == quote for n in notices):
                    start_off = None
                    end_off = None
                    if search_text and quote in search_text:
                        start_off = search_text.find(quote)
                        end_off = start_off + len(quote)
                    notices.append({
                        "quote": quote,
                        "explicit_ad_id": ref_id,
                        "start_offset": start_off,
                        "end_offset": end_off,
                    })

    return notices


# =============================================================================
# EVIDENCE VERIFICATION FUNCTIONS
# =============================================================================

def verify_opportunity_evidence(
    opportunity: OpportunityORM,
    snapshot: PageSnapshot,
) -> tuple[bool, str, int | None, int | None]:
    """Validate supporting verbatim wording and character offsets against an immutable PageSnapshot."""
    if snapshot.source_id != opportunity.source_id:
        return False, "source_id_mismatch", None, None

    if snapshot.id not in (opportunity.first_snapshot_id, opportunity.last_snapshot_id):
        return False, "snapshot_provenance_mismatch", None, None

    snap_text = snapshot.text or ""
    quote = (opportunity.verbatim_wording or "").strip()
    if not quote:
        return False, "empty_quotation", None, None

    offset = snap_text.find(quote)
    if offset != -1:
        return True, "verified_exact", offset, offset + len(quote)

    norm_snap = _normalize_text_basic(snap_text)
    norm_quote = _normalize_text_basic(quote)
    norm_offset = norm_snap.find(norm_quote)
    if norm_offset != -1:
        sample = norm_quote[:30]
        raw_idx = snap_text.lower().find(sample.lower())
        if raw_idx != -1:
            return True, "verified_normalized", raw_idx, raw_idx + len(sample)
        return True, "verified_normalized", norm_offset, norm_offset + len(norm_quote)

    return False, "quotation_not_found_in_snapshot", None, None


def verify_closure_evidence(
    opportunity: OpportunityORM,
    snapshot: PageSnapshot,
    start_offset: int | None = None,
    end_offset: int | None = None,
) -> tuple[bool, str, int | None, int | None]:
    """Validate explicit closure evidence quotation and character offsets against PageSnapshot."""
    if opportunity.status != OpportunityStatus.EXPLICITLY_CLOSED.value:
        return False, "opportunity_not_explicitly_closed", None, None

    if not opportunity.closure_evidence_quote:
        return False, "missing_closure_evidence_quote", None, None

    snap_text = snapshot.text or ""
    snap_html = getattr(snapshot, "html", None) or ""
    quote = opportunity.closure_evidence_quote.strip()

    # If explicit offsets are specified, test exact slice matching
    if start_offset is not None and end_offset is not None:
        if 0 <= start_offset <= end_offset <= len(snap_text):
            sliced = snap_text[start_offset:end_offset]
            if sliced == quote:
                return True, "closure_verified_at_exact_offsets", start_offset, end_offset
            if _normalize_text_basic(sliced) == _normalize_text_basic(quote):
                return True, "closure_verified_at_exact_offsets", start_offset, end_offset
            return False, "offset_slice_mismatch", None, None
        return False, "offsets_out_of_bounds", None, None

    offset = snap_text.find(quote)
    if offset != -1:
        return True, "closure_verified_in_text", offset, offset + len(quote)

    norm_snap = _normalize_text_basic(snap_text)
    norm_quote = _normalize_text_basic(quote)
    norm_offset = norm_snap.find(norm_quote)
    if norm_offset != -1:
        return True, "closure_verified_normalized", norm_offset, norm_offset + len(norm_quote)

    if snap_html and quote in snap_html:
        html_offset = snap_html.find(quote)
        return True, "closure_verified_in_html", html_offset, html_offset + len(quote)

    return False, "closure_quote_not_found_in_snapshot", None, None


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class OpportunityCandidate:
    """Parsed opportunity candidate from snapshot observation."""
    role_category: RoleCategory
    role_title: str
    verbatim_wording: str
    contact_channel: str | None = None
    explicit_ad_id: str | None = None
    source_job_id: str | None = None
    closure_evidence_quote: str | None = None
    is_explicit_closure: bool = False
    branch_location: str | None = None


# =============================================================================
# SNAPSHOT OPPORTUNITY PARSER
# =============================================================================

def extract_opportunity_candidates(snapshot: PageSnapshot) -> list[OpportunityCandidate]:
    """Deterministically parse healthcare recruitment vacancies from a PageSnapshot.

    Handles structured headings, snapshot text sections, ref badges, and HTML if present.
    """
    candidates: list[OpportunityCandidate] = []
    text = snapshot.text or ""
    html = getattr(snapshot, "html", None) or ""

    # Strategy 1: Heading-based text stream extraction
    headings = getattr(snapshot, "headings", []) or []
    if headings and text:
        norm_headings = []
        for h in headings:
            if isinstance(h, dict):
                norm_headings.append({
                    "text": h.get("text", ""),
                    "start": h.get("start_offset", 0),
                    "end": h.get("end_offset", 0),
                })
            elif hasattr(h, "start_offset"):
                norm_headings.append({
                    "text": getattr(h, "text", ""),
                    "start": getattr(h, "start_offset", 0),
                    "end": getattr(h, "end_offset", 0),
                })

        for i, h in enumerate(norm_headings):
            if any(term in h["text"].lower() for term in ("now closed", "is closed", "have closed", "applications closed", "no longer accepting")):
                continue
            category = classify_role_category(h["text"])
            if category != RoleCategory.OTHER:
                h_end = h["end"]
                next_start = len(text)
                for later_h in norm_headings:
                    if later_h["start"] > h_end:
                        if later_h["start"] < next_start:
                            next_start = later_h["start"]

                body = text[h_end:next_start].strip()
                ad_id = extract_explicit_ad_id(body) or extract_explicit_ad_id(h["text"])
                email_match = EMAIL_PATTERN.search(body)
                phone_match = PHONE_PATTERN.search(body)
                contact = email_match.group(0) if email_match else (phone_match.group(0) if phone_match else None)

                body_lines = [line.strip() for line in body.split("\n") if line.strip()]
                desc_line = body
                for line in body_lines:
                    if len(line) > 20 and not line.lower().startswith("ref:"):
                        desc_line = line
                        break

                candidates.append(OpportunityCandidate(
                    role_category=category,
                    role_title=h["text"],
                    verbatim_wording=desc_line if desc_line else (body if body else h["text"]),
                    contact_channel=contact,
                    explicit_ad_id=ad_id,
                ))

    # Strategy 2: If no candidates found via headings and HTML is present, try HTML DOM parsing
    if not candidates and html:
        soup = BeautifulSoup(html, "html.parser")
        job_sections = soup.find_all(["section", "article", "div", "li"], class_=re.compile(r"job-opening|job-listing|vacancy|career|job-card", re.I))

        if job_sections:
            for section in job_sections:
                sec_text = _normalize_text_basic(section.get_text(separator=" ", strip=True))
                if not sec_text:
                    continue

                title_tag = section.find(["h2", "h3", "h4", "h1", "strong"], class_=re.compile(r"title|heading|role", re.I))
                if not title_tag:
                    title_tag = section.find(["h2", "h3", "h4", "h1"])
                title = _normalize_text_basic(title_tag.get_text()) if title_tag else ""
                if not title:
                    lines = sec_text.split(".")
                    title = lines[0].strip()[:100]

                if any(term in title.lower() for term in ("now closed", "is closed", "have closed", "applications closed", "no longer accepting")):
                    continue

                ref_tag = section.find(class_=re.compile(r"ref|reference|job-id", re.I))
                ad_id = None
                if ref_tag:
                    ad_id = extract_explicit_ad_id(ref_tag.get_text())
                if not ad_id:
                    ad_id = extract_explicit_ad_id(sec_text)

                email_match = EMAIL_PATTERN.search(sec_text)
                phone_match = PHONE_PATTERN.search(sec_text)
                contact_channel = email_match.group(0) if email_match else (phone_match.group(0) if phone_match else None)

                category = classify_role_category(title, sec_text)

                p_tag = section.find("p")
                wording = _normalize_text_basic(p_tag.get_text()) if p_tag else sec_text

                candidates.append(OpportunityCandidate(
                    role_category=category,
                    role_title=title,
                    verbatim_wording=wording if wording else sec_text,
                    contact_channel=contact_channel,
                    explicit_ad_id=ad_id,
                ))

    # Strategy 3: Plain text lines fallback if still empty
    if not candidates and text:
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        for idx, line in enumerate(lines):
            if any(term in line.lower() for term in ("now closed", "is closed", "have closed", "applications closed", "no longer accepting")):
                continue
            category = classify_role_category(line)
            if category != RoleCategory.OTHER and len(line) < 120:
                following = " ".join(lines[idx + 1:idx + 4])
                ad_id = extract_explicit_ad_id(following) or extract_explicit_ad_id(line)
                email_match = EMAIL_PATTERN.search(following)
                contact = email_match.group(0) if email_match else None
                candidates.append(OpportunityCandidate(
                    role_category=category,
                    role_title=line,
                    verbatim_wording=following if following else line,
                    contact_channel=contact,
                    explicit_ad_id=ad_id,
                ))

    return candidates


# =============================================================================
# OBSERVATION ELIGIBILITY & OMISSION PROTECTION
# =============================================================================

def is_observation_eligible_for_vacancies(obs: SourceObservationORM) -> bool:
    """Determine whether an observation attempt qualifies for vacancy lifecycle transitions."""
    if obs.outcome == ObservationOutcome.FAILED.value or obs.outcome == "failed":
        return False
    if obs.outcome == ObservationOutcome.INCOMPLETE.value or obs.outcome == "incomplete":
        return False
    if obs.outcome == ObservationOutcome.SUCCESS_UNCHANGED.value or obs.outcome == "success_unchanged":
        return False
    if obs.parsing_completeness != ParsingCompleteness.COMPLETE.value and obs.parsing_completeness != "complete":
        return False
    if obs.extraction_completeness != ExtractionCompleteness.COMPLETE.value and obs.extraction_completeness != "complete":
        return False

    return obs.outcome == ObservationOutcome.SUCCESS_NEW_SNAPSHOT.value or obs.outcome == "success_new_snapshot"


def is_vacancy_extraction_omission(
    opportunity: OpportunityORM,
    current_snapshot: PageSnapshot | None,
) -> bool:
    """Defense against extraction omissions.

    Rules:
        - If explicit_ad_id is visibly present in snapshot text or HTML:
          Confirmed omission! Suppress removal and freeze absence counter.
        - If vacancy-specific advertisement context (verbatim wording describing
          remuneration, requirements, or application channel) is still present:
          Confirmed omission!
        - If only an isolated, unrelated occurrence of a generic job title appears
          (e.g. staff listing, doctor bio, patient advice) without vacancy context:
          ABSTAIN (return False). Unrelated occurrences must NOT suppress genuine absence!
        - Uncertain extraction omissions result in abstention (return False).
    """
    if current_snapshot is None:
        return False

    raw_text = _normalize_text_basic(current_snapshot.text or "").lower()
    raw_html = (getattr(current_snapshot, "html", None) or "").lower()

    # 1. Definitive: published job reference ID in snapshot
    if opportunity.explicit_ad_id:
        ad_id_lower = opportunity.explicit_ad_id.lower().strip()
        if ad_id_lower in raw_text or ad_id_lower in raw_html:
            logger.info(
                "vacancy_extraction_omission_detected opp_id=%d explicit_ad_id=%s found_in_dom=True",
                opportunity.id, opportunity.explicit_ad_id,
            )
            return True

    # 2. Distinctive vacancy advertisement wording
    if opportunity.verbatim_wording:
        wording_norm = _normalize_text_basic(opportunity.verbatim_wording).lower()
        if len(wording_norm) >= 25 and (wording_norm[:40] in raw_text or wording_norm[:40] in raw_html):
            recruitment_markers = ("seeking", "apply", "billings", "remuneration", "ref:", "contact", "salary", "hours", "urgent")
            if any(m in wording_norm for m in recruitment_markers):
                logger.info(
                    "vacancy_extraction_omission_detected opp_id=%d wording_found_in_dom=True",
                    opportunity.id,
                )
                return True

    # 3. Isolated job titles or uncertain context: ABSTAIN to prevent suppressing genuine absence
    return False


# =============================================================================
# DETERMINISTIC IDENTITY RESOLUTION
# =============================================================================

def find_matching_opportunity(
    existing_opportunities: list[OpportunityORM],
    candidate: OpportunityCandidate,
    source_id: int,
    organisation_id: int,
    excluded_opp_ids: set[int] | None = None,
) -> OpportunityORM | None:
    """Match a candidate to an existing opportunity using deterministic identity hierarchy.

    Hierarchy:
        1. Explicit published job reference (explicit_ad_id).
        2. Stable source-local identifier / slug (source_job_id).
        3. Composite identity: organisation_id, role_category, branch_location, source_id.
           - Omits role title and remuneration so changes update the existing vacancy.
           - Ambiguous composite identities (e.g. multiple anonymous vacancies) are NEVER automatically merged.
           - Opportunities already matched in the current observation are excluded.
    """
    excluded = excluded_opp_ids or set()
    candidate_id = (candidate.explicit_ad_id or candidate.source_job_id or "").strip().lower()

    # Priority 1 & 2: Stable Identifier (Explicit Ad ID or Source-Local ID)
    if candidate_id:
        for opp in existing_opportunities:
            if opp.id in excluded:
                continue
            opp_id = (opp.explicit_ad_id or "").strip().lower()
            if opp_id == candidate_id:
                return opp
        # If candidate has a stable ID, it CANNOT match an anonymous opportunity without that ID
        return None

    # Priority 3: Composite Identity (when candidate has NO stable identifier)
    composite_candidates = [
        opp for opp in existing_opportunities
        if opp.id not in excluded
        and opp.organisation_id == organisation_id
        and opp.source_id == source_id
        and opp.role_category == candidate.role_category.value
        and not opp.explicit_ad_id
    ]

    # Exactly ONE unreferenced opportunity exists: match it
    if len(composite_candidates) == 1:
        return composite_candidates[0]

    # Multiple unreferenced opportunities exist: AMBIGUOUS! Abstain from guessing.
    return None


# =============================================================================
# CORE OPPORTUNITY LIFECYCLE DETECTOR
# =============================================================================

def resolve_source_organisation_id(session: Session, source_id: int, fallback_org_id: int | None = None) -> int:
    """Resolve canonical organisation_id for a registered source."""
    if fallback_org_id is not None:
        return fallback_org_id

    links = session.execute(
        select(OrganisationSourceLink).where(OrganisationSourceLink.source_id == source_id)
    ).scalars().all()

    if links:
        for link in links:
            if link.verification_status in ("confirmed", "verified"):
                return link.organisation_id
        return links[0].organisation_id

    source = session.get(Source, source_id)
    if source and source.domain:
        org = session.execute(
            select(Organisation).where(Organisation.primary_domain == source.domain)
        ).scalar_one_or_none()
        if org:
            return org.id

    raise ValueError(f"Cannot resolve organisation_id for source_id={source_id}. Ensure OrganisationSourceLink exists.")


def _emit_opportunity_change_event(
    session: Session,
    source_id: int,
    organisation_id: int,
    obs_id: int,
    previous_snapshot_id: int | None,
    current_snapshot_id: int,
    event_type: str,
    scope_key: str,
    old_value: str | None,
    new_value: str | None,
    rationale: str,
    diff_metadata: dict[str, Any],
    idempotency_key: str,
) -> None:
    """Record an immutable, append-only ChangeEvent for opportunity transitions (idempotent)."""
    existing = session.execute(
        select(ChangeEventORM).where(ChangeEventORM.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing:
        return

    # Check constraint: previous_snapshot_id IS NULL OR previous_snapshot_id != current_snapshot_id
    safe_prev_snap = previous_snapshot_id if previous_snapshot_id != current_snapshot_id else None

    evt = ChangeEventORM(
        source_id=source_id,
        organisation_id=organisation_id,
        previous_observation_id=None,
        current_observation_id=obs_id,
        previous_snapshot_id=safe_prev_snap,
        current_snapshot_id=current_snapshot_id,
        previous_fact_id=None,
        current_fact_id=None,
        event_type=event_type,
        entity_type="opportunity",
        field_key="vacancy",
        scope_key=scope_key,
        old_value=old_value,
        new_value=new_value,
        rationale=rationale,
        diff_metadata=diff_metadata if isinstance(diff_metadata, dict) else (json.loads(diff_metadata) if diff_metadata else {}),
        idempotency_key=idempotency_key,
    )
    session.add(evt)


def process_opportunity_observation(
    session: Session,
    obs: SourceObservationORM,
    candidates: list[OpportunityCandidate] | None = None,
    organisation_id: int | None = None,
    reference_time: datetime | None = None,
) -> list[OpportunityORM]:
    """Process an observation against the opportunity lifecycle state machine.

    Evaluates new candidates, modifications, absences, explicit closures, staleness, and reappearances.
    """
    source_id = obs.source_id
    org_id = resolve_source_organisation_id(session, source_id, organisation_id)
    current_snapshot = session.get(PageSnapshot, obs.snapshot_id) if obs.snapshot_id else None

    existing_opps = session.execute(
        select(OpportunityORM).where(OpportunityORM.source_id == source_id)
    ).scalars().all()
    existing_list = list(existing_opps)

    if candidates is None and current_snapshot:
        candidates = extract_opportunity_candidates(current_snapshot)
    elif candidates is None:
        candidates = []

    snap_text = current_snapshot.text or "" if current_snapshot else ""
    snap_html = getattr(current_snapshot, "html", None) or "" if current_snapshot else ""
    closure_notices = extract_closure_notices(snap_html or snap_text, target_text=snap_text) if current_snapshot else []

    eligible = is_observation_eligible_for_vacancies(obs)

    if not eligible:
        logger.info(
            "opportunity_observation_ineligible obs_id=%d outcome=%s parsing=%s extraction=%s freezing_absence_counters",
            obs.id, obs.outcome, obs.parsing_completeness, obs.extraction_completeness,
        )
        return existing_list

    matched_opp_ids: set[int] = set()

    for candidate in candidates:
        match = find_matching_opportunity(
            existing_list,
            candidate,
            source_id,
            org_id,
            excluded_opp_ids=matched_opp_ids,
        )

        effective_ad_id = candidate.explicit_ad_id or candidate.source_job_id

        if match is None:
            # New opportunity discovered
            new_opp = OpportunityORM(
                organisation_id=org_id,
                source_id=source_id,
                first_snapshot_id=obs.snapshot_id or 1,
                last_snapshot_id=obs.snapshot_id or 1,
                role_category=candidate.role_category.value,
                role_title=candidate.role_title,
                verbatim_wording=candidate.verbatim_wording,
                contact_channel=candidate.contact_channel,
                explicit_ad_id=effective_ad_id,
                status=OpportunityStatus.ADVERTISED.value,
                closure_evidence_quote=None,
                consecutive_absences=0,
                first_seen_at=current_snapshot.fetched_at if current_snapshot else _utcnow(),
                last_seen_at=current_snapshot.fetched_at if current_snapshot else _utcnow(),
                closed_at=None,
            )
            # Evaluate staleness if published with an already-passed closing date
            stale_status = evaluate_opportunity_staleness(new_opp, reference_time)
            if stale_status == OpportunityStatus.POSSIBLY_STALE.value:
                new_opp.status = OpportunityStatus.POSSIBLY_STALE.value

            session.add(new_opp)
            session.flush()
            existing_list.append(new_opp)
            matched_opp_ids.add(new_opp.id)

            _emit_opportunity_change_event(
                session=session,
                source_id=source_id,
                organisation_id=org_id,
                obs_id=obs.id,
                previous_snapshot_id=None,
                current_snapshot_id=obs.snapshot_id or 1,
                event_type="fact_added",
                scope_key=new_opp.role_category,
                old_value=None,
                new_value=new_opp.role_title,
                rationale=f"Discovered new {new_opp.role_category} workforce vacancy (ad_id={new_opp.explicit_ad_id})",
                diff_metadata={
                    "opportunity_id": new_opp.id,
                    "explicit_ad_id": new_opp.explicit_ad_id,
                    "role_title": new_opp.role_title,
                    "verbatim_wording": new_opp.verbatim_wording,
                    "status": new_opp.status,
                },
                idempotency_key=f"opp:{source_id}:{obs.id}:{new_opp.id}:added",
            )
            logger.info("opportunity_created id=%d role=%s ad_id=%s", new_opp.id, new_opp.role_category, new_opp.explicit_ad_id)
        else:
            matched_opp_ids.add(match.id)
            prev_last_snapshot = match.last_snapshot_id
            match.last_snapshot_id = obs.snapshot_id or match.last_snapshot_id
            if current_snapshot:
                match.last_seen_at = current_snapshot.fetched_at

            match.consecutive_absences = 0

            # Reappearance handling
            if match.status in (
                OpportunityStatus.UNCONFIRMED_PRIOR.value,
                OpportunityStatus.NO_LONGER_OBSERVED.value,
                OpportunityStatus.EXPLICITLY_CLOSED.value,
            ):
                prev_status = match.status
                orig_closure_quote = match.closure_evidence_quote
                match.status = OpportunityStatus.ADVERTISED.value
                match.closed_at = None
                match.closure_evidence_quote = None

                if candidate.role_title and candidate.role_title != match.role_title:
                    match.role_title = candidate.role_title
                if candidate.verbatim_wording and candidate.verbatim_wording != match.verbatim_wording:
                    match.verbatim_wording = candidate.verbatim_wording
                if candidate.contact_channel:
                    match.contact_channel = candidate.contact_channel

                # Staleness check upon return
                stale_status = evaluate_opportunity_staleness(match, reference_time)
                if stale_status == OpportunityStatus.POSSIBLY_STALE.value:
                    match.status = OpportunityStatus.POSSIBLY_STALE.value

                _emit_opportunity_change_event(
                    session=session,
                    source_id=source_id,
                    organisation_id=org_id,
                    obs_id=obs.id,
                    previous_snapshot_id=prev_last_snapshot,
                    current_snapshot_id=obs.snapshot_id or 1,
                    event_type="fact_reappeared",
                    scope_key=match.role_category,
                    old_value=f"status:{prev_status}",
                    new_value=f"status:{match.status}",
                    rationale=f"Workforce vacancy reappeared in complete observation (prev_status={prev_status})",
                    diff_metadata={
                        "opportunity_id": match.id,
                        "explicit_ad_id": match.explicit_ad_id,
                        "role_title": match.role_title,
                        "previous_status": prev_status,
                        "restored_status": match.status,
                        "original_closure_quote": orig_closure_quote,
                    },
                    idempotency_key=f"opp:{source_id}:{obs.id}:{match.id}:reappeared",
                )
                logger.info("opportunity_reappeared id=%d prev_status=%s new_status=%s", match.id, prev_status, match.status)

            elif match.status in (
                OpportunityStatus.ADVERTISED.value,
                OpportunityStatus.CHANGED.value,
                OpportunityStatus.POSSIBLY_STALE.value,
            ):
                old_title = match.role_title
                old_wording = match.verbatim_wording
                title_changed = candidate.role_title and candidate.role_title != match.role_title
                wording_changed = candidate.verbatim_wording and candidate.verbatim_wording != match.verbatim_wording

                if title_changed or wording_changed:
                    match.status = OpportunityStatus.CHANGED.value
                    if title_changed:
                        match.role_title = candidate.role_title
                    if wording_changed:
                        match.verbatim_wording = candidate.verbatim_wording
                    if candidate.contact_channel:
                        match.contact_channel = candidate.contact_channel

                    _emit_opportunity_change_event(
                        session=session,
                        source_id=source_id,
                        organisation_id=org_id,
                        obs_id=obs.id,
                        previous_snapshot_id=prev_last_snapshot,
                        current_snapshot_id=obs.snapshot_id or 1,
                        event_type="fact_modified",
                        scope_key=match.role_category,
                        old_value=old_title,
                        new_value=match.role_title,
                        rationale=f"Updated {match.role_category} workforce vacancy title/wording (ad_id={match.explicit_ad_id})",
                        diff_metadata={
                            "opportunity_id": match.id,
                            "explicit_ad_id": match.explicit_ad_id,
                            "old_title": old_title,
                            "new_title": match.role_title,
                            "old_wording": old_wording,
                            "new_wording": match.verbatim_wording,
                            "status": match.status,
                        },
                        idempotency_key=f"opp:{source_id}:{obs.id}:{match.id}:modified",
                    )
                    logger.info("opportunity_changed id=%d title_changed=%s wording_changed=%s", match.id, title_changed, wording_changed)

                # Check staleness
                stale_status = evaluate_opportunity_staleness(match, reference_time)
                if stale_status == OpportunityStatus.POSSIBLY_STALE.value:
                    match.status = OpportunityStatus.POSSIBLY_STALE.value

    # Check unmatched existing opportunities (absences and closures)
    for opp in existing_list:
        if opp.id in matched_opp_ids:
            continue

        # 1. Check for explicit closure notice in the snapshot
        closure_found = False
        for notice in closure_notices:
            notice_ad_id = notice.get("explicit_ad_id")
            if notice_ad_id and opp.explicit_ad_id and notice_ad_id.strip().lower() == opp.explicit_ad_id.strip().lower():
                opp.status = OpportunityStatus.EXPLICITLY_CLOSED.value
                opp.closure_evidence_quote = notice["quote"]
                opp.closed_at = current_snapshot.fetched_at if current_snapshot else _utcnow()
                opp.last_snapshot_id = obs.snapshot_id or opp.last_snapshot_id
                closure_found = True

                start_off = notice.get("start_offset")
                end_off = notice.get("end_offset")
                if start_off is None and snap_text and opp.closure_evidence_quote:
                    idx = snap_text.find(opp.closure_evidence_quote)
                    if idx != -1:
                        start_off = idx
                        end_off = idx + len(opp.closure_evidence_quote)

                _emit_opportunity_change_event(
                    session=session,
                    source_id=source_id,
                    organisation_id=org_id,
                    obs_id=obs.id,
                    previous_snapshot_id=opp.first_snapshot_id if opp.first_snapshot_id != obs.snapshot_id else None,
                    current_snapshot_id=obs.snapshot_id or 1,
                    event_type="fact_modified",
                    scope_key=opp.role_category,
                    old_value=opp.status,
                    new_value=OpportunityStatus.EXPLICITLY_CLOSED.value,
                    rationale=f"Explicit closure evidence confirmed: '{opp.closure_evidence_quote}'",
                    diff_metadata={
                        "opportunity_id": opp.id,
                        "explicit_ad_id": opp.explicit_ad_id,
                        "closure_evidence_quote": opp.closure_evidence_quote,
                        "closure_start_offset": start_off,
                        "closure_end_offset": end_off,
                        "status": OpportunityStatus.EXPLICITLY_CLOSED.value,
                    },
                    idempotency_key=f"opp:{source_id}:{obs.id}:{opp.id}:closed",
                )
                logger.info("opportunity_explicitly_closed id=%d quote='%s'", opp.id, opp.closure_evidence_quote)
                break

        if closure_found:
            continue

        if opp.status == OpportunityStatus.EXPLICITLY_CLOSED.value:
            continue

        # 2. Check extraction omission defense
        if is_vacancy_extraction_omission(opp, current_snapshot):
            logger.info("opportunity_omission_suppressed opp_id=%d freezing_counter", opp.id)
            continue

        # 3. Genuine absence: advance consecutive absence counter
        opp.consecutive_absences += 1
        opp.last_snapshot_id = obs.snapshot_id or opp.last_snapshot_id

        if opp.consecutive_absences == 1:
            opp.status = OpportunityStatus.UNCONFIRMED_PRIOR.value
            _emit_opportunity_change_event(
                session=session,
                source_id=source_id,
                organisation_id=org_id,
                obs_id=obs.id,
                previous_snapshot_id=opp.first_snapshot_id if opp.first_snapshot_id != obs.snapshot_id else None,
                current_snapshot_id=obs.snapshot_id or 1,
                event_type="unconfirmed_prior",
                scope_key=opp.role_category,
                old_value=opp.role_title,
                new_value=None,
                rationale=f"Vacancy absent from 1st eligible complete observation (consecutive_absences=1)",
                diff_metadata={
                    "opportunity_id": opp.id,
                    "explicit_ad_id": opp.explicit_ad_id,
                    "consecutive_absences": 1,
                    "status": OpportunityStatus.UNCONFIRMED_PRIOR.value,
                },
                idempotency_key=f"opp:{source_id}:{obs.id}:{opp.id}:unconfirmed_prior",
            )
            logger.info("opportunity_unconfirmed_prior id=%d", opp.id)
        elif opp.consecutive_absences >= 2:
            opp.status = OpportunityStatus.NO_LONGER_OBSERVED.value
            _emit_opportunity_change_event(
                session=session,
                source_id=source_id,
                organisation_id=org_id,
                obs_id=obs.id,
                previous_snapshot_id=opp.first_snapshot_id if opp.first_snapshot_id != obs.snapshot_id else None,
                current_snapshot_id=obs.snapshot_id or 1,
                event_type="fact_unobserved",
                scope_key=opp.role_category,
                old_value=opp.role_title,
                new_value=None,
                rationale=f"Vacancy absent across 2 consecutive complete observations (consecutive_absences={opp.consecutive_absences})",
                diff_metadata={
                    "opportunity_id": opp.id,
                    "explicit_ad_id": opp.explicit_ad_id,
                    "consecutive_absences": opp.consecutive_absences,
                    "status": OpportunityStatus.NO_LONGER_OBSERVED.value,
                },
                idempotency_key=f"opp:{source_id}:{obs.id}:{opp.id}:unobserved",
            )
            logger.info("opportunity_no_longer_observed id=%d absences=%d", opp.id, opp.consecutive_absences)

    session.flush()
    return existing_list


def scan_source_opportunities(
    session: Session,
    source_id: int,
    organisation_id: int | None = None,
    reference_time: datetime | None = None,
) -> list[OpportunityORM]:
    """Scan and process opportunities for a registered source across its observation stream."""
    observations = session.execute(
        select(SourceObservationORM)
        .where(SourceObservationORM.source_id == source_id)
        .order_by(SourceObservationORM.observation_sequence.asc())
    ).scalars().all()

    if not observations:
        snapshots = session.execute(
            select(PageSnapshot)
            .where(PageSnapshot.source_id == source_id)
            .order_by(PageSnapshot.fetched_at.asc())
        ).scalars().all()

        if snapshots:
            from backend.app.intelligence.observation_tracker import record_observation
            for snap in snapshots:
                obs = record_observation(
                    session=session,
                    source_id=source_id,
                    outcome=ObservationOutcome.SUCCESS_NEW_SNAPSHOT,
                    snapshot_id=snap.id,
                    parsing_completeness=ParsingCompleteness.COMPLETE,
                    extraction_completeness=ExtractionCompleteness.COMPLETE,
                )
                process_opportunity_observation(session, obs, organisation_id=organisation_id, reference_time=reference_time)
    else:
        for obs in observations:
            process_opportunity_observation(session, obs, organisation_id=organisation_id, reference_time=reference_time)

    return session.execute(
        select(OpportunityORM).where(OpportunityORM.source_id == source_id).order_by(OpportunityORM.id.asc())
    ).scalars().all()


def scan_organisation_opportunities(
    session: Session,
    organisation_id: int,
    reference_time: datetime | None = None,
) -> list[OpportunityORM]:
    """Scan and process opportunities across all sources linked to an organisation."""
    links = session.execute(
        select(OrganisationSourceLink).where(OrganisationSourceLink.organisation_id == organisation_id)
    ).scalars().all()

    results: list[OpportunityORM] = []
    seen_ids: set[int] = set()

    for link in links:
        opps = scan_source_opportunities(session, link.source_id, organisation_id=organisation_id, reference_time=reference_time)
        for opp in opps:
            if opp.id not in seen_ids:
                results.append(opp)
                seen_ids.add(opp.id)

    return results
