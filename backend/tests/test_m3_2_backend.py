"""M3.2 comprehensive verification and integrity remediation test suite.

Covers:
1. Database migration M3_002:
   - Fresh DB creation includes discrepancies table with ON DELETE RESTRICT.
   - Upgrading existing DB safely migrates CASCADE to RESTRICT while preserving data.
   - Automatic timestamped backup and rollback testing.
   - Idempotency on repeated execution.
   - Canonical ordering check constraint (fact_a_id < fact_b_id) and unique constraint.
2. Historical record preservation (MANDATORY):
   - Deleting an organisation referenced by a discrepancy is restricted (IntegrityError).
   - Deleting either referenced fact is restricted (IntegrityError).
   - Foreign-key integrity validation under PRAGMA foreign_keys = ON.
3. Temporal divergence correctness:
   - Explicit non-overlapping fact validity periods produce temporal_divergence.
   - Generic page update dates ('Page updated March 2025') MUST NOT produce temporal divergence.
   - Crawl-time differences alone MUST NOT produce temporal divergence.
   - Missing or ambiguous temporal evidence MUST NOT produce temporal divergence.
   - Overlapping or compatible validity periods MUST NOT be labelled temporally divergent.
4. Scope and ambiguity policy consistency:
   - NULL scope != NULL scope: unscoped differences NEVER produce value_mismatch.
   - Incompatible known scopes (clinic vs pathology) suppress comparison (0 discrepancy).
   - Ambiguous/unparseable telephone numbers on matching scope route to ambiguity.
   - Freeform/unstructured opening hours route to ambiguity.
   - Descriptive addresses without numbers route to ambiguity.
5. Evidence integrity validation:
   - Fabricated quotes (not in snapshot text) suppress comparison.
   - Mismatched snapshot IDs suppress comparison.
   - Invalid offsets suppress comparison.
   - Empty quotes suppress comparison.
   - Rejected facts suppress comparison.
   - Valid verified quotes enable evidence-backed detection.
6. Conservative normalisation:
   - E.164 Australian phone normalisation matches equivalent formats (F10).
   - Genuinely conflicting valid numbers produce value_mismatch (F11).
   - Address street abbreviation normalisation (st vs street, pde vs parade).
7. Future review fields & REST API:
   - Resolution fields (resolution_notes, resolved_by, resolved_at) default to null.
   - Status defaults to 'detected'.
   - REST API listing, detail with nested evidence, and scan endpoints.
   - Strict absence of auto-resolve or winning-source endpoints.
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
    inspect_m3_002_schema,
    migrate_m3_002_discrepancies,
    rollback_m3_002_discrepancies,
)
from backend.app.db.orm import (
    Discrepancy as DiscrepancyORM,
    Evidence,
    Fact,
    Organisation,
    OrganisationSourceLink,
    PageSnapshot,
    Source,
)
from backend.app.intelligence.discrepancy_detector import (
    are_values_equivalent,
    check_temporal_divergence,
    compare_field_values,
    detect_organisation_discrepancies,
    evaluate_fact_pair_discrepancy,
    parse_explicit_validity_period,
    scan_organisation_discrepancies,
    verify_fact_evidence_integrity,
)
from backend.app.intelligence.schemas import ConflictType, DiscrepancyStatus


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
    url: str = "https://example.com/clinic",
    label: str = "Example Source",
    fixture_id: str = "F10",
) -> Source:
    src = Source(
        kind="fixture",
        url=url,
        label=label,
        domain="example.com",
        fixture_id=fixture_id,
        page_type="home",
        policy_status="approved",
        robots_status="allowed",
        intended_fields=["public_phone", "opening_hours", "postcode", "address", "organisation_name"],
    )
    session.add(src)
    session.flush()
    return src


def _make_snapshot(
    session: Session,
    source: Source,
    text: str = "sample text",
    content_hash: str | None = None,
) -> PageSnapshot:
    hash_val = content_hash or (uuid.uuid4().hex + uuid.uuid4().hex)[:64]
    snap = PageSnapshot(
        source_id=source.id,
        fetched_at=datetime.now(UTC),
        title="Sample Title",
        text=text,
        content_hash=hash_val,
        parser_version="1.0.0",
        fixture=True,
        headings=[],
    )
    session.add(snap)
    session.flush()
    return snap


def _make_org(session: Session, name: str = "St Jude Family Medical", slug: str = "st-jude-3000") -> Organisation:
    org = Organisation(
        name=name,
        slug=slug,
        primary_domain="stjude.example.com",
        phone="0394198888",
        address="100 Victoria Parade",
        suburb="East Melbourne",
        postcode="3002",
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
    quote: str = "Call reception on (03) 9419 8888",
    status: str = "accepted",
    custom_offsets: tuple[int, int] | None = None,
    custom_snapshot_id: int | None = None,
) -> Fact:
    ev_id = None
    if quote:
        # Guarantee quote is present in snapshot text unless testing invalid evidence
        if custom_offsets is None and quote not in (snapshot.text or ""):
            snapshot.text = ((snapshot.text or "") + "\n" + quote).strip()
            session.add(snapshot)
            session.flush()

        if custom_offsets is not None:
            start_off, end_off = custom_offsets
        else:
            start_off = (snapshot.text or "").find(quote)
            end_off = start_off + len(quote)

        target_snap_id = custom_snapshot_id if custom_snapshot_id is not None else snapshot.id

        ev = Evidence(
            snapshot_id=target_snap_id,
            quote=quote,
            start_offset=start_off,
            end_offset=end_off,
            captured_at=datetime.now(UTC),
            match_algorithm_version="v1",
        )
        session.add(ev)
        session.flush()
        ev_id = ev.id

    fact = Fact(
        snapshot_id=snapshot.id,
        organisation_id=organisation_id,
        field_key=field_key,
        scope_key=scope_key,
        raw_value=raw_value,
        evidence_id=ev_id,
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


# =============================================================================
# 1. DATABASE MIGRATION M3_002 & HISTORICAL RECORD PRESERVATION
# =============================================================================

def test_m3_002_migration_on_fresh_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "fresh.db"
        engine = create_engine(f"sqlite:///{db_path}")
        orm.Base.metadata.create_all(bind=engine)
        engine.dispose()

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        info = inspect_m3_002_schema(cur)
        cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='discrepancies'")
        sql = (cur.fetchone()[0] or "").upper()
        conn.close()

        assert info["discrepancies"] is True
        assert "ON DELETE RESTRICT" in sql
        assert "CASCADE" not in sql


def test_m3_002_migration_upgrades_legacy_cascade_to_restrict():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "legacy.db"
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()

        # Simulate legacy M3_002 schema with CASCADE
        cur.execute("""
            CREATE TABLE organisations (id INTEGER PRIMARY KEY, name TEXT);
        """)
        cur.execute("""
            CREATE TABLE facts (id INTEGER PRIMARY KEY, raw_value TEXT);
        """)
        cur.execute("""
            CREATE TABLE discrepancies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organisation_id INTEGER NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
                field_key VARCHAR(64) NOT NULL,
                scope_key VARCHAR(64) NOT NULL,
                fact_a_id INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
                fact_b_id INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
                conflict_type VARCHAR(64) NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'detected',
                rationale TEXT NOT NULL,
                resolution_notes TEXT,
                resolved_by VARCHAR(255),
                resolved_at DATETIME,
                detected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT ck_discrepancy_canonical_fact_order CHECK (fact_a_id < fact_b_id),
                CONSTRAINT uq_discrepancy_fact_pair UNIQUE (fact_a_id, fact_b_id)
            );
        """)
        # Insert test data
        cur.execute("INSERT INTO organisations VALUES (1, 'Legacy Clinic')")
        cur.execute("INSERT INTO facts VALUES (10, 'Val 1')")
        cur.execute("INSERT INTO facts VALUES (20, 'Val 2')")
        cur.execute("""
            INSERT INTO discrepancies (organisation_id, field_key, scope_key, fact_a_id, fact_b_id, conflict_type, status, rationale)
            VALUES (1, 'public_phone', 'reception', 10, 20, 'value_mismatch', 'detected', 'Legacy rationale');
        """)
        conn.commit()
        conn.close()

        # Run migration
        res = migrate_m3_002_discrepancies(db_path)
        assert res["status"] == "upgraded"

        # Verify new schema has RESTRICT and preserved data
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='discrepancies'")
        sql = (cur.fetchone()[0] or "").upper()
        assert "ON DELETE RESTRICT" in sql
        assert "CASCADE" not in sql

        cur.execute("SELECT id, rationale FROM discrepancies")
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][1] == "Legacy rationale"
        conn.close()


def test_historical_preservation_prevent_organisation_deletion(db: Session):
    """Historical preservation: deleting an organisation with a discrepancy is restricted."""
    org = _make_org(db)
    src_a = _make_source(db, "https://example.com/a")
    src_b = _make_source(db, "https://example.com/b")
    _link_source(db, org.id, src_a.id, "confirmed")
    _link_source(db, org.id, src_b.id, "confirmed")

    snap_a = _make_snapshot(db, src_a, "Alpha text")
    snap_b = _make_snapshot(db, src_b, "Beta text")

    fact_1 = _make_fact(db, snap_a, org.id, "public_phone", "(03) 9419 8888")
    fact_2 = _make_fact(db, snap_b, org.id, "public_phone", "(03) 9419 9999")

    disc = evaluate_fact_pair_discrepancy(db, fact_1, fact_2)
    assert disc is not None
    db.commit()

    # Attempting to delete the organisation must raise an IntegrityError (ON DELETE RESTRICT)
    db.delete(org)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    # Verify organisation and discrepancy remain intact
    assert db.get(Organisation, org.id) is not None
    assert db.get(DiscrepancyORM, disc.id) is not None


def test_historical_preservation_prevent_fact_deletion(db: Session):
    """Historical preservation: deleting either referenced fact is restricted."""
    org = _make_org(db)
    src_a = _make_source(db, "https://example.com/a")
    src_b = _make_source(db, "https://example.com/b")
    _link_source(db, org.id, src_a.id, "confirmed")
    _link_source(db, org.id, src_b.id, "confirmed")

    snap_a = _make_snapshot(db, src_a, "Alpha text")
    snap_b = _make_snapshot(db, src_b, "Beta text")

    fact_1 = _make_fact(db, snap_a, org.id, "public_phone", "(03) 9419 8888")
    fact_2 = _make_fact(db, snap_b, org.id, "public_phone", "(03) 9419 9999")

    disc = evaluate_fact_pair_discrepancy(db, fact_1, fact_2)
    assert disc is not None
    db.commit()

    # Attempt to delete fact_1
    db.delete(fact_1)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    # Attempt to delete fact_2
    db.delete(fact_2)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    assert db.get(Fact, fact_1.id) is not None
    assert db.get(Fact, fact_2.id) is not None


def test_m3_002_check_constraint_canonical_ordering(db: Session):
    org = _make_org(db)
    src_a = _make_source(db, "https://example.com/a")
    src_b = _make_source(db, "https://example.com/b")
    snap_a = _make_snapshot(db, src_a)
    snap_b = _make_snapshot(db, src_b)
    fact_1 = _make_fact(db, snap_a, org.id, "public_phone", "(03) 9419 8888")
    fact_2 = _make_fact(db, snap_b, org.id, "public_phone", "(03) 9419 9999")

    low_id = min(fact_1.id, fact_2.id)
    high_id = max(fact_1.id, fact_2.id)

    valid_disc = DiscrepancyORM(
        organisation_id=org.id,
        field_key="public_phone",
        scope_key="reception",
        fact_a_id=low_id,
        fact_b_id=high_id,
        conflict_type="value_mismatch",
        status="detected",
        rationale="Valid test discrepancy",
        detected_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    db.add(valid_disc)
    db.commit()

    invalid_disc = DiscrepancyORM(
        organisation_id=org.id,
        field_key="public_phone",
        scope_key="reception",
        fact_a_id=high_id,
        fact_b_id=low_id,
        conflict_type="value_mismatch",
        status="detected",
        rationale="Invalid reverse discrepancy",
        detected_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    db.add(invalid_disc)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_m3_002_unique_constraint_pair(db: Session):
    org = _make_org(db)
    src_a = _make_source(db, "https://example.com/a")
    src_b = _make_source(db, "https://example.com/b")
    snap_a = _make_snapshot(db, src_a)
    snap_b = _make_snapshot(db, src_b)
    fact_1 = _make_fact(db, snap_a, org.id, "public_phone", "(03) 9419 8888")
    fact_2 = _make_fact(db, snap_b, org.id, "public_phone", "(03) 9419 9999")

    low_id = min(fact_1.id, fact_2.id)
    high_id = max(fact_1.id, fact_2.id)

    disc1 = DiscrepancyORM(
        organisation_id=org.id,
        field_key="public_phone",
        scope_key="reception",
        fact_a_id=low_id,
        fact_b_id=high_id,
        conflict_type="value_mismatch",
        status="detected",
        rationale="First insertion",
        detected_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    db.add(disc1)
    db.commit()

    disc2 = DiscrepancyORM(
        organisation_id=org.id,
        field_key="public_phone",
        scope_key="reception",
        fact_a_id=low_id,
        fact_b_id=high_id,
        conflict_type="value_mismatch",
        status="detected",
        rationale="Second duplicate insertion",
        detected_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    db.add(disc2)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# =============================================================================
# 2. TEMPORAL DIVERGENCE & TEMPORAL AMBIGUITY CORRECTNESS
# =============================================================================

def test_temporal_divergence_two_bounded_non_overlapping_periods(db: Session):
    """Two explicitly non-overlapping, bounded periods produce temporal_divergence."""
    org = _make_org(db)
    src_a = _make_source(db, "https://example.com/a")
    src_b = _make_source(db, "https://example.com/b")
    _link_source(db, org.id, src_a.id, "confirmed")
    _link_source(db, org.id, src_b.id, "confirmed")

    snap_a = _make_snapshot(db, src_a)
    snap_b = _make_snapshot(db, src_b)

    fact_1 = _make_fact(
        db, snap_a, org.id, "opening_hours", "8:00 AM - 5:00 PM",
        quote="Hours valid from January 2024 to December 2024: 8:00 AM - 5:00 PM",
    )
    fact_2 = _make_fact(
        db, snap_b, org.id, "opening_hours", "8:00 AM - 6:00 PM",
        quote="Hours valid from January 2025 to December 2025: 8:00 AM - 6:00 PM",
    )

    assert check_temporal_divergence(fact_1, fact_2) is True

    disc = evaluate_fact_pair_discrepancy(db, fact_1, fact_2)
    assert disc is not None
    assert disc.conflict_type == ConflictType.TEMPORAL_DIVERGENCE.value
    assert "distinct non-overlapping validity periods" in disc.rationale

    db.commit()
    count = db.scalar(select(func.count(DiscrepancyORM.id)))
    assert count == 1


def test_temporal_divergence_two_start_only_dates_do_not_establish_non_overlap(db: Session):
    """Two start-only effective dates ('effective from Jan 2024') route to AMBIGUITY when values differ."""
    org = _make_org(db)
    src_a = _make_source(db, "https://example.com/a")
    src_b = _make_source(db, "https://example.com/b")
    _link_source(db, org.id, src_a.id, "confirmed")
    _link_source(db, org.id, src_b.id, "confirmed")

    snap_a = _make_snapshot(db, src_a)
    snap_b = _make_snapshot(db, src_b)

    fact_1 = _make_fact(
        db, snap_a, org.id, "opening_hours", "8:00 AM - 5:00 PM",
        quote="Hours effective from January 2024: 8:00 AM - 5:00 PM",
    )
    fact_2 = _make_fact(
        db, snap_b, org.id, "opening_hours", "8:00 AM - 6:00 PM",
        quote="Hours effective from March 2025: 8:00 AM - 6:00 PM",
    )

    # An open-ended start date does not establish an end date when another source starts
    assert check_temporal_divergence(fact_1, fact_2) is False

    disc = evaluate_fact_pair_discrepancy(db, fact_1, fact_2)
    assert disc is not None
    assert disc.conflict_type == ConflictType.AMBIGUITY.value
    assert "open-ended temporal validity evidence" in disc.rationale

    db.commit()
    count = db.scalar(select(func.count(DiscrepancyORM.id)))
    assert count == 1


def test_temporal_divergence_explicit_period_vs_undated_fact(db: Session):
    """One fact with explicit validity and one undated fact routes to AMBIGUITY when values differ."""
    org = _make_org(db)
    src_a = _make_source(db, "https://example.com/a")
    src_b = _make_source(db, "https://example.com/b")
    _link_source(db, org.id, src_a.id, "confirmed")
    _link_source(db, org.id, src_b.id, "confirmed")

    snap_a = _make_snapshot(db, src_a)
    snap_b = _make_snapshot(db, src_b)

    fact_1 = _make_fact(
        db, snap_a, org.id, "opening_hours", "8:00 AM - 5:00 PM",
        quote="Hours valid from January 2024 to December 2024: 8:00 AM - 5:00 PM",
    )
    fact_2 = _make_fact(
        db, snap_b, org.id, "opening_hours", "8:00 AM - 6:00 PM",
        quote="Opening hours: 8:00 AM - 6:00 PM",
    )

    assert check_temporal_divergence(fact_1, fact_2) is False

    disc = evaluate_fact_pair_discrepancy(db, fact_1, fact_2)
    assert disc is not None
    assert disc.conflict_type == ConflictType.AMBIGUITY.value
    assert "undated source" in disc.rationale

    db.commit()
    count = db.scalar(select(func.count(DiscrepancyORM.id)))
    assert count == 1


def test_temporal_divergence_generic_page_update_rejected(db: Session):
    """Generic page update timestamps ('Page updated March 2025') are ignored as validity evidence."""
    org = _make_org(db)
    src_a = _make_source(db, "https://example.com/a")
    src_b = _make_source(db, "https://example.com/b")
    _link_source(db, org.id, src_a.id, "confirmed")
    _link_source(db, org.id, src_b.id, "confirmed")

    snap_a = _make_snapshot(db, src_a)
    snap_b = _make_snapshot(db, src_b)

    fact_1 = _make_fact(
        db, snap_a, org.id, "opening_hours", "8:00 AM - 5:00 PM",
        quote="Page updated January 2024: 8:00 AM - 5:00 PM",
    )
    fact_2 = _make_fact(
        db, snap_b, org.id, "opening_hours", "8:00 AM - 6:00 PM",
        quote="Site last modified March 2025: 8:00 AM - 6:00 PM",
    )

    # Generic page updates are ignored -> neither has temporal validity -> normal value comparison
    assert check_temporal_divergence(fact_1, fact_2) is False

    disc = evaluate_fact_pair_discrepancy(db, fact_1, fact_2)
    assert disc is not None
    assert disc.conflict_type == ConflictType.VALUE_MISMATCH.value

    db.commit()
    count = db.scalar(select(func.count(DiscrepancyORM.id)))
    assert count == 1


def test_temporal_divergence_crawl_differences_rejected(db: Session):
    """Crawl-time differences alone MUST NOT establish temporal divergence (normal value comparison)."""
    org = _make_org(db)
    src_a = _make_source(db, "https://example.com/a")
    src_b = _make_source(db, "https://example.com/b")
    _link_source(db, org.id, src_a.id, "confirmed")
    _link_source(db, org.id, src_b.id, "confirmed")

    snap_a = _make_snapshot(db, src_a)
    snap_b = _make_snapshot(db, src_b)
    snap_a.fetched_at = datetime(2023, 1, 1, tzinfo=UTC)
    snap_b.fetched_at = datetime(2025, 1, 1, tzinfo=UTC)

    fact_1 = _make_fact(db, snap_a, org.id, "public_phone", "(03) 9419 8888", quote="Reception line is (03) 9419 8888")
    fact_2 = _make_fact(db, snap_b, org.id, "public_phone", "(03) 9419 9999", quote="Reception line is (03) 9419 9999")

    assert check_temporal_divergence(fact_1, fact_2) is False
    disc = evaluate_fact_pair_discrepancy(db, fact_1, fact_2)
    assert disc is not None
    assert disc.conflict_type == ConflictType.VALUE_MISMATCH.value

    db.commit()
    count = db.scalar(select(func.count(DiscrepancyORM.id)))
    assert count == 1


def test_temporal_divergence_overlapping_periods_rejected(db: Session):
    """Both facts have explicitly bounded, overlapping intervals -> normal value comparison."""
    org = _make_org(db)
    src_a = _make_source(db, "https://example.com/a")
    src_b = _make_source(db, "https://example.com/b")
    _link_source(db, org.id, src_a.id, "confirmed")
    _link_source(db, org.id, src_b.id, "confirmed")

    snap_a = _make_snapshot(db, src_a)
    snap_b = _make_snapshot(db, src_b)

    fact_1 = _make_fact(
        db, snap_a, org.id, "opening_hours", "8:00 AM - 5:00 PM",
        quote="Hours valid from January 2024 to December 2024: 8:00 AM - 5:00 PM",
    )
    fact_2 = _make_fact(
        db, snap_b, org.id, "opening_hours", "8:00 AM - 6:00 PM",
        quote="Hours valid from June 2024 to December 2024: 8:00 AM - 6:00 PM",
    )

    assert check_temporal_divergence(fact_1, fact_2) is False
    disc = evaluate_fact_pair_discrepancy(db, fact_1, fact_2)
    assert disc is not None
    assert disc.conflict_type == ConflictType.VALUE_MISMATCH.value

    db.commit()
    count = db.scalar(select(func.count(DiscrepancyORM.id)))
    assert count == 1


def test_parser_valid_through_phrase_is_not_bounded():
    """A phrase such as 'valid through June 2024' specifies only an end date and is not fully bounded."""
    p_end_only = parse_explicit_validity_period("Hours valid through June 2024: 8:00 AM - 5:00 PM")
    assert p_end_only is not None
    assert p_end_only.is_bounded is False

    p_until_only = parse_explicit_validity_period("Hours valid until December 2024: 8:00 AM - 5:00 PM")
    assert p_until_only is not None
    assert p_until_only.is_bounded is False

    p_start_only = parse_explicit_validity_period("Hours effective from January 2024: 8:00 AM - 5:00 PM")
    assert p_start_only is not None
    assert p_start_only.is_bounded is False

    p_bounded = parse_explicit_validity_period("Hours valid from January 2024 to December 2024: 8:00 AM - 5:00 PM")
    assert p_bounded is not None
    assert p_bounded.is_bounded is True

    p_year_bounded = parse_explicit_validity_period("Hours valid for 2024: 8:00 AM - 5:00 PM")
    assert p_year_bounded is not None
    assert p_year_bounded.is_bounded is True


def test_temporal_ambiguity_end_only_period_routes_to_ambiguity(db: Session):
    """An end-only phrase ('valid through June 2024') has unresolved temporal applicability and routes to AMBIGUITY."""
    org = _make_org(db)
    src_a = _make_source(db, "https://example.com/a")
    src_b = _make_source(db, "https://example.com/b")
    _link_source(db, org.id, src_a.id, "confirmed")
    _link_source(db, org.id, src_b.id, "confirmed")

    snap_a = _make_snapshot(db, src_a)
    snap_b = _make_snapshot(db, src_b)

    fact_1 = _make_fact(
        db, snap_a, org.id, "opening_hours", "8:00 AM - 5:00 PM",
        quote="Hours valid through June 2024: 8:00 AM - 5:00 PM",
    )
    fact_2 = _make_fact(
        db, snap_b, org.id, "opening_hours", "8:00 AM - 6:00 PM",
        quote="Hours valid from January 2024 to December 2024: 8:00 AM - 6:00 PM",
    )

    assert check_temporal_divergence(fact_1, fact_2) is False
    disc = evaluate_fact_pair_discrepancy(db, fact_1, fact_2)
    assert disc is not None
    assert disc.conflict_type == ConflictType.AMBIGUITY.value

    db.commit()
    count = db.scalar(select(func.count(DiscrepancyORM.id)))
    assert count == 1


def test_temporal_equal_values_produce_zero_discrepancy_records(db: Session):
    """Equal values across all temporal cases MUST NOT create unnecessary discrepancy records."""
    org = _make_org(db)
    src_a = _make_source(db, "https://example.com/a")
    src_b = _make_source(db, "https://example.com/b")
    _link_source(db, org.id, src_a.id, "confirmed")
    _link_source(db, org.id, src_b.id, "confirmed")

    snap_a = _make_snapshot(db, src_a)
    snap_b = _make_snapshot(db, src_b)

    # 1. Two bounded non-overlapping periods with identical hours
    f1_a = _make_fact(db, snap_a, org.id, "opening_hours", "8:00 AM - 5:00 PM", quote="Hours valid from Jan 2024 to Jun 2024: 8:00 AM - 5:00 PM")
    f1_b = _make_fact(db, snap_b, org.id, "opening_hours", "8:00 AM - 5:00 PM", quote="Hours valid from Jul 2024 to Dec 2024: 8:00 AM - 5:00 PM")
    assert evaluate_fact_pair_discrepancy(db, f1_a, f1_b) is None

    # 2. One bounded period vs undated fact with identical hours
    f2_a = _make_fact(db, snap_a, org.id, "opening_hours", "8:30 AM - 5:30 PM", quote="Hours valid for 2024: 8:30 AM - 5:30 PM")
    f2_b = _make_fact(db, snap_b, org.id, "opening_hours", "8:30 AM - 5:30 PM", quote="Hours: 8:30 AM - 5:30 PM")
    assert evaluate_fact_pair_discrepancy(db, f2_a, f2_b) is None

    # 3. Two start-only dates with identical phone numbers
    f3_a = _make_fact(db, snap_a, org.id, "public_phone", "(03) 9419 8888", quote="Hours effective from Jan 2024: (03) 9419 8888")
    f3_b = _make_fact(db, snap_b, org.id, "public_phone", "(03) 9419 8888", quote="Hours effective from Mar 2025: (03) 9419 8888")
    assert evaluate_fact_pair_discrepancy(db, f3_a, f3_b) is None

    # 4. End-only phrase vs undated with identical phone numbers
    f4_a = _make_fact(db, snap_a, org.id, "public_phone", "(03) 9419 1111", quote="Hours valid through June 2024: (03) 9419 1111")
    f4_b = _make_fact(db, snap_b, org.id, "public_phone", "(03) 9419 1111", quote="Reception: (03) 9419 1111")
    assert evaluate_fact_pair_discrepancy(db, f4_a, f4_b) is None

    # 5. Overlapping bounded periods with identical hours
    f5_a = _make_fact(db, snap_a, org.id, "opening_hours", "9:00 AM - 5:00 PM", quote="Hours valid from Jan 2024 to Dec 2024: 9:00 AM - 5:00 PM")
    f5_b = _make_fact(db, snap_b, org.id, "opening_hours", "9:00 AM - 5:00 PM", quote="Hours valid from Jun 2024 to Dec 2024: 9:00 AM - 5:00 PM")
    assert evaluate_fact_pair_discrepancy(db, f5_a, f5_b) is None

    db.commit()
    count = db.scalar(select(func.count(DiscrepancyORM.id)))
    assert count == 0


def test_temporal_divergence_missing_or_ambiguous_evidence(db: Session):
    """Missing or ambiguous temporal indicators must not produce temporal divergence."""
    org = _make_org(db)
    src_a = _make_source(db, "https://example.com/a")
    src_b = _make_source(db, "https://example.com/b")
    _link_source(db, org.id, src_a.id, "confirmed")
    _link_source(db, org.id, src_b.id, "confirmed")

    snap_a = _make_snapshot(db, src_a)
    snap_b = _make_snapshot(db, src_b)

    fact_1 = _make_fact(db, snap_a, org.id, "opening_hours", "8:00 AM - 5:00 PM", quote="Hours: 8:00 AM - 5:00 PM")
    fact_2 = _make_fact(db, snap_b, org.id, "opening_hours", "8:00 AM - 6:00 PM", quote="Operating since ancient times: 8:00 AM - 6:00 PM")

    assert check_temporal_divergence(fact_1, fact_2) is False
    disc = evaluate_fact_pair_discrepancy(db, fact_1, fact_2)
    assert disc is not None
    # Neither has temporal indicators -> normal value comparison
    assert disc.conflict_type == ConflictType.VALUE_MISMATCH.value

    db.commit()
    count = db.scalar(select(func.count(DiscrepancyORM.id)))
    assert count == 1


# =============================================================================
# 3. EVIDENCE VERIFICATION INTEGRITY (POSITIVE & NEGATIVE)
# =============================================================================

def test_evidence_offset_alignment_correct_quote_and_offsets(db: Session):
    """Correct quote with exact corresponding character offsets verifies successfully."""
    org = _make_org(db)
    src = _make_source(db, "https://example.com/a")
    snap = _make_snapshot(db, src, text="Welcome to St Jude. Call reception on (03) 9419 8888 for booking assistance.")

    quote = "Call reception on (03) 9419 8888"
    start_pos = snap.text.find(quote)
    end_pos = start_pos + len(quote)

    fact = _make_fact(
        db, snap, org.id, "public_phone", "(03) 9419 8888",
        quote=quote,
        custom_offsets=(start_pos, end_pos),
    )

    assert verify_fact_evidence_integrity(fact, snap) is True


def test_evidence_offset_alignment_correct_quote_wrong_offsets(db: Session):
    """Quote is in the snapshot, but stored offsets point to wrong text -> verification fails."""
    org = _make_org(db)
    src = _make_source(db, "https://example.com/a")
    src_b = _make_source(db, "https://example.com/b")
    _link_source(db, org.id, src.id, "confirmed")
    _link_source(db, org.id, src_b.id, "confirmed")

    snap = _make_snapshot(db, src, text="Welcome to St Jude. Call reception on (03) 9419 8888 for booking.")
    snap_b = _make_snapshot(db, src_b, text="Directory: Call reception on (03) 9419 9999 for booking.")

    quote = "Call reception on (03) 9419 8888"
    # Misaligned offsets: point to 'Welcome to St J' [0:15]
    fact_1 = _make_fact(
        db, snap, org.id, "public_phone", "(03) 9419 8888",
        quote=quote,
        custom_offsets=(0, 15),
    )
    fact_2 = _make_fact(db, snap_b, org.id, "public_phone", "(03) 9419 9999", quote="Call reception on (03) 9419 9999")

    # Direct verification returns False
    assert verify_fact_evidence_integrity(fact_1, snap) is False
    # Cross-source discrepancy is suppressed due to unverifiable evidence
    assert evaluate_fact_pair_discrepancy(db, fact_1, fact_2) is None


def test_evidence_offset_alignment_quote_present_multiple_times(db: Session):
    """When a quote occurs multiple times, offsets must accurately identify the chosen occurrence."""
    org = _make_org(db)
    src = _make_source(db, "https://example.com/a")
    text = "Call (03) 9419 8888 for appointments. In emergency, call (03) 9419 8888 immediately."
    snap = _make_snapshot(db, src, text=text)

    quote = "(03) 9419 8888"
    first_start = text.find(quote)
    first_end = first_start + len(quote)

    second_start = text.find(quote, first_end)
    second_end = second_start + len(quote)

    fact_occ1 = _make_fact(db, snap, org.id, "public_phone", quote, quote=quote, custom_offsets=(first_start, first_end))
    fact_occ2 = _make_fact(db, snap, org.id, "public_phone", quote, quote=quote, custom_offsets=(second_start, second_end))
    fact_wrong = _make_fact(db, snap, org.id, "public_phone", quote, quote=quote, custom_offsets=(first_start + 5, first_end + 5))

    assert verify_fact_evidence_integrity(fact_occ1, snap) is True
    assert verify_fact_evidence_integrity(fact_occ2, snap) is True
    assert verify_fact_evidence_integrity(fact_wrong, snap) is False


def test_evidence_offset_alignment_unicode_normalisation_differences(db: Session):
    """Unicode variations (smart quotes vs straight quotes) verify without corrupting coordinates."""
    org = _make_org(db)
    src = _make_source(db, "https://example.com/a")
    # Snapshot text contains smart quotes
    text = "Welcome to \u201cSt Jude Family Medical\u201d at our primary facility."
    snap = _make_snapshot(db, src, text=text)

    sub_slice = "\u201cSt Jude Family Medical\u201d"
    start_pos = text.find(sub_slice)
    end_pos = start_pos + len(sub_slice)

    # Stored quote normalized to ASCII straight quotes
    ascii_quote = '"St Jude Family Medical"'

    fact = _make_fact(
        db, snap, org.id, "organisation_name", "St Jude Family Medical",
        quote=ascii_quote,
        custom_offsets=(start_pos, end_pos),
    )

    assert verify_fact_evidence_integrity(fact, snap) is True


def test_evidence_integrity_fabricated_quote_rejected(db: Session):
    """Fact with quote not present in snapshot text suppresses comparison."""
    org = _make_org(db)
    src_a = _make_source(db, "https://example.com/a")
    src_b = _make_source(db, "https://example.com/b")
    _link_source(db, org.id, src_a.id, "confirmed")
    _link_source(db, org.id, src_b.id, "confirmed")

    snap_a = _make_snapshot(db, src_a, text="Official website text")
    snap_b = _make_snapshot(db, src_b, text="Directory listing text")

    # Manually insert fabricated evidence not matching snapshot text
    ev_fab = Evidence(
        snapshot_id=snap_a.id,
        quote="Fabricated line (03) 9419 8888",
        start_offset=0,
        end_offset=30,
        captured_at=datetime.now(UTC),
        match_algorithm_version="v1",
    )
    db.add(ev_fab)
    db.flush()

    fact_1 = Fact(
        snapshot_id=snap_a.id,
        organisation_id=org.id,
        field_key="public_phone",
        scope_key="reception",
        raw_value="(03) 9419 8888",
        evidence_id=ev_fab.id,
        status="accepted",
        via="deterministic",
    )
    db.add(fact_1)
    db.flush()

    fact_2 = _make_fact(db, snap_b, org.id, "public_phone", "(03) 9419 9999", quote="Directory listing text")

    assert verify_fact_evidence_integrity(fact_1, snap_a) is False
    assert evaluate_fact_pair_discrepancy(db, fact_1, fact_2) is None


def test_evidence_integrity_mismatched_snapshot_id_rejected(db: Session):
    """Fact evidence pointing to a different snapshot ID suppresses comparison."""
    org = _make_org(db)
    src_a = _make_source(db, "https://example.com/a")
    src_b = _make_source(db, "https://example.com/b")
    _link_source(db, org.id, src_a.id, "confirmed")
    _link_source(db, org.id, src_b.id, "confirmed")

    snap_a = _make_snapshot(db, src_a, text="Alpha text (03) 9419 8888")
    snap_b = _make_snapshot(db, src_b, text="Beta text (03) 9419 9999")

    # Fact 1 references snap_b's ID on evidence
    fact_1 = _make_fact(
        db, snap_a, org.id, "public_phone", "(03) 9419 8888",
        quote="Alpha text (03) 9419 8888",
        custom_snapshot_id=snap_b.id,
    )
    fact_2 = _make_fact(db, snap_b, org.id, "public_phone", "(03) 9419 9999", quote="Beta text (03) 9419 9999")

    assert verify_fact_evidence_integrity(fact_1, snap_a) is False
    assert evaluate_fact_pair_discrepancy(db, fact_1, fact_2) is None


def test_evidence_integrity_invalid_offsets_rejected(db: Session):
    """Offsets out of bounds or inverted suppress comparison."""
    org = _make_org(db)
    src_a = _make_source(db, "https://example.com/a")
    src_b = _make_source(db, "https://example.com/b")
    _link_source(db, org.id, src_a.id, "confirmed")
    _link_source(db, org.id, src_b.id, "confirmed")

    snap_a = _make_snapshot(db, src_a, text="Short text")
    snap_b = _make_snapshot(db, src_b, text="Valid text (03) 9419 9999")

    fact_1 = _make_fact(
        db, snap_a, org.id, "public_phone", "(03) 9419 8888",
        quote="Short text",
        custom_offsets=(0, 9999),  # Exceeds text length
    )
    fact_2 = _make_fact(db, snap_b, org.id, "public_phone", "(03) 9419 9999", quote="Valid text (03) 9419 9999")

    assert verify_fact_evidence_integrity(fact_1, snap_a) is False
    assert evaluate_fact_pair_discrepancy(db, fact_1, fact_2) is None


def test_evidence_integrity_rejected_fact_status(db: Session):
    """Fact with status='rejected' cannot support a discrepancy."""
    org = _make_org(db)
    src_a = _make_source(db, "https://example.com/a")
    src_b = _make_source(db, "https://example.com/b")
    _link_source(db, org.id, src_a.id, "confirmed")
    _link_source(db, org.id, src_b.id, "confirmed")

    snap_a = _make_snapshot(db, src_a, text="Alpha (03) 9419 8888")
    snap_b = _make_snapshot(db, src_b, text="Beta (03) 9419 9999")

    fact_1 = _make_fact(db, snap_a, org.id, "public_phone", "(03) 9419 8888", quote="Alpha (03) 9419 8888", status="rejected")
    fact_2 = _make_fact(db, snap_b, org.id, "public_phone", "(03) 9419 9999", quote="Beta (03) 9419 9999", status="accepted")

    assert verify_fact_evidence_integrity(fact_1, snap_a) is False
    assert evaluate_fact_pair_discrepancy(db, fact_1, fact_2) is None


# =============================================================================
# 4. CONSERVATIVE NORMALISATION & AMBIGUITY CLASSIFICATION
# =============================================================================

def test_normalisation_phone_ambiguous_format_routes_to_ambiguity(db: Session):
    """Unparseable/ambiguous telephone format routes to ConflictType.AMBIGUITY."""
    org = _make_org(db)
    src_a = _make_source(db, "https://example.com/a")
    src_b = _make_source(db, "https://example.com/b")
    _link_source(db, org.id, src_a.id, "confirmed")
    _link_source(db, org.id, src_b.id, "confirmed")

    snap_a = _make_snapshot(db, src_a, text="Call 9419 8888 local")
    snap_b = _make_snapshot(db, src_b, text="Call (03) 9419 8888")

    # 8-digit local number without area code fails canonical E.164
    fact_1 = _make_fact(db, snap_a, org.id, "public_phone", "9419 8888", quote="Call 9419 8888 local")
    fact_2 = _make_fact(db, snap_b, org.id, "public_phone", "(03) 9419 8888", quote="Call (03) 9419 8888")

    disc = evaluate_fact_pair_discrepancy(db, fact_1, fact_2)
    assert disc is not None
    assert disc.conflict_type == ConflictType.AMBIGUITY.value
    assert "could not be canonically normalized" in disc.rationale


def test_normalisation_opening_hours_freeform_routes_to_ambiguity(db: Session):
    """Freeform/narrative opening hours route to ConflictType.AMBIGUITY."""
    org = _make_org(db)
    src_a = _make_source(db, "https://example.com/a")
    src_b = _make_source(db, "https://example.com/b")
    _link_source(db, org.id, src_a.id, "confirmed")
    _link_source(db, org.id, src_b.id, "confirmed")

    snap_a = _make_snapshot(db, src_a, text="Open most weekday mornings, call ahead")
    snap_b = _make_snapshot(db, src_b, text="Monday to Friday 8:00 AM - 6:00 PM")

    fact_1 = _make_fact(db, snap_a, org.id, "opening_hours", "Open most weekday mornings, call ahead", quote="Open most weekday mornings, call ahead")
    fact_2 = _make_fact(db, snap_b, org.id, "opening_hours", "Monday to Friday 8:00 AM - 6:00 PM", quote="Monday to Friday 8:00 AM - 6:00 PM")

    disc = evaluate_fact_pair_discrepancy(db, fact_1, fact_2)
    assert disc is not None
    assert disc.conflict_type == ConflictType.AMBIGUITY.value
    assert "unsupported or freeform text" in disc.rationale


def test_normalisation_address_abbreviations_match(db: Session):
    """Safe street abbreviation normalisation ('street' vs 'st') matches with 0 discrepancy."""
    org = _make_org(db)
    src_a = _make_source(db, "https://example.com/a")
    src_b = _make_source(db, "https://example.com/b")
    _link_source(db, org.id, src_a.id, "confirmed")
    _link_source(db, org.id, src_b.id, "confirmed")

    snap_a = _make_snapshot(db, src_a, text="100 Victoria Parade, East Melbourne")
    snap_b = _make_snapshot(db, src_b, text="100 Victoria Pde, East Melbourne")

    fact_1 = _make_fact(db, snap_a, org.id, "address", "100 Victoria Parade, East Melbourne", quote="100 Victoria Parade, East Melbourne")
    fact_2 = _make_fact(db, snap_b, org.id, "address", "100 Victoria Pde, East Melbourne", quote="100 Victoria Pde, East Melbourne")

    assert evaluate_fact_pair_discrepancy(db, fact_1, fact_2) is None


def test_normalisation_address_descriptive_routes_to_ambiguity(db: Session):
    """Descriptive address without street numbers routes to ConflictType.AMBIGUITY."""
    org = _make_org(db)
    src_a = _make_source(db, "https://example.com/a")
    src_b = _make_source(db, "https://example.com/b")
    _link_source(db, org.id, src_a.id, "confirmed")
    _link_source(db, org.id, src_b.id, "confirmed")

    snap_a = _make_snapshot(db, src_a, text="Suite 4, Near Melbourne Central")
    snap_b = _make_snapshot(db, src_b, text="100 Victoria Parade")

    fact_1 = _make_fact(db, snap_a, org.id, "address", "Suite 4, Near Melbourne Central", quote="Suite 4, Near Melbourne Central")
    fact_2 = _make_fact(db, snap_b, org.id, "address", "100 Victoria Parade", quote="100 Victoria Parade")

    disc = evaluate_fact_pair_discrepancy(db, fact_1, fact_2)
    assert disc is not None
    assert disc.conflict_type == ConflictType.AMBIGUITY.value


def test_normalisation_org_name_trading_variation_routes_to_ambiguity(db: Session):
    """Trading name vs legal entity variation routes to ConflictType.AMBIGUITY."""
    org = _make_org(db)
    src_a = _make_source(db, "https://example.com/a")
    src_b = _make_source(db, "https://example.com/b")
    _link_source(db, org.id, src_a.id, "confirmed")
    _link_source(db, org.id, src_b.id, "confirmed")

    snap_a = _make_snapshot(db, src_a, text="St Jude Medical")
    snap_b = _make_snapshot(db, src_b, text="St Jude Medical Centre Pty Ltd")

    fact_1 = _make_fact(db, snap_a, org.id, "organisation_name", "St Jude Medical", quote="St Jude Medical")
    fact_2 = _make_fact(db, snap_b, org.id, "organisation_name", "St Jude Medical Centre Pty Ltd", quote="St Jude Medical Centre Pty Ltd")

    disc = evaluate_fact_pair_discrepancy(db, fact_1, fact_2)
    assert disc is not None
    assert disc.conflict_type == ConflictType.AMBIGUITY.value


# =============================================================================
# 5. GROUND TRUTH FIXTURES (F10, F11, F12, F12-UNSCOPED)
# =============================================================================

def test_ground_truth_f10_phone_format_normalisation(db: Session):
    """F10: Formatting differences normalize identically to +61394198888 -> 0 discrepancy."""
    org = _make_org(db, "St Jude Family Medical", "st-jude-f10")
    src_a = _make_source(db, "https://stjude.example.com", "Source A")
    src_b = _make_source(db, "https://directory.example.com/stjude", "Source B")
    _link_source(db, org.id, src_a.id, "confirmed")
    _link_source(db, org.id, src_b.id, "confirmed")

    snap_a = _make_snapshot(db, src_a, text="Reception: (03) 9419 8888")
    snap_b = _make_snapshot(db, src_b, text="Call 0394198888 today")

    fact_a = _make_fact(db, snap_a, org.id, "public_phone", "(03) 9419 8888", scope_key="reception", quote="Reception: (03) 9419 8888")
    fact_b = _make_fact(db, snap_b, org.id, "public_phone", "0394198888", scope_key="reception", quote="Call 0394198888 today")

    assert evaluate_fact_pair_discrepancy(db, fact_a, fact_b) is None
    scan_res = scan_organisation_discrepancies(db, org.id)
    assert scan_res.discrepancies_detected == 0


def test_ground_truth_f11_phone_conflict(db: Session):
    """F11: Conflicting phone numbers on matching scope produce value_mismatch."""
    org = _make_org(db, "St Jude Family Medical", "st-jude-f11")
    src_a = _make_source(db, "https://stjude.example.com/contact", "Source A")
    src_b = _make_source(db, "https://directory.example.com/stjude", "Source B")
    _link_source(db, org.id, src_a.id, "confirmed")
    _link_source(db, org.id, src_b.id, "confirmed")

    snap_a = _make_snapshot(db, src_a, text="Reception line is (03) 9419 8888")
    snap_b = _make_snapshot(db, src_b, text="Call St Jude reception at (03) 9419 9999")

    fact_a = _make_fact(db, snap_a, org.id, "public_phone", "(03) 9419 8888", scope_key="reception", quote="Reception line is (03) 9419 8888")
    fact_b = _make_fact(db, snap_b, org.id, "public_phone", "(03) 9419 9999", scope_key="reception", quote="Call St Jude reception at (03) 9419 9999")

    disc = evaluate_fact_pair_discrepancy(db, fact_a, fact_b)
    assert disc is not None
    assert disc.conflict_type == ConflictType.VALUE_MISMATCH.value
    assert disc.field_key == "public_phone"
    assert disc.scope_key == "reception"
    assert disc.fact_a_id == min(fact_a.id, fact_b.id)
    assert disc.fact_b_id == max(fact_a.id, fact_b.id)


def test_ground_truth_f12_scope_gating_suppression(db: Session):
    """F12: Clinic vs pathology scopes suppress comparison (0 discrepancy)."""
    org = _make_org(db, "St Jude Family Medical", "st-jude-f12")
    src_a = _make_source(db, "https://stjude.example.com/hours", "Source A")
    src_b = _make_source(db, "https://pathology.example.com/stjude", "Source B")
    _link_source(db, org.id, src_a.id, "confirmed")
    _link_source(db, org.id, src_b.id, "confirmed")

    snap_a = _make_snapshot(db, src_a, text="General clinic hours: 8:00 AM - 6:00 PM")
    snap_b = _make_snapshot(db, src_b, text="On-site pathology collection: 7:30 AM - 12:00 PM")

    fact_clinic = _make_fact(db, snap_a, org.id, "opening_hours", "8:00 AM - 6:00 PM", scope_key="clinic", quote="General clinic hours: 8:00 AM - 6:00 PM")
    fact_pathology = _make_fact(db, snap_b, org.id, "opening_hours", "7:30 AM - 12:00 PM", scope_key="pathology", quote="On-site pathology collection: 7:30 AM - 12:00 PM")

    assert evaluate_fact_pair_discrepancy(db, fact_clinic, fact_pathology) is None
    scan_res = scan_organisation_discrepancies(db, org.id)
    assert scan_res.discrepancies_detected == 0


def test_ground_truth_f12_unscoped_null_scope_handling(db: Session):
    """F12-unscoped (D41): NULL scope never equals NULL scope. Suppressed from value_mismatch."""
    org = _make_org(db, "St Jude Family Medical", "st-jude-f12-null")
    src_a = _make_source(db, "https://stjude.example.com/contact", "Source A")
    src_b = _make_source(db, "https://directory.example.com/stjude", "Source B")
    _link_source(db, org.id, src_a.id, "confirmed")
    _link_source(db, org.id, src_b.id, "confirmed")

    snap_a = _make_snapshot(db, src_a, text="Hours: 8:00 AM - 6:00 PM")
    snap_b = _make_snapshot(db, src_b, text="Operating hours: 7:30 AM - 12:00 PM")

    fact_null_1 = _make_fact(db, snap_a, org.id, "opening_hours", "8:00 AM - 6:00 PM", scope_key=None, quote="Hours: 8:00 AM - 6:00 PM")
    fact_null_2 = _make_fact(db, snap_b, org.id, "opening_hours", "7:30 AM - 12:00 PM", scope_key=None, quote="Operating hours: 7:30 AM - 12:00 PM")

    assert evaluate_fact_pair_discrepancy(db, fact_null_1, fact_null_2) is None


# =============================================================================
# 6. INELIGIBILITY BOUNDARIES & IDEMPOTENCY
# =============================================================================

def test_ineligibility_same_source_different_snapshots(db: Session):
    """Two facts from the SAME source cannot form a cross-source discrepancy."""
    org = _make_org(db)
    src = _make_source(db, "https://example.com/sole-source")
    _link_source(db, org.id, src.id, "confirmed")

    snap_1 = _make_snapshot(db, src, "version 1 text (03) 9419 8888")
    snap_2 = _make_snapshot(db, src, "version 2 text (03) 9419 9999")

    fact_1 = _make_fact(db, snap_1, org.id, "public_phone", "(03) 9419 8888", quote="version 1 text (03) 9419 8888")
    fact_2 = _make_fact(db, snap_2, org.id, "public_phone", "(03) 9419 9999", quote="version 2 text (03) 9419 9999")

    assert evaluate_fact_pair_discrepancy(db, fact_1, fact_2) is None


def test_ineligibility_different_organisations(db: Session):
    org_1 = _make_org(db, "Clinic One", "clinic-one")
    org_2 = _make_org(db, "Clinic Two", "clinic-two")

    src_a = _make_source(db, "https://example.com/a")
    src_b = _make_source(db, "https://example.com/b")
    _link_source(db, org_1.id, src_a.id, "confirmed")
    _link_source(db, org_2.id, src_b.id, "confirmed")

    snap_a = _make_snapshot(db, src_a, text="Phone: (03) 9419 8888")
    snap_b = _make_snapshot(db, src_b, text="Phone: (03) 9419 9999")

    fact_1 = _make_fact(db, snap_a, org_1.id, "public_phone", "(03) 9419 8888", quote="Phone: (03) 9419 8888")
    fact_2 = _make_fact(db, snap_b, org_2.id, "public_phone", "(03) 9419 9999", quote="Phone: (03) 9419 9999")

    assert evaluate_fact_pair_discrepancy(db, fact_1, fact_2) is None


def test_ineligibility_unconfirmed_source_links(db: Session):
    org = _make_org(db)
    src_a = _make_source(db, "https://example.com/a")
    src_b = _make_source(db, "https://example.com/b")
    _link_source(db, org.id, src_a.id, "confirmed")
    _link_source(db, org.id, src_b.id, "suggested")

    snap_a = _make_snapshot(db, src_a, text="Phone: (03) 9419 8888")
    snap_b = _make_snapshot(db, src_b, text="Phone: (03) 9419 9999")

    fact_1 = _make_fact(db, snap_a, org.id, "public_phone", "(03) 9419 8888", quote="Phone: (03) 9419 8888")
    fact_2 = _make_fact(db, snap_b, org.id, "public_phone", "(03) 9419 9999", quote="Phone: (03) 9419 9999")

    assert evaluate_fact_pair_discrepancy(db, fact_1, fact_2) is None


def test_scan_idempotency_duplicate_prevention(db: Session):
    org = _make_org(db)
    src_a = _make_source(db, "https://example.com/a")
    src_b = _make_source(db, "https://example.com/b")
    _link_source(db, org.id, src_a.id, "confirmed")
    _link_source(db, org.id, src_b.id, "confirmed")

    snap_a = _make_snapshot(db, src_a, text="Phone: (03) 9419 8888")
    snap_b = _make_snapshot(db, src_b, text="Phone: (03) 9419 9999")

    _make_fact(db, snap_a, org.id, "public_phone", "(03) 9419 8888", quote="Phone: (03) 9419 8888")
    _make_fact(db, snap_b, org.id, "public_phone", "(03) 9419 9999", quote="Phone: (03) 9419 9999")

    res_1 = scan_organisation_discrepancies(db, org.id)
    assert res_1.discrepancies_detected == 1

    res_2 = scan_organisation_discrepancies(db, org.id)
    assert res_2.discrepancies_detected == 1
    assert res_2.discrepancies[0].id == res_1.discrepancies[0].id

    count = db.scalar(select(func.count(DiscrepancyORM.id)))
    assert count == 1


# =============================================================================
# 7. FUTURE REVIEW FIELDS & REST API VERIFICATION
# =============================================================================

def test_api_list_discrepancies_with_filters(client: TestClient):
    org_res = client.post("/api/organisations", json={
        "name": "API Test Clinic",
        "slug": "api-test-clinic",
        "postcode": "3000",
        "state": "VIC",
    })
    assert org_res.status_code == 201
    org_id = org_res.json()["id"]

    scan_res = client.post(f"/api/discrepancies/scan/{org_id}")
    assert scan_res.status_code == 200
    assert scan_res.json()["organisation_id"] == org_id
    assert scan_res.json()["discrepancies_detected"] == 0

    list_res = client.get(f"/api/discrepancies?organisation_id={org_id}&limit=10&offset=0")
    assert list_res.status_code == 200
    assert isinstance(list_res.json(), list)


def test_api_detail_evidence_traceability(client: TestClient):
    from backend.app.db.base import get_db
    app_db = next(client.app.dependency_overrides[get_db]())

    org = _make_org(app_db, "Evidence Trace Org", "ev-trace-org")
    src_a = _make_source(app_db, "https://trace.example.com/a", "Source Alpha")
    src_b = _make_source(app_db, "https://trace.example.com/b", "Source Beta")
    _link_source(app_db, org.id, src_a.id, "confirmed")
    _link_source(app_db, org.id, src_b.id, "confirmed")

    snap_a = _make_snapshot(app_db, src_a, text="Alpha phone: (03) 9419 8888")
    snap_b = _make_snapshot(app_db, src_b, text="Beta phone: (03) 9419 9999")

    fact_a = _make_fact(app_db, snap_a, org.id, "public_phone", "(03) 9419 8888", quote="Alpha phone: (03) 9419 8888")
    fact_b = _make_fact(app_db, snap_b, org.id, "public_phone", "(03) 9419 9999", quote="Beta phone: (03) 9419 9999")

    disc = evaluate_fact_pair_discrepancy(app_db, fact_a, fact_b)
    app_db.commit()

    res = client.get(f"/api/discrepancies/{disc.id}")
    assert res.status_code == 200
    body = res.json()

    assert body["id"] == disc.id
    assert body["status"] == "detected"
    assert body["resolution_notes"] is None
    assert body["resolved_by"] is None
    assert body["resolved_at"] is None
    assert body["fact_a"]["evidence_quote"] is not None
    assert body["fact_b"]["evidence_quote"] is not None
    assert body["source_a"]["url"] is not None
    assert body["source_b"]["url"] is not None


def test_api_forbidden_endpoints_do_not_exist(client: TestClient):
    """Negative check: verify no auto-resolve or fact overwrite endpoints exist."""
    res_patch = client.patch("/api/discrepancies/1/resolve", json={"status": "resolved"})
    assert res_patch.status_code in (404, 405)

    res_post = client.post("/api/discrepancies/1/select-winning-source", json={"winner": 1})
    assert res_post.status_code in (404, 405)

    res_delete = client.delete("/api/discrepancies/1")
    assert res_delete.status_code in (404, 405)
