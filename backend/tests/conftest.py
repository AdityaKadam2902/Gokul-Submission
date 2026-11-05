"""Shared pytest configuration for PracticeSignal backend tests.

M0 part: repository-root resolution for docset/fixture assertions — the M0
suite runs on the standard library alone.

M1 part: session-scoped in-memory database + FastAPI TestClient fixtures for
the vertical-slice suite. The test app uses dependency override so no file-system
database is ever created by tests (backend/data must stay empty during runs).
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "backend" / "tests" / "fixtures"
PAGES_DIR = FIXTURES_DIR / "pages"
PROMPTS_DIR = REPO_ROOT / "prompts"
DOCS_DIR = REPO_ROOT / "docs"


def repo_path(*parts: str) -> Path:
    return REPO_ROOT.joinpath(*parts)


# ---------------------------------------------------------------------------
# M1 fixtures: isolated app + in-memory database (no disk artefacts)
# ---------------------------------------------------------------------------

@pytest.fixture()
def m1_app():
    """The real FastAPI app wired to a single shared in-memory SQLite engine."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from backend.app.api.main import app as fastapi_app
    from backend.app.db import orm
    from backend.app.db.base import get_db as get_db_dependency

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    orm.Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    fastapi_app.dependency_overrides[get_db_dependency] = override_get_db
    yield fastapi_app
    fastapi_app.dependency_overrides.pop(get_db_dependency, None)
    engine.dispose()


@pytest.fixture()
def m1_client(m1_app):
    from fastapi.testclient import TestClient

    return TestClient(m1_app)
