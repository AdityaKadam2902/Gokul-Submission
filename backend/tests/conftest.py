"""Shared pytest configuration for PracticeSignal backend tests (M0).

Provides repository-root resolution for docset/fixture assertions without
installing any application dependency. M0 tests must run on the stdlib alone.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "backend" / "tests" / "fixtures"
PAGES_DIR = FIXTURES_DIR / "pages"
PROMPTS_DIR = REPO_ROOT / "prompts"
DOCS_DIR = REPO_ROOT / "docs"


def repo_path(*parts: str) -> Path:
    return REPO_ROOT.joinpath(*parts)
