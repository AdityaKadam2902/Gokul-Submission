"""Deterministic validation test suite for Milestone 3 synthetic evaluation corpus (ARCHITECTURE §8, D45)."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

M3_CORPUS_DIR = Path(__file__).parent / "fixtures" / "m3_corpus"


@pytest.fixture(scope="module")
def manifest() -> dict:
    manifest_path = M3_CORPUS_DIR / "manifest.json"
    assert manifest_path.is_file(), f"Manifest file missing at {manifest_path}"
    with open(manifest_path, "r", encoding="utf-8") as f:
        return json.load(f)


class TestManifestIntegrity:
    """Validate manifest completeness, fixture registration and synthetic attestations."""

    def test_manifest_structure_and_attestation(self, manifest):
        assert manifest["manifest_version"] == "3.0"
        assert manifest["milestone"] == "M3.0"
        attestation = manifest["synthetic_attestation"]
        assert attestation["all_data_invented"] is True
        assert attestation["no_live_scrape_represented_or_performed"] is True
        assert attestation["reserved_example_domains_used"] is True
        assert "D39" in attestation["decision_log_refs"]
        assert "D47" in attestation["decision_log_refs"]

    def test_all_manifest_fixtures_exist_on_disk(self, manifest):
        assert len(manifest["fixtures"]) >= 30, "Expected at least 30 synthetic test fixtures"
        seen_ids = set()
        for fixture_entry in manifest["fixtures"]:
            f_id = fixture_entry["id"]
            assert f_id not in seen_ids, f"Duplicate fixture ID detected: {f_id}"
            seen_ids.add(f_id)

            rel_file = fixture_entry["file"]
            abs_file = M3_CORPUS_DIR / rel_file
            assert abs_file.is_file(), f"Fixture file {rel_file} listed in manifest does not exist on disk"

    def test_all_disk_fixtures_registered_in_manifest(self, manifest):
        manifest_files = {entry["file"].replace("\\", "/") for entry in manifest["fixtures"]}
        disk_files = {
            p.relative_to(M3_CORPUS_DIR).as_posix()
            for p in M3_CORPUS_DIR.glob("**/*.json")
            if p.name != "manifest.json"
        }
        unregistered = disk_files - manifest_files
        assert not unregistered, f"Unregistered fixture files found on disk: {unregistered}"


class TestCorpusWatermarksAndDomains:
    """Assert that every fixture is strictly synthetic, watermarked and uses non-routable domains."""

    def test_fixtures_watermark_and_synthetic_flags(self, manifest):
        for fixture_entry in manifest["fixtures"]:
            abs_file = M3_CORPUS_DIR / fixture_entry["file"]
            with open(abs_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            assert data.get("synthetic") is True, f"{fixture_entry['id']} missing synthetic=True"
            assert data.get("synthetic_watermark") == "PRACTICESIGNAL_SYNTHETIC_FIXTURE_DO_NOT_TREAT_AS_REAL_DATA", (
                f"{fixture_entry['id']} missing correct synthetic watermark"
            )

            # Check that URLs and domains use reserved .example.org
            src_ident = data.get("source_identity", {})
            if "url" in src_ident:
                assert ".example.org" in src_ident["url"], (
                    f"{fixture_entry['id']} URL '{src_ident['url']}' does not use reserved example domain"
                )
            if "domain" in src_ident:
                assert ".example.org" in src_ident["domain"], (
                    f"{fixture_entry['id']} domain '{src_ident['domain']}' does not use reserved example domain"
                )


class TestGroupAlphaMatchingGroundTruth:
    """Validate entity resolution ground truth, matching tiers, branch isolation and corporate phone rejection."""

    def _load(self, rel_path: str) -> dict:
        with open(M3_CORPUS_DIR / rel_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_f7_exact_domain_match_strong_suggestion(self):
        f7_b = self._load("group_alpha_matching/F7_exact_domain_b.json")
        outcome = f7_b["expected_outcome"]
        assert outcome["match_tier"] == "strong_match_suggestion"
        assert outcome["zero_auto_merge"] is True
        assert outcome["evidence_signals"]["domain_match"] is True
        assert outcome["evidence_signals"]["postcode_match"] is True

    def test_f8_name_variation_possible_match(self):
        f8_b = self._load("group_alpha_matching/F8_name_variation_b.json")
        outcome = f8_b["expected_outcome"]
        assert outcome["match_tier"] == "possible_match"
        assert outcome["review_status"] == "needs_review"
        assert outcome["evidence_signals"]["postcode_match"] is True
        assert 80.0 <= outcome["raw_metric_score_range"][0] <= 90.0

    def test_f9_lookalike_branches_strict_isolation(self):
        f9_frankston = self._load("group_alpha_matching/F9_branch_frankston.json")
        outcome = f9_frankston["expected_outcome"]
        assert outcome["match_tier"] == "insufficient_evidence"
        assert "metropolitan-medical-box-hill-3128" in outcome["disallowed_matches"]
        assert "3128 vs 3199" in outcome["rationale"]

    def test_f9_precinct_distinct_suites(self):
        f9_suite4 = self._load("group_alpha_matching/F9_precinct_suite4.json")
        outcome = f9_suite4["expected_outcome"]
        assert outcome["match_tier"] == "insufficient_evidence"
        assert "metropolitan-medical-box-hill-3128" in outcome["disallowed_matches"]

    def test_f9_shared_corporate_phone_rejected(self):
        f9_phone = self._load("group_alpha_matching/F9_shared_1300_phone.json")
        outcome = f9_phone["expected_outcome"]
        assert outcome["match_tier"] == "insufficient_evidence"
        assert "1300" in outcome["rationale"]

    def test_f9_generic_clinic_name_rejected(self):
        f9_neg = self._load("group_alpha_matching/F9_neg_generic.json")
        outcome = f9_neg["expected_outcome"]
        assert outcome["match_tier"] == "insufficient_evidence"
        assert outcome["emit_match_candidate"] is False


class TestGroupBetaDiscrepanciesGroundTruth:
    """Validate discrepancy detection, phone normalization, departmental scope isolation and unscoped review."""

    def _load(self, rel_path: str) -> dict:
        with open(M3_CORPUS_DIR / rel_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_f10_phone_formatting_agreement(self):
        f10_a = self._load("group_beta_discrepancy/F10_phone_format_a.json")
        f10_b = self._load("group_beta_discrepancy/F10_phone_format_b.json")
        # E.164 normalization produces identical strings
        assert f10_a["extracted_fact"]["normalized_value"] == f10_b["extracted_fact"]["normalized_value"] == "+61394198888"
        assert f10_b["expected_outcome"]["discrepancy_detected"] is False

    def test_f11_phone_conflict_detected(self):
        f11_b = self._load("group_beta_discrepancy/F11_phone_conflict_b.json")
        outcome = f11_b["expected_outcome"]
        assert outcome["discrepancy_detected"] is True
        assert outcome["conflict_type"] == "value_mismatch"
        assert outcome["status"] == "detected"

    def test_f12_departmental_hours_scope_isolation(self):
        f12_clinic = self._load("group_beta_discrepancy/F12_hours_clinic.json")
        f12_pathology = self._load("group_beta_discrepancy/F12_hours_pathology.json")
        assert f12_clinic["extracted_fact"]["scope_key"] == "clinic"
        assert f12_pathology["extracted_fact"]["scope_key"] == "pathology"
        assert f12_pathology["expected_outcome"]["discrepancy_detected"] is False

    def test_f12_unscoped_hours_routes_to_review(self):
        f12_unscoped = self._load("group_beta_discrepancy/F12_hours_unscoped_b.json")
        outcome = f12_unscoped["expected_outcome"]
        assert outcome["discrepancy_detected"] is False
        assert outcome["review_required"] is True
        assert "NULL" in outcome["rationale"]


class TestGroupGammaChangesGroundTruth:
    """Validate observation sequences, unchanged crawl short-circuiting, omissions and 2-crawl absence rule."""

    def _load(self, rel_path: str) -> dict:
        with open(M3_CORPUS_DIR / rel_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_f13_v1_and_unchanged_heartbeat(self):
        v1 = self._load("group_gamma_changes/F13_v1_baseline.json")
        unchanged = self._load("group_gamma_changes/F13_unchanged.json")
        assert v1["expected_outcome"]["observation_outcome"] == "success_new_snapshot"
        assert v1["expected_outcome"]["snapshot_created"] is True
        assert unchanged["expected_outcome"]["observation_outcome"] == "success_unchanged"
        assert unchanged["expected_outcome"]["snapshot_created"] is False
        assert unchanged["expected_outcome"]["change_events_emitted"] == 0

    def test_f13_v2_modifications_and_additions(self):
        v2 = self._load("group_gamma_changes/F13_v2_modified.json")
        outcome = v2["expected_outcome"]
        assert outcome["observation_outcome"] == "success_new_snapshot"
        assert outcome["change_events_emitted"] == 2
        events = {e["event_type"]: e for e in outcome["expected_events"]}
        assert "fact_modified" in events
        assert "fact_added" in events

    def test_f13_v3_consecutive_absence_rule(self):
        v3 = self._load("group_gamma_changes/F13_v3_omitted.json")
        outcome = v3["expected_outcome"]
        assert outcome["consecutive_absence_threshold"] == 2
        assert outcome["first_absence_state"] == "unconfirmed_prior"
        assert outcome["second_absence_state"] == "fact_unobserved"
        assert outcome["historical_fact_deleted"] is False

    def test_f13_crawler_failure_freezes_sequence(self):
        err = self._load("group_gamma_changes/F13_err_failure.json")
        outcome = err["expected_outcome"]
        assert outcome["observation_outcome"] == "failed"
        assert outcome["snapshot_created"] is False
        assert outcome["change_events_emitted"] == 0
        assert outcome["consecutive_missing_counter_incremented"] is False

    def test_f13_extraction_omission_suppresses_removal(self):
        omit = self._load("group_gamma_changes/F13_omit_extraction.json")
        outcome = omit["expected_outcome"]
        assert outcome["dom_verification_check"] == "passed_value_present_in_text"
        assert outcome["classification"] == "extraction_omission"
        assert outcome["change_events_emitted"] == 0


class TestGroupDeltaVacanciesGroundTruth:
    """Validate vacancy identity, wording updates, 2-crawl absence, explicit closures and zero 'filled' claims."""

    def _load(self, rel_path: str) -> dict:
        with open(M3_CORPUS_DIR / rel_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_f14_v1_advertised_with_job_id(self):
        v1 = self._load("group_delta_vacancies/F14_v1_advertised.json")
        outcome = v1["expected_outcome"]
        assert outcome["role_category"] == "gp"
        assert outcome["explicit_ad_id"] == "GP-2026-04"
        assert outcome["status"] == "advertised"
        assert outcome["published_date_inferred"] is False

    def test_f14_v2_title_change_updates_status_without_duplicate(self):
        v2 = self._load("group_delta_vacancies/F14_v2_changed.json")
        outcome = v2["expected_outcome"]
        assert outcome["identity_match_via"] == "explicit_ad_id"
        assert outcome["duplicate_created"] is False
        assert outcome["status"] == "changed"

    def test_f14_v3a_and_v3b_absence_sequence_and_no_filled_claim(self):
        v3a = self._load("group_delta_vacancies/F14_v3a_absent_once.json")
        v3b = self._load("group_delta_vacancies/F14_v3b_absent_twice.json")
        assert v3a["expected_outcome"]["status"] == "unconfirmed_prior"
        assert v3a["expected_outcome"]["marked_as_filled"] is False
        assert v3b["expected_outcome"]["status"] == "no_longer_observed"
        assert v3b["expected_outcome"]["marked_as_filled"] is False

    def test_f14_v3c_explicit_closure_requires_quote(self):
        v3c = self._load("group_delta_vacancies/F14_v3c_explicit_closure.json")
        outcome = v3c["expected_outcome"]
        assert outcome["status"] == "explicitly_closed"
        assert "closed" in outcome["closure_evidence_quote"].lower()
        assert outcome["marked_as_filled"] is False

    def test_f14_v3d_reappearance_without_duplicate(self):
        v3d = self._load("group_delta_vacancies/F14_v3d_reappeared.json")
        outcome = v3d["expected_outcome"]
        assert outcome["status"] == "advertised"
        assert outcome["duplicate_created"] is False
        assert outcome["change_event_emitted"] == "fact_reappeared"


class TestGroupEpsilonFactOwnershipGroundTruth:
    """Validate multi-branch corporate page fact ownership mapping via heading context (D47)."""

    def _load(self, rel_path: str) -> dict:
        with open(M3_CORPUS_DIR / rel_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_f15_heading_context_resolves_branch_ownership(self):
        f15 = self._load("group_epsilon_fact_ownership/F15_multibranch_page.json")
        mappings = f15["expected_outcome"]["ownership_mappings"]
        assert f15["expected_outcome"]["domain_only_assignment_permitted"] is False

        box_hill_facts = [m for m in mappings if m["heading_context"] == "Box Hill Family Practice"]
        assert len(box_hill_facts) == 2
        for fact in box_hill_facts:
            assert fact["resolved_organisation_slug"] == "metropolitan-medical-box-hill-3128"

        frankston_facts = [m for m in mappings if m["heading_context"] == "Frankston Medical Centre"]
        assert len(frankston_facts) == 2
        for fact in frankston_facts:
            assert fact["resolved_organisation_slug"] == "metropolitan-medical-frankston-3199"

        corporate_facts = [m for m in mappings if m["heading_context"] == "MetroHealth Corporate Headquarters"]
        assert len(corporate_facts) == 1
        assert corporate_facts[0]["resolved_organisation_slug"] is None
        assert corporate_facts[0]["review_required"] is True
        assert corporate_facts[0]["reason_code"] == "corporate_level_unassigned"
