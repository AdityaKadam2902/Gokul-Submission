"""M3.1 comprehensive verification & remediation test suite (ARCHITECTURE §6.1, D39, D40, D47).

Covers:
1. Database migration M3_001: fresh DB, existing M0-M2 DB, repeated startup, schema rollback,
   backup restoration, foreign key validation, and data preservation.
2. Observation heartbeat engine: all 4 outcomes (success_new_snapshot, success_unchanged,
   incomplete, failed), independent completeness states, error transparent logging, zero duplicate snapshots.
3. Concurrency-safe sequence allocation: multi-threaded SQLite test proving atomic savepoint
   allocation under concurrent writes without sequence collisions or lost observations.
4. Complete 3-tier entity resolution matrix: all 4 positive rules + 7 negative rejection gates,
   separation of metric scores from probability claims, zero automatic merges.
5. Fact ownership and review reason persistence: single-practice direct mapping, multi-branch
   heading resolution, corporate unassigned fallback, missing heading context, conflicting branch
   evidence, parent-domain-only isolation, non-overwrite, and invalid org ID rejection.
6. Source-link management & API: deliberate operator verification lifecycle (suggested -> confirmed | rejected),
   mandatory verified_by operator identity enforcement, CRUD, candidate querying, and absence of merge endpoints.
"""

from __future__ import annotations

import concurrent.futures
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.exceptions import FixtureError, ValidationError_
from backend.app.db import orm
from backend.app.db.migrations import (
    inspect_m3_001_schema,
    migrate_m3_001_organisations,
    rollback_m3_001_organisations,
    rollback_migration,
)
from backend.app.db.orm import (
    Evidence,
    Fact,
    Organisation,
    OrganisationMatchCandidate,
    OrganisationSourceLink,
    PageSnapshot,
    Source,
    SourceObservation,
)
from backend.app.ingest.pipeline import run_ingestion
from backend.app.intelligence.entity_resolver import (
    MatchEvaluation,
    evaluate_source_and_org,
    propose_match_candidate,
)
from backend.app.intelligence.fact_ownership import (
    assign_fact_ownership,
    assign_snapshot_facts_ownership,
)
from backend.app.intelligence.observation_tracker import (
    get_latest_observation,
    list_observations,
    record_observation,
)
from backend.app.intelligence.schemas import (
    ExtractionCompleteness,
    MatchTier,
    ObservationOutcome,
    ParsingCompleteness,
    ReviewStatus,
)
from backend.app.models import (
    OrganisationCreate,
    OrganisationLinkCreate,
    OrganisationLinkUpdate,
    SourceCreate,
)
from backend.app.sources import registry


@pytest.fixture()
def db():
    """In-memory SQLite session with full schema including M3_001 tables."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    orm.Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def client(m1_client):
    return m1_client


# =============================================================================
# 1. DATABASE MIGRATION M3_001 TESTS
# =============================================================================

def test_m3_001_migration_lifecycle_and_backup_recovery():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_migration.db"
        engine = create_engine(f"sqlite:///{db_path}")

        # 1. Initialize M0-M2 tables only (simulate pre-M3 database)
        tables_to_create = [
            orm.Base.metadata.tables["sources"],
            orm.Base.metadata.tables["page_snapshots"],
            orm.Base.metadata.tables["prompt_runs"],
            orm.Base.metadata.tables["evidence"],
            orm.Base.metadata.tables["facts"],
        ]
        orm.Base.metadata.create_all(bind=engine, tables=tables_to_create)

        # Insert pre-existing source and fact to verify data preservation
        with Session(engine) as session:
            src = Source(
                kind="fixture",
                label="Baseline Source",
                fixture_id="F1",
                page_type="home",
                policy_status="manual_fixture_pending_policy_check",
                robots_status="fixture_exempt",
                intended_fields=["organisation_name"],
            )
            session.add(src)
            session.flush()

            snap = PageSnapshot(
                source_id=src.id,
                title="Baseline Page",
                text="Welcome to Clinic.",
                content_hash="abc" * 21 + "a",
                parser_version="1.0.0",
                fixture=True,
                headings=[],
            )
            session.add(snap)
            session.flush()

            fact = Fact(
                snapshot_id=snap.id,
                field_key="organisation_name",
                raw_value="Clinic",
                normalized_value="Clinic",
                via="deterministic",
                status="candidate",
            )
            session.add(fact)
            session.commit()

        engine.dispose()

        # 2. Verify schema before migration
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        pre_schema = inspect_m3_001_schema(cur)
        conn.close()
        assert pre_schema["organisations"] is False
        assert pre_schema["source_observations"] is False

        # 3. Run migration M3_001
        res1 = migrate_m3_001_organisations(db_path)
        assert res1["status"] == "success"
        assert len(res1["tables_created"]) == 4
        backup_path = res1["backup_path"]
        assert Path(backup_path).is_file()

        # 4. Verify schema after migration
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        post_schema = inspect_m3_001_schema(cur)
        conn.close()
        assert post_schema["organisations"] is True
        assert post_schema["organisation_source_links"] is True
        assert post_schema["source_observations"] is True
        assert post_schema["organisation_match_candidates"] is True

        # 5. Idempotency: re-running migration should succeed without changes
        res2 = migrate_m3_001_organisations(db_path)
        assert res2["status"] == "up_to_date"

        # 6. Verify pre-existing data is preserved completely
        engine2 = create_engine(f"sqlite:///{db_path}")
        with Session(engine2) as session:
            saved_src = session.scalar(select(Source).where(Source.label == "Baseline Source"))
            assert saved_src is not None
            assert len(saved_src.snapshots) == 1
            saved_fact = session.scalar(select(Fact).where(Fact.raw_value == "Clinic"))
            assert saved_fact is not None
            assert saved_fact.organisation_id is None
        engine2.dispose()

        # 7. Test schema rollback (dropping M3.1 tables)
        rollback_res = rollback_m3_001_organisations(db_path)
        assert rollback_res["status"] == "success"

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        rb_schema = inspect_m3_001_schema(cur)
        conn.close()
        assert rb_schema["organisations"] is False

        # 8. Test full file recovery from backup using rollback_migration
        rollback_migration(db_path, backup_path)
        engine3 = create_engine(f"sqlite:///{db_path}")
        with Session(engine3) as session:
            assert session.scalar(select(Source).where(Source.label == "Baseline Source")) is not None
        engine3.dispose()


# =============================================================================
# 2. OBSERVATION HEARTBEAT ENGINE TESTS (D40)
# =============================================================================

def test_observation_heartbeat_all_four_outcomes(db: Session):
    source = registry.create_source(
        db,
        SourceCreate(
            kind="fixture",
            page_type="home",
            fixture_id="F1",
            label="Heartbeat Test Source",
            intended_fields=["organisation_name"],
        ),
    )
    db.commit()

    # 1. Outcome: success_new_snapshot
    outcome1 = run_ingestion(db, source)
    db.commit()
    assert outcome1.unchanged is False

    obs1 = get_latest_observation(db, source.id)
    assert obs1 is not None
    assert obs1.outcome == ObservationOutcome.SUCCESS_NEW_SNAPSHOT.value
    assert obs1.content_changed is True
    assert obs1.observation_sequence == 1
    assert obs1.snapshot_id == outcome1.snapshot_id
    assert obs1.parsing_completeness == ParsingCompleteness.COMPLETE.value
    assert obs1.extraction_completeness == ExtractionCompleteness.NOT_RUN.value

    # 2. Outcome: success_unchanged (short-circuit; zero duplicate snapshots)
    outcome2 = run_ingestion(db, source)
    db.commit()
    assert outcome2.unchanged is True
    assert outcome2.snapshot_id == outcome1.snapshot_id

    obs2 = get_latest_observation(db, source.id)
    assert obs2 is not None
    assert obs2.outcome == ObservationOutcome.SUCCESS_UNCHANGED.value
    assert obs2.content_changed is False
    assert obs2.observation_sequence == 2
    assert obs2.snapshot_id == outcome1.snapshot_id

    # Verify immutable snapshot count: strictly 1 snapshot persisted
    snap_count = len(list(db.scalars(select(PageSnapshot).where(PageSnapshot.source_id == source.id)).all()))
    assert snap_count == 1

    # 3. Outcome: incomplete observation (e.g. truncated parse or partial capture)
    obs3 = record_observation(
        session=db,
        source_id=source.id,
        outcome=ObservationOutcome.INCOMPLETE,
        content_changed=False,
        headings_count=1,
        character_count=50,
        error_details="parser_warning_partial_truncation",
    )
    db.commit()
    assert obs3.outcome == ObservationOutcome.INCOMPLETE.value
    assert obs3.content_changed is False
    assert obs3.parsing_completeness == ParsingCompleteness.PARTIAL.value
    assert obs3.extraction_completeness == ExtractionCompleteness.NOT_RUN.value
    assert obs3.observation_sequence == 3

    # 4. Outcome: failed observation (ingestion failure)
    source.fixture_id = "F_NONEXISTENT"
    db.commit()

    with pytest.raises(FixtureError):
        run_ingestion(db, source)

    obs4 = get_latest_observation(db, source.id)
    assert obs4 is not None
    assert obs4.outcome == ObservationOutcome.FAILED.value
    assert obs4.content_changed is False
    assert obs4.observation_sequence == 4
    assert obs4.snapshot_id is None
    assert obs4.parsing_completeness == ParsingCompleteness.FAILED.value
    assert "unknown fixture id" in (obs4.error_details or "")

    # Chronological history check
    all_obs = list_observations(db, source.id)
    assert [o.observation_sequence for o in all_obs] == [1, 2, 3, 4]


def test_observation_independent_completeness_matrix(db: Session):
    source = registry.create_source(
        db,
        SourceCreate(
            kind="fixture",
            page_type="home",
            fixture_id="F1",
            label="Completeness Matrix Source",
            intended_fields=["organisation_name"],
        ),
    )
    db.commit()

    # Case A: parsing complete, extraction complete
    o1 = record_observation(
        session=db,
        source_id=source.id,
        outcome=ObservationOutcome.SUCCESS_NEW_SNAPSHOT,
        content_changed=True,
        parsing_completeness=ParsingCompleteness.COMPLETE,
        extraction_completeness=ExtractionCompleteness.COMPLETE,
    )
    assert o1.parsing_completeness == "complete"
    assert o1.extraction_completeness == "complete"

    # Case B: parsing complete, extraction partial
    o2 = record_observation(
        session=db,
        source_id=source.id,
        outcome=ObservationOutcome.SUCCESS_UNCHANGED,
        content_changed=False,
        parsing_completeness=ParsingCompleteness.COMPLETE,
        extraction_completeness=ExtractionCompleteness.PARTIAL,
    )
    assert o2.parsing_completeness == "complete"
    assert o2.extraction_completeness == "partial"

    # Case C: parsing partial, extraction not_run
    o3 = record_observation(
        session=db,
        source_id=source.id,
        outcome=ObservationOutcome.INCOMPLETE,
        content_changed=False,
        parsing_completeness=ParsingCompleteness.PARTIAL,
        extraction_completeness=ExtractionCompleteness.NOT_RUN,
    )
    assert o3.parsing_completeness == "partial"
    assert o3.extraction_completeness == "not_run"


# =============================================================================
# 3. CONCURRENCY-SAFE SEQUENCE ALLOCATION TEST
# =============================================================================

def test_observation_concurrency_safe_monotonic_sequences():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "concurrent_obs.db"
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"timeout": 30},
            future=True,
        )
        orm.Base.metadata.create_all(bind=engine)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)

        with SessionLocal() as session:
            src = Source(
                kind="fixture",
                label="Concurrent Ingestion Source",
                fixture_id="F1",
                page_type="home",
                policy_status="manual_fixture_pending_policy_check",
                robots_status="fixture_exempt",
            )
            session.add(src)
            session.commit()
            source_id = src.id

        total_workers = 10

        def worker_task(worker_idx: int) -> int:
            with SessionLocal() as s:
                outcome = "success_new_snapshot" if worker_idx == 0 else ("failed" if worker_idx % 2 == 0 else "success_unchanged")
                obs = record_observation(
                    session=s,
                    source_id=source_id,
                    outcome=outcome,
                    content_changed=(outcome == "success_new_snapshot"),
                )
                s.commit()
                return obs.observation_sequence

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(worker_task, i) for i in range(total_workers)]
            allocated_sequences = [f.result() for f in concurrent.futures.as_completed(futures)]

        # Verify all sequence allocations succeeded and strictly match 1..10
        allocated_sequences.sort()
        assert allocated_sequences == list(range(1, total_workers + 1))

        # Query database and verify all 10 records are present with unique sequences
        with SessionLocal() as s:
            persisted_obs = list(s.scalars(
                select(SourceObservation).where(SourceObservation.source_id == source_id).order_by(SourceObservation.observation_sequence.asc())
            ).all())
            assert len(persisted_obs) == total_workers
            assert [o.observation_sequence for o in persisted_obs] == list(range(1, total_workers + 1))

        engine.dispose()


# =============================================================================
# 4. CONSERVATIVE 3-TIER ENTITY RESOLVER TESTS (D39)
# =============================================================================

def test_entity_resolver_positive_rule_1_local_phone_and_postcode(db: Session):
    org = Organisation(
        name="Carlton Family Medical",
        slug="carlton-family-medical",
        phone="(03) 9347 1234",
        address="120 Drummond Street",
        suburb="Carlton",
        postcode="3053",
        state="VIC",
    )
    db.add(org)
    db.commit()

    evaluation = evaluate_source_and_org(
        source_url="https://carltonmedical.example.org",
        source_domain="carltonmedical.example.org",
        source_label="Carlton Family Medical",
        source_phone="(03) 9347 1234",
        source_address="120 Drummond Street",
        source_postcode="3053",
        source_suburb="Carlton",
        source_state="VIC",
        org=org,
    )
    assert evaluation.tier == MatchTier.STRONG_MATCH_SUGGESTION
    assert evaluation.signals.local_phone_match is True
    assert evaluation.signals.postcode_match is True


def test_entity_resolver_positive_rule_2_domain_postcode_locality(db: Session):
    org = Organisation(
        name="St Jude Medical",
        slug="st-jude-medical",
        primary_domain="stjudemedical.example.org",
        phone=None,
        address="50 Collins St",
        suburb="Melbourne",
        postcode="3000",
        state="VIC",
    )
    db.add(org)
    db.commit()

    # Exact non-aggregator domain + exact postcode + suburb
    evaluation = evaluate_source_and_org(
        source_url="https://stjudemedical.example.org",
        source_domain="stjudemedical.example.org",
        source_label="St Jude Medical Centre",
        source_phone=None,
        source_address="50 Collins St",
        source_postcode="3000",
        source_suburb="Melbourne",
        source_state="VIC",
        org=org,
    )
    assert evaluation.tier == MatchTier.STRONG_MATCH_SUGGESTION
    assert evaluation.signals.domain_match is True
    assert evaluation.signals.postcode_match is True


def test_entity_resolver_positive_rule_3_name_similarity_and_postcode(db: Session):
    org = Organisation(
        name="East Melbourne General Practice",
        slug="east-melbourne-gp",
        primary_domain="eastmelbournegp.com.au",
        phone="(03) 9419 8888",
        postcode="3002",
        state="VIC",
    )
    db.add(org)
    db.commit()

    # Differing domain and differing phone, but token-sort similarity >= 88 and matching postcode
    evaluation = evaluate_source_and_org(
        source_url="https://other-domain.example.org",
        source_domain="other-domain.example.org",
        source_label="East Melbourne General Practice Clinic",
        source_phone="(03) 9419 1111",
        source_address="250 Victoria Parade",
        source_postcode="3002",
        source_suburb="East Melbourne",
        source_state="VIC",
        org=org,
    )
    assert evaluation.tier == MatchTier.POSSIBLE_MATCH
    assert evaluation.raw_metric_score is not None and evaluation.raw_metric_score >= 88.0
    assert evaluation.signals.postcode_match is True


def test_entity_resolver_positive_rule_4_aggregator_slug_address(db: Session):
    org = Organisation(
        name="MetroHealth Box Hill",
        slug="metrohealth-box-hill",
        primary_domain="metrohealth.example.org",
        address="450 Station St",
        suburb="Box Hill",
        postcode="3128",
        state="VIC",
    )
    db.add(org)
    db.commit()

    # Approved aggregator (directory.example.org) + branch slug in path + street address match
    evaluation = evaluate_source_and_org(
        source_url="https://directory.example.org/clinics/metrohealth-box-hill",
        source_domain="directory.example.org",
        source_label="MetroHealth Box Hill",
        source_phone=None,
        source_address="450 Station St",
        source_postcode="3128",
        source_suburb="Box Hill",
        source_state="VIC",
        org=org,
    )
    assert evaluation.tier == MatchTier.POSSIBLE_MATCH
    assert evaluation.signals.branch_slug_match is True
    assert evaluation.signals.street_address_match is True


def test_entity_resolver_negative_parent_domain_alone(db: Session):
    org = Organisation(
        name="Healius Corporate",
        slug="healius-corporate",
        primary_domain="healius.com.au",
        postcode="3000",
        state="VIC",
    )
    db.add(org)
    db.commit()

    # Parent domain matches, but source has NO postcode or locality proof
    evaluation = evaluate_source_and_org(
        source_url="https://healius.com.au/about",
        source_domain="healius.com.au",
        source_label="Healius National About",
        source_phone=None,
        source_address=None,
        source_postcode=None,
        source_suburb=None,
        source_state=None,
        org=org,
    )
    assert evaluation.tier == MatchTier.INSUFFICIENT_EVIDENCE
    assert "insufficient branch-level identity proof" in evaluation.rationale.lower()


def test_entity_resolver_negative_shared_corporate_1300_phone(db: Session):
    org = Organisation(
        name="National Health Hub",
        slug="national-health-hub",
        phone="1300 123 456",
        postcode="3000",
        state="VIC",
    )
    db.add(org)
    db.commit()

    evaluation = evaluate_source_and_org(
        source_url="https://healthhub.example.org",
        source_domain="healthhub.example.org",
        source_label="Health Hub Info",
        source_phone="1300 123 456",
        source_address=None,
        source_postcode="3000",
        source_suburb="Melbourne",
        source_state="VIC",
        org=org,
    )
    assert evaluation.tier == MatchTier.INSUFFICIENT_EVIDENCE
    assert "shared corporate" in evaluation.rationale.lower() or "1300" in evaluation.rationale


def test_entity_resolver_negative_differing_branch_postcodes(db: Session):
    # F9 branch isolation: MetroHealth Carlton (3053) vs MetroHealth Box Hill (3128)
    org_carlton = Organisation(
        name="MetroHealth Carlton",
        slug="metrohealth-carlton",
        phone="(03) 9347 5555",
        postcode="3053",
        state="VIC",
    )
    db.add(org_carlton)
    db.commit()

    evaluation = evaluate_source_and_org(
        source_url="https://metrohealth.example.org/locations/box-hill",
        source_domain="metrohealth.example.org",
        source_label="MetroHealth Box Hill",
        source_phone="(03) 9890 1111",
        source_address="450 Station St",
        source_postcode="3128",  # Differs from Carlton 3053!
        source_suburb="Box Hill",
        source_state="VIC",
        org=org_carlton,
    )
    assert evaluation.tier == MatchTier.INSUFFICIENT_EVIDENCE
    assert "differing postcodes" in evaluation.rationale.lower()


def test_entity_resolver_negative_distinct_practice_suites(db: Session):
    org = Organisation(
        name="Specialist Care Suite 1",
        slug="specialist-care-suite-1",
        address="Suite 1, 100 Collins St",
        postcode="3000",
        state="VIC",
    )
    db.add(org)
    db.commit()

    evaluation = evaluate_source_and_org(
        source_url="https://specialistcare.example.org",
        source_domain="specialistcare.example.org",
        source_label="Specialist Care",
        source_phone="(03) 9111 2222",
        source_address="Suite 2, 100 Collins St",  # Distinct Suite!
        source_postcode="3000",
        source_suburb="Melbourne",
        source_state="VIC",
        org=org,
    )
    assert evaluation.tier == MatchTier.INSUFFICIENT_EVIDENCE
    assert "distinct practice suites" in evaluation.rationale.lower()


def test_entity_resolver_negative_generic_name_without_local_proof(db: Session):
    org = Organisation(
        name="Family Medical Centre",
        slug="family-medical-centre",
        postcode="3000",
        state="VIC",
    )
    db.add(org)
    db.commit()

    # Generic clinic name lacking verified local phone or official domain match
    evaluation = evaluate_source_and_org(
        source_url="https://generic-directory.example.org",
        source_domain="generic-directory.example.org",
        source_label="Family Medical Centre",
        source_phone="(03) 9000 0000",
        source_address=None,
        source_postcode="3000",
        source_suburb=None,
        source_state=None,
        org=org,
    )
    assert evaluation.tier == MatchTier.INSUFFICIENT_EVIDENCE
    assert "generic practice name" in evaluation.rationale.lower()


def test_entity_resolver_negative_unapproved_aggregator_domain(db: Session):
    org = Organisation(
        name="City Medical",
        slug="city-medical",
        address="100 Bourke St",
        postcode="3000",
        state="VIC",
    )
    db.add(org)
    db.commit()

    # Domain is NOT in APPROVED_AGGREGATOR_DOMAINS
    evaluation = evaluate_source_and_org(
        source_url="https://unapproved-directory.com/clinics/city-medical",
        source_domain="unapproved-directory.com",
        source_label="Unapproved Directory Australia",
        source_phone=None,
        source_address="100 Bourke St",
        source_postcode="3000",
        source_suburb="Melbourne",
        source_state="VIC",
        org=org,
    )
    assert evaluation.tier == MatchTier.INSUFFICIENT_EVIDENCE


def test_entity_resolver_negative_ambiguous_geographic_evidence(db: Session):
    org = Organisation(
        name="Carlton Doctors",
        slug="carlton-doctors",
        primary_domain="carltondoctors.com.au",
        postcode="3053",
        state="VIC",
    )
    db.add(org)
    db.commit()

    # Missing postcode
    evaluation = evaluate_source_and_org(
        source_url="https://carltondoctors.com.au",
        source_domain="carltondoctors.com.au",
        source_label="Carlton Doctors",
        source_phone=None,
        source_address=None,
        source_postcode=None,  # Missing!
        source_suburb=None,
        source_state=None,
        org=org,
    )
    assert evaluation.tier == MatchTier.INSUFFICIENT_EVIDENCE


def test_entity_resolver_propose_candidate_idempotency_and_no_auto_merges(db: Session):
    org = Organisation(
        name="Ballarat Clinic",
        slug="ballarat-clinic",
        primary_domain="ballaratclinic.com.au",
        phone="(03) 5331 1000",
        postcode="3350",
        state="VIC",
    )
    db.add(org)
    db.flush()

    source = Source(
        kind="fixture",
        fixture_id="F1",
        page_type="home",
        label="Ballarat Clinic Home",
        domain="ballaratclinic.com.au",
        policy_status="manual_fixture_pending_policy_check",
        robots_status="fixture_exempt",
    )
    db.add(source)
    db.flush()

    snap = PageSnapshot(
        source_id=source.id,
        title="Ballarat Clinic",
        text="Ballarat Clinic (03) 5331 1000 Ballarat VIC 3350",
        content_hash="bh" * 32,
        parser_version="1.0.0",
        fixture=True,
        headings=[],
    )
    db.add(snap)
    db.commit()

    # Propose match candidate
    c1 = propose_match_candidate(db, source.id, org.id)
    assert c1 is not None
    assert c1.match_tier == MatchTier.STRONG_MATCH_SUGGESTION.value
    assert c1.status == ReviewStatus.SUGGESTED.value
    assert isinstance(c1.raw_metric_score, float)
    # Ensure scores are not converted to probability claims
    assert 0.0 <= c1.raw_metric_score <= 100.0

    # Idempotent call returns existing candidate without duplicate insertion
    c2 = propose_match_candidate(db, source.id, org.id)
    assert c2 is not None
    assert c2.id == c1.id


# =============================================================================
# 5. FACT OWNERSHIP AND REVIEW REASONS PERSISTENCE (D47)
# =============================================================================

def test_fact_ownership_single_practice_link_persisted(db: Session):
    org = Organisation(name="Richmond Medical", slug="richmond-medical", postcode="3121", state="VIC")
    db.add(org)
    db.flush()

    source = Source(kind="fixture", fixture_id="F1", page_type="home", label="Richmond", policy_status="manual_fixture_pending_policy_check", robots_status="fixture_exempt")
    db.add(source)
    db.flush()

    link = OrganisationSourceLink(
        organisation_id=org.id,
        source_id=source.id,
        relationship_type="official_practice_site",
        verification_status="confirmed",
        verified_by="analyst_test",
    )
    db.add(link)
    db.flush()

    snap = PageSnapshot(source_id=source.id, title="Home", text="Richmond Medical GP.", content_hash="h1" * 32, parser_version="1.0.0", fixture=True, headings=[])
    db.add(snap)
    db.flush()

    fact = Fact(snapshot_id=snap.id, field_key="organisation_name", raw_value="Richmond Medical", via="deterministic", status="candidate")
    db.add(fact)
    db.commit()

    assigned_id = assign_fact_ownership(db, fact)
    db.commit()

    # Query from database to verify persistence
    saved_fact = db.get(Fact, fact.id)
    assert saved_fact is not None
    assert saved_fact.organisation_id == org.id
    assert saved_fact.needs_review_reason is None


def test_fact_ownership_multi_branch_heading_persisted(db: Session):
    org_boxhill = Organisation(name="MetroHealth Box Hill", slug="metrohealth-box-hill", suburb="Box Hill", postcode="3128", state="VIC")
    org_carlton = Organisation(name="MetroHealth Carlton", slug="metrohealth-carlton", suburb="Carlton", postcode="3053", state="VIC")
    db.add_all([org_boxhill, org_carlton])
    db.flush()

    source = Source(kind="fixture", fixture_id="F9", page_type="services", label="Multi Branch", policy_status="manual_fixture_pending_policy_check", robots_status="fixture_exempt")
    db.add(source)
    db.flush()

    db.add(OrganisationSourceLink(organisation_id=org_boxhill.id, source_id=source.id, relationship_type="branch_page", verification_status="confirmed", verified_by="analyst_test"))
    db.add(OrganisationSourceLink(organisation_id=org_carlton.id, source_id=source.id, relationship_type="branch_page", verification_status="confirmed", verified_by="analyst_test"))
    db.flush()

    text = "Locations\n\nBox Hill Branch\nPhone: (03) 9890 1111\n\nCarlton Branch\nPhone: (03) 9347 5555"
    headings = [
        {"level": 1, "text": "Locations", "start_offset": 0, "end_offset": 9},
        {"level": 2, "text": "Box Hill Branch", "start_offset": 11, "end_offset": 26},
        {"level": 2, "text": "Carlton Branch", "start_offset": 51, "end_offset": 65},
    ]
    snap = PageSnapshot(source_id=source.id, title="Locations", text=text, content_hash="h2" * 32, parser_version="1.0.0", fixture=True, headings=headings)
    db.add(snap)
    db.flush()

    ev = Evidence(snapshot_id=snap.id, start_offset=35, end_offset=49, quote="(03) 9890 1111", match_algorithm_version="1.0")
    db.add(ev)
    db.flush()

    fact = Fact(snapshot_id=snap.id, field_key="phone", raw_value="(03) 9890 1111", evidence_id=ev.id, via="deterministic", status="candidate")
    db.add(fact)
    db.commit()

    assigned_id = assign_fact_ownership(db, fact)
    db.commit()

    saved_fact = db.get(Fact, fact.id)
    assert saved_fact is not None
    assert saved_fact.organisation_id == org_boxhill.id
    assert saved_fact.needs_review_reason is None


def test_fact_ownership_corporate_heading_persisted(db: Session):
    org = Organisation(name="National Health", slug="national-health", postcode="3000", state="VIC")
    db.add(org)
    db.flush()

    source = Source(kind="fixture", fixture_id="F1", page_type="home", label="Src", policy_status="manual_fixture_pending_policy_check", robots_status="fixture_exempt")
    db.add(source)
    db.flush()

    db.add(OrganisationSourceLink(organisation_id=org.id, source_id=source.id, relationship_type="branch_page", verification_status="confirmed", verified_by="analyst_test"))
    db.flush()

    text = "Careers\nJoin our team."
    headings = [{"level": 1, "text": "Careers", "start_offset": 0, "end_offset": 7}]
    snap = PageSnapshot(source_id=source.id, title="Careers", text=text, content_hash="h3" * 32, parser_version="1.0.0", fixture=True, headings=headings)
    db.add(snap)
    db.flush()

    ev = Evidence(snapshot_id=snap.id, start_offset=8, end_offset=22, quote="Join our team", match_algorithm_version="1.0")
    db.add(ev)
    db.flush()

    fact = Fact(snapshot_id=snap.id, field_key="vacancy_notice", raw_value="Join team", evidence_id=ev.id, via="deterministic", status="candidate")
    db.add(fact)
    db.commit()

    assigned_id = assign_fact_ownership(db, fact)
    db.commit()

    saved_fact = db.get(Fact, fact.id)
    assert saved_fact is not None
    assert saved_fact.organisation_id is None
    assert saved_fact.needs_review_reason == "heading_scope_corporate_unassigned"


def test_fact_ownership_missing_heading_context_persisted(db: Session):
    org1 = Organisation(name="Branch A", slug="branch-a", postcode="3000", state="VIC")
    org2 = Organisation(name="Branch B", slug="branch-b", postcode="3001", state="VIC")
    db.add_all([org1, org2])
    db.flush()

    source = Source(kind="fixture", fixture_id="F1", page_type="home", label="Src", policy_status="manual_fixture_pending_policy_check", robots_status="fixture_exempt")
    db.add(source)
    db.flush()

    db.add(OrganisationSourceLink(organisation_id=org1.id, source_id=source.id, relationship_type="branch_page", verification_status="confirmed", verified_by="analyst_test"))
    db.add(OrganisationSourceLink(organisation_id=org2.id, source_id=source.id, relationship_type="branch_page", verification_status="confirmed", verified_by="analyst_test"))
    db.flush()

    snap = PageSnapshot(source_id=source.id, title="No Headings", text="Some text.", content_hash="h4" * 32, parser_version="1.0.0", fixture=True, headings=[])
    db.add(snap)
    db.flush()

    # Fact has NO evidence and NO headings
    fact = Fact(snapshot_id=snap.id, field_key="phone", raw_value="123", via="deterministic", status="candidate")
    db.add(fact)
    db.commit()

    assigned_id = assign_fact_ownership(db, fact)
    db.commit()

    saved_fact = db.get(Fact, fact.id)
    assert saved_fact is not None
    assert saved_fact.organisation_id is None
    assert saved_fact.needs_review_reason == "missing_heading_context"


def test_fact_ownership_conflicting_branch_evidence_persisted(db: Session):
    org1 = Organisation(name="City Medical Centre", slug="city-medical-centre", postcode="3000", state="VIC")
    org2 = Organisation(name="City Medical Clinic", slug="city-medical-clinic", postcode="3000", state="VIC")
    db.add_all([org1, org2])
    db.flush()

    source = Source(kind="fixture", fixture_id="F1", page_type="home", label="Src", policy_status="manual_fixture_pending_policy_check", robots_status="fixture_exempt")
    db.add(source)
    db.flush()

    db.add(OrganisationSourceLink(organisation_id=org1.id, source_id=source.id, relationship_type="branch_page", verification_status="confirmed", verified_by="analyst_test"))
    db.add(OrganisationSourceLink(organisation_id=org2.id, source_id=source.id, relationship_type="branch_page", verification_status="confirmed", verified_by="analyst_test"))
    db.flush()

    # Heading matches BOTH org1 and org2 ("City Medical")
    headings = [{"level": 1, "text": "City Medical Services", "start_offset": 0, "end_offset": 21}]
    snap = PageSnapshot(source_id=source.id, title="City", text="City Medical Services\nPhone: 123", content_hash="h5" * 32, parser_version="1.0.0", fixture=True, headings=headings)
    db.add(snap)
    db.flush()

    ev = Evidence(snapshot_id=snap.id, start_offset=29, end_offset=32, quote="123", match_algorithm_version="1.0")
    db.add(ev)
    db.flush()

    fact = Fact(snapshot_id=snap.id, field_key="phone", raw_value="123", evidence_id=ev.id, via="deterministic", status="candidate")
    db.add(fact)
    db.commit()

    assigned_id = assign_fact_ownership(db, fact)
    db.commit()

    saved_fact = db.get(Fact, fact.id)
    assert saved_fact is not None
    assert saved_fact.organisation_id is None
    assert saved_fact.needs_review_reason == "conflicting_branch_evidence"


def test_fact_ownership_parent_domain_only_persisted(db: Session):
    org = Organisation(name="National Health Parent", slug="national-health-parent", postcode="3000", state="VIC")
    db.add(org)
    db.flush()

    source = Source(kind="fixture", fixture_id="F1", page_type="home", label="HQ", policy_status="manual_fixture_pending_policy_check", robots_status="fixture_exempt")
    db.add(source)
    db.flush()

    # Relationship is corporate_parent
    db.add(OrganisationSourceLink(organisation_id=org.id, source_id=source.id, relationship_type="corporate_parent", verification_status="confirmed", verified_by="analyst_test"))
    db.flush()

    snap = PageSnapshot(source_id=source.id, title="HQ", text="Corporate text", content_hash="corp" * 16, parser_version="1.0.0", fixture=True, headings=[])
    db.add(snap)
    db.flush()

    fact = Fact(snapshot_id=snap.id, field_key="policy", raw_value="National policy", via="deterministic", status="candidate")
    db.add(fact)
    db.commit()

    res = assign_fact_ownership(db, fact)
    db.commit()

    saved_fact = db.get(Fact, fact.id)
    assert saved_fact is not None
    assert saved_fact.organisation_id is None
    assert saved_fact.needs_review_reason == "heading_scope_corporate_unassigned"


def test_fact_ownership_preserves_existing_assignment(db: Session):
    org1 = Organisation(name="Org One", slug="org-one", postcode="3000", state="VIC")
    org2 = Organisation(name="Org Two", slug="org-two", postcode="3001", state="VIC")
    db.add_all([org1, org2])
    db.flush()

    source = Source(kind="fixture", fixture_id="F1", page_type="home", label="Src", policy_status="manual_fixture_pending_policy_check", robots_status="fixture_exempt")
    db.add(source)
    db.flush()

    db.add(OrganisationSourceLink(organisation_id=org2.id, source_id=source.id, relationship_type="official_practice_site", verification_status="confirmed", verified_by="analyst_test"))
    db.flush()

    snap = PageSnapshot(source_id=source.id, title="Title", text="Text", content_hash="h6" * 32, parser_version="1.0.0", fixture=True, headings=[])
    db.add(snap)
    db.flush()

    fact = Fact(snapshot_id=snap.id, field_key="phone", raw_value="123", organisation_id=org1.id, via="deterministic", status="candidate")
    db.add(fact)
    db.commit()

    # Non-overwrite preserve
    res = assign_fact_ownership(db, fact, overwrite_existing=False)
    assert res == org1.id
    assert fact.organisation_id == org1.id


def test_fact_ownership_rejects_invalid_organisation_id(db: Session):
    snap = PageSnapshot(source_id=1, title="T", text="X", content_hash="h7" * 32, parser_version="1.0.0", fixture=True, headings=[])
    fact = Fact(snapshot_id=snap.id, field_key="phone", raw_value="123", organisation_id=999999, via="deterministic", status="candidate")

    # Attempting to assign or inspect a fact with non-existent organisation_id raises ValueError
    with pytest.raises(ValueError, match="non-existent organisation_id"):
        assign_fact_ownership(db, fact, overwrite_existing=False)


# =============================================================================
# 6. SOURCE-LINK MANAGEMENT & REST API TESTS
# =============================================================================

def test_api_organisations_crud_and_filters(client: TestClient):
    payload = {
        "name": "St Kilda Doctors",
        "slug": "st-kilda-doctors",
        "primary_domain": "stkildadoctors.com.au",
        "phone": "(03) 9534 0000",
        "address": "150 Carlisle St",
        "suburb": "St Kilda",
        "postcode": "3182",
        "state": "VIC",
        "status": "active",
    }
    create_resp = client.post("/api/organisations", json=payload)
    assert create_resp.status_code == 201, create_resp.text
    org = create_resp.json()
    assert org["slug"] == "st-kilda-doctors"

    # Duplicate slug returns 409 conflict
    dup_resp = client.post("/api/organisations", json=payload)
    assert dup_resp.status_code == 409
    assert dup_resp.json()["error"]["code"] == "organisation_slug_conflict"

    # Filter by postcode and state
    list_resp = client.get("/api/organisations?postcode=3182&state=VIC")
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1

    # Filter with mismatch returns empty list
    mismatch_resp = client.get("/api/organisations?postcode=4000")
    assert mismatch_resp.status_code == 200
    assert len(mismatch_resp.json()) == 0

    # Detail view
    detail_resp = client.get(f"/api/organisations/{org['id']}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["active_facts_count"] == 0
    assert detail["source_links"] == []


def test_api_source_link_lifecycle_and_verification_rules(client: TestClient):
    org_resp = client.post("/api/organisations", json={
        "name": "South Yarra Medical",
        "slug": "south-yarra-medical",
        "postcode": "3141",
        "state": "VIC",
    })
    assert org_resp.status_code == 201
    org_id = org_resp.json()["id"]

    src_resp = client.post("/api/sources", json={
        "kind": "fixture",
        "fixture_id": "F1",
        "page_type": "home",
        "label": "South Yarra Source",
        "intended_fields": ["organisation_name"],
    })
    assert src_resp.status_code == 201
    source_id = src_resp.json()["id"]

    # Rule: cannot create link with verification_status='confirmed' without verified_by
    unverified_create = client.post(f"/api/organisations/{org_id}/sources", json={
        "source_id": source_id,
        "relationship_type": "official_practice_site",
        "verification_status": "confirmed",
        # missing verified_by
    })
    assert unverified_create.status_code == 422
    assert unverified_create.json()["error"]["code"] == "link_verification_error"

    # 1. Create link with status='suggested'
    link_resp = client.post(f"/api/organisations/{org_id}/sources", json={
        "source_id": source_id,
        "relationship_type": "official_practice_site",
        "verification_status": "suggested",
    })
    assert link_resp.status_code == 201
    link = link_resp.json()
    link_id = link["id"]
    assert link["verification_status"] == "suggested"
    assert link["verified_at"] is None

    # Duplicate link returns 409 conflict
    dup_link = client.post(f"/api/organisations/{org_id}/sources", json={
        "source_id": source_id,
        "relationship_type": "official_practice_site",
    })
    assert dup_link.status_code == 409
    assert dup_link.json()["error"]["code"] == "source_link_conflict"

    # 2. Rule: cannot transition to 'confirmed' without verified_by
    unverified_patch = client.patch(f"/api/organisations/{org_id}/sources/{link_id}", json={
        "verification_status": "confirmed",
    })
    assert unverified_patch.status_code == 422
    assert unverified_patch.json()["error"]["code"] == "link_verification_error"

    # 3. Deliberate transition to 'confirmed' with verified_by operator identity
    confirmed_patch = client.patch(f"/api/organisations/{org_id}/sources/{link_id}", json={
        "verification_status": "confirmed",
        "verified_by": "lead_analyst",
    })
    assert confirmed_patch.status_code == 200
    confirmed_data = confirmed_patch.json()
    assert confirmed_data["verification_status"] == "confirmed"
    assert confirmed_data["verified_by"] == "lead_analyst"
    assert confirmed_data["verified_at"] is not None

    # 4. Transition to 'rejected'
    rejected_patch = client.patch(f"/api/organisations/{org_id}/sources/{link_id}", json={
        "verification_status": "rejected",
        "verified_by": "qa_supervisor",
    })
    assert rejected_patch.status_code == 200
    assert rejected_patch.json()["verification_status"] == "rejected"
    assert rejected_patch.json()["verified_by"] == "qa_supervisor"

    # 5. Return to 'suggested' clears verified_at
    reset_patch = client.patch(f"/api/organisations/{org_id}/sources/{link_id}", json={
        "verification_status": "suggested",
    })
    assert reset_patch.status_code == 200
    assert reset_patch.json()["verification_status"] == "suggested"
    assert reset_patch.json()["verified_at"] is None


def test_api_match_candidates_scan_and_absence_of_merge_endpoint(client: TestClient):
    org_resp = client.post("/api/organisations", json={
        "name": "Scan Org",
        "slug": "scan-org-test",
        "postcode": "3000",
        "state": "VIC",
    })
    org_id = org_resp.json()["id"]

    src_resp = client.post("/api/sources", json={
        "kind": "fixture",
        "fixture_id": "F1",
        "page_type": "home",
        "label": "Scan Source",
        "intended_fields": ["organisation_name"],
    })
    source_id = src_resp.json()["id"]

    # Trigger scan
    scan_resp = client.post(f"/api/match-candidates/scan/{source_id}")
    assert scan_resp.status_code == 200

    # Query match candidates
    cand_resp = client.get("/api/match-candidates")
    assert cand_resp.status_code == 200

    # Verify strict absence of automatic merge endpoints
    merge_resp = client.post(f"/api/organisations/{org_id}/merge", json={"target_id": 999})
    assert merge_resp.status_code in (404, 405)


def test_api_source_observations_history_endpoint(client: TestClient):
    src_resp = client.post("/api/sources", json={
        "kind": "fixture",
        "fixture_id": "F1",
        "page_type": "home",
        "label": "Obs History Source",
        "intended_fields": ["organisation_name"],
    })
    source_id = src_resp.json()["id"]

    # Ingest to generate heartbeat
    ingest_resp = client.post(f"/api/ingestions/{source_id}")
    assert ingest_resp.status_code == 200

    obs_resp = client.get(f"/api/sources/{source_id}/observations")
    assert obs_resp.status_code == 200
    observations = obs_resp.json()
    assert len(observations) >= 1
    assert observations[0]["observation_sequence"] == 1
    assert observations[0]["outcome"] == "success_new_snapshot"
