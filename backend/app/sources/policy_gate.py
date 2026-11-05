"""Policy gate: the single validation point for what the pipeline may touch.

Responsibility: decide — deterministically, from stored source state —
whether ingestion may run, BEFORE any network operation would exist.
M1 reality check: there is no fetcher and no outbound path anywhere in
this codebase; the gate still enforces policy so the ordering guarantee
(M1-AC 1: rejection precedes any network access) is structural, not
accidental.

Source of truth for states: docs/SOURCE_REGISTER.md §1.
"""

from __future__ import annotations

from backend.app.core.exceptions import (
    FixtureError,
    LiveFetchNotAuthorised,
    SourceBlocked,
    ValidationError_,
)
from backend.app.models import SourceCreate

# Policy states (docs/SOURCE_REGISTER.md §1)
STATE_APPROVED_LIVE = "approved_live"
STATE_MANUAL_FIXTURE = "manual_fixture_pending_policy_check"
STATE_BLOCKED = "blocked"
STATE_UNAVAILABLE = "unavailable"

# API-settable states. ``approved_live`` is deliberately absent: flipping to
# live collection requires the documented manual robots/terms procedure in
# SOURCE_REGISTER §3 and is recorded in the register file, not via HTTP.
API_SETTABLE_POLICY_STATUSES = (STATE_MANUAL_FIXTURE, STATE_BLOCKED)


def validate_registration(payload: SourceCreate) -> dict:
    """Validate a source-registration request; return normalised decisions.

    Raises:
        ValidationError_: kind/policy/fixture combination is not permitted.
        FixtureError: kind=fixture but the fixture id is not in the manifest
            (deferred to then ingestion also re-checks).
    """
    if payload.policy_status not in API_SETTABLE_POLICY_STATUSES:
        raise ValidationError_(
            f"policy_status '{payload.policy_status}' cannot be set through the API; "
            f"allowed here: {list(API_SETTABLE_STATUSES)}",
            details={"requested": payload.policy_status},
        )
    if payload.kind == "fixture":
        if not payload.fixture_id:
            raise ValidationError_("kind=fixture requires fixture_id", details={"field": "fixture_id"})
    elif payload.kind == "url":
        if payload.url is None:
            raise ValidationError_("kind=url requires url", details={"field": "url"})
        if payload.policy_status != STATE_MANUAL_FIXTURE:
            raise ValidationError_(
                "new url sources must be registered as 'manual_fixture_pending_policy_check'; "
                "live approval is a documented manual procedure, not an API switch",
                details={"requested": payload.policy_status},
            )
    return {"decision": "accepted_pending_manifest_check", "http_status_authorized": False}


def validate_ingestion_ready(source) -> None:
    """Check a persisted source row may be ingested. Raises before any network call.

    M1 supports only the fixture path:
      - ``blocked``     → 409 (no ingestion at all; prior data retained — register §1)
      - kind=url        → 409 (no fetcher in M1; live approval is impossible today
                          because zero ``approved_live`` rows exist in the register)
      - otherwise       → allowed when a fixture id is bound
    """
    if source.policy_status == STATE_BLOCKED:
        raise SourceBlocked(
            f"source {source.id} is blocked: no ingestion permitted (SOURCE_REGISTER §1)",
            details={"source_id": source.id},
        )
    if source.kind == "url":
        raise LiveFetchNotAuthorised(
            "live fetching is not authorised for this source: M1 implements the fixture "
            "path only, and no 'approved_live' source exists in docs/SOURCE_REGISTER.md",
            details={"source_id": source.id, "policy_status": source.policy_status},
        )
    if source.kind == "fixture" and not source.fixture_id:
        raise FixtureError(
            f"fixture source {source.id} has no fixture_id bound",
            details={"source_id": source.id},
        )


# Operator convenience name kept consistent with known states.
API_SETTABLE_STATUSES = API_SETTABLE_POLICY_STATUSES
