"""Deterministic heading-based scope assignment (ARCHITECTURE §6.1; DECISION_LOG D11).

Responsibility:
    Derives the operational scope key (e.g. 'clinic', 'pathology', 'reception')
    for an extracted fact using the document's heading hierarchy captured at parse time.

Rules (DECISION_LOG D11):
    1. Deterministic heading chain is authoritative.
    2. Multiple conflicting candidate scopes in one context -> needs_review (never guessed).
    3. Model proposal is an explicit FALLBACK only when heading context is completely absent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ScopeAssignmentResult:
    scope_key: str | None
    is_ambiguous: bool
    assignment_basis: str  # 'heading_chain', 'model_fallback', 'default_general', 'ambiguous'
    review_reason: str | None


SCOPE_MAPPINGS = [
    (re.compile(r"\bpathology\b", re.IGNORECASE), "pathology"),
    (re.compile(r"\breception\b", re.IGNORECASE), "reception"),
    (re.compile(r"\bclinic(al)?\b|\bconsultations?\b", re.IGNORECASE), "clinic"),
    (re.compile(r"\btelehealth\b", re.IGNORECASE), "telehealth"),
    (re.compile(r"\bgeneral practice\b", re.IGNORECASE), "general_practice"),
    (re.compile(r"\bvaccin(ation|e)\b|\bimmunis\b", re.IGNORECASE), "immunisation"),
]


def assign_scope_from_headings(
    start_offset: int,
    end_offset: int,
    headings: list[dict[str, Any]],
    model_scope_proposal: str | None = None,
) -> ScopeAssignmentResult:
    """Assign scope key based on enclosing heading hierarchy."""
    if not headings:
        if model_scope_proposal:
            clean_proposal = model_scope_proposal.strip().lower()
            return ScopeAssignmentResult(
                scope_key=clean_proposal,
                is_ambiguous=False,
                assignment_basis="model_fallback",
                review_reason=None,
            )
        return ScopeAssignmentResult(
            scope_key="general",
            is_ambiguous=False,
            assignment_basis="default_general",
            review_reason=None,
        )

    # Find the nearest preceding heading before start_offset
    preceding = [h for h in headings if h.get("start_offset", 0) <= start_offset]
    if not preceding:
        # Before the first heading
        return ScopeAssignmentResult(
            scope_key="general",
            is_ambiguous=False,
            assignment_basis="default_general",
            review_reason=None,
        )

    # Sort by start_offset descending to get the closest enclosing heading
    preceding_sorted = sorted(preceding, key=lambda h: h.get("start_offset", 0), reverse=True)
    nearest_heading = preceding_sorted[0]
    heading_text = nearest_heading.get("text", "")

    # Check for scope keywords in the nearest heading
    matched_scopes = []
    for pattern, scope in SCOPE_MAPPINGS:
        if pattern.search(heading_text):
            matched_scopes.append(scope)

    # If multiple contradictory scopes found in the same heading: ambiguous
    if len(matched_scopes) > 1:
        return ScopeAssignmentResult(
            scope_key=None,
            is_ambiguous=True,
            assignment_basis="ambiguous",
            review_reason=f"ambiguous_multiple_scopes_in_heading: {heading_text}",
        )

    if len(matched_scopes) == 1:
        return ScopeAssignmentResult(
            scope_key=matched_scopes[0],
            is_ambiguous=False,
            assignment_basis="heading_chain",
            review_reason=None,
        )

    # If the nearest heading did not match, check higher parent headings if available
    for h in preceding_sorted[1:]:
        for pattern, scope in SCOPE_MAPPINGS:
            if pattern.search(h.get("text", "")):
                return ScopeAssignmentResult(
                    scope_key=scope,
                    is_ambiguous=False,
                    assignment_basis="heading_chain",
                    review_reason=None,
                )

    # Fallback to model proposal if heading text had no scope keywords
    if model_scope_proposal:
        clean_proposal = model_scope_proposal.strip().lower()
        return ScopeAssignmentResult(
            scope_key=clean_proposal,
            is_ambiguous=False,
            assignment_basis="model_fallback",
            review_reason=None,
        )

    return ScopeAssignmentResult(
        scope_key="general",
        is_ambiguous=False,
        assignment_basis="default_general",
        review_reason=None,
    )
