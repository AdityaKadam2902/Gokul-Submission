"""Deterministic evidence verification (ARCHITECTURE §6.1, §8; DECISION_LOG D10).

Responsibility:
    Validates that candidate facts proposed by the AI model are strictly grounded
    in the immutable snapshot text.
    - Confirms that the field is permitted by the source allowlist.
    - Matches the evidence quote against the snapshot text (exact and whitespace-normalized).
    - Computes authoritative character offsets in Python (never trusts model offsets).
    - Enforces field-specific value-to-evidence validation (Requirement 4):
      * raw_value must be supported by the quotation;
      * business-contact rule rejects testimonial-derived contact info (D6/D10);
      * official_website rejects external booking links (F3);
      * prompt-injection text is rejected structurally.

Inputs:
    Candidate field proposal, snapshot text, heading chain, intended_fields allowlist.
Outputs:
    VerificationResult: verified boolean, start_offset, end_offset, matched_quote,
    status ('candidate' | 'needs_review' | 'rejected'), rejection_reason.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from backend.app.core.logging import get_logger

logger = get_logger(__name__)

MATCH_ALGORITHM_VERSION = "py_subslice_v1"

# Patterns indicating testimonial or review context
TESTIMONIAL_PATTERNS = [
    re.compile(r"\btestimonials?\b", re.IGNORECASE),
    re.compile(r"\bpatient reviews?\b", re.IGNORECASE),
    re.compile(r"\b(note|words?) from (our )?patients?\b", re.IGNORECASE),
    re.compile(r"\bwhat our patients (say|think|note)\b", re.IGNORECASE),
    re.compile(r"\blooked after my\b", re.IGNORECASE),
    re.compile(r"\bcall (them|me) on\b", re.IGNORECASE),
]

# Patterns indicating injection payloads
INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"you\s+must\s+output", re.IGNORECASE),
    re.compile(r"override\s+system\s+prompt", re.IGNORECASE),
    re.compile(r"new\s+instruction:", re.IGNORECASE),
]

# Booking portal indicators that must not be mistaken for official website
EXTERNAL_BOOKING_DOMAINS = [
    "example-booking.com",
    "setmore.com",
    "hotdoc.com.au",
    "healthengine.com.au",
]


@dataclass(frozen=True)
class VerificationResult:
    verified: bool
    status: str  # 'candidate', 'needs_review', 'rejected'
    start_offset: int | None
    end_offset: int | None
    matched_quote: str | None
    rejection_reason: str | None
    match_algorithm: str = MATCH_ALGORITHM_VERSION


def _normalize_whitespace(text: str) -> str:
    """Normalize unicode spaces, smart quotes, hyphens and collapse whitespace."""
    # Normalize unicode forms
    text = unicodedata.normalize("NFKC", text)
    # Replace smart quotes with standard
    text = text.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    # Replace unicode dashes with ASCII hyphen
    text = text.replace("—", "-").replace("–", "-").replace("−", "-")
    # Collapse multiple whitespaces
    return re.sub(r"\s+", " ", text).strip()


def find_quote_offsets(snapshot_text: str, quote: str) -> tuple[int, int, str] | None:
    """Find exact or whitespace-tolerant quote in snapshot text.

    Returns (start_offset, end_offset, matched_substring) or None.
    """
    clean_quote = quote.strip()
    if not clean_quote:
        return None

    # 1. Exact match attempt
    idx = snapshot_text.find(clean_quote)
    if idx != -1:
        return idx, idx + len(clean_quote), snapshot_text[idx : idx + len(clean_quote)]

    # 2. Case-insensitive exact match
    lower_snap = snapshot_text.lower()
    lower_quote = clean_quote.lower()
    idx = lower_snap.find(lower_quote)
    if idx != -1:
        return idx, idx + len(clean_quote), snapshot_text[idx : idx + len(clean_quote)]

    # 3. Whitespace-normalized regex search
    norm_quote = _normalize_whitespace(clean_quote)
    tokens = [token for token in norm_quote.split(" ") if token]
    if not tokens:
        return None

    # Replace dashes in tokens with regex that matches any dash or sequence of dashes
    pattern_tokens = []
    for token in tokens:
        escaped = re.escape(token)
        t = re.sub(r"(\\?[-—–−])+", r"[-—–−]+", escaped)
        pattern_tokens.append(t)

    pattern_str = r"\s+".join(pattern_tokens)
    try:
        match = re.search(pattern_str, snapshot_text, re.IGNORECASE)
        if match:
            return match.start(), match.end(), snapshot_text[match.start() : match.end()]
    except re.error:
        pass

    return None


def validate_value_against_evidence(
    field_key: str,
    raw_value: str,
    evidence_quote: str,
    surrounding_context: str = "",
) -> tuple[bool, str | None]:
    """Validate that raw_value is genuinely substantiated by evidence_quote.

    Returns (is_valid, failure_reason).
    """
    clean_val = raw_value.strip()
    clean_quote = evidence_quote.strip()
    combined_context = f"{clean_quote} {surrounding_context}".lower()

    # Rule 1: Prompt Injection Guard
    for pat in INJECTION_PATTERNS:
        if pat.search(clean_val) or pat.search(clean_quote):
            return False, "prompt_injection_payload_detected"

    # Rule 2: Business-Contact Rule (ARCHITECTURE §4, DECISION_LOG D6/D10)
    # Reject contact details originating in testimonial / review blocks
    is_contact_field = field_key in ("public_phone", "public_email", "contact", "vacancy_contact_channel")
    if is_contact_field:
        for pat in TESTIMONIAL_PATTERNS:
            if pat.search(combined_context):
                return False, "testimonial_contact_rejected_by_business_rule"

    # Rule 3: Official website cannot be an external booking portal (F3 edge case)
    if field_key == "official_website":
        for booking_domain in EXTERNAL_BOOKING_DOMAINS:
            if booking_domain in clean_val.lower() or booking_domain in clean_quote.lower():
                return False, "external_booking_link_rejected_as_official_website"

    # Rule 4: Substantial containment or semantic alignment
    # The raw_value or its primary core components must be in the quote
    norm_val = _normalize_whitespace(clean_val).lower()
    norm_quote = _normalize_whitespace(clean_quote).lower()

    if norm_val in norm_quote:
        return True, None

    # Check if numbers or key words in raw_value appear in quote
    val_digits = re.sub(r"\D", "", clean_val)
    if val_digits and len(val_digits) >= 6:
        quote_digits = re.sub(r"\D", "", clean_quote)
        if val_digits in quote_digits:
            return True, None
        return False, "raw_value_digits_not_in_evidence_quote"

    # Check word overlap for multi-word strings
    val_words = set(re.findall(r"\w+", norm_val))
    quote_words = set(re.findall(r"\w+", norm_quote))
    if val_words and val_words.issubset(quote_words):
        return True, None

    # If substantial overlap (e.g. > 70% of words in quote)
    overlap = len(val_words.intersection(quote_words))
    if val_words and (overlap / len(val_words)) >= 0.7:
        return True, None

    return False, "raw_value_not_substantiated_by_evidence_quote"


def verify_candidate_evidence(
    candidate: dict[str, Any],
    snapshot_text: str,
    intended_fields: list[str],
    surrounding_context: str = "",
) -> VerificationResult:
    """Verify an individual candidate proposed by the LLM."""
    field_key = candidate.get("field_key", "")
    raw_value = str(candidate.get("raw_value", "")).strip()
    evidence_quote = str(candidate.get("evidence_quote", "")).strip()

    # Gate 1: Source field allowlist (D6)
    if field_key not in intended_fields:
        return VerificationResult(
            verified=False,
            status="rejected",
            start_offset=None,
            end_offset=None,
            matched_quote=None,
            rejection_reason=f"field '{field_key}' is not in source intended_fields allowlist",
        )

    # Gate 2: Value and quote presence
    if not raw_value:
        return VerificationResult(
            verified=False,
            status="rejected",
            start_offset=None,
            end_offset=None,
            matched_quote=None,
            rejection_reason="candidate raw_value is empty",
        )
    if not evidence_quote:
        return VerificationResult(
            verified=False,
            status="rejected",
            start_offset=None,
            end_offset=None,
            matched_quote=None,
            rejection_reason="candidate evidence_quote is missing",
        )

    # Gate 3: Exact or tolerant quote matching in snapshot text
    match_offsets = find_quote_offsets(snapshot_text, evidence_quote)
    if match_offsets is None:
        return VerificationResult(
            verified=False,
            status="rejected",
            start_offset=None,
            end_offset=None,
            matched_quote=None,
            rejection_reason="evidence_quote not found in snapshot text",
        )

    start_offset, end_offset, matched_str = match_offsets

    # Extract surrounding context window (up to 300 chars before and 150 after) to capture enclosing headings/notes
    window_start = max(0, start_offset - 300)
    window_end = min(len(snapshot_text), end_offset + 150)
    context_window = snapshot_text[window_start:window_end]
    full_context = f"{context_window} {surrounding_context}"

    # Gate 4: Value-to-evidence validation (Requirement 4)
    val_ok, fail_reason = validate_value_against_evidence(
        field_key, raw_value, matched_str, full_context
    )
    if not val_ok:
        return VerificationResult(
            verified=False,
            status="rejected",
            start_offset=start_offset,
            end_offset=end_offset,
            matched_quote=matched_str,
            rejection_reason=fail_reason,
        )

    # Passed all deterministic verification gates -> valid candidate fact
    return VerificationResult(
        verified=True,
        status="candidate",
        start_offset=start_offset,
        end_offset=end_offset,
        matched_quote=matched_str,
        rejection_reason=None,
    )
