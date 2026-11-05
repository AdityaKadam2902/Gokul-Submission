"""Source registry service: CRUD + status management (ARCHITECTURE §6.1).

Responsibility: the single owner of ``sources`` persistence and policy
state. Routers stay thin; every create/update/delete rule lives here.
Registration decisions, fixture identification and status management
follow docs/SOURCE_REGISTER.md and policy_gate semantics.

Deletion (D8): removing a source cascade-removes its snapshots (the only
M1-relvant dependents). The cascade is explicit in the ORM relationship
and is logged here. No other destructive path exists in application code.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.exceptions import SourceNotFound, ValidationError_
from backend.app.core.logging import get_logger, safe_preview
from backend.app.db.orm import Source
from backend.app.models import SourceCreate, SourcePatch
from backend.app.sources import policy_gate
from backend.app.sources.fixture_loader import load_manifest

logger = get_logger(__name__)


def _domain_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        from urllib.parse import urlparse
        return (urlparse(url).hostname or "").lower() or None
    except ValueError:
        return None


def create_source(session: Session, payload: SourceCreate) -> Source:
    policy_gate.validate_registration(payload)

    fixture_id = None
    if payload.kind == "fixture":
        manifest = load_manifest()
        if payload.fixture_id not in manifest["fixtures"]:
            raise ValidationError_(
                f"unknown fixture_id '{payload.fixture_id}'",
                details={"known": sorted(manifest["fixtures"])},
            )
        fixture_id = payload.fixture_id

    source = Source(  # type: ignore[call-arg]
        kind=payload.kind,
        fixture_id=fixture_id,
        url=str(payload.url) if payload.url else None,
        domain=_domain_of(str(payload.url) if payload.url else None),
        page_type=payload.page_type,
        policy_status=payload.policy_status,
        robots_status="unknown",
        intended_fields=list(payload.intended_fields),
        approval_note=payload.approval_note,
        reviewed_by=payload.reviewed_by,
    )
    session.add(source)
    session.flush()
    logger.info(
        "source_registered id=%d kind=%s page_type=%s fixture_id=%s policy=%s note=%s",
        source.id, source.kind, source.page_type, source.fixture_id,
        source.policy_status, safe_preview(payload.approval_note or "", 60),
    )
    return source


def list_sources(session: Session) -> list[Source]:
    return list(session.scalars(select(Source).order_by(Source.id)).all())


def get_source_or_404(session: Session, source_id: int) -> Source:
    source = session.get(Source, source_id)
    if source is None:
        raise SourceNotFound(f"no source with id {source_id}", details={"source_id": source_id})
    return source


def update_source(session: Session, source_id: int, patch: SourcePatch) -> Source:
    """Operator-editable metadata only; policy/robots state is not API-editable."""
    source = get_source_or_404(session, source_id)
    if patch.label is not None:
        source.label = patch.label
    if patch.page_type is not None:
        source.page_type = patch.page_type
    if patch.intended_fields is not None:
        source.intended_fields = list(patch.intended_fields)
    if patch.approval_note is not None:
        source.approval_note = patch.approval_note
    session.flush()
    logger.info("source_updated id=%d fields=%s", source.id, list(patch.model_dump(exclude_unset=True)))
    return source


def delete_source(session: Session, source_id: int) -> int:
    """Delete one source and cascade its snapshots (D8), with an audit log."""
    source = get_source_or_404(session, source_id)
    snapshot_count = len(source.snapshots)
    session.delete(source)  # relationship cascade handles page_snapshots
    session.flush()
    logger.warning(
        "source_deleted id=%d cascaded_snapshots=%d policy_status=%s",
        source_id, snapshot_count, source.policy_status,
    )
    return source_id
