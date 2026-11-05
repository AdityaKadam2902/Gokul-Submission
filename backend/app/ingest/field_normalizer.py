"""Deterministic field normalization (ARCHITECTURE §6.1; DECISION_LOG D7).

Responsibility:
    Normalizes structured field values (phone, email, URL, opening hours)
    in deterministic Python code.

Guarantees:
    - DATA PRESERVATION: The raw published value is ALWAYS stored untouched.
    - NO INVENTED VALUES: If normalization is uncertain, ambiguous, or fails,
      normalized_value is set to None and needs_review is returned.
    - NO MODEL FALLBACK: Normalization never queries an LLM to guess formats.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class NormalizationOutcome:
    raw_value: str
    normalized_value: str | None
    needs_review: bool
    review_reason: str | None


def normalize_phone(raw_value: str) -> NormalizationOutcome:
    """Normalize Australian telephone numbers to E.164 format."""
    clean = raw_value.strip()
    digits = re.sub(r"\D", "", clean)

    # Standard Australian formats:
    # 10 digits starting with 0: e.g. 07 5550 0100 -> +61755500100
    if len(digits) == 10 and digits.startswith("0"):
        return NormalizationOutcome(
            raw_value=clean,
            normalized_value=f"+61{digits[1:]}",
            needs_review=False,
            review_reason=None,
        )
    # 11 digits starting with 61: e.g. 61755500100 -> +61755500100
    if len(digits) == 11 and digits.startswith("61"):
        return NormalizationOutcome(
            raw_value=clean,
            normalized_value=f"+{digits}",
            needs_review=False,
            review_reason=None,
        )
    # 8 digits local (ambiguous area code)
    if len(digits) == 8:
        return NormalizationOutcome(
            raw_value=clean,
            normalized_value=None,
            needs_review=True,
            review_reason="phone_missing_area_code",
        )

    return NormalizationOutcome(
        raw_value=clean,
        normalized_value=None,
        needs_review=True,
        review_reason="phone_format_unrecognized",
    )


def normalize_email(raw_value: str) -> NormalizationOutcome:
    """Normalize business email address."""
    clean = raw_value.strip().lower()
    email_regex = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
    if email_regex.match(clean):
        return NormalizationOutcome(
            raw_value=raw_value.strip(),
            normalized_value=clean,
            needs_review=False,
            review_reason=None,
        )
    return NormalizationOutcome(
        raw_value=raw_value.strip(),
        normalized_value=None,
        needs_review=True,
        review_reason="email_syntax_invalid",
    )


def normalize_url(raw_value: str) -> NormalizationOutcome:
    """Normalize web URL."""
    clean = raw_value.strip()
    if not clean.startswith(("http://", "https://")):
        clean = f"https://{clean}"
    try:
        parsed = urlparse(clean)
        if parsed.netloc and "." in parsed.netloc:
            # Canonical lowercase host, strip trailing slash on domain
            canonical = f"{parsed.scheme}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"
            if parsed.query:
                canonical = f"{canonical}?{parsed.query}"
            return NormalizationOutcome(
                raw_value=raw_value.strip(),
                normalized_value=canonical,
                needs_review=False,
                review_reason=None,
            )
    except Exception:
        pass

    return NormalizationOutcome(
        raw_value=raw_value.strip(),
        normalized_value=None,
        needs_review=True,
        review_reason="url_format_invalid",
    )


def normalize_hours(raw_value: str) -> NormalizationOutcome:
    """Normalize opening hours text. Preserves raw string if irregular."""
    clean = raw_value.strip()
    # Simple standard format: Day – Day Time am – Time pm
    # e.g., "Monday – Friday 8:00 am – 6:00 pm"
    pattern = re.compile(
        r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*[-—–]\s*"
        r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*[:]?\s*"
        r"(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s*[-—–]\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm))$",
        re.IGNORECASE,
    )
    match = pattern.match(clean)
    if match:
        start_day, end_day, start_t, end_t = match.groups()
        normalized = f"{start_day.capitalize()}-{end_day.capitalize()} {start_t.lower()}-{end_t.lower()}"
        return NormalizationOutcome(
            raw_value=clean,
            normalized_value=normalized,
            needs_review=False,
            review_reason=None,
        )

    # If it is single day: "Saturday 8:30 am – 1:00 pm"
    single_day_pattern = re.compile(
        r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*[:]?\s*"
        r"(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s*[-—–]\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm))$",
        re.IGNORECASE,
    )
    m2 = single_day_pattern.match(clean)
    if m2:
        day, start_t, end_t = m2.groups()
        normalized = f"{day.capitalize()} {start_t.lower()}-{end_t.lower()}"
        return NormalizationOutcome(
            raw_value=clean,
            normalized_value=normalized,
            needs_review=False,
            review_reason=None,
        )

    # Complex, multi-period, or descriptive hours -> keep raw, flag review
    return NormalizationOutcome(
        raw_value=clean,
        normalized_value=None,
        needs_review=True,
        review_reason="complex_or_non_standard_hours",
    )


def normalize_field(field_key: str, raw_value: str) -> NormalizationOutcome:
    """Route normalization based on field type. Always preserves raw_value."""
    if not raw_value:
        return NormalizationOutcome(
            raw_value="",
            normalized_value=None,
            needs_review=True,
            review_reason="empty_value",
        )

    clean = raw_value.strip()

    if "phone" in field_key:
        return normalize_phone(clean)
    if "email" in field_key:
        return normalize_email(clean)
    if "website" in field_key or "url" in field_key:
        return normalize_url(clean)
    if "hours" in field_key:
        return normalize_hours(clean)

    # For text/name fields: whitespace-trimmed, raw preserved as normalized
    return NormalizationOutcome(
        raw_value=clean,
        normalized_value=clean,
        needs_review=False,
        review_reason=None,
    )
