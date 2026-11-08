"""M3.4 comprehensive information freshness & monitoring policy engine test suite.

Covers (ARCHITECTURE §6.1, DECISION_LOG D46):
1. Observation freshness:
   - Fresh source observation (age < aging threshold).
   - Aging source observation (approaching threshold).
   - Stale source observation (exceeds threshold).
   - Re-observation heartbeat: unchanged crawl confirms continued observability without resetting content age.
   - Preservation of last valid observation: failed/incomplete crawls do not overwrite last successful crawl currency.
   - Completeness isolation: partial parsing / failed extraction do not count as complete.
2. Fact freshness (explicit evidence only):
   - Explicitly dated facts (effective from, published on, updated date).
   - Undated facts classified as UNKNOWN (never falsely labeled STALE).
   - Fresh crawl does NOT make an old fact fresh (vital product principle).
   - Generic page update metadata is rejected and not attributed to the fact.
3. Information categories & policies:
   - Category thresholds (identity 90d, operational 30d, services 60d, hours 30d, general 60d).
   - Source-specific policy overrides.
   - Deterministic evaluation using injected reference clock.
4. Evidence verification vs fact currency:
   - Evidence quote verification integrity is separated from factual currency.
5. Organisation summary & isolation:
   - Multi-source aggregation, unobservable sources, unknown facts counts.
6. API routes & serialization:
   - Policy inspection, source freshness, fact freshness, organisation summary, dynamic evaluation.
   - 404 on missing entities, 400 on invalid requests.
7. Zero mutation guarantee:
   - Freshness evaluation never creates change events, overwrites facts, or alters observations.
"""

from __future__ import annotations

import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import orm
from backend.app.db.orm import (
    ChangeEvent as ChangeEventORM,
    Evidence,
    Fact,
    Organisation,
    OrganisationSourceLink,
    PageSnapshot,
    Source,
    SourceObservation as SourceObservationORM,
)
from backend.app.intelligence.freshness_engine import (
    DEFAULT_FRESHNESS_POLICY,
    evaluate_evidence_freshness,
    evaluate_fact_freshness,
    evaluate_organisation_freshness_summary,
    evaluate_source_content_freshness,
    evaluate_source_observation_freshness,
    extract_fact_temporal_evidence,
    map_field_to_category,
)
from backend.app.intelligence.schemas import (
    FreshnessCategory,
    FreshnessInformationCategory,
    FreshnessPolicy,
    TemporalPrecision,
)


@pytest.fixture()
def db():
    """In-memory SQLite session with full schema and foreign keys strictly enabled."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    orm.Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def client(db: Session):
    """FastAPI TestClient with overridden get_db dependency."""
    from backend.app.api.main import create_app
    from backend.app.db.base import get_db

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# =============================================================================
# HELPER FACTORIES
# =============================================================================

def _make_source(db: Session, url: str = "https://freshness.example.com", label: str = "Freshness Source") -> Source:
    src = Source(
        url=url,
        domain="freshness.example.com",
        kind="official_site",
        label=label,
        page_type="home",
        policy_status="allowed",
        robots_status="allowed",
    )
    db.add(src)
    db.flush()
    return src


def _make_org(db: Session, name: str = "Freshness Clinic", slug: str = "freshness-clinic") -> Organisation:
    org = Organisation(
        name=name,
        slug=slug,
        primary_domain="freshness.example.com",
        phone="03 9123 4567",
        address="100 Freshness St",
        suburb="Melbourne",
        postcode="3000",
        state="VIC",
        status="active",
    )
    db.add(org)
    db.flush()
    return org


def _link_source(db: Session, org_id: int, source_id: int, status: str = "confirmed") -> OrganisationSourceLink:
    link = OrganisationSourceLink(
        organisation_id=org_id,
        source_id=source_id,
        relationship_type="official_practice_site",
        verification_status=status,
        verified_by="test_reviewer",
        verified_at=datetime.now(UTC),
    )
    db.add(link)
    db.flush()
    return link


def _make_snapshot(db: Session, source: Source, text: str, fetched_at: datetime | None = None) -> PageSnapshot:
    snap = PageSnapshot(
        source_id=source.id,
        fetched_at=fetched_at or datetime.now(UTC),
        title="Snapshot Title",
        text=text,
        content_hash=uuid.uuid4().hex[:16],
        parser_version="v1",
        fixture=False,
    )
    db.add(snap)
    db.flush()
    return snap


def _make_observation(
    db: Session,
    source_id: int,
    outcome: str = "success_new_snapshot",
    snapshot_id: int | None = None,
    observed_at: datetime | None = None,
    seq: int = 1,
    parsing: str = "complete",
    extraction: str = "complete",
    error_details: str | None = None,
) -> SourceObservationORM:
    obs = SourceObservationORM(
        source_id=source_id,
        observed_at=observed_at or datetime.now(UTC),
        outcome=outcome,
        snapshot_id=snapshot_id,
        content_hash=uuid.uuid4().hex[:16] if snapshot_id else None,
        content_changed=outcome == "success_new_snapshot",
        parsing_completeness=parsing,
        extraction_completeness=extraction,
        observation_sequence=seq,
        error_details=error_details,
    )
    db.add(obs)
    db.flush()
    return obs


def _make_fact(
    db: Session,
    snapshot: PageSnapshot,
    org_id: int | None,
    field_key: str,
    raw_value: str,
    evidence_quote: str | None = None,
) -> Fact:
    ev_quote = evidence_quote or f"Telephone: {raw_value}"
    snap_text = snapshot.text or ""
    if ev_quote in snap_text:
        start_offset = snap_text.find(ev_quote)
        end_offset = start_offset + len(ev_quote)
    else:
        start_offset = 0
        end_offset = len(ev_quote)

    ev = Evidence(
        snapshot_id=snapshot.id,
        quote=ev_quote,
        start_offset=start_offset,
        end_offset=end_offset,
        captured_at=snapshot.fetched_at,
        match_algorithm_version="v1",
    )
    db.add(ev)
    db.flush()

    fact = Fact(
        snapshot_id=snapshot.id,
        organisation_id=org_id,
        field_key=field_key,
        scope_key="general",
        raw_value=raw_value,
        evidence_id=ev.id,
        via="deterministic",
        status="accepted",
    )
    db.add(fact)
    db.flush()
    return fact


# =============================================================================
# 1. OBSERVATION FRESHNESS TESTS
# =============================================================================

def test_fresh_source_observation(db: Session):
    """Source observed 5 days ago against 30-day threshold is classified as FRESH."""
    ref_time = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    src = _make_source(db)
    snap = _make_snapshot(db, src, "Content", fetched_at=ref_time - timedelta(days=5))
    _make_observation(db, src.id, "success_new_snapshot", snap.id, observed_at=ref_time - timedelta(days=5))

    res = evaluate_source_observation_freshness(db, src.id, reference_time=ref_time)
    assert res.classification == FreshnessCategory.FRESH
    assert res.observation_age_days == 5.0
    assert res.latest_attempt_failed is False


def test_aging_source_observation(db: Session):
    """Source observed 26 days ago (>= 80% of 30d threshold) is classified as AGING."""
    ref_time = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    src = _make_source(db)
    snap = _make_snapshot(db, src, "Content", fetched_at=ref_time - timedelta(days=26))
    _make_observation(db, src.id, "success_new_snapshot", snap.id, observed_at=ref_time - timedelta(days=26))

    res = evaluate_source_observation_freshness(db, src.id, reference_time=ref_time)
    assert res.classification == FreshnessCategory.AGING
    assert res.observation_age_days == 26.0


def test_stale_source_observation(db: Session):
    """Source observed 45 days ago (> 30d threshold) is classified as STALE."""
    ref_time = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    src = _make_source(db)
    snap = _make_snapshot(db, src, "Content", fetched_at=ref_time - timedelta(days=45))
    _make_observation(db, src.id, "success_new_snapshot", snap.id, observed_at=ref_time - timedelta(days=45))

    res = evaluate_source_observation_freshness(db, src.id, reference_time=ref_time)
    assert res.classification == FreshnessCategory.STALE
    assert res.observation_age_days == 45.0


def test_repeated_unchanged_observations_heartbeat(db: Session):
    """Unchanged observation heartbeat confirms continued observability without advancing content age."""
    ref_time = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    src = _make_source(db)

    # Content changed 60 days ago
    snap = _make_snapshot(db, src, "Oakwood Clinic Content", fetched_at=ref_time - timedelta(days=60))
    _make_observation(db, src.id, "success_new_snapshot", snap.id, observed_at=ref_time - timedelta(days=60), seq=1)

    # Re-observed 2 days ago as unchanged
    _make_observation(db, src.id, "success_unchanged", snap.id, observed_at=ref_time - timedelta(days=2), seq=2)

    # Observation freshness is FRESH (observed 2 days ago)
    obs_fresh = evaluate_source_observation_freshness(db, src.id, reference_time=ref_time)
    assert obs_fresh.classification == FreshnessCategory.FRESH
    assert obs_fresh.observation_age_days == 2.0
    assert obs_fresh.last_unchanged_observation_at is not None

    # Content freshness remains anchored to 60 days ago (when content actually changed)
    content_fresh = evaluate_source_content_freshness(db, src.id, reference_time=ref_time)
    assert content_fresh.content_age_days == 60.0
    assert content_fresh.unchanged_reobservations_count == 1


def test_failed_latest_attempt_preserves_valid_observation(db: Session):
    """Failed crawl preserves last successful crawl date while visibly flagging latest attempt failure."""
    ref_time = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    src = _make_source(db)

    # Valid observation at Day -10
    snap = _make_snapshot(db, src, "Content", fetched_at=ref_time - timedelta(days=10))
    _make_observation(db, src.id, "success_new_snapshot", snap.id, observed_at=ref_time - timedelta(days=10), seq=1)

    # Failed attempt at Day -1 (HTTP 500 error)
    _make_observation(
        db, src.id, "failed", None,
        observed_at=ref_time - timedelta(days=1), seq=2,
        parsing="failed", extraction="not_run", error_details="500 Internal Server Error",
    )

    res = evaluate_source_observation_freshness(db, src.id, reference_time=ref_time)
    # Currency is derived from Day -10 (age = 10 days -> FRESH)
    assert res.classification == FreshnessCategory.FRESH
    assert res.observation_age_days == 10.0
    assert res.latest_attempt_failed is True
    assert res.latest_attempt_status == "failed"
    assert "500 Internal Server Error" in res.rationale


def test_incomplete_latest_attempt_preserves_valid_observation(db: Session):
    """Incomplete crawl preserves last valid observation currency and reports incomplete status."""
    ref_time = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    src = _make_source(db)

    snap = _make_snapshot(db, src, "Content", fetched_at=ref_time - timedelta(days=15))
    _make_observation(db, src.id, "success_new_snapshot", snap.id, observed_at=ref_time - timedelta(days=15), seq=1)

    # Incomplete attempt at Day -2 (truncated page)
    _make_observation(
        db, src.id, "incomplete", None,
        observed_at=ref_time - timedelta(days=2), seq=2,
        parsing="partial", extraction="not_run", error_details="HTML truncated at 1024 bytes",
    )

    res = evaluate_source_observation_freshness(db, src.id, reference_time=ref_time)
    assert res.observation_age_days == 15.0
    assert res.latest_attempt_failed is True
    assert res.latest_attempt_status == "incomplete"


def test_observation_completeness_isolation(db: Session):
    """Partial parsing and failed extraction do NOT update complete parsing/extraction timestamps."""
    ref_time = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    src = _make_source(db)

    # Complete observation at Day -20
    snap_1 = _make_snapshot(db, src, "Full HTML", fetched_at=ref_time - timedelta(days=20))
    _make_observation(
        db, src.id, "success_new_snapshot", snap_1.id,
        observed_at=ref_time - timedelta(days=20), seq=1,
        parsing="complete", extraction="complete",
    )

    # Observation with partial parsing at Day -5
    snap_2 = _make_snapshot(db, src, "Partial HTML", fetched_at=ref_time - timedelta(days=5))
    _make_observation(
        db, src.id, "success_new_snapshot", snap_2.id,
        observed_at=ref_time - timedelta(days=5), seq=2,
        parsing="partial", extraction="failed",
    )

    res = evaluate_source_observation_freshness(db, src.id, reference_time=ref_time)
    # last_complete_parsing_at and last_complete_extraction_at remain at Day -20
    assert res.last_complete_parsing_at == snap_1.fetched_at
    assert res.last_complete_extraction_at == snap_1.fetched_at


# =============================================================================
# 2. FACT FRESHNESS & TEMPORAL EVIDENCE TESTS
# =============================================================================

def test_explicitly_dated_fact_freshness(db: Session):
    """Fact with explicit publication/effective date is classified according to fact age."""
    ref_time = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)
    src = _make_source(db)
    snap = _make_snapshot(
        db, src,
        "Oakwood Clinic Consultation fees effective from 1 January 2026 for all consultations.",
        fetched_at=ref_time - timedelta(days=2),
    )
    _make_observation(db, src.id, "success_new_snapshot", snap.id, observed_at=ref_time - timedelta(days=2))

    fact = _make_fact(
        db, snap, None, "consultation_fee", "$85.00",
        evidence_quote="Consultation fees effective from 1 January 2026 for all consultations.",
    )

    res = evaluate_fact_freshness(db, fact.id, reference_time=ref_time)
    assert res.has_explicit_temporal_evidence is True
    assert res.fact_date_type == "effective_date"
    assert res.explicit_fact_date == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    # Age from 2026-01-01 to 2026-02-01 12:00 is 31.5 days (vs 60d general threshold -> FRESH)
    assert res.fact_age_days == 31.5
    assert res.fact_freshness_classification == FreshnessCategory.FRESH


def test_undated_fact_is_unknown_not_stale(db: Session):
    """Fact without explicit temporal evidence is classified as UNKNOWN, never falsely marked STALE."""
    ref_time = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    src = _make_source(db)
    snap = _make_snapshot(db, src, "Main Telephone: (03) 9123 4567", fetched_at=ref_time - timedelta(days=2))
    _make_observation(db, src.id, "success_new_snapshot", snap.id, observed_at=ref_time - timedelta(days=2))

    fact = _make_fact(db, snap, None, "public_phone", "(03) 9123 4567", evidence_quote="Main Telephone: (03) 9123 4567")

    res = evaluate_fact_freshness(db, fact.id, reference_time=ref_time)
    assert res.has_explicit_temporal_evidence is False
    assert res.explicit_fact_date is None
    assert res.fact_freshness_classification == FreshnessCategory.UNKNOWN
    assert res.overall_classification == FreshnessCategory.UNKNOWN
    # Observation freshness itself is FRESH (crawled 2 days ago)
    assert res.observation_freshness.classification == FreshnessCategory.FRESH


def test_fresh_crawl_does_not_make_old_fact_fresh(db: Session):
    """VITAL PRINCIPLE: A fresh crawl today does NOT make a 5-year-old fact fresh."""
    ref_time = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    src = _make_source(db)

    # Page crawled TODAY (0.1 days ago)
    snap = _make_snapshot(
        db, src,
        "Notice published on 15 March 2021: New clinic policy established.",
        fetched_at=ref_time - timedelta(hours=2),
    )
    _make_observation(db, src.id, "success_new_snapshot", snap.id, observed_at=ref_time - timedelta(hours=2))

    fact = _make_fact(
        db, snap, None, "clinical_policy", "New clinic policy",
        evidence_quote="Notice published on 15 March 2021: New clinic policy established.",
    )

    res = evaluate_fact_freshness(db, fact.id, reference_time=ref_time)
    # Observation is FRESH (crawled today)
    assert res.observation_freshness.classification == FreshnessCategory.FRESH
    assert res.observation_freshness.observation_age_days < 1.0

    # Fact itself is STALE (published in 2021, age ~1812 days vs 60d threshold)
    assert res.has_explicit_temporal_evidence is True
    assert res.fact_freshness_classification == FreshnessCategory.STALE
    assert res.overall_classification == FreshnessCategory.STALE


def test_generic_page_update_metadata_rejected(db: Session):
    """Generic page footer/update metadata ('Page updated March 2026') is rejected and fact remains UNKNOWN."""
    ref_time = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)
    src = _make_source(db)
    snap = _make_snapshot(
        db, src,
        "Reception Telephone: (03) 9123 4567. Page updated March 2026.",
        fetched_at=ref_time - timedelta(days=1),
    )
    _make_observation(db, src.id, "success_new_snapshot", snap.id, observed_at=ref_time - timedelta(days=1))

    fact = _make_fact(
        db, snap, None, "public_phone", "(03) 9123 4567",
        evidence_quote="Reception Telephone: (03) 9123 4567. Page updated March 2026.",
    )

    res = evaluate_fact_freshness(db, fact.id, reference_time=ref_time)
    # Generic page update was rejected; fact is undated -> UNKNOWN
    assert res.has_explicit_temporal_evidence is False
    assert res.fact_freshness_classification == FreshnessCategory.UNKNOWN


# =============================================================================
# 3. INFORMATION CATEGORIES & POLICY THRESHOLDS
# =============================================================================

def test_category_specific_thresholds():
    """Verify field key mapping to distinct categories and threshold lookups."""
    assert map_field_to_category("practice_name") == FreshnessInformationCategory.IDENTITY
    assert map_field_to_category("address_street") == FreshnessInformationCategory.IDENTITY
    assert map_field_to_category("public_phone") == FreshnessInformationCategory.OPERATIONAL
    assert map_field_to_category("service_names") == FreshnessInformationCategory.SERVICES
    assert map_field_to_category("opening_hours") == FreshnessInformationCategory.HOURS
    assert map_field_to_category("parking_instructions") == FreshnessInformationCategory.GENERAL

    pol = DEFAULT_FRESHNESS_POLICY
    assert pol.get_threshold("identity") == 90
    assert pol.get_threshold("operational") == 30
    assert pol.get_threshold("services") == 60
    assert pol.get_threshold("hours") == 30
    assert pol.get_threshold("general") == 60


def test_source_specific_policy_overrides(db: Session):
    """Policy allows source-specific threshold overrides."""
    pol = FreshnessPolicy(
        policy_id="custom_v1",
        category_thresholds_days={"operational": 30, "hours": 30},
        source_overrides={
            100: {"hours": 7},  # Source 100 has strict 7-day hours threshold
        },
    )
    assert pol.get_threshold("hours", source_id=100) == 7
    assert pol.get_threshold("hours", source_id=200) == 30


def test_aging_versus_stale_boundaries():
    """Verify deterministic transition boundaries across Fresh, Aging, and Stale."""
    pol = DEFAULT_FRESHNESS_POLICY  # operational threshold 30d, ratio 0.8 -> aging is 24-30d
    # 0 to 23.9d -> FRESH
    assert pol.classify_age(10.0, 30) == FreshnessCategory.FRESH
    assert pol.classify_age(23.9, 30) == FreshnessCategory.FRESH
    # 24.0d to 30.0d -> AGING
    assert pol.classify_age(24.0, 30) == FreshnessCategory.AGING
    assert pol.classify_age(29.9, 30) == FreshnessCategory.AGING
    assert pol.classify_age(30.0, 30) == FreshnessCategory.AGING
    # > 30.0d -> STALE
    assert pol.classify_age(30.1, 30) == FreshnessCategory.STALE
    assert pol.classify_age(60.0, 30) == FreshnessCategory.STALE


# =============================================================================
# 4. EVIDENCE VERIFICATION VS FACT CURRENCY
# =============================================================================

def test_evidence_verification_vs_fact_currency(db: Session):
    """Evidence quote verification integrity is completely separate from fact freshness."""
    ref_time = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    src = _make_source(db)
    snap = _make_snapshot(
        db, src,
        "Dr Jane Doe joined clinic. Notice published 10 January 2020.",
        fetched_at=ref_time - timedelta(days=5),
    )
    _make_observation(db, src.id, "success_new_snapshot", snap.id, observed_at=ref_time - timedelta(days=5))

    fact = _make_fact(
        db, snap, None, "practitioners", "Dr Jane Doe",
        evidence_quote="Dr Jane Doe joined clinic. Notice published 10 January 2020.",
    )

    res = evaluate_fact_freshness(db, fact.id, reference_time=ref_time)
    # Evidence verification is successful (quote is verbatim in text)
    assert res.evidence_freshness.is_verified is True
    # Fact itself is STALE (published in 2020)
    assert res.fact_freshness_classification == FreshnessCategory.STALE
    # Observation is FRESH (crawled 5 days ago)
    assert res.observation_freshness.classification == FreshnessCategory.FRESH


# =============================================================================
# 5. UNOBSERVABLE & MULTI-ORGANISATION TESTS
# =============================================================================

def test_unobservable_source_handling(db: Session):
    """Source with zero observation attempts is classified as UNOBSERVABLE."""
    src = _make_source(db)
    res = evaluate_source_observation_freshness(db, src.id)
    assert res.classification == FreshnessCategory.UNOBSERVABLE
    assert res.observation_age_days is None


def test_organisation_freshness_summary_and_isolation(db: Session):
    """Organisation summary aggregates all linked sources and facts without cross-org leakage."""
    ref_time = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

    # Org A with confirmed source A
    org_a = _make_org(db, "Org A", "org-a")
    src_a = _make_source(db, "https://orga.example.com", "Source A")
    _link_source(db, org_a.id, src_a.id, "confirmed")

    snap_a = _make_snapshot(db, src_a, "Phone: (03) 9123 4567 Fees effective 1 February 2026", fetched_at=ref_time - timedelta(days=3))
    _make_observation(db, src_a.id, "success_new_snapshot", snap_a.id, observed_at=ref_time - timedelta(days=3))

    # Fact 1: Undated (unknown)
    _make_fact(db, snap_a, org_a.id, "public_phone", "(03) 9123 4567", evidence_quote="Phone: (03) 9123 4567")
    # Fact 2: Dated 2026-02-01 (fresh)
    _make_fact(db, snap_a, org_a.id, "consultation_fee", "$90", evidence_quote="Fees effective 1 February 2026")

    # Org B with unobservable source B
    org_b = _make_org(db, "Org B", "org-b")
    src_b = _make_source(db, "https://orgb.example.com", "Source B")
    _link_source(db, org_b.id, src_b.id, "confirmed")

    summary_a = evaluate_organisation_freshness_summary(db, org_a.id, reference_time=ref_time)
    assert summary_a.organisation_id == org_a.id
    assert summary_a.confirmed_source_count == 1
    assert summary_a.total_facts_count == 2
    assert summary_a.fresh_facts_count == 1
    assert summary_a.unknown_facts_count == 1
    assert summary_a.unobservable_sources_count == 0

    summary_b = evaluate_organisation_freshness_summary(db, org_b.id, reference_time=ref_time)
    assert summary_b.confirmed_source_count == 1
    assert summary_b.unobservable_sources_count == 1
    assert summary_b.total_facts_count == 0


def test_zero_change_event_mutation_during_freshness(db: Session):
    """Freshness evaluation is strictly read-only and never creates change events or alters facts."""
    src = _make_source(db)
    snap = _make_snapshot(db, src, "Phone: 111", fetched_at=datetime.now(UTC))
    _make_observation(db, src.id, "success_new_snapshot", snap.id)
    fact = _make_fact(db, snap, None, "public_phone", "111")
    db.commit()

    initial_change_events = db.scalar(select(func.count(ChangeEventORM.id)))

    # Run evaluations
    evaluate_source_observation_freshness(db, src.id)
    evaluate_source_content_freshness(db, src.id)
    evaluate_fact_freshness(db, fact.id)
    db.commit()

    final_change_events = db.scalar(select(func.count(ChangeEventORM.id)))
    assert initial_change_events == final_change_events


# =============================================================================
# 6. REST API ENDPOINT TESTS
# =============================================================================

def test_api_freshness_policy_endpoint(client: TestClient):
    """GET /api/freshness/policy returns active policy configuration."""
    res = client.get("/api/freshness/policy")
    assert res.status_code == 200
    data = res.json()
    assert data["policy_id"] == "ps_freshness_v1"
    assert "identity" in data["category_thresholds_days"]
    assert data["category_thresholds_days"]["identity"] == 90
    assert data["category_thresholds_days"]["operational"] == 30


def test_api_source_freshness_endpoint(client: TestClient, db: Session):
    """GET /api/freshness/source/{source_id} returns observation freshness."""
    src = _make_source(db, "https://api-src.example.com", "API Source")
    snap = _make_snapshot(db, src, "API text")
    _make_observation(db, src.id, "success_new_snapshot", snap.id)
    db.commit()

    res = client.get(f"/api/freshness/source/{src.id}")
    assert res.status_code == 200
    data = res.json()
    assert data["source_id"] == src.id
    assert data["classification"] == "fresh"
    assert data["policy_id"] == "ps_freshness_v1"

    # 404 on missing source
    res_404 = client.get("/api/freshness/source/999999")
    assert res_404.status_code == 404


def test_api_fact_freshness_endpoint(client: TestClient, db: Session):
    """GET /api/freshness/fact/{fact_id} returns complete 4D freshness detail."""
    src = _make_source(db)
    snap = _make_snapshot(db, src, "Fees effective from 1 January 2026: $100")
    _make_observation(db, src.id, "success_new_snapshot", snap.id)
    fact = _make_fact(db, snap, None, "consultation_fee", "$100", evidence_quote="Fees effective from 1 January 2026: $100")
    db.commit()

    res = client.get(f"/api/freshness/fact/{fact.id}")
    assert res.status_code == 200
    data = res.json()
    assert data["fact_id"] == fact.id
    assert data["has_explicit_temporal_evidence"] is True
    assert "observation_freshness" in data
    assert "content_freshness" in data
    assert "evidence_freshness" in data

    # 404 on missing fact
    res_404 = client.get("/api/freshness/fact/999999")
    assert res_404.status_code == 404


def test_api_organisation_freshness_endpoint(client: TestClient, db: Session):
    """GET /api/freshness/organisation/{org_id} returns aggregated summary."""
    org = _make_org(db, "API Org", "api-org")
    src = _make_source(db)
    _link_source(db, org.id, src.id, "confirmed")
    snap = _make_snapshot(db, src, "Phone: (03) 9123 4567")
    _make_observation(db, src.id, "success_new_snapshot", snap.id)
    _make_fact(db, snap, org.id, "public_phone", "(03) 9123 4567")
    db.commit()

    res = client.get(f"/api/freshness/organisation/{org.id}")
    assert res.status_code == 200
    data = res.json()
    assert data["organisation_id"] == org.id
    assert data["confirmed_source_count"] == 1
    assert data["total_facts_count"] == 1

    # 404 on missing organisation
    res_404 = client.get("/api/freshness/organisation/999999")
    assert res_404.status_code == 404


def test_api_freshness_evaluate_post_endpoint(client: TestClient, db: Session):
    """POST /api/freshness/evaluate allows dynamic reference time and custom thresholds."""
    src = _make_source(db)
    snap = _make_snapshot(db, src, "Phone: 123", fetched_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    _make_observation(db, src.id, "success_new_snapshot", snap.id, observed_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    db.commit()

    # Evaluate at 2026-01-05 (4 days later -> FRESH)
    payload_fresh = {
        "source_id": src.id,
        "reference_time": "2026-01-05T12:00:00Z",
    }
    res_fresh = client.post("/api/freshness/evaluate", json=payload_fresh)
    assert res_fresh.status_code == 200
    assert res_fresh.json()["data"]["classification"] == "fresh"

    # Evaluate at 2026-03-01 (60 days later -> STALE)
    payload_stale = {
        "source_id": src.id,
        "reference_time": "2026-03-01T12:00:00Z",
    }
    res_stale = client.post("/api/freshness/evaluate", json=payload_stale)
    assert res_stale.status_code == 200
    assert res_stale.json()["data"]["classification"] == "stale"

    # Evaluate with custom threshold override (threshold=100 days -> FRESH at Day 60)
    payload_override = {
        "source_id": src.id,
        "reference_time": "2026-03-01T12:00:00Z",
        "category_thresholds_override": {"operational": 100},
    }
    res_override = client.post("/api/freshness/evaluate", json=payload_override)
    assert res_override.status_code == 200
    assert res_override.json()["data"]["classification"] == "fresh"

    # 400 when no entity supplied
    res_bad = client.post("/api/freshness/evaluate", json={})
    assert res_bad.status_code == 400


# =============================================================================
# 8. DATE PRECISION & SEMANTIC INTEGRITY TESTS (M3.4 GATE)
# =============================================================================

def test_temporal_date_precision_year_crossing_boundary(db: Session):
    """Effective from 2026 evaluated near the end of 2026 crosses policy boundary -> classified as UNKNOWN."""
    ref_time = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    src = _make_source(db)
    snap = _make_snapshot(
        db, src,
        "Oakwood Clinic Consultation fees effective from 2026 for all consultations.",
        fetched_at=ref_time - timedelta(days=2),
    )
    _make_observation(db, src.id, "success_new_snapshot", snap.id, observed_at=ref_time - timedelta(days=2))

    fact = _make_fact(
        db, snap, None, "consultation_fee", "$85.00",
        evidence_quote="Consultation fees effective from 2026 for all consultations.",
    )

    res = evaluate_fact_freshness(db, fact.id, reference_time=ref_time)
    assert res.has_explicit_temporal_evidence is True
    assert res.date_precision == TemporalPrecision.YEAR
    assert res.date_range_start == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    assert res.date_range_end == datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)
    # Range spans fresh (at 2026-12-31) and stale (at 2026-01-01 > 60d threshold) -> UNKNOWN
    assert res.fact_freshness_classification == FreshnessCategory.UNKNOWN
    assert res.overall_classification == FreshnessCategory.UNKNOWN


def test_temporal_date_precision_year_all_stale(db: Session):
    """Effective from 2020 evaluated in 2026: all dates in range exceed threshold -> STALE."""
    ref_time = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    src = _make_source(db)
    snap = _make_snapshot(
        db, src,
        "Oakwood Clinic Policy effective from 2020.",
        fetched_at=ref_time - timedelta(days=2),
    )
    _make_observation(db, src.id, "success_new_snapshot", snap.id, observed_at=ref_time - timedelta(days=2))

    fact = _make_fact(
        db, snap, None, "consultation_fee", "$65.00",
        evidence_quote="Policy effective from 2020.",
    )

    res = evaluate_fact_freshness(db, fact.id, reference_time=ref_time)
    assert res.has_explicit_temporal_evidence is True
    assert res.date_precision == TemporalPrecision.YEAR
    assert res.date_range_start == datetime(2020, 1, 1, 0, 0, tzinfo=UTC)
    assert res.date_range_end == datetime(2020, 12, 31, 23, 59, 59, tzinfo=UTC)
    # Both endpoints exceed 60d threshold by years -> STALE
    assert res.fact_freshness_classification == FreshnessCategory.STALE
    assert res.overall_classification == FreshnessCategory.STALE


def test_temporal_date_precision_month_fresh(db: Session):
    """Updated April 2026 evaluated in early May 2026: all dates in month are FRESH against 60d threshold."""
    ref_time = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)
    src = _make_source(db)
    snap = _make_snapshot(
        db, src,
        "Oakwood Clinic fees updated April 2026 for all services.",
        fetched_at=ref_time - timedelta(days=1),
    )
    _make_observation(db, src.id, "success_new_snapshot", snap.id, observed_at=ref_time - timedelta(days=1))

    fact = _make_fact(
        db, snap, None, "consultation_fee", "$95.00",
        evidence_quote="fees updated April 2026 for all services.",
    )

    res = evaluate_fact_freshness(db, fact.id, reference_time=ref_time)
    assert res.has_explicit_temporal_evidence is True
    assert res.date_precision == TemporalPrecision.MONTH
    assert res.date_range_start == datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
    assert res.date_range_end == datetime(2026, 4, 30, 23, 59, 59, tzinfo=UTC)
    # At 2026-04-30 age is ~5d (FRESH); at 2026-04-01 age is ~34d (FRESH, < 48d aging threshold)
    assert res.fact_freshness_classification == FreshnessCategory.FRESH
    assert res.overall_classification == FreshnessCategory.FRESH


def test_temporal_date_precision_month_crossing_boundary(db: Session):
    """Hours updated April 2026 evaluated in late May against 30d threshold crosses boundary -> UNKNOWN."""
    ref_time = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    src = _make_source(db)
    snap = _make_snapshot(
        db, src,
        "Oakwood Clinic opening hours updated April 2026: 8am to 6pm.",
        fetched_at=ref_time - timedelta(days=1),
    )
    _make_observation(db, src.id, "success_new_snapshot", snap.id, observed_at=ref_time - timedelta(days=1))

    fact = _make_fact(
        db, snap, None, "opening_hours", "8am to 6pm",
        evidence_quote="opening hours updated April 2026: 8am to 6pm.",
    )

    res = evaluate_fact_freshness(db, fact.id, reference_time=ref_time)
    assert res.has_explicit_temporal_evidence is True
    assert res.date_precision == TemporalPrecision.MONTH
    # At 2026-04-30 age is 20d (FRESH, < 24d); at 2026-04-01 age is 49d (STALE, > 30d) -> UNKNOWN
    assert res.fact_freshness_classification == FreshnessCategory.UNKNOWN
    assert res.overall_classification == FreshnessCategory.UNKNOWN


def test_temporal_date_precision_day_exact(db: Session):
    """Updated April 25, 2026 provides exact day precision."""
    ref_time = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    src = _make_source(db)
    snap = _make_snapshot(
        db, src,
        "Fees updated April 25, 2026: Standard consultation $80.",
        fetched_at=ref_time - timedelta(days=1),
    )
    _make_observation(db, src.id, "success_new_snapshot", snap.id, observed_at=ref_time - timedelta(days=1))

    fact = _make_fact(
        db, snap, None, "consultation_fee", "$80.00",
        evidence_quote="Fees updated April 25, 2026: Standard consultation $80.",
    )

    res = evaluate_fact_freshness(db, fact.id, reference_time=ref_time)
    assert res.has_explicit_temporal_evidence is True
    assert res.date_precision == TemporalPrecision.DAY
    assert res.explicit_fact_date == datetime(2026, 4, 25, 0, 0, tzinfo=UTC)
    assert res.fact_age_days == 15.5
    assert res.fact_freshness_classification == FreshnessCategory.FRESH


def test_temporal_date_precision_missing_year_is_unknown(db: Session):
    """Updated April 25 with no year: precision is UNKNOWN and year is NEVER guessed from crawl/DB timestamps."""
    ref_time = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    src = _make_source(db)
    snap = _make_snapshot(
        db, src,
        "Opening hours updated April 25: Monday to Friday 9am-5pm.",
        fetched_at=ref_time - timedelta(days=1),
    )
    _make_observation(db, src.id, "success_new_snapshot", snap.id, observed_at=ref_time - timedelta(days=1))

    fact = _make_fact(
        db, snap, None, "opening_hours", "Monday to Friday 9am-5pm",
        evidence_quote="hours updated April 25: Monday to Friday 9am-5pm.",
    )

    res = evaluate_fact_freshness(db, fact.id, reference_time=ref_time)
    assert res.has_explicit_temporal_evidence is True
    assert res.date_precision == TemporalPrecision.UNKNOWN
    assert res.date_range_start is None
    assert res.date_range_end is None
    assert res.fact_freshness_classification == FreshnessCategory.UNKNOWN
    assert res.overall_classification == FreshnessCategory.UNKNOWN


def test_content_freshness_snapshots_sequence_and_unchanged_heartbeats(db: Session):
    """Test sequence of two snapshots separated by multiple unchanged observations.

    Unchanged heartbeats confirm continued observability without advancing the content observation timestamp.
    """
    ref_time = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)
    src = _make_source(db)

    # Snapshot 1 on 2026-01-01
    snap_1_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    snap_1 = _make_snapshot(db, src, "Content Version 1", fetched_at=snap_1_time)
    _make_observation(db, src.id, "success_new_snapshot", snap_1.id, observed_at=snap_1_time, seq=1)

    # 3 unchanged observations on Day 5, Day 10, Day 15
    for seq, days in enumerate([5, 10, 15], start=2):
        _make_observation(db, src.id, "success_unchanged", snap_1.id, observed_at=datetime(2026, 1, days, 12, 0, tzinfo=UTC), seq=seq)

    # Evaluate content freshness prior to snapshot 2: current version first observed at Day 1
    res_v1 = evaluate_source_content_freshness(db, src.id, reference_time=datetime(2026, 1, 18, 12, 0, tzinfo=UTC))
    assert res_v1.current_version_first_observed_at == snap_1_time
    assert res_v1.snapshot_count == 1
    assert res_v1.unchanged_reobservations_count == 3
    assert res_v1.previous_version_observed_at is None

    # Snapshot 2 on 2026-01-20 (new content hash)
    snap_2_time = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
    snap_2 = _make_snapshot(db, src, "Content Version 2 (Modified)", fetched_at=snap_2_time)
    _make_observation(db, src.id, "success_new_snapshot", snap_2.id, observed_at=snap_2_time, seq=5)

    # 2 more unchanged observations on Day 25 and Day 30
    _make_observation(db, src.id, "success_unchanged", snap_2.id, observed_at=datetime(2026, 1, 25, 12, 0, tzinfo=UTC), seq=6)
    _make_observation(db, src.id, "success_unchanged", snap_2.id, observed_at=datetime(2026, 1, 30, 12, 0, tzinfo=UTC), seq=7)

    # Evaluate at 2026-02-01:
    res_v2 = evaluate_source_content_freshness(db, src.id, reference_time=ref_time)
    # First observation of current version is snap_2_time (2026-01-20), NOT the unchanged observation times (Day 25/30)!
    assert res_v2.current_version_first_observed_at == snap_2_time
    assert res_v2.content_observation_age_days == 12.0
    # Previous version is snap_1_time (2026-01-01)
    assert res_v2.previous_version_observed_at == snap_1_time
    assert res_v2.previous_version_content_hash == snap_1.content_hash
    assert res_v2.latest_content_hash == snap_2.content_hash
    assert res_v2.snapshot_count == 2
    assert res_v2.unchanged_reobservations_count == 5
    # Rationale explicitly notes observation timing vs publisher modification timestamp
    assert "reflects PracticeSignal crawler observation timing" in res_v2.rationale


def test_evidence_capture_vs_verification_time(db: Session):
    """Distinguish captured_at (original evidence capture) from checked_at (runtime evaluation time).

    An old quotation retains full verification integrity against the immutable snapshot.
    """
    ref_time = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    capture_time = datetime(2020, 1, 15, 12, 0, tzinfo=UTC)
    src = _make_source(db)
    snap = _make_snapshot(
        db, src,
        "Dr Alice Smith joined as Principal GP on 15 January 2020.",
        fetched_at=capture_time,
    )
    _make_observation(db, src.id, "success_new_snapshot", snap.id, observed_at=capture_time)

    fact = _make_fact(
        db, snap, None, "practitioners", "Dr Alice Smith",
        evidence_quote="Dr Alice Smith joined as Principal GP on 15 January 2020.",
    )

    res = evaluate_fact_freshness(db, fact.id, reference_time=ref_time)
    ev_fresh = res.evidence_freshness
    # captured_at is 2020-01-15
    assert ev_fresh.captured_at == capture_time
    # checked_at is ref_time (2026-03-01)
    assert ev_fresh.checked_at == ref_time
    assert ev_fresh.evaluated_at == ref_time
    # Valid quotation remains integrity-verified despite being 6 years old
    assert ev_fresh.is_verified is True
    assert ev_fresh.threshold_governance == "immutable_snapshot_integrity"
    assert "re-verified verbatim at stored offsets" in ev_fresh.rationale
    assert "does not degrade simply with age" in ev_fresh.rationale


def test_policy_threshold_selection_and_governance(db: Session):
    """Every evaluation exposes its policy_id, exact threshold applied, and governance category."""
    ref_time = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    src = _make_source(db)
    snap = _make_snapshot(db, src, "Phone: (03) 9123 4567", fetched_at=ref_time - timedelta(days=5))
    _make_observation(db, src.id, "success_new_snapshot", snap.id, observed_at=ref_time - timedelta(days=5))

    fact_phone = _make_fact(db, snap, None, "public_phone", "(03) 9123 4567")
    fact_name = _make_fact(db, snap, None, "practice_name", "Oakwood Clinic")

    # Policy with explicit source observation threshold override
    custom_policy = FreshnessPolicy(
        policy_id="test_governance_v1",
        source_observation_threshold_days=14,  # default 14d
        source_content_threshold_days=45,      # default 45d
        category_thresholds_days={
            "identity": 120,
            "operational": 21,
            "services": 45,
            "hours": 14,
            "general": 60,
        },
        source_observation_overrides={src.id: 10},  # override src to 10d
    )

    # 1. Source observation freshness: governed by source_observation_threshold
    obs_fresh = evaluate_source_observation_freshness(db, src.id, policy=custom_policy, reference_time=ref_time)
    assert obs_fresh.policy_id == "test_governance_v1"
    assert obs_fresh.threshold_days == 10  # from source_observation_overrides
    assert obs_fresh.threshold_governance == "source_observation"

    # 2. Source content freshness: governed by source_content_threshold
    content_fresh = evaluate_source_content_freshness(db, src.id, policy=custom_policy, reference_time=ref_time)
    assert content_fresh.policy_id == "test_governance_v1"
    assert content_fresh.threshold_days == 45  # from source_content_threshold_days
    assert content_fresh.threshold_governance == "source_content"

    # 3. Fact freshness (operational: 21d)
    fact_phone_fresh = evaluate_fact_freshness(db, fact_phone.id, policy=custom_policy, reference_time=ref_time)
    assert fact_phone_fresh.policy_threshold_days == 21
    assert fact_phone_fresh.threshold_governance == "fact_category:operational"

    # 4. Fact freshness (identity: 120d)
    fact_name_fresh = evaluate_fact_freshness(db, fact_name.id, policy=custom_policy, reference_time=ref_time)
    assert fact_name_fresh.policy_threshold_days == 120
    assert fact_name_fresh.threshold_governance == "fact_category:identity"


def test_api_source_content_freshness_endpoint(client: TestClient, db: Session):
    """GET /api/freshness/source/{source_id}/content exposes current and previous version observation details."""
    ref_time = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)
    src = _make_source(db)
    snap_1 = _make_snapshot(db, src, "Content V1", fetched_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    _make_observation(db, src.id, "success_new_snapshot", snap_1.id, observed_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC), seq=1)
    _make_observation(db, src.id, "success_unchanged", snap_1.id, observed_at=datetime(2026, 1, 10, 12, 0, tzinfo=UTC), seq=2)

    snap_2 = _make_snapshot(db, src, "Content V2", fetched_at=datetime(2026, 1, 15, 12, 0, tzinfo=UTC))
    _make_observation(db, src.id, "success_new_snapshot", snap_2.id, observed_at=datetime(2026, 1, 15, 12, 0, tzinfo=UTC), seq=3)
    db.commit()

    res = client.get(f"/api/freshness/source/{src.id}/content")
    assert res.status_code == 200
    data = res.json()
    assert data["source_id"] == src.id
    assert data["snapshot_count"] == 2
    assert data["unchanged_reobservations_count"] == 1
    assert "2026-01-15T12:00:00" in data["current_version_first_observed_at"]
    assert "2026-01-01T12:00:00" in data["previous_version_observed_at"]
    assert data["threshold_governance"] == "source_content"
