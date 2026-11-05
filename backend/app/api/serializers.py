"""ORM → response-model serializers.

Responsibility: convert ORM rows + DB datetimes into plain dicts that
satisfy the Pydantic response models. Timezone handling is centralised
here so timestamps are always ISO-8601 with an explicit UTC timezone
(ARCHITECTURE §6.3 "timestamps timezone-explicit") even though SQLite
stores naive values.
"""

from __future__ import annotations

from datetime import UTC, datetime

from backend.app.db.orm import Evidence, Fact, PageSnapshot, PromptRun, Source


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat(timespec="milliseconds")


def to_snapshot_head(snapshot: PageSnapshot) -> dict:
    return {
        "id": snapshot.id,
        "source_id": snapshot.source_id,
        "fetched_at": _iso(snapshot.fetched_at) or "",
        "title": snapshot.title,
        "content_hash": snapshot.content_hash,
        "parser_version": snapshot.parser_version,
        "fixture": bool(snapshot.fixture),
    }


def to_source_read(source: Source) -> dict:
    latest = source.snapshots[0] if source.snapshots else None
    return {
        "id": source.id,
        "kind": source.kind,
        "label": source.label,
        "url": source.url,
        "domain": source.domain,
        "fixture_id": source.fixture_id,
        "page_type": source.page_type,
        "policy_status": source.policy_status,
        "robots_status": source.robots_status,
        "intended_fields": list(source.intended_fields or []),
        "approval_note": source.approval_note,
        "reviewed_by": source.reviewed_by,
        "created_at": _iso(source.created_at) or "",
        "updated_at": _iso(source.updated_at) or "",
        "last_snapshot_id": latest.id if latest else None,
        "last_snapshot_at": _iso(latest.fetched_at) if latest else None,
        "last_snapshot_hash": latest.content_hash if latest else None,
    }


def to_snapshot_read(snapshot: PageSnapshot, source: Source) -> dict:
    return {
        "id": snapshot.id,
        "source_id": snapshot.source_id,
        "fetched_at": _iso(snapshot.fetched_at) or "",
        "title": snapshot.title,
        "text": snapshot.text,
        "content_hash": snapshot.content_hash,
        "parser_version": snapshot.parser_version,
        "fixture": bool(snapshot.fixture),
        "headings": [
            {
                "level": int(h.get("level", 0)),
                "text": str(h.get("text", "")),
                "start_offset": int(h.get("start_offset", 0)),
                "end_offset": int(h.get("end_offset", 0)),
            }
            for h in (snapshot.headings or [])
        ],
        "source_label": source.label,
        "source_fixture": source.kind == "fixture",
    }


def to_fact_read(fact: Fact) -> dict:
    ev = fact.evidence
    return {
        "id": fact.id,
        "snapshot_id": fact.snapshot_id,
        "prompt_run_id": fact.prompt_run_id,
        "organisation_id": fact.organisation_id,
        "field_key": fact.field_key,
        "scope_key": fact.scope_key,
        "raw_value": fact.raw_value,
        "normalized_value": fact.normalized_value,
        "timezone": fact.timezone,
        "evidence_id": fact.evidence_id,
        "evidence_quote": ev.quote if ev else None,
        "evidence_start_offset": ev.start_offset if ev else None,
        "evidence_end_offset": ev.end_offset if ev else None,
        "model_tag": fact.model_tag,
        "prompt_version": fact.prompt_version,
        "via": fact.via,
        "status": fact.status,
        "needs_review_reason": fact.needs_review_reason,
        "created_at": _iso(fact.created_at) or "",
    }


def to_evidence_read(evidence: Evidence) -> dict:
    return {
        "id": evidence.id,
        "snapshot_id": evidence.snapshot_id,
        "start_offset": evidence.start_offset,
        "end_offset": evidence.end_offset,
        "quote": evidence.quote,
        "captured_at": _iso(evidence.captured_at) or "",
        "match_algorithm_version": evidence.match_algorithm_version,
    }


def to_prompt_run_read(run: PromptRun) -> dict:
    return {
        "id": run.id,
        "snapshot_id": run.snapshot_id,
        "model_tag": run.model_tag,
        "prompt_version": run.prompt_version,
        "request_payload_hash": run.request_payload_hash,
        "raw_response_ref": run.raw_response_ref,
        "parse_ok": bool(run.parse_ok),
        "validation_errors": list(run.validation_errors or []),
        "latency_ms": float(run.latency_ms),
        "prompt_eval_count": int(run.prompt_eval_count),
        "eval_count": int(run.eval_count),
        "tokens_per_second": float(run.tokens_per_second),
        "evidence_checks": dict(run.evidence_checks or {}),
        "created_at": _iso(run.created_at) or "",
    }

