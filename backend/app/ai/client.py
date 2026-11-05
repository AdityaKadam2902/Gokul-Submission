"""Ollama local model client adapter (ARCHITECTURE §6.1, §7; DECISION_LOG D2, D4).

Responsibility:
    Single owner of communication with the host's local Ollama HTTP runtime.
    Pins the model tag to 'llama3.2:3b' (D2); executes single structured-output
    calls via Ollama's format parameter (D4); enforces a concurrency ceiling of
    exactly ONE inference request at a time to protect CPU resources.

Inputs:
    Prompt text, JSON Schema dictionary, optional generation options.
Outputs:
    OllamaInferenceResult containing the raw text response, parsed JSON (if valid),
    latency, token counts, and execution metrics.
Failure modes:
    - Service unreachable -> OllamaUnavailable (503)
    - Pinned model missing -> OllamaModelNotFound (503)
    - Inference timeout -> OllamaTimeout (504)
    - Malformed API status -> OllamaInferenceError (502)
    - Exceeds input character limit -> OllamaInputTooLarge (413)
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

from backend.app.core.config import get_settings
from backend.app.core.exceptions import (
    OllamaInferenceError,
    OllamaInputTooLarge,
    OllamaModelNotFound,
    OllamaTimeout,
    OllamaUnavailable,
)
from backend.app.core.logging import get_logger

logger = get_logger(__name__)

PINNED_MODEL_TAG = "llama3.2:3b"


@dataclass(frozen=True)
class OllamaInferenceResult:
    """Metadata and raw text captured from one Ollama inference call."""

    raw_response: str
    parsed_json: dict[str, Any] | None
    parse_ok: bool
    model: str
    prompt_eval_count: int
    prompt_eval_duration_ms: float
    eval_count: int
    eval_duration_ms: float
    total_duration_ms: float
    tokens_per_second: float


class OllamaClient:
    """Thread-safe and async-safe local Ollama client with concurrency cap of 1."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        max_prompt_chars: int | None = None,
        transport: httpx.BaseTransport | None = None,
        async_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        settings = get_settings()
        raw_url = base_url or settings.ollama_url
        # Normalize localhost to 127.0.0.1 for consistent loopback binding on Windows
        if "localhost" in raw_url:
            raw_url = raw_url.replace("localhost", "127.0.0.1")
        self.base_url = raw_url.rstrip("/")
        self.model = model or settings.ollama_model or PINNED_MODEL_TAG
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else float(settings.ollama_timeout_seconds)
        )
        self.max_prompt_chars = (
            max_prompt_chars
            if max_prompt_chars is not None
            else settings.max_prompt_chars
        )
        self._transport = transport
        self._async_transport = async_transport
        # Concurrency guards: strictly 1 inference at a time
        self._async_lock = asyncio.Lock()
        self._sync_lock = threading.Lock()

    def check_health(self) -> dict[str, Any]:
        """Synchronous probe of Ollama service and model availability."""
        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=5.0,
                transport=self._transport,
            ) as client:
                resp = client.get("/api/tags")
                if resp.status_code != 200:
                    return {
                        "available": False,
                        "model_present": False,
                        "error": f"Ollama HTTP {resp.status_code}",
                    }
                data = resp.json()
                models = [m.get("name", "") for m in data.get("models", [])]
                model_found = any(self.model in m for m in models)
                return {
                    "available": True,
                    "model": self.model,
                    "model_present": model_found,
                    "installed_models": models,
                }
        except httpx.ConnectError:
            return {"available": False, "model_present": False, "error": "connection refused"}
        except Exception as exc:
            return {"available": False, "model_present": False, "error": str(exc)}

    async def check_health_async(self) -> dict[str, Any]:
        """Asynchronous probe of Ollama service and model availability."""
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=5.0,
                transport=self._async_transport,
            ) as client:
                resp = await client.get("/api/tags")
                if resp.status_code != 200:
                    return {
                        "available": False,
                        "model_present": False,
                        "error": f"Ollama HTTP {resp.status_code}",
                    }
                data = resp.json()
                models = [m.get("name", "") for m in data.get("models", [])]
                model_found = any(self.model in m for m in models)
                return {
                    "available": True,
                    "model": self.model,
                    "model_present": model_found,
                    "installed_models": models,
                }
        except httpx.ConnectError:
            return {"available": False, "model_present": False, "error": "connection refused"}
        except Exception as exc:
            return {"available": False, "model_present": False, "error": str(exc)}

    def _prepare_payload(
        self, prompt: str, schema: dict[str, Any], options: dict[str, Any] | None
    ) -> dict[str, Any]:
        if len(prompt) > self.max_prompt_chars:
            raise OllamaInputTooLarge(
                f"Prompt size ({len(prompt)} chars) exceeds maximum allowed ({self.max_prompt_chars} chars)",
                details={"prompt_length": len(prompt), "max_allowed": self.max_prompt_chars},
            )
        opts: dict[str, Any] = {"temperature": 0.0}
        if options:
            opts.update(options)
        return {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": schema,
            "options": opts,
        }

    def _parse_response(
        self, status_code: int, response_data: dict[str, Any], elapsed_s: float
    ) -> OllamaInferenceResult:
        if status_code == 404:
            raise OllamaModelNotFound(
                f"Model '{self.model}' not found in local Ollama runtime",
                details={"model": self.model},
            )
        if status_code != 200:
            raise OllamaInferenceError(
                f"Ollama API returned HTTP {status_code}: {response_data}",
                details={"status_code": status_code, "response": response_data},
            )

        raw_text = response_data.get("response", "")
        parsed: dict[str, Any] | None = None
        parse_ok = False
        try:
            parsed = json.loads(raw_text)
            parse_ok = isinstance(parsed, dict)
        except Exception:
            parse_ok = False

        p_eval_cnt = response_data.get("prompt_eval_count", 0)
        p_eval_ms = response_data.get("prompt_eval_duration", 0) / 1e6
        eval_cnt = response_data.get("eval_count", 0)
        eval_ms = response_data.get("eval_duration", 0) / 1e6
        total_ms = (
            response_data.get("total_duration", 0) / 1e6
            if "total_duration" in response_data
            else elapsed_s * 1000.0
        )
        tps = eval_cnt / (eval_ms / 1000.0) if eval_ms > 0 else 0.0

        return OllamaInferenceResult(
            raw_response=raw_text,
            parsed_json=parsed,
            parse_ok=parse_ok,
            model=response_data.get("model", self.model),
            prompt_eval_count=p_eval_cnt,
            prompt_eval_duration_ms=round(p_eval_ms, 2),
            eval_count=eval_cnt,
            eval_duration_ms=round(eval_ms, 2),
            total_duration_ms=round(total_ms, 2),
            tokens_per_second=round(tps, 2),
        )

    def generate_structured_sync(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        options: dict[str, Any] | None = None,
    ) -> OllamaInferenceResult:
        """Synchronous structured generation guarded by a single-inference lock."""
        payload = self._prepare_payload(prompt, schema, options)

        with self._sync_lock:
            t0 = time.perf_counter()
            try:
                with httpx.Client(
                    base_url=self.base_url,
                    timeout=self.timeout_seconds,
                    transport=self._transport,
                ) as client:
                    resp = client.post("/api/generate", json=payload)
                    elapsed = time.perf_counter() - t0
                    try:
                        resp_json = resp.json()
                    except Exception:
                        resp_json = {"raw": resp.text}
                    return self._parse_response(resp.status_code, resp_json, elapsed)
            except httpx.ConnectError as exc:
                raise OllamaUnavailable(
                    f"Could not connect to Ollama service at {self.base_url}",
                    details={"base_url": self.base_url, "error": str(exc)},
                ) from exc
            except httpx.TimeoutException as exc:
                raise OllamaTimeout(
                    f"Ollama inference timed out after {self.timeout_seconds}s",
                    details={"timeout_seconds": self.timeout_seconds},
                ) from exc

    async def generate_structured(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        options: dict[str, Any] | None = None,
    ) -> OllamaInferenceResult:
        """Asynchronous structured generation guarded by a single-inference lock."""
        payload = self._prepare_payload(prompt, schema, options)

        async with self._async_lock:
            t0 = time.perf_counter()
            try:
                async with httpx.AsyncClient(
                    base_url=self.base_url,
                    timeout=self.timeout_seconds,
                    transport=self._async_transport,
                ) as client:
                    resp = await client.post("/api/generate", json=payload)
                    elapsed = time.perf_counter() - t0
                    try:
                        resp_json = resp.json()
                    except Exception:
                        resp_json = {"raw": resp.text}
                    return self._parse_response(resp.status_code, resp_json, elapsed)
            except httpx.ConnectError as exc:
                raise OllamaUnavailable(
                    f"Could not connect to Ollama service at {self.base_url}",
                    details={"base_url": self.base_url, "error": str(exc)},
                ) from exc
            except httpx.TimeoutException as exc:
                raise OllamaTimeout(
                    f"Ollama inference timed out after {self.timeout_seconds}s",
                    details={"timeout_seconds": self.timeout_seconds},
                ) from exc


# Module-level client instance (singleton pattern)
_client: OllamaClient | None = None


def get_ollama_client() -> OllamaClient:
    global _client
    if _client is None:
        _client = OllamaClient()
    return _client


def set_ollama_client_for_tests(client: OllamaClient | None) -> None:
    global _client
    _client = client
