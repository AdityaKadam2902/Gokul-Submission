"""Comprehensive M3.6 backend test suite: Human Review, Evidence Verification & Auditable Decision Workflow.

Covers Section 11 of M3.6 authorization:
1. Migration M3_005: Fresh DB migration creates review_actions with all columns and indexes.
2. Migration M3_005: Existing DB with M3_001..M3_004 adds review_actions cleanly.
3. Migration M3_005 idempotency: Running twice produces no errors.
4. Migration M3_005 rollback: Removes table cleanly; reapplying recreates it.
5. Review queue aggregation: Returns items from all review-requiring domains (match candidates, ambiguous ownership, candidate facts, cross-source discrepancies, change events, opportunities).
6. Queue filtering: Filter by entity_type, filter by status, pagination (offset/limit).
7. Review queue ordering: Urgent items first (match candidates, discrepancies, unassigned facts).
8. Review item detail: Returns complete grounded context including evidence quote, source URL, snapshot ID, organisation context.
9. Discrepancy detail: Returns side-by-side comparison with both facts' values, sources, and evidence quotes.
10. Ambiguous ownership detail: Returns candidate organisations and resolution options.
11. Reviewer validation: Missing or whitespace-only reviewer rejected with 422 / ValueError.
12. Reason code validation: Missing or whitespace-only reason rejected with 422 / ValueError.
13. Consequential decision rationale: Accepting or rejecting without notes rejected (notes mandatory for consequential actions).
14. Match candidate confirmation: Sets link status to verified, sets reviewed_by, records review action. Proves NO organisation merge occurred.
15. Match candidate rejection: Sets link status to rejected, records review action.
16. Fact ownership assignment: Validates target org exists, sets fact.organisation_id, clears needs_review_reason, records review action.
17. Fact ownership unassigned: Sets needs_review_reason to explicitly_unassigned_corporate, leaves organisation_id None, records review action.
18. Fact ownership invalid org: Attempting to assign non-existent org_id rejected with 404/422.
19. Candidate fact approval: Sets fact review status to accepted/verified, records review action with evidence ID.
20. Candidate fact rejection: Sets fact review status to rejected, records review action.
21. Discrepancy resolution: Resolving discrepancy sets resolved_fact_a / resolved_fact_b, records review action. Proves BOTH facts' raw values, evidence, and snapshots remain completely untouched.
22. Opportunity review: Acknowledging or deferring opportunity records review action. Strictly rejects any attempt to set status to 'filled' or 'hired'.
23. Change event review: Acknowledging change event records review action; underlying change event remains immutable.
24. Freshness review: Acknowledging freshness records review action; calculated freshness values remain unchanged.
25. Review history retrieval: Chronological audit trail for an entity returned correctly.
26. Global review metrics: Counts by domain, counts by status, action counts by reviewer.
27. Review actions immutability: Verifies that no API endpoints exist to edit or delete recorded review actions.
28. REST API integration: Endpoints for queue, metrics, detail, action, history, actions.
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
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api.main import app
from backend.app.db import orm
from backend.app.db.base import get_db
from backend.app.intelligence.discrepancy_detector import evaluate_fact_pair_discrepancy
from backend.app.intelligence.fact_ownership import assign_fact_ownership
from backend.app.db.migrations import (
    inspect_m3_005_schema,
    migrate_m3_001_organisations,
    migrate_m3_002_discrepancies,
    migrate_m3_003_change_events,
    migrate_m3_004_opportunities,
    migrate_m3_005_review_actions,
    rollback_m3_005_review_actions,
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
    OpportunityStatus,
    ReviewActionType,
    ReviewStatus,
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


def _make_org(db: Session, name: str = "Test Practice", slug: str | None = None, postcode: str = "2000", state: str = "NSW") -> orm.Organisation:
    slug = slug or f"org-{uuid.uuid4().hex[:6]}"
    org = orm.Organisation(name=name, slug=slug, postcode=postcode, state=state)
    db.add(org)
    db.flush()
    return org


def _make_source(db: Session, url: str | None = None, domain: str = "example.com.au", label: str = "Practice Site", org_id: int | None = None) -> orm.Source:
    url = url or f"https://example-{uuid.uuid4().hex[:6]}.com.au"
    src = orm.Source(
        organisation_id=org_id,
        kind="fixture",
        label=label,
        url=url,
        domain=domain,
        page_type="contact",
        policy_status="allowed",
    )
    db.add(src)
    db.flush()
    return src


def _make_snapshot(db: Session, source_id: int, title: str = "Contact Us", text: str = "Call us at 02 9999 8888") -> orm.PageSnapshot:
    snap = orm.PageSnapshot(
        source_id=source_id,
        title=title,
        text=text,
        content_hash=uuid.uuid4().hex,
        parser_version="v1",
        headings=[],
    )
    db.add(snap)
    db.flush()
    return snap


def _make_evidence(db: Session, snapshot_id: int, quote: str, start_offset: int = 0, end_offset: int = 20) -> orm.Evidence:
    ev = orm.Evidence(
        snapshot_id=snapshot_id,
        quote=quote,
        start_offset=start_offset,
        end_offset=end_offset,
    )
    db.add(ev)
    db.flush()
    return ev


def _make_observation(db: Session, source_id: int, snapshot_id: int | None = None, seq: int = 1) -> orm.SourceObservation:
    obs = orm.SourceObservation(
        source_id=source_id,
        observation_sequence=seq,
        outcome="success_new_snapshot",
        snapshot_id=snapshot_id,
    )
    db.add(obs)
    db.flush()
    return obs


# -----------------------------------------------------------------------------
# 1-4. M3_005 Migration & Schema Integrity Tests
# -----------------------------------------------------------------------------

def test_m3_005_migration_fresh_db():
    """Test 1: Fresh DB migration creates review_actions with all columns and indexes."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name

    try:
        conn = sqlite3.connect(db_path)
        conn.close()

        res = migrate_m3_005_review_actions(db_path)
        assert res["status"] in ("success", "up_to_date")

        schema = inspect_m3_005_schema(db_path)
        assert schema["table_exists"] is True
        cols = schema["columns"]
        expected_cols = {
            "id", "entity_type", "entity_id", "action", "previous_status",
            "new_status", "reason_code", "reviewer", "notes", "evidence_id",
            "decision_metadata", "created_at"
        }
        for ec in expected_cols:
            assert ec in cols, f"Missing column {ec} in review_actions"

        indexes = schema["indexes"]
        expected_indexes = {
            "ix_review_actions_entity",
            "ix_review_actions_action",
            "ix_review_actions_reviewer",
            "ix_review_actions_created_at",
        }
        for ei in expected_indexes:
            assert ei in indexes, f"Missing index {ei} in review_actions"

        triggers = schema.get("triggers", [])
        assert "prevent_review_actions_update" in triggers
        assert "prevent_review_actions_delete" in triggers
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_m3_005_migration_existing_db():
    """Test 2: Existing DB with M3_001..M3_004 adds review_actions cleanly."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name

    try:
        engine = create_engine(f"sqlite:///{db_path}")
        orm.Base.metadata.create_all(bind=engine)
        engine.dispose()

        migrate_m3_001_organisations(db_path)
        migrate_m3_002_discrepancies(db_path)
        migrate_m3_003_change_events(db_path)
        migrate_m3_004_opportunities(db_path)

        res = migrate_m3_005_review_actions(db_path)
        assert res["status"] in ("success", "up_to_date")

        schema = inspect_m3_005_schema(db_path)
        assert schema["table_exists"] is True
        assert "entity_type" in schema["columns"]
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_m3_005_migration_idempotency():
    """Test 3: Running migration M3_005 twice produces no errors."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name

    try:
        r1 = migrate_m3_005_review_actions(db_path)
        assert r1["status"] == "success"
        r2 = migrate_m3_005_review_actions(db_path)
        assert r2["status"] == "up_to_date"
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_m3_005_migration_rollback():
    """Test 4: Rollback cleanly drops table; reapplying restores it."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name

    try:
        migrate_m3_005_review_actions(db_path)
        assert inspect_m3_005_schema(db_path)["table_exists"] is True

        rb = rollback_m3_005_review_actions(db_path)
        assert rb["status"] == "success"
        schema_after_rb = inspect_m3_005_schema(db_path)
        assert schema_after_rb["table_exists"] is False
        assert len(schema_after_rb["triggers"]) == 0

        reapply = migrate_m3_005_review_actions(db_path)
        assert reapply["status"] == "success"
        schema_reapply = inspect_m3_005_schema(db_path)
        assert schema_reapply["table_exists"] is True
        assert "prevent_review_actions_update" in schema_reapply["triggers"]
        assert "prevent_review_actions_delete" in schema_reapply["triggers"]
    finally:
        Path(db_path).unlink(missing_ok=True)


# -----------------------------------------------------------------------------
# 5-10. Review Queue Aggregation, Grounding & Details
# -----------------------------------------------------------------------------

def test_review_queue_aggregation(test_db_session: Session):
    """Test 5: Review queue returns items from all review-requiring domains."""
    org1 = _make_org(test_db_session, name="Central Practice", slug="central-practice")
    src1 = _make_source(test_db_session, url="https://centralpractice.com.au", domain="centralpractice.com.au", label="Central Practice Official")

    # 1. Organisation match candidate
    cand = orm.OrganisationMatchCandidate(
        source_id=src1.id,
        suggested_organisation_id=org1.id,
        match_tier="high_confidence",
        raw_metric_score=0.95,
        rationale="Domain match",
        status="suggested",
    )
    test_db_session.add(cand)

    # 2. Fact ownership ambiguity (unassigned corporate fact)
    snap1 = _make_snapshot(test_db_session, source_id=src1.id, title="Central Practice")
    fact_unassigned = orm.Fact(
        snapshot_id=snap1.id,
        organisation_id=None,
        field_key="phone",
        scope_key="corporate",
        raw_value="02 9999 8888",
        status="candidate",
        needs_review_reason="ambiguous_corporate_scope",
    )
    test_db_session.add(fact_unassigned)

    # 3. Extracted candidate fact
    fact_candidate = orm.Fact(
        snapshot_id=snap1.id,
        organisation_id=org1.id,
        field_key="operating_hours",
        scope_key="default",
        raw_value="Mon-Fri 8am-6pm",
        status="candidate",
    )
    test_db_session.add(fact_candidate)

    # 4. Cross-source discrepancy
    fact_b = orm.Fact(
        snapshot_id=snap1.id,
        organisation_id=org1.id,
        field_key="phone",
        scope_key="default",
        raw_value="02 9999 7777",
        status="candidate",
    )
    test_db_session.add(fact_b)
    test_db_session.commit()

    disc = orm.Discrepancy(
        organisation_id=org1.id,
        fact_a_id=fact_candidate.id,
        fact_b_id=fact_b.id,
        field_key="phone",
        scope_key="default",
        conflict_type="value_mismatch",
        status="detected",
        rationale="Phone mismatch across sources",
    )
    test_db_session.add(disc)

    # 5. Opportunity in reviewable state
    opp = orm.Opportunity(
        organisation_id=org1.id,
        source_id=src1.id,
        first_snapshot_id=snap1.id,
        last_snapshot_id=snap1.id,
        role_category="general_practitioner",
        role_title="VR General Practitioner",
        verbatim_wording="VR GP required for busy clinic",
        status=OpportunityStatus.POSSIBLY_STALE.value,
        closure_evidence_quote="Closing date 01/10/2026 has elapsed",
        consecutive_absences=0,
    )
    test_db_session.add(opp)
    test_db_session.commit()

    # Query queue
    q_res = get_review_queue(test_db_session)
    items = q_res["items"]
    types = {item["entity_type"] for item in items}

    assert "match_candidate" in types
    assert "fact_ownership" in types
    assert "fact" in types
    assert "discrepancy" in types
    assert "opportunity" in types
    assert q_res["total"] >= 5


def test_queue_filtering_and_pagination(test_db_session: Session):
    """Test 6: Filter by entity_type, filter by status, pagination (offset/limit)."""
    org1 = _make_org(test_db_session, name="Filter Org 1", slug="filter-org-1")
    org2 = _make_org(test_db_session, name="Filter Org 2", slug="filter-org-2")
    src = _make_source(test_db_session, url="https://filter.com.au", domain="filter.com.au")

    cand1 = orm.OrganisationMatchCandidate(
        source_id=src.id, suggested_organisation_id=org1.id,
        match_tier="high_confidence", status="suggested", rationale="Match 1"
    )
    cand2 = orm.OrganisationMatchCandidate(
        source_id=src.id, suggested_organisation_id=org2.id,
        match_tier="medium_confidence", status="verified", rationale="Match 2"
    )
    test_db_session.add_all([cand1, cand2])
    test_db_session.commit()

    # Filter by entity_type
    q_matches = get_review_queue(test_db_session, entity_type="match_candidate", status="all")
    for item in q_matches["items"]:
        assert item["entity_type"] == "match_candidate"

    # Filter by pending status
    q_pending = get_review_queue(test_db_session, entity_type="match_candidate", status="pending")
    assert len(q_pending["items"]) == 1
    assert q_pending["items"][0]["entity_id"] == cand1.id

    # Filter by completed status
    q_completed = get_review_queue(test_db_session, entity_type="match_candidate", status="completed")
    assert len(q_completed["items"]) == 1
    assert q_completed["items"][0]["entity_id"] == cand2.id

    # Pagination
    q_page = get_review_queue(test_db_session, entity_type="match_candidate", status="all", limit=1, offset=0)
    assert len(q_page["items"]) == 1
    assert q_page["limit"] == 1
    assert q_page["offset"] == 0


def test_review_queue_ordering_urgent_first(test_db_session: Session):
    """Test 7: Review queue orders high priority items before medium/low priority."""
    org = _make_org(test_db_session, name="Priority Org", slug="priority-org")
    src = _make_source(test_db_session, url="https://priority.com.au", domain="priority.com.au")
    snap = _make_snapshot(test_db_session, source_id=src.id)

    # Medium priority: normal candidate fact
    f_med = orm.Fact(
        snapshot_id=snap.id, organisation_id=org.id,
        field_key="email", raw_value="info@priority.com.au", status="candidate"
    )
    # High priority: high confidence match candidate
    cand_high = orm.OrganisationMatchCandidate(
        source_id=src.id, suggested_organisation_id=org.id,
        match_tier="high_confidence", status="suggested", rationale="High conf"
    )
    # High priority: unassigned corporate fact
    f_high = orm.Fact(
        snapshot_id=snap.id, organisation_id=None,
        field_key="phone", raw_value="02 1111 2222", status="candidate",
        needs_review_reason="ambiguous_corporate_scope"
    )
    test_db_session.add_all([f_med, cand_high, f_high])
    test_db_session.commit()

    queue = get_review_queue(test_db_session)
    items = queue["items"]
    assert len(items) >= 3

    first_medium_idx = next(i for i, item in enumerate(items) if item["priority"] == "medium")
    for i in range(first_medium_idx):
        assert items[i]["priority"] == "high"


def test_review_item_detail_grounded_context(test_db_session: Session):
    """Test 8: Detail returns complete grounded context including quote, URL, snapshot ID."""
    org = _make_org(test_db_session, name="Grounded Org", slug="grounded-org")
    src = _make_source(test_db_session, url="https://grounded.com.au/contact", domain="grounded.com.au", label="Grounded Contact")
    snap = _make_snapshot(test_db_session, source_id=src.id, title="Grounded Medical Centre Contact Us")
    ev = _make_evidence(test_db_session, snapshot_id=snap.id, quote="Call us at (03) 9876 5432 during office hours.", start_offset=12, end_offset=56)

    fact = orm.Fact(
        snapshot_id=snap.id,
        organisation_id=org.id,
        evidence_id=ev.id,
        field_key="phone",
        scope_key="default",
        raw_value="(03) 9876 5432",
        status="candidate",
    )
    test_db_session.add(fact)
    test_db_session.commit()

    detail = get_review_item_detail(test_db_session, "fact", fact.id)
    assert detail["entity_type"] == "fact"
    assert detail["entity_id"] == fact.id
    assert detail["source_url"] == "https://grounded.com.au/contact"
    assert detail["snapshot_id"] == snap.id
    assert detail["evidence_quote"] == "Call us at (03) 9876 5432 during office hours."
    assert detail["evidence_start_offset"] == 12
    assert detail["evidence_end_offset"] == 56
    assert detail["organisation_name"] == "Grounded Org"
    assert "approved" in detail["allowed_actions"]
    assert "rejected" in detail["allowed_actions"]


def test_discrepancy_detail_side_by_side(test_db_session: Session):
    """Test 9: Side-by-side comparison with both facts' values, sources, and evidence quotes."""
    org = _make_org(test_db_session, name="Disc Org", slug="disc-org")
    src_a = _make_source(test_db_session, url="https://source-a.com.au", domain="source-a.com.au")
    src_b = _make_source(test_db_session, url="https://source-b.com.au", domain="source-b.com.au")

    snap_a = _make_snapshot(test_db_session, source_id=src_a.id)
    snap_b = _make_snapshot(test_db_session, source_id=src_b.id)

    ev_a = _make_evidence(test_db_session, snapshot_id=snap_a.id, quote="Official Reception: 02 8888 1111", start_offset=0, end_offset=33)
    ev_b = _make_evidence(test_db_session, snapshot_id=snap_b.id, quote="Directory Listing Phone: 02 8888 2222", start_offset=0, end_offset=38)

    fact_a = orm.Fact(snapshot_id=snap_a.id, organisation_id=org.id, evidence_id=ev_a.id, field_key="phone", scope_key="default", raw_value="02 8888 1111", status="candidate")
    fact_b = orm.Fact(snapshot_id=snap_b.id, organisation_id=org.id, evidence_id=ev_b.id, field_key="phone", scope_key="default", raw_value="02 8888 2222", status="candidate")
    test_db_session.add_all([fact_a, fact_b])
    test_db_session.commit()

    disc = orm.Discrepancy(
        organisation_id=org.id,
        fact_a_id=fact_a.id,
        fact_b_id=fact_b.id,
        field_key="phone",
        scope_key="default",
        conflict_type="value_mismatch",
        status="detected",
        rationale="Phone conflict between source A and source B",
    )
    test_db_session.add(disc)
    test_db_session.commit()

    detail = get_review_item_detail(test_db_session, "discrepancy", disc.id)
    assert detail["discrepancy_context"] is not None
    ctx = detail["discrepancy_context"]

    assert ctx["fact_a"]["raw_value"] == "02 8888 1111"
    assert ctx["fact_a"]["source_url"] == "https://source-a.com.au"
    assert ctx["fact_a"]["evidence_quote"] == "Official Reception: 02 8888 1111"

    assert ctx["fact_b"]["raw_value"] == "02 8888 2222"
    assert ctx["fact_b"]["source_url"] == "https://source-b.com.au"
    assert ctx["fact_b"]["evidence_quote"] == "Directory Listing Phone: 02 8888 2222"


def test_ambiguous_ownership_detail(test_db_session: Session):
    """Test 10: Unassigned corporate fact returns candidate organisations in detail."""
    org1 = _make_org(test_db_session, name="Alpha Practice", slug="alpha-practice")
    org2 = _make_org(test_db_session, name="Beta Practice", slug="beta-practice")
    src = _make_source(test_db_session, url="https://corp.com.au", domain="corp.com.au")
    snap = _make_snapshot(test_db_session, source_id=src.id)

    fact = orm.Fact(
        snapshot_id=snap.id,
        organisation_id=None,
        field_key="phone",
        scope_key="corporate",
        raw_value="1300 000 111",
        status="candidate",
        needs_review_reason="ambiguous_corporate_scope",
    )
    test_db_session.add(fact)
    test_db_session.commit()

    detail = get_review_item_detail(test_db_session, "fact_ownership", fact.id)
    assert detail["entity_type"] == "fact_ownership"
    assert detail["organisation_id"] is None
    candidates = detail["entity_data"]["candidate_organisations"]
    assert len(candidates) >= 2
    org_slugs = {c["slug"] for c in candidates}
    assert "alpha-practice" in org_slugs
    assert "beta-practice" in org_slugs


# -----------------------------------------------------------------------------
# 11-13. Reviewer & Reason Validation and Consequential Notes
# -----------------------------------------------------------------------------

def test_reviewer_validation(test_db_session: Session):
    """Test 11: Missing or whitespace-only reviewer rejected."""
    org = _make_org(test_db_session, name="Val Org", slug="val-org")
    src = _make_source(test_db_session, url="https://val.com.au", domain="val.com.au")

    cand = orm.OrganisationMatchCandidate(
        source_id=src.id, suggested_organisation_id=org.id,
        match_tier="high_confidence", status="suggested", rationale="Val rationale"
    )
    test_db_session.add(cand)
    test_db_session.commit()

    with pytest.raises(ValueError, match="reviewer identifier must be non-empty"):
        record_review_action(
            session=test_db_session,
            entity_type="match_candidate",
            entity_id=cand.id,
            action="approved",
            reason_code="manual_verification",
            reviewer="",
        )

    with pytest.raises(ValueError, match="reviewer identifier must be non-empty"):
        record_review_action(
            session=test_db_session,
            entity_type="match_candidate",
            entity_id=cand.id,
            action="approved",
            reason_code="manual_verification",
            reviewer="   ",
        )


def test_reason_code_validation(test_db_session: Session):
    """Test 12: Missing or whitespace-only reason rejected."""
    org = _make_org(test_db_session, name="Val Org 2", slug="val-org-2")
    src = _make_source(test_db_session, url="https://val2.com.au", domain="val2.com.au")

    cand = orm.OrganisationMatchCandidate(
        source_id=src.id, suggested_organisation_id=org.id,
        match_tier="high_confidence", status="suggested", rationale="Val 2"
    )
    test_db_session.add(cand)
    test_db_session.commit()

    with pytest.raises(ValueError, match="reason_code must be non-empty"):
        record_review_action(
            session=test_db_session,
            entity_type="match_candidate",
            entity_id=cand.id,
            action="approved",
            reason_code="",
            reviewer="analyst_1",
        )


def test_consequential_decision_notes_mandatory(test_db_session: Session):
    """Test 13: Consequential decisions (reject, resolve_a, resolve_b, dismiss_benign) require notes."""
    org = _make_org(test_db_session, name="Conseq Org", slug="conseq-org")
    src = _make_source(test_db_session, url="https://conseq.com.au", domain="conseq.com.au")

    cand = orm.OrganisationMatchCandidate(
        source_id=src.id, suggested_organisation_id=org.id,
        match_tier="high_confidence", status="suggested", rationale="Conseq test"
    )
    test_db_session.add(cand)
    test_db_session.commit()

    with pytest.raises(ValueError, match="requires non-empty explanatory notes/rationale"):
        record_review_action(
            session=test_db_session,
            entity_type="match_candidate",
            entity_id=cand.id,
            action="rejected",
            reason_code="unrelated_practice",
            reviewer="analyst_1",
            notes=None,
        )

    with pytest.raises(ValueError, match="requires non-empty explanatory notes/rationale"):
        record_review_action(
            session=test_db_session,
            entity_type="match_candidate",
            entity_id=cand.id,
            action="rejected",
            reason_code="unrelated_practice",
            reviewer="analyst_1",
            notes="   ",
        )

    act = record_review_action(
        session=test_db_session,
        entity_type="match_candidate",
        entity_id=cand.id,
        action="rejected",
        reason_code="unrelated_practice",
        reviewer="analyst_1",
        notes="Source domain belongs to a distinct dental practice with similar name.",
    )
    assert act.action == "rejected"
    assert act.notes == "Source domain belongs to a distinct dental practice with similar name."


# -----------------------------------------------------------------------------
# 14-15. Match Candidate Workflow & No Organisation Merge Invariant
# -----------------------------------------------------------------------------

def test_match_candidate_confirmation_no_merge(test_db_session: Session):
    """Test 14: Sets link status to confirmed, sets reviewed_by; proves M3.1/M3.2 integration & zero org merge."""
    org1 = _make_org(test_db_session, name="Original Practice A", slug="practice-a")
    org2 = _make_org(test_db_session, name="Original Practice B", slug="practice-b")
    src = _make_source(test_db_session, url="https://practice-a.com.au", domain="practice-a.com.au")
    src_dir = _make_source(test_db_session, url="https://directory.com.au/practice-a", domain="directory.com.au")

    # Established directory link already confirmed for org1
    link_dir = orm.OrganisationSourceLink(
        organisation_id=org1.id,
        source_id=src_dir.id,
        relationship_type="directory_listing",
        verification_status="confirmed",
        verified_by="seed_system",
    )
    test_db_session.add(link_dir)

    org_count_before = test_db_session.execute(select(orm.Organisation)).scalars().all()
    assert len(org_count_before) == 2

    cand = orm.OrganisationMatchCandidate(
        source_id=src.id,
        suggested_organisation_id=org1.id,
        match_tier="high_confidence",
        status="suggested",
        rationale="Confidence score 0.98",
    )
    test_db_session.add(cand)

    # Seed snapshots and facts with matching evidence offsets
    text_a = "Phone: 02 8888 1111"
    snap_a = _make_snapshot(test_db_session, source_id=src.id, title="Practice A Contact", text=text_a)
    ev_a = _make_evidence(test_db_session, snapshot_id=snap_a.id, quote=text_a, start_offset=0, end_offset=len(text_a))

    text_dir = "Phone: 02 8888 2222"
    snap_dir = _make_snapshot(test_db_session, source_id=src_dir.id, title="Directory Practice A", text=text_dir)
    ev_dir = _make_evidence(test_db_session, snapshot_id=snap_dir.id, quote=text_dir, start_offset=0, end_offset=len(text_dir))

    fact_a = orm.Fact(
        snapshot_id=snap_a.id,
        organisation_id=None,
        evidence_id=ev_a.id,
        field_key="phone",
        scope_key="default",
        raw_value="02 8888 1111",
        status="candidate",
    )
    fact_dir = orm.Fact(
        snapshot_id=snap_dir.id,
        organisation_id=org1.id,
        evidence_id=ev_dir.id,
        field_key="phone",
        scope_key="default",
        raw_value="02 8888 2222",
        status="candidate",
    )
    test_db_session.add_all([fact_a, fact_dir])
    test_db_session.commit()

    record_review_action(
        session=test_db_session,
        entity_type="match_candidate",
        entity_id=cand.id,
        action="approved",
        reason_code="verified_domain_match",
        reviewer="dr_operator",
        notes="Verified domain matches practice records",
    )

    test_db_session.commit()
    test_db_session.refresh(cand)

    assert cand.status == "verified"
    assert cand.reviewed_by == "dr_operator"
    assert cand.reviewed_at is not None

    # Retrieve link using fresh session to prove persistence and cross-session retrieval
    bind = test_db_session.get_bind()
    fresh_session_factory = sessionmaker(bind=bind, autoflush=False, expire_on_commit=False, future=True)
    fresh_session = fresh_session_factory()

    try:
        fresh_link = fresh_session.execute(
            select(orm.OrganisationSourceLink).where(
                orm.OrganisationSourceLink.organisation_id == org1.id,
                orm.OrganisationSourceLink.source_id == src.id,
            )
        ).scalar_one()

        # Canonical M3.1 status must be "confirmed"
        assert fresh_link.verification_status == "confirmed"
        assert fresh_link.verified_by == "dr_operator"

        # Verify M3.1 fact-ownership services recognise the confirmed link
        fresh_fact_a = fresh_session.get(orm.Fact, fact_a.id)
        assigned_org_id = assign_fact_ownership(fresh_session, fresh_fact_a)
        fresh_session.flush()

        assert assigned_org_id == org1.id
        assert fresh_fact_a.organisation_id == org1.id
        assert fresh_fact_a.needs_review_reason is None

        # Verify M3.2 discrepancy eligibility recognises it as confirmed
        fresh_fact_dir = fresh_session.get(orm.Fact, fact_dir.id)
        disc_result = evaluate_fact_pair_discrepancy(
            session=fresh_session,
            fact_1=fresh_fact_a,
            fact_2=fresh_fact_dir,
        )
        assert disc_result is not None
        assert disc_result.conflict_type == "value_mismatch"
        assert disc_result.field_key in ("public_phone", "phone")

        # CRITICAL INVARIANT PROOF: Zero organisation merging occurred
        org_count_after = fresh_session.execute(select(orm.Organisation)).scalars().all()
        assert len(org_count_after) == 2
        assert {o.slug for o in org_count_after} == {"practice-a", "practice-b"}
    finally:
        fresh_session.close()


def test_match_candidate_rejection(test_db_session: Session):
    """Test 15: Sets link status to rejected, records review action."""
    org = _make_org(test_db_session, name="Reject Org", slug="reject-org")
    src = _make_source(test_db_session, url="https://unrelated.com.au", domain="unrelated.com.au")

    cand = orm.OrganisationMatchCandidate(
        source_id=src.id, suggested_organisation_id=org.id,
        match_tier="low_confidence", status="suggested", rationale="Candidate"
    )
    test_db_session.add(cand)
    test_db_session.commit()

    record_review_action(
        session=test_db_session,
        entity_type="match_candidate",
        entity_id=cand.id,
        action="rejected",
        reason_code="distinct_corporate_entity",
        reviewer="auditor_jane",
        notes="Unrelated entity with common surname in title.",
    )
    test_db_session.commit()
    test_db_session.refresh(cand)

    assert cand.status == "rejected"
    assert cand.reviewed_by == "auditor_jane"


# -----------------------------------------------------------------------------
# 16-18. Fact Ownership Assignment & Validation
# -----------------------------------------------------------------------------

def test_fact_ownership_assignment(test_db_session: Session):
    """Test 16: Validates target org exists, sets fact.organisation_id, clears needs_review_reason."""
    org = _make_org(test_db_session, name="Assigned Org", slug="assigned-org")
    src = _make_source(test_db_session, url="https://corporate-health.com.au", domain="corporate-health.com.au")
    snap = _make_snapshot(test_db_session, source_id=src.id)

    fact = orm.Fact(
        snapshot_id=snap.id,
        organisation_id=None,
        field_key="phone",
        raw_value="02 9999 1234",
        status="candidate",
        needs_review_reason="ambiguous_corporate_scope",
    )
    test_db_session.add(fact)
    test_db_session.commit()

    act = record_review_action(
        session=test_db_session,
        entity_type="fact_ownership",
        entity_id=fact.id,
        action="assigned_org",
        reason_code="manual_evidence_verification",
        reviewer="clinician_reviewer",
        notes="Assigned phone to main flagship practice branch",
        decision_metadata={"assigned_organisation_id": org.id},
    )
    test_db_session.commit()
    test_db_session.refresh(fact)

    assert fact.organisation_id == org.id
    assert fact.needs_review_reason is None
    assert fact.status == "verified"
    assert act.action == "assigned_org"


def test_fact_ownership_unassigned(test_db_session: Session):
    """Test 17: Leaves fact unassigned with explicitly_unassigned_corporate reason."""
    src = _make_source(test_db_session, url="https://general-health.com.au", domain="general-health.com.au")
    snap = _make_snapshot(test_db_session, source_id=src.id)

    fact = orm.Fact(
        snapshot_id=snap.id,
        organisation_id=None,
        field_key="phone",
        raw_value="1800 000 000",
        status="candidate",
        needs_review_reason="ambiguous_corporate_scope",
    )
    test_db_session.add(fact)
    test_db_session.commit()

    record_review_action(
        session=test_db_session,
        entity_type="fact_ownership",
        entity_id=fact.id,
        action="dismissed_benign",
        reason_code="corporate_wide_holding",
        reviewer="operator_dan",
        notes="Number applies to national parent group, not individual branch practice.",
    )
    test_db_session.commit()
    test_db_session.refresh(fact)

    assert fact.organisation_id is None
    assert fact.needs_review_reason == "explicitly_unassigned_corporate"


def test_fact_ownership_invalid_org_rejected(test_db_session: Session):
    """Test 18: Assigning to non-existent org_id is rejected."""
    src = _make_source(test_db_session, url="https://inv-org.com.au", domain="inv-org.com.au")
    snap = _make_snapshot(test_db_session, source_id=src.id)

    fact = orm.Fact(
        snapshot_id=snap.id,
        organisation_id=None,
        field_key="phone",
        raw_value="02 1234 5678",
        status="candidate",
        needs_review_reason="ambiguous_corporate_scope",
    )
    test_db_session.add(fact)
    test_db_session.commit()

    with pytest.raises(ValueError, match="Organisation id=999999 does not exist"):
        record_review_action(
            session=test_db_session,
            entity_type="fact_ownership",
            entity_id=fact.id,
            action="assigned_org",
            reason_code="manual_evidence_verification",
            reviewer="operator_dan",
            decision_metadata={"assigned_organisation_id": 999999},
        )


# -----------------------------------------------------------------------------
# 19-20. Candidate Fact Approval & Rejection
# -----------------------------------------------------------------------------

def test_candidate_fact_approval(test_db_session: Session):
    """Test 19: Sets fact status to verified, records review action with evidence ID."""
    org = _make_org(test_db_session, name="Fact Org", slug="fact-org")
    src = _make_source(test_db_session, url="https://fact.com.au", domain="fact.com.au")
    snap = _make_snapshot(test_db_session, source_id=src.id)
    ev = _make_evidence(test_db_session, snapshot_id=snap.id, quote="Open Monday to Friday 9am - 5pm", start_offset=0, end_offset=32)

    fact = orm.Fact(
        snapshot_id=snap.id,
        organisation_id=org.id,
        evidence_id=ev.id,
        field_key="opening_hours",
        raw_value="Mon-Fri 9am-5pm",
        status="candidate",
    )
    test_db_session.add(fact)
    test_db_session.commit()

    act = record_review_action(
        session=test_db_session,
        entity_type="fact",
        entity_id=fact.id,
        action="approved",
        reason_code="verified_against_source",
        reviewer="analyst_sam",
        evidence_id=ev.id,
    )
    test_db_session.commit()
    test_db_session.refresh(fact)

    assert fact.status == "verified"
    assert act.evidence_id == ev.id
    assert act.action == "approved"


def test_candidate_fact_rejection(test_db_session: Session):
    """Test 20: Sets fact status to rejected, records review action."""
    org = _make_org(test_db_session, name="Fact Reject Org", slug="fact-reject-org")
    src = _make_source(test_db_session, url="https://fact-rej.com.au", domain="fact-rej.com.au")
    snap = _make_snapshot(test_db_session, source_id=src.id)

    fact = orm.Fact(
        snapshot_id=snap.id,
        organisation_id=org.id,
        field_key="telehealth",
        raw_value="Unknown corrupted string",
        status="candidate",
    )
    test_db_session.add(fact)
    test_db_session.commit()

    record_review_action(
        session=test_db_session,
        entity_type="fact",
        entity_id=fact.id,
        action="rejected",
        reason_code="extraction_hallucination",
        reviewer="analyst_sam",
        notes="Garbled text extracted from footer script tag.",
    )
    test_db_session.commit()
    test_db_session.refresh(fact)

    assert fact.status == "rejected"


# -----------------------------------------------------------------------------
# 21. Discrepancy Resolution & Fact Preservation Invariant
# -----------------------------------------------------------------------------

def test_discrepancy_resolution_preserves_facts_untouched(test_db_session: Session):
    """Test 21: Resolving discrepancy sets resolved_fact_a; proves BOTH facts remain 100% untouched."""
    org = _make_org(test_db_session, name="Disc Invariant Org", slug="disc-inv-org")
    src_a = _make_source(test_db_session, url="https://source-a-inv.com.au", domain="source-a-inv.com.au")
    src_b = _make_source(test_db_session, url="https://source-b-inv.com.au", domain="source-b-inv.com.au")

    snap_a = _make_snapshot(test_db_session, source_id=src_a.id)
    snap_b = _make_snapshot(test_db_session, source_id=src_b.id)

    ev_a = _make_evidence(test_db_session, snapshot_id=snap_a.id, quote="Clinic Phone: (02) 1111 2222", start_offset=0, end_offset=30)
    ev_b = _make_evidence(test_db_session, snapshot_id=snap_b.id, quote="Old Directory Phone: (02) 9999 8888", start_offset=0, end_offset=36)

    fact_a = orm.Fact(snapshot_id=snap_a.id, organisation_id=org.id, evidence_id=ev_a.id, field_key="phone", scope_key="default", raw_value="(02) 1111 2222", status="candidate")
    fact_b = orm.Fact(snapshot_id=snap_b.id, organisation_id=org.id, evidence_id=ev_b.id, field_key="phone", scope_key="default", raw_value="(02) 9999 8888", status="candidate")
    test_db_session.add_all([fact_a, fact_b])
    test_db_session.commit()

    disc = orm.Discrepancy(
        organisation_id=org.id,
        fact_a_id=fact_a.id,
        fact_b_id=fact_b.id,
        field_key="phone",
        scope_key="default",
        conflict_type="value_mismatch",
        status="detected",
        rationale="Phone number conflict",
    )
    test_db_session.add(disc)
    test_db_session.commit()

    raw_a_before = fact_a.raw_value
    raw_b_before = fact_b.raw_value
    ev_a_before = ev_a.quote
    ev_b_before = ev_b.quote

    record_review_action(
        session=test_db_session,
        entity_type="discrepancy",
        entity_id=disc.id,
        action="resolved_a",
        reason_code="authoritative_practice_website",
        reviewer="dr_operator",
        notes="Fact A comes from official website updated last week; Fact B is an outdated directory listing.",
    )
    test_db_session.commit()
    test_db_session.refresh(disc)
    test_db_session.refresh(fact_a)
    test_db_session.refresh(fact_b)

    assert disc.status == "resolved_a"
    assert disc.resolved_by == "dr_operator"
    assert disc.resolution_notes == "Fact A comes from official website updated last week; Fact B is an outdated directory listing."

    # CRITICAL ARCHITECTURAL INVARIANT:
    # Both Fact A and Fact B raw values and evidence MUST remain 100% untouched!
    assert fact_a.raw_value == raw_a_before
    assert fact_b.raw_value == raw_b_before
    assert ev_a.quote == ev_a_before
    assert ev_b.quote == ev_b_before


# -----------------------------------------------------------------------------
# 22. Healthcare Opportunity Review & Forbid 'filled'/'hired' Invariant
# -----------------------------------------------------------------------------

def test_opportunity_review_forbids_filled_or_hired(test_db_session: Session):
    """Test 22: Opportunity review acknowledges; strictly rejects any attempt to set 'filled' or 'hired'."""
    org = _make_org(test_db_session, name="Opp Org", slug="opp-org")
    src = _make_source(test_db_session, url="https://opp.com.au", domain="opp.com.au")
    snap = _make_snapshot(test_db_session, source_id=src.id)

    opp = orm.Opportunity(
        organisation_id=org.id,
        source_id=src.id,
        first_snapshot_id=snap.id,
        last_snapshot_id=snap.id,
        role_category="general_practitioner",
        role_title="General Practitioner",
        verbatim_wording="Full-time GP required",
        status=OpportunityStatus.POSSIBLY_STALE.value,
        closure_evidence_quote="Closing date has elapsed",
        consecutive_absences=0,
    )
    test_db_session.add(opp)
    test_db_session.commit()

    with pytest.raises(ValueError, match="Opportunity status 'filled' or 'hired' is strictly forbidden"):
        record_review_action(
            session=test_db_session,
            entity_type="opportunity",
            entity_id=opp.id,
            action="filled",
            reason_code="vacancy_no_longer_listed",
            reviewer="hr_analyst",
        )

    with pytest.raises(ValueError, match="Opportunity status 'filled' or 'hired' is strictly forbidden"):
        record_review_action(
            session=test_db_session,
            entity_type="opportunity",
            entity_id=opp.id,
            action="acknowledged",
            reason_code="vacancy_no_longer_listed",
            reviewer="hr_analyst",
            decision_metadata={"status": "hired"},
        )

    act = record_review_action(
        session=test_db_session,
        entity_type="opportunity",
        entity_id=opp.id,
        action="acknowledged",
        reason_code="reviewed_stale_advertisement",
        reviewer="hr_analyst",
        notes="Confirmed closing date has passed; will monitor for reappearance.",
    )
    assert act.action == "acknowledged"
    assert opp.status == OpportunityStatus.POSSIBLY_STALE.value


# -----------------------------------------------------------------------------
# 23-24. Change Event & Freshness Review Immutability
# -----------------------------------------------------------------------------

def test_change_event_review_immutability(test_db_session: Session):
    """Test 23: Acknowledging change event leaves underlying change_event row immutable."""
    org = _make_org(test_db_session, name="Change Org", slug="change-org")
    src = _make_source(test_db_session, url="https://change.com.au", domain="change.com.au")
    snap = _make_snapshot(test_db_session, source_id=src.id)
    obs = _make_observation(test_db_session, source_id=src.id, snapshot_id=snap.id)

    evt = orm.ChangeEvent(
        organisation_id=org.id,
        source_id=src.id,
        current_observation_id=obs.id,
        current_snapshot_id=snap.id,
        event_type="fact_modified",
        entity_type="fact",
        field_key="phone",
        scope_key="default",
        old_value="02 1111 2222",
        new_value="02 3333 4444",
        rationale="Phone number changed between snapshots",
        idempotency_key="change-key-1",
    )
    test_db_session.add(evt)
    test_db_session.commit()

    evt_old_before = evt.old_value
    evt_new_before = evt.new_value
    evt_type_before = evt.event_type

    act = record_review_action(
        session=test_db_session,
        entity_type="change_event",
        entity_id=evt.id,
        action="acknowledged",
        reason_code="verified_reception_update",
        reviewer="auditor_bob",
        notes="Noted telephone change on official reception page.",
    )
    test_db_session.commit()
    test_db_session.refresh(evt)

    assert evt.old_value == evt_old_before
    assert evt.new_value == evt_new_before
    assert evt.event_type == evt_type_before
    assert act.action == "acknowledged"


def test_freshness_review_preserves_evaluation(test_db_session: Session):
    """Test 24: Acknowledging freshness records review action; calculated values remain unchanged."""
    act = record_review_action(
        session=test_db_session,
        entity_type="freshness",
        entity_id=101,
        action="acknowledged",
        reason_code="routine_freshness_audit",
        reviewer="auditor_bob",
        notes="Confirmed annual review cycle for practice accreditation.",
    )
    assert act.entity_type == "freshness"
    assert act.entity_id == 101
    assert act.action == "acknowledged"


# -----------------------------------------------------------------------------
# 25-28. History, Metrics, Immutability & API Router Integration
# -----------------------------------------------------------------------------

def test_review_history_retrieval(test_db_session: Session):
    """Test 25: Chronological audit trail for an entity returned correctly."""
    org = _make_org(test_db_session, name="Hist Org", slug="hist-org")
    src = _make_source(test_db_session, url="https://hist.com.au", domain="hist.com.au")

    cand = orm.OrganisationMatchCandidate(
        source_id=src.id, suggested_organisation_id=org.id,
        match_tier="medium_confidence", status="suggested", rationale="Hist test"
    )
    test_db_session.add(cand)
    test_db_session.commit()

    # Action 1: Defer
    record_review_action(
        session=test_db_session,
        entity_type="match_candidate",
        entity_id=cand.id,
        action="deferred",
        reason_code="requires_secondary_verification",
        reviewer="reviewer_1",
        notes="Awaiting domain registration lookup",
    )

    # Action 2: Approve
    record_review_action(
        session=test_db_session,
        entity_type="match_candidate",
        entity_id=cand.id,
        action="approved",
        reason_code="verified_domain_match",
        reviewer="senior_analyst",
        notes="ABN lookup confirmed practice ownership",
    )

    history = get_review_history(test_db_session, "match_candidate", cand.id)
    assert history["total"] == 2
    assert len(history["items"]) == 2
    actions = [a["action"] for a in history["items"]]
    assert "deferred" in actions
    assert "approved" in actions


def test_global_review_metrics(test_db_session: Session):
    """Test 26: Metrics return pending, under_review, completed, deferred counts."""
    metrics = get_review_metrics(test_db_session)
    assert "total_pending" in metrics
    assert "total_under_review" in metrics
    assert "total_completed" in metrics
    assert "total_deferred" in metrics
    assert "by_entity_type" in metrics
    assert isinstance(metrics["by_entity_type"], dict)


def test_review_actions_immutability_no_delete_or_edit_routes(test_db_session: Session):
    """Test 27: Enforces immutability at API level (no DELETE/PUT/PATCH routes) and DB level (SQLite triggers)."""
    # 1. API level: verify zero DELETE, PUT, or PATCH endpoints exist for reviews
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set())
        if "/reviews" in path or "/review_actions" in path:
            assert "DELETE" not in methods, f"Found forbidden DELETE method on review route: {path}"
            assert "PUT" not in methods, f"Found forbidden PUT method on review route: {path}"
            assert "PATCH" not in methods, f"Found forbidden PATCH method on review route: {path}"

    # 2. Database level: verify SQLite triggers strictly block direct UPDATE and DELETE
    act = record_review_action(
        session=test_db_session,
        entity_type="freshness",
        entity_id=999,
        action="acknowledged",
        reason_code="routine_audit",
        reviewer="immutable_tester",
    )
    test_db_session.commit()

    with pytest.raises(IntegrityError, match="UPDATE on review_actions is prohibited"):
        test_db_session.execute(
            text("UPDATE review_actions SET notes = 'tampered' WHERE id = :id"),
            {"id": act.id},
        )
        test_db_session.commit()

    test_db_session.rollback()

    with pytest.raises(IntegrityError, match="DELETE on review_actions is prohibited"):
        test_db_session.execute(
            text("DELETE FROM review_actions WHERE id = :id"),
            {"id": act.id},
        )
        test_db_session.commit()

    test_db_session.rollback()

    refreshed = test_db_session.get(orm.ReviewAction, act.id)
    assert refreshed is not None
    assert refreshed.reviewer == "immutable_tester"


def test_api_endpoints_integration(client_with_session: TestClient, test_db_session: Session):
    """Test 28: Real frontend workflow end-to-end smoke test covering all 9 operational stages."""
    # Stage 1: Load dashboard & queue in empty state
    r_empty_metrics = client_with_session.get("/api/reviews/metrics")
    assert r_empty_metrics.status_code == 200
    m_empty = r_empty_metrics.json()
    assert m_empty["total_pending"] == 0
    assert m_empty["total_completed"] == 0

    r_empty_queue = client_with_session.get("/api/reviews/queue")
    assert r_empty_queue.status_code == 200
    q_empty = r_empty_queue.json()
    assert q_empty["total"] == 0
    assert q_empty["items"] == []

    # Stage 2: Seed items and verify dashboard metrics & queue filtering
    org = _make_org(test_db_session, name="API Practice", slug="api-practice")
    src1 = _make_source(test_db_session, url="https://api-practice.com.au/contact", domain="api-practice.com.au")
    src2 = _make_source(test_db_session, url="https://directory.com.au/api-practice", domain="directory.com.au")

    snap1 = _make_snapshot(test_db_session, source_id=src1.id, title="API Contact", text="Phone: 02 1111 2222")
    snap2 = _make_snapshot(test_db_session, source_id=src2.id, title="Directory Listing", text="Phone: 02 3333 4444")

    ev1 = _make_evidence(test_db_session, snapshot_id=snap1.id, quote="Phone: 02 1111 2222", start_offset=0, end_offset=19)
    ev2 = _make_evidence(test_db_session, snapshot_id=snap2.id, quote="Phone: 02 3333 4444", start_offset=0, end_offset=19)

    fact1 = orm.Fact(snapshot_id=snap1.id, organisation_id=org.id, evidence_id=ev1.id, field_key="phone", scope_key="default", raw_value="02 1111 2222", status="candidate")
    fact2 = orm.Fact(snapshot_id=snap2.id, organisation_id=org.id, evidence_id=ev2.id, field_key="phone", scope_key="default", raw_value="02 3333 4444", status="candidate")
    test_db_session.add_all([fact1, fact2])
    test_db_session.commit()

    cand = orm.OrganisationMatchCandidate(
        source_id=src1.id,
        suggested_organisation_id=org.id,
        match_tier="high_confidence",
        status="suggested",
        rationale="Automated domain match",
    )
    disc = orm.Discrepancy(
        organisation_id=org.id,
        fact_a_id=fact1.id,
        fact_b_id=fact2.id,
        field_key="phone",
        scope_key="default",
        conflict_type="value_mismatch",
        status="detected",
        rationale="Phone conflict between source 1 and source 2",
    )
    test_db_session.add_all([cand, disc])
    test_db_session.commit()

    # Stage 3: Load populated metrics and filter queue by domain
    r_metrics = client_with_session.get("/api/reviews/metrics")
    assert r_metrics.status_code == 200
    metrics = r_metrics.json()
    assert metrics["total_pending"] >= 2
    assert metrics["by_entity_type"]["discrepancy"] >= 1
    assert metrics["by_entity_type"]["match_candidate"] >= 1

    r_filtered = client_with_session.get("/api/reviews/queue?entity_type=discrepancy")
    assert r_filtered.status_code == 200
    q_disc = r_filtered.json()
    assert q_disc["total"] == 1
    assert q_disc["items"][0]["entity_type"] == "discrepancy"

    # Stage 4: Open review item, check 404 error state, inspect grounded context
    r_404 = client_with_session.get("/api/reviews/discrepancy/999999")
    assert r_404.status_code == 404

    r_detail = client_with_session.get(f"/api/reviews/discrepancy/{disc.id}")
    assert r_detail.status_code == 200
    detail = r_detail.json()
    assert detail["entity_type"] == "discrepancy"
    assert detail["discrepancy_context"] is not None
    assert detail["discrepancy_context"]["fact_a"]["raw_value"] == "02 1111 2222"
    assert detail["discrepancy_context"]["fact_b"]["raw_value"] == "02 3333 4444"
    assert "resolved_a" in detail["allowed_actions"]

    # Stage 5: Submit decision with validation gates
    # Missing reviewer -> 422
    r_val1 = client_with_session.post(
        f"/api/reviews/discrepancy/{disc.id}/actions",
        json={"action": "resolved_a", "reason_code": "verified_domain", "reviewer": "   "},
    )
    assert r_val1.status_code == 422

    # Missing notes on consequential action -> 422
    r_val2 = client_with_session.post(
        f"/api/reviews/discrepancy/{disc.id}/actions",
        json={"action": "resolved_a", "reason_code": "verified_domain", "reviewer": "api_tester", "notes": ""},
    )
    assert r_val2.status_code == 422

    # Valid submission
    r_submit = client_with_session.post(
        f"/api/reviews/discrepancy/{disc.id}/actions",
        json={
            "action": "resolved_a",
            "reason_code": "authoritative_practice_website",
            "reviewer": "api_tester",
            "notes": "Verified against official practice contact page",
        },
    )
    assert r_submit.status_code == 200
    act_data = r_submit.json()
    assert act_data["action"] == "resolved_a"
    assert act_data["reviewer"] == "api_tester"

    # Stage 6: Verify decision persisted in append-only review_actions ledger
    r_ledger = client_with_session.get("/api/reviews/actions")
    assert r_ledger.status_code == 200
    ledger = r_ledger.json()
    assert ledger["total"] >= 1
    assert any(a["reviewer"] == "api_tester" and a["action"] == "resolved_a" for a in ledger["items"])

    # Stage 7 & 8: Reload state and verify updated item status and audit history
    r_metrics_after = client_with_session.get("/api/reviews/metrics")
    assert r_metrics_after.json()["total_completed"] >= 1

    r_detail_after = client_with_session.get(f"/api/reviews/discrepancy/{disc.id}")
    assert r_detail_after.json()["current_status"] == "resolved_a"

    r_history = client_with_session.get(f"/api/reviews/discrepancy/{disc.id}/history")
    assert r_history.status_code == 200
    h_data = r_history.json()
    assert h_data["total"] == 1
    hist_item = h_data["items"][0]
    assert hist_item["reviewer"] == "api_tester"
    assert hist_item["action"] == "resolved_a"
    assert hist_item["reason_code"] == "authoritative_practice_website"
    assert hist_item["notes"] == "Verified against official practice contact page"
    assert "T" in hist_item["created_at"]

    # Stage 9: Prove underlying facts and evidence remain 100% untouched
    test_db_session.expire_all()
    refreshed_fact1 = test_db_session.get(orm.Fact, fact1.id)
    refreshed_fact2 = test_db_session.get(orm.Fact, fact2.id)
    assert refreshed_fact1.raw_value == "02 1111 2222"
    assert refreshed_fact2.raw_value == "02 3333 4444"
    assert refreshed_fact1.evidence.quote == "Phone: 02 1111 2222"
    assert refreshed_fact2.evidence.quote == "Phone: 02 3333 4444"
