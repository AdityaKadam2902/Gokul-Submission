"""Evidence-backed cross-source discrepancy detection engine (ARCHITECTURE §6.1, DECISION_LOG D41).

Responsibilities:
    1. Retrieve eligible facts belonging to a confirmed practice organisation.
    2. Identify distinct-source comparisons (same organisation, distinct sources).
    3. Apply semantic field matching against an explicit allowlist.
    4. Apply strict evidence integrity validation:
       - Quotes must be non-empty and match the source snapshot text.
       - Offsets must be strictly within bounds.
       - Rejected facts and unverifiable evidence suppress value mismatch detection.
    5. Apply scope compatibility gating:
       - Identical, non-null scopes are eligible.
       - Conflicting scopes (e.g. clinic vs pathology) suppress comparison (0 discrepancy).
       - NULL scopes on either side do not compare equal (suppressed; NEVER produce value_mismatch).
    6. Apply conservative, deterministic normalisation:
       - Australian telephone numbers normalized to E.164. Formatting variations that normalize
         identically produce 0 discrepancy (F10). Conflicting valid numbers produce value_mismatch (F11).
         Unparseable/ambiguous numbers route to ambiguity (never false value_mismatch).
       - Opening hours, addresses and names distinguish safe normalization from semantic guessing;
         unsupported or freeform variations route to ambiguity.
    7. Classify disagreements using ConflictType taxonomy (value_mismatch, temporal_divergence, ambiguity):
       - Temporal divergence requires explicit, non-overlapping fact validity periods (not page updates or crawl times).
       - Overlapping or compatible validity periods are not classified as temporal divergence.
    8. Idempotently persist Discrepancy records with canonical ordering (fact_a_id < fact_b_id)
       and RESTRICT foreign-key preservation.
    9. Zero automatic winning source selection; zero fact value overwrites.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.logging import get_logger
from backend.app.db.orm import (
    Discrepancy as DiscrepancyORM,
    Fact,
    Organisation,
    OrganisationSourceLink,
    PageSnapshot,
    Source,
)
from backend.app.ingest.evidence_verifier import find_all_quote_offsets
from backend.app.ingest.field_normalizer import normalize_phone
from backend.app.intelligence.schemas import ConflictType, DiscrepancyStatus

logger = get_logger(__name__)

# Explicit, documented allowlist of semantically comparable practice fields
COMPARABLE_FIELD_ALLOWLIST: set[str] = {
    "public_phone",
    "phone",
    "reception_phone",
    "opening_hours",
    "hours",
    "address",
    "postcode",
    "organisation_name",
}

CANONICAL_FIELD_MAPPING: dict[str, str] = {
    "phone": "public_phone",
    "reception_phone": "public_phone",
    "public_phone": "public_phone",
    "hours": "opening_hours",
    "opening_hours": "opening_hours",
    "address": "address",
    "postcode": "postcode",
    "organisation_name": "organisation_name",
}

MONTH_MAP: dict[str, int] = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


@dataclass(frozen=True)
class TemporalValidity:
    start_year: int
    start_month: int
    end_year: int
    end_month: int
    is_bounded: bool


@dataclass(frozen=True)
class ValueComparisonResult:
    is_equivalent: bool
    is_ambiguous: bool = False
    ambiguity_reason: str | None = None


@dataclass
class DiscrepancyScanResult:
    organisation_id: int
    total_facts_scanned: int
    comparisons_evaluated: int
    discrepancies_detected: int
    discrepancies: list[DiscrepancyORM]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def get_canonical_field(field_key: str) -> str | None:
    """Return the canonical semantic field name if in allowlist, else None."""
    clean_key = field_key.strip().lower()
    return CANONICAL_FIELD_MAPPING.get(clean_key)


def _normalize_text_basic(text: str) -> str:
    """Safely normalize unicode, hyphens, and collapse whitespace without semantic distortion."""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    text = text.replace("—", "-").replace("–", "-").replace("−", "-")
    return re.sub(r"\s+", " ", text).strip().lower()


def verify_fact_evidence_integrity(fact: Fact, snapshot: PageSnapshot) -> bool:
    """Validate that fact evidence is genuine, non-empty, and matches snapshot text."""
    if fact.status == "rejected":
        return False
    if not fact.evidence:
        return False

    ev = fact.evidence
    quote = (ev.quote or "").strip()
    if not quote:
        return False
    if ev.snapshot_id != snapshot.id:
        return False

    snap_text = snapshot.text or ""
    if ev.start_offset is None or ev.end_offset is None:
        return False
    if ev.start_offset < 0 or ev.end_offset <= ev.start_offset or ev.end_offset > len(snap_text):
        return False

    # Check slice at stored offsets directly in original-text coordinates
    slice_text = snap_text[ev.start_offset:ev.end_offset]
    clean_slice = _normalize_text_basic(slice_text)
    clean_quote = _normalize_text_basic(quote)
    if clean_slice == clean_quote:
        return True

    # Offsets must accurately identify the quote in the snapshot; if mismatched, verification fails
    return False


def parse_explicit_validity_period(quote: str) -> TemporalValidity | None:
    """Extract explicit fact-specific validity period.

    Rejects generic page update/crawl timestamps (e.g. 'Page updated March 2025').
    Distinguishes fully bounded intervals ('valid from Jan 2024 to Dec 2024', 'valid for 2024')
    from open-ended dates. A phrase such as 'effective from January 2024' specifies only a start
    date without an end boundary, while 'valid through June 2024' or 'until December 2024' specifies
    only an end date without an explicit start boundary. Neither represents a fully bounded interval
    (is_bounded=False).
    """
    clean = quote.lower()

    # Exclude page-level maintenance metadata
    clean_no_meta = re.sub(
        r"\b(?:last\s+)?(?:page\s+|site\s+|content\s+)?(?:updated|modified|reviewed|crawled|checked|published)\s*(?:on|at|in|:)?\s*[a-z0-9,\s]+",
        "",
        clean,
    )

    # Require explicit fact-validity anchors
    m = re.search(
        r"\b(?:effective|valid|operating)\s+(?:from|between|during|for)?\s*([a-z0-9,\s\-]+)",
        clean_no_meta,
    )
    if not m:
        return None

    val_str = m.group(1)
    years = [int(y) for y in re.findall(r"\b(20\d{2})\b", val_str)]
    if not years:
        return None

    months = [MONTH_MAP[mo] for mo in re.findall(r"\b(" + "|".join(MONTH_MAP.keys()) + r")\b", val_str)]
    has_range_connector = bool(re.search(r"\b(?:to|until|through|and)\b|[-–—]", val_str))

    if len(years) >= 2 and has_range_connector:
        # Bounded between two explicit years
        s_y, e_y = years[0], years[1]
        s_m = months[0] if len(months) >= 1 else 1
        e_m = months[1] if len(months) >= 2 else 12
        return TemporalValidity(s_y, s_m, e_y, e_m, is_bounded=True)
    elif len(years) == 1 and len(months) >= 2 and has_range_connector:
        # Bounded between two months in the same year (e.g. 'from Jan to Dec 2024')
        s_y = years[0]
        s_m, e_m = months[0], months[1]
        return TemporalValidity(s_y, s_m, s_y, e_m, is_bounded=True)
    elif len(years) == 1 and bool(re.search(r"\b(?:for|during)\s+(?:the\s+)?(?:year\s+)?20\d{2}\b", clean_no_meta)):
        # Whole calendar year bounded (e.g. 'valid for 2024')
        y = years[0]
        return TemporalValidity(y, 1, y, 12, is_bounded=True)
    else:
        # Start-only or end-only / open-ended (e.g. 'effective from January 2024', 'valid through June 2024')
        s_y = years[0]
        s_m = months[0] if months else 1
        return TemporalValidity(s_y, s_m, s_y, s_m, is_bounded=False)


def check_temporal_divergence(fact_a: Fact, fact_b: Fact) -> bool:
    """Check whether explicit, bounded, non-overlapping fact validity periods explain the disagreement.

    Rules:
        - Both facts must have explicit, bounded validity periods (both start and end dates).
        - Two start-only effective dates ('effective from Jan 2024' vs 'effective from Mar 2025') do NOT
          establish non-overlap (an open-ended effective date does not terminate upon another date).
        - An explicit period compared with an undated fact produces False (never invent intervals).
        - Generic page update dates or crawl differences MUST NOT produce temporal divergence.
        - Overlapping or compatible validity periods MUST NOT be labelled temporally divergent.
    """
    ev_a = fact_a.evidence.quote if fact_a.evidence else ""
    ev_b = fact_b.evidence.quote if fact_b.evidence else ""

    period_a = parse_explicit_validity_period(ev_a)
    period_b = parse_explicit_validity_period(ev_b)

    if not period_a or not period_b:
        return False

    # Both periods MUST be explicitly bounded to establish non-overlap
    if not period_a.is_bounded or not period_b.is_bounded:
        return False

    start1 = period_a.start_year * 12 + period_a.start_month
    end1 = period_a.end_year * 12 + period_a.end_month
    start2 = period_b.start_year * 12 + period_b.start_month
    end2 = period_b.end_year * 12 + period_b.end_month

    # Disjoint intervals: period 1 ends strictly before period 2 begins, or vice versa
    is_disjoint = (end1 < start2) or (end2 < start1)
    return is_disjoint


def classify_temporal_conflict(
    fact_1: Fact,
    fact_2: Fact,
    scope_key: str,
    field_key: str,
) -> tuple[ConflictType, str]:
    """Classify conflict type between two differing facts based on temporal validity evidence.

    Rules:
        - Neither fact has fact-specific temporal evidence:
          Normal value comparison is permitted -> ConflictType.VALUE_MISMATCH.
        - Both facts have explicitly bounded, non-overlapping intervals:
          Classify as ConflictType.TEMPORAL_DIVERGENCE.
        - Both facts have explicitly bounded, overlapping intervals:
          Normal value comparison is permitted -> ConflictType.VALUE_MISMATCH.
        - One fact has an explicit validity interval and the other is undated:
          Temporal applicability is unresolved -> ConflictType.AMBIGUITY.
        - Either fact has only an open-ended effective date (is_bounded=False) and the other
          fact's temporal applicability is unresolved -> ConflictType.AMBIGUITY.
        - Generic page update dates and crawl timestamps must be ignored as validity evidence.
    """
    ev_1 = fact_1.evidence.quote if fact_1.evidence else ""
    ev_2 = fact_2.evidence.quote if fact_2.evidence else ""

    period_1 = parse_explicit_validity_period(ev_1)
    period_2 = parse_explicit_validity_period(ev_2)

    val_1_disp = fact_1.raw_value
    val_2_disp = fact_2.raw_value

    # Case 1: Neither fact has fact-specific temporal evidence
    if period_1 is None and period_2 is None:
        return (
            ConflictType.VALUE_MISMATCH,
            f"Value mismatch: Independent confirmed sources publish conflicting {field_key} values "
            f"('{val_1_disp}' vs '{val_2_disp}') on matching scope '{scope_key}'.",
        )

    # Case 4: One fact has an explicit validity interval and the other is undated
    if (period_1 is not None and period_2 is None) or (period_2 is not None and period_1 is None):
        return (
            ConflictType.AMBIGUITY,
            f"Ambiguity: Disagreement on {field_key} ('{val_1_disp}' vs '{val_2_disp}') involves "
            f"one source with an explicit validity period and another undated source; "
            f"temporal applicability is unresolved and requires operator review.",
        )

    # Case 5: Either fact has only an open-ended date (is_bounded=False)
    if not period_1.is_bounded or not period_2.is_bounded:
        return (
            ConflictType.AMBIGUITY,
            f"Ambiguity: Disagreement on {field_key} ('{val_1_disp}' vs '{val_2_disp}') involves "
            f"open-ended temporal validity evidence without verified non-overlapping boundaries; "
            f"temporal applicability is unresolved and requires operator review.",
        )

    # Both periods are explicitly bounded: check for overlap
    start1 = period_1.start_year * 12 + period_1.start_month
    end1 = period_1.end_year * 12 + period_1.end_month
    start2 = period_2.start_year * 12 + period_2.start_month
    end2 = period_2.end_year * 12 + period_2.end_month

    is_disjoint = (end1 < start2) or (end2 < start1)

    # Case 2: Both facts have explicitly bounded, non-overlapping intervals
    if is_disjoint:
        return (
            ConflictType.TEMPORAL_DIVERGENCE,
            f"Temporal divergence: Independent confirmed sources publish differing {field_key} values "
            f"('{val_1_disp}' vs '{val_2_disp}') referring to distinct non-overlapping validity periods.",
        )

    # Case 3: Both facts have explicitly bounded, overlapping intervals -> normal value comparison
    return (
        ConflictType.VALUE_MISMATCH,
        f"Value mismatch: Independent confirmed sources publish conflicting {field_key} values "
        f"('{val_1_disp}' vs '{val_2_disp}') during overlapping validity periods on matching scope '{scope_key}'.",
    )


def compare_field_values(
    canonical_field: str,
    val_a: str,
    val_b: str,
    scope_key: str,
) -> ValueComparisonResult:
    """Conservative, deterministic value comparison distinguishing equivalence, mismatch, and ambiguity."""
    if canonical_field == "public_phone":
        norm_a = normalize_phone(val_a).normalized_value
        norm_b = normalize_phone(val_b).normalized_value
        if norm_a and norm_b:
            if norm_a == norm_b:
                return ValueComparisonResult(is_equivalent=True)
            return ValueComparisonResult(is_equivalent=False, is_ambiguous=False)
        # Canonical E.164 normalisation failed on one or both values
        return ValueComparisonResult(
            is_equivalent=False,
            is_ambiguous=True,
            ambiguity_reason=f"Ambiguity: Telephone numbers on scope '{scope_key}' could not be canonically normalized to E.164 ('{val_a}' vs '{val_b}').",
        )

    if canonical_field == "postcode":
        digits_a = re.sub(r"\D", "", val_a)
        digits_b = re.sub(r"\D", "", val_b)
        if len(digits_a) == 4 and len(digits_b) == 4:
            if digits_a == digits_b:
                return ValueComparisonResult(is_equivalent=True)
            return ValueComparisonResult(is_equivalent=False, is_ambiguous=False)
        return ValueComparisonResult(
            is_equivalent=False,
            is_ambiguous=True,
            ambiguity_reason=f"Ambiguity: Postcode values ('{val_a}' vs '{val_b}') are not valid Australian 4-digit codes.",
        )

    if canonical_field == "opening_hours":
        clean_a = _normalize_text_basic(val_a)
        clean_b = _normalize_text_basic(val_b)
        if clean_a == clean_b:
            return ValueComparisonResult(is_equivalent=True)

        time_pat = r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b"
        times_a = re.findall(time_pat, clean_a)
        times_b = re.findall(time_pat, clean_b)

        if times_a and times_b:
            return ValueComparisonResult(is_equivalent=False, is_ambiguous=False)

        return ValueComparisonResult(
            is_equivalent=False,
            is_ambiguous=True,
            ambiguity_reason=f"Ambiguity: Opening hours description contains unsupported or freeform text ('{val_a}' vs '{val_b}') that cannot be deterministically verified as contradictory without human review.",
        )

    if canonical_field == "address":
        def _clean_addr(s: str) -> str:
            s = _normalize_text_basic(s)
            s = re.sub(r"[^\w\s]", " ", s)
            s = re.sub(r"\bstreet\b", "st", s)
            s = re.sub(r"\broad\b", "rd", s)
            s = re.sub(r"\bavenue\b", "ave", s)
            s = re.sub(r"\bparade\b", "pde", s)
            s = re.sub(r"\bhighway\b", "hwy", s)
            return re.sub(r"\s+", " ", s).strip()

        ca = _clean_addr(val_a)
        cb = _clean_addr(val_b)
        if ca == cb:
            return ValueComparisonResult(is_equivalent=True)

        street_type_pat = r"\b(?:st|rd|ave|pde|hwy|dr|drive|way|pl|place|lane|ln|cres|crescent|bvd|boulevard|ct|court)\b"
        has_street_a = bool(re.search(street_type_pat, ca)) and bool(re.search(r"\b\d+\b", ca))
        has_street_b = bool(re.search(street_type_pat, cb)) and bool(re.search(r"\b\d+\b", cb))
        if has_street_a and has_street_b:
            return ValueComparisonResult(is_equivalent=False, is_ambiguous=False)

        return ValueComparisonResult(
            is_equivalent=False,
            is_ambiguous=True,
            ambiguity_reason=f"Ambiguity: Address values ('{val_a}' vs '{val_b}') are descriptive or missing street numbers; operator review required.",
        )

    if canonical_field == "organisation_name":
        def _clean_name(s: str) -> str:
            s = _normalize_text_basic(s)
            s = re.sub(r"\b(?:pty|ltd|proprietary|limited)\b\.?", "", s)
            s = re.sub(r"[^\w\s]", " ", s)
            return re.sub(r"\s+", " ", s).strip()

        cna = _clean_name(val_a)
        cnb = _clean_name(val_b)
        if cna == cnb:
            return ValueComparisonResult(is_equivalent=True)

        if cna in cnb or cnb in cna:
            return ValueComparisonResult(
                is_equivalent=False,
                is_ambiguous=True,
                ambiguity_reason=f"Ambiguity: Organisation name variations ('{val_a}' vs '{val_b}') may represent trading vs legal entity names; operator review required.",
            )

        return ValueComparisonResult(is_equivalent=False, is_ambiguous=False)

    # General fallback text comparison
    clean_a = _normalize_text_basic(val_a)
    clean_b = _normalize_text_basic(val_b)
    if clean_a == clean_b:
        return ValueComparisonResult(is_equivalent=True)
    return ValueComparisonResult(is_equivalent=False, is_ambiguous=False)


def are_values_equivalent(canonical_field: str, val_a: str, val_b: str) -> bool:
    """Deterministic field-specific equivalence comparison."""
    return compare_field_values(canonical_field, val_a, val_b, "").is_equivalent


def evaluate_fact_pair_discrepancy(
    session: Session,
    fact_1: Fact,
    fact_2: Fact,
    confirmed_source_ids: set[int] | None = None,
) -> DiscrepancyORM | None:
    """Evaluate pairwise facts for cross-source discrepancy.

    Enforces:
        - Same non-null organisation_id
        - Both source links confirmed
        - Distinct source IDs
        - Supported semantic field in allowlist
        - Scope compatibility gating:
          * Matching non-null scopes compare
          * Differing scopes suppress comparison
          * NULL scope != NULL scope (suppressed; NEVER value_mismatch)
        - Strict evidence integrity check on both facts
        - Conservative value comparison (value_mismatch vs temporal_divergence vs ambiguity)
        - Canonical fact_a_id < fact_b_id ordering
    """
    # 1. Organisation identity check
    if fact_1.organisation_id is None or fact_2.organisation_id is None:
        return None
    if fact_1.organisation_id != fact_2.organisation_id:
        return None

    org_id = fact_1.organisation_id

    # 2. Retrieve snapshot records
    snap_1 = fact_1.snapshot or session.get(PageSnapshot, fact_1.snapshot_id)
    snap_2 = fact_2.snapshot or session.get(PageSnapshot, fact_2.snapshot_id)
    if not snap_1 or not snap_2:
        return None

    # 3. Evidence integrity check (both must be verified, genuine, and match snapshot text)
    if not verify_fact_evidence_integrity(fact_1, snap_1) or not verify_fact_evidence_integrity(fact_2, snap_2):
        logger.debug(
            "discrepancy_eval_skipped reason=unverifiable_evidence fact1=%d fact2=%d",
            fact_1.id, fact_2.id,
        )
        return None

    source_id_1 = snap_1.source_id
    source_id_2 = snap_2.source_id

    # 4. Distinct source check (same source cannot form a cross-source discrepancy)
    if source_id_1 == source_id_2:
        return None

    # 5. Confirmed source relationship check
    if confirmed_source_ids is None:
        confirmed_source_ids = set(
            session.scalars(
                select(OrganisationSourceLink.source_id).where(
                    OrganisationSourceLink.organisation_id == org_id,
                    OrganisationSourceLink.verification_status == "confirmed",
                )
            ).all()
        )

    if source_id_1 not in confirmed_source_ids or source_id_2 not in confirmed_source_ids:
        logger.debug(
            "discrepancy_eval_skipped reason=unconfirmed_source_link org_id=%d src1=%d src2=%d",
            org_id, source_id_1, source_id_2,
        )
        return None

    # 6. Semantic field allowlist check
    canon_field_1 = get_canonical_field(fact_1.field_key)
    canon_field_2 = get_canonical_field(fact_2.field_key)
    if not canon_field_1 or not canon_field_2 or canon_field_1 != canon_field_2:
        return None

    field_key = canon_field_1

    # 7. Scope compatibility gating
    scope_1 = fact_1.scope_key
    scope_2 = fact_2.scope_key

    # NULL scope rule (Decision D41): NULL never equals NULL. Suppress comparison; never value_mismatch
    if scope_1 is None or scope_2 is None:
        logger.debug(
            "discrepancy_eval_suppressed reason=unscoped_null_scope fact1=%d fact2=%d",
            fact_1.id, fact_2.id,
        )
        return None

    # Incompatible departmental scopes (e.g. clinic vs pathology): suppress comparison
    if scope_1.strip().lower() != scope_2.strip().lower():
        logger.debug(
            "discrepancy_eval_suppressed reason=incompatible_scope scope1=%s scope2=%s fact1=%d fact2=%d",
            scope_1, scope_2, fact_1.id, fact_2.id,
        )
        return None

    scope_key = scope_1.strip().lower()

    # 8. Value comparison
    val_1 = fact_1.normalized_value or fact_1.raw_value
    val_2 = fact_2.normalized_value or fact_2.raw_value

    comp_result = compare_field_values(field_key, val_1, val_2, scope_key)
    if comp_result.is_equivalent:
        logger.debug(
            "discrepancy_eval_no_conflict field=%s val1=%s val2=%s",
            field_key, val_1, val_2,
        )
        return None

    # 9. Conflict classification
    if comp_result.is_ambiguous:
        conflict_type = ConflictType.AMBIGUITY
        rationale = comp_result.ambiguity_reason or f"Ambiguity: Discrepancy on {field_key} requires operator review."
    else:
        conflict_type, rationale = classify_temporal_conflict(fact_1, fact_2, scope_key, field_key)

    # 10. Canonical fact_a_id < fact_b_id ordering
    fact_a = fact_1 if fact_1.id < fact_2.id else fact_2
    fact_b = fact_2 if fact_1.id < fact_2.id else fact_1

    # Check for existing discrepancy (idempotent duplicate prevention)
    existing = session.scalar(
        select(DiscrepancyORM).where(
            DiscrepancyORM.fact_a_id == fact_a.id,
            DiscrepancyORM.fact_b_id == fact_b.id,
        )
    )
    if existing:
        return existing

    now = _utcnow()
    discrepancy = DiscrepancyORM(
        organisation_id=org_id,
        field_key=field_key,
        scope_key=scope_key,
        fact_a_id=fact_a.id,
        fact_b_id=fact_b.id,
        conflict_type=conflict_type.value,
        status=DiscrepancyStatus.DETECTED.value,
        rationale=rationale,
        detected_at=now,
        created_at=now,
    )
    session.add(discrepancy)
    session.flush()

    logger.info(
        "discrepancy_detected org_id=%d field=%s scope=%s fact_a=%d fact_b=%d conflict=%s",
        org_id, field_key, scope_key, fact_a.id, fact_b.id, conflict_type.value,
    )
    return discrepancy


def scan_organisation_discrepancies(
    session: Session,
    organisation_id: int,
) -> DiscrepancyScanResult:
    """Scan all confirmed facts for an organisation and detect cross-source discrepancies."""
    org = session.get(Organisation, organisation_id)
    if not org:
        raise ValueError(f"Organisation with id {organisation_id} does not exist")

    # 1. Retrieve confirmed source IDs for this organisation
    confirmed_source_ids = set(
        session.scalars(
            select(OrganisationSourceLink.source_id).where(
                OrganisationSourceLink.organisation_id == organisation_id,
                OrganisationSourceLink.verification_status == "confirmed",
            )
        ).all()
    )

    if len(confirmed_source_ids) < 2:
        logger.info(
            "discrepancy_scan_skipped reason=insufficient_confirmed_sources org_id=%d count=%d",
            organisation_id, len(confirmed_source_ids),
        )
        return DiscrepancyScanResult(
            organisation_id=organisation_id,
            total_facts_scanned=0,
            comparisons_evaluated=0,
            discrepancies_detected=0,
            discrepancies=[],
        )

    # 2. Retrieve all non-rejected facts with valid evidence for this organisation
    facts = list(
        session.scalars(
            select(Fact).where(
                Fact.organisation_id == organisation_id,
                Fact.status != "rejected",
            )
        ).all()
    )

    # Filter to facts originating from confirmed sources and having non-empty evidence matching snapshot
    eligible_facts: list[Fact] = []
    for f in facts:
        snap = f.snapshot or session.get(PageSnapshot, f.snapshot_id)
        if snap and snap.source_id in confirmed_source_ids and verify_fact_evidence_integrity(f, snap):
            eligible_facts.append(f)

    # 3. Pairwise comparison across distinct sources
    detected: list[DiscrepancyORM] = []
    seen_pairs: set[tuple[int, int]] = set()
    comparisons_count = 0

    for i in range(len(eligible_facts)):
        for j in range(i + 1, len(eligible_facts)):
            f1 = eligible_facts[i]
            f2 = eligible_facts[j]

            pair_key = (min(f1.id, f2.id), max(f1.id, f2.id))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            comparisons_count += 1

            disc = evaluate_fact_pair_discrepancy(
                session=session,
                fact_1=f1,
                fact_2=f2,
                confirmed_source_ids=confirmed_source_ids,
            )
            if disc is not None and disc not in detected:
                detected.append(disc)

    session.flush()
    logger.info(
        "discrepancy_scan_complete org_id=%d eligible_facts=%d comparisons=%d discrepancies=%d",
        organisation_id, len(eligible_facts), comparisons_count, len(detected),
    )
    return DiscrepancyScanResult(
        organisation_id=organisation_id,
        total_facts_scanned=len(eligible_facts),
        comparisons_evaluated=comparisons_count,
        discrepancies_detected=len(detected),
        discrepancies=detected,
    )


def detect_organisation_discrepancies(
    session: Session,
    organisation_id: int,
) -> list[DiscrepancyORM]:
    """Scan all confirmed facts for an organisation and return detected discrepancies."""
    return scan_organisation_discrepancies(session, organisation_id).discrepancies
