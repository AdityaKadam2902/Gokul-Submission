"""Evidence-aware information freshness & monitoring policy engine (ARCHITECTURE §6.1, D46).

Responsibility:
    Evaluates how current observed healthcare-practice information is, strictly distinguishing
    observation recency from information validity, content immutability, and evidence verification.

Four Distinct Freshness Dimensions (D46):
    1. Observation Freshness:
       How recently PracticeSignal successfully observed a source. Evaluated from the
       source_observations history (last attempted, last successful new snapshot, last successful
       unchanged heartbeat, last complete parsing, and last complete extraction). Failed and
       incomplete crawls do not update successful observation currency.
    2. Content Freshness:
       How recently the source's observed content actually changed, determined via immutable page
       snapshots and content hashes. Unchanged observations confirm continued observability of the
       existing snapshot without advancing content authorship or modification dates.
    3. Fact Freshness:
       How recently the factual information was explicitly published, updated, or made effective.
       Requires explicit, fact-specific temporal evidence in the supporting quotation (e.g.
       'effective from 1 Jan 2026', 'updated March 2026'). Undated facts are classified as UNKNOWN.
       NEVER inferred from crawl time (fetched_at/observed_at), generic page metadata, or DB timestamps.
    4. Evidence Verification Freshness:
       How recently the underlying evidence quotation and source snapshot were verified for integrity
       using the M2 evidence verification system. A valid quotation remains verifiable even when old.

Guarantees:
    - DETERMINISTIC EVALUATION: Calculations use an explicit or injected reference clock.
    - CONFIGURABLE POLICIES: Category-specific thresholds (identity 90d, operational 30d, etc.)
      with source-specific override capabilities and versioning.
    - NO LLM IN THE LOOP: Freshness calculations are 100% deterministic and rule-based.
    - NO UNKNOWN/STALE CONFUSION: Undated facts are classified as UNKNOWN, never falsely marked STALE.
    - NO CRAWL/FACT CONFUSION: A fresh crawl never magically makes an old or undated fact fresh.
    - PRESERVATION OF LAST VALID OBSERVATION: A failed latest crawl does not erase prior valid currency.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.logging import get_logger
from backend.app.db.orm import (
    Fact,
    Organisation,
    OrganisationSourceLink,
    PageSnapshot,
    Source,
    SourceObservation,
)
from backend.app.intelligence.discrepancy_detector import verify_fact_evidence_integrity
from backend.app.intelligence.schemas import (
    EvidenceVerificationFreshness,
    FactFreshnessDetail,
    FreshnessCategory,
    FreshnessInformationCategory,
    FreshnessPolicy,
    OrganisationFreshnessSummary,
    SourceContentFreshness,
    SourceObservationFreshness,
    TemporalPrecision,
)

logger = get_logger(__name__)

# Global default monitoring policy (D46)
DEFAULT_FRESHNESS_POLICY = FreshnessPolicy(
    policy_id="ps_freshness_v1",
    version="1.0.0",
    description="PracticeSignal standard deterministic monitoring policy",
    source_observation_threshold_days=30,  # Expected observation crawl heartbeat: 30 days
    source_content_threshold_days=90,      # Content unchanged alerting threshold: 90 days
    category_thresholds_days={
        "identity": 90,     # Stable practice identity: 90 days
        "operational": 30,  # Operational contact details: 30 days
        "services": 60,     # Clinical & allied health services: 60 days
        "hours": 30,        # Operating hours: 30 days
        "general": 60,      # All other factual fields: 60 days
    },
    aging_threshold_ratio=0.8,
)

MONTH_MAP = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """Ensure a datetime is timezone-aware in UTC, normalizing offset-naive datetimes from SQLite."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def map_field_to_category(field_key: str | None) -> FreshnessInformationCategory:
    """Map canonical field key to information category for policy threshold lookup."""
    k = (field_key or "").strip().lower()
    if k in ("practice_name", "name", "address_street", "suburb", "postcode", "state", "abn", "corporate_parent", "acn"):
        return FreshnessInformationCategory.IDENTITY
    if k in ("public_phone", "phone", "email", "fax", "website", "contact_channel"):
        return FreshnessInformationCategory.OPERATIONAL
    if k in ("service_names", "services", "clinical_services", "allied_health", "specialties", "practitioners", "doctors"):
        return FreshnessInformationCategory.SERVICES
    if k in ("opening_hours", "hours", "operating_hours", "holiday_hours"):
        return FreshnessInformationCategory.HOURS
    return FreshnessInformationCategory.GENERAL


@dataclass
class ParsedFactDate:
    """Explicitly parsed fact temporal evidence preserving precision and interval range."""
    precision: TemporalPrecision
    date_start: datetime | None
    date_end: datetime | None
    matched_phrase: str
    date_type: str
    raw_date_text: str

    @property
    def explicit_date(self) -> datetime | None:
        """Representative single date if exact (DAY) or range start."""
        return self.date_start

    def __iter__(self):
        # 3-tuple unpacking backward-compatibility: (explicit_date, matched_phrase, date_type)
        return iter((self.date_start, self.matched_phrase, self.date_type))


def _parse_temporal_expression(raw_text: str) -> tuple[TemporalPrecision, datetime | None, datetime | None]:
    """Parse raw date string into TemporalPrecision and (date_start, date_end) range in UTC.

    Preserves:
    - DAY: exact day interval [00:00:00, 23:59:59]
    - MONTH: full calendar month interval [1st 00:00:00, last_day 23:59:59]
    - YEAR: full calendar year interval [Jan 1 00:00:00, Dec 31 23:59:59]
    - UNKNOWN: missing year/day, incomplete or unparseable -> (None, None)

    Never infers missing year or day from crawl timestamps, current time, or DB creation dates.
    """
    s = re.sub(r"(?<=[0-9])(?:st|nd|rd|th)\b", "", raw_text.strip().lower())
    s = s.replace(",", " ").strip()
    parts = s.split()

    if "/" in s:
        nums = [int(p) for p in s.split("/") if p.isdigit()]
        if len(nums) == 3:
            d, m, y = nums
            if y < 100:
                y += 2000
            m = min(max(m, 1), 12)
            _, max_d = calendar.monthrange(y, m)
            d = min(max(d, 1), max_d)
            start = datetime(y, m, d, 0, 0, 0, tzinfo=UTC)
            end = datetime(y, m, d, 23, 59, 59, tzinfo=UTC)
            return TemporalPrecision.DAY, start, end

    if "-" in s and len(parts) == 1:
        nums = [int(p) for p in s.split("-") if p.isdigit()]
        if len(nums) == 3:
            y, m, d = nums
            m = min(max(m, 1), 12)
            _, max_d = calendar.monthrange(y, m)
            d = min(max(d, 1), max_d)
            start = datetime(y, m, d, 0, 0, 0, tzinfo=UTC)
            end = datetime(y, m, d, 23, 59, 59, tzinfo=UTC)
            return TemporalPrecision.DAY, start, end

    # 3 parts: day month year or month day year
    if len(parts) == 3:
        if parts[0].isdigit() and parts[1] in MONTH_MAP and parts[2].isdigit():
            day = int(parts[0])
            month = MONTH_MAP[parts[1]]
            year = int(parts[2])
            _, last_day = calendar.monthrange(year, month)
            day = min(max(day, 1), last_day)
            return TemporalPrecision.DAY, datetime(year, month, day, 0, 0, 0, tzinfo=UTC), datetime(year, month, day, 23, 59, 59, tzinfo=UTC)
        if parts[0] in MONTH_MAP and parts[1].isdigit() and parts[2].isdigit():
            month = MONTH_MAP[parts[0]]
            day = int(parts[1])
            year = int(parts[2])
            _, last_day = calendar.monthrange(year, month)
            day = min(max(day, 1), last_day)
            return TemporalPrecision.DAY, datetime(year, month, day, 0, 0, 0, tzinfo=UTC), datetime(year, month, day, 23, 59, 59, tzinfo=UTC)

    # 2 parts: month year OR month day (missing year)
    if len(parts) == 2:
        if parts[0] in MONTH_MAP and parts[1].isdigit():
            val = int(parts[1])
            if val >= 1900:  # year
                month = MONTH_MAP[parts[0]]
                _, last_day = calendar.monthrange(val, month)
                return TemporalPrecision.MONTH, datetime(val, month, 1, 0, 0, 0, tzinfo=UTC), datetime(val, month, last_day, 23, 59, 59, tzinfo=UTC)
            else:  # day without year! Missing year precision
                return TemporalPrecision.UNKNOWN, None, None
        if parts[0].isdigit() and parts[1] in MONTH_MAP:
            return TemporalPrecision.UNKNOWN, None, None

    # 1 part: year
    if len(parts) == 1 and parts[0].isdigit() and len(parts[0]) == 4:
        y = int(parts[0])
        return TemporalPrecision.YEAR, datetime(y, 1, 1, 0, 0, 0, tzinfo=UTC), datetime(y, 12, 31, 23, 59, 59, tzinfo=UTC)

    return TemporalPrecision.UNKNOWN, None, None


def extract_fact_temporal_evidence(
    quote: str | None,
) -> ParsedFactDate | None:
    """Deterministically parse explicit fact-specific publication, update, or effective date.

    Preserves temporal date precision (DAY, MONTH, YEAR, UNKNOWN).
    Rejects generic page maintenance / crawl metadata (e.g. 'Page updated March 2025', 'Site checked 2025').
    Returns ParsedFactDate with precision and interval range, or None if undated.
    """
    if not quote or not quote.strip():
        return None

    clean = quote.strip()
    clean_lower = clean.lower()

    # Step 1: Filter out generic page-level metadata headers/footers
    clean_no_meta = re.sub(
        r"\b(?:last\s+(?:updated|modified|reviewed|checked)|(?:page|site|website|webpage|content)\s+(?:updated|modified|reviewed|checked|published))\s*(?:on|at|in|:)?\s*[a-z0-9,\s\-]+(?:\.|$)",
        " ",
        clean_lower,
    )

    date_regex = (
        r"("
        r"[0-9]{1,2}(?:st|nd|rd|th)?\s+[a-z]+(?:\s*,\s*|\s+)[0-9]{4}"
        r"|[a-z]+\s+[0-9]{1,2}(?:st|nd|rd|th)?(?:\s*,\s*|\s+)[0-9]{4}"
        r"|[a-z]+\s+[0-9]{1,2}(?:st|nd|rd|th)?\b"
        r"|[0-9]{1,2}(?:st|nd|rd|th)?\s+[a-z]+\b"
        r"|[a-z]+\s+[0-9]{4}\b"
        r"|[0-9]{4}-[0-9]{2}-[0-9]{2}\b"
        r"|[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}\b"
        r"|[0-9]{4}\b"
        r")"
    )

    # Step 2: Look for explicit fact anchors
    # Pattern A: Effective date
    m_eff = re.search(r"\b(effective\s+(?:from|on|between|date)?\s*" + date_regex + r")", clean_no_meta)
    if m_eff:
        precision, start, end = _parse_temporal_expression(m_eff.group(2))
        return ParsedFactDate(
            precision=precision,
            date_start=start,
            date_end=end,
            matched_phrase=m_eff.group(1).strip(),
            date_type="effective_date",
            raw_date_text=m_eff.group(2).strip(),
        )

    # Pattern B: Explicit announcement / notice published date
    m_pub = re.search(r"\b((?:notice|announcement|article|fee\s+schedule|policy)\s+published\s*(?:on|in|at|:)?\s*" + date_regex + r")", clean_no_meta)
    if m_pub:
        precision, start, end = _parse_temporal_expression(m_pub.group(2))
        return ParsedFactDate(
            precision=precision,
            date_start=start,
            date_end=end,
            matched_phrase=m_pub.group(1).strip(),
            date_type="published_date",
            raw_date_text=m_pub.group(2).strip(),
        )

    # Pattern C: Explicitly dated attribute update
    m_upd = re.search(r"\b((?:hours|fees|services?|pricing|schedule|roster)\s+updated\s*(?:on|in|at|:)?\s*" + date_regex + r")", clean_no_meta)
    if m_upd:
        precision, start, end = _parse_temporal_expression(m_upd.group(2))
        return ParsedFactDate(
            precision=precision,
            date_start=start,
            date_end=end,
            matched_phrase=m_upd.group(1).strip(),
            date_type="updated_date",
            raw_date_text=m_upd.group(2).strip(),
        )

    # Pattern D: Explicit validity period
    m_val = re.search(r"\b(valid\s+(?:from|for|through|during)?\s*" + date_regex + r")", clean_no_meta)
    if m_val:
        precision, start, end = _parse_temporal_expression(m_val.group(2))
        return ParsedFactDate(
            precision=precision,
            date_start=start,
            date_end=end,
            matched_phrase=m_val.group(1).strip(),
            date_type="validity_date",
            raw_date_text=m_val.group(2).strip(),
        )

    # Pattern E: "as of" date
    m_asof = re.search(r"\b(as\s+of\s+" + date_regex + r")", clean_no_meta)
    if m_asof:
        precision, start, end = _parse_temporal_expression(m_asof.group(2))
        return ParsedFactDate(
            precision=precision,
            date_start=start,
            date_end=end,
            matched_phrase=m_asof.group(1).strip(),
            date_type="as_of_date",
            raw_date_text=m_asof.group(2).strip(),
        )

    return None


def evaluate_source_observation_freshness(
    session: Session,
    source_id: int,
    policy: FreshnessPolicy | None = None,
    reference_time: datetime | None = None,
) -> SourceObservationFreshness:
    """Evaluate observation freshness for a registered source based on observation attempts."""
    source = session.get(Source, source_id)
    if not source:
        raise ValueError(f"Source with id {source_id} does not exist")

    pol = policy or DEFAULT_FRESHNESS_POLICY
    ref_time = _ensure_utc(reference_time or _utcnow())
    threshold = pol.get_source_observation_threshold(source_id)

    # Fetch all observations chronologically
    observations = list(
        session.scalars(
            select(SourceObservation)
            .where(SourceObservation.source_id == source_id)
            .order_by(SourceObservation.observed_at.asc(), SourceObservation.id.asc())
        ).all()
    )

    if not observations:
        return SourceObservationFreshness(
            source_id=source_id,
            last_attempted_at=None,
            last_successful_observation_at=None,
            last_new_snapshot_at=None,
            last_unchanged_observation_at=None,
            last_complete_parsing_at=None,
            last_complete_extraction_at=None,
            observation_age_days=None,
            classification=FreshnessCategory.UNOBSERVABLE,
            policy_id=pol.policy_id,
            threshold_days=threshold,
            threshold_governance="source_observation",
            latest_attempt_status=None,
            latest_attempt_failed=False,
            as_of_reference_time=ref_time,
            rationale=f"Source {source_id} ({source.url}) has zero recorded observation attempts.",
        )

    last_attempted = observations[-1]
    successful_obs = [
        o for o in observations
        if (o.outcome or "").lower() in ("success_new_snapshot", "success_unchanged")
    ]
    new_snap_obs = [
        o for o in observations
        if (o.outcome or "").lower() == "success_new_snapshot"
    ]
    unchanged_obs = [
        o for o in observations
        if (o.outcome or "").lower() == "success_unchanged"
    ]
    complete_parsing_obs = [
        o for o in observations
        if (o.parsing_completeness or "").lower() == "complete"
    ]
    complete_extract_obs = [
        o for o in observations
        if (o.extraction_completeness or "").lower() == "complete"
    ]

    last_attempted_at = _ensure_utc(last_attempted.observed_at)
    last_successful_at = _ensure_utc(successful_obs[-1].observed_at) if successful_obs else None
    last_new_snap_at = _ensure_utc(new_snap_obs[-1].observed_at) if new_snap_obs else None
    last_unchanged_at = _ensure_utc(unchanged_obs[-1].observed_at) if unchanged_obs else None
    last_complete_parse_at = _ensure_utc(complete_parsing_obs[-1].observed_at) if complete_parsing_obs else None
    last_complete_extract_at = _ensure_utc(complete_extract_obs[-1].observed_at) if complete_extract_obs else None

    latest_attempt_status = last_attempted.outcome
    latest_attempt_failed = (latest_attempt_status or "").lower() in ("failed", "incomplete")

    if not last_successful_at:
        # Zero successful observations: unobservable
        return SourceObservationFreshness(
            source_id=source_id,
            last_attempted_at=last_attempted_at,
            last_successful_observation_at=None,
            last_new_snapshot_at=None,
            last_unchanged_observation_at=None,
            last_complete_parsing_at=last_complete_parse_at,
            last_complete_extraction_at=last_complete_extract_at,
            observation_age_days=None,
            classification=FreshnessCategory.UNOBSERVABLE,
            policy_id=pol.policy_id,
            threshold_days=threshold,
            threshold_governance="source_observation",
            latest_attempt_status=latest_attempt_status,
            latest_attempt_failed=latest_attempt_failed,
            as_of_reference_time=ref_time,
            rationale=(
                f"Source {source_id} has {len(observations)} observation attempt(s), but zero successful crawls. "
                f"Latest attempt failed with outcome '{latest_attempt_status}'."
            ),
        )

    # Calculate observation age from the latest successful observation attempt
    # (preserving last valid observation when latest attempt fails)
    age_days = max(0.0, (ref_time - last_successful_at).total_seconds() / 86400.0)
    classification = pol.classify_age(age_days, threshold)

    if latest_attempt_failed:
        rationale = (
            f"Last successful observation occurred on {last_successful_at.date()} ({age_days:.1f} days ago vs "
            f"{threshold}d policy threshold: {classification.value}). However, latest attempted crawl at "
            f"{last_attempted_at} failed with outcome '{latest_attempt_status}' ({last_attempted.error_details or 'no details'})."
        )
    else:
        rationale = (
            f"Observation freshness is {classification.value}: Last successful crawl was observed on "
            f"{last_successful_at.date()} ({age_days:.1f} days ago against {threshold}d policy threshold)."
        )

    return SourceObservationFreshness(
        source_id=source_id,
        last_attempted_at=last_attempted_at,
        last_successful_observation_at=last_successful_at,
        last_new_snapshot_at=last_new_snap_at,
        last_unchanged_observation_at=last_unchanged_at,
        last_complete_parsing_at=last_complete_parse_at,
        last_complete_extraction_at=last_complete_extract_at,
        observation_age_days=round(age_days, 2),
        classification=classification,
        policy_id=pol.policy_id,
        threshold_days=threshold,
        threshold_governance="source_observation",
        latest_attempt_status=latest_attempt_status,
        latest_attempt_failed=latest_attempt_failed,
        as_of_reference_time=ref_time,
        rationale=rationale,
    )


def evaluate_source_content_freshness(
    session: Session,
    source_id: int,
    policy: FreshnessPolicy | None = None,
    reference_time: datetime | None = None,
) -> SourceContentFreshness:
    """Evaluate when a source's observed content was first captured by PracticeSignal."""
    source = session.get(Source, source_id)
    if not source:
        raise ValueError(f"Source with id {source_id} does not exist")

    pol = policy or DEFAULT_FRESHNESS_POLICY
    ref_time = _ensure_utc(reference_time or _utcnow())
    threshold = pol.get_source_content_threshold(source_id)

    snapshots = list(
        session.scalars(
            select(PageSnapshot)
            .where(PageSnapshot.source_id == source_id)
            .order_by(PageSnapshot.fetched_at.asc(), PageSnapshot.id.asc())
        ).all()
    )

    if not snapshots:
        return SourceContentFreshness(
            source_id=source_id,
            current_version_first_observed_at=None,
            previous_version_observed_at=None,
            previous_version_content_hash=None,
            content_observation_age_days=None,
            actual_publisher_modified_at=None,
            last_content_change_at=None,
            content_age_days=None,
            latest_content_hash=None,
            snapshot_count=0,
            unchanged_reobservations_count=0,
            policy_id=pol.policy_id,
            threshold_days=threshold,
            threshold_governance="source_content",
            as_of_reference_time=ref_time,
            rationale=f"Source {source_id} has no stored page snapshots to evaluate content freshness.",
        )

    latest_snap = snapshots[-1]
    # First observation of current version: earliest snapshot sharing latest_snap's content_hash
    current_hash_snaps = [s for s in snapshots if s.content_hash == latest_snap.content_hash]
    first_current_snap = current_hash_snaps[0]
    current_version_first_observed_at = _ensure_utc(first_current_snap.fetched_at)
    content_obs_age = max(0.0, (ref_time - current_version_first_observed_at).total_seconds() / 86400.0)

    # Previous version observation: most recent snapshot with a different content_hash
    prev_snaps = [s for s in snapshots if s.content_hash != latest_snap.content_hash]
    previous_version_observed_at = _ensure_utc(prev_snaps[-1].fetched_at) if prev_snaps else None
    previous_version_content_hash = prev_snaps[-1].content_hash if prev_snaps else None

    # Total unchanged heartbeats
    total_unchanged_count = len(list(session.scalars(
        select(SourceObservation.id)
        .where(
            SourceObservation.source_id == source_id,
            SourceObservation.outcome == "success_unchanged",
        )
    ).all()))

    rationale = (
        f"Current content version (hash {latest_snap.content_hash[:10]}...) was first observed by PracticeSignal "
        f"on {current_version_first_observed_at.date()} ({content_obs_age:.1f} days ago). "
        f"Note: This timestamp reflects PracticeSignal crawler observation timing, not publisher modification timestamp. "
        f"Publisher modification timestamp is not explicitly published in content metadata. "
        f"Source has {len(snapshots)} distinct stored snapshot(s) and {total_unchanged_count} unchanged heartbeat re-observation(s)."
    )

    return SourceContentFreshness(
        source_id=source_id,
        current_version_first_observed_at=current_version_first_observed_at,
        previous_version_observed_at=previous_version_observed_at,
        previous_version_content_hash=previous_version_content_hash,
        content_observation_age_days=round(content_obs_age, 2),
        actual_publisher_modified_at=None,
        last_content_change_at=current_version_first_observed_at,
        content_age_days=round(content_obs_age, 2),
        latest_content_hash=latest_snap.content_hash,
        snapshot_count=len(snapshots),
        unchanged_reobservations_count=total_unchanged_count,
        policy_id=pol.policy_id,
        threshold_days=threshold,
        threshold_governance="source_content",
        as_of_reference_time=ref_time,
        rationale=rationale,
    )


def evaluate_evidence_verification_freshness(
    fact: Fact,
    snapshot: PageSnapshot | None,
    policy: FreshnessPolicy | None = None,
    reference_time: datetime | None = None,
) -> EvidenceVerificationFreshness:
    """Evaluate freshness of evidence verification for a stored fact."""
    pol = policy or DEFAULT_FRESHNESS_POLICY
    ref_time = _ensure_utc(reference_time or _utcnow())
    if not fact.evidence:
        return EvidenceVerificationFreshness(
            evidence_id=None,
            snapshot_id=fact.snapshot_id,
            captured_at=None,
            checked_at=ref_time,
            evaluated_at=ref_time,
            is_verified=False,
            quote_length=0,
            policy_id=pol.policy_id,
            threshold_governance="immutable_snapshot_integrity",
            as_of_reference_time=ref_time,
            rationale="Fact has no linked evidence quotation record.",
        )

    ev = fact.evidence
    is_verified = verify_fact_evidence_integrity(fact, snapshot) if snapshot else False
    quote_len = len(ev.quote or "")
    captured_at = _ensure_utc(ev.captured_at) if ev.captured_at else None

    rationale = (
        f"Evidence quote ({quote_len} chars) originally captured on {captured_at.date() if captured_at else 'unknown'} "
        f"was re-verified verbatim at stored offsets in snapshot DOM text at check time {ref_time.date()}. "
        f"Integrity is verified; evidence validity does not degrade simply with age."
        if is_verified else
        f"Evidence quotation could not be verified verbatim in snapshot text at check time {ref_time.date()} (offset broken or altered)."
    )

    return EvidenceVerificationFreshness(
        evidence_id=ev.id,
        snapshot_id=fact.snapshot_id,
        captured_at=captured_at,
        checked_at=ref_time,
        evaluated_at=ref_time,
        is_verified=is_verified,
        quote_length=quote_len,
        policy_id=pol.policy_id,
        threshold_governance="immutable_snapshot_integrity",
        as_of_reference_time=ref_time,
        rationale=rationale,
    )


# Alias for concise invocation
evaluate_evidence_freshness = evaluate_evidence_verification_freshness



def evaluate_fact_freshness(
    session: Session,
    fact_id: int,
    policy: FreshnessPolicy | None = None,
    reference_time: datetime | None = None,
) -> FactFreshnessDetail:
    """Evaluate 4-dimensional freshness for a stored fact (D46)."""
    fact = session.get(Fact, fact_id)
    if not fact:
        raise ValueError(f"Fact with id {fact_id} does not exist")

    pol = policy or DEFAULT_FRESHNESS_POLICY
    ref_time = _ensure_utc(reference_time or _utcnow())

    # Retrieve snapshot and source
    snap = session.get(PageSnapshot, fact.snapshot_id) if fact.snapshot_id else None
    source_id = snap.source_id if snap else None
    if not source_id:
        raise ValueError(f"Fact {fact_id} is not linked to a valid source snapshot")

    # 1. Observation Freshness
    obs_freshness = evaluate_source_observation_freshness(
        session=session,
        source_id=source_id,
        policy=pol,
        reference_time=ref_time,
    )

    # 2. Content Freshness
    content_freshness = evaluate_source_content_freshness(
        session=session,
        source_id=source_id,
        policy=pol,
        reference_time=ref_time,
    )

    # 3. Evidence Verification Freshness
    ev_freshness = evaluate_evidence_verification_freshness(
        fact=fact,
        snapshot=snap,
        policy=pol,
        reference_time=ref_time,
    )

    # 4. Fact Freshness (explicit fact-specific temporal evidence only)
    info_category = map_field_to_category(fact.field_key)
    threshold = pol.get_fact_threshold(info_category.value, source_id)

    ev_quote = fact.evidence.quote if fact.evidence else None
    parsed = extract_fact_temporal_evidence(ev_quote)

    if parsed is not None and parsed.precision != TemporalPrecision.UNKNOWN and parsed.date_start is not None and parsed.date_end is not None:
        has_explicit = True
        date_start = _ensure_utc(parsed.date_start)
        date_end = _ensure_utc(parsed.date_end)
        min_fact_age = max(0.0, (ref_time - date_end).total_seconds() / 86400.0)
        max_fact_age = max(0.0, (ref_time - date_start).total_seconds() / 86400.0)
        cat_at_end = pol.classify_age(min_fact_age, threshold)
        cat_at_start = pol.classify_age(max_fact_age, threshold)

        if cat_at_end == cat_at_start:
            fact_classification = cat_at_end
            overall_classification = cat_at_end
            if parsed.precision == TemporalPrecision.DAY:
                fact_age_days = round(max_fact_age, 2)
                rationale = (
                    f"Fact '{fact.field_key}' has explicit {parsed.date_type} ('{parsed.matched_phrase}', exact day precision {date_start.date()}) "
                    f"dating to {date_start.date()} ({fact_age_days:.1f} days ago vs {threshold}d policy threshold: "
                    f"{fact_classification.value}). Source observation is {obs_freshness.classification.value}."
                )
            else:
                fact_age_days = round((min_fact_age + max_fact_age) / 2.0, 2)
                rationale = (
                    f"Fact '{fact.field_key}' has explicit {parsed.date_type} ('{parsed.matched_phrase}', {parsed.precision.value} precision, "
                    f"range {date_start.date()} to {date_end.date()}). Age range ({min_fact_age:.1f}d to {max_fact_age:.1f}d) consistently "
                    f"falls within '{fact_classification.value}' threshold ({threshold}d). Source observation is {obs_freshness.classification.value}."
                )
        else:
            # Crosses policy boundary!
            fact_classification = FreshnessCategory.UNKNOWN
            overall_classification = FreshnessCategory.UNKNOWN
            fact_age_days = round((min_fact_age + max_fact_age) / 2.0, 2)
            rationale = (
                f"Fact '{fact.field_key}' has explicit {parsed.date_type} ('{parsed.matched_phrase}', {parsed.precision.value} precision, "
                f"range {date_start.date()} to {date_end.date()}). Age range spans {min_fact_age:.1f}d ({cat_at_end.value}) to "
                f"{max_fact_age:.1f}d ({cat_at_start.value}), crossing policy threshold boundaries ({threshold}d). "
                f"Classified as unknown due to incomplete temporal precision."
            )
    elif parsed is not None and parsed.precision == TemporalPrecision.UNKNOWN:
        has_explicit = True
        date_start = None
        date_end = None
        min_fact_age = None
        max_fact_age = None
        fact_age_days = None
        fact_classification = FreshnessCategory.UNKNOWN
        overall_classification = FreshnessCategory.UNKNOWN
        rationale = (
            f"Fact '{fact.field_key}' contains date expression '{parsed.matched_phrase}' with missing year or day precision. "
            f"Year cannot be inferred from crawl timestamp or database creation dates; classified as unknown."
        )
    else:
        has_explicit = False
        date_start = None
        date_end = None
        min_fact_age = None
        max_fact_age = None
        fact_age_days = None
        fact_classification = FreshnessCategory.UNKNOWN
        overall_classification = FreshnessCategory.UNKNOWN
        rationale = (
            f"Fact '{fact.field_key}' contains no explicit publication, update, or effective date in evidence. "
            f"Fact freshness is unknown (not inferred from crawl date or DB creation timestamp). "
            f"Underlying source observation is {obs_freshness.classification.value} ({obs_freshness.observation_age_days}d ago)."
        )

    # If source is unobservable, overall classification is unobservable
    if obs_freshness.classification == FreshnessCategory.UNOBSERVABLE:
        overall_classification = FreshnessCategory.UNOBSERVABLE

    return FactFreshnessDetail(
        fact_id=fact_id,
        source_id=source_id,
        organisation_id=fact.organisation_id,
        field_key=fact.field_key,
        information_category=info_category,
        raw_value=fact.raw_value,
        observation_freshness=obs_freshness,
        content_freshness=content_freshness,
        evidence_freshness=ev_freshness,
        has_explicit_temporal_evidence=has_explicit,
        explicit_fact_date=date_start if (parsed and parsed.precision == TemporalPrecision.DAY) else (date_start if date_start else None),
        fact_date_type=parsed.date_type if parsed else None,
        temporal_evidence_quote=parsed.matched_phrase if parsed else None,
        date_precision=parsed.precision if parsed else None,
        date_range_start=date_start,
        date_range_end=date_end,
        min_fact_age_days=round(min_fact_age, 2) if min_fact_age is not None else None,
        max_fact_age_days=round(max_fact_age, 2) if max_fact_age is not None else None,
        fact_age_days=fact_age_days,
        fact_freshness_classification=fact_classification,
        overall_classification=overall_classification,
        policy_id=pol.policy_id,
        policy_threshold_days=threshold,
        threshold_governance=f"fact_category:{info_category.value}",
        as_of_reference_time=ref_time,
        rationale=rationale,
    )


def evaluate_organisation_freshness_summary(
    session: Session,
    organisation_id: int,
    policy: FreshnessPolicy | None = None,
    reference_time: datetime | None = None,
) -> OrganisationFreshnessSummary:
    """Evaluate freshness metrics across all confirmed sources and linked facts for an organisation."""
    org = session.get(Organisation, organisation_id)
    if not org:
        raise ValueError(f"Organisation with id {organisation_id} does not exist")

    pol = policy or DEFAULT_FRESHNESS_POLICY
    ref_time = _ensure_utc(reference_time or _utcnow())

    # Query confirmed linked sources
    confirmed_links = list(
        session.scalars(
            select(OrganisationSourceLink)
            .where(
                OrganisationSourceLink.organisation_id == organisation_id,
                OrganisationSourceLink.verification_status == "confirmed",
            )
        ).all()
    )
    source_ids = [link.source_id for link in confirmed_links]

    sources_freshness: list[SourceObservationFreshness] = []
    unobservable_sources_count = 0

    for s_id in source_ids:
        src_fresh = evaluate_source_observation_freshness(
            session=session,
            source_id=s_id,
            policy=pol,
            reference_time=ref_time,
        )
        sources_freshness.append(src_fresh)
        if src_fresh.classification == FreshnessCategory.UNOBSERVABLE:
            unobservable_sources_count += 1

    # Query active facts for this organisation
    facts = list(
        session.scalars(
            select(Fact)
            .where(
                Fact.organisation_id == organisation_id,
                Fact.status != "rejected",
            )
        ).all()
    )

    fresh_count = 0
    aging_count = 0
    stale_count = 0
    unknown_count = 0

    for f in facts:
        try:
            detail = evaluate_fact_freshness(
                session=session,
                fact_id=f.id,
                policy=pol,
                reference_time=ref_time,
            )
            cat = detail.overall_classification
            if cat == FreshnessCategory.FRESH:
                fresh_count += 1
            elif cat == FreshnessCategory.AGING:
                aging_count += 1
            elif cat == FreshnessCategory.STALE:
                stale_count += 1
            else:
                unknown_count += 1
        except Exception as exc:
            logger.warning("fact_freshness_evaluation_error fact_id=%d error=%s", f.id, exc)
            unknown_count += 1

    summary_rationale = (
        f"Organisation '{org.name}' has {len(source_ids)} confirmed source(s) and {len(facts)} active fact(s). "
        f"Fact freshness: {fresh_count} fresh, {aging_count} aging, {stale_count} stale, {unknown_count} unknown. "
        f"Unobservable sources: {unobservable_sources_count}."
    )

    return OrganisationFreshnessSummary(
        organisation_id=organisation_id,
        organisation_name=org.name,
        confirmed_source_count=len(source_ids),
        total_facts_count=len(facts),
        fresh_facts_count=fresh_count,
        aging_facts_count=aging_count,
        stale_facts_count=stale_count,
        unknown_facts_count=unknown_count,
        unobservable_sources_count=unobservable_sources_count,
        sources=sources_freshness,
        as_of_reference_time=ref_time,
        policy_id=pol.policy_id,
        summary_rationale=summary_rationale,
    )
