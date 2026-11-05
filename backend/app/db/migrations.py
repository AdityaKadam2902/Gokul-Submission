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


if __name__ == "__main__":
    from backend.app.core.config import get_settings
    db_file = get_settings().database_file
    res = migrate_m2_facts_table(db_file)
    print(res)
