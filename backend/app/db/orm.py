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

from sqlalchemy import Boolean, CheckConstraint, DDL, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, event
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
    source_links: Mapped[list["OrganisationSourceLink"]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
    )
    observations: Mapped[list["SourceObservation"]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
        order_by="SourceObservation.observation_sequence.asc()",
    )
    change_events: Mapped[list["ChangeEvent"]] = relationship(
        back_populates="source",
        passive_deletes=True,
    )
    opportunities: Mapped[list["Opportunity"]] = relationship(
        back_populates="source",
        passive_deletes=True,
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
    prompt_run_id: Mapped[int | None] = mapped_column(ForeignKey("prompt_runs.id", ondelete="SET NULL"), nullable=True)
    field_key: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_value: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(64), default=None, nullable=True)
    evidence_id: Mapped[int | None] = mapped_column(ForeignKey("evidence.id", ondelete="SET NULL"), nullable=True)
    model_tag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    via: Mapped[str] = mapped_column(String(32), default="ai_extraction", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="candidate", nullable=False)
    needs_review_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    snapshot: Mapped[PageSnapshot] = relationship(back_populates="facts")
    prompt_run: Mapped[PromptRun | None] = relationship(back_populates="facts")
    evidence: Mapped[Evidence | None] = relationship(back_populates="facts")

    __table_args__ = (
        Index("ix_facts_snapshot_id", "snapshot_id"),
        Index("ix_facts_field_key", "field_key"),
        Index("ix_facts_status", "status"),
        Index("ix_facts_prompt_run_id", "prompt_run_id"),
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
    facts: Mapped[list["Fact"]] = relationship(back_populates="prompt_run")

    __table_args__ = (
        Index("ix_prompt_runs_snapshot_id", "snapshot_id"),
        Index("ix_prompt_runs_request_hash", "request_payload_hash"),
    )


class Organisation(Base):
    """Canonical healthcare practice organisation entity (ARCHITECTURE §6.1, D39, D40)."""

    __tablename__ = "organisations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    primary_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    corporate_parent_id: Mapped[int | None] = mapped_column(ForeignKey("organisations.id", ondelete="SET NULL"), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    suburb: Mapped[str | None] = mapped_column(String(128), nullable=True)
    postcode: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    source_links: Mapped[list["OrganisationSourceLink"]] = relationship(
        back_populates="organisation",
        cascade="all, delete-orphan",
    )
    match_candidates: Mapped[list["OrganisationMatchCandidate"]] = relationship(
        back_populates="suggested_organisation",
        cascade="all, delete-orphan",
    )
    discrepancies: Mapped[list["Discrepancy"]] = relationship(
        back_populates="organisation",
        passive_deletes=True,
    )
    change_events: Mapped[list["ChangeEvent"]] = relationship(
        back_populates="organisation",
        passive_deletes=True,
    )
    opportunities: Mapped[list["Opportunity"]] = relationship(
        back_populates="organisation",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_organisations_slug", "slug", unique=True),
        Index("ix_organisations_primary_domain", "primary_domain"),
        Index("ix_organisations_phone", "phone"),
        Index("ix_organisations_locality", "state", "postcode", "suburb"),
    )


class OrganisationSourceLink(Base):
    """Additive N:M mapping between organisations and registered source URLs with path scoping (D40)."""

    __tablename__ = "organisation_source_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organisation_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    relationship_type: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    verification_status: Mapped[str] = mapped_column(String(32), nullable=False, default="suggested")
    verified_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    organisation: Mapped[Organisation] = relationship(back_populates="source_links")
    source: Mapped[Source] = relationship(back_populates="source_links")

    __table_args__ = (
        UniqueConstraint("organisation_id", "source_id", "scope_path", name="uq_org_source_scope"),
        Index("ix_org_source_links_org_id", "organisation_id"),
        Index("ix_org_source_links_source_id", "source_id"),
        Index("ix_org_source_links_status", "verification_status"),
    )


class SourceObservation(Base):
    """Observation attempt heartbeat recording successful crawls, unchanged states, and failures (D40)."""

    __tablename__ = "source_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("page_snapshots.id", ondelete="SET NULL"), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_changed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    parsing_completeness: Mapped[str] = mapped_column(String(32), nullable=False, default="complete")
    extraction_completeness: Mapped[str] = mapped_column(String(32), nullable=False, default="not_run")
    headings_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    character_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    observation_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    error_details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    source: Mapped[Source] = relationship(back_populates="observations")
    snapshot: Mapped[PageSnapshot | None] = relationship()

    __table_args__ = (
        UniqueConstraint("source_id", "observation_sequence", name="uq_source_obs_seq"),
        Index("ix_source_obs_source_seq", "source_id", "observation_sequence"),
        Index("ix_source_obs_source_date", "source_id", "observed_at"),
        Index("ix_source_obs_outcome", "outcome"),
    )


class OrganisationMatchCandidate(Base):
    """Conservative entity resolution match suggestion between a source and organisation (D39)."""

    __tablename__ = "organisation_match_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    suggested_organisation_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False)
    match_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_metric_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence_signals: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="suggested")
    reviewed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    source: Mapped[Source] = relationship()
    suggested_organisation: Mapped[Organisation] = relationship(back_populates="match_candidates")

    __table_args__ = (
        UniqueConstraint("source_id", "suggested_organisation_id", name="uq_match_source_org"),
        Index("ix_match_candidates_source_id", "source_id"),
        Index("ix_match_candidates_org_id", "suggested_organisation_id"),
        Index("ix_match_candidates_status", "status"),
        Index("ix_match_candidates_tier", "match_tier"),
    )


class Discrepancy(Base):
    """Evidence-backed factual conflict between independent sources linked to the same organisation (ARCHITECTURE §6.1, D41)."""

    __tablename__ = "discrepancies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organisation_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="RESTRICT"), nullable=False)
    field_key: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(64), nullable=False)
    fact_a_id: Mapped[int] = mapped_column(ForeignKey("facts.id", ondelete="RESTRICT"), nullable=False)
    fact_b_id: Mapped[int] = mapped_column(ForeignKey("facts.id", ondelete="RESTRICT"), nullable=False)
    conflict_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="detected")
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    organisation: Mapped[Organisation] = relationship(back_populates="discrepancies")
    fact_a: Mapped[Fact] = relationship(foreign_keys=[fact_a_id])
    fact_b: Mapped[Fact] = relationship(foreign_keys=[fact_b_id])

    __table_args__ = (
        CheckConstraint("fact_a_id < fact_b_id", name="ck_discrepancy_canonical_fact_order"),
        UniqueConstraint("fact_a_id", "fact_b_id", name="uq_discrepancy_fact_pair"),
        Index("ix_discrepancies_org_id", "organisation_id"),
        Index("ix_discrepancies_field_scope", "field_key", "scope_key"),
        Index("ix_discrepancies_conflict_type", "conflict_type"),
        Index("ix_discrepancies_status", "status"),
    )


class ChangeEvent(Base):
    """Immutable, append-only temporal change event between successive observations of the same source (ARCHITECTURE §6.1, D42)."""

    __tablename__ = "change_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False)
    organisation_id: Mapped[int | None] = mapped_column(ForeignKey("organisations.id", ondelete="RESTRICT"), nullable=True)
    previous_observation_id: Mapped[int | None] = mapped_column(ForeignKey("source_observations.id", ondelete="RESTRICT"), nullable=True)
    current_observation_id: Mapped[int] = mapped_column(ForeignKey("source_observations.id", ondelete="RESTRICT"), nullable=False)
    previous_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("page_snapshots.id", ondelete="RESTRICT"), nullable=True)
    current_snapshot_id: Mapped[int] = mapped_column(ForeignKey("page_snapshots.id", ondelete="RESTRICT"), nullable=False)
    previous_fact_id: Mapped[int | None] = mapped_column(ForeignKey("facts.id", ondelete="RESTRICT"), nullable=True)
    current_fact_id: Mapped[int | None] = mapped_column(ForeignKey("facts.id", ondelete="RESTRICT"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False, default="fact")
    field_key: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    diff_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    source: Mapped[Source] = relationship(back_populates="change_events")
    organisation: Mapped[Organisation | None] = relationship(back_populates="change_events")
    previous_observation: Mapped[SourceObservation | None] = relationship(foreign_keys=[previous_observation_id])
    current_observation: Mapped[SourceObservation] = relationship(foreign_keys=[current_observation_id])
    previous_snapshot: Mapped[PageSnapshot | None] = relationship(foreign_keys=[previous_snapshot_id])
    current_snapshot: Mapped[PageSnapshot] = relationship(foreign_keys=[current_snapshot_id])
    previous_fact: Mapped[Fact | None] = relationship(foreign_keys=[previous_fact_id])
    current_fact: Mapped[Fact | None] = relationship(foreign_keys=[current_fact_id])

    __table_args__ = (
        CheckConstraint(
            "previous_snapshot_id IS NULL OR previous_snapshot_id != current_snapshot_id",
            name="ck_change_event_snapshots",
        ),
        Index("ix_change_events_source_id", "source_id"),
        Index("ix_change_events_org_id", "organisation_id"),
        Index("ix_change_events_event_type", "event_type"),
        Index("ix_change_events_field_scope", "field_key", "scope_key"),
        Index("ix_change_events_detected_at", "detected_at"),
        Index("ix_change_events_idempotency", "idempotency_key", unique=True),
    )


class Opportunity(Base):
    """Healthcare workforce recruitment vacancy entity (ARCHITECTURE §6.1, D43)."""

    __tablename__ = "opportunities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organisation_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="RESTRICT"), nullable=False)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False)
    first_snapshot_id: Mapped[int] = mapped_column(ForeignKey("page_snapshots.id", ondelete="RESTRICT"), nullable=False)
    last_snapshot_id: Mapped[int] = mapped_column(ForeignKey("page_snapshots.id", ondelete="RESTRICT"), nullable=False)
    role_category: Mapped[str] = mapped_column(String(64), nullable=False)
    role_title: Mapped[str] = mapped_column(String(255), nullable=False)
    verbatim_wording: Mapped[str] = mapped_column(Text, nullable=False)
    contact_channel: Mapped[str | None] = mapped_column(String(255), nullable=True)
    explicit_ad_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="advertised")
    closure_evidence_quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_absences: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    organisation: Mapped[Organisation] = relationship(back_populates="opportunities")
    source: Mapped[Source] = relationship(back_populates="opportunities")
    first_snapshot: Mapped[PageSnapshot] = relationship(foreign_keys=[first_snapshot_id])
    last_snapshot: Mapped[PageSnapshot] = relationship(foreign_keys=[last_snapshot_id])

    __table_args__ = (
        Index("ix_opportunities_organisation_id", "organisation_id"),
        Index("ix_opportunities_role_category", "role_category"),
        Index("ix_opportunities_status", "status"),
        Index("ix_opportunities_explicit_ad_id", "explicit_ad_id"),
        Index("ix_opportunities_source_id", "source_id"),
    )


class ReviewAction(Base):
    """Application-level append-only record of a human decision by a locally identified reviewer (ARCHITECTURE §6.1, D44)."""

    __tablename__ = "review_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_status: Mapped[str] = mapped_column(String(32), nullable=False)
    new_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewer: Mapped[str] = mapped_column(String(255), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_id: Mapped[int | None] = mapped_column(ForeignKey("evidence.id", ondelete="RESTRICT"), nullable=True)
    decision_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    evidence: Mapped[Evidence | None] = relationship()

    __table_args__ = (
        Index("ix_review_actions_entity", "entity_type", "entity_id"),
        Index("ix_review_actions_action", "action"),
        Index("ix_review_actions_reviewer", "reviewer"),
        Index("ix_review_actions_created_at", "created_at"),
    )


event.listen(
    ReviewAction.__table__,
    "after_create",
    DDL("""
    CREATE TRIGGER IF NOT EXISTS prevent_review_actions_update
    BEFORE UPDATE ON review_actions
    BEGIN
        SELECT RAISE(FAIL, 'UPDATE on review_actions is prohibited: audit records are append-only');
    END;
    """).execute_if(dialect="sqlite"),
)

event.listen(
    ReviewAction.__table__,
    "after_create",
    DDL("""
    CREATE TRIGGER IF NOT EXISTS prevent_review_actions_delete
    BEFORE DELETE ON review_actions
    BEGIN
        SELECT RAISE(FAIL, 'DELETE on review_actions is prohibited: audit records are append-only');
    END;
    """).execute_if(dialect="sqlite"),
)


class PracticeLifecycleEvent(Base):
    """Healthcare practice lifecycle intelligence event (closure, relocation, corporate acquisition) (M3.7)."""

    __tablename__ = "practice_lifecycle_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organisation_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="RESTRICT"), nullable=False)
    branch_locality: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_branch_level: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("page_snapshots.id", ondelete="RESTRICT"), nullable=False)
    evidence_id: Mapped[int | None] = mapped_column(ForeignKey("evidence.id", ondelete="RESTRICT"), nullable=True)
    evidence_quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    effective_date: Mapped[str | None] = mapped_column(String(64), nullable=True)
    date_precision: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    acquiring_organisation_id: Mapped[int | None] = mapped_column(ForeignKey("organisations.id", ondelete="RESTRICT"), nullable=True)
    acquiring_entity_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    review_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    organisation: Mapped[Organisation] = relationship(foreign_keys=[organisation_id])
    acquiring_organisation: Mapped[Organisation | None] = relationship(foreign_keys=[acquiring_organisation_id])
    source: Mapped[Source] = relationship()
    snapshot: Mapped[PageSnapshot] = relationship()
    evidence: Mapped[Evidence | None] = relationship()

    __table_args__ = (
        Index("ix_practice_lifecycle_events_org", "organisation_id"),
        Index("ix_practice_lifecycle_events_source", "source_id"),
        Index("ix_practice_lifecycle_events_snapshot", "snapshot_id"),
        Index("ix_practice_lifecycle_events_type", "event_type"),
        Index("ix_practice_lifecycle_events_status", "status"),
        Index("ix_practice_lifecycle_events_review", "review_status"),
        Index("ix_practice_lifecycle_events_idempotency", "idempotency_key", unique=True),
    )
