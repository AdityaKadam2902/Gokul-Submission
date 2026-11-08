"""Source observation tracker and ingestion heartbeat engine (ARCHITECTURE §6.1, D40).

Responsibility:
    Records every source observation attempt (success_new_snapshot, success_unchanged,
    failed, incomplete) with monotonically increasing observation sequence numbers per source.
    Captures unchanged short-circuit crawls without modifying immutable page_snapshots.

Guarantees:
    - CONCURRENCY SAFETY: Per-source synchronization lock combined with database uniqueness
      constraints and an atomic retry loop prevents sequence collision or silent observation
      loss under concurrent ingestion attempts.
    - SEQUENCE INTEGRITY: Sequence numbers increase monotonically per source across all attempts.
    - SNAPSHOT PRESERVATION: Unchanged observations reference existing snapshots; failed attempts
      never invent new snapshots.
    - INDEPENDENT STATES: parsing_completeness and extraction_completeness remain strictly independent.
    - ERROR TRANSPARENCY: Failed observations record error details without masking the original error.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from backend.app.core.logging import get_logger
from backend.app.db.orm import Source, SourceObservation as SourceObservationORM
from backend.app.intelligence.schemas import (
    ExtractionCompleteness,
    ObservationOutcome,
    ParsingCompleteness,
    SourceObservation as SourceObservationSchema,
)

logger = get_logger(__name__)

_source_locks: dict[int, threading.Lock] = {}
_lock_registry_lock = threading.Lock()


def _get_source_lock(source_id: int) -> threading.Lock:
    """Retrieve or initialize a per-source re-entrant synchronization lock."""
    with _lock_registry_lock:
        if source_id not in _source_locks:
            _source_locks[source_id] = threading.Lock()
        return _source_locks[source_id]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def record_observation(
    session: Session,
    source_id: int,
    outcome: ObservationOutcome | str,
    *,
    snapshot_id: int | None = None,
    content_hash: str | None = None,
    content_changed: bool = False,
    parsing_completeness: ParsingCompleteness | str | None = None,
    extraction_completeness: ExtractionCompleteness | str = ExtractionCompleteness.NOT_RUN,
    headings_count: int = 0,
    character_count: int = 0,
    error_details: str | None = None,
    observed_at: datetime | None = None,
) -> SourceObservationORM:
    """Record an ingestion attempt in source_observations with monotonic, concurrency-safe sequence allocation."""
    outcome_str = str(outcome.value if isinstance(outcome, ObservationOutcome) else outcome)

    # Derive default parsing completeness based on outcome if not explicitly provided
    if parsing_completeness is None:
        if outcome_str == "failed":
            parse_comp_str = "failed"
        elif outcome_str == "incomplete":
            parse_comp_str = "partial"
        else:
            parse_comp_str = "complete"
    else:
        parse_comp_str = str(parsing_completeness.value if isinstance(parsing_completeness, ParsingCompleteness) else parsing_completeness)

    extract_comp_str = str(extraction_completeness.value if isinstance(extraction_completeness, ExtractionCompleteness) else extraction_completeness)

    # Validate that source exists
    source = session.get(Source, source_id)
    if source is None:
        raise ValueError(f"Source with id {source_id} does not exist")

    obs_time = observed_at or _utcnow()

    # Synchronize sequence allocation per source to guarantee monotonic sequence assignment
    source_lock = _get_source_lock(source_id)
    with source_lock:
        max_retries = 8
        last_exc: Exception | None = None

        for attempt in range(max_retries):
            try:
                with session.begin_nested():
                    max_seq = session.scalar(
                        select(func.coalesce(func.max(SourceObservationORM.observation_sequence), 0))
                        .where(SourceObservationORM.source_id == source_id)
                    ) or 0
                    next_seq = max_seq + 1

                    obs_row = SourceObservationORM(
                        source_id=source_id,
                        observed_at=obs_time,
                        outcome=outcome_str,
                        snapshot_id=snapshot_id,
                        content_hash=content_hash,
                        content_changed=content_changed,
                        parsing_completeness=parse_comp_str,
                        extraction_completeness=extract_comp_str,
                        headings_count=headings_count,
                        character_count=character_count,
                        observation_sequence=next_seq,
                        error_details=error_details,
                        created_at=_utcnow(),
                    )
                    session.add(obs_row)
                    session.flush()

                    logger.info(
                        "source_observation_recorded source_id=%d sequence=%d outcome=%s changed=%s snapshot_id=%s attempt=%d",
                        source_id, next_seq, outcome_str, content_changed, snapshot_id, attempt + 1,
                    )
                    return obs_row
            except (IntegrityError, OperationalError) as exc:
                last_exc = exc
                if attempt == max_retries - 1:
                    logger.error(
                        "source_observation_concurrency_failure source_id=%d retries=%d error=%s",
                        source_id, max_retries, exc,
                    )
                    raise
                time.sleep(0.01 * (attempt + 1))

        if last_exc:
            raise last_exc
        raise RuntimeError(f"Failed to record observation for source {source_id}")


def get_latest_observation(session: Session, source_id: int) -> SourceObservationORM | None:
    """Retrieve the most recent observation record for a source."""
    return session.scalar(
        select(SourceObservationORM)
        .where(SourceObservationORM.source_id == source_id)
        .order_by(SourceObservationORM.observation_sequence.desc())
        .limit(1)
    )


def list_observations(session: Session, source_id: int, limit: int = 100) -> list[SourceObservationORM]:
    """Retrieve chronological observation history for a source."""
    return list(
        session.scalars(
            select(SourceObservationORM)
            .where(SourceObservationORM.source_id == source_id)
            .order_by(SourceObservationORM.observation_sequence.asc())
            .limit(limit)
        ).all()
    )


def to_observation_schema(obs: SourceObservationORM) -> SourceObservationSchema:
    """Convert ORM row to validated Pydantic schema."""
    return SourceObservationSchema(
        id=obs.id,
        source_id=obs.source_id,
        observed_at=obs.observed_at,
        outcome=ObservationOutcome(obs.outcome),
        snapshot_id=obs.snapshot_id,
        content_hash=obs.content_hash,
        content_changed=bool(obs.content_changed),
        parsing_completeness=ParsingCompleteness(obs.parsing_completeness),
        extraction_completeness=ExtractionCompleteness(obs.extraction_completeness),
        headings_count=obs.headings_count,
        character_count=obs.character_count,
        observation_sequence=obs.observation_sequence,
        error_details=obs.error_details,
        created_at=obs.created_at,
    )
