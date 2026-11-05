"""Snapshot persistence: deterministic hashing, immutability, hash short-circuit.

Responsibility (single owner, ARCHITECTURE §6.1 ``ingest.snapshot_store``):
turn a parsed page into an immutable ``page_snapshots`` row, or detect an
unchanged re-ingest via the content hash and refuse to create a duplicate
snapshot. Mixed concerns are kept out: nothing here implies "live fetched"
— the ``fixture`` flag and ``fetched_at`` carry provenance, and fixture
ingestion is always labelled.

Hash definition (deterministic, documented for tests):
``content_hash = sha256(parser_version utf-8 + "\n" + final text utf-8)``.
The parser version participates so a parser upgrade produces a new hash
next re-ingest instead of a silent same-hash confusion.

Immutability: no update/mutation path is provided for snapshots; SQLAlchemy
relation is read-only from the API. D8 cascade lives in the ORM.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.exceptions import SnapshotImmutabilityViolation
from backend.app.core.logging import get_logger
from backend.app.db.orm import PageSnapshot, Source
from backend.app.parsers import text_extract
from backend.app.parsers.text_extract import ParsedPage

logger = get_logger(__name__)


def compute_content_hash(parser_version: str, text: str) -> str:
    digest = hashlib.sha256()
    digest.update(parser_version.encode("utf-8"))
    digest.update(b"\n")
    digest.update(text.encode("utf-8"))
    return digest.hexdigest()


@dataclass(frozen=True)
class IngestOutcome:
    """Persisted outcome of one ingestion attempt (or hash short-circuit)."""

    snapshot_id: int | None
    unchanged: bool
    previous_snapshot_id: int | None
    content_hash: str


def find_snapshot_by_hash(session: Session, source_id: int, content_hash: str) -> PageSnapshot | None:
    return session.scalar(
        select(PageSnapshot).where(
            PageSnapshot.source_id == source_id,
            PageSnapshot.content_hash == content_hash,
        )
    )


def latest_snapshot(session: Session, source_id: int) -> PageSnapshot | None:
    return session.scalar(
        select(PageSnapshot)
        .where(PageSnapshot.source_id == source_id)
        .order_by(PageSnapshot.fetched_at.desc(), PageSnapshot.id.desc())
        .limit(1)
    )


def persist_snapshot(
    session: Session,
    source: Source,
    parsed: ParsedPage,
    *,
    fetched_at=None,
) -> tuple[PageSnapshot, IngestOutcome]:
    """Persist one snapshot unless identical content already exists.

    Returns ``(snapshot, outcome)`` where ``snapshot`` is the existing row on
    the short-circuit path and the newly created row otherwise.
    """
    hash_ = compute_content_hash(text_extract.PARSER_VERSION, parsed.text)

    existing = find_snapshot_by_hash(session, source.id, hash_)
    if existing is not None:
        logger.info(
            "ingest_unchanged source_id=%d snapshot_id=%d content_hash=%s",
            source.id, existing.id, hash_[:12],
        )
        return existing, IngestOutcome(
            snapshot_id=existing.id,
            unchanged=True,
            previous_snapshot_id=existing.id,
            content_hash=hash_,
        )

    snapshot = PageSnapshot(
        source_id=source.id,
        title=parsed.title,
        text=parsed.text,
        content_hash=hash_,
        parser_version=text_extract.PARSER_VERSION,
        fixture=bool(source.kind == "fixture"),
        headings=[
            {
                "level": span.level,
                "text": span.text,
                "start_offset": span.start_offset,
                "end_offset": span.end_offset,
            }
            for span in parsed.headings
        ],
    )
    if fetched_at is not None:  # tests pin time; production uses default clock
        snapshot.fetched_at = fetched_at
    session.add(snapshot)
    session.flush()
    logger.info(
        "ingest_snapshot_created source_id=%d snapshot_id=%d content_hash=%s headings=%d chars=%d",
        source.id, snapshot.id, hash_[:12], len(parsed.headings), len(parsed.text),
    )
    return snapshot, IngestOutcome(
        snapshot_id=snapshot.id,
        unchanged=False,
        previous_snapshot_id=None,
        content_hash=hash_,
    )


def guard_immutable(snapshot: PageSnapshot) -> None:
    """Belt-and-braces check for every code path that received a snapshot."""
    if snapshot._sa_instance_state.pending or snapshot.id is None:  # noqa: SLF001
        return
    # Any future update path must route through here and be refused.
    raise SnapshotImmutabilityViolation("page_snapshots rows are immutable; create a new snapshot instead")
