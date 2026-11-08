"""Conservative healthcare organisation entity resolver (ARCHITECTURE §6.1, D12, D39).

Responsibility:
    Evaluates potential matches between registered sources and practice organisations
    using a deterministic, conservative 3-tier matching engine.

Guarantees:
    - DISCRETE TIERS: Emits strong_match_suggestion, possible_match, or insufficient_evidence.
    - ZERO PROBABILITY CLAIMS: Raw string similarity scores are strictly separated from probability.
    - ZERO AUTO-MERGE: Produces reviewable match candidates only; merges require human approval.
    - BRANCH ISOLATION: Same corporate brand in differing postcodes evaluates to insufficient_evidence.
    - AGGREGATOR AWARE: Root domain match on approved aggregators without branch slug is rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.logging import get_logger
from backend.app.db.orm import (
    Organisation as OrganisationORM,
    OrganisationMatchCandidate as OrganisationMatchCandidateORM,
    PageSnapshot,
    Source,
)
from backend.app.ingest.field_normalizer import normalize_phone
from backend.app.intelligence.schemas import MatchSignals, MatchTier, ReviewStatus

logger = get_logger(__name__)

# Documented configurable allowlist of healthcare corporate aggregators & directory networks
APPROVED_AGGREGATOR_DOMAINS: set[str] = {
    "directory.example.org",
    "healius.com.au",
    "forhealth.com.au",
    "metrohealth.example.org",
    "healthhub.example.org",
}

SHARED_NATIONAL_PHONE_PREFIXES = ("1300", "1800", "13", "+611300", "+611800", "+6113")
GENERIC_PRACTICE_NAMES = {
    "family medical centre",
    "family healthcare clinic",
    "medical centre",
    "medical practice",
    "general practice",
    "health clinic",
    "doctors clinic",
}


@dataclass(frozen=True)
class MatchEvaluation:
    tier: MatchTier
    raw_metric_score: float | None
    signals: MatchSignals
    rationale: str


def is_corporate_phone(phone_digits_or_str: str | None) -> bool:
    """Check if telephone number uses a national shared 1300 / 1800 prefix."""
    if not phone_digits_or_str:
        return False
    digits = re.sub(r"\D", "", phone_digits_or_str)
    return any(digits.startswith(prefix.replace("+", "").replace(" ", "")) for prefix in ("1300", "1800", "13", "611300", "611800", "6113"))


def evaluate_source_and_org(
    source_url: str | None,
    source_domain: str | None,
    source_label: str | None,
    source_phone: str | None,
    source_address: str | None,
    source_postcode: str | None,
    source_suburb: str | None,
    source_state: str | None,
    org: OrganisationORM,
) -> MatchEvaluation:
    """Evaluate pairwise matching signals and determine qualitative match tier."""
    parsed_url = urlparse(source_url or "")
    url_path = (parsed_url.path or "").lower()
    domain = (source_domain or parsed_url.netloc or "").lower().removeprefix("www.")
    org_domain = (org.primary_domain or "").lower().removeprefix("www.")

    # 1. Evaluate Aggregator & Domain matching
    is_aggregator = domain in APPROVED_AGGREGATOR_DOMAINS or any(agg in domain for agg in APPROVED_AGGREGATOR_DOMAINS)
    domain_match = bool(domain and org_domain and (domain == org_domain))
    branch_slug_match = bool(url_path and org.slug in url_path or (org.suburb and org.suburb.lower() in url_path))

    # 2. Evaluate Geographic Locality
    postcode_match = bool(source_postcode and org.postcode and source_postcode.strip() == org.postcode.strip())
    suburb_match = bool(source_suburb and org.suburb and source_suburb.strip().lower() == org.suburb.strip().lower())
    state_match = bool(source_state and org.state and source_state.strip().upper() == org.state.strip().upper())

    # 3. Evaluate Phone matching
    corporate_phone_detected = is_corporate_phone(source_phone)
    local_phone_match = False
    if source_phone and org.phone and not corporate_phone_detected:
        norm_src = normalize_phone(source_phone).normalized_value
        norm_org = normalize_phone(org.phone).normalized_value
        if norm_src and norm_org and norm_src == norm_org:
            local_phone_match = True

    # 4. Evaluate Address & Suite distinction
    street_address_match = False
    distinct_suite_detected = False
    if source_address and org.address:
        clean_src_addr = source_address.lower()
        clean_org_addr = org.address.lower()
        # Suite distinction check
        src_suite = re.search(r"suite\s*(\d+)", clean_src_addr)
        org_suite = re.search(r"suite\s*(\d+)", clean_org_addr)
        if src_suite and org_suite and src_suite.group(1) != org_suite.group(1):
            distinct_suite_detected = True

        src_num = re.search(r"(\d+)\s+([a-z]+)", clean_src_addr)
        org_num = re.search(r"(\d+)\s+([a-z]+)", clean_org_addr)
        if src_num and org_num and src_num.group(0) == org_num.group(0):
            street_address_match = not distinct_suite_detected

    # 5. Evaluate String Similarity on Practice Name
    clean_label = (source_label or "").strip()
    clean_org_name = org.name.strip()
    name_similarity = fuzz.token_sort_ratio(clean_label, clean_org_name) if clean_label else 0.0

    # Generic clinic name defense
    is_generic_name = clean_label.lower() in GENERIC_PRACTICE_NAMES or clean_org_name.lower() in GENERIC_PRACTICE_NAMES

    signals = MatchSignals(
        domain_match=domain_match and not is_aggregator,
        postcode_match=postcode_match,
        local_phone_match=local_phone_match,
        corporate_phone_detected=corporate_phone_detected,
        branch_slug_match=branch_slug_match,
        street_address_match=street_address_match,
        name_similarity_metric=float(name_similarity),
    )

    # -------------------------------------------------------------------------
    # RULE GATES — DETERMINISTIC TIER ASSIGNMENT
    # -------------------------------------------------------------------------

    # Gate 1: Insufficient Evidence negative cases
    if distinct_suite_detected:
        return MatchEvaluation(
            tier=MatchTier.INSUFFICIENT_EVIDENCE,
            raw_metric_score=float(name_similarity),
            signals=signals,
            rationale="Distinct practice suites sharing a building/postcode must remain separate organisations.",
        )

    if source_postcode and org.postcode and not postcode_match:
        return MatchEvaluation(
            tier=MatchTier.INSUFFICIENT_EVIDENCE,
            raw_metric_score=float(name_similarity),
            signals=signals,
            rationale=f"Lookalike branch network in differing postcodes ({source_postcode} vs {org.postcode}) mandates strict branch isolation.",
        )

    if is_generic_name and not (local_phone_match or (domain_match and not is_aggregator)):
        return MatchEvaluation(
            tier=MatchTier.INSUFFICIENT_EVIDENCE,
            raw_metric_score=float(name_similarity),
            signals=signals,
            rationale="Generic practice name lacking verifiable local telephone or official domain match.",
        )

    if corporate_phone_detected and not street_address_match:
        return MatchEvaluation(
            tier=MatchTier.INSUFFICIENT_EVIDENCE,
            raw_metric_score=float(name_similarity),
            signals=signals,
            rationale="Shared corporate 1300/1800 contact number without branch-level street address match.",
        )

    # Gate 2: Strong Match Suggestion
    # Rule 2A: Exact local practice phone + exact postcode
    if local_phone_match and postcode_match:
        return MatchEvaluation(
            tier=MatchTier.STRONG_MATCH_SUGGESTION,
            raw_metric_score=float(name_similarity),
            signals=signals,
            rationale="Exact local practice telephone (E.164) and identical 4-digit postcode.",
        )

    # Rule 2B: Exact non-aggregator official domain + postcode + suburb/state
    if domain_match and not is_aggregator and postcode_match and (suburb_match or state_match or street_address_match):
        return MatchEvaluation(
            tier=MatchTier.STRONG_MATCH_SUGGESTION,
            raw_metric_score=float(name_similarity),
            signals=signals,
            rationale="Exact official practice domain with matching postcode and locality evidence.",
        )

    # Gate 3: Possible Match
    # Rule 3A: RapidFuzz score >= 88.0 + exact postcode
    if name_similarity >= 88.0 and postcode_match:
        return MatchEvaluation(
            tier=MatchTier.POSSIBLE_MATCH,
            raw_metric_score=float(name_similarity),
            signals=signals,
            rationale=f"High name similarity ({name_similarity:.1f}/100) and matching postcode ({org.postcode}).",
        )

    # Rule 3B: Approved aggregator domain + branch slug + street address match
    if is_aggregator and branch_slug_match and street_address_match:
        return MatchEvaluation(
            tier=MatchTier.POSSIBLE_MATCH,
            raw_metric_score=float(name_similarity),
            signals=signals,
            rationale="Approved corporate aggregator with matching branch slug and street address evidence.",
        )

    # Gate 4: Default Fallback -> Insufficient Evidence
    return MatchEvaluation(
        tier=MatchTier.INSUFFICIENT_EVIDENCE,
        raw_metric_score=float(name_similarity),
        signals=signals,
        rationale="Insufficient branch-level identity proof to establish an organisation match suggestion.",
    )


def extract_source_signals(session: Session, source: Source) -> dict[str, Any]:
    """Extract known metadata and candidate fact values from source and its latest snapshot."""
    data: dict[str, Any] = {
        "url": source.url,
        "domain": source.domain,
        "label": source.label,
        "phone": None,
        "address": None,
        "postcode": None,
        "suburb": None,
        "state": None,
    }

    # Inspect latest snapshot text for address, postcode, phone indicators
    latest_snap = source.snapshots[0] if source.snapshots else None
    if latest_snap:
        text = latest_snap.text
        # Search phone
        phone_match = re.search(r"(\(0\d\)\s*\d{4}\s*\d{4}|\b0\d{9}\b|\b1[38]00\s*\d{3}\s*\d{3}\b)", text)
        if phone_match:
            data["phone"] = phone_match.group(0)

        # Search postcode / state (e.g. Melbourne VIC 3000, Box Hill VIC 3128, Surfers Paradise QLD 4217)
        loc_match = re.search(r"\b([A-Za-z\s]+)\s+(VIC|QLD|NSW|WA|SA|TAS|ACT|NT)\s+(\d{4})\b", text)
        if loc_match:
            data["suburb"] = loc_match.group(1).strip()
            data["state"] = loc_match.group(2).strip()
            data["postcode"] = loc_match.group(3).strip()

        # Search address (e.g. 120 Collins St, 450 Station St, 12 Nepean Hwy)
        addr_match = re.search(r"\b(\d+[\w\s]*(?:Street|St|Road|Rd|Avenue|Ave|Highway|Hwy|Lane|Ln|Boulevard|Blvd|Suite\s*\d+))\b", text, re.IGNORECASE)
        if addr_match:
            data["address"] = addr_match.group(0).strip()

    return data


def propose_match_candidate(
    session: Session,
    source_id: int,
    organisation_id: int,
    *,
    override_signals: dict[str, Any] | None = None,
) -> OrganisationMatchCandidateORM | None:
    """Evaluate pairwise match and insert idempotent match candidate if strong or possible."""
    source = session.get(Source, source_id)
    org = session.get(OrganisationORM, organisation_id)
    if not source or not org:
        raise ValueError(f"Source {source_id} or Organisation {organisation_id} not found")

    # Check if candidate already exists (idempotency)
    existing = session.scalar(
        select(OrganisationMatchCandidateORM)
        .where(
            OrganisationMatchCandidateORM.source_id == source_id,
            OrganisationMatchCandidateORM.suggested_organisation_id == organisation_id,
        )
    )
    if existing:
        return existing

    signals_dict = override_signals or extract_source_signals(session, source)
    evaluation = evaluate_source_and_org(
        source_url=signals_dict.get("url") or source.url,
        source_domain=signals_dict.get("domain") or source.domain,
        source_label=signals_dict.get("label") or source.label,
        source_phone=signals_dict.get("phone"),
        source_address=signals_dict.get("address"),
        source_postcode=signals_dict.get("postcode"),
        source_suburb=signals_dict.get("suburb"),
        source_state=signals_dict.get("state"),
        org=org,
    )

    if evaluation.tier == MatchTier.INSUFFICIENT_EVIDENCE:
        logger.info(
            "match_candidate_rejected source_id=%d org_id=%d rationale=%s",
            source_id, organisation_id, evaluation.rationale,
        )
        return None

    status_str = ReviewStatus.SUGGESTED if evaluation.tier == MatchTier.STRONG_MATCH_SUGGESTION else ReviewStatus.NEEDS_REVIEW

    candidate = OrganisationMatchCandidateORM(
        source_id=source_id,
        suggested_organisation_id=organisation_id,
        match_tier=evaluation.tier.value,
        raw_metric_score=evaluation.raw_metric_score,
        evidence_signals=evaluation.signals.model_dump(),
        rationale=evaluation.rationale,
        status=status_str.value,
    )
    session.add(candidate)
    session.flush()
    logger.info(
        "match_candidate_created source_id=%d org_id=%d tier=%s status=%s score=%s",
        source_id, organisation_id, evaluation.tier.value, status_str.value, evaluation.raw_metric_score,
    )
    return candidate
