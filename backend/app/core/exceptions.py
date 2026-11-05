"""Typed exceptions per concern (ARCHITECTURE §8 "Error handling").

Every failure mode used by M1 routers maps to a concrete subclass; routers
translate them into consistent 4xx JSON error payloads via one handler —
5xx never leaks stack traces into HTTP responses.
"""

from __future__ import annotations


class PracticeSignalError(Exception):
    """Base class: a machine-readable error code + human explanation."""

    http_status = 500
    error_code = "internal_error"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class SourceNotFound(PracticeSignalError):
    http_status = 404
    error_code = "source_not_found"


class SnapshotNotFound(PracticeSignalError):
    http_status = 404
    error_code = "snapshot_not_found"


class PolicyViolation(PracticeSignalError):
    """A request attempted something the source policy forbids.

    Always raised *before* any network access would occur (M1 performs no
    network activity at all); the class exists so the ordering guarantee
    is testable (M1-AC 1).
    """

    http_status = 422
    error_code = "policy_violation"


class LiveFetchNotAuthorised(PolicyViolation):
    """Ingestion was requested for a live (url) source with no approval.

    M1 implements the fixture path only; live fetching requires an
    ``approved_live`` source AND the fetcher (a later milestone).
    """

    http_status = 409
    error_code = "live_fetch_not_authorised"


class SourceBlocked(PolicyViolation):
    http_status = 409
    error_code = "source_blocked"


class FixtureError(PracticeSignalError):
    """Fixture corpus problem: missing file, bad manifest entry, bad schema."""

    http_status = 500
    error_code = "fixture_error"


class ValidationError_(PracticeSignalError):
    """Domain-level validation failure that FastAPI's 422 does not cover."""

    http_status = 422
    error_code = "domain_validation_error"


class SnapshotImmutabilityViolation(PracticeSignalError):
    http_status = 409
    error_code = "snapshot_immutable"
