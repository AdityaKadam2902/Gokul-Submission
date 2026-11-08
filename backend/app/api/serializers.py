"""ORM → response-model serializers.

Responsibility: convert ORM rows + DB datetimes into plain dicts that
satisfy the Pydantic response models. Timezone handling is centralised
here so timestamps are always ISO-8601 with an explicit UTC timezone
(ARCHITECTURE §6.3 "timestamps timezone-explicit") even though SQLite
stores naive values.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from backend.app.db.orm import (
    ChangeEvent,
    Discrepancy,
    Evidence,
    Fact,
    Opportunity,
    Organisation,
    OrganisationMatchCandidate,
    OrganisationSourceLink,
    PageSnapshot,
    PromptRun,
    ReviewAction,
    Source,
    SourceObservation,
)


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


def to_organisation_read(org: Organisation) -> dict:
    return {
        "id": org.id,
        "name": org.name,
        "slug": org.slug,
        "primary_domain": org.primary_domain,
        "corporate_parent_id": org.corporate_parent_id,
        "phone": org.phone,
        "address": org.address,
        "suburb": org.suburb,
        "postcode": org.postcode,
        "state": org.state,
        "status": org.status,
        "created_at": _iso(org.created_at) or "",
        "updated_at": _iso(org.updated_at) or "",
    }


def to_organisation_link_read(link: OrganisationSourceLink) -> dict:
    return {
        "id": link.id,
        "organisation_id": link.organisation_id,
        "source_id": link.source_id,
        "relationship_type": link.relationship_type,
        "scope_path": link.scope_path,
        "verification_status": link.verification_status,
        "verified_by": link.verified_by,
        "verified_at": _iso(link.verified_at),
        "created_at": _iso(link.created_at) or "",
    }


def to_match_candidate_read(candidate: OrganisationMatchCandidate) -> dict:
    return {
        "id": candidate.id,
        "source_id": candidate.source_id,
        "suggested_organisation_id": candidate.suggested_organisation_id,
        "match_tier": candidate.match_tier,
        "raw_metric_score": candidate.raw_metric_score,
        "evidence_signals": candidate.evidence_signals or {},
        "rationale": candidate.rationale,
        "status": candidate.status,
        "reviewed_by": candidate.reviewed_by,
        "reviewed_at": _iso(candidate.reviewed_at),
        "created_at": _iso(candidate.created_at) or "",
    }


def to_source_observation_read(obs: SourceObservation) -> dict:
    return {
        "id": obs.id,
        "source_id": obs.source_id,
        "observed_at": _iso(obs.observed_at) or "",
        "outcome": obs.outcome,
        "snapshot_id": obs.snapshot_id,
        "content_hash": obs.content_hash,
        "content_changed": bool(obs.content_changed),
        "parsing_completeness": obs.parsing_completeness,
        "extraction_completeness": obs.extraction_completeness,
        "headings_count": obs.headings_count,
        "character_count": obs.character_count,
        "observation_sequence": obs.observation_sequence,
        "error_details": obs.error_details,
        "created_at": _iso(obs.created_at) or "",
    }


def to_discrepancy_read(d: Discrepancy) -> dict:
    return {
        "id": d.id,
        "organisation_id": d.organisation_id,
        "field_key": d.field_key,
        "scope_key": d.scope_key,
        "fact_a_id": d.fact_a_id,
        "fact_b_id": d.fact_b_id,
        "conflict_type": d.conflict_type,
        "status": d.status,
        "rationale": d.rationale,
        "resolution_notes": d.resolution_notes,
        "resolved_by": d.resolved_by,
        "resolved_at": _iso(d.resolved_at),
        "detected_at": _iso(d.detected_at) or "",
        "created_at": _iso(d.created_at) or "",
    }


def to_discrepancy_detail_read(
    d: Discrepancy,
    fact_a: Fact | None = None,
    fact_b: Fact | None = None,
    source_a: Source | None = None,
    source_b: Source | None = None,
) -> dict:
    base = to_discrepancy_read(d)
    fa = fact_a or d.fact_a
    fb = fact_b or d.fact_b
    sa = source_a or (fa.snapshot.source if fa and fa.snapshot else None)
    sb = source_b or (fb.snapshot.source if fb and fb.snapshot else None)
    base["fact_a"] = to_fact_read(fa) if fa else None
    base["fact_b"] = to_fact_read(fb) if fb else None
    base["source_a"] = to_source_read(sa) if sa else None
    base["source_b"] = to_source_read(sb) if sb else None
    return base


def to_change_event_read(ev: ChangeEvent) -> dict:
    diff_meta = ev.diff_metadata
    if isinstance(diff_meta, str):
        try:
            diff_meta = json.loads(diff_meta)
        except Exception:
            diff_meta = {}
    return {
        "id": ev.id,
        "source_id": ev.source_id,
        "organisation_id": ev.organisation_id,
        "previous_observation_id": ev.previous_observation_id,
        "current_observation_id": ev.current_observation_id,
        "previous_snapshot_id": ev.previous_snapshot_id,
        "current_snapshot_id": ev.current_snapshot_id,
        "previous_fact_id": ev.previous_fact_id,
        "current_fact_id": ev.current_fact_id,
        "event_type": ev.event_type,
        "entity_type": ev.entity_type,
        "field_key": ev.field_key,
        "scope_key": ev.scope_key,
        "old_value": ev.old_value,
        "new_value": ev.new_value,
        "rationale": ev.rationale,
        "diff_metadata": diff_meta or {},
        "idempotency_key": ev.idempotency_key,
        "detected_at": _iso(ev.detected_at) or "",
        "created_at": _iso(ev.created_at) or "",
    }


def to_change_event_detail_read(
    ev: ChangeEvent,
    source: Source | None = None,
    organisation: Organisation | None = None,
    previous_fact: Fact | None = None,
    current_fact: Fact | None = None,
    previous_snapshot: PageSnapshot | None = None,
    current_snapshot: PageSnapshot | None = None,
) -> dict:
    base = to_change_event_read(ev)
    src = source or ev.source
    org = organisation or ev.organisation
    pf = previous_fact or ev.previous_fact
    cf = current_fact or ev.current_fact
    ps = previous_snapshot or ev.previous_snapshot
    cs = current_snapshot or ev.current_snapshot

    base["source"] = to_source_read(src) if src else None
    base["organisation"] = to_organisation_read(org) if org else None
    base["previous_fact"] = to_fact_read(pf) if pf else None
    base["current_fact"] = to_fact_read(cf) if cf else None
    base["previous_snapshot"] = to_snapshot_head(ps) if ps else None
    base["current_snapshot"] = to_snapshot_head(cs) if cs else None
    return base


def to_freshness_policy_read(pol: Any) -> dict:
    return {
        "policy_id": pol.policy_id,
        "version": pol.version,
        "description": pol.description,
        "source_observation_threshold_days": getattr(pol, "source_observation_threshold_days", 30),
        "source_content_threshold_days": getattr(pol, "source_content_threshold_days", 90),
        "category_thresholds_days": pol.category_thresholds_days,
        "aging_threshold_ratio": pol.aging_threshold_ratio,
        "source_observation_overrides": getattr(pol, "source_observation_overrides", {}),
        "source_overrides": pol.source_overrides,
        "threshold_governance_documentation": {
            "source_observation": "Governs expected recency of PracticeSignal crawler observation attempts (default 30d).",
            "source_content": "Governs expected recency of source content changes before review (default 90d).",
            "category:identity": "Governs fact freshness for practice identity fields (name, address, ABN) (default 90d).",
            "category:operational": "Governs fact freshness for operational channels (phone, email) (default 30d).",
            "category:services": "Governs fact freshness for clinical/allied health services and practitioners (default 60d).",
            "category:hours": "Governs fact freshness for opening and operating hours (default 30d).",
            "category:general": "Governs fact freshness for fees, billing, and uncategorized facts (default 60d).",
            "evidence_verification": "Integrity check of verbatim quotation against immutable snapshot; does not decay with age.",
        },
    }


def to_source_observation_freshness_read(f: Any) -> dict:
    return {
        "source_id": f.source_id,
        "last_attempted_at": _iso(f.last_attempted_at),
        "last_successful_observation_at": _iso(f.last_successful_observation_at),
        "last_new_snapshot_at": _iso(f.last_new_snapshot_at),
        "last_unchanged_observation_at": _iso(f.last_unchanged_observation_at),
        "last_complete_parsing_at": _iso(f.last_complete_parsing_at),
        "last_complete_extraction_at": _iso(f.last_complete_extraction_at),
        "observation_age_days": f.observation_age_days,
        "classification": f.classification.value if hasattr(f.classification, "value") else str(f.classification),
        "policy_id": f.policy_id,
        "threshold_days": f.threshold_days,
        "threshold_governance": getattr(f, "threshold_governance", "source_observation"),
        "latest_attempt_status": f.latest_attempt_status,
        "latest_attempt_failed": f.latest_attempt_failed,
        "as_of_reference_time": _iso(f.as_of_reference_time) or "",
        "rationale": f.rationale,
    }


def to_source_content_freshness_read(f: Any) -> dict:
    return {
        "source_id": f.source_id,
        "current_version_first_observed_at": _iso(getattr(f, "current_version_first_observed_at", None)),
        "previous_version_observed_at": _iso(getattr(f, "previous_version_observed_at", None)),
        "previous_version_content_hash": getattr(f, "previous_version_content_hash", None),
        "content_observation_age_days": getattr(f, "content_observation_age_days", None),
        "actual_publisher_modified_at": _iso(getattr(f, "actual_publisher_modified_at", None)),
        "last_content_change_at": _iso(f.last_content_change_at),
        "content_age_days": f.content_age_days,
        "latest_content_hash": f.latest_content_hash,
        "snapshot_count": f.snapshot_count,
        "unchanged_reobservations_count": f.unchanged_reobservations_count,
        "policy_id": getattr(f, "policy_id", "ps_freshness_v1"),
        "threshold_days": getattr(f, "threshold_days", 90),
        "threshold_governance": getattr(f, "threshold_governance", "source_content"),
        "as_of_reference_time": _iso(f.as_of_reference_time) or "",
        "rationale": f.rationale,
    }


def to_evidence_verification_freshness_read(f: Any) -> dict:
    return {
        "evidence_id": f.evidence_id,
        "snapshot_id": f.snapshot_id,
        "captured_at": _iso(f.captured_at),
        "checked_at": _iso(getattr(f, "checked_at", None)),
        "evaluated_at": _iso(getattr(f, "evaluated_at", None)),
        "is_verified": f.is_verified,
        "quote_length": f.quote_length,
        "policy_id": getattr(f, "policy_id", "ps_freshness_v1"),
        "threshold_governance": getattr(f, "threshold_governance", "immutable_snapshot_integrity"),
        "as_of_reference_time": _iso(f.as_of_reference_time) or "",
        "rationale": f.rationale,
    }


def to_fact_freshness_read(f: Any) -> dict:
    return {
        "fact_id": f.fact_id,
        "source_id": f.source_id,
        "organisation_id": f.organisation_id,
        "field_key": f.field_key,
        "information_category": f.information_category.value if hasattr(f.information_category, "value") else str(f.information_category),
        "raw_value": f.raw_value,
        "observation_freshness": to_source_observation_freshness_read(f.observation_freshness),
        "content_freshness": to_source_content_freshness_read(f.content_freshness),
        "evidence_freshness": to_evidence_verification_freshness_read(f.evidence_freshness),
        "has_explicit_temporal_evidence": f.has_explicit_temporal_evidence,
        "explicit_fact_date": _iso(f.explicit_fact_date),
        "fact_date_type": f.fact_date_type,
        "temporal_evidence_quote": f.temporal_evidence_quote,
        "date_precision": f.date_precision.value if hasattr(f.date_precision, "value") else (str(f.date_precision) if f.date_precision else None),
        "date_range_start": _iso(getattr(f, "date_range_start", None)),
        "date_range_end": _iso(getattr(f, "date_range_end", None)),
        "min_fact_age_days": getattr(f, "min_fact_age_days", None),
        "max_fact_age_days": getattr(f, "max_fact_age_days", None),
        "fact_age_days": f.fact_age_days,
        "fact_freshness_classification": f.fact_freshness_classification.value if hasattr(f.fact_freshness_classification, "value") else str(f.fact_freshness_classification),
        "overall_classification": f.overall_classification.value if hasattr(f.overall_classification, "value") else str(f.overall_classification),
        "policy_id": f.policy_id,
        "policy_threshold_days": f.policy_threshold_days,
        "threshold_governance": getattr(f, "threshold_governance", "fact_category"),
        "as_of_reference_time": _iso(f.as_of_reference_time) or "",
        "rationale": f.rationale,
    }


def to_organisation_freshness_summary_read(s: Any) -> dict:
    return {
        "organisation_id": s.organisation_id,
        "organisation_name": s.organisation_name,
        "confirmed_source_count": s.confirmed_source_count,
        "total_facts_count": s.total_facts_count,
        "fresh_facts_count": s.fresh_facts_count,
        "aging_facts_count": s.aging_facts_count,
        "stale_facts_count": s.stale_facts_count,
        "unknown_facts_count": s.unknown_facts_count,
        "unobservable_sources_count": s.unobservable_sources_count,
        "sources": [to_source_observation_freshness_read(src) for src in s.sources],
        "as_of_reference_time": _iso(s.as_of_reference_time) or "",
        "policy_id": s.policy_id,
        "summary_rationale": s.summary_rationale,
    }


def to_opportunity_read(opp: Opportunity) -> dict:
    """Format an Opportunity ORM instance for the API."""
    return {
        "id": opp.id,
        "organisation_id": opp.organisation_id,
        "source_id": opp.source_id,
        "first_snapshot_id": opp.first_snapshot_id,
        "last_snapshot_id": opp.last_snapshot_id,
        "role_category": opp.role_category,
        "role_title": opp.role_title,
        "verbatim_wording": opp.verbatim_wording,
        "contact_channel": opp.contact_channel,
        "explicit_ad_id": opp.explicit_ad_id,
        "status": opp.status,
        "closure_evidence_quote": opp.closure_evidence_quote,
        "consecutive_absences": opp.consecutive_absences,
        "first_seen_at": _iso(opp.first_seen_at),
        "last_seen_at": _iso(opp.last_seen_at),
        "closed_at": _iso(opp.closed_at),
        "created_at": _iso(opp.created_at),
        "updated_at": _iso(opp.updated_at),
    }


def to_opportunity_detail_read(
    opp: Opportunity,
    organisation_name: str | None = None,
    source_url: str | None = None,
) -> dict:
    """Format an Opportunity with expanded organisation and source details."""
    payload = to_opportunity_read(opp)
    payload["organisation_name"] = organisation_name or (opp.organisation.name if opp.organisation else None)
    payload["source_url"] = source_url or (opp.source.url if opp.source else None)
    return payload
def to_review_action_read(action: ReviewAction) -> dict:
    """Format a ReviewAction ORM instance for the API."""
    meta = action.decision_metadata
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    return {
        "id": action.id,
        "entity_type": action.entity_type,
        "entity_id": action.entity_id,
        "action": action.action,
        "previous_status": action.previous_status,
        "new_status": action.new_status,
        "reason_code": action.reason_code,
        "reviewer": action.reviewer,
        "notes": action.notes,
        "evidence_id": action.evidence_id,
        "decision_metadata": meta or {},
        "created_at": _iso(action.created_at) or "",
    }
