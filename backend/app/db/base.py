"""SQLite engine, session factory and reproducible schema bootstrap (D9).

Responsibility: single owner of database connectivity and initialisation.
M1 creates exactly two tables — ``sources`` and ``page_snapshots``
(IMPLEMENTATION_PLAN §3; DECISION_LOG D9: no speculative future tables).

Inputs:  ``Settings.database_file`` (path to the SQLite file).
Outputs: ``get_db`` (FastAPI dependency), ``create_all``-based ``init_db``.
Failure modes: the DB directory is created on demand; a locked/corrupt DB
surfaces as an ``OperationalError`` from SQLAlchemy (typed 5xx at the API).

Reproducibility: ``init_db`` is idempotent; ``scripts/reset_db.sh`` deletes
the file for a clean, reproducible re-bootstrap (never run in tests).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.core.config import get_settings

# M1 requires the codebase to be importable even before backend/app is an
# installed module path; these imports are plain package-relative.
from backend.app.db.orm import Base  # noqa: F401  (needed for metadata.create_all)
import backend.app.db.orm as orm  # noqa: F401  (table registration side-effect)


def _make_engine(database_file: str | Path) -> Engine:
    url = database_file if str(database_file).startswith("sqlite:") else f"sqlite:///{database_file}"
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False},  # FastAPI threadpool
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


def get_database_file(override: str | Path | None = None) -> Path:
    if override is not None:
        return Path(override).resolve()
    file = get_settings().database_file
    file.parent.mkdir(parents=True, exist_ok=True)
    return file


# Lazily-created default engine for the running application; tests inject
# their own session factory instead (see conftest in backend/tests).
_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = _make_engine(get_database_file())
    return _engine


def set_engine_for_tests(engine: Engine) -> None:
    global _engine
    _engine = engine


def init_db(engine: Engine | None = None) -> None:
    """Create the tables if absent, and run safe schema migration for facts table."""
    target = engine if engine is not None else get_engine()
    orm.Base.metadata.create_all(bind=target)

    # For file-based SQLite engines, ensure M2 and M3 migrations run if needed
    try:
        from backend.app.db.migrations import (
            migrate_m2_facts_table,
            migrate_m3_001_organisations,
            migrate_m3_002_discrepancies,
            migrate_m3_003_change_events,
            migrate_m3_004_opportunities,
            migrate_m3_005_review_actions,
            migrate_m3_006_lifecycle_events,
        )
        db_file = get_database_file()
        if db_file.is_file():
            migrate_m2_facts_table(db_file)
            migrate_m3_001_organisations(db_file)
            migrate_m3_002_discrepancies(db_file)
            migrate_m3_003_change_events(db_file)
            migrate_m3_004_opportunities(db_file)
            migrate_m3_005_review_actions(db_file)
            migrate_m3_006_lifecycle_events(db_file)
    except Exception:
        pass


SessionLocal = sessionmaker(autocommit=False, autoflush=False, future=True)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a session bound to the default engine."""
    db = SessionLocal(bind=get_engine())
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Transactional scope for non-request code (scripts/tests)."""
    target = engine if engine is not None else get_engine()
    session = SessionLocal(bind=target)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
