#!/usr/bin/env python3
"""PracticeSignal — local model benchmark scaffold (M0).

STATUS (M0): scaffold only, per docs/IMPLEMENTATION_PLAN.md §2. It performs
NO network calls and NO model invocations. The real benchmark (CPU latency
p50/p95, tokens/sec, JSON-validity rate) is implemented at M2 against the
pinned llama3.2:3b runtime and recorded verbatim in docs/RESULTS.md (D5).

Usage at M0:
    python scripts/benchmark_ai.py --selfcheck
"""

from __future__ import annotations

import argparse
import json
import sys

EXPECTED_MODEL_TAG = "llama3.2:3b"  # DECISION_LOG D2
PLAN_REF = "docs/IMPLEMENTATION_PLAN.md §2 (M0), §4 (M2 gate)"


def selfcheck() -> int:
    payload = {
        "tool": "practicesignal-benchmark",
        "status": "scaffold",
        "milestone_implemented": False,
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


def main() -> int:
    parser = argparse.ArgumentParser(description="PracticeSignal benchmark (M0 scaffold)")
    parser.add_argument("--selfcheck", action="store_true", help="print scaffold status; runs no model")
    args = parser.parse_args()
    if not args.selfcheck:
        print(
            "No model runs are implemented at M0 (per IMPLEMENTATION_PLAN §2).\n"
            "Run with --selfcheck for a scaffold status report.",
            file=sys.stderr,
        )
        return 1
    return selfcheck()


if __name__ == "__main__":
    raise SystemExit(main())
