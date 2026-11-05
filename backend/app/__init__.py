"""PracticeSignal backend application package (M1 vertical slice, pre-AI).

Responsibility: hosts the FastAPI application, database layer, source
registry/policy gate, fixture loader, HTML text parser and snapshot store.
Implements only the M1 milestone scope (docs/IMPLEMENTATION_PLAN.md §3);
AI extraction, conflicts, review workflow and exports land in M2–M4.
"""

__version__ = "0.1.0.dev0"
