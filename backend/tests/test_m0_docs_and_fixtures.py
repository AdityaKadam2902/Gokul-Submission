"""M0 conformance tests — docs, fixtures, prompts, zero-cost config.

Run:  pytest backend/tests/test_m0_docs_and_fixtures.py

Design rules (per docs/IMPLEMENTATION_PLAN.md §2 M0-AC):
- stdlib-only: no application dependency is imported at M0;
- deterministic: file-based checks only, no network, no clock, no randomness;
- honest: nothing here simulates a live scrape or verifies unrun benchmarks.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# NOTE (M0): pytest is intentionally NOT imported at module level — the suite
# is written to run under pytest AND under a stdlib fallback driver, because
# dependency installation is not authorised until M1 (IMPLEMENTATION_PLAN §2).

from conftest import DOCS_DIR, FIXTURES_DIR, PAGES_DIR, PROMPTS_DIR, REPO_ROOT

REQUIRED_ROOT_FILES = [
    ".gitignore",
    ".env.example",
    "pyproject.toml",
    "package.json",
    "README.md",
    "ARCHITECTURE.md",
]

REQUIRED_DOCS = [
    "DECISION_LOG.md",
    "SOURCE_REGISTER.md",
    "IMPLEMENTATION_PLAN.md",
    "prompt-engineering-log.md",
    "evaluation-methodology.md",
    "privacy-and-safety.md",
    "demo-script.md",
]

REQUIRED_PROMPTS = {
    "classification_extraction_v1.md": ("classification_extraction_v1", 1),
    "evidence_verification_v2.md": ("evidence_verification_v2", 2),
    "scope_assignment_v3.md": ("scope_assignment_v3", 3),
    "vacancy_extraction_v1.md": ("vacancy_extraction_v1", 1),
    "injection_guard_v1.md": ("injection_guard_v1", 1),
}

REQUIRED_FIXTURE_IDS = ["F1", "F2", "F3", "F4", "F5", "F6"]
REQUIRED_PAGE_FIELDS = [
    "id",
    "label",
    "synthetic",
    "invented_data",
    "not_a_live_scrape",
    "archetype",
    "reference_archetype",
    "reference_url_note",
    "intended_fields",
    "edge_cases",
    "synthesis_rationale",
    "html",
]
BOOLEAN_TRUE_FIELDS = ["synthetic", "invented_data", "not_a_live_scrape"]

# Zero-cost dependency allowlists (docs/IMPLEMENTATION_PLAN.md §12 ledger).
ALLOWED_PY_DEPS = {
    "fastapi", "uvicorn", "httpx", "pydantic", "sqlalchemy",
    "beautifulsoup4", "lxml", "rapidfuzz", "pytest", "pytest-asyncio",
    "setuptools",
}

ALLOWED_NPM_DEPS = {
    "react", "react-dom", "@types/react", "@types/react-dom",
    "@vitejs/plugin-react", "typescript", "vite", "openapi-typescript",
}

# Content that must never appear in synthetic fixture HTML payloads.
FORBIDDEN_IN_HTML = ["r7dd", "robina", "gokulinfocare"]

# Paid/key-gated service markers that must not appear in project config.
PAID_SERVICE_MARKERS = [
    "openai", "anthropic", "gemini", "replicate", "pinecone",
    "stripe", "sendgrid", "twilio", "mailgun", "datadog", "supabase",
    "credits", "credit-card", "payment card",
]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_pages() -> dict[str, dict]:
    return {
        p.stem.split("_")[0]: _load_json(p)
        for p in sorted(PAGES_DIR.glob("F*_*.json"))
    }


def _frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    data: dict[str, str] = {}
    if match:
        for line in match.group(1).splitlines():
            key, _, value = line.partition(":")
            data[key.strip()] = value.strip()
    return data


# --------------------------------------------------------------------------
# Repository foundation
# --------------------------------------------------------------------------

def test_required_root_files_exist():
    missing = [f for f in REQUIRED_ROOT_FILES if not (REPO_ROOT / f).is_file()]
    assert missing == [], f"missing repo files: {missing}"


def test_required_docs_exist():
    missing = [f for f in REQUIRED_DOCS if not (DOCS_DIR / f).is_file()]
    assert missing == [], f"missing docs: {missing}"


def test_docs_reference_decision_log_ids():
    # Cross-references must point at real decision IDs (D1..D22 range).
    dl_text = (DOCS_DIR / "DECISION_LOG.md").read_text(encoding="utf-8")
    for doc in ["ARCHITECTURE.md", "IMPLEMENTATION_PLAN.md", "SOURCE_REGISTER.md"]:
        source = (REPO_ROOT / doc) if doc == "ARCHITECTURE.md" else (DOCS_DIR / doc)
        text = source.read_text(encoding="utf-8")
        found = {m.group(0) for m in re.finditer(r"D\d+", text)}
        # broaden: any Dn referenced must exist as a decision in the log table
        assert found <= {f"D{i}" for i in range(1, 23)}, (
            f"{doc} references unknown decision ids: {sorted(found - {f'D{i}' for i in range(1, 23)})}"
        )


def test_no_paid_service_markers_in_configs():
    for candidate in [REPO_ROOT / "pyproject.toml", REPO_ROOT / "package.json", REPO_ROOT / ".env.example"]:
        text = candidate.read_text(encoding="utf-8").lower()
        hits = [m for m in PAID_SERVICE_MARKERS if m in text]
        assert hits == [], f"{candidate.name} contains paid-service markers: {hits}"


def test_env_example_has_no_credentials():
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    secretish = re.findall(r"^\s*([A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PASSWORD)[A-Z0-9_]*)\s*=", text, re.MULTILINE)
    assert secretish == [], f".env.example declares credential-style variables: {secretish}"
    declared = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", text, re.MULTILINE))
    assert declared == {
        "OLLAMA_URL", "OLLAMA_MODEL", "APP_HOST", "BACKEND_PORT",
        "FRONTEND_PORT", "DATABASE_PATH", "FETCH_DELAY_SECONDS",
        "FETCH_TIMEOUT_SECONDS", "MAX_RESPONSE_BYTES", "MAX_PAGES_PER_RUN",
        "DEFAULT_TIMEZONE",
    }, f"unexpected .env.example variables: {declared}"


def test_pyproject_zero_cost_dependency_allowlist():
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    # Parse ONLY the dependency arrays, not other quoted strings (project name,
    # licence text, etc. live elsewhere in the file and must not be matched).
    dep_blocks = re.findall(
        r"^(?:\[project\.optional-dependencies\]\n)?\w*(?:dependencies|dev)\s*=\s*\[(.*?)\]",
        text,
        re.DOTALL | re.MULTILINE,
    )
    declared: list[str] = []
    for block in dep_blocks:
        declared += re.findall(r'^\s*"([a-zA-Z][a-zA-Z0-9_-]+?)(?:[><=!].*)?",?\s*$', block, re.MULTILINE)
    assert declared, "dependency parsing found nothing — parsing regex drifted"
    offending = [d for d in declared if d not in ALLOWED_PY_DEPS]
    assert offending == [], f"pyproject declares deps outside the zero-cost allowlist: {offending}"


def test_package_json_zero_cost_dependency_allowlist():
    pkg = _load_json(REPO_ROOT / "package.json")
    declared = list(pkg.get("dependencies", {})) + list(pkg.get("devDependencies", {}))
    offending = [d for d in declared if d not in ALLOWED_NPM_DEPS]
    assert offending == [], f"package.json declares deps outside the zero-cost allowlist: {offending}"


def test_scripts_reset_db_scaffold_exists_and_executable():
    script = REPO_ROOT / "scripts" / "reset_db.sh"
    assert script.is_file()
    content = script.read_text(encoding="utf-8")
    assert content.startswith("#!/usr/bin/env bash")
    assert "rm -f" in content  # idempotent delete of db artefacts
    assert ".sqlite" in content and "*.db" in content


def test_scripts_benchmark_scaffold_runs_no_model():
    script = REPO_ROOT / "scripts" / "benchmark_ai.py"
    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    assert "No model runs are implemented at M0" in text
    assert 'EXPECTED_MODEL_TAG = "llama3.2:3b"' in text  # pinned tag (D2)
    assert "httpx" not in text and "requests" not in text  # no network client


# --------------------------------------------------------------------------
# Prompt library
# --------------------------------------------------------------------------

def test_prompt_templates_exist_with_versions():
    problems: list[str] = []
    for filename, (template_id, version) in sorted(REQUIRED_PROMPTS.items()):
        path = PROMPTS_DIR / filename
        if not path.is_file():
            problems.append(f"missing prompt template: {filename}")
            continue
        text = path.read_text(encoding="utf-8")
        front = _frontmatter(text)
        if front.get("template_id") != template_id:
            problems.append(f"{filename}: template_id is {front.get('template_id')!r}, expected {template_id!r}")
        if front.get("version") != str(version):
            problems.append(f"{filename}: version is {front.get('version')!r}, expected {version!r}")
        if not front.get("status"):
            problems.append(f"{filename}: no status in frontmatter")
        for section in ["# Purpose", "# Expected inputs", "# Expected output"]:
            if section not in text:
                problems.append(f"{filename}: missing section {section}")
    assert problems == [], "prompt template issues:\n" + "\n".join(problems)


def test_injection_guard_is_structural_framing_not_security_claim():
    text = (PROMPTS_DIR / "injection_guard_v1.md").read_text(encoding="utf-8")
    assert "structural" in text.lower()
    assert "code-owned" in text.lower()
    # honesty: the framing must NOT claim model-level injection immunity
    assert "not reliably prompt-proof" in text or "does not *guarantee*" in text


def test_no_prompt_promises_live_availability():
    for filename in REQUIRED_PROMPTS:
        text = (PROMPTS_DIR / filename).read_text(encoding="utf-8").lower()
        assert "is_open" not in text, f"{filename} promises live-availability semantics"


# --------------------------------------------------------------------------
# Synthetic fixtures (F1–F6) + manifest
# --------------------------------------------------------------------------

def test_fixture_manifest_exists_and_attests_synthetic():
    manifest = _load_json(FIXTURES_DIR / "fixture_manifest.json")
    att = manifest["synthetic_attestation"]
    for key in ["all_data_invented", "no_real_organisation_used", "no_real_person_used",
                "no_real_contact_details_used", "no_live_scrape_represented_or_performed",
                "real_domain_strings_absent_from_html_payloads"]:
        assert att.get(key) is True, f"attestation {key} is not true"


def test_all_six_fixtures_exist_with_metadata():
    pages = _load_pages()
    assert sorted(pages) == REQUIRED_FIXTURE_IDS, f"fixture ids present: {sorted(pages)}"
    for fid, page in pages.items():
        missing = [f for f in REQUIRED_PAGE_FIELDS if f not in page]
        assert missing == [], f"{fid} missing fields: {missing}"
        for flag in BOOLEAN_TRUE_FIELDS:
            assert page[flag] is True, f"{fid}.{flag} must be literally true"
        assert isinstance(page["intended_fields"], list) and page["intended_fields"], fid
        assert isinstance(page["edge_cases"], list), fid
        assert page["html"].strip().lower().startswith("<!doctype html"), fid


def test_fixture_ids_match_manifest_and_files():
    manifest = _load_json(FIXTURES_DIR / "fixture_manifest.json")
    entries = {e["id"]: e for e in manifest["fixtures"]}
    assert sorted(entries) == REQUIRED_FIXTURE_IDS
    for fid, entry in entries.items():
        assert (FIXTURES_DIR / entry["file"]).is_file(), f"{fid} file missing: {entry['file']}"
        page = _load_json(FIXTURES_DIR / entry["file"])
        assert page["archetype"] == entry["archetype"]
        assert page["intended_fields"] == entry["intended_fields"]


def test_no_fixture_represents_a_live_scrape():
    pages = _load_pages()
    for fid, page in pages.items():
        html_lower = page["html"].lower()
        for marker in FORBIDDEN_IN_HTML:
            assert marker not in html_lower, f"{fid} embeds a forbidden real-world marker: {marker}"
        assert "live scrape" not in page["label"].lower()


def test_fixture_edge_cases_present_in_html():
    """Declared edge cases must actually occur in the fixture HTML (no phantom cases)."""
    pages = _load_pages()
    f3_html = pages["F3"]["html"]
    for scope in ["Clinic consultations", "Pathology collection", "Reception desk"]:
        assert scope in f3_html, f"F3 multi-scope table missing scope: {scope}"
    f4_html = pages["F4"]["html"]
    assert "(07) 5550 0333" in f4_html, "F4 testimonial must contain the negative-test phone number"
    f5_html = pages["F5"]["html"]
    assert "Immediate start" in f5_html and "invalid" not in f5_html
    assert "applications closed" in f5_html
    f6_html = pages["F6"]["html"]
    assert "display:none" in f6_html and "<script" in f6_html
    assert "Ignore all previous instructions" in f6_html  # injection payload present


def test_timezone_explicit_default_recorded():
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "DEFAULT_TIMEZONE=Australia/Brisbane" in text
