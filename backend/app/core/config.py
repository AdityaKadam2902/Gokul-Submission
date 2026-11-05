""" Central configuration load (ARCHITECTURE §8 "Configuration management").

Responsibility: load settings once from environment variables with a
simple local ``.env`` fallback (parsed with the standard library only —
no extra dependency). Safe defaults for every value; **no secrets exist**
in this project (zero-cost policy, DECISION_LOG D21).

Inputs:  process environment + optional ``.env`` at the repository root.
Outputs: an immutable ``Settings`` instance (via ``get_settings()``).
Failure modes: a malformed ``.env`` line is ignored with a warning log,
never a crash; unknown keys are ignored.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]

# Every variable, its default, and where it is documented (.env.example).
_ENV_DEFAULTS: dict[str, str] = {
    "APP_HOST": "127.0.0.1",
    "BACKEND_PORT": "8000",
    "FRONTEND_PORT": "5173",
    "DATABASE_PATH": "backend/data/practicesignal.db",
    "OLLAMA_URL": "http://localhost:11434",
    "OLLAMA_MODEL": "llama3.2:3b",
    "OLLAMA_TIMEOUT_SECONDS": "90",
    "MAX_PROMPT_CHARS": "16000",
    "FETCH_DELAY_SECONDS": "3",
    "FETCH_TIMEOUT_SECONDS": "20",
    "MAX_RESPONSE_BYTES": "2000000",
    "MAX_PAGES_PER_RUN": "10",
    "DEFAULT_TIMEZONE": "Australia/Brisbane",
}


def _load_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser (stdlib only). Comments/blank lines ignored."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:  # unreadable .env: keep running on env/defaults
        logger.warning("env_file_unreadable path=%s error=%s", path, exc)
        return values
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key.replace("_", "").isalnum():
            continue  # malformed key: ignore, never crash on operator typo
        values[key] = value.strip().strip("'\"")
    return values


@dataclass(frozen=True)
class Settings:
    app_host: str
    backend_port: int
    frontend_port: int
    database_path: str
    ollama_url: str
    ollama_model: str
    ollama_timeout_seconds: int
    max_prompt_chars: int
    fetch_delay_seconds: int
    fetch_timeout_seconds: int
    max_response_bytes: int
    max_pages_per_run: int
    default_timezone: str
    cors_origins: tuple[str, ...]

    @property
    def database_file(self) -> Path:
        """Absolute path of the SQLite file; parent directory created lazily."""
        raw = Path(self.database_path)
        path = raw if raw.is_absolute() else (REPO_ROOT / raw)
        return path.resolve()


def get_settings() -> Settings:
    """Compose settings: process env > .env file > safe defaults (cached)."""
    return _compute_settings()


@lru_cache(maxsize=1)
def _compute_settings() -> Settings:
    file_values = _load_env_file(REPO_ROOT / ".env")
    resolved: dict[str, str] = {}
    for key, default in _ENV_DEFAULTS.items():
        value = os.environ.get(key) or file_values.get(key) or default
        resolved[key] = value  # os env wins, then .env, then default

    def _int(key: str, minimum: int = 1) -> int:
        try:
            return max(minimum, int(resolved[key]))
        except (TypeError, ValueError):
            logger.warning("invalid_int_env key=%s value=%r using_default", key, resolved[key])
            return max(minimum, int(_ENV_DEFAULTS[key]))

    return Settings(
        app_host=resolved["APP_HOST"],
        backend_port=_int("BACKEND_PORT"),
        frontend_port=_int("FRONTEND_PORT"),
        database_path=resolved["DATABASE_PATH"],
        ollama_url=resolved["OLLAMA_URL"],
        ollama_model=resolved["OLLAMA_MODEL"],
        ollama_timeout_seconds=_int("OLLAMA_TIMEOUT_SECONDS", 10),
        max_prompt_chars=_int("MAX_PROMPT_CHARS", 1000),
        fetch_delay_seconds=_int("FETCH_DELAY_SECONDS"),
        fetch_timeout_seconds=_int("FETCH_TIMEOUT_SECONDS"),
        max_response_bytes=_int("MAX_RESPONSE_BYTES"),
        max_pages_per_run=_int("MAX_PAGES_PER_RUN"),
        default_timezone=resolved["DEFAULT_TIMEZONE"],
        # Explicit localhost-only CORS: dev UI port, nothing else (M1-AC 7).
        cors_origins=(
            f"http://localhost:{_int('BACKEND_PORT')}",
            f"http://127.0.0.1:{_int('BACKEND_PORT')}",
            f"http://localhost:{_int('FRONTEND_PORT')}",
            f"http://127.0.0.1:{_int('FRONTEND_PORT')}",
        ),
    )


def reset_settings_cache() -> None:
    """Test helper: clear the cached settings so env changes take effect."""
    _compute_settings.cache_clear()
