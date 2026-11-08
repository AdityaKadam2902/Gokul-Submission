"""Temporal change detection & observation delta engine (ARCHITECTURE §6.1, DECISION_LOG D42).

Responsibility:
    Identifies deterministic, evidence-backed changes between successive observations
    belonging to the SAME registered healthcare practice source over time.

Supported Event Semantics (D42):
    - fact_added: Fact appears in an eligible extraction with no prior fact in the source's history.
    - fact_modified: Existing source-local fact observed with a different comparable value.
    - unconfirmed_prior: Fact absent on 1st complete eligible extraction (1st absence).
    - fact_unobserved: Fact absent across 2 consecutive complete eligible extractions (2nd absence).
    - fact_reappeared: Previously unobserved/unconfirmed fact reappears in a subsequent extraction.

Guarantees & Safeguards:
    - SOURCE-LOCAL ONLY: Compares successive observations within the exact same source.
      Never compares different sources to generate change events.
    - EXTRACTION OMISSION DEFENSE: A fact missing from extraction but whose value or evidence quote
      is visibly present in the current snapshot's text is classified as an extraction omission,
      suppressing false removal alerts and freezing the absence counter.
    - INELIGIBLE OBSERVATION FREEZE: Failed crawls, incomplete observations, partial parsing,
      or partial extractions freeze the consecutive absence counter and emit zero change events.
    - UNCHANGED SHORT-CIRCUIT: An unchanged crawl (success_unchanged) records a heartbeat without
      creating duplicate snapshots, duplicate change events, or advancing absence counters.
    - REUSE DEMONSTRATION: Extraction completeness 'not_run' is ineligible unless validly
      reusing the exact unchanged snapshot of an earlier complete extraction.
    - HISTORICAL PRESERVATION: Old facts, snapshots, observations, and historical change events
      are immutable and never overwritten or cascade-deleted.
    - DETERMINISTIC IDEMPOTENCY: Every change event has a deterministic idempotency key
      preventing duplicate inserts on repeated scans.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.core.logging import get_logger
from backend.app.db.orm import (
    ChangeEvent as ChangeEventORM,
    Fact,
    Organisation,
    OrganisationSourceLink,
    PageSnapshot,
    Source,
    SourceObservation as SourceObservationORM,
)
from backend.app.intelligence.discrepancy_detector import (
    are_values_equivalent,
    verify_fact_evidence_integrity,
)
from backend.app.intelligence.schemas import (
    ChangeEventCategory,
    DiffMetadata,
    ExtractionCompleteness,
    ObservationOutcome,
    ParsingCompleteness,
)

logger = get_logger(__name__)

REPEATABLE_FIELDS = {
    "service_names",
    "services",
    "clinical_services",
    "practitioners",
    "doctors",
    "specialties",
    "allied_health",
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _normalize_text_basic(text: str) -> str:
    """Safely normalize text, punctuation, and whitespace for deterministic matching."""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    text = text.replace("—", "-").replace("–", "-").replace("−", "-")
    return re.sub(r"\s+", " ", text).strip().lower()


def compute_fact_identity(fact: Fact) -> str:
    """Compute deterministic source-local fact identity.

    For single-value fields (e.g. public_phone, opening_hours), identity is (field_key, scope_key).
    For multi-instance/repeatable fields (e.g. service_names), identity is (field_key, scope_key, normalized_value).
    """
    field_key = (fact.field_key or "").strip().lower()
    scope_key = (fact.scope_key or "default").strip().lower()
    if field_key in REPEATABLE_FIELDS:
        val_norm = _normalize_text_basic(fact.normalized_value or fact.raw_value or "")
        return f"{field_key}:{scope_key}:{val_norm}"
    return f"{field_key}:{scope_key}"


def is_eligible_observation(obs: SourceObservationORM) -> bool:
    """Determine whether an observation attempt satisfies completeness conditions for change detection.

    Eligible for fact comparison:
        - Outcome is success_new_snapshot.
        - Parsing completeness is 'complete'.
        - Extraction completeness is 'complete'.

    Eligible as an unchanged snapshot heartbeat:
        - Outcome is success_unchanged.
        - Parsing completeness is 'complete'.
        (Freezes absence counters; never treated as a complete extraction of new facts).

    Ineligible (Freezes state, emits 0 events):
        - Outcome is failed or incomplete.
        - Parsing completeness is partial or failed.
        - Extraction completeness is partial, failed, or not_run (without success_unchanged heartbeat).
    """
    outcome = (obs.outcome or "").lower()
    if outcome not in ("success_new_snapshot", "success_unchanged"):
        return False

    parsing = (obs.parsing_completeness or "").lower()
    if parsing != "complete":
        return False

    extraction = (obs.extraction_completeness or "").lower()
    if outcome == "success_new_snapshot":
        return extraction == "complete"

    # success_unchanged: eligible as an unchanged heartbeat if parsing was complete.
    # Note: extraction_completeness is typically 'not_run'; this is strictly frozen
    # and never treated as a complete extraction of new facts.
    if outcome == "success_unchanged":
        return True

    return False


def is_extraction_omission(
    fact: Fact,
    current_snapshot: PageSnapshot | None,
) -> bool:
    """Defense against extraction omissions (F13 Ground Truth).

    If a fact is absent from extraction output, but its evidence quote or raw value
    is STILL visibly present in the current snapshot's text, it is an extraction omission,
    NOT a genuine removal by the healthcare practice.
    """
    if current_snapshot is None or not current_snapshot.text:
        return False

    snap_text_norm = _normalize_text_basic(current_snapshot.text)

    # 1. Check if the original evidence quote is still verbatim in snapshot text
    if fact.evidence and fact.evidence.quote:
        ev_quote_norm = _normalize_text_basic(fact.evidence.quote)
        if len(ev_quote_norm) >= 6 and ev_quote_norm in snap_text_norm:
            logger.info(
                "extraction_omission_detected fact_id=%d field=%s quote_present_in_dom=True",
                fact.id, fact.field_key,
            )
            return True

    # 2. Check if the raw or normalized fact value is present in snapshot text
    raw_val_norm = _normalize_text_basic(fact.raw_value or "")
    if len(raw_val_norm) >= 4 and raw_val_norm in snap_text_norm:
        logger.info(
            "extraction_omission_detected fact_id=%d field=%s value_present_in_dom=True",
            fact.id, fact.field_key,
        )
        return True

    return False


def build_idempotency_key(
    source_id: int,
    current_obs_id: int,
    field_key: str,
    scope_key: str | None,
    event_type: str,
    fact_identity: str,
    current_snap_id: int,
) -> str:
    """Generate a deterministic, canonical idempotency key for a change event."""
    raw = f"{source_id}:{current_obs_id}:{field_key}:{scope_key or 'default'}:{event_type}:{fact_identity}:{current_snap_id}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    prefix = f"evt_s{source_id}_o{current_obs_id}_{field_key}_{event_type}"[:100]
    return f"{prefix}_{digest}"


@dataclass
class FactState:
    fact: Fact
    identity_key: str
    status: str = "observed"  # observed, unconfirmed_prior, fact_unobserved
    consecutive_absent_count: int = 0
    last_observed_snapshot_id: int = 0
    last_observed_obs_id: int = 0


@dataclass
class ChangeScanResult:
    source_id: int
    observations_evaluated: int
    changes_detected: int
    changes: list[ChangeEventORM] = field(default_factory=list)


def _evaluate_source_observation_stream(
    session: Session,
    source_id: int,
) -> tuple[list[ChangeEventORM], dict[str, FactState]]:
    """Scan all observations for a source in sequence and evaluate change events and fact states.
        - Observation sequence is the chronological authority.
        - Baseline observation establishes initial facts; emits 0 change events.
        - Unchanged observations (success_unchanged) confirm presence; emit 0 change events.
        - Ineligible observations freeze absence counters and emit 0 change events.
        - Extraction omissions suppress removal alerts and freeze absence counters.
        - First complete absence emits unconfirmed_prior.
        - Second consecutive complete absence emits fact_unobserved.
        - Returning facts emit fact_reappeared.
        - Modified values emit fact_modified.
        - New facts emit fact_added.
        - Idempotent: repeated runs return existing records without duplication.
    """
    source = session.get(Source, source_id)
    if source is None:
        raise ValueError(f"Source with id {source_id} does not exist")

    # Determine confirmed organisation link if one exists
    org_link = session.scalar(
        select(OrganisationSourceLink).where(
            OrganisationSourceLink.source_id == source_id,
            OrganisationSourceLink.verification_status == "confirmed",
        )
    )
    organisation_id = org_link.organisation_id if org_link else None

    # Load all observations for this source ordered chronologically by sequence
    observations = list(
        session.scalars(
            select(SourceObservationORM)
            .where(SourceObservationORM.source_id == source_id)
            .order_by(SourceObservationORM.observation_sequence.asc(), SourceObservationORM.id.asc())
        ).all()
    )

    if not observations:
        return []

    # Map snapshot_id -> list of non-rejected facts with verified evidence
    snapshot_facts: dict[int, list[Fact]] = {}

    def get_snapshot_facts(snap_id: int) -> list[Fact]:
        if snap_id not in snapshot_facts:
            snap = session.get(PageSnapshot, snap_id)
            if not snap:
                snapshot_facts[snap_id] = []
                return []
            facts = list(
                session.scalars(
                    select(Fact).where(
                        Fact.snapshot_id == snap_id,
                        Fact.status != "rejected",
                    )
                ).all()
            )
            # Verify evidence integrity
            verified = [f for f in facts if verify_fact_evidence_integrity(f, snap)]
            snapshot_facts[snap_id] = verified
        return snapshot_facts[snap_id]

    # Stateful tracker for source facts across time
    known_facts: dict[str, FactState] = {}
    is_baseline = True
    detected_events: list[ChangeEventORM] = []
    last_eligible_snapshot: PageSnapshot | None = None
    last_eligible_obs: SourceObservationORM | None = None

    for obs in observations:
        outcome = (obs.outcome or "").lower()

        # Ineligible observations freeze state
        if not is_eligible_observation(obs):
            logger.debug(
                "change_detector_skip_ineligible source=%d obs=%d outcome=%s parse=%s extract=%s",
                source_id, obs.id, obs.outcome, obs.parsing_completeness, obs.extraction_completeness,
            )
            continue

        # Handle success_unchanged
        if outcome == "success_unchanged":
            # Re-observation of the same immutable snapshot: freeze existing absence count.
            # Never infer that all previously known facts are present merely because the page hash is unchanged.
            # Does not advance or reset absence counters; zero change events emitted.
            logger.debug(
                "change_detector_success_unchanged_frozen source=%d obs=%d",
                source_id, obs.id,
            )
            last_eligible_obs = obs
            continue

        # Outcome is success_new_snapshot
        current_snap = session.get(PageSnapshot, obs.snapshot_id) if obs.snapshot_id else None
        if not current_snap:
            continue

        # Check if re-observing the exact same immutable snapshot
        if last_eligible_snapshot and current_snap.id == last_eligible_snapshot.id:
            # Re-observation of the same immutable snapshot: freeze existing absence count
            logger.debug(
                "change_detector_same_snapshot_frozen source=%d obs=%d snap=%d",
                source_id, obs.id, current_snap.id,
            )
            last_eligible_obs = obs
            continue

        curr_facts = get_snapshot_facts(current_snap.id)

        # 1. Baseline observation: establish initial facts without emitting events
        if is_baseline:
            for f in curr_facts:
                ident = compute_fact_identity(f)
                known_facts[ident] = FactState(
                    fact=f,
                    identity_key=ident,
                    status="observed",
                    consecutive_absent_count=0,
                    last_observed_snapshot_id=current_snap.id,
                    last_observed_obs_id=obs.id,
                )
            is_baseline = False
            last_eligible_snapshot = current_snap
            last_eligible_obs = obs
            logger.info(
                "change_detector_baseline_established source=%d obs=%d facts_count=%d",
                source_id, obs.id, len(curr_facts),
            )
            continue

        # 2. Subsequent observations: compare with established known state
        curr_facts_by_ident: dict[str, Fact] = {}
        for f in curr_facts:
            ident = compute_fact_identity(f)
            curr_facts_by_ident[ident] = f

        # Check additions, modifications, and reappearances
        for ident, f in curr_facts_by_ident.items():
            if ident not in known_facts:
                # FACT ADDED
                field_key = f.field_key
                scope_key = f.scope_key
                old_val = None
                new_val = f.raw_value
                event_type = ChangeEventCategory.FACT_ADDED.value
                rationale = (
                    f"Fact added: Independent source {source.url} published new {field_key} "
                    f"('{new_val}') not previously observed in established baseline."
                )
                diff_meta = {
                    "wording_diff": f"+{new_val}",
                    "new_normalized": f.normalized_value or f.raw_value,
                    "observation_count_absent": 0,
                    "omission_confirmed_in_dom": False,
                }
                idempotency_key = build_idempotency_key(
                    source_id=source_id,
                    current_obs_id=obs.id,
                    field_key=field_key,
                    scope_key=scope_key,
                    event_type=event_type,
                    fact_identity=ident,
                    current_snap_id=current_snap.id,
                )
                evt = _persist_change_event(
                    session=session,
                    source_id=source_id,
                    organisation_id=organisation_id,
                    previous_observation_id=last_eligible_obs.id if last_eligible_obs else None,
                    current_observation_id=obs.id,
                    previous_snapshot_id=last_eligible_snapshot.id if last_eligible_snapshot else None,
                    current_snapshot_id=current_snap.id,
                    previous_fact_id=None,
                    current_fact_id=f.id,
                    event_type=event_type,
                    field_key=field_key,
                    scope_key=scope_key,
                    old_value=old_val,
                    new_value=new_val,
                    rationale=rationale,
                    diff_metadata=diff_meta,
                    idempotency_key=idempotency_key,
                )
                detected_events.append(evt)
                known_facts[ident] = FactState(
                    fact=f,
                    identity_key=ident,
                    status="observed",
                    consecutive_absent_count=0,
                    last_observed_snapshot_id=current_snap.id,
                    last_observed_obs_id=obs.id,
                )
            else:
                prior_state = known_facts[ident]
                prior_fact = prior_state.fact

                # 1. Fact was confirmed unobserved (>= 2 complete absences) and has now reappeared
                if prior_state.status == "fact_unobserved":
                    field_key = f.field_key
                    scope_key = f.scope_key
                    old_val = prior_fact.raw_value
                    new_val = f.raw_value
                    event_type = ChangeEventCategory.FACT_REAPPEARED.value
                    rationale = (
                        f"Fact reappeared: Previously unobserved {field_key} ('{new_val}') "
                        f"reappeared in observation {obs.observation_sequence}."
                    )
                    diff_meta = {
                        "wording_diff": f"reappeared: {new_val}",
                        "new_normalized": f.normalized_value or f.raw_value,
                        "observation_count_absent": 0,
                        "omission_confirmed_in_dom": False,
                    }
                    idempotency_key = build_idempotency_key(
                        source_id=source_id,
                        current_obs_id=obs.id,
                        field_key=field_key,
                        scope_key=scope_key,
                        event_type=event_type,
                        fact_identity=ident,
                        current_snap_id=current_snap.id,
                    )
                    evt = _persist_change_event(
                        session=session,
                        source_id=source_id,
                        organisation_id=organisation_id,
                        previous_observation_id=last_eligible_obs.id if last_eligible_obs else None,
                        current_observation_id=obs.id,
                        previous_snapshot_id=last_eligible_snapshot.id if last_eligible_snapshot else None,
                        current_snapshot_id=current_snap.id,
                        previous_fact_id=prior_fact.id,
                        current_fact_id=f.id,
                        event_type=event_type,
                        field_key=field_key,
                        scope_key=scope_key,
                        old_value=old_val,
                        new_value=new_val,
                        rationale=rationale,
                        diff_metadata=diff_meta,
                        idempotency_key=idempotency_key,
                    )
                    detected_events.append(evt)
                    prior_state.status = "observed"
                    prior_state.consecutive_absent_count = 0
                    prior_state.fact = f
                    prior_state.last_observed_snapshot_id = current_snap.id
                    prior_state.last_observed_obs_id = obs.id

                # 2. Fact was unconfirmed_prior (1 absence); positive observation resets count
                elif prior_state.status == "unconfirmed_prior":
                    prior_state.status = "observed"
                    prior_state.consecutive_absent_count = 0
                    if f.field_key not in REPEATABLE_FIELDS:
                        old_raw = prior_fact.raw_value
                        new_raw = f.raw_value
                        if not are_values_equivalent(f.field_key, old_raw, new_raw):
                            field_key = f.field_key
                            scope_key = f.scope_key
                            event_type = ChangeEventCategory.FACT_MODIFIED.value
                            rationale = (
                                f"Fact modified: Source {source.url} updated {field_key} "
                                f"from '{old_raw}' to '{new_raw}' on scope '{scope_key}'."
                            )
                            diff_meta = {
                                "wording_diff": f"-{old_raw} +{new_raw}",
                                "old_normalized": prior_fact.normalized_value or prior_fact.raw_value,
                                "new_normalized": f.normalized_value or f.raw_value,
                                "observation_count_absent": 0,
                                "omission_confirmed_in_dom": False,
                            }
                            idempotency_key = build_idempotency_key(
                                source_id=source_id,
                                current_obs_id=obs.id,
                                field_key=field_key,
                                scope_key=scope_key,
                                event_type=event_type,
                                fact_identity=ident,
                                current_snap_id=current_snap.id,
                            )
                            evt = _persist_change_event(
                                session=session,
                                source_id=source_id,
                                organisation_id=organisation_id,
                                previous_observation_id=last_eligible_obs.id if last_eligible_obs else None,
                                current_observation_id=obs.id,
                                previous_snapshot_id=last_eligible_snapshot.id if last_eligible_snapshot else None,
                                current_snapshot_id=current_snap.id,
                                previous_fact_id=prior_fact.id,
                                current_fact_id=f.id,
                                event_type=event_type,
                                field_key=field_key,
                                scope_key=scope_key,
                                old_value=old_raw,
                                new_value=new_raw,
                                rationale=rationale,
                                diff_metadata=diff_meta,
                                idempotency_key=idempotency_key,
                            )
                            detected_events.append(evt)
                    prior_state.fact = f
                    prior_state.last_observed_snapshot_id = current_snap.id
                    prior_state.last_observed_obs_id = obs.id

                # 3. Fact was already observed: reset absence count and check for value modifications
                elif f.field_key not in REPEATABLE_FIELDS:
                    old_raw = prior_fact.raw_value
                    new_raw = f.raw_value
                    if not are_values_equivalent(f.field_key, old_raw, new_raw):
                        field_key = f.field_key
                        scope_key = f.scope_key
                        event_type = ChangeEventCategory.FACT_MODIFIED.value
                        rationale = (
                            f"Fact modified: Source {source.url} updated {field_key} "
                            f"from '{old_raw}' to '{new_raw}' on scope '{scope_key}'."
                        )
                        diff_meta = {
                            "wording_diff": f"-{old_raw} +{new_raw}",
                            "old_normalized": prior_fact.normalized_value or prior_fact.raw_value,
                            "new_normalized": f.normalized_value or f.raw_value,
                            "observation_count_absent": 0,
                            "omission_confirmed_in_dom": False,
                        }
                        idempotency_key = build_idempotency_key(
                            source_id=source_id,
                            current_obs_id=obs.id,
                            field_key=field_key,
                            scope_key=scope_key,
                            event_type=event_type,
                            fact_identity=ident,
                            current_snap_id=current_snap.id,
                        )
                        evt = _persist_change_event(
                            session=session,
                            source_id=source_id,
                            organisation_id=organisation_id,
                            previous_observation_id=last_eligible_obs.id if last_eligible_obs else None,
                            current_observation_id=obs.id,
                            previous_snapshot_id=last_eligible_snapshot.id if last_eligible_snapshot else None,
                            current_snapshot_id=current_snap.id,
                            previous_fact_id=prior_fact.id,
                            current_fact_id=f.id,
                            event_type=event_type,
                            field_key=field_key,
                            scope_key=scope_key,
                            old_value=old_raw,
                            new_value=new_raw,
                            rationale=rationale,
                            diff_metadata=diff_meta,
                            idempotency_key=idempotency_key,
                        )
                        detected_events.append(evt)
                        prior_state.fact = f
                        prior_state.consecutive_absent_count = 0
                        prior_state.last_observed_snapshot_id = current_snap.id
                        prior_state.last_observed_obs_id = obs.id
                    else:
                        prior_state.consecutive_absent_count = 0
                else:
                    prior_state.consecutive_absent_count = 0

        # Check absences and omissions for facts not in current extraction
        for ident, state in list(known_facts.items()):
            if ident not in curr_facts_by_ident:
                # Fact is missing from extraction output
                # Check extraction omission defense
                if is_extraction_omission(state.fact, current_snap):
                    logger.info(
                        "suppressing_omission_removal source=%d obs=%d fact_id=%d ident=%s",
                        source_id, obs.id, state.fact.id, ident,
                    )
                    # Omission detected: freeze absence counter
                    continue

                # Genuine absence: advance consecutive absence counter
                state.consecutive_absent_count += 1
                field_key = state.fact.field_key
                scope_key = state.fact.scope_key
                old_val = state.fact.raw_value

                if state.consecutive_absent_count == 1:
                    # 1st consecutive complete absence -> unconfirmed_prior
                    state.status = "unconfirmed_prior"
                    event_type = ChangeEventCategory.UNCONFIRMED_PRIOR.value
                    rationale = (
                        f"Unconfirmed prior: Previously observed {field_key} ('{old_val}') was absent "
                        f"on complete observation {obs.observation_sequence} (absence 1 of 2 required)."
                    )
                    diff_meta = {
                        "wording_diff": f"absent_once: {old_val}",
                        "old_normalized": state.fact.normalized_value or state.fact.raw_value,
                        "observation_count_absent": 1,
                        "omission_confirmed_in_dom": False,
                    }
                    idempotency_key = build_idempotency_key(
                        source_id=source_id,
                        current_obs_id=obs.id,
                        field_key=field_key,
                        scope_key=scope_key,
                        event_type=event_type,
                        fact_identity=ident,
                        current_snap_id=current_snap.id,
                    )
                    evt = _persist_change_event(
                        session=session,
                        source_id=source_id,
                        organisation_id=organisation_id,
                        previous_observation_id=last_eligible_obs.id if last_eligible_obs else None,
                        current_observation_id=obs.id,
                        previous_snapshot_id=last_eligible_snapshot.id if last_eligible_snapshot else None,
                        current_snapshot_id=current_snap.id,
                        previous_fact_id=state.fact.id,
                        current_fact_id=None,
                        event_type=event_type,
                        field_key=field_key,
                        scope_key=scope_key,
                        old_value=old_val,
                        new_value=None,
                        rationale=rationale,
                        diff_metadata=diff_meta,
                        idempotency_key=idempotency_key,
                    )
                    detected_events.append(evt)

                elif state.consecutive_absent_count >= 2 and state.status != "fact_unobserved":
                    # 2nd consecutive complete absence -> fact_unobserved
                    state.status = "fact_unobserved"
                    event_type = ChangeEventCategory.FACT_UNOBSERVED.value
                    rationale = (
                        f"Fact unobserved: Fact {field_key} ('{old_val}') was absent across "
                        f"{state.consecutive_absent_count} consecutive complete observations (2-observation rule met)."
                    )
                    diff_meta = {
                        "wording_diff": f"unobserved: {old_val}",
                        "old_normalized": state.fact.normalized_value or state.fact.raw_value,
                        "observation_count_absent": state.consecutive_absent_count,
                        "omission_confirmed_in_dom": False,
                    }
                    idempotency_key = build_idempotency_key(
                        source_id=source_id,
                        current_obs_id=obs.id,
                        field_key=field_key,
                        scope_key=scope_key,
                        event_type=event_type,
                        fact_identity=ident,
                        current_snap_id=current_snap.id,
                    )
                    evt = _persist_change_event(
                        session=session,
                        source_id=source_id,
                        organisation_id=organisation_id,
                        previous_observation_id=last_eligible_obs.id if last_eligible_obs else None,
                        current_observation_id=obs.id,
                        previous_snapshot_id=last_eligible_snapshot.id if last_eligible_snapshot else None,
                        current_snapshot_id=current_snap.id,
                        previous_fact_id=state.fact.id,
                        current_fact_id=None,
                        event_type=event_type,
                        field_key=field_key,
                        scope_key=scope_key,
                        old_value=old_val,
                        new_value=None,
                        rationale=rationale,
                        diff_metadata=diff_meta,
                        idempotency_key=idempotency_key,
                    )
                    detected_events.append(evt)

        last_eligible_snapshot = current_snap
        last_eligible_obs = obs

    session.flush()
    logger.info(
        "change_detection_complete source=%d events_count=%d",
        source_id, len(detected_events),
    )
    return detected_events, known_facts


def detect_source_changes(
    session: Session,
    source_id: int,
) -> list[ChangeEventORM]:
    """Scan all observations for a source in sequence and detect temporal change events.
        - Observation sequence is the chronological authority.
        - Baseline observation establishes initial facts; emits 0 change events.
        - Unchanged observations (success_unchanged) confirm presence; emit 0 change events.
        - Ineligible observations freeze absence counters and emit 0 change events.
        - Extraction omissions suppress removal alerts and freeze absence counters.
        - First complete absence emits unconfirmed_prior.
        - Second consecutive complete absence emits fact_unobserved.
        - Returning facts emit fact_reappeared.
        - Modified values emit fact_modified.
        - New facts emit fact_added.
        - Idempotent: repeated runs return existing records without duplication.
    """
    events, _ = _evaluate_source_observation_stream(session, source_id)
    return events


def evaluate_source_fact_states(
    session: Session,
    source_id: int,
) -> dict[str, FactState]:
    """Evaluate and return the latest state (including absence counters and statuses) of all known facts for a source."""
    _, states = _evaluate_source_observation_stream(session, source_id)
    return states


def _persist_change_event(
    session: Session,
    *,
    source_id: int,
    organisation_id: int | None,
    previous_observation_id: int | None,
    current_observation_id: int,
    previous_snapshot_id: int | None,
    current_snapshot_id: int,
    previous_fact_id: int | None,
    current_fact_id: int | None,
    event_type: str,
    field_key: str,
    scope_key: str | None,
    old_value: str | None,
    new_value: str | None,
    rationale: str,
    diff_metadata: dict[str, Any],
    idempotency_key: str,
) -> ChangeEventORM:
    """Safely persist a change event or return the existing event if idempotency key matches."""
    existing = session.scalar(
        select(ChangeEventORM).where(ChangeEventORM.idempotency_key == idempotency_key)
    )
    if existing:
        return existing

    now = _utcnow()
    evt = ChangeEventORM(
        source_id=source_id,
        organisation_id=organisation_id,
        previous_observation_id=previous_observation_id,
        current_observation_id=current_observation_id,
        previous_snapshot_id=previous_snapshot_id,
        current_snapshot_id=current_snapshot_id,
        previous_fact_id=previous_fact_id,
        current_fact_id=current_fact_id,
        event_type=event_type,
        entity_type="fact",
        field_key=field_key,
        scope_key=scope_key,
        old_value=old_val if (old_val := old_value) is not None else None,
        new_value=new_val if (new_val := new_value) is not None else None,
        rationale=rationale,
        diff_metadata=diff_metadata,
        idempotency_key=idempotency_key,
        detected_at=now,
        created_at=now,
    )
    session.add(evt)
    session.flush()
    return evt


def scan_source_changes(
    session: Session,
    source_id: int,
) -> ChangeScanResult:
    """Execute change detection scan for a registered source and return summary."""
    events = detect_source_changes(session, source_id)
    obs_count = session.scalar(
        select(func.count(SourceObservationORM.id)).where(SourceObservationORM.source_id == source_id)
    ) or 0
    return ChangeScanResult(
        source_id=source_id,
        observations_evaluated=obs_count,
        changes_detected=len(events),
        changes=events,
    )


def scan_organisation_changes(
    session: Session,
    organisation_id: int,
) -> list[ChangeEventORM]:
    """Execute change detection across all confirmed sources for an organisation."""
    org = session.get(Organisation, organisation_id)
    if not org:
        raise ValueError(f"Organisation {organisation_id} does not exist")

    confirmed_source_ids = list(
        session.scalars(
            select(OrganisationSourceLink.source_id).where(
                OrganisationSourceLink.organisation_id == organisation_id,
                OrganisationSourceLink.verification_status == "confirmed",
            )
        ).all()
    )

    all_events: list[ChangeEventORM] = []
    for s_id in confirmed_source_ids:
        events = detect_source_changes(session, s_id)
        all_events.extend(events)

    return all_events
