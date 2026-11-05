"""Fixture loader: loads the M0 synthetic fixture corpus. Never contacts the network.

Responsibility: locate, validate and return fixture packages from
``backend/tests/fixtures`` using the **actual manifest schema** (inspected,
not assumed): ``id`` / ``file`` / ``archetype`` / ``intended_fields`` plus
the per-page required fields enforced by the M0 conformance tests.

Inputs:  a fixture id such as ``"F3"``.
Outputs: a validated ``Fixture`` record (html + metadata + provenance flags).
Failure modes: unknown id, missing file, path traversal attempt, or a page
violating the manifest schema contract → typed ``FixtureError``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from backend.app.core.exceptions import FixtureError
from backend.app.core.logging import get_logger

logger = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES_DIR = REPO_ROOT / "backend" / "tests" / "fixtures"

# Mirror of the manifest schema_contract (validated, not blindly trusted).
REQUIRED_PAGE_FIELDS = (
    "id", "label", "synthetic", "invented_data", "not_a_live_scrape",
    "archetype", "reference_archetype", "reference_url_note",
    "intended_fields", "edge_cases", "synthesis_rationale", "html",
)
REQUIRED_BOOLEAN_TRUE = ("synthetic", "invented_data", "not_a_live_scrape")


@dataclass(frozen=True)
class Fixture:
    """One validated synthetic fixture (provenance flags preserved)."""

    id: str
    file: str
    archetype: str
    intended_fields: list[str]
    edge_cases: list[str] = field(default_factory=list)
    label: str = ""
    synthesis_rationale: str = ""
    html: str = ""
    synthetic: bool = True
    invented_data: bool = True
    not_a_live_scrape: bool = True


_manifest_cache: dict | None = None


def load_manifest() -> dict:
    """Load and minimally validate the fixture manifest (cached)."""
    global _manifest_cache
    if _manifest_cache is not None:
        return _manifest_cache
    path = FIXTURES_DIR / "fixture_manifest.json"
    if not path.is_file():
        raise FixtureError(f"fixture manifest missing at {path}")
    try:
        with path.open("r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        raise FixtureError(f"fixture manifest unreadable/invalid: {exc}") from exc

    entries = manifest.get("fixtures")
    if not isinstance(entries, list) or not entries:
        raise FixtureError("fixture manifest has no 'fixtures' list")

    seen: dict[str, dict] = {}
    for entry in entries:
        fid = str(entry.get("id", ""))
        if not fid or fid in seen:
            raise FixtureError(f"fixture manifest entry has missing/duplicate id: {entry!r}")
        if "file" not in entry or "archetype" not in entry or "intended_fields" not in entry:
            raise FixtureError(f"fixture {fid} manifest entry lacks required keys")
        # Path must stay inside the fixtures directory (no traversal).
        candidate = (FIXTURES_DIR / entry["file"]).resolve()
        try:
            candidate.relative_to(FIXTURES_DIR.resolve())
        except ValueError as exc:
            raise FixtureError(f"fixture {fid} path escapes fixtures dir: {entry['file']}") from exc
        if not candidate.is_file():
            raise FixtureError(f"fixture {fid} file missing: {entry['file']}")
        seen[fid] = entry

    _manifest_cache = {"fixtures": seen}
    logger.info("fixture_manifest_loaded count=%d", len(seen))
    return _manifest_cache


def load_fixture(fixture_id: str) -> Fixture:
    """Load + validate one fixture package by manifest id (e.g. 'F3')."""
    manifest = load_manifest()
    entry = manifest["fixtures"].get(fixture_id)
    if entry is None:
        raise FixtureError(
            f"unknown fixture id '{fixture_id}'",
            known=sorted(manifest["fixtures"]),
        )
    page_path = FIXTURES_DIR / entry["file"]
    try:
        with page_path.open("r", encoding="utf-8") as fh:
            page = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        raise FixtureError(f"fixture {fixture_id} unreadable/invalid: {exc}") from exc

    missing = [key for key in REQUIRED_PAGE_FIELDS if key not in page]
    if missing:
        raise FixtureError(f"fixture {fixture_id} missing required fields: {missing}")
    for flag in REQUIRED_BOOLEAN_TRUE:
        if page.get(flag) is not True:
            raise FixtureError(f"fixture {fixture_id} provenance flag {flag} is not literally true")
    html = page.get("html", "")
    if not html.strip().lower().startswith("<!doctype html"):
        raise FixtureError(f"fixture {fixture_id} html is not an HTML document")

    fixture = Fixture(
        id=page["id"],
        file=str(entry["file"]),
        archetype=page["archetype"],
        intended_fields=list(page["intended_fields"]),
        edge_cases=list(page.get("edge_cases", [])),
        label=str(page.get("label", "")),
        synthesis_rationale=str(page.get("synthesis_rationale", "")),
        html=html,
        synthetic=page["synthetic"],
        invented_data=page["invented_data"],
        not_a_live_scrape=page["not_a_live_scrape"],
    )
    # Fixture ids must agree between manifest and page payload.
    if fixture.id != fixture_id:
        raise FixtureError(f"fixture {fixture_id} payload self-identifies as {fixture.id}")
    if fixture.archetype != entry["archetype"]:
        raise FixtureError(f"fixture {fixture_id} archetype mismatch vs manifest")
    if fixture.intended_fields != list(entry["intended_fields"]):
        raise FixtureError(f"fixture {fixture_id} intended_fields mismatch vs manifest")

    logger.info(
        "fixture_loaded id=%s archetype=%s fields=%d",
        fixture.id, fixture.archetype, len(fixture.intended_fields),
    )
    return fixture


def reset_manifest_cache() -> None:
    """Test helper: force a fresh manifest load on next call."""
    global _manifest_cache
    _manifest_cache = None
