"""Comprehensive M3.5 backend test suite: Healthcare Practice & Workforce Vacancy Intelligence.

Verifies:
1. Migration M3_004:
   - Additive migration creating opportunities table with ON DELETE RESTRICT.
   - Idempotency and upgrade of legacy CASCADE definitions.
   - Rollback capability.
   - Foreign-key integrity: restricts cascade deletion of referenced organisations/sources/snapshots.
2. Synthetic F14 Ground Truth Lifecycle:
   - F14_v1: Initial observation of GP vacancy with explicit Job Reference ID (status=advertised, absences=0).
   - F14_v2: Title and remuneration wording modified (status=changed, duplicate_created=False).
   - F14_v3a: First eligible absence (status=unconfirmed_prior, absences=1, marked_as_filled=False).
   - F14_v3b: Second consecutive eligible absence (status=no_longer_observed, absences=2, marked_as_filled=False).
   - F14_v3c: Explicit closure notice (status=explicitly_closed, verbatim quotation preserved).
   - F14_v3d: Reappearance (status=advertised, absences=0, duplicate_created=False).
3. Absence Accounting & Ineligible Crawl Freezing:
   - Failed crawl freezes absence counters and suppresses transitions.
   - Incomplete crawl freezes counters.
   - Partial parsing or partial extraction freezes counters.
   - Unchanged snapshot heartbeat (success_unchanged) freezes counters.
   - Extraction omission defense: vacancy omitted from candidate extraction but still visible in snapshot DOM text/HTML suppresses disappearance and freezes counters.
4. Deterministic Identity Resolution:
   - Job title change preserves identity (title omitted from composite identity).
   - Remuneration change preserves identity.
   - Branch isolation preserves distinct opportunities across locations.
   - Explicit ad ID takes highest precedence.
5. Invariants & Strict Prohibitions:
   - Status 'filled' or 'hired' is strictly forbidden by architecture.
   - Explicit closure requires verbatim quotation.
6. REST API Endpoints:
   - GET /api/opportunities with filters (role_category, organisation_id, source_id, status, pagination).
   - GET /api/opportunities/{id} with expanded details and 404 error handling.
   - POST /api/opportunities/scan/{source_id} with deterministic execution and 404 error handling.
   - Negative check: zero endpoints for deletion or mark-as-filled.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api.main import app
from backend.app.db import orm
from backend.app.db.base import get_db
from backend.app.db.migrations import (
    inspect_m3_004_schema,
    migrate_m3_004_opportunities,
    rollback_m3_004_opportunities,
)
from backend.app.db.orm import (
    ChangeEvent as ChangeEventORM,
    Opportunity as OpportunityORM,
    Organisation,
    OrganisationSourceLink,
    PageSnapshot,
    Source,
    SourceObservation as SourceObservationORM,
)
from backend.app.intelligence.opportunity_detector import (
    OpportunityCandidate,
    classify_role_category,
    evaluate_opportunity_staleness,
    extract_closure_notices,
    extract_explicit_ad_id,
    extract_explicit_closing_date,
    extract_opportunity_candidates,
    is_observation_eligible_for_vacancies,
    is_vacancy_extraction_omission,
    process_opportunity_observation,
    scan_organisation_opportunities,
    scan_source_opportunities,
    verify_closure_evidence,
    verify_opportunity_evidence,
)
from backend.app.intelligence.schemas import (
    ExtractionCompleteness,
    ObservationOutcome,
    Opportunity,
    OpportunityStatus,
    ParsingCompleteness,
    RoleCategory,
)
from backend.app.parsers.text_extract import parse

M3_CORPUS_DIR = Path(__file__).resolve().parent / "fixtures" / "m3_corpus"


# =============================================================================
# FIXTURES & HELPERS
# =============================================================================

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
def client(db: Session):
    """FastAPI TestClient with overridden get_db dependency."""
    def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.clear()


def _make_org(db: Session, name: str = "Test Medical", slug: str | None = None, postcode: str = "3000", state: str = "VIC") -> Organisation:
    slug = slug or f"test-org-{uuid.uuid4().hex[:6]}"
    org = Organisation(name=name, slug=slug, postcode=postcode, state=state)
    db.add(org)
    db.flush()
    return org


def _make_source(db: Session, url: str | None = None, label: str = "Careers Page", org_id: int | None = None) -> Source:
    url = url or f"https://example-{uuid.uuid4().hex[:6]}.org/careers"
    src = Source(
        organisation_id=org_id,
        kind="fixture",
        label=label,
        url=url,
        domain="example.org",
        page_type="careers",
        policy_status="allowed",
    )
    db.add(src)
    db.flush()
    return src


def _link_source(db: Session, org_id: int, source_id: int, rel_type: str = "official_careers", status: str = "verified") -> OrganisationSourceLink:
    link = OrganisationSourceLink(
        organisation_id=org_id,
        source_id=source_id,
        relationship_type=rel_type,
        verification_status=status,
    )
    db.add(link)
    db.flush()
    return link


def _make_snapshot(db: Session, source: Source, text: str, html: str = "", title: str = "Careers", headings: list | None = None) -> PageSnapshot:
    snap = PageSnapshot(
        source_id=source.id,
        title=title,
        text=text,
        content_hash=uuid.uuid4().hex,
        parser_version="v1",
        headings=headings or [],
    )
    if html:
        setattr(snap, "html", html)
    db.add(snap)
    db.flush()
    return snap


def _make_observation(
    db: Session,
    source_id: int,
    outcome: str = "success_new_snapshot",
    snapshot_id: int | None = None,
    seq: int = 1,
    parsing: str = "complete",
    extraction: str = "complete",
) -> SourceObservationORM:
    obs = SourceObservationORM(
        source_id=source_id,
        observation_sequence=seq,
        outcome=outcome,
        snapshot_id=snapshot_id,
        parsing_completeness=parsing,
        extraction_completeness=extraction,
    )
    db.add(obs)
    db.flush()
    return obs


def _load_corpus_fixture(rel_path: str) -> dict:
    with open(M3_CORPUS_DIR / rel_path, "r", encoding="utf-8") as f:
        return json.load(f)


# =============================================================================
# 1. DATABASE MIGRATION M3_004 TESTS
# =============================================================================

def test_m3_004_migration_on_fresh_db():
    """Verify additive migration M3_004 creates opportunities table with correct schema."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name

    try:
        # Create minimal parent tables
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("CREATE TABLE organisations (id INTEGER PRIMARY KEY, name TEXT)")
        cur.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY, url TEXT)")
        cur.execute("CREATE TABLE page_snapshots (id INTEGER PRIMARY KEY, text TEXT)")
        conn.commit()
        conn.close()

        res = migrate_m3_004_opportunities(db_path)
        assert res["status"] == "success"
        assert "opportunities" in res["tables_created"]

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        info = inspect_m3_004_schema(cur)
        assert info["opportunities"] is True
        cols = info["columns"]
        assert "id" in cols
        assert "organisation_id" in cols
        assert "source_id" in cols
        assert "first_snapshot_id" in cols
        assert "last_snapshot_id" in cols
        assert "role_category" in cols
        assert "role_title" in cols
        assert "verbatim_wording" in cols
        assert "explicit_ad_id" in cols
        assert "status" in cols
        assert "closure_evidence_quote" in cols
        assert "consecutive_absences" in cols
        assert "first_seen_at" in cols
        assert "last_seen_at" in cols
        assert "closed_at" in cols

        # Check indexes
        cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='opportunities'")
        indexes = {row[0] for row in cur.fetchall()}
        assert "ix_opportunities_organisation_id" in indexes
        assert "ix_opportunities_role_category" in indexes
        assert "ix_opportunities_status" in indexes
        assert "ix_opportunities_explicit_ad_id" in indexes
        assert "ix_opportunities_source_id" in indexes
        conn.close()
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_m3_004_migration_idempotent():
    """Verify running M3_004 migration multiple times is safe and returns skipped."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name

    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("CREATE TABLE organisations (id INTEGER PRIMARY KEY, name TEXT)")
        cur.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY, url TEXT)")
        cur.execute("CREATE TABLE page_snapshots (id INTEGER PRIMARY KEY, text TEXT)")
        conn.commit()
        conn.close()

        res1 = migrate_m3_004_opportunities(db_path)
        assert res1["status"] == "success"

        res2 = migrate_m3_004_opportunities(db_path)
        assert res2["status"] == "skipped"
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_m3_004_migration_upgrades_legacy_cascade_to_restrict():
    """Verify legacy table with CASCADE is upgraded to RESTRICT while preserving data."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name

    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("CREATE TABLE organisations (id INTEGER PRIMARY KEY, name TEXT)")
        cur.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY, url TEXT)")
        cur.execute("CREATE TABLE page_snapshots (id INTEGER PRIMARY KEY, text TEXT)")
        cur.execute("""
            CREATE TABLE opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organisation_id INTEGER NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
                source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                first_snapshot_id INTEGER NOT NULL REFERENCES page_snapshots(id) ON DELETE CASCADE,
                last_snapshot_id INTEGER NOT NULL REFERENCES page_snapshots(id) ON DELETE CASCADE,
                role_category VARCHAR(64) NOT NULL,
                role_title VARCHAR(255) NOT NULL,
                verbatim_wording TEXT NOT NULL,
                contact_channel VARCHAR(255),
                explicit_ad_id VARCHAR(128),
                status VARCHAR(32) NOT NULL DEFAULT 'advertised',
                closure_evidence_quote TEXT,
                first_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                closed_at DATETIME,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Insert test row
        cur.execute("INSERT INTO organisations VALUES (1, 'Org 1')")
        cur.execute("INSERT INTO sources VALUES (1, 'https://test.org')")
        cur.execute("INSERT INTO page_snapshots VALUES (1, 'text')")
        cur.execute("""
            INSERT INTO opportunities (id, organisation_id, source_id, first_snapshot_id, last_snapshot_id, role_category, role_title, verbatim_wording, explicit_ad_id)
            VALUES (1, 1, 1, 1, 1, 'gp', 'VR GP', 'Wording', 'GP-01')
        """)
        conn.commit()
        conn.close()

        res = migrate_m3_004_opportunities(db_path)
        assert res["status"] == "success"
        assert res.get("action") == "upgraded"

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='opportunities'")
        table_sql = cur.fetchone()[0].upper()
        assert "CASCADE" not in table_sql
        assert "RESTRICT" in table_sql

        # Verify row preserved and consecutive_absences added
        cur.execute("SELECT id, role_title, explicit_ad_id, consecutive_absences FROM opportunities WHERE id=1")
        row = cur.fetchone()
        assert row[0] == 1
        assert row[1] == "VR GP"
        assert row[2] == "GP-01"
        assert row[3] == 0  # default for new column
        conn.close()
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_m3_004_rollback():
    """Verify rollback drops opportunities table safely."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name

    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("CREATE TABLE organisations (id INTEGER PRIMARY KEY, name TEXT)")
        cur.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY, url TEXT)")
        cur.execute("CREATE TABLE page_snapshots (id INTEGER PRIMARY KEY, text TEXT)")
        conn.commit()
        conn.close()

        migrate_m3_004_opportunities(db_path)
        res_rollback = rollback_m3_004_opportunities(db_path)
        assert res_rollback["status"] == "success"

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        info = inspect_m3_004_schema(cur)
        assert info["opportunities"] is False
        conn.close()
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_foreign_key_restricts(db: Session):
    """Verify ON DELETE RESTRICT prevents deleting referenced organisations, sources, or snapshots."""
    org = _make_org(db, "Restrict Practice", "restrict-practice-3000")
    src = _make_source(db, org_id=org.id)
    _link_source(db, org.id, src.id)
    snap = _make_snapshot(db, src, "Careers opening")
    obs = _make_observation(db, src.id, snapshot_id=snap.id, seq=1)

    cand = OpportunityCandidate(
        role_category=RoleCategory.GP,
        role_title="General Practitioner",
        verbatim_wording="Looking for GP",
        explicit_ad_id="GP-RESTRICT-01",
    )
    opps = process_opportunity_observation(db, obs, candidates=[cand])
    assert len(opps) == 1
    db.commit()

    # Attempting to delete organisation referenced by opportunity must fail
    with pytest.raises(IntegrityError):
        db.delete(org)
        db.flush()
    db.rollback()

    # Attempting to delete source referenced by opportunity must fail
    with pytest.raises(IntegrityError):
        db.delete(src)
        db.flush()
    db.rollback()

    # Attempting to delete snapshot referenced by opportunity must fail
    with pytest.raises(IntegrityError):
        db.delete(snap)
        db.flush()
    db.rollback()


# =============================================================================
# 2. SYNTHETIC F14 GROUND TRUTH LIFECYCLE EVALUATION
# =============================================================================

def test_f14_full_lifecycle_sequence(db: Session):
    """Comprehensive end-to-end evaluation of F14 workforce vacancy lifecycle:

    F14_v1 (advertised) -> F14_v2 (changed) -> F14_v3a (unconfirmed_prior)
    -> F14_v3b (no_longer_observed) -> F14_v3c (explicitly_closed) -> F14_v3d (reappeared)
    """
    v1_data = _load_corpus_fixture("group_delta_vacancies/F14_v1_advertised.json")
    v2_data = _load_corpus_fixture("group_delta_vacancies/F14_v2_changed.json")
    v3a_data = _load_corpus_fixture("group_delta_vacancies/F14_v3a_absent_once.json")
    v3b_data = _load_corpus_fixture("group_delta_vacancies/F14_v3b_absent_twice.json")
    v3c_data = _load_corpus_fixture("group_delta_vacancies/F14_v3c_explicit_closure.json")
    v3d_data = _load_corpus_fixture("group_delta_vacancies/F14_v3d_reappeared.json")

    org = _make_org(db, name=v1_data["ground_truth_organisation"]["name"], slug=v1_data["ground_truth_organisation"]["slug"])
    src = _make_source(db, url=v1_data["source_identity"]["url"], label="St Jude Careers", org_id=org.id)
    _link_source(db, org.id, src.id)

    # -------------------------------------------------------------------------
    # Step 1: F14_v1 Initial Advertisement
    # -------------------------------------------------------------------------
    p1 = parse(v1_data["html"])
    snap1 = _make_snapshot(db, src, p1.text, html=v1_data["html"], title=p1.title, headings=[{"level": h.level, "text": h.text, "start_offset": h.start_offset, "end_offset": h.end_offset} for h in p1.headings])
    obs1 = _make_observation(db, src.id, snapshot_id=snap1.id, seq=1)

    opps1 = process_opportunity_observation(db, obs1)
    assert len(opps1) == 1
    opp = opps1[0]
    assert opp.status == OpportunityStatus.ADVERTISED.value
    assert opp.role_category == RoleCategory.GP.value
    assert opp.role_title == "VR General Practitioner"
    assert opp.explicit_ad_id == "GP-2026-04"
    assert opp.consecutive_absences == 0
    assert opp.contact_channel == "hr@stjude-medical.example.org"
    assert "70% billings" in opp.verbatim_wording
    initial_opp_id = opp.id

    # -------------------------------------------------------------------------
    # Step 2: F14_v2 Title & Remuneration Modification
    # -------------------------------------------------------------------------
    p2 = parse(v2_data["html"])
    snap2 = _make_snapshot(db, src, p2.text, html=v2_data["html"], title=p2.title, headings=[{"level": h.level, "text": h.text, "start_offset": h.start_offset, "end_offset": h.end_offset} for h in p2.headings])
    obs2 = _make_observation(db, src.id, snapshot_id=snap2.id, seq=2)

    opps2 = process_opportunity_observation(db, obs2)
    assert len(opps2) == 1, "Must not create a duplicate opportunity on modification!"
    opp = opps2[0]
    assert opp.id == initial_opp_id
    assert opp.status == OpportunityStatus.CHANGED.value
    assert opp.role_title == "VR General Practitioner — Urgent Start"
    assert "72% billings" in opp.verbatim_wording
    assert opp.consecutive_absences == 0
    assert opp.first_snapshot_id == snap1.id
    assert opp.last_snapshot_id == snap2.id

    # -------------------------------------------------------------------------
    # Step 3: F14_v3a Absent on 1st Complete Observation
    # -------------------------------------------------------------------------
    p3a = parse(v3a_data["html"])
    snap3a = _make_snapshot(db, src, p3a.text, html=v3a_data["html"], title=p3a.title, headings=[{"level": h.level, "text": h.text, "start_offset": h.start_offset, "end_offset": h.end_offset} for h in p3a.headings])
    obs3a = _make_observation(db, src.id, snapshot_id=snap3a.id, seq=3)

    opps3a = process_opportunity_observation(db, obs3a)
    assert len(opps3a) == 1
    opp = opps3a[0]
    assert opp.id == initial_opp_id
    assert opp.status == OpportunityStatus.UNCONFIRMED_PRIOR.value
    assert opp.consecutive_absences == 1
    # STRICT ARCHITECTURAL INVARIANT: status 'filled' or 'hired' is forbidden
    assert opp.status != "filled"
    assert opp.status != "hired"

    # -------------------------------------------------------------------------
    # Step 4: F14_v3b Absent on 2nd Consecutive Complete Observation
    # -------------------------------------------------------------------------
    p3b = parse(v3b_data["html"])
    snap3b = _make_snapshot(db, src, p3b.text, html=v3b_data["html"], title=p3b.title, headings=[{"level": h.level, "text": h.text, "start_offset": h.start_offset, "end_offset": h.end_offset} for h in p3b.headings])
    obs3b = _make_observation(db, src.id, snapshot_id=snap3b.id, seq=4)

    opps3b = process_opportunity_observation(db, obs3b)
    assert len(opps3b) == 1
    opp = opps3b[0]
    assert opp.id == initial_opp_id
    assert opp.status == OpportunityStatus.NO_LONGER_OBSERVED.value
    assert opp.consecutive_absences == 2
    assert opp.status != "filled"
    assert opp.status != "hired"

    # -------------------------------------------------------------------------
    # Step 5: F14_v3c Explicit Closure Notice
    # -------------------------------------------------------------------------
    p3c = parse(v3c_data["html"])
    snap3c = _make_snapshot(db, src, p3c.text, html=v3c_data["html"], title=p3c.title, headings=[{"level": h.level, "text": h.text, "start_offset": h.start_offset, "end_offset": h.end_offset} for h in p3c.headings])
    obs3c = _make_observation(db, src.id, snapshot_id=snap3c.id, seq=5)

    opps3c = process_opportunity_observation(db, obs3c)
    assert len(opps3c) == 1
    opp = opps3c[0]
    assert opp.id == initial_opp_id
    assert opp.status == OpportunityStatus.EXPLICITLY_CLOSED.value
    assert opp.closure_evidence_quote == "Applications for VR GP role (Ref: GP-2026-04) are now closed."
    assert opp.closed_at is not None
    assert opp.status != "filled"
    assert opp.status != "hired"

    # -------------------------------------------------------------------------
    # Step 6: F14_v3d Reappearance
    # -------------------------------------------------------------------------
    p3d = parse(v3d_data["html"])
    snap3d = _make_snapshot(db, src, p3d.text, html=v3d_data["html"], title=p3d.title, headings=[{"level": h.level, "text": h.text, "start_offset": h.start_offset, "end_offset": h.end_offset} for h in p3d.headings])
    obs3d = _make_observation(db, src.id, snapshot_id=snap3d.id, seq=6)

    opps3d = process_opportunity_observation(db, obs3d)
    assert len(opps3d) == 1, "Reappeared vacancy must restore existing row without duplicate creation!"
    opp = opps3d[0]
    assert opp.id == initial_opp_id
    assert opp.status == OpportunityStatus.ADVERTISED.value
    assert opp.consecutive_absences == 0
    assert opp.closure_evidence_quote is None
    assert opp.closed_at is None
    assert opp.last_snapshot_id == snap3d.id


# =============================================================================
# 3. ABSENCE ACCOUNTING & INELIGIBLE OBSERVATION FREEZING
# =============================================================================

def test_failed_and_incomplete_crawl_freezes_absence_counter(db: Session):
    """Verify crawler failures and incomplete observations freeze absence counter."""
    org = _make_org(db, "Freeze Practice", "freeze-practice-3000")
    src = _make_source(db, org_id=org.id)
    _link_source(db, org.id, src.id)

    snap1 = _make_snapshot(db, src, "VR General Practitioner Ref: GP-101 seeking GP")
    obs1 = _make_observation(db, src.id, snapshot_id=snap1.id, seq=1)
    cand = OpportunityCandidate(
        role_category=RoleCategory.GP,
        role_title="VR General Practitioner",
        verbatim_wording="seeking GP",
        explicit_ad_id="GP-101",
    )
    opps = process_opportunity_observation(db, obs1, candidates=[cand])
    assert opps[0].status == "advertised"
    assert opps[0].consecutive_absences == 0

    # Crawl 2: Failed observation (crawler timeout)
    obs2 = _make_observation(db, src.id, outcome="failed", snapshot_id=None, seq=2, parsing="not_run", extraction="not_run")
    opps = process_opportunity_observation(db, obs2, candidates=[])
    assert opps[0].status == "advertised"
    assert opps[0].consecutive_absences == 0

    # Crawl 3: Incomplete observation (partial network transfer)
    obs3 = _make_observation(db, src.id, outcome="incomplete", snapshot_id=None, seq=3, parsing="not_run", extraction="not_run")
    opps = process_opportunity_observation(db, obs3, candidates=[])
    assert opps[0].status == "advertised"
    assert opps[0].consecutive_absences == 0


def test_partial_parsing_and_extraction_freezes_absence_counter(db: Session):
    """Verify partial parsing or partial extraction freezes absence counter."""
    org = _make_org(db, "Partial Practice", "partial-practice-3000")
    src = _make_source(db, org_id=org.id)
    _link_source(db, org.id, src.id)

    snap1 = _make_snapshot(db, src, "VR General Practitioner Ref: GP-102 seeking GP")
    obs1 = _make_observation(db, src.id, snapshot_id=snap1.id, seq=1)
    cand = OpportunityCandidate(
        role_category=RoleCategory.GP,
        role_title="VR General Practitioner",
        verbatim_wording="seeking GP",
        explicit_ad_id="GP-102",
    )
    opps = process_opportunity_observation(db, obs1, candidates=[cand])
    assert opps[0].status == "advertised"

    # Crawl 2: Partial parsing
    snap2 = _make_snapshot(db, src, "Malformed HTML")
    obs2 = _make_observation(db, src.id, snapshot_id=snap2.id, seq=2, parsing="partial", extraction="complete")
    opps = process_opportunity_observation(db, obs2, candidates=[])
    assert opps[0].status == "advertised"
    assert opps[0].consecutive_absences == 0

    # Crawl 3: Partial extraction
    snap3 = _make_snapshot(db, src, "Valid HTML")
    obs3 = _make_observation(db, src.id, snapshot_id=snap3.id, seq=3, parsing="complete", extraction="partial")
    opps = process_opportunity_observation(db, obs3, candidates=[])
    assert opps[0].status == "advertised"
    assert opps[0].consecutive_absences == 0


def test_unchanged_snapshot_heartbeat_freezes_absence_counter(db: Session):
    """Verify success_unchanged heartbeat does not advance absence counter."""
    org = _make_org(db, "Heartbeat Practice", "heartbeat-practice-3000")
    src = _make_source(db, org_id=org.id)
    _link_source(db, org.id, src.id)

    snap1 = _make_snapshot(db, src, "VR General Practitioner Ref: GP-103 seeking GP")
    obs1 = _make_observation(db, src.id, snapshot_id=snap1.id, seq=1)
    cand = OpportunityCandidate(
        role_category=RoleCategory.GP,
        role_title="VR General Practitioner",
        verbatim_wording="seeking GP",
        explicit_ad_id="GP-103",
    )
    opps = process_opportunity_observation(db, obs1, candidates=[cand])
    assert opps[0].status == "advertised"

    # Crawl 2: success_unchanged heartbeat
    obs2 = _make_observation(db, src.id, outcome="success_unchanged", snapshot_id=snap1.id, seq=2)
    opps = process_opportunity_observation(db, obs2, candidates=[])
    assert opps[0].status == "advertised"
    assert opps[0].consecutive_absences == 0


def test_extraction_omission_defense_suppresses_removal(db: Session):
    """Verify that when a vacancy is omitted by extraction but remains in DOM text, disappearance is suppressed."""
    org = _make_org(db, "Omission Practice", "omission-practice-3000")
    src = _make_source(db, org_id=org.id)
    _link_source(db, org.id, src.id)

    snap1 = _make_snapshot(db, src, "VR General Practitioner Ref: GP-104 seeking GP")
    obs1 = _make_observation(db, src.id, snapshot_id=snap1.id, seq=1)
    cand = OpportunityCandidate(
        role_category=RoleCategory.GP,
        role_title="VR General Practitioner",
        verbatim_wording="seeking GP",
        explicit_ad_id="GP-104",
    )
    opps = process_opportunity_observation(db, obs1, candidates=[cand])
    opp = opps[0]
    assert opp.status == "advertised"

    # Crawl 2: Extractor returns 0 candidates, BUT raw snapshot text STILL contains 'GP-104'
    snap2 = _make_snapshot(db, src, "VR General Practitioner Ref: GP-104 seeking GP (extractor missed this)")
    obs2 = _make_observation(db, src.id, snapshot_id=snap2.id, seq=2)

    # Empty candidate list passed
    opps = process_opportunity_observation(db, obs2, candidates=[])
    opp = opps[0]
    assert opp.status == "advertised", "Omission defense must preserve status"
    assert opp.consecutive_absences == 0, "Omission defense must freeze absence counter"


# =============================================================================
# 4. DETERMINISTIC IDENTITY RESOLUTION TESTS
# =============================================================================

def test_title_change_preserves_opportunity_identity(db: Session):
    """Verify modifying role title (e.g. adding Senior/Urgent) preserves identity without duplicate."""
    org = _make_org(db, "Title Change Practice", "title-change-3000")
    src = _make_source(db, org_id=org.id)
    _link_source(db, org.id, src.id)

    snap1 = _make_snapshot(db, src, "Practice Nurse opening")
    obs1 = _make_observation(db, src.id, snapshot_id=snap1.id, seq=1)
    cand1 = OpportunityCandidate(
        role_category=RoleCategory.NURSE,
        role_title="Practice Nurse",
        verbatim_wording="Looking for friendly practice nurse",
        explicit_ad_id=None,  # No explicit ad ID -> relies on composite identity
    )
    opps1 = process_opportunity_observation(db, obs1, candidates=[cand1])
    assert len(opps1) == 1
    opp_id = opps1[0].id

    # Crawl 2: Title changes to "Senior Practice Nurse"
    snap2 = _make_snapshot(db, src, "Senior Practice Nurse opening")
    obs2 = _make_observation(db, src.id, snapshot_id=snap2.id, seq=2)
    cand2 = OpportunityCandidate(
        role_category=RoleCategory.NURSE,
        role_title="Senior Practice Nurse",
        verbatim_wording="Looking for senior practice nurse",
        explicit_ad_id=None,
    )
    opps2 = process_opportunity_observation(db, obs2, candidates=[cand2])
    assert len(opps2) == 1, "Must not create a second row for title change!"
    assert opps2[0].id == opp_id
    assert opps2[0].status == OpportunityStatus.CHANGED.value
    assert opps2[0].role_title == "Senior Practice Nurse"


def test_branch_isolation_distinct_opportunities(db: Session):
    """Verify distinct branches advertising same role category are preserved as distinct opportunities."""
    org_a = _make_org(db, "St Jude City", "st-jude-city-3000", postcode="3000")
    org_b = _make_org(db, "St Jude Suburb", "st-jude-suburb-3121", postcode="3121")

    src_a = _make_source(db, url="https://stjude-city.example.org/careers", org_id=org_a.id)
    src_b = _make_source(db, url="https://stjude-suburb.example.org/careers", org_id=org_b.id)

    _link_source(db, org_a.id, src_a.id)
    _link_source(db, org_b.id, src_b.id)

    snap_a = _make_snapshot(db, src_a, "City VR GP opening")
    obs_a = _make_observation(db, src_a.id, snapshot_id=snap_a.id, seq=1)
    cand_a = OpportunityCandidate(
        role_category=RoleCategory.GP,
        role_title="VR General Practitioner",
        verbatim_wording="City location, 70% billings",
    )
    opps_a = process_opportunity_observation(db, obs_a, candidates=[cand_a])

    snap_b = _make_snapshot(db, src_b, "Suburb VR GP opening")
    obs_b = _make_observation(db, src_b.id, snapshot_id=snap_b.id, seq=1)
    cand_b = OpportunityCandidate(
        role_category=RoleCategory.GP,
        role_title="VR General Practitioner",
        verbatim_wording="Suburb location, 75% billings",
    )
    opps_b = process_opportunity_observation(db, obs_b, candidates=[cand_b])

    assert len(opps_a) == 1
    assert len(opps_b) == 1
    assert opps_a[0].id != opps_b[0].id
    assert opps_a[0].organisation_id == org_a.id
    assert opps_b[0].organisation_id == org_b.id


def test_explicit_ad_id_priority(db: Session):
    """Verify explicit ad IDs separate multiple vacancies of the same category at the same practice."""
    org = _make_org(db, "Multi Vacancy Practice", "multi-vac-3000")
    src = _make_source(db, org_id=org.id)
    _link_source(db, org.id, src.id)

    snap = _make_snapshot(db, src, "Two GP vacancies")
    obs = _make_observation(db, src.id, snapshot_id=snap.id, seq=1)

    cand1 = OpportunityCandidate(
        role_category=RoleCategory.GP,
        role_title="VR GP Morning",
        verbatim_wording="Morning session GP",
        explicit_ad_id="GP-MORN-01",
    )
    cand2 = OpportunityCandidate(
        role_category=RoleCategory.GP,
        role_title="VR GP Evening",
        verbatim_wording="Evening session GP",
        explicit_ad_id="GP-EVE-02",
    )

    opps = process_opportunity_observation(db, obs, candidates=[cand1, cand2])
    assert len(opps) == 2
    ids = {o.explicit_ad_id for o in opps}
    assert ids == {"GP-MORN-01", "GP-EVE-02"}


# =============================================================================
# 5. INVARIANTS & STRICT PROHIBITIONS
# =============================================================================

def test_filled_or_hired_status_strictly_forbidden():
    """Verify architecture strictly forbids setting status to 'filled' or 'hired'."""
    # Check OpportunityStatus enum
    allowed_statuses = {s.value for s in OpportunityStatus}
    assert "filled" not in allowed_statuses
    assert "hired" not in allowed_statuses

    # Attempting to construct Opportunity with 'filled' must fail validation
    with pytest.raises(Exception):
        Opportunity(
            organisation_id=1,
            source_id=1,
            first_snapshot_id=1,
            last_snapshot_id=1,
            role_category=RoleCategory.GP,
            role_title="Doctor",
            verbatim_wording="Wording",
            status="filled",  # Forbidden!
        )


def test_closure_requires_verbatim_quote():
    """Verify that transitioning to explicitly_closed requires closure_evidence_quote."""
    with pytest.raises(ValueError, match="closure_evidence_quote"):
        Opportunity(
            organisation_id=1,
            source_id=1,
            first_snapshot_id=1,
            last_snapshot_id=1,
            role_category=RoleCategory.GP,
            role_title="Doctor",
            verbatim_wording="Wording",
            status=OpportunityStatus.EXPLICITLY_CLOSED,
            closure_evidence_quote=None,  # Forbidden without proof!
        )


# =============================================================================
# 6. REST API ENDPOINTS
# =============================================================================

def test_api_list_opportunities_and_filters(client: TestClient, db: Session):
    """Verify GET /api/opportunities with role_category and status filtering."""
    org = _make_org(db, "API Practice", "api-practice-3000")
    src = _make_source(db, org_id=org.id)
    _link_source(db, org.id, src.id)
    snap = _make_snapshot(db, src, "Vacancies")
    obs = _make_observation(db, src.id, snapshot_id=snap.id, seq=1)

    c1 = OpportunityCandidate(role_category=RoleCategory.GP, role_title="GP Role", verbatim_wording="GP details", explicit_ad_id="API-GP-1")
    c2 = OpportunityCandidate(role_category=RoleCategory.NURSE, role_title="RN Role", verbatim_wording="RN details", explicit_ad_id="API-RN-1")
    process_opportunity_observation(db, obs, candidates=[c1, c2])
    db.commit()

    # List all
    res = client.get("/api/opportunities")
    assert res.status_code == 200
    data = res.json()
    assert len(data) >= 2

    # Filter by role_category
    res_gp = client.get("/api/opportunities?role_category=gp")
    assert res_gp.status_code == 200
    gp_data = res_gp.json()
    assert all(o["role_category"] == "gp" for o in gp_data)

    # Filter by organisation_id
    res_org = client.get(f"/api/opportunities?organisation_id={org.id}")
    assert res_org.status_code == 200
    assert len(res_org.json()) == 2


def test_api_get_opportunity_detail_and_404(client: TestClient, db: Session):
    """Verify GET /api/opportunities/{id} returns detailed opportunity or 404."""
    org = _make_org(db, "Detail Practice", "detail-practice-3000")
    src = _make_source(db, org_id=org.id)
    _link_source(db, org.id, src.id)
    snap = _make_snapshot(db, src, "Careers")
    obs = _make_observation(db, src.id, snapshot_id=snap.id, seq=1)

    c = OpportunityCandidate(role_category=RoleCategory.GP, role_title="GP Detail", verbatim_wording="Details", explicit_ad_id="API-DET-1")
    opps = process_opportunity_observation(db, obs, candidates=[c])
    opp_id = opps[0].id
    db.commit()

    # 200 Detail
    res = client.get(f"/api/opportunities/{opp_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == opp_id
    assert body["role_title"] == "GP Detail"
    assert body["organisation_name"] == org.name
    assert body["source_url"] == src.url

    # 404 Non-existent
    res_404 = client.get("/api/opportunities/999999")
    assert res_404.status_code == 404
    err = res_404.json()
    assert err["error"]["code"] == "opportunity_not_found"


def test_api_scan_source_opportunities_and_404(client: TestClient, db: Session):
    """Verify POST /api/opportunities/scan/{source_id} triggers scan or returns 404."""
    org = _make_org(db, "Scan Practice", "scan-practice-3000")
    src = _make_source(db, org_id=org.id)
    _link_source(db, org.id, src.id)
    snap = _make_snapshot(db, src, "Careers at Scan Practice\nVR General Practitioner\nRef: GP-SCAN-01\nSeeking doctor")
    obs = _make_observation(db, src.id, snapshot_id=snap.id, seq=1)
    db.commit()

    res = client.post(f"/api/opportunities/scan/{src.id}")
    assert res.status_code == 200
    result = res.json()
    assert result["source_id"] == src.id
    assert result["opportunities_found"] >= 1
    assert "gp" in [o["role_category"] for o in result["opportunities"]]

    # 404 for unknown source
    res_404 = client.post("/api/opportunities/scan/999999")
    assert res_404.status_code == 404
    assert res_404.json()["error"]["code"] == "source_not_found"


def test_api_forbidden_endpoints_do_not_exist(client: TestClient):
    """Negative security test: verify no endpoints exist to delete or mark opportunities filled."""
    # PATCH /api/opportunities/1/mark-filled -> 404 or 405
    res = client.patch("/api/opportunities/1/mark-filled", json={"status": "filled"})
    assert res.status_code in (404, 405)

    # DELETE /api/opportunities/1 -> 404 or 405 (historical preservation)
    res_del = client.delete("/api/opportunities/1")
    assert res_del.status_code in (404, 405)


# =============================================================================
# 7. ACCEPTANCE GATE: POSSIBLY_STALE & CLOSING DATE VERIFICATION
# =============================================================================

def test_possibly_stale_before_and_after_published_closing_date(db: Session):
    """Verify possibly_stale lifecycle state across explicitly published closing date.

    Expected:
        - Before expiry: advertised.
        - After expiry: possibly_stale, where policy permits.
        - Expiry alone must NOT produce explicitly_closed.
        - Expiry alone must NOT produce no_longer_observed.
        - Explicit closure quote produces explicitly_closed.
        - Missing or ambiguous closing dates must NOT produce fabricated expiry.
    """
    org = _make_org(db, "Closing Date Practice", "closing-date-3000")
    src = _make_source(db, org_id=org.id)
    _link_source(db, org.id, src.id)

    # Vacancy with explicit closing date 15 March 2026
    wording = "Seeking VR GP, 70% billings. Ref: GP-STALE-01. Applications close 15 March 2026."
    snap1 = _make_snapshot(db, src, wording)
    obs1 = _make_observation(db, src.id, snapshot_id=snap1.id, seq=1)
    cand = OpportunityCandidate(
        role_category=RoleCategory.GP,
        role_title="VR General Practitioner",
        verbatim_wording=wording,
        explicit_ad_id="GP-STALE-01",
    )

    # 1. Evaluated BEFORE expiry (1 March 2026) -> advertised
    before_ref = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    opps1 = process_opportunity_observation(db, obs1, candidates=[cand], reference_time=before_ref)
    opp = opps1[0]
    assert opp.status == OpportunityStatus.ADVERTISED.value
    assert opp.consecutive_absences == 0

    # 2. Evaluated AFTER expiry (20 March 2026) -> possibly_stale
    after_ref = datetime(2026, 3, 20, 12, 0, tzinfo=UTC)
    stale_status = evaluate_opportunity_staleness(opp, reference_time=after_ref)
    assert stale_status == OpportunityStatus.POSSIBLY_STALE.value

    # Process observation at after_ref -> updates to possibly_stale
    opps2 = process_opportunity_observation(db, obs1, candidates=[cand], reference_time=after_ref)
    assert opps2[0].status == OpportunityStatus.POSSIBLY_STALE.value
    assert opps2[0].status != OpportunityStatus.EXPLICITLY_CLOSED.value
    assert opps2[0].status != OpportunityStatus.NO_LONGER_OBSERVED.value
    assert opps2[0].closure_evidence_quote is None

    # 3. Missing or ambiguous closing dates must NOT produce fabricated expiry
    ambiguous_wording = "Seeking Nurse, immediate start. Apply ASAP! Ref: NURSE-AMBIG."
    snap_amb = _make_snapshot(db, src, ambiguous_wording)
    obs_amb = _make_observation(db, src.id, snapshot_id=snap_amb.id, seq=2)
    cand_amb = OpportunityCandidate(
        role_category=RoleCategory.NURSE,
        role_title="Practice Nurse",
        verbatim_wording=ambiguous_wording,
        explicit_ad_id="NURSE-AMBIG",
    )
    opps_amb = process_opportunity_observation(db, obs_amb, candidates=[cand_amb], reference_time=after_ref)
    assert opps_amb[-1].status == OpportunityStatus.ADVERTISED.value
    assert opps_amb[-1].status != OpportunityStatus.POSSIBLY_STALE.value


# =============================================================================
# 8. ACCEPTANCE GATE: IDENTITY COLLISIONS & AMBIGUOUS MERGING
# =============================================================================

def test_identity_collision_two_explicit_job_references(db: Session):
    """Two independent explicit references at same practice remain separate."""
    org = _make_org(db, "Ref Collision Practice", "ref-coll-3000")
    src = _make_source(db, org_id=org.id)
    _link_source(db, org.id, src.id)

    snap = _make_snapshot(db, src, "Two distinct GP roles")
    obs = _make_observation(db, src.id, snapshot_id=snap.id, seq=1)

    c1 = OpportunityCandidate(role_category=RoleCategory.GP, role_title="VR GP Morning", verbatim_wording="Morning session", explicit_ad_id="REF-101")
    c2 = OpportunityCandidate(role_category=RoleCategory.GP, role_title="VR GP Evening", verbatim_wording="Evening session", explicit_ad_id="REF-102")

    opps = process_opportunity_observation(db, obs, candidates=[c1, c2])
    assert len(opps) == 2
    assert opps[0].id != opps[1].id
    assert {opps[0].explicit_ad_id, opps[1].explicit_ad_id} == {"REF-101", "REF-102"}


def test_identity_collision_two_distinct_source_local_identifiers(db: Session):
    """Two distinct source-local identifiers (slugs/IDs) remain separate."""
    org = _make_org(db, "Slug Collision Practice", "slug-coll-3000")
    src = _make_source(db, org_id=org.id)
    _link_source(db, org.id, src.id)

    snap = _make_snapshot(db, src, "Two slug vacancies")
    obs = _make_observation(db, src.id, snapshot_id=snap.id, seq=1)

    c1 = OpportunityCandidate(role_category=RoleCategory.GP, role_title="GP Role A", verbatim_wording="Slug A", source_job_id="slug:job-alpha")
    c2 = OpportunityCandidate(role_category=RoleCategory.GP, role_title="GP Role B", verbatim_wording="Slug B", source_job_id="slug:job-beta")

    opps = process_opportunity_observation(db, obs, candidates=[c1, c2])
    assert len(opps) == 2
    assert opps[0].id != opps[1].id
    assert {opps[0].explicit_ad_id, opps[1].explicit_ad_id} == {"slug:job-alpha", "slug:job-beta"}


def test_identity_collision_two_vacancies_no_stable_identifiers(db: Session):
    """Two vacancies with no stable identifiers in same observation are NOT collapsed."""
    org = _make_org(db, "No Ident Practice", "no-ident-3000")
    src = _make_source(db, org_id=org.id)
    _link_source(db, org.id, src.id)

    snap = _make_snapshot(db, src, "Two anonymous GP vacancies")
    obs = _make_observation(db, src.id, snapshot_id=snap.id, seq=1)

    c1 = OpportunityCandidate(role_category=RoleCategory.GP, role_title="VR General Practitioner", verbatim_wording="Full time role", explicit_ad_id=None)
    c2 = OpportunityCandidate(role_category=RoleCategory.GP, role_title="VR General Practitioner", verbatim_wording="Part time weekend role", explicit_ad_id=None)

    opps = process_opportunity_observation(db, obs, candidates=[c1, c2])
    assert len(opps) == 2, "Two vacancies without identifiers must remain separate, not conflated!"
    assert opps[0].id != opps[1].id


def test_database_uniqueness_constraints_permit_multiple_vacancies(db: Session):
    """Verify SQLite table indices do not restrict legitimate multiple vacancies."""
    org = _make_org(db, "Multi Vac Practice", "multi-vac-db-3000")
    src = _make_source(db, org_id=org.id)
    _link_source(db, org.id, src.id)
    snap = _make_snapshot(db, src, "Snapshot")

    o1 = OpportunityORM(organisation_id=org.id, source_id=src.id, first_snapshot_id=snap.id, last_snapshot_id=snap.id, role_category="gp", role_title="GP 1", verbatim_wording="W1", explicit_ad_id="GP-DB-1")
    o2 = OpportunityORM(organisation_id=org.id, source_id=src.id, first_snapshot_id=snap.id, last_snapshot_id=snap.id, role_category="gp", role_title="GP 2", verbatim_wording="W2", explicit_ad_id="GP-DB-2")
    o3 = OpportunityORM(organisation_id=org.id, source_id=src.id, first_snapshot_id=snap.id, last_snapshot_id=snap.id, role_category="gp", role_title="GP 3", verbatim_wording="W3", explicit_ad_id=None)

    db.add_all([o1, o2, o3])
    db.flush()
    assert o1.id and o2.id and o3.id


# =============================================================================
# 9. ACCEPTANCE GATE: HISTORICAL PRESERVATION & TRACEABILITY
# =============================================================================

def test_historical_preservation_lifecycle_sequence(db: Session):
    """advertised -> changed -> explicitly_closed -> advertised again:
    Verifies that previous titles, remuneration, closure quotes, and snapshots remain recoverable.
    """
    org = _make_org(db, "Traceable Practice", "trace-practice-3000")
    src = _make_source(db, org_id=org.id)
    _link_source(db, org.id, src.id)

    # Obs 1: advertised (VR GP, 70% billings)
    snap1 = _make_snapshot(db, src, "VR GP Ref: GP-TRACE 70% billings")
    obs1 = _make_observation(db, src.id, snapshot_id=snap1.id, seq=1)
    c1 = OpportunityCandidate(role_category=RoleCategory.GP, role_title="VR General Practitioner", verbatim_wording="70% billings", explicit_ad_id="GP-TRACE")
    opps1 = process_opportunity_observation(db, obs1, candidates=[c1])
    opp = opps1[0]
    assert opp.status == "advertised"
    opp_id = opp.id

    # Obs 2: changed (VR GP — Urgent, 72% billings)
    snap2 = _make_snapshot(db, src, "VR GP — Urgent Ref: GP-TRACE 72% billings")
    obs2 = _make_observation(db, src.id, snapshot_id=snap2.id, seq=2)
    c2 = OpportunityCandidate(role_category=RoleCategory.GP, role_title="VR General Practitioner — Urgent", verbatim_wording="72% billings", explicit_ad_id="GP-TRACE")
    opps2 = process_opportunity_observation(db, obs2, candidates=[c2])
    assert opps2[0].status == "changed"

    # Obs 3: explicitly_closed (quote)
    snap3 = _make_snapshot(db, src, "Applications for GP role (Ref: GP-TRACE) are now closed.")
    obs3 = _make_observation(db, src.id, snapshot_id=snap3.id, seq=3)
    opps3 = process_opportunity_observation(db, obs3, candidates=[])
    assert opps3[0].status == "explicitly_closed"
    assert "closed" in opps3[0].closure_evidence_quote.lower()

    # Obs 4: advertised again (reappeared)
    snap4 = _make_snapshot(db, src, "VR GP Ref: GP-TRACE 70% billings")
    obs4 = _make_observation(db, src.id, snapshot_id=snap4.id, seq=4)
    opps4 = process_opportunity_observation(db, obs4, candidates=[c1])
    assert opps4[0].status == "advertised"
    assert opps4[0].id == opp_id

    # RECOVERY AUDIT: Verify all historical transitions are recoverable from ChangeEvents and Snapshots
    events = db.execute(
        select(ChangeEventORM).where(ChangeEventORM.source_id == src.id).order_by(ChangeEventORM.id.asc())
    ).scalars().all()

    event_types = [e.event_type for e in events]
    assert "fact_added" in event_types
    assert "fact_modified" in event_types
    assert "fact_reappeared" in event_types

    # Prove previous title "VR General Practitioner — Urgent" is recoverable
    mod_event = next(e for e in events if e.event_type == "fact_modified" and "Urgent" in (e.new_value or ""))
    assert mod_event.old_value == "VR General Practitioner"
    assert mod_event.new_value == "VR General Practitioner — Urgent"

    # Prove original closure quote is preserved in history even after reappearance
    closure_event = next(e for e in events if "closed" in str(e.diff_metadata))
    meta = closure_event.diff_metadata if isinstance(closure_event.diff_metadata, dict) else json.loads(closure_event.diff_metadata)
    assert "Applications for GP role (Ref: GP-TRACE) are now closed." in meta.get("closure_evidence_quote", "")

    # Prove previous snapshots remain accessible
    assert db.get(PageSnapshot, snap1.id) is not None
    assert db.get(PageSnapshot, snap2.id) is not None
    assert db.get(PageSnapshot, snap3.id) is not None
    assert db.get(PageSnapshot, snap4.id) is not None


# =============================================================================
# 10. ACCEPTANCE GATE: EVIDENCE INTEGRITY & OFFSET VALIDATION
# =============================================================================

def test_evidence_integrity_quotation_and_offsets(db: Session):
    """Verify supporting quotation against immutable snapshot and character offsets."""
    org = _make_org(db, "Evidence Practice", "ev-practice-3000")
    src = _make_source(db, org_id=org.id)
    _link_source(db, org.id, src.id)

    full_text = "Careers Portal\nSeeking VR General Practitioner, 70% billings, immediate start. Ref: GP-EV-01.\nContact hr@test.org"
    snap = _make_snapshot(db, src, full_text)
    obs = _make_observation(db, src.id, snapshot_id=snap.id, seq=1)

    quote = "Seeking VR General Practitioner, 70% billings, immediate start."
    c = OpportunityCandidate(role_category=RoleCategory.GP, role_title="VR General Practitioner", verbatim_wording=quote, explicit_ad_id="GP-EV-01")
    opps = process_opportunity_observation(db, obs, candidates=[c])
    opp = opps[0]

    # Validate evidence
    valid, reason, start, end = verify_opportunity_evidence(opp, snap)
    assert valid is True
    assert reason in ("verified_exact", "verified_normalized")
    assert start is not None and end is not None
    assert snap.text[start:end] == quote

    # Fabricated quotation must be rejected
    opp.verbatim_wording = "Fabricated wording that never appeared on the page"
    valid_bad, reason_bad, _, _ = verify_opportunity_evidence(opp, snap)
    assert valid_bad is False
    assert reason_bad == "quotation_not_found_in_snapshot"


def test_evidence_integrity_closure_quotation(db: Session):
    """Verify explicit closure quotation against snapshot and offsets."""
    org = _make_org(db, "Closure Ev Practice", "closure-ev-3000")
    src = _make_source(db, org_id=org.id)
    _link_source(db, org.id, src.id)

    full_text = "Notice: Applications for GP position (Ref: GP-CLOSE-99) are now closed."
    snap = _make_snapshot(db, src, full_text)
    obs = _make_observation(db, src.id, snapshot_id=snap.id, seq=1)

    opp = OpportunityORM(
        organisation_id=org.id,
        source_id=src.id,
        first_snapshot_id=snap.id,
        last_snapshot_id=snap.id,
        role_category="gp",
        role_title="VR GP",
        verbatim_wording="Seeking GP",
        explicit_ad_id="GP-CLOSE-99",
        status=OpportunityStatus.EXPLICITLY_CLOSED.value,
        closure_evidence_quote="Applications for GP position (Ref: GP-CLOSE-99) are now closed.",
    )
    db.add(opp)
    db.flush()

    valid, reason, start, end = verify_closure_evidence(opp, snap)
    assert valid is True
    assert start is not None and end is not None
    assert "closed" in snap.text[start:end].lower()

    # Fabricated closure quotation must be rejected
    opp.closure_evidence_quote = "This role was filled yesterday by HR"
    valid_bad, reason_bad, _, _ = verify_closure_evidence(opp, snap)
    assert valid_bad is False
    assert reason_bad == "closure_quote_not_found_in_snapshot"


def test_unrelated_job_title_occurrence_does_not_suppress_absence(db: Session):
    """Ensure unrelated occurrences of a job title (e.g. staff bio) do NOT suppress genuine absence."""
    org = _make_org(db, "Bio Practice", "bio-practice-3000")
    src = _make_source(db, org_id=org.id)
    _link_source(db, org.id, src.id)

    # Obs 1: Genuine vacancy
    wording = "Seeking VR General Practitioner, 70% billings, immediate start. Ref: GP-BIO-01."
    snap1 = _make_snapshot(db, src, wording)
    obs1 = _make_observation(db, src.id, snapshot_id=snap1.id, seq=1)
    c = OpportunityCandidate(role_category=RoleCategory.GP, role_title="VR General Practitioner", verbatim_wording=wording, explicit_ad_id="GP-BIO-01")
    opps = process_opportunity_observation(db, obs1, candidates=[c])
    opp = opps[0]
    assert opp.status == "advertised"

    # Obs 2: Vacancy is removed, but page text has unrelated mention: "About us: Dr Smith is our VR General Practitioner."
    unrelated_text = "About our practice: Dr Smith is our VR General Practitioner and has served for 10 years."
    snap2 = _make_snapshot(db, src, unrelated_text)
    obs2 = _make_observation(db, src.id, snapshot_id=snap2.id, seq=2)

    # Empty candidate list passed
    opps2 = process_opportunity_observation(db, obs2, candidates=[])
    # The unrelated occurrence must NOT suppress absence! It should transition to unconfirmed_prior!
    assert opps2[0].status == OpportunityStatus.UNCONFIRMED_PRIOR.value
    assert opps2[0].consecutive_absences == 1


def test_f14_corpus_ground_truth_manifest_audit():
    """Verify that all six F14 synthetic fixtures exist and adhere to the manifest."""
    manifest_path = M3_CORPUS_DIR / "manifest.json"
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    f14_entries = [fx for fx in manifest["fixtures"] if fx["group"] == "group_delta_vacancies"]
    assert len(f14_entries) == 6

    expected_ids = {"F14_v1", "F14_v2", "F14_v3a", "F14_v3b", "F14_v3c", "F14_v3d"}
    manifest_ids = {e["id"] for e in f14_entries}
    assert manifest_ids == expected_ids

    for entry in f14_entries:
        fpath = M3_CORPUS_DIR / entry["file"]
        assert fpath.is_file(), f"Fixture file {fpath} must exist"
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data.get("synthetic") is True
        assert data.get("synthetic_watermark") == "PRACTICESIGNAL_SYNTHETIC_FIXTURE_DO_NOT_TREAT_AS_REAL_DATA"
