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


def find_all_quote_offsets(snapshot_text: str, quote: str) -> list[tuple[int, int, str]]:
    """Find all exact or whitespace-tolerant occurrences of quote in snapshot_text.

    Returns a list of (start_offset, end_offset, matched_substring).
    """
    clean_quote = quote.strip()
    if not clean_quote:
        return []

    # 1. Exact match attempts
    occurrences: list[tuple[int, int, str]] = []
    idx = 0
    while True:
        pos = snapshot_text.find(clean_quote, idx)
        if pos == -1:
            break
        occurrences.append((pos, pos + len(clean_quote), snapshot_text[pos : pos + len(clean_quote)]))
        idx = pos + 1

    if occurrences:
        return occurrences

    # 2. Case-insensitive exact match
    lower_snap = snapshot_text.lower()
    lower_quote = clean_quote.lower()
    idx = 0
    while True:
        pos = lower_snap.find(lower_quote, idx)
        if pos == -1:
            break
        occurrences.append((pos, pos + len(clean_quote), snapshot_text[pos : pos + len(clean_quote)]))
        idx = pos + 1

    if occurrences:
        return occurrences

    # 3. Whitespace-normalized regex search
    norm_quote = _normalize_whitespace(clean_quote)
    tokens = [token for token in norm_quote.split(" ") if token]
    if not tokens:
        return []

    pattern_tokens = []
    for token in tokens:
        escaped = re.escape(token)
        t = re.sub(r"(\\?[-—–−])+", r"[-—–−]+", escaped)
        pattern_tokens.append(t)

    pattern_str = r"\s+".join(pattern_tokens)
    try:
        for match in re.finditer(pattern_str, snapshot_text, re.IGNORECASE):
            occurrences.append((match.start(), match.end(), snapshot_text[match.start() : match.end()]))
    except re.error:
        pass

    return occurrences


def resolve_quote_offsets(
    snapshot_text: str,
    quote: str,
    headings: list[dict[str, Any]] | None = None,
    heading_hint: str | None = None,
    scope_hint: str | None = None,
) -> tuple[int, int, str, bool] | None:
    """Resolve quote offsets taking into account repeated occurrences and heading scope.

    Returns (start_offset, end_offset, matched_substring, is_ambiguous) or None.
    """
    occurrences = find_all_quote_offsets(snapshot_text, quote)
    if not occurrences:
        return None

    if len(occurrences) == 1:
        return occurrences[0][0], occurrences[0][1], occurrences[0][2], False

    # Multiple occurrences: check if they belong to different headings
    if not headings:
        # Without headings, repeated quote across page cannot be disambiguated
        return occurrences[0][0], occurrences[0][1], occurrences[0][2], True

    def _nearest_heading(offset: int) -> dict[str, Any] | None:
        preceding = [h for h in headings if h.get("start_offset", 0) <= offset]
        if not preceding:
            return None
        return sorted(preceding, key=lambda h: h.get("start_offset", 0), reverse=True)[0]

    # Map each occurrence to its enclosing heading
    occ_with_headings: list[tuple[tuple[int, int, str], dict[str, Any] | None]] = []
    distinct_headings: set[str] = set()

    for occ in occurrences:
        h = _nearest_heading(occ[0])
        h_text = h.get("text", "").strip() if h else ""
        distinct_headings.add(h_text.lower())
        occ_with_headings.append((occ, h))

    # If all occurrences belong to the exact same heading, they share the same scope
    if len(distinct_headings) <= 1:
        return occurrences[0][0], occurrences[0][1], occurrences[0][2], False

    # They fall under DIFFERENT headings (different operational scopes).
    # Try to match heading_hint or scope_hint
    signals = []
    if heading_hint:
        signals.append(heading_hint.lower())
    if scope_hint:
        signals.append(scope_hint.lower())

    matched_candidates: list[tuple[int, int, str]] = []
    if signals:
        for occ, h in occ_with_headings:
            h_text = (h.get("text", "") if h else "").lower()
            if any(s in h_text or h_text in s for s in signals):
                matched_candidates.append(occ)

    if len(matched_candidates) == 1:
        # Uniquely resolved by heading/scope context!
        chosen = matched_candidates[0]
        return chosen[0], chosen[1], chosen[2], False

    # Ambiguous across multiple scopes: do NOT silently select first occurrence
    first = occurrences[0]
    return first[0], first[1], first[2], True


def find_quote_offsets(snapshot_text: str, quote: str) -> tuple[int, int, str] | None:
    """Find exact or whitespace-tolerant quote in snapshot text.

    Returns (start_offset, end_offset, matched_substring) or None.
    Preserved for backwards-compatibility.
    """
    res = resolve_quote_offsets(snapshot_text, quote)
    if res is None:
        return None
    return res[0], res[1], res[2]


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
        if pat.search(clean_val) or pat.search(clean_quote) or pat.search(combined_context):
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
    headings: list[dict[str, Any]] | None = None,
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

    # Gate 3: Exact or tolerant quote matching with repeated quote disambiguation
    scope_hint = str(candidate.get("scope_proposal") or "")
    heading_hint = str(candidate.get("heading_context") or surrounding_context or "")
    match_res = resolve_quote_offsets(
        snapshot_text=snapshot_text,
        quote=evidence_quote,
        headings=headings,
        heading_hint=heading_hint,
        scope_hint=scope_hint,
    )
    if match_res is None:
        return VerificationResult(
            verified=False,
            status="rejected",
            start_offset=None,
            end_offset=None,
            matched_quote=None,
            rejection_reason="evidence_quote not found in snapshot text",
        )

    start_offset, end_offset, matched_str, is_ambiguous = match_res

    # If identical quote occurs under multiple headings and context is ambiguous,
    # route to needs_review rather than silently selecting the first occurrence (Requirement 6).
    if is_ambiguous:
        return VerificationResult(
            verified=True,
            status="needs_review",
            start_offset=start_offset,
            end_offset=end_offset,
            matched_quote=matched_str,
            rejection_reason="ambiguous_repeated_quote_across_multiple_scopes",
        )

    # Extract surrounding context window bounded by section/paragraph to avoid cross-section contamination
    window_start = max(0, start_offset - 200)
    if headings:
        preceding = [h for h in headings if h.get("start_offset", 0) <= start_offset]
        if preceding:
            nearest_h_start = max(h.get("start_offset", 0) for h in preceding)
            window_start = max(window_start, nearest_h_start)
    else:
        p_boundary = snapshot_text.rfind("\n\n", 0, start_offset)
        if p_boundary != -1 and p_boundary >= window_start:
            window_start = p_boundary + 2

    window_end = min(len(snapshot_text), end_offset + 150)
    if headings:
        subsequent = [h for h in headings if h.get("start_offset", 0) > end_offset]
        if subsequent:
            next_h_start = min(h.get("start_offset", 0) for h in subsequent)
            window_end = min(window_end, next_h_start)
    else:
        next_p = snapshot_text.find("\n\n", end_offset)
        if next_p != -1 and next_p <= window_end:
            window_end = next_p

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
