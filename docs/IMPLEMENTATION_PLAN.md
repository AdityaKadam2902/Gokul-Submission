# PracticeSignal — Implementation Plan

> **Document type:** Ordered milestones, per-milestone acceptance criteria, unresolved issues.
> **Prepared:** 4 November 2026 · Phase 0.5 output. Companion to `ARCHITECTURE.md` and `docs/DECISION_LOG.md` (decisions D1–D22 referenced below).
> **Rule:** every milestone ships independently executable tests and allows a working end-to-end demo at its boundary. **M1 is a complete pre-AI vertical slice (D18); AI lands only in M2.**

---

## 1. Principles

1. Vertical before vertical polish: nothing is built that cannot soon be demonstrated through the UI.
2. Determinism first: every pipeline stage either proves its output from stored evidence or flags it for review.
3. Zero-cost enforced: dependency additions require a licence/cost note in this file's §12 table before install.
4. Database grows only as features land (D9); fixtures are never mistaken for live scrapes (D15).
5. Each milestone's acceptance tests double as its failure-scenario checklist; failing tests block gate passage.

---

## 2. M0 — Source policy & project foundation

**Objective.** Convert Phase 0.5 documentation into an executable project baseline: repo scaffold, toolchain verification, policy register complete and live, and the synthetic fixture corpus authored and schema-validated. No application logic beyond schemas/tests.

**Files/modules.** `README.md`, `.gitignore`, `.env.example`, `pyproject.toml`, `package.json`, `docs/` (this doc set), `prompts/` (versioned template stubs), `backend/tests/conftest.py` + fixture validators, `backend/tests/fixtures/*.json|html` (synthetic), `scripts/reset_db.sh`, `scripts/benchmark_ai.py` (scaffold only; runs in M5/M2 gate).

**Dependencies.** None beyond local toolchain (Python 3.12, Node 24, Ollama+`llama3.2:3b` already installed; nothing downloaded).

**Expected output.** Clean-clone repository; `pytest backend/tests/test_m0_docs_and_fixtures.py` green (filename aligned 4 Nov 2026 with the client instruction and the §11 test map, which previously conflicted with an earlier `test_m0_fixtures.py` mention); docs complete; zero live records, zero model downloads.

**Testing & acceptance criteria (M0-AC):**
1. `git clone` → venv + dependency bootstrap reproduces without any network beyond PyPI (dependency manifest is `pyproject.toml`; no separate `requirements.txt` is maintained); no API keys exist anywhere.
2. All seven `docs/` + `prompts/` template stubs exist and cross-reference `DECISION_LOG` IDs correctly.
3. Six synthetic fixtures (home/services/contact/team/vacancies/policies archetypes) validate against the documented fixture manifest schema (fields: id, archetype, intended_fields, synthesis rationale, labelled `fixture: true`).
4. `docs/SOURCE_REGISTER.md` shows zero `approved_live` rows; r7dd.com recorded `manual_fixture_pending_policy_check`.
5. Toolchain versions recorded; `scripts/reset_db.sh` is executable and idempotent.

**Failure scenarios & responses.** Robots check on r7dd returns unfetchable ⇒ stays fixture-only (D14); a licence conflict emerges for any planned dep ⇒ replace before install or document and stop; fixture content accidentally copies real site text ⇒ rewrite before commit.

---

## 3. M1 — First complete vertical slice (pre-AI)

**Objective.** Registered fixture source → policy-gated load → parsing → SQLite persistence → FastAPI → minimal React UI. **No AI anywhere in this milestone.** Demonstrable end-to-end in the browser with fixture data (D18).

**Files/modules.** `backend/app/core/` (config, logging, exceptions), `db/` (SQLite session + bootstrap for: sources, page_snapshots per D9), `models/` (Pydantic: SourceCreate/Read, SnapshotRead, OrgSummary), `sources/` (registry, policy_gate, fixture_loader), `parsers/text_extract` (heading-preserving extraction, script/style/hidden stripping, parser version stamp), `ingest/snapshot_store` (content-hash, immutability), `api/routers/sources.py|snapshot.py|health.py`, frontend minimal pages: Sources list, Snapshot detail, Health panel.

**Dependencies.** M0 complete.

**Semantic guarantees.** Un-allowlisted hosts cannot reach fetch snodes; fetched-vs-failure is separated; a fetch failure retains the prior snapshot; fixture rows visible as `fixture` in the UI; no records beyond `intended_fields` persist.

**Expected output.** Demo: register a fixture source, ingest, inspect snapshot + parsed text with heading structure and offsets, filter by freshness; docs updated.

**Testing & acceptance criteria (M1-AC):**
1. Unit: policy gate rejects external URLs on unapproved hosts **before any outbound request** (no sockets open — assertable via injected transport).
2. Unit: fetch of a registered non-fixture source with HTTPS-only enforcement hits redirect loop at cross-host hop (test doubles).
3. Unit: `text_extract` preserves heading chain; strips `<script>/<style>/hidden`; records parser_version.
4. Integration: POST `/api/sources` with `kind=fixture` → snapshot row created with hash; GET `/api/snapshots/{id}` returns parsed text + entity/field scaffolding.
5. Integration: re-ingest unchanged fixture ⇒ **hash short-circuit** prevents re-parse (D4 behavior, deterministic path); changed fixture creates a new snapshot and a `change_event` draft row (no AI yet).
6. Integration: fetch failure recorded separately from content status; prior snapshot untouched.
7. UI: fixture sources list renders with `fixture` badges; snapshot detail shows text + offsets; empty/error states implemented.
8. Manual: fresh clone → `make setup && make dev` → all acceptance flows clickable.
9. Types: `npm run generate:types` builds (D19, no hand-written types).

**Failure scenarios & responses.** Snapshot immutability violated by mutation API ⇒ schema-level constraint; policy gate drifts after fixture-added rows ⇒ regression test locking the allowlist state; CORS misconfig in dev ⇒ allowed via explicit localhost origin list only.

---

## 4. M2 — AI structured extraction + evidence verification

**Objective.** One structured AI call per changed/first-time snapshot (D4), heading scope proposals as fallback (D11), deterministic evidence verification of every proposed field (D10/D11), candidate-fact persistence with prompt-run records. Model output is always `candidate` — approved state is reached only through review in M4.

**Files/modules.** `ai/client.py` (Ollama adapter, timeouts + model tag pinning), `ai/prompt_store.py` + `prompts/*.md` (versioned classification+extraction V1, evidence-constrained V2, scope V3, vacancy V1, injection-guard V1), `ingest/evidence_verifier.py`, `ingest/field_normalizer.py` (raw-preserving on failure, D7), `ingest/scope_assignment.py`, `extraction/candidates.py` (maps verified candidates into `facts`, `prompt_runs`, `candidates`), `scripts/benchmark_ai.py` (D5 run before M2 lands), DB: `organisations` (first records), `facts`, `evidence`, `prompt_runs` tables (D9).

**Dependencies.** M1 complete; Ollama running locally with `llama3.2:3b`; no other dependency.

**Expected output.** Fixture source ingest produces audited candidates with per-field verified quotes/offsets (zero for unsupported), `needs_review` reasons present; benchmark report committed to `docs/RESULTS.md` (p50/p95 tokens/sec, JSON-validity).

**Testing & acceptance criteria (M2-AC):**
1. Unit: evidence verifier rejects every candidate whose quote is absent from the stored snapshot (probed via mutated quote strings); accepts when whitespace-normalised match found; records offsets.
2. Unit: field normalizer preserves raw value + `needs_review` for unparseable hours/phone/url/address variants; never substitutes or invents.
3. Unit: scope assignment picks fixture headings; ambiguous ⇒ `needs_review`.
4. Integration: fixture corpus end-to-end — every field classified; correct core facts extracted; unsupported fields rejected/deferred; prompt_runs row per call (model tag, prompt version, latency, JSON validity).
5. Integration: **one** AI call per changed snapshot; un-changed snapshot ⇒ zero AI calls (D4 short-circuit regression check).
6. Integration: content filters reject testimonial-sourced and off-allowlist candidates deterministically (D6/D7).
7. Benchmark: runs green below a documented CPU latency ceiling for the reference corpus (M2 gate); recorded in RESULTS.
8. Negative: corrupted snapshot (mismatched hash) refuses AI stage and surfaces failure state.
9. Feature checks: offline-Ollama ⇒ graceful extraction-unavailable state; fixture provenance remains visible; zero paid API calls logged anywhere.

**Failure scenarios & responses.** Ollama returns malformed JSON ⇒ candidates marked failed-validation without persistence; model responds with forbidden fields ⇒ allowlist rejects; problematic quote match ⇒ review flag; model down ⇒ typed unavailable state, no fabrication.

---

## 5. M3 — Intelligence

**Objective.** Conflict detection (scope-aware), duplicate detection (suggestion-only, D12), change detection + freshness states, opportunity lifecycle (`open_claimed`/`possibly_stale`/`removed_from_source`/`closed_confirmed` via explicit review review only). No new data category; works on existing orgs/services/vacancies/snapshots.

**Files/modules.** `intelligence/conflicts.py`, `intelligence/dedupe.py`, `intelligence/freshness.py`, `db` additions per D9 (opportunities, change_events), `api/routers/organisations.py|opportunities.py|changes.py`, frontend org/opportunity/change views (read-only at this milestone).

**Dependencies.** M2 complete.

**Expected output.** Duplicate suggestion panel; change history between fixture revisions; staleness markers; vacancy states; conflict display with evidence quotes for both sides.

**Testing & acceptance criteria (M3-AC):**
1. Unit: conflicts never compare fields across differing scope_keys; ambiguous scope never resolved by code alone.
2. Unit: inlet/outlet change detection marks `possibly_stale` (never `closed_confirmed` automatically).
3. Unit: normalised duplicate detection matrix produces expected suggestions on standard dupes; **suggests only**, never merges.
4. Unit: freshness: a snapshot fetched within window stays fresh; aging/stale boundaries tested as rule switches.
5. Integration: multiple-fixture scenario — differing hours at differing scopes not flagged as conflict; matching-scope mismatch flagged with both evidence excerpts.
6. Integration: change-events trigger and link journal entries between affected entities.
7. UI: change log and opportunity cards render states, fixture badges intact; no claims of live availability anywhere.
8. Demo path: unduplicated and unclosed regression test for supervised status machine on every trigger.

**Failure scenarios & responses.** Rapid-fixture dedup false-positive → shows suggestion (no merge) and bumps threshold only with test; persistence of a stale concurrency edge (change-record race) handled via single-writer ingest path.

---

## 6. M4 — Professional review dashboard + exports

**Objective.** Full reviewer workflow (approve/reject/correct/defer/merge-after-confirm) over facts, conflicts and duplicates; provenance-complete CSV/JSON exports; UI per ARCHITECTURE §11 including empty/error/offline states.

**Files/modules.** `review/workflow.py` + `review_actions` (D8), `exports/renderers.py`, `api/routers/review.py|exports.py`, frontend review queue, evidence drawer, opportunity cards, exports panel.

**Dependencies.** M3 complete.

**Expected output.** Reviewer can progress all fixture corpus entities end to end; CSV/JSON exports embed source URL, evidence excerpt + offsets, retrieval timestamp, scope, review decision and reviewer; user-visible audit trail.

**Testing & acceptance criteria (M4-AC):**
1. Unit: every review action appends an immutable audit row; corrections preserve original candidate per D8.
2. Unit: export renders model-tag, prompt-version, retrieval timestamp, review status, and per-field evidence in both CSV and JSON; row counts match filtered sets.
3. Integration: end-to-end approve/reject/defer flows for every candidate category; rejected/deferred never appear in exports.
4. Integration: approval after human correction produces a superseded status, not a silent overwrite.
5. UI: review queue lists approved/conflict/dedupe/docusign reject reasons; evidence drawer opens per fact with quote + offsets + timestamps; every screen has empty, error, offline (Ollama unreachable), and no-data states.
6. Exports: CSV opens in a spreadsheet tool without column drift; JSON validates against the exported schema.
7. No outbound communication exists anywhere in the codebase (grep-level negative test); no automated outreach buttons/stubs.
8. Types: regenerated and committed; no runtime surprises against changed endpoints.

**Failure scenarios & responses.** Export column drift caught by schema test; reviewers cannot silently alter history (audit enforced); browser session failure does not silently post duplicate review actions (idempotency key on review routes).

---

## 7. M5 — Testing, security, performance, reliability, documentation hardening

**Objective.** Hardening only — no new features. Full §10 matrix green; negative corpus (injection, multi-scope, malformed JSON, mismatches, cross-domain redirects, hidden text, lookalike organisations, testimonial contact values); fetch-security tests complete (D13 wording everywhere — no SSRF-immunity claims); benchmark refresh against final prompt set; README-driven clean-clone reproducibility; privacy-and-safety doc finalised; screenshot pack and demo script drafted.

**Files/modules.** `tests/` end-to-end, `docs/privacy-and-safety.md`, `docs/RESULTS.md` benchmark + evaluation sections, `docs/demo-script.md`, prompts final freeze, `scripts/` (benchmark, bootstrap, reset).

**Dependencies.** M4 complete.

**Testing & acceptance criteria (M5-AC):**
1. All AC rows in §10 pass in CI-style supervised run; no skips except documented (e.g., model-unavailable for benchmark in environments without Ollama — marker-explained).
2. §10 negative-corpus rows pass with message-level diagnostics.
3. Benchmark re-run and re-committed post prompt-freeze (latency p50/p95, tokens/sec, JSON validity rate).
4. Fresh-clone reproducibility from README only (no hidden steps), one-shot `make setup && make test`.
5. Code-search ids: no secret patterns, no live scraping endpoints, no client-server drifts, no model binary committed (`llama3.2:3b` referenced, never shipped); `.gitignore` covers `.env`, fixture HTML (if any), SQLite data dirs.
6. Screenshots for every section of §11 + a recorded demo script.

**Failure scenarios & responses.** Any AC red ⇒ milestone gate refuses passage (fix-first); perf regression detected ⇒ prompt/model revisit only via measured proposal (D5), not immediate swap; docs drift detected by cross-link checker.

---

## 8. M6 — Submission, demonstration, final QA

**Objective.** Deliver the four client-fixed assessment items (D17): working project, video ≥ 2 min, complete prompt documentation incl. real revisions, accessible project link. Claims audit against ARCHITECTURE §13 ("must not claim" list).

**Files/modules.** Final `README.md` (problem, users, features, architecture, honest limitations, setup, test instructions), final `docs/RESULTS.md`, `docs/prompt-engineering-log.md` (iteration narrative + rationale), project link recorded.

**Dependencies.** M5 green.

**Testing & acceptance criteria (M6-AC):**
1. All five evaluation reports + benchmarks + screenshots exist in the repo at review time (not links elsewhere).
2. Demo video ≥ 2 min shows: fixture-or-live ingestion, evidence-verified AI extraction with prompt version visible, a rejection/review case, export, and the honest "no live claims" framing (D15).
3. Claims audit: every assertion in README/video mapped to a committed artifact; no mislabeled fixtures (D15), no unsubstantiated performance numbers (D5), no SSRF-immunity wording (D13).
4. All four assessment deliverables checked off with links.
5. Tag/freeze: `v0.1.0-assessment` tagged; working tree clean except known-open §9 items.

**Failure scenarios & responses.** Encoder/host availability late in the week ⇒ export higher-quality earlier and re-record only if claims changed; last-minute feature temptation ⇒ blocked by §10 honesty gates.

---

## 9. Remaining unresolved issues (single source of truth)

| # | Issue | Owner | Blocking | Resolution path |
|---|---|---|---|---|
| U1 | r7dd.com robots.txt + terms/privacy **manual verification** outstanding (reviewer environment could not fetch the site) | human reviewer | M2 (only if transitioning to live-fetch) — not blocking fixture work | Browser check + record in SOURCE_REGISTER §3 procedure |
| U2 | PracticeSignal name collision/trademark/domain search | project owner | Only public release (M6 publish) | Search + confirm before external links |
| U3 | CPU latency ceiling acceptance value for M2 gate (both numbers fixed only after first benchmark) | engineer + client | M2-AC7 threshold | Run benchmark, agree threshold in writing |
| U4 | Final Llama Community Licence attribution wording (README + docs) | engineer | M0 docs pass | Draft per official notice text |
| U5 | Decision: whether any *additional* third-party fixture source is needed (beyond the six archetypes) | client | None (V1 works with six) | Re-open SOURCE_REGISTER §3 if so |
| U6 | Exact review-queue default reviewer identity string for audit rows | client | cosmetic | Pick default `local-operator` unless another supplied |
| U7 | Windows host port/permissions for any Ollama binding quirks | local user | None expected (Ollama locally already) | Document exact dev commands in README |

---

## 10. Acceptance criteria matrix (consolidated, project-wide)

| ID | Criterion | Pass condition | Covered in |
|---|---|---|---|
| AC-01 | Source registration | Source appears with page types, intended fields, review status | M0/M1 |
| AC-02 | Unapproved host rejection | Fetch attempt from unallowlisted host never opens a socket | M1-AC1 |
| AC-03 | HTTPS-only | HTTP from non-localhost rejected | M1-AC2 |
| AC-04 | Snapshot creation | URL, title, timestamp, content hash persisted | M1-AC4 |
| AC-05 | Failure isolation | Failed fetch preserves prior snapshot; error recorded separately | M1-AC6 |
| AC-06 | Zero-AI ingest path | Complete vertical slice without any AI dependency | M1 |
| AC-07 | Fixture labelling | `fixture` badge visible in UI, evidence drawer, exports | M1/M4 |
| AC-08 | Hash short-circuit | Unchanged snapshot ⇒ zero AI calls (D4) | M2-AC5 |
| AC-09 | One-call-per-change rule | Single AI call per changed page; prompt runs logged | M2-AC4/5 |
| AC-10 | Evidence gating | Every accepted fact has byte-verifiable quote + offsets; unsupported ⇒ reject/needs_review | M2-AC1/4 |
| AC-11 | Raw value preservation | Normalization failure keeps raw + review flag (D7) | M2-AC2 |
| AC-12 | Deterministic scope | Heading-first scope; ambiguity ⇒ review not conflict (D11) | M2-AC3, M3-AC1/5 |
| AC-13 | Conservative dedupe | Suggestions only, no auto-merge (D12) | M3-AC3 |
| AC-14 | Freshness rules | Stale/aging/unavailable states; no automatic closure — `closed_confirmed` only via human review | M3-AC2/4 |
| AC-15 | Change log | Snapshot diffs with before/after evidence and affected entities | M3-AC6 |
| AC-16 | Review auditability | Every review action is an immutable audit row retaining originals | M4-AC1 |
| AC-17 | Provenance-complete export | CSV/JSON with URL, quote/offsets, timestamp, scope, review decision, reviewer, model/prompt version | M4-AC2 |
| AC-18 | No automated outreach | No email/SMS send path can exist in the codebase (grep-verified) | M4-AC7 |
| AC-19 | No patient/private data | Off-allowlist and testimonial-context candidates rejected deterministically | M2-AC6 |
| AC-20 | Zero-cost pitch holds from a fresh clone | No paid service is needed to reproduce the demo end to end | M0-AC1, M5-AC4 |
| AC-21 | Documented SSRF limitation | docs phrase bounded posture, not total protection (D13) | M5-AC1/5 |
| AC-22 | Honest benchmark | Latency p50/p95/tokens-sec/JSON-validity measured, committed, not estimated | M2-AC7, M5-AC3 |
| AC-23 | Negative corpus | Injection probes, multi-scope tables, quote-mismatch, cross-domain redirect, hidden-text, malformed JSON outcomes enforced | M5-AC2 |
| AC-24 | Deliverables | Working project; video ≥ 2 min; full prompt documentation with real revisions; accessible link | M6 |

---

## 11. Test execution map (independently executable per milestone)

| Milestone | Command | Gates |
|---|---|---|
| M0 | `pytest backend/tests/test_m0_docs_and_fixtures.py` | M0-AC1–5 |
| M1 | `pytest backend/tests/test_m1_policies.py backend/tests/test_m1_ingest.py && cd frontend && npm run generate:types && npm run build` | M1-AC1–9 |
| M2 | `pytest backend/tests/test_m2_extraction.py backend/tests/test_m2_evidence.py && python scripts/benchmark_ai.py --output docs/results/benchmark-m2.md` | M2-AC1–9 (benchmark skips without Ollama; mark-explained) |
| M3 | `pytest backend/tests/test_m3_intelligence.py backend/tests/test_m3_api.py` | M3-AC1–8 |
| M4 | `pytest backend/tests/test_m4_review.py backend/tests/test_m4_exports.py && cd frontend && npm run build && npm run typecheck` | M4-AC1–8 |
| M5 | `pytest backend/tests -m "not model_required"` + `python scripts/benchmark_ai.py --full` + `make deploy-check` | M5-AC1–6 |
| M6 | Tagging + demo recording; deliverables checklist manual but itemised | M6-AC1–5 |

(Benchmark and Ollama-dependent tests are explicitly marked `model_required`; environments without Ollama skip them, never fail them silently.)

---

## 12. Zero-cost dependency ledger

| Dep | Version pin target | Purpose | Licence | Cost |
|---|---|---|---|---|
| fastapi + uvicorn | latest stable | API layer | MIT | 0 |
| pydantic v2 | latest stable | schemas | MIT | 0 |
| sqlalchemy | 2.x | SQLite | MIT | 0 |
| beautifulsoup4 + lxml | latest stable | parsing | MIT/BSD | 0 |
| httpx | latest stable | controlled fetch (redirects disabled) | BSD | 0 |
| rapidfuzz | latest stable | dup suggestions | MIT | 0 |
| pytest, pytest-asyncio | latest stable | tests | MIT | 0 |
| openapi-typescript | latest stable | TS generation | MIT | 0 |
| vite, react, typescript | latest stable | frontend | MIT | 0 |
| Ollama | installed 0.40.14 | local model runtime | MIT | 0 |
| llama3.2:3b | installed | extraction model | Llama Community License | 0 (already downloaded; weights not redistributed) |

No paid, no subscription, no cloud, no credit card anywhere. New dep ⇒ row here + licence note before install; paid anything ⇒ revert.
