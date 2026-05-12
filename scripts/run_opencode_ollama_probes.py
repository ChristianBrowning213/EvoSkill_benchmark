from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.harness.opencode import (
    DEFAULT_LOCAL_OLLAMA_MODEL,
    SECONDARY_LOCAL_OLLAMA_MODEL,
    build_opencode_options,
    execute_query,
    resolve_local_ollama_model,
    shutdown_project_server,
)

ARTIFACTS_DIR = ROOT / "artifacts"
SUMMARY_LOG_PATH = ROOT / ".evoskill" / "logs" / "opencode_message_diagnostics.jsonl"
DEFAULT_TIMEOUT_SEC = 45.0
DEFAULT_MODELS = (DEFAULT_LOCAL_OLLAMA_MODEL, SECONDARY_LOCAL_OLLAMA_MODEL)
STRUCTURED_SCHEMA = {
    "type": "object",
    "properties": {
        "shell_command": {"type": "string"},
    },
    "required": ["shell_command"],
    "additionalProperties": False,
}
PROMPTS = (
    {
        "id": "tiny_plain",
        "kind": "plain_text",
        "system": "Reply in a single short sentence.",
        "prompt": "say hello",
        "structured": False,
    },
    {
        "id": "tiny_structured",
        "kind": "structured_text",
        "system": "Return one line of JSON with a single shell_command field.",
        "prompt": (
            "Return a one-line JSON object with one key named shell_command "
            'whose value is a safe command that prints hello.'
        ),
        "structured": True,
    },
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _elapsed_seconds(started_at: float | None, ended_at: float | None = None) -> float | None:
    if started_at is None:
        return None
    finish = time.monotonic() if ended_at is None else ended_at
    return round(max(finish - started_at, 0.0), 6)


def _normalize_ollama_base_url(raw: str | None) -> str | None:
    value = str(raw or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = f"http://{value}"
    if value.startswith("http://0.0.0.0"):
        value = "http://127.0.0.1" + value[len("http://0.0.0.0"):]
    if value.startswith("https://0.0.0.0"):
        value = "https://127.0.0.1" + value[len("https://0.0.0.0"):]
    if not value.rstrip("/").endswith("/v1"):
        value = value.rstrip("/") + "/v1"
    return value


def _resolve_ollama_base_url() -> str:
    for env_name in (
        "OLLAMA_API_BASE",
        "OLLAMA_BASE_URL",
        "OLLAMA_HOST",
        "OPENAI_BASE_URL",
        "JCODE_OPENAI_COMPAT_API_BASE",
    ):
        normalized = _normalize_ollama_base_url(os.environ.get(env_name))
        if normalized:
            return normalized
    return "http://127.0.0.1:11434/v1"


def _probe_classification(*, headers_received: bool, first_chunk_received: bool, completed: bool, exception_type: str | None) -> str:
    if completed:
        return "completed"
    if first_chunk_received:
        return "partial_stream"
    if headers_received:
        return "stream_wait"
    if exception_type in {"ReadTimeout", "TimeoutError"}:
        return "provider_wait"
    if exception_type:
        return "request_error"
    return "unknown"


def _assistant_snapshot(messages: list[dict[str, Any]]) -> dict[str, Any]:
    for message in reversed(messages):
        info = message.get("info", {}) or {}
        if str(info.get("role", "")).strip().lower() != "assistant":
            continue
        parts = message.get("parts", []) or []
        text = "".join(
            part.get("text", "")
            for part in parts
            if str(part.get("type", "")).strip().lower() == "text"
        )
        return {
            "assistant_message_present": True,
            "assistant_parts_count": len(parts),
            "assistant_text_present": bool(text.strip()),
            "assistant_text_preview": text[:200],
            "assistant_structured_present": info.get("structured") is not None
            or info.get("structured_output") is not None,
            "assistant_mode": info.get("mode"),
            "assistant_provider_id": info.get("providerID"),
            "assistant_model_id": info.get("modelID"),
            "assistant_tokens": info.get("tokens") or {},
        }
    return {
        "assistant_message_present": False,
        "assistant_parts_count": 0,
        "assistant_text_present": False,
        "assistant_text_preview": "",
        "assistant_structured_present": False,
        "assistant_mode": None,
        "assistant_provider_id": None,
        "assistant_model_id": None,
        "assistant_tokens": {},
    }


def _read_summary_line_count() -> int:
    if not SUMMARY_LOG_PATH.exists():
        return 0
    return len(SUMMARY_LOG_PATH.read_text(encoding="utf-8").splitlines())


def _read_summary_after(previous_count: int) -> dict[str, Any] | None:
    if not SUMMARY_LOG_PATH.exists():
        return None
    lines = SUMMARY_LOG_PATH.read_text(encoding="utf-8").splitlines()
    if len(lines) <= previous_count:
        return None
    return json.loads(lines[-1])


async def _run_direct_probe(*, model: str, prompt_spec: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
    base_url = _resolve_ollama_base_url()
    request_started_at = time.monotonic()
    started_at_utc = _utc_now()
    headers_received = False
    first_chunk_received = False
    completed = False
    first_chunk_latency_sec = None
    header_latency_sec = None
    completion_latency_sec = None
    chunk_count = 0
    total_bytes = 0
    exception_type = None
    exception_message = None
    status_code = None
    content_type = None

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt_spec["prompt"]}],
        "stream": True,
        "max_tokens": 64,
    }

    headers = {
        "Authorization": f"Bearer {os.environ.get('OLLAMA_API_KEY') or 'ollama'}",
    }

    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=timeout_sec) as client:
            request = client.build_request(
                "POST",
                "/chat/completions",
                headers=headers,
                json=payload,
            )
            response = await client.send(request, stream=True)
            status_code = response.status_code
            content_type = response.headers.get("content-type")
            headers_received = True
            header_latency_sec = _elapsed_seconds(request_started_at)
            response.raise_for_status()
            try:
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    chunk_count += 1
                    total_bytes += len(chunk)
                    if not first_chunk_received:
                        first_chunk_received = True
                        first_chunk_latency_sec = _elapsed_seconds(request_started_at)
                completed = True
                completion_latency_sec = _elapsed_seconds(request_started_at)
            finally:
                await response.aclose()
    except Exception as exc:
        exception_type = type(exc).__name__
        exception_message = str(exc)
        completion_latency_sec = _elapsed_seconds(request_started_at)

    return {
        "probe_type": "direct_ollama",
        "started_at_utc": started_at_utc,
        "model": model,
        "prompt_id": prompt_spec["id"],
        "prompt_kind": prompt_spec["kind"],
        "timeout_sec": timeout_sec,
        "request_payload": {
            "stream": True,
            "max_tokens": 64,
        },
        "base_url": base_url,
        "headers_received": headers_received,
        "first_chunk_received": first_chunk_received,
        "completed": completed,
        "status_code": status_code,
        "content_type": content_type,
        "header_latency_sec": header_latency_sec,
        "first_chunk_latency_sec": first_chunk_latency_sec,
        "completion_latency_sec": completion_latency_sec,
        "chunk_count": chunk_count,
        "total_bytes": total_bytes,
        "exception_type": exception_type,
        "exception_message": exception_message,
        "classification": _probe_classification(
            headers_received=headers_received,
            first_chunk_received=first_chunk_received,
            completed=completed,
            exception_type=exception_type,
        ),
    }


async def _run_opencode_probe(*, model: str, prompt_spec: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
    options = build_opencode_options(
        system=prompt_spec["system"],
        schema=STRUCTURED_SCHEMA,
        tools=[],
        project_root=ROOT,
        model=resolve_local_ollama_model(model),
        mode="build",
    )
    if not prompt_spec["structured"]:
        options.pop("format", None)
    options["_evoskill_request_timeout_sec"] = timeout_sec
    options["_evoskill_probe_type"] = "opencode_message"

    previous_summary_count = _read_summary_line_count()
    started_at_utc = _utc_now()

    try:
        result = await execute_query(options, prompt_spec["prompt"])
        payload = result[0]
        diagnostic = dict(payload.get("diagnostics") or {})
        assistant = _assistant_snapshot(payload.get("messages") or [])
        return {
            "probe_type": "opencode_message",
            "started_at_utc": started_at_utc,
            "model": model,
            "requested_model": options.get("requested_model"),
            "resolved_model": options.get("model"),
            "prompt_id": prompt_spec["id"],
            "prompt_kind": prompt_spec["kind"],
            "timeout_sec": timeout_sec,
            "mode": options.get("mode"),
            "format_present": "format" in options,
            "tools": options.get("tools"),
            "provider_request_started": diagnostic.get("provider_request_started"),
            "headers_received": diagnostic.get("provider_http_headers_received"),
            "first_chunk_received": diagnostic.get("provider_first_chunk_received"),
            "provider_stream_completed": diagnostic.get("provider_response_completed"),
            "assistant_completion_observed": diagnostic.get("completion_observed"),
            "header_latency_sec": diagnostic.get("response_header_latency_sec"),
            "first_chunk_latency_sec": diagnostic.get("first_chunk_latency_sec"),
            "completion_latency_sec": diagnostic.get("provider_response_latency_sec"),
            "suspected_stall_stage": diagnostic.get("suspected_stall_stage"),
            "exception_type": diagnostic.get("provider_exception_type"),
            "exception_message": diagnostic.get("provider_exception"),
            "diagnostics": diagnostic,
            **assistant,
            "classification": _probe_classification(
                headers_received=bool(diagnostic.get("provider_http_headers_received")),
                first_chunk_received=bool(diagnostic.get("provider_first_chunk_received")),
                completed=bool(diagnostic.get("completion_observed")),
                exception_type=diagnostic.get("provider_exception_type"),
            ),
        }
    except Exception as exc:
        diagnostic = _read_summary_after(previous_summary_count) or {}
        return {
            "probe_type": "opencode_message",
            "started_at_utc": started_at_utc,
            "model": model,
            "requested_model": options.get("requested_model"),
            "resolved_model": options.get("model"),
            "prompt_id": prompt_spec["id"],
            "prompt_kind": prompt_spec["kind"],
            "timeout_sec": timeout_sec,
            "mode": options.get("mode"),
            "format_present": "format" in options,
            "tools": options.get("tools"),
            "provider_request_started": diagnostic.get("provider_request_started"),
            "headers_received": diagnostic.get("provider_http_headers_received"),
            "first_chunk_received": diagnostic.get("provider_first_chunk_received"),
            "provider_stream_completed": diagnostic.get("provider_response_completed"),
            "assistant_completion_observed": diagnostic.get("completion_observed"),
            "header_latency_sec": diagnostic.get("response_header_latency_sec"),
            "first_chunk_latency_sec": diagnostic.get("first_chunk_latency_sec"),
            "completion_latency_sec": diagnostic.get("provider_response_latency_sec"),
            "suspected_stall_stage": diagnostic.get("suspected_stall_stage"),
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
            "diagnostics": diagnostic,
            "assistant_message_present": False,
            "assistant_parts_count": 0,
            "assistant_text_present": False,
            "assistant_text_preview": "",
            "assistant_structured_present": False,
            "assistant_mode": None,
            "assistant_provider_id": None,
            "assistant_model_id": None,
            "assistant_tokens": {},
            "classification": _probe_classification(
                headers_received=bool(diagnostic.get("provider_http_headers_received")),
                first_chunk_received=bool(diagnostic.get("provider_first_chunk_received")),
                completed=bool(diagnostic.get("completion_observed")),
                exception_type=type(exc).__name__,
            ),
        }


def _comparison_verdict(direct: dict[str, Any], opencode: dict[str, Any]) -> str:
    if direct.get("first_chunk_received") and not opencode.get("first_chunk_received"):
        return "OpenCode-added latency or stream handling remains more constrained than direct Ollama."
    if not direct.get("first_chunk_received") and not opencode.get("first_chunk_received"):
        return "The model/provider stalls the same way in direct Ollama and through OpenCode."
    if direct.get("completed") and not opencode.get("assistant_completion_observed"):
        return "Direct Ollama completes, but OpenCode still fails to materialize assistant output."
    if direct.get("completed") and opencode.get("assistant_completion_observed"):
        return "Both direct Ollama and OpenCode complete within the bounded window."
    return "The result is mixed and needs a per-run read of timings and stall stage."


def _build_comparison(direct_results: list[dict[str, Any]], opencode_results: list[dict[str, Any]]) -> dict[str, Any]:
    pairs: list[dict[str, Any]] = []
    direct_index = {(item["model"], item["prompt_id"]): item for item in direct_results}
    opencode_index = {(item["model"], item["prompt_id"]): item for item in opencode_results}

    for model in DEFAULT_MODELS:
        for prompt_spec in PROMPTS:
            key = (model, prompt_spec["id"])
            direct = direct_index.get(key)
            opencode = opencode_index.get(key)
            if direct is None or opencode is None:
                continue
            pairs.append({
                "model": model,
                "prompt_id": prompt_spec["id"],
                "prompt_kind": prompt_spec["kind"],
                "direct": direct,
                "opencode": opencode,
                "verdict": _comparison_verdict(direct, opencode),
            })

    return {
        "generated_at_utc": _utc_now(),
        "default_local_benchmark_model": DEFAULT_LOCAL_OLLAMA_MODEL,
        "secondary_local_comparison_model": SECONDARY_LOCAL_OLLAMA_MODEL,
        "pairs": pairs,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


async def _main(timeout_sec: float) -> None:
    direct_results: list[dict[str, Any]] = []
    opencode_results: list[dict[str, Any]] = []

    shutdown_project_server(ROOT)
    for model in DEFAULT_MODELS:
        for prompt_spec in PROMPTS:
            direct_results.append(
                await _run_direct_probe(model=model, prompt_spec=prompt_spec, timeout_sec=timeout_sec)
            )

    shutdown_project_server(ROOT)
    for model in DEFAULT_MODELS:
        for prompt_spec in PROMPTS:
            opencode_results.append(
                await _run_opencode_probe(model=model, prompt_spec=prompt_spec, timeout_sec=timeout_sec)
            )

    comparison = _build_comparison(direct_results, opencode_results)
    direct_payload = {
        "generated_at_utc": _utc_now(),
        "default_local_benchmark_model": DEFAULT_LOCAL_OLLAMA_MODEL,
        "secondary_local_comparison_model": SECONDARY_LOCAL_OLLAMA_MODEL,
        "results": direct_results,
    }
    opencode_payload = {
        "generated_at_utc": _utc_now(),
        "default_local_benchmark_model": DEFAULT_LOCAL_OLLAMA_MODEL,
        "secondary_local_comparison_model": SECONDARY_LOCAL_OLLAMA_MODEL,
        "results": opencode_results,
    }

    _write_json(ARTIFACTS_DIR / "ollama_direct_latency_probe.json", direct_payload)
    _write_json(ARTIFACTS_DIR / "opencode_message_latency_probe.json", opencode_payload)
    _write_json(ARTIFACTS_DIR / "evoskill_provider_path_comparison.json", comparison)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run bounded direct-Ollama and OpenCode provider-path probes.")
    parser.add_argument("--timeout-sec", type=float, default=DEFAULT_TIMEOUT_SEC)
    args = parser.parse_args()
    asyncio.run(_main(args.timeout_sec))
