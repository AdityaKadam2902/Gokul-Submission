"""Human Review, Evidence Verification & Auditable Decision Workflow Service (D44).

Responsibilities:
    - Retrieve reviewable queue items across all 7 intelligence domains:
        1. Organisation match candidates (D39).
        2. Fact ownership ambiguity (D40).
        3. Extracted candidate facts & evidence (D10, D11).
        4. Cross-source factual discrepancies (D41).
        5. Temporal change events (D42).
        6. Healthcare workforce opportunities (D43).
        7. Information freshness evaluations (D46).
    - Validate allowed review transitions and reviewer identity (local-first).
    - Enforce domain boundaries:
        - Never automatically merge organisations.
        - Never silently overwrite underlying facts during discrepancy resolution.
        - Never infer vacancy status 'filled' or 'hired'.
        - Never mutate historical change events or alter raw evidence timestamps.
    - Write immutable, append-only audit records to `review_actions` table.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, desc, func, select
from sqlalchemy.orm import Session

from backend.app.core.logging import get_logger
from backend.app.db.orm import (
    ChangeEvent as ChangeEventORM,
    Discrepancy,
    Evidence,
    Fact,
    Opportunity as OpportunityORM,
    Organisation,
    OrganisationMatchCandidate,
    OrganisationSourceLink,
    PageSnapshot,
    PracticeLifecycleEvent as PracticeLifecycleEventORM,
    ReviewAction as ReviewActionORM,
    Source,
)
from backend.app.intelligence.schemas import (
    OpportunityStatus,
    ReviewActionType,
    ReviewStatus,
)

logger = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# =============================================================================
# ALLOWED TRANSITIONS & ACTIONS
# =============================================================================

ALLOWED_ACTIONS_BY_ENTITY: dict[str, list[str]] = {
    "match_candidate": [
        ReviewActionType.APPROVED.value,
        ReviewActionType.ACCEPTED.value,
        ReviewActionType.REJECTED.value,
        ReviewActionType.DEFERRED.value,
        ReviewActionType.REOPENED.value,
    ],
    "fact_ownership": [
        ReviewActionType.ASSIGNED_ORG.value,
        ReviewActionType.DISMISSED_BENIGN.value,
        ReviewActionType.REJECTED.value,
        ReviewActionType.DEFERRED.value,
    ],
    "fact": [
        ReviewActionType.APPROVED.value,
        ReviewActionType.ACCEPTED.value,
        ReviewActionType.REJECTED.value,
        ReviewActionType.DEFERRED.value,
        ReviewActionType.REOPENED.value,
    ],
    "discrepancy": [
        ReviewActionType.UNDER_REVIEW.value,
        ReviewActionType.RESOLVED_A.value,
        ReviewActionType.RESOLVED_B.value,
        ReviewActionType.RESOLVED.value,
        ReviewActionType.DISMISSED_BENIGN.value,
        ReviewActionType.DISMISSED.value,
        ReviewActionType.DEFERRED.value,
        ReviewActionType.REOPENED.value,
    ],
    "change_event": [
        ReviewActionType.ACKNOWLEDGED.value,
        ReviewActionType.DEFERRED.value,
    ],
    "opportunity": [
        ReviewActionType.ACKNOWLEDGED.value,
        ReviewActionType.APPROVED.value,
        ReviewActionType.ACCEPTED.value,
        ReviewActionType.DEFERRED.value,
    ],
    "freshness": [
        ReviewActionType.ACKNOWLEDGED.value,
        ReviewActionType.DEFERRED.value,
    ],
    "lifecycle_event": [
        ReviewActionType.APPROVED.value,
        ReviewActionType.ACCEPTED.value,
        ReviewActionType.REJECTED.value,
        ReviewActionType.DEFERRED.value,
        ReviewActionType.REOPENED.value,
        ReviewActionType.ACKNOWLEDGED.value,
    ],
}


# =============================================================================
# CENTRAL REVIEW QUEUE
# =============================================================================

def get_review_queue(
    session: Session,
    entity_type: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Retrieve unified review queue across intelligence domains with grounded prioritization."""
    items: list[dict[str, Any]] = []

    # 1. Organisation Match Candidates
    if entity_type is None or entity_type == "match_candidate":
        q = select(OrganisationMatchCandidate).order_by(OrganisationMatchCandidate.id.desc())
        if status == "pending" or status is None:
            q = q.where(OrganisationMatchCandidate.status.in_(["suggested", "pending"]))
        elif status == "deferred":
            q = q.where(OrganisationMatchCandidate.status == "deferred")
        elif status == "completed":
            q = q.where(OrganisationMatchCandidate.status.in_(["verified", "rejected"]))
        elif status != "all":
            q = q.where(OrganisationMatchCandidate.status == status)

        for cand in session.execute(q).scalars().all():
            org = cand.suggested_organisation
            src = cand.source
            items.append({
                "queue_id": f"match_candidate:{cand.id}",
                "entity_type": "match_candidate",
                "entity_id": cand.id,
                "title": f"Match Suggestion: {src.label or src.url} -> {org.name if org else 'Unknown'}",
                "summary": f"Tier: {cand.match_tier}. Domain: {src.domain or src.url}.",
                "flagged_reason": cand.rationale or f"Automated entity match tier {cand.match_tier}",
                "current_status": cand.status,
                "review_status": "pending" if cand.status in ("suggested", "pending") else cand.status,
                "priority": "high" if cand.match_tier == "high_confidence" else "medium",
                "organisation_id": cand.suggested_organisation_id,
                "organisation_name": org.name if org else None,
                "source_id": cand.source_id,
                "source_url": src.url if src else None,
                "evidence_quote": None,
                "created_at": cand.created_at.isoformat() if cand.created_at else _utcnow().isoformat(),
            })

    # 2. Fact Ownership Ambiguity
    if entity_type is None or entity_type == "fact_ownership":
        q = select(Fact).where(
            Fact.organisation_id.is_(None),
            Fact.needs_review_reason.isnot(None),
        ).order_by(Fact.id.desc())

        if status == "pending" or status is None:
            q = q.where(Fact.status.in_(["candidate", "needs_review", "pending"]))
        elif status == "deferred":
            q = q.where(Fact.status == "deferred")
        elif status == "completed":
            q = q.where(Fact.status.in_(["verified", "rejected", "dismissed"]))
        elif status != "all":
            q = q.where(Fact.status == status)

        for f in session.execute(q).scalars().all():
            snap = f.snapshot
            src = snap.source if snap else None
            ev = f.evidence
            items.append({
                "queue_id": f"fact_ownership:{f.id}",
                "entity_type": "fact_ownership",
                "entity_id": f.id,
                "title": f"Unassigned Fact: {f.field_key} ({f.scope_key or 'corporate'})",
                "summary": f"Value: '{f.raw_value}'. Unresolved corporate scope.",
                "flagged_reason": f.needs_review_reason or "Ambiguous heading scope / multi-location ownership",
                "current_status": f.status,
                "review_status": "pending" if f.status in ("candidate", "needs_review") else f.status,
                "priority": "high",
                "organisation_id": None,
                "organisation_name": None,
                "source_id": src.id if src else None,
                "source_url": src.url if src else None,
                "evidence_quote": ev.quote if ev else None,
                "created_at": snap.fetched_at.isoformat() if snap and snap.fetched_at else _utcnow().isoformat(),
            })

    # 3. Extracted Candidate Facts
    if entity_type is None or entity_type == "fact":
        q = select(Fact).where(
            Fact.organisation_id.isnot(None),
            Fact.status.in_(["candidate", "needs_review"]),
        ).order_by(Fact.id.desc())

        if status == "pending" or status is None:
            q = q.where(Fact.status.in_(["candidate", "needs_review"]))
        elif status == "deferred":
            q = q.where(Fact.status == "deferred")
        elif status == "completed":
            q = q.where(Fact.status.in_(["verified", "rejected"]))
        elif status != "all":
            q = q.where(Fact.status == status)

        for f in session.execute(q).scalars().all():
            snap = f.snapshot
            src = snap.source if snap else None
            ev = f.evidence
            org = session.get(Organisation, f.organisation_id) if f.organisation_id else None
            items.append({
                "queue_id": f"fact:{f.id}",
                "entity_type": "fact",
                "entity_id": f.id,
                "title": f"Candidate Fact: {f.field_key} = '{f.raw_value}'",
                "summary": f"Field '{f.field_key}', Scope '{f.scope_key or 'default'}'.",
                "flagged_reason": "AI extraction pending human verification",
                "current_status": f.status,
                "review_status": "pending" if f.status in ("candidate", "needs_review") else f.status,
                "priority": "medium",
                "organisation_id": f.organisation_id,
                "organisation_name": org.name if org else None,
                "source_id": src.id if src else None,
                "source_url": src.url if src else None,
                "evidence_quote": ev.quote if ev else None,
                "created_at": snap.fetched_at.isoformat() if snap and snap.fetched_at else _utcnow().isoformat(),
            })

    # 4. Cross-Source Discrepancies
    if entity_type is None or entity_type == "discrepancy":
        q = select(Discrepancy).order_by(Discrepancy.id.desc())
        if status == "pending" or status is None:
            q = q.where(Discrepancy.status.in_(["detected", "under_review", "needs_investigation"]))
        elif status == "deferred":
            q = q.where(Discrepancy.status.in_(["needs_investigation", "deferred"]))
        elif status == "completed":
            q = q.where(Discrepancy.status.in_(["resolved_fact_a", "resolved_fact_b", "resolved", "dismissed_benign", "dismissed"]))
        elif status != "all":
            q = q.where(Discrepancy.status == status)

        for d in session.execute(q).scalars().all():
            org = d.organisation
            items.append({
                "queue_id": f"discrepancy:{d.id}",
                "entity_type": "discrepancy",
                "entity_id": d.id,
                "title": f"Factual Discrepancy: {d.field_key} ({d.conflict_type})",
                "summary": f"Disagreement on '{d.field_key}' ({d.scope_key}) between Fact #{d.fact_a_id} and #{d.fact_b_id}.",
                "flagged_reason": d.rationale or f"Factual divergence: {d.conflict_type}",
                "current_status": d.status,
                "review_status": "under_review" if d.status == "under_review" else ("deferred" if d.status in ("needs_investigation", "deferred") else ("pending" if d.status == "detected" else "resolved")),
                "priority": "high",
                "organisation_id": d.organisation_id,
                "organisation_name": org.name if org else None,
                "source_id": None,
                "source_url": None,
                "evidence_quote": None,
                "created_at": d.created_at.isoformat() if d.created_at else _utcnow().isoformat(),
            })

    # 5. Healthcare Opportunities
    if entity_type is None or entity_type == "opportunity":
        q = select(OpportunityORM).where(
            OpportunityORM.status.in_([
                OpportunityStatus.CHANGED.value,
                OpportunityStatus.POSSIBLY_STALE.value,
                OpportunityStatus.UNCONFIRMED_PRIOR.value,
                OpportunityStatus.NO_LONGER_OBSERVED.value,
                OpportunityStatus.EXPLICITLY_CLOSED.value,
            ])
        ).order_by(OpportunityORM.id.desc())

        if status == "pending" or status is None:
            pass  # return in-flight/changed
        elif status == "completed":
            q = q.where(OpportunityORM.id.in_(
                select(ReviewActionORM.entity_id).where(ReviewActionORM.entity_type == "opportunity")
            ))
        elif status != "all":
            q = q.where(OpportunityORM.status == status)

        for opp in session.execute(q).scalars().all():
            org = opp.organisation
            src = opp.source
            # Check if recently reviewed
            rev_action = session.execute(
                select(ReviewActionORM)
                .where(ReviewActionORM.entity_type == "opportunity", ReviewActionORM.entity_id == opp.id)
                .order_by(ReviewActionORM.id.desc())
            ).scalars().first()

            items.append({
                "queue_id": f"opportunity:{opp.id}",
                "entity_type": "opportunity",
                "entity_id": opp.id,
                "title": f"Vacancy Lifecycle: {opp.role_title} ({opp.status})",
                "summary": f"Ref: {opp.explicit_ad_id or 'None'}. Category: {opp.role_category}.",
                "flagged_reason": f"Status '{opp.status}': " + (opp.closure_evidence_quote or f"Absences: {opp.consecutive_absences}"),
                "current_status": opp.status,
                "review_status": "accepted" if rev_action else "pending",
                "priority": "high" if opp.status in (OpportunityStatus.EXPLICITLY_CLOSED.value, OpportunityStatus.POSSIBLY_STALE.value) else "medium",
                "organisation_id": opp.organisation_id,
                "organisation_name": org.name if org else None,
                "source_id": opp.source_id,
                "source_url": src.url if src else None,
                "evidence_quote": opp.closure_evidence_quote or opp.verbatim_wording[:120],
                "created_at": opp.created_at.isoformat() if opp.created_at else _utcnow().isoformat(),
            })

    # 6. Practice Lifecycle Events (Closure, Relocation, Acquisition)
    if entity_type is None or entity_type in ("lifecycle_event", "practice_lifecycle"):
        q = select(PracticeLifecycleEventORM).order_by(PracticeLifecycleEventORM.id.desc())
        if status == "pending" or status is None:
            q = q.where(PracticeLifecycleEventORM.review_status.in_(["pending", "under_review"]))
        elif status == "deferred":
            q = q.where(PracticeLifecycleEventORM.review_status == "deferred")
        elif status == "completed":
            q = q.where(PracticeLifecycleEventORM.review_status.in_(["accepted", "approved", "rejected"]))
        elif status != "all":
            q = q.where(PracticeLifecycleEventORM.review_status == status)

        for evt in session.execute(q).scalars().all():
            org = evt.organisation
            src = evt.source
            title_prefix = {
                "permanent_closure": "Permanent Closure",
                "temporary_closure": "Temporary Closure",
                "relocation": "Relocation",
                "acquisition": "Corporate Acquisition",
            }.get(evt.event_type, evt.event_type.replace("_", " ").title())
            scope_info = f" ({evt.branch_locality} branch)" if evt.is_branch_level and evt.branch_locality else ""
            items.append({
                "queue_id": f"lifecycle_event:{evt.id}",
                "entity_type": "lifecycle_event",
                "entity_id": evt.id,
                "title": f"Lifecycle: {title_prefix}{scope_info} - {evt.status}",
                "summary": f"Type: {evt.event_type}. Status: {evt.status}. Effective: {evt.effective_date or 'Undated'}.",
                "flagged_reason": evt.rationale,
                "current_status": evt.status,
                "review_status": evt.review_status,
                "priority": "high" if evt.status == "confirmed_explicit" or evt.event_type == "permanent_closure" else "medium",
                "organisation_id": evt.organisation_id,
                "organisation_name": org.name if org else None,
                "source_id": evt.source_id,
                "source_url": src.url if src else None,
                "evidence_quote": evt.evidence_quote,
                "created_at": evt.created_at.isoformat() if evt.created_at else _utcnow().isoformat(),
            })

    # Sort items by priority (high first) then creation date descending
    priority_order = {"high": 0, "medium": 1, "low": 2}
    items.sort(key=lambda x: (priority_order.get(x["priority"], 9), x["created_at"]), reverse=False)

    total = len(items)
    paginated = items[offset : offset + limit]

    return {
        "items": paginated,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


# =============================================================================
# REVIEW METRICS
# =============================================================================

def get_review_metrics(session: Session) -> dict[str, Any]:
    """Calculate pending, under_review, completed, deferred counts across entities."""
    # Count match candidates
    pending_matches = session.execute(
        select(func.count(OrganisationMatchCandidate.id)).where(OrganisationMatchCandidate.status.in_(["suggested", "pending"]))
    ).scalar_one() or 0

    # Count unassigned fact ownership
    pending_ownership = session.execute(
        select(func.count(Fact.id)).where(
            Fact.organisation_id.is_(None),
            Fact.needs_review_reason.isnot(None),
            Fact.status.in_(["candidate", "needs_review", "pending"]),
        )
    ).scalar_one() or 0

    # Count candidate facts
    pending_facts = session.execute(
        select(func.count(Fact.id)).where(
            Fact.organisation_id.isnot(None),
            Fact.status.in_(["candidate", "needs_review"]),
        )
    ).scalar_one() or 0

    # Count discrepancies
    pending_discrepancies = session.execute(
        select(func.count(Discrepancy.id)).where(Discrepancy.status.in_(["detected", "under_review", "needs_investigation"]))
    ).scalar_one() or 0

    # Count in-flight opportunities
    pending_opps = session.execute(
        select(func.count(OpportunityORM.id)).where(
            OpportunityORM.status.in_([
                OpportunityStatus.CHANGED.value,
                OpportunityStatus.POSSIBLY_STALE.value,
                OpportunityStatus.UNCONFIRMED_PRIOR.value,
                OpportunityStatus.NO_LONGER_OBSERVED.value,
                OpportunityStatus.EXPLICITLY_CLOSED.value,
            ])
        )
    ).scalar_one() or 0

    # Count pending practice lifecycle events
    pending_lifecycle = session.execute(
        select(func.count(PracticeLifecycleEventORM.id)).where(
            PracticeLifecycleEventORM.review_status.in_(["pending", "under_review"])
        )
    ).scalar_one() or 0

    # Review actions count (completed decisions)
    completed_actions = session.execute(
        select(func.count(ReviewActionORM.id))
    ).scalar_one() or 0

    # Deferred count
    deferred_matches = session.execute(
        select(func.count(OrganisationMatchCandidate.id)).where(OrganisationMatchCandidate.status == "deferred")
    ).scalar_one() or 0
    deferred_disc = session.execute(
        select(func.count(Discrepancy.id)).where(Discrepancy.status.in_(["needs_investigation", "deferred"]))
    ).scalar_one() or 0
    deferred_lifecycle = session.execute(
        select(func.count(PracticeLifecycleEventORM.id)).where(PracticeLifecycleEventORM.review_status == "deferred")
    ).scalar_one() or 0
    total_deferred = deferred_matches + deferred_disc + deferred_lifecycle

    total_pending = pending_matches + pending_ownership + pending_facts + pending_discrepancies + pending_opps + pending_lifecycle

    return {
        "total_pending": total_pending,
        "total_under_review": session.execute(select(func.count(Discrepancy.id)).where(Discrepancy.status == "under_review")).scalar_one() or 0,
        "total_completed": completed_actions,
        "total_deferred": total_deferred,
        "by_entity_type": {
            "match_candidate": pending_matches,
            "fact_ownership": pending_ownership,
            "fact": pending_facts,
            "discrepancy": pending_discrepancies,
            "opportunity": pending_opps,
            "lifecycle_event": pending_lifecycle,
        },
    }


# =============================================================================
# REVIEW ITEM DETAIL
# =============================================================================

def get_review_item_detail(session: Session, entity_type: str, entity_id: int) -> dict[str, Any]:
    """Retrieve full context, supporting quotations, snapshots, and review history for an entity."""
    history_orms = session.execute(
        select(ReviewActionORM)
        .where(ReviewActionORM.entity_type == entity_type, ReviewActionORM.entity_id == entity_id)
        .order_by(ReviewActionORM.created_at.desc())
    ).scalars().all()

    history = [
        {
            "id": h.id,
            "entity_type": h.entity_type,
            "entity_id": h.entity_id,
            "action": h.action,
            "previous_status": h.previous_status,
            "new_status": h.new_status,
            "reason_code": h.reason_code,
            "reviewer": h.reviewer,
            "notes": h.notes,
            "evidence_id": h.evidence_id,
            "decision_metadata": h.decision_metadata if isinstance(h.decision_metadata, dict) else (json.loads(h.decision_metadata) if h.decision_metadata else {}),
            "created_at": h.created_at.isoformat() if h.created_at else _utcnow().isoformat(),
        }
        for h in history_orms
    ]

    allowed_actions = ALLOWED_ACTIONS_BY_ENTITY.get(entity_type, [])

    if entity_type == "match_candidate":
        cand = session.get(OrganisationMatchCandidate, entity_id)
        if not cand:
            raise ValueError(f"OrganisationMatchCandidate id={entity_id} not found")

        org = cand.suggested_organisation
        src = cand.source
        return {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "title": f"Match Candidate: {src.label or src.url} -> {org.name if org else 'Unknown'}",
            "summary": f"Match tier '{cand.match_tier}' with raw metric score {cand.raw_metric_score}.",
            "flagged_reason": cand.rationale,
            "current_status": cand.status,
            "review_status": "pending" if cand.status in ("suggested", "pending") else cand.status,
            "priority": "high" if cand.match_tier == "high_confidence" else "medium",
            "organisation_id": cand.suggested_organisation_id,
            "organisation_name": org.name if org else None,
            "source_id": cand.source_id,
            "source_url": src.url if src else None,
            "snapshot_id": None,
            "snapshot_title": None,
            "snapshot_fetched_at": None,
            "evidence_id": None,
            "evidence_quote": None,
            "evidence_start_offset": None,
            "evidence_end_offset": None,
            "entity_data": {
                "match_tier": cand.match_tier,
                "raw_metric_score": cand.raw_metric_score,
                "evidence_signals": cand.evidence_signals,
                "suggested_org": {
                    "id": org.id,
                    "name": org.name,
                    "slug": org.slug,
                    "primary_domain": org.primary_domain,
                    "phone": org.phone,
                    "postcode": org.postcode,
                    "state": org.state,
                } if org else None,
            },
            "discrepancy_context": None,
            "opportunity_context": None,
            "change_context": None,
            "allowed_actions": allowed_actions,
            "review_history": history,
        }

    elif entity_type in ("fact", "fact_ownership"):
        fact = session.get(Fact, entity_id)
        if not fact:
            raise ValueError(f"Fact id={entity_id} not found")

        snap = fact.snapshot
        src = snap.source if snap else None
        ev = fact.evidence
        org = session.get(Organisation, fact.organisation_id) if fact.organisation_id else None

        # If fact_ownership, list candidate organisations for dropdown
        candidate_orgs = []
        if entity_type == "fact_ownership":
            orgs = session.execute(select(Organisation).limit(20)).scalars().all()
            candidate_orgs = [{"id": o.id, "name": o.name, "slug": o.slug} for o in orgs]

        return {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "title": f"Fact: {fact.field_key} = '{fact.raw_value}'",
            "summary": f"Field key '{fact.field_key}', scope key '{fact.scope_key or 'default'}'.",
            "flagged_reason": fact.needs_review_reason or "Candidate fact pending human verification",
            "current_status": fact.status,
            "review_status": "pending" if fact.status in ("candidate", "needs_review") else fact.status,
            "priority": "high" if entity_type == "fact_ownership" else "medium",
            "organisation_id": fact.organisation_id,
            "organisation_name": org.name if org else None,
            "source_id": src.id if src else None,
            "source_url": src.url if src else None,
            "snapshot_id": snap.id if snap else None,
            "snapshot_title": snap.title if snap else None,
            "snapshot_fetched_at": snap.fetched_at.isoformat() if snap and snap.fetched_at else None,
            "evidence_id": ev.id if ev else None,
            "evidence_quote": ev.quote if ev else None,
            "evidence_start_offset": ev.start_offset if ev else None,
            "evidence_end_offset": ev.end_offset if ev else None,
            "entity_data": {
                "field_key": fact.field_key,
                "scope_key": fact.scope_key,
                "raw_value": fact.raw_value,
                "normalized_value": fact.normalized_value,
                "timezone": fact.timezone,
                "via": fact.via,
                "needs_review_reason": fact.needs_review_reason,
                "candidate_organisations": candidate_orgs,
            },
            "discrepancy_context": None,
            "opportunity_context": None,
            "change_context": None,
            "allowed_actions": allowed_actions,
            "review_history": history,
        }

    elif entity_type == "discrepancy":
        disc = session.get(Discrepancy, entity_id)
        if not disc:
            raise ValueError(f"Discrepancy id={entity_id} not found")

        org = disc.organisation
        fa = disc.fact_a
        fb = disc.fact_b
        ea = fa.evidence if fa else None
        eb = fb.evidence if fb else None
        sa = fa.snapshot if fa else None
        sb = fb.snapshot if fb else None

        return {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "title": f"Discrepancy: {disc.field_key} ({disc.conflict_type})",
            "summary": f"Conflict on '{disc.field_key}' ({disc.scope_key}) between Fact #{disc.fact_a_id} and Fact #{disc.fact_b_id}.",
            "flagged_reason": disc.rationale,
            "current_status": disc.status,
            "review_status": disc.status,
            "priority": "high",
            "organisation_id": disc.organisation_id,
            "organisation_name": org.name if org else None,
            "source_id": None,
            "source_url": None,
            "snapshot_id": None,
            "snapshot_title": None,
            "snapshot_fetched_at": None,
            "evidence_id": None,
            "evidence_quote": None,
            "evidence_start_offset": None,
            "evidence_end_offset": None,
            "entity_data": {
                "field_key": disc.field_key,
                "scope_key": disc.scope_key,
                "conflict_type": disc.conflict_type,
                "resolution_notes": disc.resolution_notes,
                "resolved_by": disc.resolved_by,
                "resolved_at": disc.resolved_at.isoformat() if disc.resolved_at else None,
            },
            "discrepancy_context": {
                "fact_a": {
                    "id": fa.id if fa else None,
                    "raw_value": fa.raw_value if fa else None,
                    "normalized_value": fa.normalized_value if fa else None,
                    "source_url": sa.source.url if sa and sa.source else None,
                    "snapshot_id": sa.id if sa else None,
                    "evidence_quote": ea.quote if ea else None,
                    "evidence_start_offset": ea.start_offset if ea else None,
                    "evidence_end_offset": ea.end_offset if ea else None,
                },
                "fact_b": {
                    "id": fb.id if fb else None,
                    "raw_value": fb.raw_value if fb else None,
                    "normalized_value": fb.normalized_value if fb else None,
                    "source_url": sb.source.url if sb and sb.source else None,
                    "snapshot_id": sb.id if sb else None,
                    "evidence_quote": eb.quote if eb else None,
                    "evidence_start_offset": eb.start_offset if eb else None,
                    "evidence_end_offset": eb.end_offset if eb else None,
                },
            },
            "opportunity_context": None,
            "change_context": None,
            "allowed_actions": allowed_actions,
            "review_history": history,
        }

    elif entity_type == "opportunity":
        opp = session.get(OpportunityORM, entity_id)
        if not opp:
            raise ValueError(f"Opportunity id={entity_id} not found")

        org = opp.organisation
        src = opp.source
        snap = opp.last_snapshot

        return {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "title": f"Opportunity: {opp.role_title} ({opp.status})",
            "summary": f"Ref: {opp.explicit_ad_id or 'None'}. Category: {opp.role_category}.",
            "flagged_reason": f"Status '{opp.status}': " + (opp.closure_evidence_quote or f"Absences: {opp.consecutive_absences}"),
            "current_status": opp.status,
            "review_status": "accepted" if history else "pending",
            "priority": "high" if opp.status in (OpportunityStatus.EXPLICITLY_CLOSED.value, OpportunityStatus.POSSIBLY_STALE.value) else "medium",
            "organisation_id": opp.organisation_id,
            "organisation_name": org.name if org else None,
            "source_id": opp.source_id,
            "source_url": src.url if src else None,
            "snapshot_id": snap.id if snap else None,
            "snapshot_title": snap.title if snap else None,
            "snapshot_fetched_at": snap.fetched_at.isoformat() if snap and snap.fetched_at else None,
            "evidence_id": None,
            "evidence_quote": opp.closure_evidence_quote or opp.verbatim_wording,
            "evidence_start_offset": None,
            "evidence_end_offset": None,
            "entity_data": {
                "role_category": opp.role_category,
                "role_title": opp.role_title,
                "verbatim_wording": opp.verbatim_wording,
                "contact_channel": opp.contact_channel,
                "explicit_ad_id": opp.explicit_ad_id,
                "closure_evidence_quote": opp.closure_evidence_quote,
                "consecutive_absences": opp.consecutive_absences,
                "first_seen_at": opp.first_seen_at.isoformat() if opp.first_seen_at else None,
                "last_seen_at": opp.last_seen_at.isoformat() if opp.last_seen_at else None,
                "closed_at": opp.closed_at.isoformat() if opp.closed_at else None,
            },
            "discrepancy_context": None,
            "opportunity_context": {
                "closure_evidence_quote": opp.closure_evidence_quote,
                "consecutive_absences": opp.consecutive_absences,
            },
            "change_context": None,
            "allowed_actions": allowed_actions,
            "review_history": history,
        }

    elif entity_type == "change_event":
        evt = session.get(ChangeEventORM, entity_id)
        if not evt:
            raise ValueError(f"ChangeEvent id={entity_id} not found")

        src = evt.source
        org = evt.organisation
        return {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "title": f"Change Event: {evt.field_key} ({evt.event_type})",
            "summary": f"Old: '{evt.old_value}' -> New: '{evt.new_value}'.",
            "flagged_reason": evt.rationale,
            "current_status": "detected",
            "review_status": "accepted" if history else "pending",
            "priority": "medium",
            "organisation_id": evt.organisation_id,
            "organisation_name": org.name if org else None,
            "source_id": evt.source_id,
            "source_url": src.url if src else None,
            "snapshot_id": evt.current_snapshot_id,
            "snapshot_title": None,
            "snapshot_fetched_at": None,
            "evidence_id": None,
            "evidence_quote": None,
            "evidence_start_offset": None,
            "evidence_end_offset": None,
            "entity_data": {
                "event_type": evt.event_type,
                "entity_type": evt.entity_type,
                "field_key": evt.field_key,
                "scope_key": evt.scope_key,
                "old_value": evt.old_value,
                "new_value": evt.new_value,
                "rationale": evt.rationale,
                "diff_metadata": evt.diff_metadata,
            },
            "discrepancy_context": None,
            "opportunity_context": None,
            "change_context": {
                "previous_snapshot_id": evt.previous_snapshot_id,
                "current_snapshot_id": evt.current_snapshot_id,
            },
            "allowed_actions": allowed_actions,
            "review_history": history,
        }

    elif entity_type in ("lifecycle_event", "practice_lifecycle"):
        evt = session.get(PracticeLifecycleEventORM, entity_id)
        if not evt:
            raise ValueError(f"PracticeLifecycleEvent id={entity_id} not found")

        org = evt.organisation
        src = evt.source
        snap = evt.snapshot
        allowed_actions = ALLOWED_ACTIONS_BY_ENTITY.get("lifecycle_event", [])

        title_prefix = {
            "permanent_closure": "Permanent Closure",
            "temporary_closure": "Temporary Closure",
            "relocation": "Relocation",
            "acquisition": "Corporate Acquisition",
        }.get(evt.event_type, evt.event_type.replace("_", " ").title())
        scope_desc = f"Branch ({evt.branch_locality})" if evt.is_branch_level and evt.branch_locality else "Entire Practice"

        return {
            "entity_type": "lifecycle_event",
            "entity_id": entity_id,
            "title": f"Practice Lifecycle: {title_prefix}",
            "summary": f"Organisation: {org.name if org else 'Unknown'}. Scope: {scope_desc}. Status: {evt.status}.",
            "flagged_reason": evt.rationale,
            "current_status": evt.status,
            "review_status": evt.review_status,
            "priority": "high" if evt.status == "confirmed_explicit" or evt.event_type == "permanent_closure" else "medium",
            "organisation_id": evt.organisation_id,
            "organisation_name": org.name if org else None,
            "source_id": evt.source_id,
            "source_url": src.url if src else None,
            "snapshot_id": evt.snapshot_id,
            "snapshot_title": snap.title if snap else None,
            "snapshot_fetched_at": snap.fetched_at.isoformat() if snap and snap.fetched_at else None,
            "evidence_id": evt.evidence_id,
            "evidence_quote": evt.evidence_quote,
            "evidence_start_offset": evt.evidence.start_offset if evt.evidence else None,
            "evidence_end_offset": evt.evidence.end_offset if evt.evidence else None,
            "entity_data": {
                "event_type": evt.event_type,
                "status": evt.status,
                "branch_locality": evt.branch_locality,
                "is_branch_level": evt.is_branch_level,
                "effective_date": evt.effective_date,
                "date_precision": evt.date_precision,
                "old_value": evt.old_value,
                "new_value": evt.new_value,
                "acquiring_organisation_id": evt.acquiring_organisation_id,
                "acquiring_entity_name": evt.acquiring_entity_name,
                "rationale": evt.rationale,
            },
            "discrepancy_context": None,
            "opportunity_context": None,
            "change_context": None,
            "lifecycle_context": {
                "event_type": evt.event_type,
                "branch_locality": evt.branch_locality,
                "is_branch_level": evt.is_branch_level,
                "old_value": evt.old_value,
                "new_value": evt.new_value,
                "effective_date": evt.effective_date,
                "acquiring_entity_name": evt.acquiring_entity_name,
            },
            "allowed_actions": allowed_actions,
            "review_history": history,
        }

    else:
        raise ValueError(f"Unsupported entity_type: '{entity_type}'")


# =============================================================================
# RECORD REVIEW ACTION (APPEND-ONLY DECISION WORKFLOW)
# =============================================================================

def record_review_action(
    session: Session,
    entity_type: str,
    entity_id: int,
    action: str,
    reason_code: str,
    reviewer: str,
    notes: str | None = None,
    evidence_id: int | None = None,
    decision_metadata: dict[str, Any] | None = None,
) -> ReviewActionORM:
    """Execute a human review decision, validate boundaries, mutate state, and append immutable audit record."""
    # 1. Validate inputs
    if not reviewer or not reviewer.strip():
        raise ValueError("reviewer identifier must be non-empty")
    reviewer = reviewer.strip()

    if not reason_code or not reason_code.strip():
        raise ValueError("reason_code must be non-empty")
    reason_code = reason_code.strip()

    decision_metadata = decision_metadata or {}

    # Strict architectural prohibition against filled or hired opportunity status
    if entity_type == "opportunity":
        attempted_status = decision_metadata.get("status", "").lower()
        if attempted_status in ("filled", "hired") or action.lower() in ("filled", "hired"):
            raise ValueError("Opportunity status 'filled' or 'hired' is strictly forbidden by PracticeSignal architecture")

    # Validate allowed actions
    allowed = ALLOWED_ACTIONS_BY_ENTITY.get(entity_type, [])
    if action not in allowed:
        raise ValueError(f"Action '{action}' is not permitted for entity_type '{entity_type}'. Allowed: {allowed}")

    # Consequential decision requires notes
    consequential = {
        ReviewActionType.RESOLVED_A.value,
        ReviewActionType.RESOLVED_B.value,
        ReviewActionType.REJECTED.value,
        ReviewActionType.DISMISSED_BENIGN.value,
    }
    if action in consequential and not (notes and notes.strip()):
        raise ValueError(f"Action '{action}' requires non-empty explanatory notes/rationale")

    previous_status: str = "pending"
    new_status: str = action

    # 2. Entity domain mutation
    if entity_type == "match_candidate":
        cand = session.get(OrganisationMatchCandidate, entity_id)
        if not cand:
            raise ValueError(f"OrganisationMatchCandidate id={entity_id} not found")

        previous_status = cand.status

        if action in (ReviewActionType.APPROVED.value, ReviewActionType.ACCEPTED.value):
            cand.status = "verified"
            cand.reviewed_by = reviewer
            cand.reviewed_at = _utcnow()
            new_status = "verified"

            # Confirm or create verified organisation source link
            link = session.execute(
                select(OrganisationSourceLink).where(
                    OrganisationSourceLink.organisation_id == cand.suggested_organisation_id,
                    OrganisationSourceLink.source_id == cand.source_id,
                )
            ).scalar_one_or_none()

            if link:
                link.verification_status = "confirmed"
                link.verified_by = reviewer
                link.verified_at = _utcnow()
            else:
                new_link = OrganisationSourceLink(
                    organisation_id=cand.suggested_organisation_id,
                    source_id=cand.source_id,
                    relationship_type="official_practice_site",
                    verification_status="confirmed",
                    verified_by=reviewer,
                    verified_at=_utcnow(),
                )
                session.add(new_link)

        elif action == ReviewActionType.REJECTED.value:
            cand.status = "rejected"
            cand.reviewed_by = reviewer
            cand.reviewed_at = _utcnow()
            new_status = "rejected"

            link = session.execute(
                select(OrganisationSourceLink).where(
                    OrganisationSourceLink.organisation_id == cand.suggested_organisation_id,
                    OrganisationSourceLink.source_id == cand.source_id,
                )
            ).scalar_one_or_none()
            if link and link.verification_status == "suggested":
                link.verification_status = "rejected"

        elif action == ReviewActionType.DEFERRED.value:
            cand.status = "deferred"
            new_status = "deferred"

        elif action == ReviewActionType.REOPENED.value:
            cand.status = "suggested"
            new_status = "suggested"

    elif entity_type == "fact_ownership":
        fact = session.get(Fact, entity_id)
        if not fact:
            raise ValueError(f"Fact id={entity_id} not found")

        previous_status = fact.status

        if action == ReviewActionType.ASSIGNED_ORG.value:
            target_org_id = decision_metadata.get("assigned_organisation_id")
            if not target_org_id:
                raise ValueError("assigned_organisation_id must be provided in decision_metadata to assign fact ownership")

            # Validate organisation exists
            target_org = session.get(Organisation, target_org_id)
            if not target_org:
                raise ValueError(f"Organisation id={target_org_id} does not exist")

            fact.organisation_id = target_org.id
            fact.needs_review_reason = None
            fact.status = "verified"
            new_status = "assigned_org"

        elif action in (ReviewActionType.DISMISSED_BENIGN.value, ReviewActionType.REJECTED.value):
            # Explicitly leave unassigned
            fact.organisation_id = None
            fact.needs_review_reason = "explicitly_unassigned_corporate"
            new_status = "unassigned_corporate"

        elif action == ReviewActionType.DEFERRED.value:
            fact.status = "deferred"
            new_status = "deferred"

    elif entity_type == "fact":
        fact = session.get(Fact, entity_id)
        if not fact:
            raise ValueError(f"Fact id={entity_id} not found")

        previous_status = fact.status

        if action in (ReviewActionType.APPROVED.value, ReviewActionType.ACCEPTED.value):
            fact.status = "verified"
            new_status = "verified"
        elif action == ReviewActionType.REJECTED.value:
            fact.status = "rejected"
            new_status = "rejected"
        elif action == ReviewActionType.DEFERRED.value:
            fact.status = "needs_review"
            new_status = "deferred"

    elif entity_type == "discrepancy":
        disc = session.get(Discrepancy, entity_id)
        if not disc:
            raise ValueError(f"Discrepancy id={entity_id} not found")

        previous_status = disc.status

        if action == ReviewActionType.UNDER_REVIEW.value:
            disc.status = "under_review"
            new_status = "under_review"

        elif action in (ReviewActionType.RESOLVED_A.value, ReviewActionType.RESOLVED_B.value, ReviewActionType.RESOLVED.value):
            # Neither underlying fact is overwritten! Both original facts remain untouched.
            disc.status = action
            disc.resolved_by = reviewer
            disc.resolved_at = _utcnow()
            disc.resolution_notes = notes
            new_status = action

        elif action in (ReviewActionType.DISMISSED_BENIGN.value, ReviewActionType.DISMISSED.value):
            disc.status = "dismissed_benign"
            disc.resolved_by = reviewer
            disc.resolved_at = _utcnow()
            disc.resolution_notes = notes
            new_status = "dismissed_benign"

        elif action == ReviewActionType.DEFERRED.value:
            disc.status = "needs_investigation"
            new_status = "deferred"

        elif action == ReviewActionType.REOPENED.value:
            disc.status = "detected"
            disc.resolved_by = None
            disc.resolved_at = None
            new_status = "detected"

    elif entity_type == "opportunity":
        opp = session.get(OpportunityORM, entity_id)
        if not opp:
            raise ValueError(f"Opportunity id={entity_id} not found")

        previous_status = opp.status
        # Does not change underlying status unless acknowledged
        new_status = "acknowledged"

    elif entity_type == "change_event":
        evt = session.get(ChangeEventORM, entity_id)
        if not evt:
            raise ValueError(f"ChangeEvent id={entity_id} not found")

        previous_status = "detected"
        new_status = "acknowledged"

    elif entity_type == "freshness":
        previous_status = "evaluated"
        new_status = "acknowledged"

    elif entity_type in ("lifecycle_event", "practice_lifecycle"):
        evt = session.get(PracticeLifecycleEventORM, entity_id)
        if not evt:
            raise ValueError(f"PracticeLifecycleEvent id={entity_id} not found")

        previous_status = evt.review_status

        if action in (ReviewActionType.APPROVED.value, ReviewActionType.ACCEPTED.value):
            evt.review_status = "accepted"
            evt.reviewed_by = reviewer
            evt.reviewed_at = _utcnow()
            new_status = "accepted"
        elif action == ReviewActionType.REJECTED.value:
            evt.review_status = "rejected"
            evt.reviewed_by = reviewer
            evt.reviewed_at = _utcnow()
            new_status = "rejected"
        elif action == ReviewActionType.DEFERRED.value:
            evt.review_status = "deferred"
            new_status = "deferred"
        elif action == ReviewActionType.REOPENED.value:
            evt.review_status = "pending"
            new_status = "pending"
        elif action == ReviewActionType.ACKNOWLEDGED.value:
            evt.review_status = "acknowledged"
            evt.reviewed_by = reviewer
            evt.reviewed_at = _utcnow()
            new_status = "acknowledged"
        else:
            new_status = action

    # 3. Create durable, append-only ReviewAction audit record
    audit_record = ReviewActionORM(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        previous_status=previous_status,
        new_status=new_status,
        reason_code=reason_code,
        reviewer=reviewer,
        notes=notes,
        evidence_id=evidence_id,
        decision_metadata=decision_metadata,
        created_at=_utcnow(),
    )
    session.add(audit_record)
    session.flush()

    logger.info(
        "review_action_recorded id=%d entity=%s:%d action=%s reviewer=%s prev=%s new=%s",
        audit_record.id, entity_type, entity_id, action, reviewer, previous_status, new_status,
    )

    return audit_record


# =============================================================================
# RETRIEVE AUDIT HISTORY
# =============================================================================

def get_review_history(
    session: Session,
    entity_type: str | None = None,
    entity_id: int | None = None,
    reviewer: str | None = None,
    action: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Retrieve immutable chronological audit history of review actions."""
    q = select(ReviewActionORM).order_by(ReviewActionORM.created_at.desc())

    if entity_type:
        q = q.where(ReviewActionORM.entity_type == entity_type)
    if entity_id is not None:
        q = q.where(ReviewActionORM.entity_id == entity_id)
    if reviewer:
        q = q.where(ReviewActionORM.reviewer == reviewer)
    if action:
        q = q.where(ReviewActionORM.action == action)

    total_count = session.execute(select(func.count()).select_from(q.subquery())).scalar_one()

    records = session.execute(q.limit(limit).offset(offset)).scalars().all()

    items = [
        {
            "id": r.id,
            "entity_type": r.entity_type,
            "entity_id": r.entity_id,
            "action": r.action,
            "previous_status": r.previous_status,
            "new_status": r.new_status,
            "reason_code": r.reason_code,
            "reviewer": r.reviewer,
            "notes": r.notes,
            "evidence_id": r.evidence_id,
            "decision_metadata": r.decision_metadata if isinstance(r.decision_metadata, dict) else (json.loads(r.decision_metadata) if r.decision_metadata else {}),
            "created_at": r.created_at.isoformat() if r.created_at else _utcnow().isoformat(),
        }
        for r in records
    ]

    return {
        "items": items,
        "total": total_count,
        "limit": limit,
        "offset": offset,
    }
