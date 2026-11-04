# PracticeSignal — Demo Script (planning skeleton)

> **Document type:** Plan for the assessment's video deliverable (≥ 2 minutes, DECISION_LOG D17).
> **Status:** M0 planning only. Nothing here may claim behaviour that does not yet exist at recording time; sections are recorded only when their pipeline milestone is demonstrable.

## Recording rules

1. The video is recorded **after** M4 working functionality exists.
2. Every claim in the video must map to a committed artifact (DECISION_LOG D17; ARCHITECTURE §13 audit list).
3. Fixture usage is stated openly on screen ("synthetic fixture data"), never presented as a live crawl (D15).
4. No performance numbers are quoted unless read from `docs/RESULTS.md` (D5).
5. No affiliation claim: independent prototype, not Gokul/MIRA.

## Planned sequence (≈ 2:15 target)

| # | Segment | Planned seconds | Gate |
|---|---|---|---|
| 1 | Problem framing: practice/vacancy info is scattered; manual collection loses source + freshness | 15 | M1+ |
| 2 | Pipeline walkthrough: register source (fixture-labelled) → snapshot → parsed text | 25 | M1 |
| 3 | AI contribution: local extraction showing prompt version + evidence quotes + candidates-not-facts | 30 | M2 |
| 4 | Reliability: an unsupported field rejected; a scope-aware review; a change event → possibly_stale | 30 | M3 |
| 5 | Review + export: approve flow with audit trail; provenance-complete CSV/JSON export | 25 | M4 |
| 6 | Engineering summary: local model, SQLite, no paid services, structural injection defence, honest fetch limits | 10 | M5 |
| 7 | Close: what a production version would need (source permissions, privacy review) | 10 | — |

## A reminder to the recording author

Do not demo anything not visible in the repo at recording time. If a milestone slipped, adapt the script to what exists — honest shorter video beats claimed functionality.
