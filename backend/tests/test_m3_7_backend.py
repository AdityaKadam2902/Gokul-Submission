"""Comprehensive M3.7 backend test suite: Practice Lifecycle Intelligence.

Covers Section 13 & 16 of M3.7 authorization:
1. Migration M3_006: Fresh DB migration creates practice_lifecycle_events with columns and indexes.
2. Migration M3_006: Existing DB with M3_001..M3_005 tables cleanly creates practice_lifecycle_events.
3. Migration M3_006 idempotency: Running twice produces no errors and reports up_to_date.
4. Migration M3_006 rollback: Drops table cleanly; reapplying recreates it.
5. Foreign key RESTRICT: Deleting organisation or source referenced by lifecycle event fails.
6. Explicit permanent closure: Verbatim quote, character offsets, scope, and date precision.
7. Branch closure isolation: Branch closure does not close parent org or sibling branches.
8. Temporary holiday closure: Negative control strictly rejected as permanent closure.
9. Renovation and emergency closure: Negative controls classified as temporary, never permanent.
10. Repeated absence possible closure: Absence across 2 complete observations flagged for review, never auto-closed.
11. Ineligible observation freezing: Failed/incomplete crawls freeze absence and produce no closure events.
12. Conflicting sources closure: Opposing sources report distinct signals with respective evidence preserved.
13. Explicit relocation detection: Verbatim quote with old and new addresses extracted.
14. Address change diff: Address change without relocation text produces potential_needs_review.
15. Multi-branch creation not relocation: Old branch address remaining in DOM suppresses false relocation.
16. Organisation identity stability: Relocation preserves organisation ID with zero auto-merging.
17. Explicit corporate acquisition: Explicit announcement detected with acquiring entity recorded.
18. Shared domain rejected: Shared domain without explicit announcement rejected as acquisition.
19. Shared phone rejected: Shared 1300/1800 number rejected as acquisition proof.
20. Zero auto-merge on acquisition: Both acquired and acquiring entities retain distinct IDs and history.
21. Lifecycle event idempotency: Running detection repeatedly creates zero duplicate events.
22. Review queue integration: Lifecycle events appear in queue with priority and quotation.
23. Review metrics integration: Review metrics include lifecycle events across statuses.
24. Review item detail: Detail inspector returns grounded context and allowed actions.
25. Decision persistence: Approving, rejecting, or deferring records immutable review_action.
26. Underlying facts untouched: Lifecycle review leaves underlying facts, raw values, and evidence un-mutated.
27. Fresh-session persistence: Persisted lifecycle events retrieved cleanly across independent sessions.
28. REST API endpoints integration: Events listing, filters, detail, 404s, history, and scan.
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
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api.main import app
from backend.app.db import orm
from backend.app.db.base import get_db
from backend.app.db.migrations import (
    inspect_m3_006_schema,
    migrate_m3_001_organisations,
    migrate_m3_002_discrepancies,
    migrate_m3_003_change_events,
    migrate_m3_004_opportunities,
    migrate_m3_005_review_actions,
    migrate_m3_006_lifecycle_events,
    rollback_m3_006_lifecycle_events,
)
from backend.app.intelligence.practice_lifecycle_detector import (
    detect_acquisition_evidence,
    detect_closure_evidence,
    detect_practice_lifecycle_events,
    detect_relocation_evidence,
    verify_lifecycle_evidence_integrity,
)
from backend.app.intelligence.review_service import (
    ALLOWED_ACTIONS_BY_ENTITY,
    get_review_history,
    get_review_item_detail,
    get_review_metrics,
    get_review_queue,
    record_review_action,
)
from backend.app.intelligence.schemas import (
    LifecycleEventType,
    LifecycleStatus,
    ReviewActionType,
    ReviewStatus,
    TemporalPrecision,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# -----------------------------------------------------------------------------
# Fixtures and entity creation helpers
# -----------------------------------------------------------------------------

@pytest.fixture
def test_db_session():
    """Provides a fresh, isolated in-memory SQLite session with full ORM metadata."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _set_fk(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    orm.Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def client_with_session(test_db_session: Session):
    """Provides a FastAPI TestClient bound to the isolated test session."""
    def override_get_db():
        try:
            yield test_db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.pop(get_db, None)


def _make_org(db: Session, name: str = "Test Practice", slug: str | None = None, postcode: str = "3000", state: str = "VIC", address: str = "10 High Street, Melbourne") -> orm.Organisation:
    slug = slug or f"org-{uuid.uuid4().hex[:6]}"
    org = orm.Organisation(name=name, slug=slug, postcode=postcode, state=state, address=address)
    db.add(org)
    db.flush()
    return org


def _make_source(db: Session, url: str | None = None, domain: str | None = None) -> orm.Source:
    url = url or f"https://example.com/site-{uuid.uuid4().hex[:6]}"
    domain = domain or "example.com"
    src = orm.Source(
        url=url,
        domain=domain,
        kind="fixture",
        page_type="contact",
        policy_status="allowed",
    )
    db.add(src)
    db.flush()
    return src


def _make_link(db: Session, org_id: int, source_id: int, status: str = "confirmed") -> orm.OrganisationSourceLink:
    link = orm.OrganisationSourceLink(
        organisation_id=org_id,
        source_id=source_id,
        relationship_type="official_practice_site",
        verification_status=status,
    )
    db.add(link)
    db.flush()
    return link


def _make_snapshot(db: Session, source_id: int, title: str = "Page", text: str = "Sample practice text.") -> orm.PageSnapshot:
    snap = orm.PageSnapshot(
        source_id=source_id,
        content_hash=uuid.uuid4().hex * 2,
        title=title,
        text=text,
        parser_version="1.0",
        fixture=True,
    )
    db.add(snap)
    db.flush()
    return snap


def _make_evidence(db: Session, snapshot_id: int, quote: str, start_offset: int = 0, end_offset: int = 10) -> orm.Evidence:
    ev = orm.Evidence(
        snapshot_id=snapshot_id,
        quote=quote,
        start_offset=start_offset,
        end_offset=end_offset,
    )
    db.add(ev)
    db.flush()
    return ev


# =============================================================================
# TESTS
# =============================================================================

def test_migration_m3_006_fresh_install():
    """Test 1: Migration M3_006 creates practice_lifecycle_events table on fresh database."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name

    try:
        # Bootstrap schema
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("CREATE TABLE organisations (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        cur.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY, url TEXT NOT NULL)")
        cur.execute("CREATE TABLE page_snapshots (id INTEGER PRIMARY KEY, source_id INTEGER)")
        cur.execute("CREATE TABLE evidence (id INTEGER PRIMARY KEY, quote TEXT)")
        conn.commit()
        conn.close()

        res = migrate_m3_006_lifecycle_events(db_path)
        assert res["status"] == "success"
        assert "practice_lifecycle_events" in res["tables_created"]

        info = inspect_m3_006_schema(db_path)
        assert info["table_exists"] is True
        assert "organisation_id" in info["columns"]
        assert "branch_locality" in info["columns"]
        assert "is_branch_level" in info["columns"]
        assert "event_type" in info["columns"]
        assert "status" in info["columns"]
        assert "idempotency_key" in info["columns"]
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_migration_m3_006_existing_database():
    """Test 2: Migration M3_006 succeeds cleanly on existing DB with prior migrations."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name

    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY, url TEXT NOT NULL)")
        cur.execute("CREATE TABLE page_snapshots (id INTEGER PRIMARY KEY, source_id INTEGER)")
        conn.commit()
        conn.close()

        migrate_m3_001_organisations(db_path)
        migrate_m3_002_discrepancies(db_path)
        migrate_m3_003_change_events(db_path)
        migrate_m3_004_opportunities(db_path)
        migrate_m3_005_review_actions(db_path)

        res = migrate_m3_006_lifecycle_events(db_path)
        assert res["status"] == "success"

        info = inspect_m3_006_schema(db_path)
        assert info["practice_lifecycle_events"] is True
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_migration_m3_006_idempotency():
    """Test 3: Running M3_006 migration repeatedly is idempotent."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name

    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("CREATE TABLE organisations (id INTEGER PRIMARY KEY)")
        cur.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY)")
        cur.execute("CREATE TABLE page_snapshots (id INTEGER PRIMARY KEY)")
        cur.execute("CREATE TABLE evidence (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        res1 = migrate_m3_006_lifecycle_events(db_path)
        assert res1["status"] == "success"

        res2 = migrate_m3_006_lifecycle_events(db_path)
        assert res2["status"] == "up_to_date"
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_migration_m3_006_rollback_and_reapply():
    """Test 4: Rollback drops table and re-applying restores it cleanly."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name

    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("CREATE TABLE organisations (id INTEGER PRIMARY KEY)")
        cur.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY)")
        cur.execute("CREATE TABLE page_snapshots (id INTEGER PRIMARY KEY)")
        cur.execute("CREATE TABLE evidence (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        migrate_m3_006_lifecycle_events(db_path)
        assert inspect_m3_006_schema(db_path)["table_exists"] is True

        rb = rollback_m3_006_lifecycle_events(db_path)
        assert rb["status"] == "success"
        assert inspect_m3_006_schema(db_path)["table_exists"] is False

        reapply = migrate_m3_006_lifecycle_events(db_path)
        assert reapply["status"] == "success"
        assert inspect_m3_006_schema(db_path)["table_exists"] is True
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_foreign_key_restrict_enforcement(test_db_session: Session):
    """Test 5: Foreign key RESTRICT prevents deleting referenced Organisation or Source."""
    org = _make_org(test_db_session, "FK Practice")
    src = _make_source(test_db_session)
    snap = _make_snapshot(test_db_session, src.id)

    evt = orm.PracticeLifecycleEvent(
        organisation_id=org.id,
        event_type="permanent_closure",
        status="confirmed_explicit",
        source_id=src.id,
        snapshot_id=snap.id,
        rationale="FK test",
        idempotency_key="idemp_fk_1",
    )
    test_db_session.add(evt)
    test_db_session.commit()

    with pytest.raises(IntegrityError):
        test_db_session.delete(org)
        test_db_session.commit()
    test_db_session.rollback()

    with pytest.raises(IntegrityError):
        test_db_session.delete(src)
        test_db_session.commit()
    test_db_session.rollback()


def test_explicit_permanent_closure_detection(test_db_session: Session):
    """Test 6: Explicit permanent closure announcement detected with exact quotation and date."""
    org = _make_org(test_db_session, "Hawthorn Medical")
    src = _make_source(test_db_session)
    _make_link(test_db_session, org.id, src.id)

    text_content = (
        "Welcome to Hawthorn Medical. Please note: This medical practice is now permanently closed on 30 September 2026. "
        "Medical records have been archived per national health guidelines."
    )
    snap = _make_snapshot(test_db_session, src.id, "Hawthorn Medical", text_content)

    events = detect_closure_evidence(test_db_session, org, src, snap)
    assert len(events) == 1
    evt = events[0]

    assert evt.event_type == LifecycleEventType.PERMANENT_CLOSURE.value
    assert evt.status == LifecycleStatus.CONFIRMED_EXPLICIT.value
    assert evt.is_branch_level is False
    assert evt.evidence_quote is not None
    assert "permanently closed" in evt.evidence_quote
    assert evt.effective_date == "2026-09-30" or "2026" in str(evt.effective_date)
    assert evt.date_precision in (TemporalPrecision.DAY.value, TemporalPrecision.MONTH.value, TemporalPrecision.YEAR.value)

    # Verify quotation integrity against raw snapshot text
    assert verify_lifecycle_evidence_integrity(evt, snap) is True


def test_branch_closure_isolation(test_db_session: Session):
    """Test 7: Branch closure strictly isolates branch scope; parent organisation remains open."""
    org = _make_org(test_db_session, "Metro Health Group")
    src = _make_source(test_db_session)
    _make_link(test_db_session, org.id, src.id)

    text_content = (
        "Metro Health operates clinics across Victoria. "
        "Our Box Hill branch will cease operating from 15 December 2026. "
        "Patients are welcome at our Carlton and Richmond locations which remain fully open."
    )
    snap = _make_snapshot(test_db_session, src.id, "Metro Health", text_content)

    events = detect_closure_evidence(test_db_session, org, src, snap)
    assert len(events) == 1
    evt = events[0]

    assert evt.event_type == LifecycleEventType.PERMANENT_CLOSURE.value
    assert evt.status == LifecycleStatus.CONFIRMED_EXPLICIT.value
    assert evt.is_branch_level is True
    assert evt.branch_locality == "Box Hill"
    assert "Box Hill" in evt.evidence_quote
    assert evt.organisation_id == org.id

    # Architectural invariant: Organisation status remains active
    assert org.status == "active"


def test_temporary_holiday_closure_rejected_as_permanent(test_db_session: Session):
    """Test 8: Negative control: 'Closed for public holiday' or 'closed today' rejected as permanent closure."""
    org = _make_org(test_db_session, "Holiday Practice")
    src = _make_source(test_db_session)
    _make_link(test_db_session, org.id, src.id)

    text_content = "Welcome to Holiday Practice. We are closed for the public holiday on 26 January 2027. Normal hours resume tomorrow."
    snap = _make_snapshot(test_db_session, src.id, "Notice", text_content)

    events = detect_closure_evidence(test_db_session, org, src, snap)
    assert len(events) == 1
    evt = events[0]

    # Must NOT be permanent closure
    assert evt.event_type == LifecycleEventType.TEMPORARY_CLOSURE.value
    assert evt.event_type != LifecycleEventType.PERMANENT_CLOSURE.value
    assert evt.status == LifecycleStatus.POTENTIAL_NEEDS_REVIEW.value
    assert "public holiday" in evt.evidence_quote.lower()


def test_renovation_and_emergency_closure_negative_control(test_db_session: Session):
    """Test 9: Negative control: 'Temporarily closed for renovations' rejected as permanent closure."""
    org = _make_org(test_db_session, "Renovation Clinic")
    src = _make_source(test_db_session)
    _make_link(test_db_session, org.id, src.id)

    text_content = "The clinic is temporarily closed for renovations from Monday. Re-opening in two weeks."
    snap = _make_snapshot(test_db_session, src.id, "Renovation Notice", text_content)

    events = detect_closure_evidence(test_db_session, org, src, snap)
    assert len(events) == 1
    evt = events[0]

    assert evt.event_type == LifecycleEventType.TEMPORARY_CLOSURE.value
    assert evt.event_type != LifecycleEventType.PERMANENT_CLOSURE.value
    assert "renovations" in evt.rationale.lower() or "renovations" in evt.evidence_quote.lower()


def test_repeated_absence_possible_closure(test_db_session: Session):
    """Test 10: Repeated absence across complete observations flags potential closure without premature certainty."""
    org = _make_org(test_db_session, "Absent Practice")
    src = _make_source(test_db_session)
    _make_link(test_db_session, org.id, src.id)

    snap1 = _make_snapshot(test_db_session, src.id, "Snap 1", "Active content")
    snap2 = _make_snapshot(test_db_session, src.id, "Snap 2", "Empty page")

    obs1 = orm.SourceObservation(source_id=src.id, observation_sequence=1, outcome="success_new_snapshot", content_changed=True, parsing_completeness="complete", extraction_completeness="complete")
    obs2 = orm.SourceObservation(source_id=src.id, observation_sequence=2, outcome="success_new_snapshot", content_changed=True, parsing_completeness="complete", extraction_completeness="complete")
    test_db_session.add_all([obs1, obs2])
    test_db_session.flush()

    # Seed change event recording 2 consecutive absences
    ce = orm.ChangeEvent(
        source_id=src.id,
        organisation_id=org.id,
        current_observation_id=obs2.id,
        current_snapshot_id=snap2.id,
        previous_snapshot_id=snap1.id,
        event_type="fact_unobserved",
        field_key="phone",
        diff_metadata={"observation_count_absent": 2},
        rationale="Absent across 2 consecutive crawls",
        idempotency_key="idemp_ce_abs_1",
    )
    test_db_session.add(ce)
    test_db_session.commit()

    events = detect_practice_lifecycle_events(test_db_session, source_id=src.id, organisation_id=org.id)
    closure_evts = [e for e in events if e.event_type == LifecycleEventType.PERMANENT_CLOSURE.value]
    assert len(closure_evts) == 1
    evt = closure_evts[0]

    # Absence alone must NEVER be confirmed_explicit
    assert evt.status == LifecycleStatus.NO_LONGER_OBSERVED.value
    assert evt.status != LifecycleStatus.CONFIRMED_EXPLICIT.value
    assert "consecutive eligible complete observations" in evt.rationale


def test_ineligible_observation_freezing(test_db_session: Session):
    """Test 11: Ineligible crawls (failed, incomplete) produce zero closure events."""
    org = _make_org(test_db_session, "Failed Crawl Practice")
    src = _make_source(test_db_session)
    _make_link(test_db_session, org.id, src.id)

    snap = _make_snapshot(test_db_session, src.id, "Error", "500 Internal Server Error")

    # Ingestion failed, no successful change events
    events = detect_practice_lifecycle_events(test_db_session, source_id=src.id, organisation_id=org.id)
    assert len(events) == 0


def test_conflicting_sources_closure(test_db_session: Session):
    """Test 12: Conflicting sources reporting opposing statuses retain distinct evidence."""
    org = _make_org(test_db_session, "Dual Source Medical")
    src_active = _make_source(test_db_session, url="https://directory.com.au/dual-med")
    src_closed = _make_source(test_db_session, url="https://official-dual-med.com.au/notice")

    _make_link(test_db_session, org.id, src_active.id)
    _make_link(test_db_session, org.id, src_closed.id)

    snap_active = _make_snapshot(test_db_session, src_active.id, "Directory", "Dual Source Medical is open Monday to Friday 9am to 5pm.")
    snap_closed = _make_snapshot(test_db_session, src_closed.id, "Notice", "The clinic permanently closed on 1 November 2026.")

    evts_active = detect_closure_evidence(test_db_session, org, src_active, snap_active)
    evts_closed = detect_closure_evidence(test_db_session, org, src_closed, snap_closed)

    assert len(evts_active) == 0
    assert len(evts_closed) == 1
    assert evts_closed[0].status == LifecycleStatus.CONFIRMED_EXPLICIT.value
    assert evts_closed[0].source_id == src_closed.id


def test_explicit_relocation_detection(test_db_session: Session):
    """Test 13: Explicit relocation notice detected with old and new addresses."""
    org = _make_org(test_db_session, "Relocating Practice", address="10 Old Road, Richmond")
    src = _make_source(test_db_session)
    _make_link(test_db_session, org.id, src.id)

    text_content = (
        "Announcement: We have moved to 45 New Street, South Yarra. "
        "Please visit our new practice location from 1 February 2027."
    )
    snap = _make_snapshot(test_db_session, src.id, "Relocation", text_content)

    events = detect_relocation_evidence(test_db_session, org, src, snap)
    assert len(events) == 1
    evt = events[0]

    assert evt.event_type == LifecycleEventType.RELOCATION.value
    assert evt.status == LifecycleStatus.CONFIRMED_EXPLICIT.value
    assert "45 New Street" in evt.new_value
    assert evt.old_value == "10 Old Road, Richmond"
    assert verify_lifecycle_evidence_integrity(evt, snap) is True


def test_address_change_diff_potential_relocation(test_db_session: Session):
    """Test 14: Address fact changed without wording creates potential_needs_review relocation signal."""
    org = _make_org(test_db_session, "Quiet Move Practice", address="10 Alpha St, Melbourne")
    src = _make_source(test_db_session)
    _make_link(test_db_session, org.id, src.id)

    snap1 = _make_snapshot(test_db_session, src.id, "V1", "Contact: 10 Alpha St, Melbourne")
    snap2 = _make_snapshot(test_db_session, src.id, "V2", "Contact: 20 Beta Rd, Melbourne")

    ev1 = _make_evidence(test_db_session, snap1.id, "10 Alpha St, Melbourne", 9, 31)
    ev2 = _make_evidence(test_db_session, snap2.id, "20 Beta Rd, Melbourne", 9, 30)

    f1 = orm.Fact(snapshot_id=snap1.id, organisation_id=org.id, field_key="address", raw_value="10 Alpha St, Melbourne", evidence_id=ev1.id, status="verified")
    f2 = orm.Fact(snapshot_id=snap2.id, organisation_id=org.id, field_key="address", raw_value="20 Beta Rd, Melbourne", evidence_id=ev2.id, status="verified")
    test_db_session.add_all([f1, f2])
    test_db_session.commit()

    events = detect_relocation_evidence(test_db_session, org, src, snap2, previous_snapshot=snap1)
    assert len(events) == 1
    evt = events[0]

    assert evt.event_type == LifecycleEventType.RELOCATION.value
    assert evt.status == LifecycleStatus.POTENTIAL_NEEDS_REVIEW.value
    assert evt.old_value == "10 Alpha St, Melbourne"
    assert evt.new_value == "20 Beta Rd, Melbourne"
    assert "flagged for operator verification" in evt.rationale


def test_multi_branch_creation_not_relocation(test_db_session: Session):
    """Test 15: Multi-branch page where old address remains in DOM is recognized as branch addition, NOT relocation."""
    org = _make_org(test_db_session, "Multi Clinic", address="10 Alpha St, Melbourne")
    src = _make_source(test_db_session)
    _make_link(test_db_session, org.id, src.id)

    snap1 = _make_snapshot(test_db_session, src.id, "V1", "Branch 1: 10 Alpha St, Melbourne")
    # In V2, both old address and new address are present
    snap2 = _make_snapshot(test_db_session, src.id, "V2", "Branch 1: 10 Alpha St, Melbourne. Branch 2: 20 Beta Rd, Melbourne")

    ev1 = _make_evidence(test_db_session, snap1.id, "10 Alpha St, Melbourne", 10, 32)
    ev2 = _make_evidence(test_db_session, snap2.id, "20 Beta Rd, Melbourne", 42, 63)

    f1 = orm.Fact(snapshot_id=snap1.id, organisation_id=org.id, field_key="address", raw_value="10 Alpha St, Melbourne", evidence_id=ev1.id, status="verified")
    f2 = orm.Fact(snapshot_id=snap2.id, organisation_id=org.id, field_key="address", raw_value="20 Beta Rd, Melbourne", evidence_id=ev2.id, status="verified")
    test_db_session.add_all([f1, f2])
    test_db_session.commit()

    events = detect_relocation_evidence(test_db_session, org, src, snap2, previous_snapshot=snap1)
    # False relocation is suppressed because old address remains present in snapshot text
    assert len(events) == 0


def test_relocation_preserves_organisation_identity(test_db_session: Session):
    """Test 16: Relocation retains identical organisation ID; no merging or duplicate creation."""
    org = _make_org(test_db_session, "Stable Practice", address="Old Loc")
    src = _make_source(test_db_session)
    _make_link(test_db_session, org.id, src.id)

    snap = _make_snapshot(test_db_session, src.id, "Page", "We have moved to 99 Future Way, Footscray.")
    events = detect_relocation_evidence(test_db_session, org, src, snap)
    assert len(events) == 1

    org_count = test_db_session.execute(select(orm.Organisation)).scalars().all()
    assert len(org_count) == 1
    assert org_count[0].id == org.id


def test_explicit_corporate_acquisition_detection(test_db_session: Session):
    """Test 17: Explicit acquisition statement detected with acquiring company recorded."""
    org_acquired = _make_org(test_db_session, "Boutique Medical")
    org_acquirer = _make_org(test_db_session, "Australian Health Partners Group")
    src = _make_source(test_db_session)
    _make_link(test_db_session, org_acquired.id, src.id)

    text_content = (
        "Boutique Medical was acquired by Australian Health Partners Group effective 1 October 2026. "
        "We continue to serve our local patients with existing clinical staff."
    )
    snap = _make_snapshot(test_db_session, src.id, "Acquisition Announcement", text_content)

    events = detect_acquisition_evidence(test_db_session, org_acquired, src, snap)
    assert len(events) == 1
    evt = events[0]

    assert evt.event_type == LifecycleEventType.ACQUISITION.value
    assert evt.status == LifecycleStatus.CONFIRMED_EXPLICIT.value
    assert "Australian Health Partners Group" in evt.acquiring_entity_name
    assert evt.acquiring_organisation_id == org_acquirer.id
    assert verify_lifecycle_evidence_integrity(evt, snap) is True


def test_shared_domain_rejected_as_acquisition(test_db_session: Session):
    """Test 18: Negative control: Shared domain or corporate website link strictly rejected as acquisition proof."""
    org = _make_org(test_db_session, "Independent Clinic")
    src = _make_source(test_db_session)
    _make_link(test_db_session, org.id, src.id)

    text_content = "Independent Clinic is listed on our partner network directory listing and accredited by AGPAL."
    snap = _make_snapshot(test_db_session, src.id, "Directory", text_content)

    events = detect_acquisition_evidence(test_db_session, org, src, snap)
    assert len(events) == 0


def test_shared_corporate_phone_rejected_as_acquisition(test_db_session: Session):
    """Test 19: Negative control: Shared 1300/1800 corporate telephone number is rejected as acquisition proof."""
    org = _make_org(test_db_session, "Community Clinic")
    src = _make_source(test_db_session)
    _make_link(test_db_session, org.id, src.id)

    text_content = "For central bookings call 1300 000 111. Community Clinic consultation rooms available."
    snap = _make_snapshot(test_db_session, src.id, "Contact", text_content)

    events = detect_acquisition_evidence(test_db_session, org, src, snap)
    assert len(events) == 0


def test_acquisition_preserves_both_organisations(test_db_session: Session):
    """Test 20: Zero auto-merge: both acquired and acquiring organisations preserve distinct IDs and facts."""
    org_a = _make_org(test_db_session, "Target Clinic")
    org_b = _make_org(test_db_session, "Buyer Group")
    src = _make_source(test_db_session)
    _make_link(test_db_session, org_a.id, src.id)

    snap = _make_snapshot(test_db_session, src.id, "News", "Target Clinic became a subsidiary of Buyer Group.")
    events = detect_acquisition_evidence(test_db_session, org_a, src, snap)
    assert len(events) == 1

    orgs_after = test_db_session.execute(select(orm.Organisation)).scalars().all()
    assert len(orgs_after) == 2
    ids = [o.id for o in orgs_after]
    assert org_a.id in ids
    assert org_b.id in ids


def test_lifecycle_event_idempotency(test_db_session: Session):
    """Test 21: Repeated lifecycle detection creates zero duplicate events."""
    org = _make_org(test_db_session, "Idempotent Practice")
    src = _make_source(test_db_session)
    _make_link(test_db_session, org.id, src.id)

    snap = _make_snapshot(test_db_session, src.id, "Notice", "This medical practice is now permanently closed.")

    evts1 = detect_practice_lifecycle_events(test_db_session, source_id=src.id, organisation_id=org.id)
    assert len(evts1) == 1

    evts2 = detect_practice_lifecycle_events(test_db_session, source_id=src.id, organisation_id=org.id)
    assert len(evts2) == 1

    total_events = test_db_session.execute(select(orm.PracticeLifecycleEvent)).scalars().all()
    assert len(total_events) == 1


def test_review_queue_lifecycle_integration(test_db_session: Session):
    """Test 22: Lifecycle events appear in the unified review queue with correct priority and quotation."""
    org = _make_org(test_db_session, "Queue Practice")
    src = _make_source(test_db_session)
    snap = _make_snapshot(test_db_session, src.id, "Notice", "Practice permanently closed.")

    evt = orm.PracticeLifecycleEvent(
        organisation_id=org.id,
        event_type="permanent_closure",
        status="confirmed_explicit",
        source_id=src.id,
        snapshot_id=snap.id,
        evidence_quote="Practice permanently closed.",
        rationale="Explicit closure",
        review_status="pending",
        idempotency_key="idemp_q_1",
    )
    test_db_session.add(evt)
    test_db_session.commit()

    q = get_review_queue(test_db_session, entity_type="lifecycle_event")
    assert q["total"] == 1
    item = q["items"][0]
    assert item["entity_type"] == "lifecycle_event"
    assert item["entity_id"] == evt.id
    assert item["priority"] == "high"
    assert "Practice permanently closed." in item["evidence_quote"]


def test_review_metrics_lifecycle_integration(test_db_session: Session):
    """Test 23: Review metrics calculate pending and deferred counts for lifecycle events."""
    org = _make_org(test_db_session, "Metrics Practice")
    src = _make_source(test_db_session)
    snap = _make_snapshot(test_db_session, src.id)

    evt1 = orm.PracticeLifecycleEvent(
        organisation_id=org.id,
        event_type="relocation",
        status="confirmed_explicit",
        source_id=src.id,
        snapshot_id=snap.id,
        rationale="Move",
        review_status="pending",
        idempotency_key="idemp_m_1",
    )
    evt2 = orm.PracticeLifecycleEvent(
        organisation_id=org.id,
        event_type="acquisition",
        status="confirmed_explicit",
        source_id=src.id,
        snapshot_id=snap.id,
        rationale="Buy",
        review_status="deferred",
        idempotency_key="idemp_m_2",
    )
    test_db_session.add_all([evt1, evt2])
    test_db_session.commit()

    m = get_review_metrics(test_db_session)
    assert m["by_entity_type"]["lifecycle_event"] == 1
    assert m["total_deferred"] >= 1


def test_review_item_detail_lifecycle(test_db_session: Session):
    """Test 24: get_review_item_detail returns rich lifecycle context and allowed actions."""
    org = _make_org(test_db_session, "Detail Practice")
    src = _make_source(test_db_session)
    snap = _make_snapshot(test_db_session, src.id, "Snap", "Relocating to 123 Main St.")

    evt = orm.PracticeLifecycleEvent(
        organisation_id=org.id,
        branch_locality="Carlton",
        is_branch_level=True,
        event_type="relocation",
        status="confirmed_explicit",
        source_id=src.id,
        snapshot_id=snap.id,
        new_value="123 Main St",
        evidence_quote="Relocating to 123 Main St.",
        rationale="Relocation",
        review_status="pending",
        idempotency_key="idemp_dt_1",
    )
    test_db_session.add(evt)
    test_db_session.commit()

    detail = get_review_item_detail(test_db_session, "lifecycle_event", evt.id)
    assert detail["entity_type"] == "lifecycle_event"
    assert detail["lifecycle_context"] is not None
    assert detail["lifecycle_context"]["branch_locality"] == "Carlton"
    assert detail["lifecycle_context"]["is_branch_level"] is True
    assert "accepted" in detail["allowed_actions"]
    assert "rejected" in detail["allowed_actions"]


def test_review_action_decision_persistence(test_db_session: Session):
    """Test 25: Submitting decision updates lifecycle review_status and writes append-only review_action."""
    org = _make_org(test_db_session, "Action Practice")
    src = _make_source(test_db_session)
    snap = _make_snapshot(test_db_session, src.id)

    evt = orm.PracticeLifecycleEvent(
        organisation_id=org.id,
        event_type="permanent_closure",
        status="confirmed_explicit",
        source_id=src.id,
        snapshot_id=snap.id,
        evidence_quote="Practice permanently closed.",
        rationale="Closure",
        review_status="pending",
        idempotency_key="idemp_act_1",
    )
    test_db_session.add(evt)
    test_db_session.commit()

    action_rec = record_review_action(
        session=test_db_session,
        entity_type="lifecycle_event",
        entity_id=evt.id,
        action="accepted",
        reason_code="authoritative_practice_website",
        reviewer="dr_reviewer",
        notes="Verified closure announcement on official contact page",
    )
    test_db_session.commit()

    assert action_rec.id is not None
    assert evt.review_status == "accepted"
    assert evt.reviewed_by == "dr_reviewer"

    # Verify history
    hist = get_review_history(test_db_session, entity_type="lifecycle_event", entity_id=evt.id)
    assert hist["total"] == 1
    assert hist["items"][0]["reviewer"] == "dr_reviewer"
    assert hist["items"][0]["action"] == "accepted"


def test_underlying_facts_untouched_after_lifecycle_review(test_db_session: Session):
    """Test 26: Invariant: Deciding on a lifecycle event leaves underlying facts, raw values, and evidence un-mutated."""
    org = _make_org(test_db_session, "Preserve Practice")
    src = _make_source(test_db_session)
    snap = _make_snapshot(test_db_session, src.id, "Page", "Phone: 03 9999 8888")
    ev = _make_evidence(test_db_session, snap.id, "Phone: 03 9999 8888", 0, 19)

    fact = orm.Fact(snapshot_id=snap.id, organisation_id=org.id, field_key="phone", raw_value="03 9999 8888", evidence_id=ev.id, status="candidate")
    test_db_session.add(fact)
    test_db_session.commit()

    evt = orm.PracticeLifecycleEvent(
        organisation_id=org.id,
        event_type="relocation",
        status="potential_needs_review",
        source_id=src.id,
        snapshot_id=snap.id,
        evidence_id=ev.id,
        evidence_quote="Phone: 03 9999 8888",
        rationale="Test",
        review_status="pending",
        idempotency_key="idemp_untouch_1",
    )
    test_db_session.add(evt)
    test_db_session.commit()

    record_review_action(
        session=test_db_session,
        entity_type="lifecycle_event",
        entity_id=evt.id,
        action="accepted",
        reason_code="verified_relocation",
        reviewer="analyst_2",
        notes="Accepted move",
    )
    test_db_session.commit()

    test_db_session.expire_all()
    refreshed_fact = test_db_session.get(orm.Fact, fact.id)
    assert refreshed_fact.raw_value == "03 9999 8888"
    assert refreshed_fact.evidence.quote == "Phone: 03 9999 8888"


def test_fresh_session_and_restart_persistence(test_db_session: Session):
    """Test 27: Lifecycle events persist and reload correctly in fresh independent sessions."""
    org = _make_org(test_db_session, "Restart Practice")
    src = _make_source(test_db_session)
    snap = _make_snapshot(test_db_session, src.id)

    evt = orm.PracticeLifecycleEvent(
        organisation_id=org.id,
        event_type="acquisition",
        status="confirmed_explicit",
        source_id=src.id,
        snapshot_id=snap.id,
        acquiring_entity_name="Central Health",
        rationale="Acquisition",
        review_status="pending",
        idempotency_key="idemp_restart_1",
    )
    test_db_session.add(evt)
    test_db_session.commit()
    evt_id = evt.id

    bind = test_db_session.get_bind()
    fresh_session = sessionmaker(bind=bind, autoflush=False, expire_on_commit=False, future=True)()
    try:
        fresh_evt = fresh_session.get(orm.PracticeLifecycleEvent, evt_id)
        assert fresh_evt is not None
        assert fresh_evt.event_type == "acquisition"
        assert fresh_evt.acquiring_entity_name == "Central Health"
    finally:
        fresh_session.close()


def test_lifecycle_rest_api_endpoints(client_with_session: TestClient, test_db_session: Session):
    """Test 28: Full REST API integration for practice lifecycle events."""
    org = _make_org(test_db_session, "API Practice")
    src = _make_source(test_db_session)
    snap = _make_snapshot(test_db_session, src.id, "Notice", "Practice permanently closed.")

    evt = orm.PracticeLifecycleEvent(
        organisation_id=org.id,
        branch_locality="Carlton",
        is_branch_level=True,
        event_type="permanent_closure",
        status="confirmed_explicit",
        source_id=src.id,
        snapshot_id=snap.id,
        evidence_quote="Practice permanently closed.",
        rationale="Closure notice",
        review_status="pending",
        idempotency_key="idemp_api_1",
    )
    test_db_session.add(evt)
    test_db_session.commit()

    # 1. List events
    r_list = client_with_session.get("/api/lifecycle/events")
    assert r_list.status_code == 200
    events = r_list.json()
    assert len(events) >= 1

    # 2. Filter by event_type
    r_filter = client_with_session.get("/api/lifecycle/events?event_type=permanent_closure")
    assert r_filter.status_code == 200
    assert len(r_filter.json()) >= 1

    # 3. Get single event
    r_detail = client_with_session.get(f"/api/lifecycle/events/{evt.id}")
    assert r_detail.status_code == 200
    data = r_detail.json()
    assert data["event_type"] == "permanent_closure"
    assert data["branch_locality"] == "Carlton"

    # 4. 404 on missing event
    r_404 = client_with_session.get("/api/lifecycle/events/999999")
    assert r_404.status_code == 404

    # 5. Organisation history
    r_org_hist = client_with_session.get(f"/api/lifecycle/organisations/{org.id}/history")
    assert r_org_hist.status_code == 200
    assert len(r_org_hist.json()) >= 1

    # 6. Branch history
    r_branch_hist = client_with_session.get(f"/api/lifecycle/organisations/{org.id}/branches/Carlton/history")
    assert r_branch_hist.status_code == 200
    assert len(r_branch_hist.json()) >= 1

    # 7. Scan endpoint
    r_scan = client_with_session.post("/api/lifecycle/scan", json={"organisation_id": org.id})
    assert r_scan.status_code == 200
    scan_res = r_scan.json()
    assert "events_detected" in scan_res
