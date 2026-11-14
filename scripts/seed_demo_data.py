"""Seed realistic, rich demonstration data into PracticeSignal SQLite database.

This script populates all tables required for a full end-to-end demonstration:
- Registered Sources & Page Snapshots with grounded character offsets
- Verified Candidate Facts & AI Prompt Run audit records
- Canonical Healthcare Organisations & Source Links
- Organisation Match Candidates (Tier-1 high confidence)
- Cross-Source Factual Discrepancies (conflicting telephone numbers)
- Healthcare Workforce Recruitment Opportunities (GP vacancy)
- Practice Lifecycle Events (Permanent Branch Closure & Corporate Acquisition)
- Completed Review Actions (Audit History entry)
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.app.db.base import get_database_file, get_engine, init_db
from backend.app.db.orm import (
    Base,
    ChangeEvent,
    Discrepancy,
    Evidence,
    Fact,
    Opportunity,
    Organisation,
    OrganisationMatchCandidate,
    OrganisationSourceLink,
    PageSnapshot,
    PracticeLifecycleEvent,
    PromptRun,
    ReviewAction,
    Source,
    SourceObservation,
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def seed():
    engine = get_engine()
    init_db(engine)

    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)
    two_days_ago = now - timedelta(days=2)

    with Session(engine) as session:
        print("[seed] Clearing existing runtime demo data...")
        # Clean in reverse topological order
        session.execute(delete(ReviewAction))
        session.execute(delete(PracticeLifecycleEvent))
        session.execute(delete(Opportunity))
        session.execute(delete(ChangeEvent))
        session.execute(delete(Discrepancy))
        session.execute(delete(OrganisationMatchCandidate))
        session.execute(delete(SourceObservation))
        session.execute(delete(OrganisationSourceLink))
        session.execute(delete(Fact))
        session.execute(delete(Evidence))
        session.execute(delete(PromptRun))
        session.execute(delete(PageSnapshot))
        session.execute(delete(Source))
        session.execute(delete(Organisation))
        session.commit()

        print("[seed] Creating canonical healthcare organisations...")
        org_stjude = Organisation(
            name="St Jude Medical Centre",
            slug="st-jude-medical-centre",
            primary_domain="stjudemedical.com.au",
            phone="(03) 9419 8888",
            address="Suite 102, 12-14 Nelson Road",
            suburb="Box Hill",
            postcode="3128",
            state="VIC",
            status="active",
            created_at=two_days_ago,
            updated_at=now,
        )
        org_boxhill = Organisation(
            name="Box Hill Family Practice",
            slug="box-hill-family-practice",
            primary_domain="boxhillfamilypractice.com.au",
            phone="(03) 9890 1234",
            address="450 Station Street",
            suburb="Box Hill",
            postcode="3128",
            state="VIC",
            status="active",
            created_at=two_days_ago,
            updated_at=now,
        )
        org_melbmetro = Organisation(
            name="Melbourne Metro Healthcare",
            slug="melbourne-metro-healthcare",
            primary_domain="melbournemetrohealth.com.au",
            phone="(03) 9347 5555",
            address="200 Lygon Street",
            suburb="Carlton",
            postcode="3053",
            state="VIC",
            status="active",
            created_at=two_days_ago,
            updated_at=now,
        )
        session.add_all([org_stjude, org_boxhill, org_melbmetro])
        session.commit()

        print("[seed] Registering public healthcare sources...")
        src_stjude_web = Source(
            kind="url",
            label="St Jude Medical Centre — Official Website",
            url="https://stjudemedical.com.au/contact",
            domain="stjudemedical.com.au",
            page_type="contact",
            policy_status="allowed",
            robots_status="allowed",
            intended_fields=["phone", "address", "hours", "abn"],
            approval_note="Verified official clinic contact page",
            reviewed_by="admin_evaluator",
            created_at=two_days_ago,
            updated_at=now,
        )
        src_directory = Source(
            kind="url",
            label="AusHealth Directory — St Jude Box Hill Profile",
            url="https://aushealthdirectory.com.au/clinics/st-jude-box-hill",
            domain="aushealthdirectory.com.au",
            page_type="directory_listing",
            policy_status="allowed",
            robots_status="allowed",
            intended_fields=["phone", "address"],
            approval_note="Public healthcare directory aggregator",
            reviewed_by="admin_evaluator",
            created_at=two_days_ago,
            updated_at=now,
        )
        src_notices = Source(
            kind="url",
            label="Box Hill Family Practice — Practice Notices & Relocation",
            url="https://boxhillfamilypractice.com.au/notices",
            domain="boxhillfamilypractice.com.au",
            page_type="announcements",
            policy_status="allowed",
            robots_status="allowed",
            intended_fields=["closure_notice", "relocation_address"],
            approval_note="Official practice operational announcements",
            reviewed_by="admin_evaluator",
            created_at=two_days_ago,
            updated_at=now,
        )
        src_careers = Source(
            kind="url",
            label="Melbourne Metro Healthcare — Vacancies & Careers Portal",
            url="https://melbournemetrohealth.com.au/careers/gp-vacancies",
            domain="melbournemetrohealth.com.au",
            page_type="careers",
            policy_status="allowed",
            robots_status="allowed",
            intended_fields=["role_title", "role_category", "remuneration"],
            approval_note="Healthcare group recruitment portal",
            reviewed_by="admin_evaluator",
            created_at=two_days_ago,
            updated_at=now,
        )
        session.add_all([src_stjude_web, src_directory, src_notices, src_careers])
        session.commit()

        print("[seed] Creating immutable snapshots with grounded text...")
        # Snapshot 1: St Jude Official
        snap1_text = (
            "Welcome to St Jude Medical Centre Box Hill.\n\n"
            "We provide comprehensive primary healthcare and GP services to the Box Hill community.\n\n"
            "Contact Details:\n"
            "Telephone: (03) 9419 8888\n"
            "Facsimile: (03) 9419 8889\n"
            "Address: Suite 102, 12-14 Nelson Road, Box Hill VIC 3128\n\n"
            "Operating Hours:\n"
            "Monday to Friday: 8:00 AM – 6:00 PM\n"
            "Saturday: 9:00 AM – 1:00 PM\n"
            "Sunday: Closed\n\n"
            "Practice Registration & ABN:\n"
            "Australian Business Number (ABN): 45 123 456 789\n"
            "Accreditation: AGPAL Accredited General Practice."
        )
        snap1 = PageSnapshot(
            source_id=src_stjude_web.id,
            fetched_at=yesterday,
            title="Contact & Practice Details — St Jude Medical Centre",
            text=snap1_text,
            content_hash=_sha256(snap1_text),
            parser_version="html_clean_v2",
            headings=[
                {"level": 1, "text": "Welcome to St Jude Medical Centre Box Hill", "start_offset": 0, "end_offset": 43},
                {"level": 2, "text": "Contact Details", "start_offset": 137, "end_offset": 152},
                {"level": 2, "text": "Operating Hours", "start_offset": 274, "end_offset": 289},
                {"level": 2, "text": "Practice Registration & ABN", "start_offset": 373, "end_offset": 400},
            ],
        )

        # Snapshot 2: AusHealth Directory
        snap2_text = (
            "AusHealth Directory — Victorian Clinics Directory\n\n"
            "Practice Name: St Jude Medical Centre\n"
            "Specialties: General Practice, Pathology Collection, Allied Health\n"
            "Location: Box Hill VIC 3128\n"
            "Telephone: (03) 9419 7777\n"
            "Direct enquiries to practice administration."
        )
        snap2 = PageSnapshot(
            source_id=src_directory.id,
            fetched_at=yesterday,
            title="St Jude Medical Centre — AusHealth Directory Listing",
            text=snap2_text,
            content_hash=_sha256(snap2_text),
            parser_version="html_clean_v2",
            headings=[
                {"level": 1, "text": "AusHealth Directory — Victorian Clinics Directory", "start_offset": 0, "end_offset": 50},
            ],
        )

        # Snapshot 3: Box Hill Family Practice Notices (Closure & Acquisition)
        snap3_text = (
            "Box Hill Family Practice — Important Operational Notices\n\n"
            "Notice of Permanent Branch Closure:\n"
            "Please be advised that our Box Hill branch at 450 Station Street will permanently cease clinical operations on 31 December 2026. Services will be consolidated into our newly expanded Carlton Clinic at 200 Lygon Street. All patient medical records remain securely held.\n\n"
            "Corporate Acquisition Announcement:\n"
            "Melbourne Metro Healthcare has formally acquired clinical operations of Box Hill Family Practice effective Q4 2026. All existing clinical staff will transition to the Carlton Medical Precinct."
        )
        snap3 = PageSnapshot(
            source_id=src_notices.id,
            fetched_at=yesterday,
            title="Practice Closure & Transition Notices — Box Hill Family Practice",
            text=snap3_text,
            content_hash=_sha256(snap3_text),
            parser_version="html_clean_v2",
            headings=[
                {"level": 1, "text": "Box Hill Family Practice — Important Operational Notices", "start_offset": 0, "end_offset": 56},
                {"level": 2, "text": "Notice of Permanent Branch Closure", "start_offset": 58, "end_offset": 92},
                {"level": 2, "text": "Corporate Acquisition Announcement", "start_offset": 366, "end_offset": 401},
            ],
        )

        # Snapshot 4: Careers Portal
        snap4_text = (
            "Melbourne Metro Healthcare — Workforce Opportunities & Vacancies\n\n"
            "Position: VR General Practitioner (Full-Time or Part-Time)\n"
            "Reference ID: VAC-2026-GP04\n"
            "Remuneration: $350,000 package with 70% of billings\n"
            "Location: Carlton Medical Clinic, Melbourne\n"
            "Description: Seeking an experienced VR GP to join our high-volume multi-disciplinary health practice. Flexible sessions, dedicated nursing support, and modern facilities provided."
        )
        snap4 = PageSnapshot(
            source_id=src_careers.id,
            fetched_at=yesterday,
            title="Healthcare Vacancies — Melbourne Metro Healthcare",
            text=snap4_text,
            content_hash=_sha256(snap4_text),
            parser_version="html_clean_v2",
            headings=[
                {"level": 1, "text": "Melbourne Metro Healthcare — Workforce Opportunities & Vacancies", "start_offset": 0, "end_offset": 65},
                {"level": 2, "text": "Position: VR General Practitioner (Full-Time or Part-Time)", "start_offset": 67, "end_offset": 126},
            ],
        )

        session.add_all([snap1, snap2, snap3, snap4])
        session.commit()

        print("[seed] Creating grounded evidence quotes with exact string slicing...")
        # Helper to compute exact character offsets
        def make_evidence(snap: PageSnapshot, quote: str) -> Evidence:
            start = snap.text.find(quote)
            if start == -1:
                raise ValueError(f"Quote not found in snapshot text: {quote!r}")
            end = start + len(quote)
            return Evidence(
                snapshot_id=snap.id,
                start_offset=start,
                end_offset=end,
                quote=quote,
                captured_at=yesterday,
                match_algorithm_version="py_subslice_v1",
            )

        ev_phone_stjude = make_evidence(snap1, "(03) 9419 8888")
        ev_addr_stjude = make_evidence(snap1, "Suite 102, 12-14 Nelson Road, Box Hill VIC 3128")
        ev_hours_stjude = make_evidence(snap1, "Monday to Friday: 8:00 AM – 6:00 PM\nSaturday: 9:00 AM – 1:00 PM")
        ev_abn_stjude = make_evidence(snap1, "45 123 456 789")

        ev_phone_dir = make_evidence(snap2, "(03) 9419 7777")

        ev_closure = make_evidence(
            snap3,
            "Please be advised that our Box Hill branch at 450 Station Street will permanently cease clinical operations on 31 December 2026. Services will be consolidated into our newly expanded Carlton Clinic at 200 Lygon Street. All patient medical records remain securely held."
        )
        ev_acq = make_evidence(
            snap3,
            "Melbourne Metro Healthcare has formally acquired clinical operations of Box Hill Family Practice effective Q4 2026."
        )

        ev_vacancy = make_evidence(
            snap4,
            "Position: VR General Practitioner (Full-Time or Part-Time)\nReference ID: VAC-2026-GP04\nRemuneration: $350,000 package with 70% of billings"
        )

        session.add_all([
            ev_phone_stjude,
            ev_addr_stjude,
            ev_hours_stjude,
            ev_abn_stjude,
            ev_phone_dir,
            ev_closure,
            ev_acq,
            ev_vacancy,
        ])
        session.commit()

        print("[seed] Recording AI prompt audit logs...")
        pr1 = PromptRun(
            snapshot_id=snap1.id,
            model_tag="llama3.2:3b",
            prompt_version="classification_extraction_v1",
            request_payload_hash=_sha256("req_payload_snap1"),
            raw_response_ref="{\"phone\": \"(03) 9419 8888\", \"address\": \"Suite 102, 12-14 Nelson Road, Box Hill VIC 3128\"}",
            parse_ok=True,
            validation_errors=[],
            latency_ms=412,
            prompt_eval_count=340,
            eval_count=98,
            tokens_per_second=46.2,
            evidence_checks={"verified": 4, "unverified": 0},
            created_at=yesterday,
        )
        pr2 = PromptRun(
            snapshot_id=snap2.id,
            model_tag="llama3.2:3b",
            prompt_version="classification_extraction_v1",
            request_payload_hash=_sha256("req_payload_snap2"),
            raw_response_ref="{\"phone\": \"(03) 9419 7777\"}",
            parse_ok=True,
            validation_errors=[],
            latency_ms=385,
            prompt_eval_count=210,
            eval_count=45,
            tokens_per_second=48.1,
            evidence_checks={"verified": 1, "unverified": 0},
            created_at=yesterday,
        )
        session.add_all([pr1, pr2])
        session.commit()

        print("[seed] Extracting candidate facts...")
        fact_phone_stjude = Fact(
            organisation_id=org_stjude.id,
            snapshot_id=snap1.id,
            prompt_run_id=pr1.id,
            field_key="phone",
            scope_key="reception",
            raw_value="(03) 9419 8888",
            normalized_value="+61394198888",
            evidence_id=ev_phone_stjude.id,
            model_tag="llama3.2:3b",
            prompt_version="classification_extraction_v1",
            via="ai_extraction",
            status="verified",
            created_at=yesterday,
        )
        fact_addr_stjude = Fact(
            organisation_id=org_stjude.id,
            snapshot_id=snap1.id,
            prompt_run_id=pr1.id,
            field_key="address",
            scope_key="primary_site",
            raw_value="Suite 102, 12-14 Nelson Road, Box Hill VIC 3128",
            normalized_value="Suite 102, 12-14 Nelson Road, Box Hill VIC 3128",
            evidence_id=ev_addr_stjude.id,
            model_tag="llama3.2:3b",
            prompt_version="classification_extraction_v1",
            via="ai_extraction",
            status="verified",
            created_at=yesterday,
        )
        fact_hours_stjude = Fact(
            organisation_id=org_stjude.id,
            snapshot_id=snap1.id,
            prompt_run_id=pr1.id,
            field_key="hours",
            scope_key="general_clinic",
            raw_value="Monday to Friday: 8:00 AM – 6:00 PM, Saturday: 9:00 AM – 1:00 PM",
            normalized_value="Mo-Fr 08:00-18:00; Sa 09:00-13:00",
            evidence_id=ev_hours_stjude.id,
            model_tag="llama3.2:3b",
            prompt_version="classification_extraction_v1",
            via="ai_extraction",
            status="candidate",
            created_at=yesterday,
        )
        fact_abn_stjude = Fact(
            organisation_id=org_stjude.id,
            snapshot_id=snap1.id,
            prompt_run_id=pr1.id,
            field_key="abn",
            scope_key="corporate",
            raw_value="45 123 456 789",
            normalized_value="45123456789",
            evidence_id=ev_abn_stjude.id,
            model_tag="llama3.2:3b",
            prompt_version="classification_extraction_v1",
            via="ai_extraction",
            status="verified",
            created_at=yesterday,
        )
        fact_phone_dir = Fact(
            organisation_id=org_stjude.id,
            snapshot_id=snap2.id,
            prompt_run_id=pr2.id,
            field_key="phone",
            scope_key="reception",
            raw_value="(03) 9419 7777",
            normalized_value="+61394197777",
            evidence_id=ev_phone_dir.id,
            model_tag="llama3.2:3b",
            prompt_version="classification_extraction_v1",
            via="ai_extraction",
            status="candidate",
            created_at=yesterday,
        )
        session.add_all([
            fact_phone_stjude,
            fact_addr_stjude,
            fact_hours_stjude,
            fact_abn_stjude,
            fact_phone_dir,
        ])
        session.commit()

        print("[seed] Creating organisation source links and observation heartbeats...")
        link1 = OrganisationSourceLink(
            organisation_id=org_stjude.id,
            source_id=src_stjude_web.id,
            relationship_type="official_practice_site",
            verification_status="confirmed",
            verified_by="admin_evaluator",
            verified_at=yesterday,
            created_at=two_days_ago,
        )
        link2 = OrganisationSourceLink(
            organisation_id=org_stjude.id,
            source_id=src_directory.id,
            relationship_type="third_party_directory",
            verification_status="suggested",
            created_at=two_days_ago,
        )
        link3 = OrganisationSourceLink(
            organisation_id=org_boxhill.id,
            source_id=src_notices.id,
            relationship_type="official_practice_site",
            verification_status="confirmed",
            verified_by="admin_evaluator",
            verified_at=yesterday,
            created_at=two_days_ago,
        )
        link4 = OrganisationSourceLink(
            organisation_id=org_melbmetro.id,
            source_id=src_careers.id,
            relationship_type="careers_portal",
            verification_status="confirmed",
            verified_by="admin_evaluator",
            verified_at=yesterday,
            created_at=two_days_ago,
        )
        session.add_all([link1, link2, link3, link4])

        obs1 = SourceObservation(
            source_id=src_stjude_web.id,
            observed_at=yesterday,
            outcome="success",
            snapshot_id=snap1.id,
            content_hash=snap1.content_hash,
            content_changed=True,
            parsing_completeness="complete",
            extraction_completeness="complete",
            character_count=len(snap1.text),
            observation_sequence=1,
            created_at=yesterday,
        )
        obs2 = SourceObservation(
            source_id=src_directory.id,
            observed_at=yesterday,
            outcome="success",
            snapshot_id=snap2.id,
            content_hash=snap2.content_hash,
            content_changed=True,
            parsing_completeness="complete",
            extraction_completeness="complete",
            character_count=len(snap2.text),
            observation_sequence=1,
            created_at=yesterday,
        )
        obs3 = SourceObservation(
            source_id=src_notices.id,
            observed_at=yesterday,
            outcome="success",
            snapshot_id=snap3.id,
            content_hash=snap3.content_hash,
            content_changed=True,
            parsing_completeness="complete",
            extraction_completeness="complete",
            character_count=len(snap3.text),
            observation_sequence=1,
            created_at=yesterday,
        )
        obs4 = SourceObservation(
            source_id=src_careers.id,
            observed_at=yesterday,
            outcome="success",
            snapshot_id=snap4.id,
            content_hash=snap4.content_hash,
            content_changed=True,
            parsing_completeness="complete",
            extraction_completeness="complete",
            character_count=len(snap4.text),
            observation_sequence=1,
            created_at=yesterday,
        )
        session.add_all([obs1, obs2, obs3, obs4])
        session.commit()

        print("[seed] Creating Organisation Match Candidate for review queue...")
        match_cand = OrganisationMatchCandidate(
            source_id=src_directory.id,
            suggested_organisation_id=org_stjude.id,
            match_tier="high_confidence",
            raw_metric_score=0.92,
            evidence_signals={
                "name_similarity": 0.96,
                "domain_match": False,
                "locality_match": True,
                "postcode_match": True,
            },
            rationale="High lexical name similarity (96%) and exact suburb/postcode match (Box Hill VIC 3128).",
            status="suggested",
            created_at=yesterday,
        )
        session.add(match_cand)

        print("[seed] Creating Factual Discrepancy (phone conflict) for review queue...")
        # Note: ck_discrepancy_canonical_fact_order enforces fact_a_id < fact_b_id
        fa_id = min(fact_phone_stjude.id, fact_phone_dir.id)
        fb_id = max(fact_phone_stjude.id, fact_phone_dir.id)
        discrepancy = Discrepancy(
            organisation_id=org_stjude.id,
            field_key="phone",
            scope_key="reception",
            fact_a_id=fa_id,
            fact_b_id=fb_id,
            conflict_type="contact_number_conflict",
            status="detected",
            rationale="Official practice website reports (03) 9419 8888, whereas AusHealth Directory reports (03) 9419 7777. Direct numeric conflict requires operator resolution.",
            detected_at=now,
            created_at=now,
        )
        session.add(discrepancy)

        print("[seed] Creating Workforce Recruitment Opportunity for review queue...")
        opportunity = Opportunity(
            organisation_id=org_melbmetro.id,
            source_id=src_careers.id,
            first_snapshot_id=snap4.id,
            last_snapshot_id=snap4.id,
            role_category="general_practitioner",
            role_title="VR General Practitioner (Full-Time or Part-Time)",
            verbatim_wording="Position: VR General Practitioner (Full-Time or Part-Time)\nReference ID: VAC-2026-GP04\nRemuneration: $350,000 package with 70% of billings\nLocation: Carlton Medical Clinic",
            contact_channel="careers@melbournemetrohealth.com.au",
            explicit_ad_id="VAC-2026-GP04",
            status="possibly_stale",
            closure_evidence_quote=None,
            consecutive_absences=0,
            first_seen_at=yesterday,
            last_seen_at=now,
            created_at=yesterday,
            updated_at=now,
        )
        session.add(opportunity)

        print("[seed] Creating Practice Lifecycle Events (Closure & Acquisition) for review queue...")
        lifecycle_closure = PracticeLifecycleEvent(
            organisation_id=org_boxhill.id,
            branch_locality="Box Hill",
            is_branch_level=True,
            event_type="permanent_closure",
            status="confirmed_closed",
            source_id=src_notices.id,
            snapshot_id=snap3.id,
            evidence_id=ev_closure.id,
            evidence_quote=ev_closure.quote,
            effective_date="2026-12-31",
            date_precision="exact",
            old_value="Operating at 450 Station Street, Box Hill",
            new_value="Permanently closed from 31 December 2026; consolidated to Carlton Clinic",
            rationale="Official notice explicitly announces permanent closure of the Box Hill branch on 31 December 2026 with clinical consolidation to Carlton.",
            metadata_json={
                "closure_type": "branch_consolidation",
                "target_branch": "200 Lygon Street, Carlton",
                "patient_records_retained": True,
            },
            review_status="pending",
            idempotency_key="plc_boxhill_closure_20261231",
            detected_at=now,
            created_at=now,
            updated_at=now,
        )
        lifecycle_acq = PracticeLifecycleEvent(
            organisation_id=org_boxhill.id,
            acquiring_organisation_id=org_melbmetro.id,
            acquiring_entity_name="Melbourne Metro Healthcare",
            branch_locality=None,
            is_branch_level=False,
            event_type="acquisition",
            status="announced",
            source_id=src_notices.id,
            snapshot_id=snap3.id,
            evidence_id=ev_acq.id,
            evidence_quote=ev_acq.quote,
            effective_date="2026-10-01",
            date_precision="quarter",
            old_value="Independent Practice: Box Hill Family Practice",
            new_value="Corporate Subsidiary of Melbourne Metro Healthcare",
            rationale="Notice explicitly states Melbourne Metro Healthcare has formally acquired clinical operations effective Q4 2026.",
            metadata_json={
                "transaction_type": "full_clinical_acquisition",
                "acquiring_group": "Melbourne Metro Healthcare",
            },
            review_status="pending",
            idempotency_key="plc_boxhill_acq_melbmetro_2026q4",
            detected_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add_all([lifecycle_closure, lifecycle_acq])

        print("[seed] Creating completed Review Action for Audit History tab...")
        audit_action = ReviewAction(
            entity_type="fact",
            entity_id=fact_abn_stjude.id,
            action="verified",
            previous_status="candidate",
            new_status="verified",
            reason_code="manual_registry_crosscheck",
            reviewer="Aditya Kadam (Lead Operator)",
            notes="Cross-checked Australian Business Register (ABR) against ABN 45 123 456 789. Verified active registration under St Jude Medical Centre Pty Ltd.",
            evidence_id=ev_abn_stjude.id,
            decision_metadata={"registry": "ABR", "verified_entity": "St Jude Medical Centre Pty Ltd"},
            created_at=yesterday,
        )
        session.add(audit_action)

        session.commit()
        print("[seed] Demonstration database successfully seeded with all realistic data!")


if __name__ == "__main__":
    seed()
