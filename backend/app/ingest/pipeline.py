"""Ingestion pipeline: the M1 vertical slice runtime.

Responsibility: run one ingestion for a registered source — policy gate
→ fixture load (fixture path only in M1) → parse → hash/persist →
report outcome. This module is the fence between the API and the
pipeline stages; M2 will insert the AI extraction stage between parse
and persistence behind the same contract.

Guarantees:
- ``policy_gate.validate_ingestion_ready`` runs FIRST, before the fixture
  loader or parser is even touched — rejections cannot reach the network
  because no outbound call exists anywhere downstream.
- Fixture provenance is preserved: only sources bound to a fixture id are
  ingested in M1, and the snapshot row records ``fixture=True``.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.app.core.exceptions import LiveFetchNotAuthorised, ValidationError_
from backend.app.core.logging import get_logger, safe_preview
from backend.app.db.orm import Source
from backend.app.ingest import snapshot_store
from backend.app.parsers import text_extract
from backend.app.sources import policy_gate
from backend.app.sources.fixture_loader import load_fixture

logger = get_logger(__name__)


def run_ingestion(session: Session, source: Source) -> snapshot_store.IngestOutcome:
    # 1. Policy gate first — before any loader/parser/fetch could run.
    policy_gate.validate_ingestion_ready(source)

    if source.kind == "url":  # unreachable in M1 but kept explicit for the gate contract
        raise LiveFetchNotAuthorised(
            "live fetch stage does not exist in M1; register a fixture source instead",
            details={"source_id": source.id},
        )

    # 2. Load the bound synthetic fixture (local disk; never the network).
    fixture = load_fixture(source.fixture_id or "")
    if source.page_type != "other" and fixture.archetype != source.page_type:
        raise ValidationError_(
            f"page_type '{source.page_type}' disagrees with fixture archetype "
            f"'{fixture.archetype}' for source {source.id}",
            details={"source_id": source.id, "fixture_id": fixture.id},
        )

    # 3. Parse to structured text (headings + exact offsets).
    parsed = text_extract.parse(fixture.html)
    logger.info(
        "parsed source_id=%d fixture=%s title=%s headings=%d chars=%d",
        source.id, fixture.id, safe_preview(parsed.title or "", 60),
        len(parsed.headings), len(parsed.text),
    )

    # 4. Hash + persist (short-circuit if unchanged).
    snapshot, outcome = snapshot_store.persist_snapshot(session, source, parsed)
    return outcome
