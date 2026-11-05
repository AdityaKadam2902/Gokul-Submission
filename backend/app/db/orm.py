"""ORM models for the M1 tables (exact per ARCHITECTURE §6.3, D9).

``sources`` and ``page_snapshots`` only — ``organisations``, ``facts``,
``evidence`` etc. land in later milestones; nothing speculative here.
``sources.organisation_id`` exists as a plain nullable integer column in M1
(the FK constraint itself is added in M2 when the target table is created —
SQLite ALTER TABLE cannot add a FK later, and we do not fabricate a
half-functional constraint. Documented in docs/DECISION_LOG.md addendum).
"""

from __future__ import annotations

from datetime import datetime, UTC

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Source(Base):
    """One registered source row (allowlist state + approval metadata)."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Plain nullable int in M1; the organisations table + FK arrive in M2.
    organisation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # url | fixture
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)  # display name
    url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fixture_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # e.g. "F3"
    page_type: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_status: Mapped[str] = mapped_column(String(64), nullable=False)
    robots_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    intended_fields: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    approval_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    snapshots: Mapped[list["PageSnapshot"]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",  # D8: source deletion cascades snapshots
        order_by="PageSnapshot.fetched_at.desc()",
    )

    __table_args__ = (
        Index("ix_sources_policy_status", "policy_status"),
        Index("ix_sources_page_type", "page_type"),
    )


class PageSnapshot(Base):
    """Immutable parsed-text snapshot of one page at one point in time."""

    __tablename__ = "page_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    fixture: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Heading spans [(level, text, start_offset, end_offset)] recorded at
    # parse time so snapshot reads are deterministic without re-parsing HTML
    # (documented per-milestone column addition, ARCHITECTURE §6.3/§8).
    headings: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    source: Mapped[Source] = relationship(back_populates="snapshots")
    facts: Mapped[list["Fact"]] = relationship(
        back_populates="snapshot",
        cascade="all, delete-orphan",
        order_by="Fact.id.asc()",
    )
    evidence_items: Mapped[list["Evidence"]] = relationship(
        back_populates="snapshot",
        cascade="all, delete-orphan",
        order_by="Evidence.id.asc()",
    )
    prompt_runs: Mapped[list["PromptRun"]] = relationship(
        back_populates="snapshot",
        cascade="all, delete-orphan",
        order_by="PromptRun.id.desc()",
    )

    __table_args__ = (
        Index("ix_page_snapshots_source_id", "source_id"),
        Index("ix_page_snapshots_content_hash", "content_hash"),
        UniqueConstraint("source_id", "content_hash", name="uq_snapshot_source_hash"),
    )


class Evidence(Base):
    """Immutable excerpt citation linking a candidate fact to exact snapshot text."""

    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("page_snapshots.id", ondelete="CASCADE"), nullable=False)
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    match_algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False, default="py_subslice_v1")

    snapshot: Mapped[PageSnapshot] = relationship(back_populates="evidence_items")
    facts: Mapped[list["Fact"]] = relationship(back_populates="evidence")

    __table_args__ = (
        Index("ix_evidence_snapshot_id", "snapshot_id"),
    )


class Fact(Base):
    """One candidate fact extracted from a page snapshot (ARCHITECTURE §6.3)."""

    __tablename__ = "facts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organisation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("page_snapshots.id", ondelete="CASCADE"), nullable=False)
    field_key: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_value: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Australia/Brisbane", nullable=False)
    evidence_id: Mapped[int | None] = mapped_column(ForeignKey("evidence.id", ondelete="SET NULL"), nullable=True)
    model_tag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    via: Mapped[str] = mapped_column(String(32), default="ai_extraction", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="candidate", nullable=False)
    needs_review_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    snapshot: Mapped[PageSnapshot] = relationship(back_populates="facts")
    evidence: Mapped[Evidence | None] = relationship(back_populates="facts")

    __table_args__ = (
        Index("ix_facts_snapshot_id", "snapshot_id"),
        Index("ix_facts_field_key", "field_key"),
        Index("ix_facts_status", "status"),
    )


class PromptRun(Base):
    """Audit of every AI inference call regardless of outcome (ARCHITECTURE §6.3)."""

    __tablename__ = "prompt_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("page_snapshots.id", ondelete="CASCADE"), nullable=False)
    model_tag: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    request_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_response_ref: Mapped[str] = mapped_column(Text, nullable=False)
    parse_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    validation_errors: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    latency_ms: Mapped[float] = mapped_column(Integer, default=0, nullable=False)
    prompt_eval_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    eval_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens_per_second: Mapped[float] = mapped_column(Integer, default=0, nullable=False)
    evidence_checks: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    snapshot: Mapped[PageSnapshot] = relationship(back_populates="prompt_runs")

    __table_args__ = (
        Index("ix_prompt_runs_snapshot_id", "snapshot_id"),
        Index("ix_prompt_runs_request_hash", "request_payload_hash"),
    )

