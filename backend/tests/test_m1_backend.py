"""M1 backend test suite — the first complete vertical slice (pre-AI).

Covers the M1-AC test map (docs/IMPLEMENTATION_PLAN.md §3) using the real
application: policy gate, source registry, fixture loader, parser, snapshot
store (hashing/short-circuit/history) and the FastAPI contract.
Run:  pytest backend/tests
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.exceptions import (
    FixtureError,
    LiveFetchNotAuthorised,
    SourceBlocked,
)
from backend.app.db import orm
from backend.app.db.orm import PageSnapshot
from backend.app.ingest import snapshot_store
from backend.app.ingest.pipeline import run_ingestion
from backend.app.models import SourceCreate, SourcePatch
from backend.app.parsers import text_extract
from backend.app.parsers.text_extract import TextExtractionError
from backend.app.sources import policy_gate, registry
from backend.app.sources.fixture_loader import load_fixture, load_manifest


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True)
    orm.Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, future=True)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def client(m1_client):
    return m1_client


def register_fixture_source(client: TestClient, fixture_id: str = "F3", **overrides) -> dict:
    archetype_by_id = {fid: f["archetype"] for fid, f in load_manifest()["fixtures"].items()}
    payload = {
        "kind": "fixture",
        "page_type": archetype_by_id.get(fixture_id, "other"),
        "fixture_id": fixture_id,
        "label": f"Synthetic {fixture_id}",
        "intended_fields": ["organisation_name"],
        **overrides,
    }
    response = client.post("/api/sources", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Source registration & validation (registry + policy gate)
# ---------------------------------------------------------------------------

def test_register_fixture_source_succeeds(client):
    created = register_fixture_source(client, "F3")
    assert created["kind"] == "fixture"
    assert created["fixture_id"] == "F3"
    assert created["policy_status"] == "manual_fixture_pending_policy_check"
    assert created["id"] > 0
    assert created["intended_fields"] == ["organisation_name"]
    assert created["label"] == "Synthetic F3"  # label persists (regression: silently dropped)


def test_register_url_source_is_documented_never_approved(client):
    """URL sources register as manual_fixture_pending_policy_check; can't become fetchable."""
    response = client.post("/api/sources", json={"kind": "url", "page_type": "other", "url": "https://clinic.example.com/"})
    assert response.status_code == 201
    assert response.json()["policy_status"] == "manual_fixture_pending_policy_check"


def test_approved_live_cannot_be_set_via_api(client):
    response = client.post(
        "/api/sources",
        json={"kind": "url", "page_type": "other", "url": "https://clinic.example.com/", "policy_status": "approved_live"},
    )
    assert response.status_code == 422
    assert "approved_live" in response.json()["error"]["message"]


def test_unknown_fixture_id_rejected(client):
    response = client.post("/api/sources", json={"kind": "fixture", "page_type": "contact", "fixture_id": "F99"})
    assert response.status_code == 422


def test_invalid_requests_rejected_with_422(client):
    assert client.post("/api/sources", json={"kind": "scrape", "page_type": "contact"}).status_code == 422
    assert client.post("/api/sources", json={"kind": "fixture", "page_type": "nope", "fixture_id": "F3"}).status_code == 422
    assert client.post("/api/sources", json={"kind": "fixture", "page_type": "contact", "fixture_id": "F3", "hacker": True}).status_code == 422


def test_patch_updates_metadata_but_not_policy(client):
    created = register_fixture_source(client, "F2")
    patched = client.patch(f"/api/sources/{created['id']}", json={"label": "Renamed", "page_type": "services"})
    assert patched.status_code == 200
    assert patched.json()["label"] == "Renamed"
    # policy_status and robots_status are not API-editable (extra="forbid")
    refute = client.patch(f"/api/sources/{created['id']}", json={"policy_status": "approved_live"})
    assert refute.status_code == 422


def test_delete_cascades_snapshots(client):
    created = register_fixture_source(client, "F1")
    ingest_response = client.post(f"/api/ingestions/{created['id']}")
    assert ingest_response.status_code == 200
    snap_id = ingest_response.json()["snapshot_id"]
    assert snap_id is not None
    source_id = created["id"]

    deleted = client.delete(f"/api/sources/{source_id}")
    assert deleted.status_code == 204
    assert client.get(f"/api/sources/{source_id}").status_code == 404
    # Cascade (D8): the snapshot rows went with the source — not stray-orphans.
    assert client.get(f"/api/snapshots/{snap_id}").status_code == 404


# ---------------------------------------------------------------------------
# Policy gate: fixture-only enforcement, rejection precedes any network access
# ---------------------------------------------------------------------------

def test_gate_rejects_url_ingestion_before_any_network_access(db):
    """M1-AC 1: no sockets may be opened; the gate refuses before anything else."""
    import socket

    source = registry.create_source(
        db,
        SourceCreate(
            kind="url", page_type="other",
            url="https://unapproved-host.example.com/",
            policy_status="manual_fixture_pending_policy_check",
        ),
    )

    def no_sockets(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("policy gate must reject BEFORE any socket creation")

    original_socket = socket.socket
    socket.socket = no_sockets
    try:
        with pytest.raises(LiveFetchNotAuthorised):
            run_ingestion(db, source)
    finally:
        socket.socket = original_socket


def test_gate_blocks_blocked_source(db):
    source = registry.create_source(db, SourceCreate(kind="fixture", page_type="contact", fixture_id="F3", policy_status="blocked"))
    with pytest.raises(SourceBlocked):
        run_ingestion(db, source)


def test_gate_allows_fixture_source_past_prefetch(db):
    source = registry.create_source(db, SourceCreate(kind="fixture", page_type="team", fixture_id="F4"))
    policy_gate.validate_ingestion_ready(source)  # must not raise


def test_api_ingestion_rejects_url_source_with_409(client):
    url_source = client.post("/api/sources", json={"kind": "url", "page_type": "other", "url": "https://sc.example/"}).json()
    response = client.post(f"/api/ingestions/{url_source['id']}")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "live_fetch_not_authorised"


def test_api_ingestion_rejects_blocked_source_with_409(client):
    blocked = register_fixture_source(client, "F3", policy_status="blocked")
    response = client.post(f"/api/ingestions/{blocked['id']}")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "source_blocked"


# ---------------------------------------------------------------------------
# Fixture loader
# ---------------------------------------------------------------------------

def test_fixture_loader_returns_validated_package():
    fixture = load_fixture("F3")
    assert fixture.id == "F3"
    assert fixture.archetype == "contact"
    assert "opening_hours_clinic" in fixture.intended_fields
    assert fixture.synthetic and fixture.invented_data and fixture.not_a_live_scrape


def test_fixture_loader_rejects_unknown_id():
    with pytest.raises(FixtureError):
        load_fixture("F99")


def test_manifest_entries_match_files():
    manifest = load_manifest()
    assert sorted(manifest["fixtures"]) == ["F1", "F2", "F3", "F4", "F5", "F6"]


# ---------------------------------------------------------------------------
# Parser: headings, hierarchy, script/style/hidden removal, offsets
# ---------------------------------------------------------------------------

def test_parser_preserves_heading_hierarchy_with_exact_offsets():
    parsed = text_extract.parse(load_fixture("F3").html)
    levels = [h.level for h in parsed.headings]
    assert levels.count(1) >= 1 and 2 in levels
    assert [h.text for h in parsed.headings] == ["Contact us", "Address", "Phone and email", "Opening hours", "Bookings"]
    for h in parsed.headings:
        assert parsed.text[h.start_offset : h.end_offset] == h.text


def test_parser_strips_script_style_and_hidden_content():
    # F6 bundles adversarial content: script text, style, hidden injection block.
    parsed = text_extract.parse(load_fixture("F6").html)
    assert "hiddenPrompt" not in parsed.text
    assert "Ignore all previous instructions" not in parsed.text
    assert "13 0000 0000" not in parsed.text
    assert "SYSTEM NOTE FOR THE AI" not in parsed.text
    assert "malformedJava" not in parsed.text
    assert "display:none" not in parsed.text
    # The organisation name appears in <title> (F6 body never repeats it) —
    # title extraction must still work alongside body text.
    assert parsed.title == "Policies — Alara Park Medical Practice"
    assert "Page last updated: March 2024" in parsed.text
    # F1: visually-hidden skip link must not leak into text.
    parsed_f1 = text_extract.parse(load_fixture("F1").html)
    assert "Skip to content" not in parsed_f1.text


def test_parser_is_deterministic():
    html = load_fixture("F2").html
    first = text_extract.parse(html)
    second = text_extract.parse(html)
    assert first.text == second.text
    assert [h.start_offset for h in first.headings] == [h.start_offset for h in second.headings]


def test_parser_rejects_unusable_html():
    with pytest.raises(TextExtractionError):
        text_extract.parse("")
    with pytest.raises(TextExtractionError):
        text_extract.parse("<html><head></head><body><script>only junk</script></body></html>")
    with pytest.raises(TextExtractionError):
        text_extract.parse("<html><body></body></html>")


def test_parser_version_is_recorded():
    assert text_extract.PARSER_VERSION == "text_extract_v1"


# ---------------------------------------------------------------------------
# Snapshot store: hash determinism, short-circuit, history preservation
# ---------------------------------------------------------------------------

def test_content_hash_is_deterministic():
    text = text_extract.parse(load_fixture("F5").html).text
    h1 = snapshot_store.compute_content_hash(text_extract.PARSER_VERSION, text)
    h2 = snapshot_store.compute_content_hash(text_extract.PARSER_VERSION, text)
    assert h1 == h2 and len(h1) == 64
    other = snapshot_store.compute_content_hash(text_extract.PARSER_VERSION, text + "x")
    assert other != h1


def test_unchanged_ingestion_short_circuits_no_new_snapshot(db):
    source = registry.create_source(db, SourceCreate(kind="fixture", page_type="vacancies", fixture_id="F5"))
    first = run_ingestion(db, source)
    second = run_ingestion(db, source)
    assert first.unchanged is False and first.snapshot_id is not None
    assert second.unchanged is True
    assert second.snapshot_id == first.snapshot_id
    assert len(source.snapshots) == 1  # no duplicate row created


def test_changed_content_preserves_history(db):
    """When content changes: new snapshot created, historical row kept intact."""
    source = registry.create_source(db, SourceCreate(kind="fixture", page_type="other", fixture_id="F1"))
    first_snapshot, first_outcome = snapshot_store.persist_snapshot(db, source, text_extract.parse(load_fixture("F1").html))
    changed = text_extract.parse(load_fixture("F1").html)
    changed.text = changed.text + "\nA new line was added to represent a content change."
    second_snapshot, second_outcome = snapshot_store.persist_snapshot(db, source, changed)
    assert first_outcome.snapshot_id != second_outcome.snapshot_id
    assert first_snapshot.content_hash != second_snapshot.content_hash
    assert "A new line was added" not in first_snapshot.text  # historical row untouched
    assert second_snapshot.text.endswith("A new line was added to represent a content change.")
    assert len(source.snapshots) == 2


def test_manufactured_change_flags_page_type_only_when_consistent(db):
    """Registering a fixture under a mismatched page_type is refused at ingest."""
    source = registry.create_source(db, SourceCreate(kind="fixture", page_type="home", fixture_id="F5"))
    from backend.app.core.exceptions import ValidationError_

    with pytest.raises(ValidationError_):
        run_ingestion(db, source)
    ok = registry.create_source(db, SourceCreate(kind="fixture", page_type="other", fixture_id="F5"))
    policy_gate.validate_ingestion_ready(ok)


# ---------------------------------------------------------------------------
# DI reproducibility
# ---------------------------------------------------------------------------

def test_create_all_is_idempotent(tmp_path):
    db_file = tmp_path / "repro.db"
    from sqlalchemy import create_engine
    from backend.app.db.base import init_db

    engine = create_engine(f"sqlite:///{db_file}")
    init_db(engine)
    init_db(engine)  # second call must not raise or duplicate tables
    from sqlalchemy import inspect

    tables = sorted(inspect(engine).get_table_names())
    assert set(["page_snapshots", "sources"]).issubset(set(tables))
    assert {"evidence", "facts", "prompt_runs"}.issubset(set(tables))
    engine.dispose()


# ---------------------------------------------------------------------------
# API contract end-to-end (registration → ingestion → snapshot read)
# ---------------------------------------------------------------------------

def test_full_vertical_slice_via_api(client):
    created = register_fixture_source(client, "F4")
    detail = client.get(f"/api/sources/{created['id']}")
    assert detail.status_code == 200
    assert detail.json()["snapshots"] == []

    ingest1 = client.post(f"/api/ingestions/{created['id']}")
    assert ingest1.status_code == 200
    result1 = ingest1.json()
    assert result1["unchanged"] is False and result1["snapshot_id"] is not None

    ingest2 = client.post(f"/api/ingestions/{created['id']}")
    assert ingest2.json()["unchanged"] is True
    assert ingest2.json()["snapshot_id"] == result1["snapshot_id"]

    snap = client.get(f"/api/snapshots/{result1['snapshot_id']}")
    assert snap.status_code == 200
    body = snap.json()
    assert body["fixture"] is True and body["source_fixture"] is True
    assert body["source_id"] == created["id"]
    assert any("Our team" == h["text"] for h in body["headings"])
    for h in body["headings"]:
        assert body["text"][h["start_offset"] : h["end_offset"]] == h["text"]
    assert "(07) 5550 0333" in body["text"]  # testimonial number present in text; extraction gating is M2

    # Exactly one snapshot row, reused by the second ingestion (no duplicates).
    detail = client.get(f"/api/sources/{created['id']}").json()
    assert len(detail["snapshots"]) == 1
    assert detail["last_snapshot_id"] == result1["snapshot_id"]


def test_missing_resources_return_normalised_404(client):
    assert client.get("/api/snapshots/424242").status_code == 404
    assert client.get("/api/snapshots/424242").json()["error"]["code"] == "snapshot_not_found"
    assert client.get("/api/sources/424242").status_code == 404
    assert client.get("/api/sources/424242").json()["error"]["code"] == "source_not_found"
    assert client.post("/api/ingestions/424242").status_code == 404


def test_sources_list_and_health_endpoints(client):
    register_fixture_source(client, "F1")
    listing = client.get("/api/sources")
    assert listing.status_code == 200
    assert len(listing.json()) >= 1
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["app_ok"] is True
    assert "not used in M1" in health.json()["ollama"]


def test_source_detail_lists_snapshot_history(client):
    created = register_fixture_source(client, "F2")
    snap_id = client.post(f"/api/ingestions/{created['id']}").json()["snapshot_id"]
    detail = client.get(f"/api/sources/{created['id']}").json()
    assert [s["id"] for s in detail["snapshots"]] == [snap_id]
    assert detail["last_snapshot_hash"] == next(s["content_hash"] for s in detail["snapshots"])
