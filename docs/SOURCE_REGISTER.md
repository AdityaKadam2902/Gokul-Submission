# PracticeSignal — Source Register

> **Document type:** Single source of truth for source approval, restrictions and verification status (per ARCHITECTURE §3 and DECISION_LOG D14/D15/D17).
> **Prepared:** 4 November 2026 · Version 1.0
> Change-control: source rows are added/edited via the procedure in §3, never silently; each edit is dated and attributed.

---

## 1. Policy states

| State | Meaning | Permitted collection |
|---|---|---|
| `approved_live` | Robots + terms/privacy manually reviewed, relevant data categories declared, named approver, review date recorded | **Automated fetch** within approved scope |
| `manual_fixture_pending_policy_check` | Relevance is plausible, policy verification not yet completed | **Synthetic fixtures only — no automated crawling** |
| `blocked` | Checked and not permitted (robots/ToS/blocking/explicit denial) | No ingestion; prior data retained for audit per retention rules |
| `unavailable` | Previously approved but currently responding 403/429/auth-wall/CAPTCHA | No automated fetch; mention in change log with prior data retained |

**Hard rule:** live fetching of any URL requires its row to be in state `approved_live`. Fixture ingestion never contacts the network.

---

## 2. Registered sources

| # | Organisation / page URL | Page types | Intended fields | Policy state | Robots.txt | Terms / privacy review | Approver / date | Notes |
|---|---|---|---|---|---|---|---|---|
| S1 | Robina 7 Day Doctors — https://r7dd.com/ (home, /services/, /contact/, /team/, /vacancies/, /policies/) | home, services, contact, team, vacancies, other | practice name · public address · official website · public practice phone/email · service names/categories · GP/RN/EN vacancy text (published title, location, stated employment type, published quals wording) | `manual_fixture_pending_policy_check` | **UNVERIFIED** — reviewer environment could not retrieve robots.txt; manual browser check outstanding | Not reviewed | — | Reference structure for the synthetic fixture corpus (structure only; no real page content stored in repo). Booking link via Setmore (external) is noted contextually and **not** collected — no appointment system is built. |
| S2+ | — | — | — | — | none registered | — | — | Future rows added per §3 only. |

**Fixture-mapping table** (synthetic → real reference URL recorded for transparency; recorded data stays synthetic):

| Fixture ID | Models page structure | Reference URL | Permission basis | Content origin |
|---|---|---|---|---|
| F1–F6 (home / services / contact / team / vacancies / policies-style pages) | S1 page archetypes | https://r7dd.com/... per docs/ARCHITECTURE §3.2 | Provenance note only — not a content licence from the site | Authored synthetic content reproducing the archetype features: multi-scope hours tables, vacancy wording, testimonial-like text for negative tests, injection probes, missing/malformed variants |

---

## 3. Future-source approval procedure (mandatory for every new source)

1. **Candidate occurs** — operator proposes organisation + page URLs, reason for relevance, intended data categories (mapped to the V1 field allowlist in ARCHITECTURE §4), and a provisional page-type per URL.
2. **Preconditions** — page must be publicly accessible without login/payment; must not be patient-portals, login walls, social platforms, search-engine result aggregators or third-party directory mirrors (ARCHITECTURE §4 hard scope).
3. **Robots.txt check** — retrieve the live `robots.txt`; per relevant paths record verdict: `allowed` / `disallowed` / `unavailable` (server failure or empty reply); human records the interpretation in this register, dated.
4. **Terms / privacy review** — human reads the site's terms of use, copyright/notice and privacy statement where they may apply to automated access or reuse of content; record the outcome verbatim or factually — do not paraphrase away restrictions; note publication/licence statements if any.
5. **Data-category gate** — confirm the target pages carry **organisation/vacancy-level** public data only; if the page is predominantly personal/free-form (patient reviews, clinician-by-name directories), register as `blocked` for V1 instead.
6. **Decision** — the reviewing operator records: state (`approved_live` / `blocked` / stays `manual_fixture_pending_policy_check`), approve date, reviewer name, review comment. Only `approved_live` rows become fetchable by the application, and only for hosts/paths actually inspected.
7. **Verification entry** — the reference app performs its first controlled fetch only after registration; the first fetch's policy-gate logs and snapshot are spot-checked by a human before it is left in the natural workflow.
8. **Re-review** — state, robots and terms re-verified on any known policy change, and no later than 90 days since last verification for continuously scraped sources; sources never re-verified lapse to `unavailable`.

**Escalation:** any doubt over permissions ⇒ keep the source out of live scraping; propose `fixture` mode and, if genuinely needed by the product for assessment purposes only, surface the go/no-go question to the project owner — the app itself never auto-decides.

---

## 4. Restrictions & rules this register enforces application-side

1. Ingestion requests must originate only from this register's rows (`S2+`) — no fetch of unregistered hosts, ever, unconditionally, at any code path.
2. For rows in a state other than `approved_live`, even manual data entry via the app UI is rejected from persistence unless the record explicitly records `fixture` provenance.
3. `robots.txt` is presumed restrictive when status is `unavailable` — treat as disallowed until resolved (D13 conservative posture).
4. Contact-typed fields are accepted only where the value is **published for business contact** (ARCHITECTURE §4); contact-like values found in testimonial/review context are rejected at ingest by deterministic code, never by model judgement.
5. This register is version-locked documentation — changes to states require an audit trail entry; bulk silent edits to this file are rejected by reviewer convention.

---

## 5. Current verification status summary

| Check | Status |
|---|---|
| Live automated scraping authorised | **None** (zero sources currently fetchable live) |
| Approved fixture corpus | F1–F6 (6 synthetic pages) — structure mirrors S1 archetypes; content synthetic; tracking notes the reference URLs only |
| Blocking evidence collected | None required yet |
| Outstanding manual verification | r7dd.com robots.txt + terms/privacy review (browser-based, human-only) — gate for flipping S1 to `approved_live` |
| Robots-tool available | yes (`urllib.robotparser` via stdlib), pending first real use |

> Provenance labels never change silently: a fixture-only row becomes `approved_live` only after the §3 seven-step approval, recorded with date and approver.

---

## 6. What this register is not

- Not a statement of public-domain export for scraped or mirrored content (the application ingests **synthetic** fixtures only, until state changes).
- Not a licence grant for any third-party name, logo, brand or content — PracticeSignal is an independent, non-affiliated prototype; brand references occur only as relevant-context entity examples.
- Not a clearinghouse for "is it legal to scrape X" — for actual commercial automation the organisation must perform per-source privacy/legal review (ARCHITECTURE §4 note).
