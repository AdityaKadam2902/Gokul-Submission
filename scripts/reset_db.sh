#!/usr/bin/env bash
# PracticeSignal — reset local development database (M0 scaffold).
# Idempotent: exits cleanly whether or not the database exists.
# ASSESSMENT NOTE (M0): no application database bootstrap exists yet at M0
# (no DB layer is built before M1); this script only future-proofs the reset
# path and is intentionally inert until M1 creates backend/data/.

set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
data_dir="$repo_root/backend/data"

echo "[reset_db] repo root: $repo_root"

if [ -d "$data_dir" ]; then
  shopt -s nullglob
  removed=0
  for db in "$data_dir"/*.db "$data_dir"/*.sqlite "$data_dir"/*.sqlite3; do
    rm -f "$db" "$db-journal" "$db-wal" "$db-shm"
    echo "[reset_db] removed: $db"
    removed=1
  done
  if [ "$removed" -eq 0 ]; then
    echo "[reset_db] no database files present; nothing to do."
  fi
else
  echo "[reset_db] $data_dir does not exist yet (expected at M0); nothing to do."
fi

echo "[reset_db] done."
