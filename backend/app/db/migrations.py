"""Database migrations for M2 remediation (Timezone nullability & PromptRun lineage).

Safely migrates SQLite facts table:
1. Makes timezone nullable (removes ungrounded 'Australia/Brisbane' hardcoding).
2. Converts historical ungrounded Brisbane values to NULL; preserves verified ones.
3. Adds prompt_run_id column referencing prompt_runs(id) ON DELETE SET NULL.
4. Leaves ambiguous historical prompt_run_id as NULL rather than guessing.
5. Full data preservation with pre-migration backup and recovery instructions.
"""

from __future__ import annotations

import datetime
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from backend.app.core.logging import get_logger

logger = get_logger(__name__)

LEGITIMATE_QLD_INDICATORS = ("brisbane", "qld", "queensland", "aest", "4000", "4217", "gold coast")


def inspect_facts_schema(cursor: sqlite3.Cursor) -> dict[str, Any]:
    """Inspect facts table columns, nullability and constraints."""
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='facts'")
    if not cursor.fetchone():
        return {"exists": False, "has_prompt_run_id": False, "timezone_nullable": False}

    cursor.execute("PRAGMA table_info(facts)")
    cols = cursor.fetchall()
    col_map = {col[1]: {"type": col[2], "notnull": bool(col[3]), "default": col[4]} for col in cols}

    has_prompt_run_id = "prompt_run_id" in col_map
    tz_info = col_map.get("timezone", {})
    timezone_nullable = not tz_info.get("notnull", True)

    return {
        "exists": True,
        "has_prompt_run_id": has_prompt_run_id,
        "timezone_nullable": timezone_nullable,
        "columns": col_map,
    }


def migrate_m2_facts_table(db_path: Path | str) -> dict[str, Any]:
    """Execute safe incremental migration on SQLite facts table."""
    path = Path(db_path).resolve()
    if not path.is_file():
        return {"status": "skipped", "reason": f"Database file not found at {path}"}

    conn = sqlite3.connect(path)
    cur = conn.cursor()

    schema_status = inspect_facts_schema(cur)
    if not schema_status["exists"]:
        conn.close()
        return {"status": "skipped", "reason": "facts table does not exist"}

    if schema_status["has_prompt_run_id"] and schema_status["timezone_nullable"]:
        conn.close()
        return {"status": "up_to_date", "message": "facts table already has prompt_run_id and nullable timezone"}

    # 1. Create a timestamped backup before any modifications
    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d_%H%M%S")
    backup_path = path.with_name(f"{path.name}.bak_{timestamp}")
    shutil.copy2(path, backup_path)
    logger.info("db_migration_backup_created path=%s", backup_path)

    # 2. Inspect existing facts to determine timezone provenance and prompt_run mapping
    cur.execute("SELECT id, snapshot_id, field_key, raw_value, timezone, evidence_id FROM facts")
    existing_facts = cur.fetchall()

    # Get prompt runs per snapshot
    cur.execute("SELECT id, snapshot_id FROM prompt_runs ORDER BY id ASC")
    runs_by_snap: dict[int, list[int]] = {}
    for r_id, s_id in cur.fetchall():
        runs_by_snap.setdefault(s_id, []).append(r_id)

    # Get evidence quotes for verification check
    cur.execute("SELECT id, quote FROM evidence")
    quotes_by_ev = dict(cur.fetchall())

    nullified_tz_count = 0
    preserved_tz_count = 0
    linked_prompt_runs_count = 0

    fact_transforms: dict[int, tuple[str | None, int | None]] = {}

    for f_id, s_id, field_key, raw_val, tz, ev_id in existing_facts:
        new_tz = tz
        # Verify timezone provenance
        if tz:
            ev_quote = quotes_by_ev.get(ev_id, "") if ev_id else ""
            combined_text = f"{raw_val} {ev_quote}".lower()
            is_legit = any(ind in combined_text for ind in LEGITIMATE_QLD_INDICATORS)
            if not is_legit and tz == "Australia/Brisbane":
                new_tz = None
                nullified_tz_count += 1
            else:
                preserved_tz_count += 1

        # Check prompt_run linkage: only link if unique and unambiguous
        snap_runs = runs_by_snap.get(s_id, [])
        assigned_run_id: int | None = None
        if len(snap_runs) == 1:
            assigned_run_id = snap_runs[0]
            linked_prompt_runs_count += 1

        fact_transforms[f_id] = (new_tz, assigned_run_id)

    # 3. Perform 12-step SQLite table rebuild
    try:
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.execute("BEGIN TRANSACTION")

        # Create facts_new with nullable timezone and prompt_run_id
        cur.execute("""
            CREATE TABLE facts_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organisation_id INTEGER,
                snapshot_id INTEGER NOT NULL REFERENCES page_snapshots(id) ON DELETE CASCADE,
                prompt_run_id INTEGER REFERENCES prompt_runs(id) ON DELETE SET NULL,
                field_key VARCHAR(64) NOT NULL,
                scope_key VARCHAR(64),
                raw_value TEXT NOT NULL,
                normalized_value TEXT,
                timezone VARCHAR(64),
                evidence_id INTEGER REFERENCES evidence(id) ON DELETE SET NULL,
                model_tag VARCHAR(64),
                prompt_version VARCHAR(64),
                via VARCHAR(32) NOT NULL DEFAULT 'ai_extraction',
                status VARCHAR(32) NOT NULL DEFAULT 'candidate',
                needs_review_reason VARCHAR(255),
                created_at DATETIME NOT NULL
            )
        """)

        # Fetch all columns from old facts
        cur.execute("""
            SELECT id, organisation_id, snapshot_id, field_key, scope_key,
                   raw_value, normalized_value, timezone, evidence_id,
                   model_tag, prompt_version, via, status, needs_review_reason, created_at
            FROM facts
        """)
        old_rows = cur.fetchall()

        for row in old_rows:
            f_id = row[0]
            transformed_tz, transformed_run_id = fact_transforms.get(f_id, (row[7], None))
            cur.execute("""
                INSERT INTO facts_new (
                    id, organisation_id, snapshot_id, prompt_run_id, field_key, scope_key,
                    raw_value, normalized_value, timezone, evidence_id,
                    model_tag, prompt_version, via, status, needs_review_reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                row[0], row[1], row[2], transformed_run_id, row[3], row[4],
                row[5], row[6], transformed_tz, row[8],
                row[9], row[10], row[11], row[12], row[13], row[14]
            ))

        cur.execute("DROP TABLE facts")
        cur.execute("ALTER TABLE facts_new RENAME TO facts")

        # Recreate indexes
        cur.execute("CREATE INDEX ix_facts_snapshot_id ON facts (snapshot_id)")
        cur.execute("CREATE INDEX ix_facts_field_key ON facts (field_key)")
        cur.execute("CREATE INDEX ix_facts_status ON facts (status)")
        cur.execute("CREATE INDEX ix_facts_prompt_run_id ON facts (prompt_run_id)")

        # Verify integrity
        cur.execute("PRAGMA foreign_key_check")
        fk_errors = cur.fetchall()
        if fk_errors:
            raise RuntimeError(f"Foreign key check failed after migration: {fk_errors}")

        conn.commit()
        cur.execute("PRAGMA foreign_keys=ON")

        logger.info(
            "db_migration_complete total_facts=%d nullified_tz=%d preserved_tz=%d linked_runs=%d",
            len(old_rows), nullified_tz_count, preserved_tz_count, linked_prompt_runs_count
        )

        return {
            "status": "success",
            "backup_path": str(backup_path),
            "total_facts": len(old_rows),
            "nullified_tz_count": nullified_tz_count,
            "preserved_tz_count": preserved_tz_count,
            "linked_prompt_runs_count": linked_prompt_runs_count,
        }
    except Exception as exc:
        conn.rollback()
        cur.execute("PRAGMA foreign_keys=ON")
        logger.error("db_migration_failed error=%s. Restoring from backup.", exc)
        shutil.copy2(backup_path, path)
        raise RuntimeError(f"Migration failed and rolled back: {exc}") from exc
    finally:
        conn.close()


def rollback_migration(db_path: Path | str, backup_path: Path | str) -> None:
    """Restore database from backup file."""
    shutil.copy2(Path(backup_path), Path(db_path))
    logger.info("db_rollback_complete restored %s to %s", backup_path, db_path)


def inspect_m3_001_schema(cursor: sqlite3.Cursor) -> dict[str, bool]:
    """Check existence of the four M3.1 tables."""
    tables = ("organisations", "organisation_source_links", "source_observations", "organisation_match_candidates")
    existing = set()
    for t in tables:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,))
        if cursor.fetchone():
            existing.add(t)
    return {t: t in existing for t in tables}


def migrate_m3_001_organisations(db_path: Path | str) -> dict[str, Any]:
    """Execute safe additive migration M3_001 adding organisations, source links, observations, and match candidates."""
    path = Path(db_path).resolve()
    if not path.is_file():
        return {"status": "skipped", "reason": f"Database file not found at {path}"}

    conn = sqlite3.connect(path)
    cur = conn.cursor()

    schema_status = inspect_m3_001_schema(cur)
    if all(schema_status.values()):
        conn.close()
        return {"status": "up_to_date", "message": "M3_001 tables already exist"}

    # 1. Create timestamped backup before applying schema changes
    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d_%H%M%S")
    backup_path = path.with_name(f"{path.name}.bak_{timestamp}")
    shutil.copy2(path, backup_path)
    logger.info("db_migration_backup_created path=%s", backup_path)

    try:
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("BEGIN IMMEDIATE TRANSACTION")

        # 1. organisations
        cur.execute("""
            CREATE TABLE IF NOT EXISTS organisations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(255) NOT NULL,
                slug VARCHAR(255) NOT NULL UNIQUE,
                primary_domain VARCHAR(255),
                corporate_parent_id INTEGER REFERENCES organisations(id) ON DELETE SET NULL,
                phone VARCHAR(32),
                address VARCHAR(512),
                suburb VARCHAR(128),
                postcode VARCHAR(16) NOT NULL,
                state VARCHAR(16) NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'active',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS ix_organisations_slug ON organisations (slug)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_organisations_primary_domain ON organisations (primary_domain)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_organisations_phone ON organisations (phone)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_organisations_locality ON organisations (state, postcode, suburb)")

        # 2. organisation_source_links
        cur.execute("""
            CREATE TABLE IF NOT EXISTS organisation_source_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organisation_id INTEGER NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
                source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                relationship_type VARCHAR(64) NOT NULL,
                scope_path VARCHAR(512),
                verification_status VARCHAR(32) NOT NULL DEFAULT 'suggested',
                verified_by VARCHAR(255),
                verified_at DATETIME,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_org_source_scope UNIQUE (organisation_id, source_id, scope_path)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS ix_org_source_links_org_id ON organisation_source_links (organisation_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_org_source_links_source_id ON organisation_source_links (source_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_org_source_links_status ON organisation_source_links (verification_status)")

        # 3. source_observations
        cur.execute("""
            CREATE TABLE IF NOT EXISTS source_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                observed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                outcome VARCHAR(32) NOT NULL,
                snapshot_id INTEGER REFERENCES page_snapshots(id) ON DELETE SET NULL,
                content_hash VARCHAR(64),
                content_changed BOOLEAN NOT NULL DEFAULT 0,
                parsing_completeness VARCHAR(32) NOT NULL DEFAULT 'complete',
                extraction_completeness VARCHAR(32) NOT NULL DEFAULT 'not_run',
                headings_count INTEGER NOT NULL DEFAULT 0,
                character_count INTEGER NOT NULL DEFAULT 0,
                observation_sequence INTEGER NOT NULL,
                error_details TEXT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_source_obs_seq UNIQUE (source_id, observation_sequence)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS ix_source_obs_source_seq ON source_observations (source_id, observation_sequence)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_source_obs_source_date ON source_observations (source_id, observed_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_source_obs_outcome ON source_observations (outcome)")

        # 4. organisation_match_candidates
        cur.execute("""
            CREATE TABLE IF NOT EXISTS organisation_match_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                suggested_organisation_id INTEGER NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
                match_tier VARCHAR(32) NOT NULL,
                raw_metric_score REAL,
                evidence_signals JSON NOT NULL DEFAULT '{}',
                rationale TEXT NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'suggested',
                reviewed_by VARCHAR(255),
                reviewed_at DATETIME,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_match_source_org UNIQUE (source_id, suggested_organisation_id)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS ix_match_candidates_source_id ON organisation_match_candidates (source_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_match_candidates_org_id ON organisation_match_candidates (suggested_organisation_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_match_candidates_status ON organisation_match_candidates (status)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_match_candidates_tier ON organisation_match_candidates (match_tier)")

        # Verify integrity
        cur.execute("PRAGMA foreign_key_check")
        fk_errors = cur.fetchall()
        if fk_errors:
            raise RuntimeError(f"Foreign key check failed after M3_001 migration: {fk_errors}")

        conn.commit()
        logger.info("db_migration_m3_001_complete backup_path=%s", backup_path)
        return {
            "status": "success",
            "backup_path": str(backup_path),
            "tables_created": ["organisations", "organisation_source_links", "source_observations", "organisation_match_candidates"],
        }
    except Exception as exc:
        conn.rollback()
        logger.error("db_migration_m3_001_failed error=%s. Restoring from backup.", exc)
        shutil.copy2(backup_path, path)
        raise RuntimeError(f"Migration M3_001 failed and rolled back: {exc}") from exc
    finally:
        conn.close()


def rollback_m3_001_organisations(db_path: Path | str) -> dict[str, Any]:
    """Reversibly drop M3_001 tables if requested in development/testing."""
    path = Path(db_path).resolve()
    if not path.is_file():
        return {"status": "skipped", "reason": f"Database file not found at {path}"}

    conn = sqlite3.connect(path)
    cur = conn.cursor()
    try:
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.execute("BEGIN IMMEDIATE TRANSACTION")
        cur.execute("DROP TABLE IF EXISTS organisation_match_candidates")
        cur.execute("DROP TABLE IF EXISTS source_observations")
        cur.execute("DROP TABLE IF EXISTS organisation_source_links")
        cur.execute("DROP TABLE IF EXISTS organisations")
        conn.commit()
        return {"status": "success", "message": "Dropped M3_001 tables successfully"}
    except Exception as exc:
        conn.rollback()
        raise RuntimeError(f"Rollback M3_001 failed: {exc}") from exc
    finally:
        conn.close()


def inspect_m3_002_schema(cursor: sqlite3.Cursor) -> dict[str, bool]:
    """Check existence of the M3.2 discrepancies table."""
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='discrepancies'")
    return {"discrepancies": bool(cursor.fetchone())}


def migrate_m3_002_discrepancies(db_path: Path | str) -> dict[str, Any]:
    """Execute safe additive migration M3_002 adding discrepancies table with RESTRICT foreign keys."""
    path = Path(db_path).resolve()
    if not path.is_file():
        return {"status": "skipped", "reason": f"Database file not found at {path}"}

    conn = sqlite3.connect(path)
    cur = conn.cursor()

    schema_status = inspect_m3_002_schema(cur)
    if schema_status["discrepancies"]:
        # Verify foreign-key action: if legacy CASCADE exists, safely upgrade to RESTRICT
        cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='discrepancies'")
        row = cur.fetchone()
        table_sql = (row[0] or "").upper() if row else ""
        if "CASCADE" in table_sql:
            logger.info("db_migration_m3_002_upgrading_cascade_to_restrict")
            timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d_%H%M%S")
            backup_path = path.with_name(f"{path.name}.bak_{timestamp}")
            shutil.copy2(path, backup_path)
            try:
                cur.execute("PRAGMA foreign_keys=OFF")
                cur.execute("BEGIN IMMEDIATE TRANSACTION")
                cur.execute("ALTER TABLE discrepancies RENAME TO _discrepancies_legacy")
                cur.execute("""
                    CREATE TABLE discrepancies (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        organisation_id INTEGER NOT NULL REFERENCES organisations(id) ON DELETE RESTRICT,
                        field_key VARCHAR(64) NOT NULL,
                        scope_key VARCHAR(64) NOT NULL,
                        fact_a_id INTEGER NOT NULL REFERENCES facts(id) ON DELETE RESTRICT,
                        fact_b_id INTEGER NOT NULL REFERENCES facts(id) ON DELETE RESTRICT,
                        conflict_type VARCHAR(64) NOT NULL,
                        status VARCHAR(32) NOT NULL DEFAULT 'detected',
                        rationale TEXT NOT NULL,
                        resolution_notes TEXT,
                        resolved_by VARCHAR(255),
                        resolved_at DATETIME,
                        detected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT ck_discrepancy_canonical_fact_order CHECK (fact_a_id < fact_b_id),
                        CONSTRAINT uq_discrepancy_fact_pair UNIQUE (fact_a_id, fact_b_id)
                    )
                """)
                cur.execute("INSERT INTO discrepancies SELECT * FROM _discrepancies_legacy")
                cur.execute("DROP TABLE _discrepancies_legacy")
                cur.execute("CREATE INDEX IF NOT EXISTS ix_discrepancies_org_id ON discrepancies (organisation_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS ix_discrepancies_field_scope ON discrepancies (field_key, scope_key)")
                cur.execute("CREATE INDEX IF NOT EXISTS ix_discrepancies_conflict_type ON discrepancies (conflict_type)")
                cur.execute("CREATE INDEX IF NOT EXISTS ix_discrepancies_status ON discrepancies (status)")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA foreign_key_check")
                fk_errors = cur.fetchall()
                if fk_errors:
                    raise RuntimeError(f"Foreign key check failed: {fk_errors}")
                conn.commit()
                conn.close()
                return {"status": "upgraded", "message": "Upgraded foreign keys to ON DELETE RESTRICT", "backup_path": str(backup_path)}
            except Exception as e:
                conn.rollback()
                conn.close()
                shutil.copy2(backup_path, path)
                raise e
        conn.close()
        return {"status": "up_to_date", "message": "M3_002 discrepancies table already exists"}

    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d_%H%M%S")
    backup_path = path.with_name(f"{path.name}.bak_{timestamp}")
    shutil.copy2(path, backup_path)
    logger.info("db_migration_backup_created path=%s", backup_path)

    try:
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("BEGIN IMMEDIATE TRANSACTION")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS discrepancies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organisation_id INTEGER NOT NULL REFERENCES organisations(id) ON DELETE RESTRICT,
                field_key VARCHAR(64) NOT NULL,
                scope_key VARCHAR(64) NOT NULL,
                fact_a_id INTEGER NOT NULL REFERENCES facts(id) ON DELETE RESTRICT,
                fact_b_id INTEGER NOT NULL REFERENCES facts(id) ON DELETE RESTRICT,
                conflict_type VARCHAR(64) NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'detected',
                rationale TEXT NOT NULL,
                resolution_notes TEXT,
                resolved_by VARCHAR(255),
                resolved_at DATETIME,
                detected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT ck_discrepancy_canonical_fact_order CHECK (fact_a_id < fact_b_id),
                CONSTRAINT uq_discrepancy_fact_pair UNIQUE (fact_a_id, fact_b_id)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS ix_discrepancies_org_id ON discrepancies (organisation_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_discrepancies_field_scope ON discrepancies (field_key, scope_key)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_discrepancies_conflict_type ON discrepancies (conflict_type)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_discrepancies_status ON discrepancies (status)")

        cur.execute("PRAGMA foreign_key_check")
        fk_errors = cur.fetchall()
        if fk_errors:
            raise RuntimeError(f"Foreign key check failed after M3_002 migration: {fk_errors}")

        conn.commit()
        logger.info("db_migration_m3_002_complete backup_path=%s", backup_path)
        return {
            "status": "success",
            "backup_path": str(backup_path),
            "tables_created": ["discrepancies"],
        }
    except Exception as exc:
        conn.rollback()
        logger.error("db_migration_m3_002_failed error=%s. Restoring from backup.", exc)
        shutil.copy2(backup_path, path)
        raise RuntimeError(f"Migration M3_002 failed and rolled back: {exc}") from exc
    finally:
        conn.close()


def rollback_m3_002_discrepancies(db_path: Path | str) -> dict[str, Any]:
    """Reversibly drop M3_002 discrepancies table if requested in development/testing."""
    path = Path(db_path).resolve()
    if not path.is_file():
        return {"status": "skipped", "reason": f"Database file not found at {path}"}

    conn = sqlite3.connect(path)
    cur = conn.cursor()
    try:
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.execute("BEGIN IMMEDIATE TRANSACTION")
        cur.execute("DROP TABLE IF EXISTS discrepancies")
        conn.commit()
        return {"status": "success", "message": "Dropped M3_002 discrepancies table successfully"}
    except Exception as exc:
        conn.rollback()
        raise RuntimeError(f"Rollback M3_002 failed: {exc}") from exc
    finally:
        conn.close()


def inspect_m3_003_schema(cursor: sqlite3.Cursor) -> dict[str, Any]:
    """Inspect presence and constraints of M3_003 change_events table."""
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='change_events'")
    exists = bool(cursor.fetchone())
    cols = {}
    if exists:
        cursor.execute("PRAGMA table_info(change_events)")
        cols = {row[1]: {"type": row[2], "notnull": bool(row[3])} for row in cursor.fetchall()}
    return {
        "change_events": exists,
        "columns": cols,
    }


def migrate_m3_003_change_events(db_path: Path | str) -> dict[str, Any]:
    """Execute additive M3_003 migration adding change_events table with ON DELETE RESTRICT foreign keys."""
    path = Path(db_path).resolve()
    if not path.is_file():
        return {"status": "skipped", "reason": f"Database file not found at {path}"}

    conn = sqlite3.connect(path)
    cur = conn.cursor()

    info = inspect_m3_003_schema(cur)
    if info["change_events"]:
        cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='change_events'")
        row = cur.fetchone()
        table_sql = (row[0] or "").upper() if row else ""
        if "CASCADE" in table_sql:
            logger.info("db_migration_m3_003_upgrading_cascade_to_restrict")
            timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d_%H%M%S")
            backup_path = path.with_name(f"{path.name}.bak_{timestamp}")
            shutil.copy2(path, backup_path)
            try:
                cur.execute("PRAGMA foreign_keys=OFF")
                cur.execute("BEGIN IMMEDIATE TRANSACTION")
                cur.execute("ALTER TABLE change_events RENAME TO _change_events_legacy")
                cur.execute("""
                    CREATE TABLE change_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
                        organisation_id INTEGER REFERENCES organisations(id) ON DELETE RESTRICT,
                        previous_observation_id INTEGER REFERENCES source_observations(id) ON DELETE RESTRICT,
                        current_observation_id INTEGER NOT NULL REFERENCES source_observations(id) ON DELETE RESTRICT,
                        previous_snapshot_id INTEGER REFERENCES page_snapshots(id) ON DELETE RESTRICT,
                        current_snapshot_id INTEGER NOT NULL REFERENCES page_snapshots(id) ON DELETE RESTRICT,
                        previous_fact_id INTEGER REFERENCES facts(id) ON DELETE RESTRICT,
                        current_fact_id INTEGER REFERENCES facts(id) ON DELETE RESTRICT,
                        event_type VARCHAR(64) NOT NULL,
                        entity_type VARCHAR(32) NOT NULL DEFAULT 'fact',
                        field_key VARCHAR(64) NOT NULL,
                        scope_key VARCHAR(64),
                        old_value TEXT,
                        new_value TEXT,
                        rationale TEXT NOT NULL,
                        diff_metadata TEXT NOT NULL DEFAULT '{}',
                        idempotency_key VARCHAR(255) NOT NULL UNIQUE,
                        detected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT ck_change_event_snapshots CHECK (previous_snapshot_id IS NULL OR previous_snapshot_id != current_snapshot_id)
                    )
                """)
                cur.execute("INSERT INTO change_events SELECT * FROM _change_events_legacy")
                cur.execute("DROP TABLE _change_events_legacy")
                cur.execute("CREATE INDEX IF NOT EXISTS ix_change_events_source_id ON change_events (source_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS ix_change_events_org_id ON change_events (organisation_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS ix_change_events_event_type ON change_events (event_type)")
                cur.execute("CREATE INDEX IF NOT EXISTS ix_change_events_field_scope ON change_events (field_key, scope_key)")
                cur.execute("CREATE INDEX IF NOT EXISTS ix_change_events_detected_at ON change_events (detected_at)")
                cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_change_events_idempotency ON change_events (idempotency_key)")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA foreign_key_check")
                fk_errors = cur.fetchall()
                if fk_errors:
                    raise RuntimeError(f"Foreign key check failed: {fk_errors}")
                conn.commit()
                conn.close()
                return {"status": "upgraded", "message": "Upgraded foreign keys to ON DELETE RESTRICT", "backup_path": str(backup_path)}
            except Exception as e:
                conn.rollback()
                conn.close()
                shutil.copy2(backup_path, path)
                raise e
        conn.close()
        return {"status": "up_to_date", "message": "M3_003 change_events table already exists"}

    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d_%H%M%S")
    backup_path = path.with_name(f"{path.name}.bak_{timestamp}")
    shutil.copy2(path, backup_path)
    logger.info("db_migration_backup_created path=%s", backup_path)

    try:
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("BEGIN IMMEDIATE TRANSACTION")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS change_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
                organisation_id INTEGER REFERENCES organisations(id) ON DELETE RESTRICT,
                previous_observation_id INTEGER REFERENCES source_observations(id) ON DELETE RESTRICT,
                current_observation_id INTEGER NOT NULL REFERENCES source_observations(id) ON DELETE RESTRICT,
                previous_snapshot_id INTEGER REFERENCES page_snapshots(id) ON DELETE RESTRICT,
                current_snapshot_id INTEGER NOT NULL REFERENCES page_snapshots(id) ON DELETE RESTRICT,
                previous_fact_id INTEGER REFERENCES facts(id) ON DELETE RESTRICT,
                current_fact_id INTEGER REFERENCES facts(id) ON DELETE RESTRICT,
                event_type VARCHAR(64) NOT NULL,
                entity_type VARCHAR(32) NOT NULL DEFAULT 'fact',
                field_key VARCHAR(64) NOT NULL,
                scope_key VARCHAR(64),
                old_value TEXT,
                new_value TEXT,
                rationale TEXT NOT NULL,
                diff_metadata TEXT NOT NULL DEFAULT '{}',
                idempotency_key VARCHAR(255) NOT NULL UNIQUE,
                detected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT ck_change_event_snapshots CHECK (previous_snapshot_id IS NULL OR previous_snapshot_id != current_snapshot_id)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS ix_change_events_source_id ON change_events (source_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_change_events_org_id ON change_events (organisation_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_change_events_event_type ON change_events (event_type)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_change_events_field_scope ON change_events (field_key, scope_key)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_change_events_detected_at ON change_events (detected_at)")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_change_events_idempotency ON change_events (idempotency_key)")

        cur.execute("PRAGMA foreign_key_check")
        fk_errors = cur.fetchall()
        if fk_errors:
            raise RuntimeError(f"Foreign key check failed after M3_003 migration: {fk_errors}")

        conn.commit()
        logger.info("db_migration_m3_003_complete backup_path=%s", backup_path)
        return {
            "status": "success",
            "backup_path": str(backup_path),
            "tables_created": ["change_events"],
        }
    except Exception as exc:
        conn.rollback()
        logger.error("db_migration_m3_003_failed error=%s. Restoring from backup.", exc)
        shutil.copy2(backup_path, path)
        raise RuntimeError(f"Migration M3_003 failed and rolled back: {exc}") from exc
    finally:
        conn.close()


def rollback_m3_003_change_events(db_path: Path | str) -> dict[str, Any]:
    """Reversibly drop M3_003 change_events table if requested in development/testing."""
    path = Path(db_path).resolve()
    if not path.is_file():
        return {"status": "skipped", "reason": f"Database file not found at {path}"}

    conn = sqlite3.connect(path)
    cur = conn.cursor()
    try:
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.execute("BEGIN IMMEDIATE TRANSACTION")
        cur.execute("DROP TABLE IF EXISTS change_events")
        conn.commit()
        return {"status": "success", "message": "Dropped M3_003 change_events table successfully"}
    except Exception as exc:
        conn.rollback()
        raise RuntimeError(f"Rollback M3_003 failed: {exc}") from exc
    finally:
        conn.close()


def inspect_m3_004_schema(cursor: sqlite3.Cursor) -> dict[str, Any]:
    """Inspect presence and constraints of M3_004 opportunities table."""
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='opportunities'")
    exists = bool(cursor.fetchone())
    cols = {}
    if exists:
        cursor.execute("PRAGMA table_info(opportunities)")
        cols = {row[1]: {"type": row[2], "notnull": bool(row[3])} for row in cursor.fetchall()}
    return {
        "opportunities": exists,
        "columns": cols,
    }


def migrate_m3_004_opportunities(db_path: Path | str) -> dict[str, Any]:
    """Execute additive M3_004 migration adding opportunities table with ON DELETE RESTRICT foreign keys."""
    path = Path(db_path).resolve()
    if not path.is_file():
        return {"status": "skipped", "reason": f"Database file not found at {path}"}

    conn = sqlite3.connect(path)
    cur = conn.cursor()

    info = inspect_m3_004_schema(cur)
    if info["opportunities"]:
        cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='opportunities'")
        row = cur.fetchone()
        table_sql = (row[0] or "").upper() if row else ""
        has_cascade = "CASCADE" in table_sql
        has_consecutive = "consecutive_absences" in info["columns"]

        if not has_cascade and has_consecutive:
            conn.close()
            return {"status": "skipped", "reason": "opportunities table already exists with RESTRICT foreign keys and full schema"}

        logger.info("db_migration_m3_004_upgrading_opportunities_table")
        timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d_%H%M%S")
        backup_path = path.with_name(f"{path.name}.bak_{timestamp}")
        shutil.copy2(path, backup_path)
        try:
            cur.execute("PRAGMA foreign_keys=OFF")
            cur.execute("BEGIN IMMEDIATE TRANSACTION")
            cur.execute("ALTER TABLE opportunities RENAME TO _opportunities_legacy")
            cur.execute("""
                CREATE TABLE opportunities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    organisation_id INTEGER NOT NULL REFERENCES organisations(id) ON DELETE RESTRICT,
                    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
                    first_snapshot_id INTEGER NOT NULL REFERENCES page_snapshots(id) ON DELETE RESTRICT,
                    last_snapshot_id INTEGER NOT NULL REFERENCES page_snapshots(id) ON DELETE RESTRICT,
                    role_category VARCHAR(64) NOT NULL,
                    role_title VARCHAR(255) NOT NULL,
                    verbatim_wording TEXT NOT NULL,
                    contact_channel VARCHAR(255),
                    explicit_ad_id VARCHAR(128),
                    status VARCHAR(32) NOT NULL DEFAULT 'advertised',
                    closure_evidence_quote TEXT,
                    consecutive_absences INTEGER NOT NULL DEFAULT 0,
                    first_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    closed_at DATETIME,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            legacy_cols = list(info["columns"].keys())
            common_cols = [c for c in [
                "id", "organisation_id", "source_id", "first_snapshot_id", "last_snapshot_id",
                "role_category", "role_title", "verbatim_wording", "contact_channel",
                "explicit_ad_id", "status", "closure_evidence_quote", "consecutive_absences",
                "first_seen_at", "last_seen_at", "closed_at", "created_at", "updated_at"
            ] if c in legacy_cols]
            cols_str = ", ".join(common_cols)
            cur.execute(f"INSERT INTO opportunities ({cols_str}) SELECT {cols_str} FROM _opportunities_legacy")
            cur.execute("DROP TABLE _opportunities_legacy")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_opportunities_organisation_id ON opportunities (organisation_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_opportunities_role_category ON opportunities (role_category)")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_opportunities_status ON opportunities (status)")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_opportunities_explicit_ad_id ON opportunities (explicit_ad_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_opportunities_source_id ON opportunities (source_id)")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA foreign_key_check")
            fk_errors = cur.fetchall()
            if fk_errors:
                raise RuntimeError(f"Foreign key check failed after upgrading opportunities table: {fk_errors}")
            conn.commit()
            return {"status": "success", "backup_path": str(backup_path), "action": "upgraded"}
        except Exception as exc:
            conn.rollback()
            shutil.copy2(backup_path, path)
            raise RuntimeError(f"Migration M3_004 upgrade failed and rolled back: {exc}") from exc
        finally:
            conn.close()

    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d_%H%M%S")
    backup_path = path.with_name(f"{path.name}.bak_{timestamp}")
    shutil.copy2(path, backup_path)
    try:
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.execute("BEGIN IMMEDIATE TRANSACTION")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organisation_id INTEGER NOT NULL REFERENCES organisations(id) ON DELETE RESTRICT,
                source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
                first_snapshot_id INTEGER NOT NULL REFERENCES page_snapshots(id) ON DELETE RESTRICT,
                last_snapshot_id INTEGER NOT NULL REFERENCES page_snapshots(id) ON DELETE RESTRICT,
                role_category VARCHAR(64) NOT NULL,
                role_title VARCHAR(255) NOT NULL,
                verbatim_wording TEXT NOT NULL,
                contact_channel VARCHAR(255),
                explicit_ad_id VARCHAR(128),
                status VARCHAR(32) NOT NULL DEFAULT 'advertised',
                closure_evidence_quote TEXT,
                consecutive_absences INTEGER NOT NULL DEFAULT 0,
                first_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                closed_at DATETIME,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS ix_opportunities_organisation_id ON opportunities (organisation_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_opportunities_role_category ON opportunities (role_category)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_opportunities_status ON opportunities (status)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_opportunities_explicit_ad_id ON opportunities (explicit_ad_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_opportunities_source_id ON opportunities (source_id)")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA foreign_key_check")
        fk_errors = cur.fetchall()
        if fk_errors:
            raise RuntimeError(f"Foreign key check failed after M3_004 migration: {fk_errors}")
        conn.commit()
        logger.info("db_migration_m3_004_complete backup_path=%s", backup_path)
        return {
            "status": "success",
            "backup_path": str(backup_path),
            "tables_created": ["opportunities"],
        }
    except Exception as exc:
        conn.rollback()
        logger.error("db_migration_m3_004_failed error=%s. Restoring from backup.", exc)
        shutil.copy2(backup_path, path)
        raise RuntimeError(f"Migration M3_004 failed and rolled back: {exc}") from exc
    finally:
        conn.close()


def rollback_m3_004_opportunities(db_path: Path | str) -> dict[str, Any]:
    """Reversibly drop M3_004 opportunities table if requested in development/testing."""
    path = Path(db_path).resolve()
    if not path.is_file():
        return {"status": "skipped", "reason": f"Database file not found at {path}"}

    conn = sqlite3.connect(path)
    cur = conn.cursor()
    try:
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.execute("BEGIN IMMEDIATE TRANSACTION")
        cur.execute("DROP TABLE IF EXISTS opportunities")
        conn.commit()
        return {"status": "success", "message": "Dropped M3_004 opportunities table successfully"}
    except Exception as exc:
        conn.rollback()
        raise RuntimeError(f"Rollback M3_004 failed: {exc}") from exc
    finally:
        conn.close()

def inspect_m3_005_schema(cursor_or_path: sqlite3.Cursor | Path | str) -> dict[str, Any]:
    """Inspect presence and constraints of M3_005 review_actions table."""
    conn = None
    if isinstance(cursor_or_path, (str, Path)):
        conn = sqlite3.connect(cursor_or_path)
        cur = conn.cursor()
    else:
        cur = cursor_or_path

    try:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='review_actions'")
        exists = bool(cur.fetchone())
        cols = {}
        indexes = []
        triggers = []
        if exists:
            cur.execute("PRAGMA table_info(review_actions)")
            cols = {row[1]: {"type": row[2], "notnull": bool(row[3])} for row in cur.fetchall()}
            cur.execute("PRAGMA index_list(review_actions)")
            indexes = [row[1] for row in cur.fetchall()]
            cur.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='review_actions'")
            triggers = [row[0] for row in cur.fetchall()]
        return {
            "table_exists": exists,
            "review_actions": exists,
            "columns": cols,
            "indexes": indexes,
            "triggers": triggers,
        }
    finally:
        if conn:
            conn.close()


def migrate_m3_005_review_actions(db_path: Path | str) -> dict[str, Any]:
    """Execute additive M3_005 migration adding review_actions table."""
    path = Path(db_path).resolve()
    if not path.is_file():
        return {"status": "skipped", "reason": f"Database file not found at {path}"}

    conn = sqlite3.connect(path)
    cur = conn.cursor()

    info = inspect_m3_005_schema(cur)
    if info["review_actions"]:
        conn.close()
        return {"status": "up_to_date", "message": "review_actions table already exists"}

    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d_%H%M%S")
    backup_path = path.with_name(f"{path.name}.bak_{timestamp}")
    shutil.copy2(path, backup_path)
    logger.info("db_migration_backup_created path=%s", backup_path)

    try:
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.execute("BEGIN IMMEDIATE TRANSACTION")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS review_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type VARCHAR(32) NOT NULL,
                entity_id INTEGER NOT NULL,
                action VARCHAR(64) NOT NULL,
                previous_status VARCHAR(32) NOT NULL,
                new_status VARCHAR(32) NOT NULL,
                reason_code VARCHAR(64) NOT NULL,
                reviewer VARCHAR(255) NOT NULL,
                notes TEXT,
                evidence_id INTEGER REFERENCES evidence(id) ON DELETE RESTRICT,
                decision_metadata TEXT NOT NULL DEFAULT '{}',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS ix_review_actions_entity ON review_actions (entity_type, entity_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_review_actions_action ON review_actions (action)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_review_actions_reviewer ON review_actions (reviewer)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_review_actions_created_at ON review_actions (created_at)")

        # Database-level triggers preventing direct SQLite UPDATE and DELETE operations
        cur.execute("""
            CREATE TRIGGER IF NOT EXISTS prevent_review_actions_update
            BEFORE UPDATE ON review_actions
            BEGIN
                SELECT RAISE(FAIL, 'UPDATE on review_actions is prohibited: audit records are append-only');
            END;
        """)
        cur.execute("""
            CREATE TRIGGER IF NOT EXISTS prevent_review_actions_delete
            BEFORE DELETE ON review_actions
            BEGIN
                SELECT RAISE(FAIL, 'DELETE on review_actions is prohibited: audit records are append-only');
            END;
        """)

        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA foreign_key_check")
        fk_errors = cur.fetchall()
        if fk_errors:
            raise RuntimeError(f"Foreign key check failed after M3_005 migration: {fk_errors}")

        conn.commit()
        logger.info("db_migration_m3_005_complete backup_path=%s", backup_path)
        return {
            "status": "success",
            "backup_path": str(backup_path),
            "tables_created": ["review_actions"],
        }
    except Exception as exc:
        conn.rollback()
        logger.error("db_migration_m3_005_failed error=%s. Restoring from backup.", exc)
        shutil.copy2(backup_path, path)
        raise RuntimeError(f"Migration M3_005 failed and rolled back: {exc}") from exc
    finally:
        conn.close()


def rollback_m3_005_review_actions(db_path: Path | str) -> dict[str, Any]:
    """Reversibly drop M3_005 review_actions table if requested in development/testing."""
    path = Path(db_path).resolve()
    if not path.is_file():
        return {"status": "skipped", "reason": f"Database file not found at {path}"}

    conn = sqlite3.connect(path)
    cur = conn.cursor()
    try:
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.execute("BEGIN IMMEDIATE TRANSACTION")
        cur.execute("DROP TRIGGER IF EXISTS prevent_review_actions_update")
        cur.execute("DROP TRIGGER IF EXISTS prevent_review_actions_delete")
        cur.execute("DROP TABLE IF EXISTS review_actions")
        conn.commit()
        return {"status": "success", "message": "Dropped M3_005 review_actions table successfully"}
    except Exception as exc:
        conn.rollback()
        raise RuntimeError(f"Rollback M3_005 failed: {exc}") from exc
    finally:
        conn.close()


def inspect_m3_006_schema(cursor_or_path: sqlite3.Cursor | Path | str) -> dict[str, Any]:
    """Inspect presence and constraints of M3_006 practice_lifecycle_events table."""
    conn = None
    if isinstance(cursor_or_path, (str, Path)):
        conn = sqlite3.connect(cursor_or_path)
        cur = conn.cursor()
    else:
        cur = cursor_or_path

    try:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='practice_lifecycle_events'")
        exists = bool(cur.fetchone())
        cols = {}
        indexes = []
        if exists:
            cur.execute("PRAGMA table_info(practice_lifecycle_events)")
            cols = {row[1]: {"type": row[2], "notnull": bool(row[3])} for row in cur.fetchall()}
            cur.execute("PRAGMA index_list(practice_lifecycle_events)")
            indexes = [row[1] for row in cur.fetchall()]
        return {
            "table_exists": exists,
            "practice_lifecycle_events": exists,
            "columns": cols,
            "indexes": indexes,
        }
    finally:
        if conn:
            conn.close()


def migrate_m3_006_lifecycle_events(db_path: Path | str) -> dict[str, Any]:
    """Execute additive M3_006 migration adding practice_lifecycle_events table."""
    path = Path(db_path).resolve()
    if not path.is_file():
        return {"status": "skipped", "reason": f"Database file not found at {path}"}

    conn = sqlite3.connect(path)
    cur = conn.cursor()

    info = inspect_m3_006_schema(cur)
    if info["practice_lifecycle_events"]:
        conn.close()
        return {"status": "up_to_date", "message": "practice_lifecycle_events table already exists"}

    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d_%H%M%S")
    backup_path = path.with_name(f"{path.name}.bak_{timestamp}")
    shutil.copy2(path, backup_path)
    logger.info("db_migration_backup_created path=%s", backup_path)

    try:
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.execute("BEGIN IMMEDIATE TRANSACTION")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS practice_lifecycle_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organisation_id INTEGER NOT NULL REFERENCES organisations(id) ON DELETE RESTRICT,
                branch_locality VARCHAR(128),
                is_branch_level BOOLEAN NOT NULL DEFAULT 0,
                event_type VARCHAR(32) NOT NULL,
                status VARCHAR(32) NOT NULL,
                source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
                snapshot_id INTEGER NOT NULL REFERENCES page_snapshots(id) ON DELETE RESTRICT,
                evidence_id INTEGER REFERENCES evidence(id) ON DELETE RESTRICT,
                evidence_quote TEXT,
                effective_date VARCHAR(64),
                date_precision VARCHAR(16) NOT NULL DEFAULT 'unknown',
                old_value TEXT,
                new_value TEXT,
                acquiring_organisation_id INTEGER REFERENCES organisations(id) ON DELETE RESTRICT,
                acquiring_entity_name VARCHAR(255),
                rationale TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                review_status VARCHAR(32) NOT NULL DEFAULT 'pending',
                reviewed_by VARCHAR(255),
                reviewed_at DATETIME,
                idempotency_key VARCHAR(255) NOT NULL UNIQUE,
                detected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS ix_practice_lifecycle_events_org ON practice_lifecycle_events (organisation_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_practice_lifecycle_events_source ON practice_lifecycle_events (source_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_practice_lifecycle_events_snapshot ON practice_lifecycle_events (snapshot_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_practice_lifecycle_events_type ON practice_lifecycle_events (event_type)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_practice_lifecycle_events_status ON practice_lifecycle_events (status)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_practice_lifecycle_events_review ON practice_lifecycle_events (review_status)")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_practice_lifecycle_events_idempotency ON practice_lifecycle_events (idempotency_key)")

        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA foreign_key_check")
        fk_errors = cur.fetchall()
        if fk_errors:
            raise RuntimeError(f"Foreign key check failed after M3_006 migration: {fk_errors}")

        conn.commit()
        logger.info("db_migration_m3_006_complete backup_path=%s", backup_path)
        return {
            "status": "success",
            "backup_path": str(backup_path),
            "tables_created": ["practice_lifecycle_events"],
        }
    except Exception as exc:
        conn.rollback()
        logger.error("db_migration_m3_006_failed error=%s. Restoring from backup.", exc)
        shutil.copy2(backup_path, path)
        raise RuntimeError(f"Migration M3_006 failed and rolled back: {exc}") from exc
    finally:
        conn.close()


def rollback_m3_006_lifecycle_events(db_path: Path | str) -> dict[str, Any]:
    """Reversibly drop M3_006 practice_lifecycle_events table if requested in development/testing."""
    path = Path(db_path).resolve()
    if not path.is_file():
        return {"status": "skipped", "reason": f"Database file not found at {path}"}

    conn = sqlite3.connect(path)
    cur = conn.cursor()
    try:
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.execute("BEGIN IMMEDIATE TRANSACTION")
        cur.execute("DROP TABLE IF EXISTS practice_lifecycle_events")
        conn.commit()
        return {"status": "success", "message": "Dropped M3_006 practice_lifecycle_events table successfully"}
    except Exception as exc:
        conn.rollback()
        raise RuntimeError(f"Rollback M3_006 failed: {exc}") from exc
    finally:
        conn.close()


if __name__ == "__main__":
    from backend.app.core.config import get_settings
    db_file = get_settings().database_file
    res = migrate_m2_facts_table(db_file)
    print(res)
