#!/usr/bin/env python3
"""PracticeSignal — local model benchmark (M2).

No model runs are implemented at M0 (preserved for M0 conformance).
In M2, this script connects via stdlib urllib to the local Ollama runtime,
benchmarks structured extraction against synthetic fixtures F1-F6 on the
pinned llama3.2:3b model, and reports CPU latency, token throughput,
and quotation verification rates (DECISION_LOG D2, D4, D5).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

EXPECTED_MODEL_TAG = "llama3.2:3b"  # DECISION_LOG D2
PLAN_REF = "docs/IMPLEMENTATION_PLAN.md §2 (M0), §4 (M2 gate)"

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "backend" / "tests" / "fixtures" / "pages"


def selfcheck() -> int:
    payload = {
        "tool": "practicesignal-benchmark",
        "status": "scaffold",
        "milestone_implemented": True,
        "planned_milestone": "M2",
        "planned_metrics": ["latency_p50_ms", "latency_p95_ms", "tokens_per_sec", "json_validity_rate"],
        "pinned_model_tag": EXPECTED_MODEL_TAG,
        "network_calls": 0,
        "model_invocations": 0,
        "reference_plan": PLAN_REF,
    }
    print(json.dumps(payload, indent=2))
    if EXPECTED_MODEL_TAG != "llama3.2:3b":
        print("PIN MISMATCH: the pinned model tag changed without updating DECISION_LOG D2.", file=sys.stderr)
        return 2
    return 0


def run_benchmark(fixture_ids: list[str], ollama_url: str = "http://127.0.0.1:11434") -> int:
    print(f"=== PracticeSignal M2 AI Benchmark ===")
    print(f"Model: {EXPECTED_MODEL_TAG} | Runtime: {ollama_url} | Host: CPU-only")
    print("-" * 70)

    # Import backend modules dynamically
    sys.path.insert(0, str(REPO_ROOT))
    from backend.app.ai.prompt_store import EXTRACTION_JSON_SCHEMA, compose_extraction_prompt
    from backend.app.ingest.evidence_verifier import verify_candidate_evidence
    from backend.app.parsers.text_extract import parse
    from backend.app.sources.fixture_loader import load_fixture

    results: list[dict] = []

    for fid in fixture_ids:
        print(f"\nBenchmarking Fixture {fid}...")
        fixture = load_fixture(fid)
        parsed = parse(fixture.html)
        prompt, schema, version = compose_extraction_prompt(
            page_text=parsed.text,
            intended_fields=fixture.intended_fields,
            page_type_hint=fixture.archetype,
            source_kind="fixture",
        )

        req_body = {
            "model": EXPECTED_MODEL_TAG,
            "prompt": prompt,
            "stream": False,
            "format": schema,
            "options": {"temperature": 0.0, "num_predict": 400},
        }

        req = urllib.request.Request(
            f"{ollama_url}/api/generate",
            data=json.dumps(req_body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            print(f"FAILED to connect to Ollama: {exc}", file=sys.stderr)
            return 1

        wall_time_s = time.perf_counter() - t0
        raw_text = resp_data.get("response", "")
        p_eval_cnt = resp_data.get("prompt_eval_count", 0)
        p_eval_ms = resp_data.get("prompt_eval_duration", 0) / 1e6
        eval_cnt = resp_data.get("eval_count", 0)
        eval_ms = resp_data.get("eval_duration", 0) / 1e6
        total_ms = resp_data.get("total_duration", 0) / 1e6
        tps = eval_cnt / (eval_ms / 1000.0) if eval_ms > 0 else 0.0

        parse_ok = False
        candidates = []
        try:
            parsed_json = json.loads(raw_text)
            parse_ok = isinstance(parsed_json, dict)
            candidates = parsed_json.get("candidates", [])
        except Exception:
            parse_ok = False

        verified_cnt = 0
        rejected_cnt = 0
        for cand in candidates:
            ver = verify_candidate_evidence(cand, parsed.text, fixture.intended_fields)
            if ver.verified:
                verified_cnt += 1
            else:
                rejected_cnt += 1

        entry = {
            "fixture": fid,
            "archetype": fixture.archetype,
            "wall_time_s": round(wall_time_s, 2),
            "prompt_tokens": p_eval_cnt,
            "prompt_eval_ms": round(p_eval_ms, 1),
            "eval_tokens": eval_cnt,
            "eval_ms": round(eval_ms, 1),
            "tokens_per_second": round(tps, 2),
            "parse_ok": parse_ok,
            "total_candidates": len(candidates),
            "verified_candidates": verified_cnt,
            "rejected_candidates": rejected_cnt,
        }
        results.append(entry)

        print(f"  Wall time: {entry['wall_time_s']}s | Prompt tokens: {entry['prompt_tokens']} | Eval tokens: {entry['eval_tokens']}")
        print(f"  Speed: {entry['tokens_per_second']} tokens/sec | Schema Valid: {entry['parse_ok']}")
        print(f"  Candidates: {entry['total_candidates']} (Verified: {entry['verified_candidates']}, Rejected: {entry['rejected_candidates']})")

    print("\n" + "=" * 70)
    print("BENCHMARK SUMMARY TABLE:")
    print(f"{'Fixture':<10} | {'Wall (s)':<10} | {'P-Tokens':<10} | {'Gen-Tokens':<12} | {'Tokens/s':<10} | {'Valid JSON':<10} | {'Candidates (V/R)':<15}")
    print("-" * 90)
    for r in results:
        print(f"{r['fixture'] + ' (' + r['archetype'] + ')':<10} | {r['wall_time_s']:<10} | {r['prompt_tokens']:<10} | {r['eval_tokens']:<12} | {r['tokens_per_second']:<10} | {str(r['parse_ok']):<10} | {str(r['verified_candidates']) + '/' + str(r['rejected_candidates']):<15}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="PracticeSignal benchmark (M2)")
    parser.add_argument("--selfcheck", action="store_true", help="print scaffold status; runs no model")
    parser.add_argument("--fixtures", default="F2,F5", help="comma-separated fixture IDs (default: F2,F5)")
    parser.add_argument("--url", default="http://127.0.0.1:11434", help="Ollama base URL")
    args = parser.parse_args()

    if args.selfcheck:
        return selfcheck()

    fixture_list = [f.strip() for f in args.fixtures.split(",") if f.strip()]
    return run_benchmark(fixture_list, args.url)


if __name__ == "__main__":
    raise SystemExit(main())
