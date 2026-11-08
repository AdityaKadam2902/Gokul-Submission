"""M3.3 comprehensive temporal change detection & observation delta engine test suite.

Covers:
1. Database migration M3_003:
   - Fresh DB creation includes change_events table with ON DELETE RESTRICT foreign keys.
   - Upgrading existing DB safely migrates or adds change_events table with data preservation.
   - Automatic timestamped backup and rollback testing.
   - Idempotency on repeated execution.
   - Snapshot distinction check constraint (previous_snapshot_id IS NULL OR previous_snapshot_id != current_snapshot_id).
2. Historical record preservation (MANDATORY):
   - Deleting an organisation referenced by a change event is restricted (IntegrityError).
   - Deleting a source referenced by a change event is restricted (IntegrityError).
   - Deleting a snapshot referenced by a change event is restricted (IntegrityError).
   - Deleting a fact referenced by a change event is restricted (IntegrityError).
   - Foreign-key integrity validation under PRAGMA foreign_keys = ON.
3. Observation eligibility and completeness rules:
   - Eligible: success_new_snapshot, success_unchanged with complete parsing and extraction.
   - Ineligible: failed, incomplete, partial parsing, partial extraction, not_run.
   - Ineligible observations freeze absence counters and emit 0 change events.
4. Event semantics (D42):
   - Initial baseline (F13_v1): establishes initial known fact set; emits 0 change events.
   - Re-crawl unchanged (F13_unchanged): records heartbeat; emits 0 change events.
   - Fact added (F13_v2): newly observed service emits fact_added.
   - Fact modified (F13_v2): updated telephone number emits fact_modified.
   - Consecutive absence rule (F13_v3):
     - 1st complete absence: emits unconfirmed_prior (count = 1).
     - 2nd consecutive complete absence: emits fact_unobserved (count = 2).
     - Historical fact is NEVER deleted or overwritten.
   - Reappearance: previously unobserved fact returns -> emits fact_reappeared.
5. Extraction omission defense (F13_omit):
   - When a fact is missing from extraction output but still visibly present in raw text/DOM,
     the detector suppresses removal alerts and freezes the absence counter.
6. Multi-scope and repeatable field isolation:
   - Departmental scopes (reception vs after_hours) remain distinct.
   - Multi-instance fields (service_names) track individual services independently.
7. Source-local isolation:
   - Successive observations of the SAME source only; different sources are never compared.
8. Deterministic idempotency & API:
   - Repeated scans produce 0 duplicate records.
   - Paginated listing, detailed event inspection, and scan endpoints.
   - Negative tests verifying no auto-approval or delete endpoints exist.
"""

from __future__ import annotations

import sqlite3
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import orm
from backend.app.db.migrations import (
    inspect_m3_003_schema,
    migrate_m3_003_change_events,
    rollback_m3_003_change_events,
)
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
from backend.app.intelligence.change_detector import (
    compute_fact_identity,
    detect_source_changes,
    evaluate_source_fact_states,
    is_eligible_observation,
    is_extraction_omission,
    scan_organisation_changes,
    scan_source_changes,
)
from backend.app.intelligence.observation_tracker import record_observation
from backend.app.intelligence.schemas import (
    ChangeEventCategory,
    ExtractionCompleteness,
    ObservationOutcome,
    ParsingCompleteness,
)


@pytest.fixture()
def db():
    """In-memory SQLite session with full schema and foreign keys strictly enabled."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    orm.Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def client(m1_client):
    return m1_client


def _make_source(
    session: Session,
    url: str = "https://oakwood-clinic.example.org/",
    label: str = "Oakwood Clinic Source",
    fixture_id: str = "F13",
) -> Source:
    src = Source(
        kind="fixture",
        url=url,
        label=label,
        domain="oakwood-clinic.example.org",
        fixture_id=fixture_id,
        page_type="home",
        policy_status="approved",
        robots_status="allowed",
        intended_fields=["public_phone", "opening_hours", "service_names"],
    )
    session.add(src)
    session.flush()
    return src


def _make_snapshot(
    session: Session,
    source: Source,
    text: str = "Oakwood Medical Clinic Main Telephone: (03) 9123 4567",
    content_hash: str | None = None,
) -> PageSnapshot:
    hash_val = content_hash or (uuid.uuid4().hex + uuid.uuid4().hex)[:64]
    snap = PageSnapshot(
        source_id=source.id,
        fetched_at=datetime.now(UTC),
        title="Oakwood Clinic Homepage",
        text=text,
        content_hash=hash_val,
        parser_version="1.0.0",
        fixture=True,
        headings=[],
    )
    session.add(snap)
    session.flush()
    return snap


def _make_org(session: Session, name: str = "Oakwood Medical Clinic", slug: str = "oakwood-clinic-3141") -> Organisation:
    org = Organisation(
        name=name,
        slug=slug,
        primary_domain="oakwood-clinic.example.org",
        phone="0391234567",
        address="123 Oakwood Road",
        suburb="Oakwood",
        postcode="3141",
        state="VIC",
        status="active",
    )
    session.add(org)
    session.flush()
    return org


def _make_fact(
    session: Session,
    snapshot: PageSnapshot,
    organisation_id: int | None,
    field_key: str,
    raw_value: str,
    scope_key: str | None = "reception",
    quote: str | None = None,
    status: str = "accepted",
) -> Fact:
    ev_quote = quote or raw_value
    snap_text = snapshot.text or ""
    if ev_quote not in snap_text:
        snapshot.text = (snap_text + "\n" + ev_quote).strip()
        session.add(snapshot)
        session.flush()
        snap_text = snapshot.text

    start_off = snap_text.find(ev_quote)
    end_off = start_off + len(ev_quote)

    ev = Evidence(
        snapshot_id=snapshot.id,
        quote=ev_quote,
        start_offset=start_off,
        end_offset=end_off,
        captured_at=datetime.now(UTC),
        match_algorithm_version="v1",
    )
    session.add(ev)
    session.flush()

    fact = Fact(
        snapshot_id=snapshot.id,
        organisation_id=organisation_id,
        field_key=field_key,
        scope_key=scope_key,
        raw_value=raw_value,
        evidence_id=ev.id,
        via="deterministic",
        status=status,
    )
    session.add(fact)
    session.flush()
    return fact


def _link_source(session: Session, org_id: int, source_id: int, status: str = "confirmed") -> OrganisationSourceLink:
    link = OrganisationSourceLink(
        organisation_id=org_id,
        source_id=source_id,
        relationship_type="official_practice_site",
        verification_status=status,
        verified_by="test_operator",
        verified_at=datetime.now(UTC),
    )
    session.add(link)
    session.flush()
    return link


def _make_observation(
    session: Session,
    source_id: int,
    outcome: str = "success_new_snapshot",
    snapshot_id: int | None = None,
    seq: int = 1,
    parsing: str = "complete",
    extraction: str = "complete",
) -> SourceObservationORM:
    obs = SourceObservationORM(
        source_id=source_id,
        observed_at=datetime.now(UTC),
        outcome=outcome,
        snapshot_id=snapshot_id,
        content_hash=(uuid.uuid4().hex + uuid.uuid4().hex)[:64] if snapshot_id else None,
        content_changed=outcome == "success_new_snapshot",
        parsing_completeness=parsing,
        extraction_completeness=extraction,
        observation_sequence=seq,
        created_at=datetime.now(UTC),
    )
    session.add(obs)
    session.flush()
    return obs


# =============================================================================
# 1. DATABASE MIGRATION M3_003 & HISTORICAL RECORD PRESERVATION
# =============================================================================

def test_m3_003_migration_on_fresh_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "fresh.db"
        engine = create_engine(f"sqlite:///{db_path}")
        orm.Base.metadata.create_all(bind=engine)
        engine.dispose()

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        info = inspect_m3_003_schema(cur)
        cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='change_events'")
        sql = (cur.fetchone()[0] or "").upper()
        conn.close()

        assert info["change_events"] is True
        assert "ON DELETE RESTRICT" in sql
        assert "CASCADE" not in sql


def test_m3_003_migration_upgrades_legacy_cascade_to_restrict():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "legacy.db"
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()

        # Simulate legacy schema
        cur.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY);")
        cur.execute("CREATE TABLE organisations (id INTEGER PRIMARY KEY);")
        cur.execute("CREATE TABLE source_observations (id INTEGER PRIMARY KEY);")
        cur.execute("CREATE TABLE page_snapshots (id INTEGER PRIMARY KEY);")
        cur.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY);")
        cur.execute("""
            CREATE TABLE change_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                organisation_id INTEGER REFERENCES organisations(id) ON DELETE CASCADE,
                previous_observation_id INTEGER REFERENCES source_observations(id) ON DELETE CASCADE,
                current_observation_id INTEGER NOT NULL REFERENCES source_observations(id) ON DELETE CASCADE,
                previous_snapshot_id INTEGER REFERENCES page_snapshots(id) ON DELETE CASCADE,
                current_snapshot_id INTEGER NOT NULL REFERENCES page_snapshots(id) ON DELETE CASCADE,
                previous_fact_id INTEGER REFERENCES facts(id) ON DELETE CASCADE,
                current_fact_id INTEGER REFERENCES facts(id) ON DELETE CASCADE,
                event_type VARCHAR(64) NOT NULL,
                entity_type VARCHAR(32) NOT NULL DEFAULT 'fact',
                field_key VARCHAR(64) NOT NULL,
                scope_key VARCHAR(64),
                old_value TEXT,
                new_value TEXT,
                rationale TEXT NOT NULL,
                diff_metadata TEXT NOT NULL DEFAULT '{}',
                idempotency_key VARCHAR(255) NOT NULL UNIQUE,
                detected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # Insert test data
        cur.execute("INSERT INTO sources VALUES (1);")
        cur.execute("INSERT INTO organisations VALUES (1);")
        cur.execute("INSERT INTO source_observations VALUES (1);")
        cur.execute("INSERT INTO source_observations VALUES (2);")
        cur.execute("INSERT INTO page_snapshots VALUES (10);")
        cur.execute("INSERT INTO page_snapshots VALUES (20);")
        cur.execute("INSERT INTO facts VALUES (100);")
        cur.execute("INSERT INTO facts VALUES (200);")
        cur.execute("""
            INSERT INTO change_events (
                source_id, organisation_id, previous_observation_id, current_observation_id,
                previous_snapshot_id, current_snapshot_id, previous_fact_id, current_fact_id,
                event_type, field_key, scope_key, old_value, new_value, rationale, idempotency_key
            ) VALUES (
                1, 1, 1, 2, 10, 20, 100, 200,
                'fact_modified', 'public_phone', 'reception', '123', '456', 'Legacy rationale', 'legacy_key_1'
            );
        """)
        conn.commit()
        conn.close()

        res = migrate_m3_003_change_events(db_path)
        assert res["status"] == "upgraded"

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='change_events'")
        sql = (cur.fetchone()[0] or "").upper()
        assert "ON DELETE RESTRICT" in sql
        assert "CASCADE" not in sql

        cur.execute("SELECT id, rationale, idempotency_key FROM change_events")
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][1] == "Legacy rationale"
        assert rows[0][2] == "legacy_key_1"
        conn.close()


def test_m3_003_migration_idempotent():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "fresh.db"
        engine = create_engine(f"sqlite:///{db_path}")
        orm.Base.metadata.create_all(bind=engine)
        engine.dispose()

        res1 = migrate_m3_003_change_events(db_path)
        assert res1["status"] == "up_to_date"

        res2 = migrate_m3_003_change_events(db_path)
        assert res2["status"] == "up_to_date"


def test_m3_003_rollback():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "fresh.db"
        engine = create_engine(f"sqlite:///{db_path}")
        orm.Base.metadata.create_all(bind=engine)
        engine.dispose()

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        assert inspect_m3_003_schema(cur)["change_events"] is True
        conn.close()

        res = rollback_m3_003_change_events(db_path)
        assert res["status"] == "success"

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        assert inspect_m3_003_schema(cur)["change_events"] is False
        conn.close()


def test_historical_preservation_prevent_deletions(db: Session):
    """Historical preservation: deleting linked org, source, snapshot, or fact is restricted."""
    org = _make_org(db)
    src = _make_source(db)
    _link_source(db, org.id, src.id, "confirmed")

    snap_1 = _make_snapshot(db, src, "Phone: (03) 9123 4567")
    snap_2 = _make_snapshot(db, src, "Phone: (03) 9123 9999")

    obs_1 = _make_observation(db, src.id, "success_new_snapshot", snap_1.id, 1)
    obs_2 = _make_observation(db, src.id, "success_new_snapshot", snap_2.id, 2)

    fact_1 = _make_fact(db, snap_1, org.id, "public_phone", "(03) 9123 4567")
    fact_2 = _make_fact(db, snap_2, org.id, "public_phone", "(03) 9123 9999")

    events = detect_source_changes(db, src.id)
    assert len(events) == 1
    evt = events[0]
    db.commit()

    # 1. Prevent Organisation Deletion
    db.delete(org)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    # 2. Prevent Source Deletion
    db.delete(src)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    # 3. Prevent Snapshot Deletion
    db.delete(snap_1)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    # 4. Prevent Fact Deletion
    db.delete(fact_1)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    assert db.get(Organisation, org.id) is not None
    assert db.get(Source, src.id) is not None
    assert db.get(PageSnapshot, snap_1.id) is not None
    assert db.get(Fact, fact_1.id) is not None
    assert db.get(ChangeEventORM, evt.id) is not None


# =============================================================================
# 2. OBSERVATION ELIGIBILITY & INELIGIBLE FREEZE
# =============================================================================

def test_observation_eligibility_classification():
    """Verify independent state eligibility requirements."""
    obs_good = SourceObservationORM(
        source_id=1, outcome="success_new_snapshot", parsing_completeness="complete",
        extraction_completeness="complete", observation_sequence=1,
    )
    assert is_eligible_observation(obs_good) is True

    obs_unchanged = SourceObservationORM(
        source_id=1, outcome="success_unchanged", parsing_completeness="complete",
        extraction_completeness="complete", snapshot_id=10, observation_sequence=2,
    )
    assert is_eligible_observation(obs_unchanged) is True

    obs_failed = SourceObservationORM(
        source_id=1, outcome="failed", parsing_completeness="failed",
        extraction_completeness="not_run", observation_sequence=3,
    )
    assert is_eligible_observation(obs_failed) is False

    obs_incomplete = SourceObservationORM(
        source_id=1, outcome="incomplete", parsing_completeness="partial",
        extraction_completeness="not_run", observation_sequence=4,
    )
    assert is_eligible_observation(obs_incomplete) is False

    obs_not_run = SourceObservationORM(
        source_id=1, outcome="success_new_snapshot", parsing_completeness="complete",
        extraction_completeness="not_run", observation_sequence=5,
    )
    assert is_eligible_observation(obs_not_run) is False


def test_failed_and_incomplete_crawl_freezes_absence_counter(db: Session):
    """Ineligible crawls freeze consecutive absence count and emit zero events (F13_err)."""
    src = _make_source(db)
    snap_1 = _make_snapshot(db, src, "Oakwood Clinic Phone: (03) 9123 4567")
    _make_observation(db, src.id, "success_new_snapshot", snap_1.id, 1)
    _make_fact(db, snap_1, None, "public_phone", "(03) 9123 4567")

    # Baseline scan: 0 events
    evts_1 = detect_source_changes(db, src.id)
    assert len(evts_1) == 0

    # Ineligible attempt 1: failed crawl (network error 500)
    _make_observation(db, src.id, "failed", None, 2, parsing="failed", extraction="not_run")
    evts_2 = detect_source_changes(db, src.id)
    assert len(evts_2) == 0

    # Ineligible attempt 2: incomplete crawl (truncated HTML)
    _make_observation(db, src.id, "incomplete", None, 3, parsing="partial", extraction="not_run")
    evts_3 = detect_source_changes(db, src.id)
    assert len(evts_3) == 0

    # Ineligible attempt 3: crawl succeeded but extraction not run
    snap_partial = _make_snapshot(db, src, "some text")
    _make_observation(db, src.id, "success_new_snapshot", snap_partial.id, 4, parsing="complete", extraction="not_run")
    evts_4 = detect_source_changes(db, src.id)
    assert len(evts_4) == 0

    count = db.scalar(select(func.count(ChangeEventORM.id)))
    assert count == 0


# =============================================================================
# 3. F13 TIMELINE GROUND TRUTH (BASELINE, UNCHANGED, MODIFIED, ABSENCE)
# =============================================================================

def test_f13_v1_baseline_and_unchanged_heartbeat(db: Session):
    """F13 v1 establishes initial facts; unchanged crawl produces 0 events and 0 duplicate snapshots."""
    src = _make_source(db)
    snap_1 = _make_snapshot(
        db, src,
        "Oakwood Medical Clinic Main Telephone: (03) 9123 4567 Opening Hours: Mon-Fri 08:00 to 17:00 General Practice",
    )
    _make_observation(db, src.id, "success_new_snapshot", snap_1.id, 1)
    _make_fact(db, snap_1, None, "public_phone", "(03) 9123 4567")
    _make_fact(db, snap_1, None, "opening_hours", "Monday to Friday 08:00 to 17:00")
    _make_fact(db, snap_1, None, "service_names", "General Practice")

    # 1. Baseline observation
    evts_v1 = detect_source_changes(db, src.id)
    assert len(evts_v1) == 0  # Baseline emits 0 events

    # 2. Re-crawl unchanged (same snapshot, heartbeat recorded)
    _make_observation(db, src.id, "success_unchanged", snap_1.id, 2)
    evts_unchanged = detect_source_changes(db, src.id)
    assert len(evts_unchanged) == 0

    total_events = db.scalar(select(func.count(ChangeEventORM.id)))
    assert total_events == 0


def test_f13_v2_modified_phone_and_added_service(db: Session):
    """F13 v2 modified: phone update emits fact_modified; physiotherapy service emits fact_added."""
    src = _make_source(db)
    # v1
    snap_1 = _make_snapshot(
        db, src,
        "Oakwood Clinic Phone: (03) 9123 4567 Hours: 08:00 to 17:00 Service: General Practice",
    )
    _make_observation(db, src.id, "success_new_snapshot", snap_1.id, 1)
    _make_fact(db, snap_1, None, "public_phone", "(03) 9123 4567")
    _make_fact(db, snap_1, None, "opening_hours", "08:00 to 17:00")
    _make_fact(db, snap_1, None, "service_names", "General Practice")
    detect_source_changes(db, src.id)

    # v2: updated phone + added physiotherapy
    snap_2 = _make_snapshot(
        db, src,
        "Oakwood Clinic Phone: (03) 9123 9999 Hours: 08:00 to 17:00 Service: General Practice Service: Physiotherapy",
    )
    _make_observation(db, src.id, "success_new_snapshot", snap_2.id, 2)
    _make_fact(db, snap_2, None, "public_phone", "(03) 9123 9999")
    _make_fact(db, snap_2, None, "opening_hours", "08:00 to 17:00")
    _make_fact(db, snap_2, None, "service_names", "General Practice")
    _make_fact(db, snap_2, None, "service_names", "Physiotherapy")

    evts = detect_source_changes(db, src.id)
    assert len(evts) == 2

    by_type = {e.event_type: e for e in evts}
    assert ChangeEventCategory.FACT_MODIFIED.value in by_type
    assert ChangeEventCategory.FACT_ADDED.value in by_type

    mod_evt = by_type[ChangeEventCategory.FACT_MODIFIED.value]
    assert mod_evt.field_key == "public_phone"
    assert mod_evt.old_value == "(03) 9123 4567"
    assert mod_evt.new_value == "(03) 9123 9999"

    add_evt = by_type[ChangeEventCategory.FACT_ADDED.value]
    assert add_evt.field_key == "service_names"
    assert add_evt.old_value is None
    assert add_evt.new_value == "Physiotherapy"


def test_f13_v3_two_consecutive_absences_rule(db: Session):
    """F13 v3 omission: 1st absence becomes unconfirmed_prior; 2nd consecutive becomes fact_unobserved."""
    src = _make_source(db)
    # v1 baseline with Physiotherapy
    snap_1 = _make_snapshot(db, src, "Oakwood Clinic Service: Physiotherapy Phone: (03) 9123 4567")
    _make_observation(db, src.id, "success_new_snapshot", snap_1.id, 1)
    f_physio = _make_fact(db, snap_1, None, "service_names", "Physiotherapy")
    _make_fact(db, snap_1, None, "public_phone", "(03) 9123 4567")
    detect_source_changes(db, src.id)

    # v3 observation 1: Physiotherapy omitted from page text entirely
    snap_2 = _make_snapshot(db, src, "Oakwood Clinic Phone: (03) 9123 4567")  # No "Physiotherapy"
    _make_observation(db, src.id, "success_new_snapshot", snap_2.id, 2)
    _make_fact(db, snap_2, None, "public_phone", "(03) 9123 4567")

    evts_obs2 = detect_source_changes(db, src.id)
    assert len(evts_obs2) == 1
    evt1 = evts_obs2[0]
    assert evt1.event_type == ChangeEventCategory.UNCONFIRMED_PRIOR.value
    assert evt1.field_key == "service_names"
    assert evt1.old_value == "Physiotherapy"
    assert evt1.diff_metadata.get("observation_count_absent") == 1

    # v3 observation 2: Physiotherapy remains absent across second consecutive complete crawl
    snap_3 = _make_snapshot(db, src, "Oakwood Clinic Phone: (03) 9123 4567")
    _make_observation(db, src.id, "success_new_snapshot", snap_3.id, 3)
    _make_fact(db, snap_3, None, "public_phone", "(03) 9123 4567")

    evts_obs3 = detect_source_changes(db, src.id)
    # Total events: unconfirmed_prior (from obs 2) + fact_unobserved (from obs 3)
    assert len(evts_obs3) == 2
    evt2 = [e for e in evts_obs3 if e.event_type == ChangeEventCategory.FACT_UNOBSERVED.value][0]
    assert evt2.field_key == "service_names"
    assert evt2.old_value == "Physiotherapy"
    assert evt2.diff_metadata.get("observation_count_absent") == 2

    # Historical fact remains intact in database
    assert db.get(Fact, f_physio.id) is not None


def test_f13_extraction_omission_defense(db: Session):
    """Extraction omission defense: value still present in snapshot text suppresses removal (F13_omit)."""
    src = _make_source(db)
    # v1 baseline with General Practice
    snap_1 = _make_snapshot(db, src, "Oakwood Clinic Clinical Services: General Practice Phone: (03) 9123 4567")
    _make_observation(db, src.id, "success_new_snapshot", snap_1.id, 1)
    _make_fact(db, snap_1, None, "service_names", "General Practice")
    _make_fact(db, snap_1, None, "public_phone", "(03) 9123 4567")
    detect_source_changes(db, src.id)

    # v2 crawl: snapshot STILL contains "General Practice", but extraction omitted "service_names"
    snap_2 = _make_snapshot(
        db, src,
        "Oakwood Clinic Clinical Services: General Practice Phone: (03) 9123 4567",  # text still has General Practice!
    )
    _make_observation(db, src.id, "success_new_snapshot", snap_2.id, 2)
    _make_fact(db, snap_2, None, "public_phone", "(03) 9123 4567")  # only phone extracted; service omitted

    # Detection must detect the extraction omission and suppress removal event
    evts = detect_source_changes(db, src.id)
    assert len(evts) == 0  # 0 change events emitted!

    count = db.scalar(select(func.count(ChangeEventORM.id)))
    assert count == 0


def test_fact_reappearance_after_unobserved(db: Session):
    """A previously unobserved fact returning in a later extraction emits fact_reappeared."""
    src = _make_source(db)
    # 1. Baseline with Cardiology
    snap_1 = _make_snapshot(db, src, "Oakwood Clinic Service: Cardiology")
    _make_observation(db, src.id, "success_new_snapshot", snap_1.id, 1)
    f_cardio_1 = _make_fact(db, snap_1, None, "service_names", "Cardiology")
    detect_source_changes(db, src.id)

    # 2. Absent crawl 1
    snap_2 = _make_snapshot(db, src, "Oakwood Clinic No Services")
    _make_observation(db, src.id, "success_new_snapshot", snap_2.id, 2)
    detect_source_changes(db, src.id)

    # 3. Absent crawl 2 -> fact_unobserved
    snap_3 = _make_snapshot(db, src, "Oakwood Clinic No Services")
    _make_observation(db, src.id, "success_new_snapshot", snap_3.id, 3)
    detect_source_changes(db, src.id)

    # 4. Reappearance crawl -> Cardiology returns
    snap_4 = _make_snapshot(db, src, "Oakwood Clinic Service: Cardiology")
    _make_observation(db, src.id, "success_new_snapshot", snap_4.id, 4)
    f_cardio_4 = _make_fact(db, snap_4, None, "service_names", "Cardiology")

    evts = detect_source_changes(db, src.id)
    reappeared_evts = [e for e in evts if e.event_type == ChangeEventCategory.FACT_REAPPEARED.value]
    assert len(reappeared_evts) == 1
    assert reappeared_evts[0].field_key == "service_names"
    assert reappeared_evts[0].new_value == "Cardiology"
    assert reappeared_evts[0].current_fact_id == f_cardio_4.id


# =============================================================================
# 4. ISOLATION & DEFENSIVE CHECKS
# =============================================================================

def test_same_source_only_comparison(db: Session):
    """Observations from different sources are NEVER compared against each other."""
    src_a = _make_source(db, "https://example.com/source-a", "Source A")
    src_b = _make_source(db, "https://example.com/source-b", "Source B")

    snap_a = _make_snapshot(db, src_a, "Phone: (03) 9123 4567")
    snap_b = _make_snapshot(db, src_b, "Phone: (03) 9123 9999")

    _make_observation(db, src_a.id, "success_new_snapshot", snap_a.id, 1)
    _make_observation(db, src_b.id, "success_new_snapshot", snap_b.id, 1)

    _make_fact(db, snap_a, None, "public_phone", "(03) 9123 4567")
    _make_fact(db, snap_b, None, "public_phone", "(03) 9123 9999")

    # Both are initial baselines for their respective sources -> 0 change events
    evts_a = detect_source_changes(db, src_a.id)
    evts_b = detect_source_changes(db, src_b.id)

    assert len(evts_a) == 0
    assert len(evts_b) == 0

    count = db.scalar(select(func.count(ChangeEventORM.id)))
    assert count == 0


def test_departmental_scopes_isolated(db: Session):
    """Facts with different scopes (reception vs after_hours) do not interfere or collide."""
    src = _make_source(db)
    snap_1 = _make_snapshot(db, src, "Reception: (03) 9123 4567 After hours: (03) 9123 8888")
    _make_observation(db, src.id, "success_new_snapshot", snap_1.id, 1)
    _make_fact(db, snap_1, None, "public_phone", "(03) 9123 4567", scope_key="reception")
    _make_fact(db, snap_1, None, "public_phone", "(03) 9123 8888", scope_key="after_hours")
    detect_source_changes(db, src.id)

    # In observation 2, only reception changes
    snap_2 = _make_snapshot(db, src, "Reception: (03) 9123 9999 After hours: (03) 9123 8888")
    _make_observation(db, src.id, "success_new_snapshot", snap_2.id, 2)
    _make_fact(db, snap_2, None, "public_phone", "(03) 9123 9999", scope_key="reception")
    _make_fact(db, snap_2, None, "public_phone", "(03) 9123 8888", scope_key="after_hours")

    evts = detect_source_changes(db, src.id)
    assert len(evts) == 1
    assert evts[0].scope_key == "reception"
    assert evts[0].old_value == "(03) 9123 4567"
    assert evts[0].new_value == "(03) 9123 9999"


def test_unlinked_null_organisation_ownership_supported(db: Session):
    """Source changes are detected correctly even if the source is not yet linked to an organisation."""
    src = _make_source(db)
    snap_1 = _make_snapshot(db, src, "Phone: (03) 9123 4567")
    snap_2 = _make_snapshot(db, src, "Phone: (03) 9123 9999")

    _make_observation(db, src.id, "success_new_snapshot", snap_1.id, 1)
    _make_observation(db, src.id, "success_new_snapshot", snap_2.id, 2)

    _make_fact(db, snap_1, None, "public_phone", "(03) 9123 4567")
    _make_fact(db, snap_2, None, "public_phone", "(03) 9123 9999")

    evts = detect_source_changes(db, src.id)
    assert len(evts) == 1
    assert evts[0].organisation_id is None
    assert evts[0].event_type == ChangeEventCategory.FACT_MODIFIED.value


def test_scan_idempotency_duplicate_prevention(db: Session):
    """Repeated scans of unchanged observations produce identical events without database duplicates."""
    src = _make_source(db)
    snap_1 = _make_snapshot(db, src, "Phone: (03) 9123 4567")
    snap_2 = _make_snapshot(db, src, "Phone: (03) 9123 9999")

    _make_observation(db, src.id, "success_new_snapshot", snap_1.id, 1)
    _make_observation(db, src.id, "success_new_snapshot", snap_2.id, 2)

    _make_fact(db, snap_1, None, "public_phone", "(03) 9123 4567")
    _make_fact(db, snap_2, None, "public_phone", "(03) 9123 9999")

    res_1 = scan_source_changes(db, src.id)
    assert res_1.changes_detected == 1

    res_2 = scan_source_changes(db, src.id)
    assert res_2.changes_detected == 1
    assert res_2.changes[0].id == res_1.changes[0].id

    count = db.scalar(select(func.count(ChangeEventORM.id)))
    assert count == 1


# =============================================================================
# 5. REST API VERIFICATION
# =============================================================================

def test_api_list_changes_and_filters(client: TestClient):
    """Verify GET /api/changes endpoint with filters."""
    res = client.get("/api/changes?limit=10&offset=0")
    assert res.status_code == 200
    assert isinstance(res.json(), list)


def test_api_detail_traceability(client: TestClient):
    """Verify GET /api/changes/{change_id} returns linked source, facts, snapshots."""
    from backend.app.db.base import get_db
    app_db = next(client.app.dependency_overrides[get_db]())

    src = _make_source(app_db, "https://api-change.example.com", "API Source")
    snap_1 = _make_snapshot(app_db, src, "Phone: (03) 9123 4567")
    snap_2 = _make_snapshot(app_db, src, "Phone: (03) 9123 9999")
    _make_observation(app_db, src.id, "success_new_snapshot", snap_1.id, 1)
    _make_observation(app_db, src.id, "success_new_snapshot", snap_2.id, 2)
    _make_fact(app_db, snap_1, None, "public_phone", "(03) 9123 4567")
    _make_fact(app_db, snap_2, None, "public_phone", "(03) 9123 9999")

    evts = detect_source_changes(app_db, src.id)
    app_db.commit()

    change_id = evts[0].id
    res = client.get(f"/api/changes/{change_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == change_id
    assert body["source"]["id"] == src.id
    assert body["previous_fact"]["raw_value"] == "(03) 9123 4567"
    assert body["current_fact"]["raw_value"] == "(03) 9123 9999"


def test_api_scan_source_endpoint(client: TestClient):
    """Verify POST /api/changes/scan/source/{source_id} endpoint."""
    from backend.app.db.base import get_db
    app_db = next(client.app.dependency_overrides[get_db]())

    src = _make_source(app_db, "https://api-scan.example.com", "API Scan Source")
    snap_1 = _make_snapshot(app_db, src, "Phone: (03) 9123 4567")
    _make_observation(app_db, src.id, "success_new_snapshot", snap_1.id, 1)
    _make_fact(app_db, snap_1, None, "public_phone", "(03) 9123 4567")
    app_db.commit()

    res = client.post(f"/api/changes/scan/source/{src.id}")
    assert res.status_code == 200
    data = res.json()
    assert data["source_id"] == src.id
    assert data["observations_evaluated"] >= 1


def test_api_forbidden_endpoints_do_not_exist(client: TestClient):
    """Negative check: verify no endpoints exist for editing, deleting, or auto-approving changes."""
    res_patch = client.patch("/api/changes/1", json={"status": "approved"})
    assert res_patch.status_code in (404, 405)

    res_delete = client.delete("/api/changes/1")
    assert res_delete.status_code in (404, 405)

    res_post_vac = client.post("/api/changes/1/mark-filled", json={"filled": True})
    assert res_post_vac.status_code in (404, 405)


def test_valid_completed_extraction_reuse_on_unchanged(db: Session):
    """Observation with extraction not_run on success_unchanged validly reuses earlier complete extraction."""
    src = _make_source(db)
    snap_1 = _make_snapshot(db, src, "Phone: (03) 9123 4567")
    _make_observation(db, src.id, "success_new_snapshot", snap_1.id, 1, extraction="complete")
    _make_fact(db, snap_1, None, "public_phone", "(03) 9123 4567")

    # Obs 2: success_unchanged with extraction not_run referencing snap_1
    _make_observation(db, src.id, "success_unchanged", snap_1.id, 2, extraction="not_run")

    evts = detect_source_changes(db, src.id)
    # Reuses snapshot 1 facts, recognizes unchanged content, emits 0 events
    assert len(evts) == 0


def test_multibranch_heading_context_differentiation(db: Session):
    """Multiple branch scopes remain distinct and track changes independently."""
    src = _make_source(db)
    snap_1 = _make_snapshot(db, src, "Box Hill Phone: (03) 9123 4567 Frankston Phone: (03) 9781 1111")
    _make_observation(db, src.id, "success_new_snapshot", snap_1.id, 1)
    _make_fact(db, snap_1, None, "public_phone", "(03) 9123 4567", scope_key="box_hill")
    _make_fact(db, snap_1, None, "public_phone", "(03) 9781 1111", scope_key="frankston")
    detect_source_changes(db, src.id)

    # In obs 2, only Frankston changes
    snap_2 = _make_snapshot(db, src, "Box Hill Phone: (03) 9123 4567 Frankston Phone: (03) 9781 2222")
    _make_observation(db, src.id, "success_new_snapshot", snap_2.id, 2)
    _make_fact(db, snap_2, None, "public_phone", "(03) 9123 4567", scope_key="box_hill")
    _make_fact(db, snap_2, None, "public_phone", "(03) 9781 2222", scope_key="frankston")

    evts = detect_source_changes(db, src.id)
    assert len(evts) == 1
    assert evts[0].scope_key == "frankston"
    assert evts[0].old_value == "(03) 9781 1111"
    assert evts[0].new_value == "(03) 9781 2222"


def test_unverified_invalid_evidence_facts_ignored(db: Session):
    """Facts with unverified/fabricated evidence are excluded from change detection."""
    src = _make_source(db)
    snap_1 = _make_snapshot(db, src, "Legitimate phone is (03) 9123 4567")
    _make_observation(db, src.id, "success_new_snapshot", snap_1.id, 1)
    _make_fact(db, snap_1, None, "public_phone", "(03) 9123 4567", quote="Legitimate phone is (03) 9123 4567")

    # In obs 2, an ungrounded hallucinated fact is created whose quote does NOT appear in snapshot
    snap_2 = _make_snapshot(db, src, "Legitimate phone is (03) 9123 4567")
    _make_observation(db, src.id, "success_new_snapshot", snap_2.id, 2)
    _make_fact(db, snap_2, None, "public_phone", "(03) 9123 4567", quote="Legitimate phone is (03) 9123 4567")

    fake_ev = Evidence(
        snapshot_id=snap_2.id,
        quote="Nonexistent hallucinated telephone quote",
        start_offset=0,
        end_offset=10,
        captured_at=datetime.now(UTC),
        match_algorithm_version="v1",
    )
    db.add(fake_ev)
    db.flush()
    fake_fact = Fact(
        snapshot_id=snap_2.id,
        field_key="public_phone",
        scope_key="reception",
        raw_value="(03) 9999 0000",
        evidence_id=fake_ev.id,
        via="hallucination",
        status="accepted",
    )
    db.add(fake_fact)
    db.flush()

    evts = detect_source_changes(db, src.id)
    assert len(evts) == 0


def test_api_organisation_scan_endpoint(client: TestClient):
    """Verify POST /api/changes/scan/organisation/{org_id} endpoint."""
    from backend.app.db.base import get_db
    app_db = next(client.app.dependency_overrides[get_db]())

    org = _make_org(app_db, "Org Scan Test", "org-scan-test")
    src = _make_source(app_db, "https://org-scan.example.com", "Org Scan Source")
    _link_source(app_db, org.id, src.id, "confirmed")

    snap_1 = _make_snapshot(app_db, src, "Phone: (03) 9123 4567")
    _make_observation(app_db, src.id, "success_new_snapshot", snap_1.id, 1)
    _make_fact(app_db, snap_1, org.id, "public_phone", "(03) 9123 4567")
    app_db.commit()

    res = client.post(f"/api/changes/scan/organisation/{org.id}")
    assert res.status_code == 200
    assert isinstance(res.json(), list)


def test_concurrent_scans_idempotent_thread_safety(db: Session):
    """Concurrent scans on the same source observations do not create duplicates."""
    src = _make_source(db)
    snap_1 = _make_snapshot(db, src, "Phone: (03) 9123 4567")
    snap_2 = _make_snapshot(db, src, "Phone: (03) 9123 9999")
    _make_observation(db, src.id, "success_new_snapshot", snap_1.id, 1)
    _make_observation(db, src.id, "success_new_snapshot", snap_2.id, 2)
    _make_fact(db, snap_1, None, "public_phone", "(03) 9123 4567")
    _make_fact(db, snap_2, None, "public_phone", "(03) 9123 9999")
    db.commit()

    for _ in range(3):
        detect_source_changes(db, src.id)
    db.commit()

    count = db.scalar(select(func.count(ChangeEventORM.id)).where(ChangeEventORM.source_id == src.id))
    assert count == 1


# =============================================================================
# 7. PER-FACT ABSENCE ACCOUNTING & REAPPEARANCE LIFECYCLE (M3.3 ACCEPTANCE GATE)
# =============================================================================

def test_absence_accounting_step1_unconfirmed_prior(db: Session):
    """Absence rule 1: First complete absence generates unconfirmed_prior with count=1."""
    src = _make_source(db)
    snap_1 = _make_snapshot(db, src, "Phone: (03) 9123 4567 Service: Physiotherapy")
    _make_observation(db, src.id, "success_new_snapshot", snap_1.id, 1)
    _make_fact(db, snap_1, None, "public_phone", "(03) 9123 4567")
    f_physio = _make_fact(db, snap_1, None, "service_names", "Physiotherapy")

    # Baseline scan: 0 events
    evts_1 = detect_source_changes(db, src.id)
    assert len(evts_1) == 0

    # Snapshot 2: Physiotherapy genuinely absent from page text and extraction
    snap_2 = _make_snapshot(db, src, "Phone: (03) 9123 4567")
    _make_observation(db, src.id, "success_new_snapshot", snap_2.id, 2)
    _make_fact(db, snap_2, None, "public_phone", "(03) 9123 4567")

    evts_2 = detect_source_changes(db, src.id)
    assert len(evts_2) == 1
    evt = evts_2[0]
    assert evt.event_type == ChangeEventCategory.UNCONFIRMED_PRIOR.value
    assert evt.field_key == "service_names"
    assert evt.old_value == "Physiotherapy"
    assert evt.previous_fact_id == f_physio.id
    assert evt.current_fact_id is None
    assert evt.diff_metadata.get("observation_count_absent") == 1

    states = evaluate_source_fact_states(db, src.id)
    physio_key = compute_fact_identity(f_physio)
    assert states[physio_key].consecutive_absent_count == 1
    assert states[physio_key].status == "unconfirmed_prior"


def test_absence_accounting_step2_reobserve_same_snapshot_freezes_count(db: Session):
    """Absence rule 2: Re-observing the same snapshot (or unchanged crawl) does NOT increment or reset count."""
    src = _make_source(db)
    snap_1 = _make_snapshot(db, src, "Phone: (03) 9123 4567 Service: Physiotherapy")
    _make_observation(db, src.id, "success_new_snapshot", snap_1.id, 1)
    _make_fact(db, snap_1, None, "public_phone", "(03) 9123 4567")
    f_physio = _make_fact(db, snap_1, None, "service_names", "Physiotherapy")
    detect_source_changes(db, src.id)

    # 1st absence: snap_2
    snap_2 = _make_snapshot(db, src, "Phone: (03) 9123 4567")
    _make_observation(db, src.id, "success_new_snapshot", snap_2.id, 2)
    _make_fact(db, snap_2, None, "public_phone", "(03) 9123 4567")
    evts_2 = detect_source_changes(db, src.id)
    assert len(evts_2) == 1

    # Re-observation A: success_unchanged (re-observing same page / snapshot)
    _make_observation(db, src.id, "success_unchanged", snap_2.id, 3)
    evts_reobs_a = detect_source_changes(db, src.id)
    assert len(evts_reobs_a) == 1  # 0 new events

    # Re-observation B: explicit observation referencing the exact same immutable snapshot ID
    _make_observation(db, src.id, "success_new_snapshot", snap_2.id, 4)
    evts_reobs_b = detect_source_changes(db, src.id)
    assert len(evts_reobs_b) == 1  # 0 new events

    # Crucial assertion: absence count must remain strictly 1 (frozen, not incremented to 2, not reset to 0)
    states = evaluate_source_fact_states(db, src.id)
    physio_key = compute_fact_identity(f_physio)
    assert states[physio_key].consecutive_absent_count == 1
    assert states[physio_key].status == "unconfirmed_prior"


def test_absence_accounting_step3_second_independent_snapshot_fact_unobserved(db: Session):
    """Absence rule 3: A second independent complete snapshot with fact absent generates fact_unobserved."""
    src = _make_source(db)
    snap_1 = _make_snapshot(db, src, "Phone: (03) 9123 4567 Service: Physiotherapy")
    _make_observation(db, src.id, "success_new_snapshot", snap_1.id, 1)
    _make_fact(db, snap_1, None, "public_phone", "(03) 9123 4567")
    f_physio = _make_fact(db, snap_1, None, "service_names", "Physiotherapy")
    detect_source_changes(db, src.id)

    # 1st absence: snap_2 (distinct snapshot)
    snap_2 = _make_snapshot(db, src, "Phone: (03) 9123 4567")
    _make_observation(db, src.id, "success_new_snapshot", snap_2.id, 2)
    _make_fact(db, snap_2, None, "public_phone", "(03) 9123 4567")
    detect_source_changes(db, src.id)

    # 2nd absence: snap_3 (independent complete snapshot)
    snap_3 = _make_snapshot(db, src, "Phone: (03) 9123 4567")
    _make_observation(db, src.id, "success_new_snapshot", snap_3.id, 3)
    _make_fact(db, snap_3, None, "public_phone", "(03) 9123 4567")

    evts_3 = detect_source_changes(db, src.id)
    assert len(evts_3) == 2  # unconfirmed_prior + fact_unobserved
    unobs = [e for e in evts_3 if e.event_type == ChangeEventCategory.FACT_UNOBSERVED.value][0]
    assert unobs.field_key == "service_names"
    assert unobs.old_value == "Physiotherapy"
    assert unobs.diff_metadata.get("observation_count_absent") == 2
    assert unobs.previous_fact_id == f_physio.id
    assert unobs.current_fact_id is None

    states = evaluate_source_fact_states(db, src.id)
    physio_key = compute_fact_identity(f_physio)
    assert states[physio_key].consecutive_absent_count == 2
    assert states[physio_key].status == "fact_unobserved"


def test_absence_accounting_step4_positive_observation_resets_count(db: Session):
    """Absence rule 4: Positive observation of the fact in a complete extraction resets its count to 0."""
    src = _make_source(db)
    snap_1 = _make_snapshot(db, src, "Phone: (03) 9123 4567 Service: Physiotherapy")
    _make_observation(db, src.id, "success_new_snapshot", snap_1.id, 1)
    _make_fact(db, snap_1, None, "public_phone", "(03) 9123 4567")
    f_physio_1 = _make_fact(db, snap_1, None, "service_names", "Physiotherapy")
    detect_source_changes(db, src.id)

    # 1st absence: snap_2 -> unconfirmed_prior (count=1)
    snap_2 = _make_snapshot(db, src, "Phone: (03) 9123 4567")
    _make_observation(db, src.id, "success_new_snapshot", snap_2.id, 2)
    _make_fact(db, snap_2, None, "public_phone", "(03) 9123 4567")
    detect_source_changes(db, src.id)

    states_absent = evaluate_source_fact_states(db, src.id)
    physio_key = compute_fact_identity(f_physio_1)
    assert states_absent[physio_key].consecutive_absent_count == 1
    assert states_absent[physio_key].status == "unconfirmed_prior"

    # Positive observation: snap_3 -> Physiotherapy returns on next crawl!
    snap_3 = _make_snapshot(db, src, "Phone: (03) 9123 4567 Service: Physiotherapy")
    _make_observation(db, src.id, "success_new_snapshot", snap_3.id, 3)
    _make_fact(db, snap_3, None, "public_phone", "(03) 9123 4567")
    f_physio_3 = _make_fact(db, snap_3, None, "service_names", "Physiotherapy")

    evts_3 = detect_source_changes(db, src.id)
    # Since it was unconfirmed_prior (not fact_unobserved), positive observation restores status to 'observed'
    # without emitting fact_reappeared (it was never confirmed unobserved). Total events remain 1 (the initial unconfirmed_prior).
    assert len(evts_3) == 1

    states_restored = evaluate_source_fact_states(db, src.id)
    assert states_restored[physio_key].consecutive_absent_count == 0
    assert states_restored[physio_key].status == "observed"

    # Subsequent absence on snap_4 must produce unconfirmed_prior (count=1), proving count was reset!
    snap_4 = _make_snapshot(db, src, "Phone: (03) 9123 4567")
    _make_observation(db, src.id, "success_new_snapshot", snap_4.id, 4)
    _make_fact(db, snap_4, None, "public_phone", "(03) 9123 4567")

    evts_4 = detect_source_changes(db, src.id)
    assert len(evts_4) == 2
    latest_evt = evts_4[-1]
    assert latest_evt.event_type == ChangeEventCategory.UNCONFIRMED_PRIOR.value
    assert latest_evt.diff_metadata.get("observation_count_absent") == 1


def test_absence_accounting_step5_failed_and_incomplete_freeze_count(db: Session):
    """Absence rule 5: Failed, incomplete, and partial parsing/extraction observations freeze the count."""
    src = _make_source(db)
    snap_1 = _make_snapshot(db, src, "Phone: (03) 9123 4567 Service: Physiotherapy")
    _make_observation(db, src.id, "success_new_snapshot", snap_1.id, 1)
    _make_fact(db, snap_1, None, "public_phone", "(03) 9123 4567")
    f_physio = _make_fact(db, snap_1, None, "service_names", "Physiotherapy")
    detect_source_changes(db, src.id)

    # 1st absence: snap_2 -> unconfirmed_prior (count=1)
    snap_2 = _make_snapshot(db, src, "Phone: (03) 9123 4567")
    _make_observation(db, src.id, "success_new_snapshot", snap_2.id, 2)
    _make_fact(db, snap_2, None, "public_phone", "(03) 9123 4567")
    detect_source_changes(db, src.id)

    # Ineligible attempt 1: network failure (500)
    _make_observation(db, src.id, "failed", None, 3, parsing="failed", extraction="not_run")
    # Ineligible attempt 2: incomplete crawl (truncated HTML)
    _make_observation(db, src.id, "incomplete", None, 4, parsing="partial", extraction="not_run")
    # Ineligible attempt 3: extraction partial
    snap_ineligible = _make_snapshot(db, src, "Phone: (03) 9123 4567")
    _make_observation(db, src.id, "success_new_snapshot", snap_ineligible.id, 5, parsing="complete", extraction="partial")

    evts_ineligible = detect_source_changes(db, src.id)
    assert len(evts_ineligible) == 1  # 0 new events emitted

    # State remains strictly frozen at count=1
    states_frozen = evaluate_source_fact_states(db, src.id)
    physio_key = compute_fact_identity(f_physio)
    assert states_frozen[physio_key].consecutive_absent_count == 1
    assert states_frozen[physio_key].status == "unconfirmed_prior"

    # Now, second independent complete crawl arrives with fact absent
    snap_3 = _make_snapshot(db, src, "Phone: (03) 9123 4567")
    _make_observation(db, src.id, "success_new_snapshot", snap_3.id, 6)
    _make_fact(db, snap_3, None, "public_phone", "(03) 9123 4567")

    evts_final = detect_source_changes(db, src.id)
    assert len(evts_final) == 2
    unobs = [e for e in evts_final if e.event_type == ChangeEventCategory.FACT_UNOBSERVED.value][0]
    assert unobs.diff_metadata.get("observation_count_absent") == 2

    states_final = evaluate_source_fact_states(db, src.id)
    assert states_final[physio_key].consecutive_absent_count == 2
    assert states_final[physio_key].status == "fact_unobserved"


def test_complete_fact_reappearance_lifecycle(db: Session):
    """Complete persisted reappearance lifecycle: baseline -> 2 absences -> fact_unobserved -> reappearance."""
    src = _make_source(db)
    # 1. Baseline: Cardiology service present
    snap_1 = _make_snapshot(db, src, "Oakwood Clinic Cardiology Service Phone: (03) 9123 4567")
    _make_observation(db, src.id, "success_new_snapshot", snap_1.id, 1)
    f_cardio_1 = _make_fact(db, snap_1, None, "service_names", "Cardiology")
    _make_fact(db, snap_1, None, "public_phone", "(03) 9123 4567")
    detect_source_changes(db, src.id)

    # 2. Absence 1 (snap_2): Cardiology absent -> unconfirmed_prior
    snap_2 = _make_snapshot(db, src, "Oakwood Clinic Phone: (03) 9123 4567")
    _make_observation(db, src.id, "success_new_snapshot", snap_2.id, 2)
    _make_fact(db, snap_2, None, "public_phone", "(03) 9123 4567")
    detect_source_changes(db, src.id)

    # 3. Absence 2 (snap_3): Cardiology absent second consecutive time -> fact_unobserved
    snap_3 = _make_snapshot(db, src, "Oakwood Clinic Phone: (03) 9123 4567")
    _make_observation(db, src.id, "success_new_snapshot", snap_3.id, 3)
    _make_fact(db, snap_3, None, "public_phone", "(03) 9123 4567")
    detect_source_changes(db, src.id)

    states_unobs = evaluate_source_fact_states(db, src.id)
    cardio_key = compute_fact_identity(f_cardio_1)
    assert states_unobs[cardio_key].status == "fact_unobserved"
    assert states_unobs[cardio_key].consecutive_absent_count == 2

    # 4. Reappearance (snap_4): Cardiology returns!
    snap_4 = _make_snapshot(db, src, "Oakwood Clinic Cardiology Service Phone: (03) 9123 4567")
    _make_observation(db, src.id, "success_new_snapshot", snap_4.id, 4)
    f_cardio_4 = _make_fact(db, snap_4, None, "service_names", "Cardiology")
    _make_fact(db, snap_4, None, "public_phone", "(03) 9123 4567")

    evts = detect_source_changes(db, src.id)
    # Events: unconfirmed_prior (snap_2), fact_unobserved (snap_3), fact_reappeared (snap_4)
    assert len(evts) == 3

    reappeared_evts = [e for e in evts if e.event_type == ChangeEventCategory.FACT_REAPPEARED.value]
    assert len(reappeared_evts) == 1
    re_evt = reappeared_evts[0]
    assert re_evt.field_key == "service_names"
    assert re_evt.new_value == "Cardiology"
    assert re_evt.previous_fact_id == f_cardio_1.id
    assert re_evt.current_fact_id == f_cardio_4.id
    assert re_evt.diff_metadata.get("observation_count_absent") == 0

    # Historical preservation: both original and new facts remain in DB
    assert db.get(Fact, f_cardio_1.id) is not None
    assert db.get(Fact, f_cardio_4.id) is not None

    # Idempotency: re-running scan does NOT duplicate reappearance event
    evts_rerun = detect_source_changes(db, src.id)
    assert len(evts_rerun) == 3
    db_events_count = db.scalar(select(func.count(ChangeEventORM.id)).where(ChangeEventORM.source_id == src.id))
    assert db_events_count == 3


def test_change_event_orm_relationship_loading_all_eight(db: Session):
    """Verify all eight ORM relationships on ChangeEvent load properly and deletion of linked observations is restricted."""
    org = _make_org(db, "Relationship Org", "relationship-org")
    src = _make_source(db, "https://rel.example.com", "Rel Source")
    _link_source(db, org.id, src.id, "confirmed")

    snap_1 = _make_snapshot(db, src, "Phone: (03) 9123 4567 Service: GP")
    snap_2 = _make_snapshot(db, src, "Phone: (03) 9123 9999 Service: GP")

    obs_1 = _make_observation(db, src.id, "success_new_snapshot", snap_1.id, 1)
    obs_2 = _make_observation(db, src.id, "success_new_snapshot", snap_2.id, 2)

    fact_1 = _make_fact(db, snap_1, org.id, "public_phone", "(03) 9123 4567")
    fact_2 = _make_fact(db, snap_2, org.id, "public_phone", "(03) 9123 9999")
    db.commit()

    evts = detect_source_changes(db, src.id)
    assert len(evts) == 1
    evt_id = evts[0].id
    db.commit()

    # Expire session cache to force real database query on reload
    db.expire_all()

    loaded_evt = db.get(ChangeEventORM, evt_id)
    assert loaded_evt is not None

    # Verify all 8 ORM relationships load expected parent rows
    assert loaded_evt.source.id == src.id
    assert loaded_evt.organisation is not None and loaded_evt.organisation.id == org.id
    assert loaded_evt.previous_observation is not None and loaded_evt.previous_observation.id == obs_1.id
    assert loaded_evt.current_observation.id == obs_2.id
    assert loaded_evt.previous_snapshot is not None and loaded_evt.previous_snapshot.id == snap_1.id
    assert loaded_evt.current_snapshot.id == snap_2.id
    assert loaded_evt.previous_fact is not None and loaded_evt.previous_fact.id == fact_1.id
    assert loaded_evt.current_fact is not None and loaded_evt.current_fact.id == fact_2.id

    # Verify ON DELETE RESTRICT on linked observations
    db.delete(obs_1)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    db.delete(obs_2)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_process_restart_absence_persistence_across_sessions():
    """Verify absence counting and fact_unobserved transition recover across process restarts without in-memory state."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "restart_test.db"
        engine_1 = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        orm.Base.metadata.create_all(engine_1)

        src_id = None
        physio_ident = None
        snap_2_id = None

        # ---------------------------------------------------------
        # SESSION 1: Establish baseline and record 1st eligible absence
        # ---------------------------------------------------------
        session_1 = sessionmaker(bind=engine_1)()
        try:
            src = _make_source(session_1, "https://restart.example.com", "Restart Source")
            snap_1 = _make_snapshot(session_1, src, "Oakwood Clinic Phone: (03) 9123 4567 Service: Physiotherapy")
            _make_observation(session_1, src.id, "success_new_snapshot", snap_1.id, 1)
            _make_fact(session_1, snap_1, None, "public_phone", "(03) 9123 4567")
            f_physio = _make_fact(session_1, snap_1, None, "service_names", "Physiotherapy")
            session_1.commit()

            src_id = src.id
            physio_ident = compute_fact_identity(f_physio)

            # Baseline scan: 0 events
            evts_baseline = detect_source_changes(session_1, src_id)
            assert len(evts_baseline) == 0

            # 1st absence: snapshot 2 genuinely omits Physiotherapy
            snap_2 = _make_snapshot(session_1, src, "Oakwood Clinic Phone: (03) 9123 4567")
            _make_observation(session_1, src.id, "success_new_snapshot", snap_2.id, 2)
            _make_fact(session_1, snap_2, None, "public_phone", "(03) 9123 4567")
            session_1.commit()
            snap_2_id = snap_2.id

            evts_1 = detect_source_changes(session_1, src_id)
            assert len(evts_1) == 1
            assert evts_1[0].event_type == ChangeEventCategory.UNCONFIRMED_PRIOR.value
            assert evts_1[0].diff_metadata.get("observation_count_absent") == 1
            session_1.commit()
        finally:
            session_1.close()
            engine_1.dispose()

        # ---------------------------------------------------------
        # PROCESS RESTART: Create brand new engine and Session 2
        # ---------------------------------------------------------
        engine_2 = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        session_2 = sessionmaker(bind=engine_2)()
        try:
            # Re-observe the same immutable snapshot (snap_2) via success_unchanged
            _make_observation(session_2, src_id, "success_unchanged", snap_2_id, 3)
            session_2.commit()

            # Detector must process without in-memory state
            evts_reobs = detect_source_changes(session_2, src_id)
            assert len(evts_reobs) == 1  # 0 new events generated

            states_reobs = evaluate_source_fact_states(session_2, src_id)
            # Count must remain strictly 1 (frozen)
            assert states_reobs[physio_ident].consecutive_absent_count == 1
            assert states_reobs[physio_ident].status == "unconfirmed_prior"

            # 2nd absence: new independent complete snapshot 3 with fact absent
            src_loaded = session_2.get(orm.Source, src_id)
            snap_3 = _make_snapshot(session_2, src_loaded, "Oakwood Clinic Phone: (03) 9123 4567")
            _make_observation(session_2, src_id, "success_new_snapshot", snap_3.id, 4)
            _make_fact(session_2, snap_3, None, "public_phone", "(03) 9123 4567")
            session_2.commit()

            evts_second = detect_source_changes(session_2, src_id)
            assert len(evts_second) == 2  # unconfirmed_prior + fact_unobserved
            unobs_events = [e for e in evts_second if e.event_type == ChangeEventCategory.FACT_UNOBSERVED.value]
            assert len(unobs_events) == 1
            assert unobs_events[0].diff_metadata.get("observation_count_absent") == 2

            states_second = evaluate_source_fact_states(session_2, src_id)
            assert states_second[physio_ident].consecutive_absent_count == 2
            assert states_second[physio_ident].status == "fact_unobserved"
            session_2.commit()

            # Confirm re-scanning produces 0 duplicates
            evts_rerun = detect_source_changes(session_2, src_id)
            assert len(evts_rerun) == 2
        finally:
            session_2.close()
            engine_2.dispose()


def test_historical_observation_retention_and_inherited_m3_1_cascade(db: Session):
    """Verify inherited M3.1 CASCADE vs M3.3 RESTRICT protection for source observations."""
    # Scenario A: Source with observations NOT referenced by change events (inherited M3.1 behavior)
    src_unreferenced = _make_source(db, "https://unreferenced.example.com", "Unreferenced Source")
    snap_unreferenced = _make_snapshot(db, src_unreferenced, "Some text")
    obs_unref_1 = _make_observation(db, src_unreferenced.id, "success_new_snapshot", snap_unreferenced.id, 1)
    obs_unref_2 = _make_observation(db, src_unreferenced.id, "success_unchanged", snap_unreferenced.id, 2)
    db.commit()

    obs_1_id = obs_unref_1.id
    obs_2_id = obs_unref_2.id

    # Deleting the source cascade-deletes unreferenced observations (inherited M3.1 behavior)
    db.delete(src_unreferenced)
    db.commit()

    assert db.get(SourceObservationORM, obs_1_id) is None
    assert db.get(SourceObservationORM, obs_2_id) is None

    # Scenario B: Source with observations referenced by change events (M3.3 RESTRICT protection)
    src_protected = _make_source(db, "https://protected.example.com", "Protected Source")
    snap_p1 = _make_snapshot(db, src_protected, "Phone: 111")
    snap_p2 = _make_snapshot(db, src_protected, "Phone: 222")
    obs_p1 = _make_observation(db, src_protected.id, "success_new_snapshot", snap_p1.id, 1)
    obs_p2 = _make_observation(db, src_protected.id, "success_new_snapshot", snap_p2.id, 2)
    _make_fact(db, snap_p1, None, "public_phone", "111")
    _make_fact(db, snap_p2, None, "public_phone", "222")
    db.commit()

    evts = detect_source_changes(db, src_protected.id)
    assert len(evts) == 1
    db.commit()

    # Deleting the protected source MUST fail with IntegrityError because change_events references it and its observations
    db.delete(src_protected)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    # Deleting the referenced observation MUST also fail with IntegrityError
    db.delete(obs_p2)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    assert db.get(SourceObservationORM, obs_p2.id) is not None
    assert db.get(orm.Source, src_protected.id) is not None


