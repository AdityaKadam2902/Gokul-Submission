"""Unit tests for Milestone 3 Pydantic domain contracts and validation rules (ARCHITECTURE §6.1, D39–D47)."""

from __future__ import annotations

from datetime import datetime, UTC
import pytest
from pydantic import ValidationError

from backend.app.intelligence.schemas import (
    ChangeEvent,
    ChangeEventCategory,
    ConflictType,
    DiffMetadata,
    Discrepancy,
    DiscrepancyStatus,
    ExtractionCompleteness,
    FreshnessAssessment,
    FreshnessCategory,
    MatchSignals,
    MatchTier,
    Opportunity,
    OpportunityStatus,
    Organisation,
    OrganisationMatchCandidate,
    OrganisationSourceLink,
    OrganisationStatus,
    ParsingCompleteness,
    RelationshipType,
    ReviewAction,
    ReviewActionType,
    ReviewStatus,
    RoleCategory,
    SourceObservation,
    ObservationOutcome,
    VerificationStatus,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TestOrganisationSchemas:
    """Test organisation domain contract and slug validation."""

    def test_valid_organisation_creation(self):
        org = Organisation(
            name="St Jude Family Medical Centre",
            slug="st-jude-family-medical-3000",
            primary_domain="stjude-medical.example.org",
            phone="+61394198888",
            address="120 Collins St",
            suburb="Melbourne",
            postcode="3000",
            state="VIC",
            status=OrganisationStatus.ACTIVE,
        )
        assert org.name == "St Jude Family Medical Centre"
        assert org.slug == "st-jude-family-medical-3000"
        assert org.status == OrganisationStatus.ACTIVE

    def test_invalid_slug_pattern(self):
        with pytest.raises(ValidationError):
            Organisation(
                name="Bad Slug Clinic",
                slug="Bad Slug!",  # spaces and special chars forbidden
                postcode="3000",
                state="VIC",
            )

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            Organisation(
                name="Test Clinic",
                slug="test-clinic",
                postcode="3000",
                state="VIC",
                unexpected_field="disallowed",
            )


class TestSourceObservationSchemas:
    """Test observation attempt heartbeat and consistency validators (D40)."""

    def test_valid_success_new_snapshot(self):
        obs = SourceObservation(
            source_id=1,
            observed_at=_utcnow(),
            outcome=ObservationOutcome.SUCCESS_NEW_SNAPSHOT,
            snapshot_id=10,
            content_hash="a" * 64,
            content_changed=True,
            parsing_completeness=ParsingCompleteness.COMPLETE,
            extraction_completeness=ExtractionCompleteness.COMPLETE,
            observation_sequence=1,
        )
        assert obs.outcome == ObservationOutcome.SUCCESS_NEW_SNAPSHOT
        assert obs.content_changed is True

    def test_valid_success_unchanged_heartbeat(self):
        obs = SourceObservation(
            source_id=1,
            observed_at=_utcnow(),
            outcome=ObservationOutcome.SUCCESS_UNCHANGED,
            snapshot_id=10,
            content_hash="a" * 64,
            content_changed=False,
            parsing_completeness=ParsingCompleteness.COMPLETE,
            extraction_completeness=ExtractionCompleteness.COMPLETE,
            observation_sequence=2,
        )
        assert obs.outcome == ObservationOutcome.SUCCESS_UNCHANGED
        assert obs.content_changed is False

    def test_inconsistent_outcome_validation_errors(self):
        # SUCCESS_NEW_SNAPSHOT with content_changed=False must fail
        with pytest.raises(ValidationError, match="SUCCESS_NEW_SNAPSHOT requires content_changed=True"):
            SourceObservation(
                source_id=1,
                observed_at=_utcnow(),
                outcome=ObservationOutcome.SUCCESS_NEW_SNAPSHOT,
                content_changed=False,
                observation_sequence=1,
            )

        # SUCCESS_UNCHANGED with content_changed=True must fail
        with pytest.raises(ValidationError, match="SUCCESS_UNCHANGED requires content_changed=False"):
            SourceObservation(
                source_id=1,
                observed_at=_utcnow(),
                outcome=ObservationOutcome.SUCCESS_UNCHANGED,
                content_changed=True,
                observation_sequence=1,
            )

        # FAILED with content_changed=True must fail
        with pytest.raises(ValidationError, match="FAILED outcome cannot report content_changed=True"):
            SourceObservation(
                source_id=1,
                observed_at=_utcnow(),
                outcome=ObservationOutcome.FAILED,
                content_changed=True,
                observation_sequence=1,
            )


class TestMatchCandidateSchemas:
    """Test conservative 3-tier entity resolution schemas and score semantics (D39)."""

    def test_strong_match_suggestion_requires_branch_proof(self):
        # Valid strong match with local phone and postcode
        candidate = OrganisationMatchCandidate(
            source_id=1,
            suggested_organisation_id=2,
            match_tier=MatchTier.STRONG_MATCH_SUGGESTION,
            raw_metric_score=95.0,
            evidence_signals=MatchSignals(
                domain_match=True,
                postcode_match=True,
                local_phone_match=True,
                corporate_phone_detected=False,
            ),
            rationale="Exact official domain and local reception telephone match in postcode 3000.",
            status=ReviewStatus.SUGGESTED,
        )
        assert candidate.match_tier == MatchTier.STRONG_MATCH_SUGGESTION

        # Invalid strong match with root domain only (corporate phone detected, no branch proof)
        with pytest.raises(ValidationError, match="STRONG_MATCH_SUGGESTION requires branch-level identity proof"):
            OrganisationMatchCandidate(
                source_id=1,
                suggested_organisation_id=2,
                match_tier=MatchTier.STRONG_MATCH_SUGGESTION,
                evidence_signals=MatchSignals(
                    domain_match=True,
                    postcode_match=False,
                    local_phone_match=False,
                    corporate_phone_detected=True,
                ),
                rationale="Root domain match only.",
            )

    def test_possible_match_tier(self):
        candidate = OrganisationMatchCandidate(
            source_id=1,
            suggested_organisation_id=2,
            match_tier=MatchTier.POSSIBLE_MATCH,
            raw_metric_score=88.5,
            evidence_signals=MatchSignals(
                domain_match=False,
                postcode_match=True,
                name_similarity_metric=88.5,
            ),
            rationale="Fuzzy name similarity of 88.5 and identical postcode.",
            status=ReviewStatus.NEEDS_REVIEW,
        )
        assert candidate.match_tier == MatchTier.POSSIBLE_MATCH
        assert candidate.status == ReviewStatus.NEEDS_REVIEW


class TestDiscrepancySchemas:
    """Test evidence-backed cross-source discrepancy schemas (D41)."""

    def test_valid_discrepancy(self):
        disc = Discrepancy(
            organisation_id=1,
            field_key="public_phone",
            scope_key="general",
            fact_a_id=101,
            fact_b_id=102,
            conflict_type=ConflictType.VALUE_MISMATCH,
            status=DiscrepancyStatus.DETECTED,
            resolution_notes="Phone conflict between practice site and directory.",
        )
        assert disc.fact_a_id != disc.fact_b_id
        assert disc.status == DiscrepancyStatus.DETECTED

    def test_identical_facts_rejected(self):
        with pytest.raises(ValidationError, match="fact_a_id and fact_b_id must be distinct"):
            Discrepancy(
                organisation_id=1,
                field_key="public_phone",
                scope_key="general",
                fact_a_id=101,
                fact_b_id=101,
            )


class TestChangeEventSchemas:
    """Test temporal snapshot diffing schemas (D42)."""

    def test_valid_change_event(self):
        event = ChangeEvent(
            source_id=1,
            organisation_id=2,
            previous_snapshot_id=10,
            current_snapshot_id=11,
            event_type=ChangeEventCategory.FACT_MODIFIED,
            entity_type="fact",
            entity_id=5,
            field_key="public_phone",
            scope_key="general",
            old_value="(03) 9123 4567",
            new_value="(03) 9123 9999",
            diff_metadata=DiffMetadata(wording_diff="Phone changed from 4567 to 9999"),
        )
        assert event.previous_snapshot_id != event.current_snapshot_id
        assert event.event_type == ChangeEventCategory.FACT_MODIFIED

    def test_identical_snapshots_rejected(self):
        with pytest.raises(ValidationError, match="previous_snapshot_id and current_snapshot_id must be distinct"):
            ChangeEvent(
                source_id=1,
                previous_snapshot_id=10,
                current_snapshot_id=10,
                event_type=ChangeEventCategory.FACT_MODIFIED,
                entity_type="fact",
                field_key="public_phone",
            )


class TestOpportunitySchemas:
    """Test healthcare recruitment vacancy schemas and conservative lifecycles (D43)."""

    def test_valid_advertised_vacancy(self):
        opp = Opportunity(
            organisation_id=1,
            source_id=2,
            first_snapshot_id=10,
            last_snapshot_id=10,
            role_category=RoleCategory.GP,
            role_title="VR General Practitioner",
            verbatim_wording="Seeking VR GP, 70% billings, immediate start. Ref: GP-2026-04",
            explicit_ad_id="GP-2026-04",
            status=OpportunityStatus.ADVERTISED,
        )
        assert opp.role_category == RoleCategory.GP
        assert opp.status == OpportunityStatus.ADVERTISED

    def test_explicitly_closed_requires_verbatim_quote(self):
        # Fails without closure quote
        with pytest.raises(ValidationError, match="status EXPLICITLY_CLOSED strictly requires closure_evidence_quote"):
            Opportunity(
                organisation_id=1,
                source_id=2,
                first_snapshot_id=10,
                last_snapshot_id=11,
                role_category=RoleCategory.GP,
                role_title="VR General Practitioner",
                verbatim_wording="Seeking VR GP",
                status=OpportunityStatus.EXPLICITLY_CLOSED,
                closure_evidence_quote=None,
            )

        # Passes with verified quote
        opp = Opportunity(
            organisation_id=1,
            source_id=2,
            first_snapshot_id=10,
            last_snapshot_id=11,
            role_category=RoleCategory.GP,
            role_title="VR General Practitioner",
            verbatim_wording="Seeking VR GP",
            status=OpportunityStatus.EXPLICITLY_CLOSED,
            closure_evidence_quote="Applications for VR GP role (Ref: GP-2026-04) are now closed.",
        )
        assert opp.status == OpportunityStatus.EXPLICITLY_CLOSED


class TestReviewActionSchemas:
    """Test append-only audit trail schemas with locally identified reviewers (D44)."""

    def test_valid_review_action(self):
        action = ReviewAction(
            entity_type="match_candidate",
            entity_id=1,
            action=ReviewActionType.APPROVED,
            previous_status="suggested",
            new_status="approved",
            reason_code="verified_branch_identity",
            reviewer="local_analyst_1",
            notes="Confirmed via street address and local phone.",
        )
        assert action.reviewer == "local_analyst_1"
        assert action.action == ReviewActionType.APPROVED
