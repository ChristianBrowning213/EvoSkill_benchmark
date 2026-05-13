"""OpenCode harness: server management, query execution, and response parsing.

Uses raw httpx to talk to the OpenCode server. The Python SDK sends
model/provider as flat fields which the server ignores; the correct
format is a nested ``model: {providerID, modelID}`` object.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Type

import httpx
from pydantic import BaseModel, ValidationError

from ..provider_auth import (
    PROVIDER_ENV_KEYS,
    apply_provider_auth_env,
    ensure_provider_api_key,
)
from ..utils import resolve_project_root

_SERVER_PORTS: dict[str, int] = {}
_SERVER_PIDS: dict[str, int] = {}
_SPAWNED_THIS_RUN: set[str] = set()
_SERVER_LOG_HANDLES: dict[str, Any] = {}
_SERVER_SIGNATURES: dict[str, str] = {}

_TIMEOUT = 1800
_EVENT_LOG_NAME = "opencode_message_events.jsonl"
_SUMMARY_LOG_NAME = "opencode_message_diagnostics.jsonl"

logger = logging.getLogger(__name__)

_PROVIDER_BASE_URL_ENV_KEYS: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_BASE_URL",),
    "openai": ("OPENAI_BASE_URL",),
    "ollama": (
        "OLLAMA_BASE_URL",
        "OLLAMA_API_BASE",
        "OLLAMA_HOST",
        "OPENAI_BASE_URL",
        "JCODE_OPENAI_COMPAT_API_BASE",
    ),
    "google": ("GOOGLE_BASE_URL", "GEMINI_BASE_URL"),
    "openrouter": ("OPENROUTER_BASE_URL",),
    "groq": ("GROQ_BASE_URL",),
    "mistral": ("MISTRAL_BASE_URL",),
    "together": ("TOGETHER_BASE_URL",),
    "deepseek": ("DEEPSEEK_BASE_URL",),
    "xai": ("XAI_BASE_URL",),
}

class _DiagnosticProbeResponse(BaseModel):
    reply: str

def _project_log_dir(cwd: str | Path | None) -> Path:
    root = resolve_project_root(cwd)
    return root / ".evoskill" / "logs"

def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")

def _safe_json(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): _safe_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_json(item) for item in value]
    return str(value)

def _elapsed_seconds(started_at: float | None, ended_at: float | None = None) -> float | None:
    if started_at is None:
        return None
    finish = time.monotonic() if ended_at is None else ended_at
    return round(max(finish - started_at, 0.0), 6)

def _resolve_provider_base_url(provider: str | None) -> tuple[str | None, str | None]:
    env_names = _PROVIDER_BASE_URL_ENV_KEYS.get(str(provider or "").strip().lower(), ())
    for env_name in env_names:
        value = os.environ.get(env_name)
        if value:
            return value, env_name
    return None, None

def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]

def _resolve_key(cwd: str | Path | None) -> str:
    return str(Path(cwd).resolve()) if cwd else ""

def _server_signature(options: dict[str, Any]) -> str:
    return json.dumps({
        "inline_config": options.get("_evoskill_opencode_inline_config"),
        "server_log_path": options.get("_evoskill_server_log_path"),
        "server_log_level": options.get("_evoskill_server_log_level"),
        "provider_id": options.get("provider_id"),
    }, sort_keys=True, default=str)

def _message_model_name(options: dict[str, Any]) -> str:
    if options.get("model"):
        return str(options["model"])
    provider_id = str(options.get("provider_id", "anthropic"))
    model_id = str(options.get("model_id", "claude-sonnet-4-6"))
    return f"{provider_id}/{model_id}"

def _build_message_body(options: dict[str, Any], query: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "parts": [{"type": "text", "text": query}],
        "model": {
            "providerID": options.get("provider_id", "anthropic"),
            "modelID": options.get("model_id", "claude-sonnet-4-6"),
        },
    }
    if options.get("agent"):
        body["agent"] = options["agent"]
    if options.get("system"):
        body["system"] = options["system"]
    if options.get("mode"):
        body["mode"] = options["mode"]
    if options.get("format"):
        body["format"] = options["format"]
    if options.get("tools"):
        body["tools"] = options["tools"]
    return body

def request_shape_metadata(options: dict[str, Any], query: str) -> dict[str, Any]:
    body = _build_message_body(options, query)
    inline_config_raw = options.get("_evoskill_opencode_inline_config")
    inline_config = None
    inline_agent_prompt_len = None
    inline_agent_names: list[str] = []
    if isinstance(inline_config_raw, str) and inline_config_raw.strip():
        try:
            inline_config = json.loads(inline_config_raw)
        except json.JSONDecodeError:
            inline_config = None
        if isinstance(inline_config, dict):
            agents = inline_config.get("agent") or {}
            if isinstance(agents, dict):
                inline_agent_names = sorted(str(name) for name in agents.keys())
            chosen_agent = options.get("agent")
            if chosen_agent and isinstance(agents.get(chosen_agent), dict):
                inline_agent_prompt_len = len(
                    str((agents[chosen_agent] or {}).get("prompt", ""))
                )

    tools = body.get("tools") or {}
    tool_names = sorted(str(name) for name in tools.keys())
    format_value = body.get("format")
    format_schema = None
    format_required_count = None
    if isinstance(format_value, dict):
        format_schema = format_value.get("schema")
        if isinstance(format_schema, dict):
            format_required_count = len(format_schema.get("required") or [])

    payload_bytes = len(json.dumps(body, sort_keys=True).encode("utf-8"))
    provider_base_url, provider_base_url_source = _resolve_provider_base_url(
        str(options.get("provider_id", "anthropic"))
    )
    return {
        "provider_id": str(options.get("provider_id", "anthropic")),
        "model": _message_model_name(options),
        "requested_model": options.get("requested_model") or options.get("model"),
        "base_url": provider_base_url,
        "base_url_source": provider_base_url_source,
        "dispatch_style": (
            "async_poll"
            if options.get("_evoskill_async_message_polling")
            else "stream_wait"
        ),
        "agent": options.get("agent"),
        "inline_agent_names": inline_agent_names,
        "inline_agent_prompt_len": inline_agent_prompt_len,
        "mode": body.get("mode"),
        "format_present": "format" in body,
        "format_type": format_value.get("type") if isinstance(format_value, dict) else None,
        "format_required_count": format_required_count,
        "body_system_prompt_len": len(str(body.get("system", ""))),
        "user_prompt_len": len(query),
        "tool_surface_size": len(tool_names),
        "tool_names": tool_names,
        "payload_size_bytes": payload_bytes,
        "polling_enabled": bool(options.get("_evoskill_async_message_polling")),
        "request_timeout_sec": float(options.get("_evoskill_request_timeout_sec", _TIMEOUT) or _TIMEOUT),
        "cwd": str(options.get("cwd") or ""),
        "body_keys": sorted(str(key) for key in body.keys()),
        "body_preview": {
            "agent": body.get("agent"),
            "mode": body.get("mode"),
            "has_system": "system" in body,
            "has_format": "format" in body,
            "has_tools": "tools" in body,
        },
    }

def _assistant_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        msg
        for msg in messages
        if str((msg.get("info") or {}).get("role", "")).strip().lower() == "assistant"
    ]

def _extract_assistant_text(messages: list[dict[str, Any]]) -> str:
    texts: list[str] = []
    for msg in _assistant_messages(messages):
        for part in msg.get("parts", []) or []:
            if part.get("type") == "text" and part.get("text"):
                texts.append(str(part["text"]))
    return "".join(texts).strip()

def _assistant_has_structured_output(messages: list[dict[str, Any]]) -> bool:
    for msg in _assistant_messages(messages):
        info = msg.get("info", {}) or {}
        if info.get("structured") is not None or info.get("structured_output") is not None:
            return True
    return False

def _assistant_has_meaningful_output(messages: list[dict[str, Any]]) -> bool:
    return bool(_extract_assistant_text(messages) or _assistant_has_structured_output(messages))

def _build_message_diagnostic(options: dict[str, Any], *, base_url: str, query: str) -> dict[str, Any]:
    provider_id = str(options.get("provider_id", "anthropic")).strip().lower()
    provider_base_url, provider_base_url_source = _resolve_provider_base_url(provider_id)
    log_dir = _project_log_dir(options.get("cwd"))
    retry_attempt = max(int(options.get("_evoskill_retry_attempt", 1) or 1), 1)
    retry_limit = max(int(options.get("_evoskill_retry_limit", 1) or 1), 1)
    request_timeout_sec = float(options.get("_evoskill_request_timeout_sec", _TIMEOUT) or _TIMEOUT)
    requested_model_name = options.get("requested_model") or options.get("model")
    resolved_model_name = _message_model_name(options)

    return {
        "run_id": uuid.uuid4().hex,
        "harness": "opencode",
        "probe_type": options.get("_evoskill_probe_type", "opencode_message"),
        "request_kind": "message",
        "provider_id": provider_id,
        "provider_mode": options.get("mode"),
        "provider_adapter": "opencode_http_session_message",
        "provider_transport": "httpx_async_stream",
        "provider_base_url": provider_base_url,
        "provider_base_url_source": provider_base_url_source,
        "server_base_url": base_url,
        "model_name": resolved_model_name,
        "requested_model_name": requested_model_name,
        "resolved_model_name": resolved_model_name,
        "request_timeout_sec": request_timeout_sec,
        "payload_size_bytes": 0,
        "query_size_bytes": len(query.encode("utf-8")),
        "retry_attempt": retry_attempt,
        "retry_limit": retry_limit,
        "retry_count": retry_attempt - 1,
        "provider_request_started": False,
        "provider_http_headers_received": False,
        "provider_response_started": False,
        "provider_first_chunk_received": False,
        "provider_stream_complete": False,
        "provider_response_completed": False,
        "provider_response_latency_sec": None,
        "response_header_latency_sec": None,
        "first_chunk_latency_sec": None,
        "provider_exception": None,
        "provider_exception_type": None,
        "parse_failures": 0,
        "stream_parse_failures": 0,
        "message_poll_iterations": 0,
        "tool_selection_started": False,
        "tool_selection_completed": False,
        "assistant_output_tokens": 0,
        "assistant_reasoning_tokens": 0,
        "completion_observed": False,
        "empty_success_observed": False,
        "stop_reason": None,
        "suspected_stall_stage": "before_request",
        "summary_path": str(log_dir / _SUMMARY_LOG_NAME),
        "event_path": str(log_dir / _EVENT_LOG_NAME),
    }

def _emit_diagnostic_event(options: dict[str, Any], diagnostic: dict[str, Any], event: str, **fields: Any) -> None:
    payload = {
        "event": event,
        "run_id": diagnostic.get("run_id"),
        "session_id": diagnostic.get("session_id"),
        "provider_id": diagnostic.get("provider_id"),
        "model_name": diagnostic.get("model_name"),
        **{key: _safe_json(value) for key, value in fields.items()},
    }
    logger.info("opencode_diagnostic %s", json.dumps(payload, sort_keys=True))
    _append_jsonl(Path(diagnostic["event_path"]), payload)

def _message_response_preview(raw_body: bytes, limit: int = 240) -> str:
    return raw_body.decode("utf-8", errors="replace")[:limit]

def _normalize_chat_info_candidate(candidate: Any) -> dict[str, Any] | None:
    if isinstance(candidate, dict):
        if isinstance(candidate.get("data"), dict):
            return candidate["data"]
        return candidate
    if isinstance(candidate, list):
        for item in reversed(candidate):
            normalized = _normalize_chat_info_candidate(item)
            if normalized is not None:
                return normalized
        return None

def _parse_message_response_body(
    raw_body: bytes,
    options: dict[str, Any],
    diagnostic: dict[str, Any],
) -> dict[str, Any]:
    text = raw_body.decode("utf-8", errors="replace").strip()
    parse_attempts: list[tuple[str, str]] = []
    candidates: list[tuple[str, str]] = [("json", text)]

    if text:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        data_lines = [line[5:].strip() for line in lines if line.startswith("data:")]
        if lines:
            candidates.append(("ndjson_last", lines[-1]))
        if data_lines:
            last_data = next(
                (line for line in reversed(data_lines) if line and line != "[DONE]"),
                "",
            )
            if last_data:
                candidates.append(("sse_last", last_data))

    seen_payloads: set[tuple[str, str]] = set()
    for parser_name, candidate_text in candidates:
        key = (parser_name, candidate_text)
        if key in seen_payloads or not candidate_text:
            continue
        seen_payloads.add(key)
        try:
            normalized = _normalize_chat_info_candidate(json.loads(candidate_text))
            if normalized is not None:
                diagnostic["message_response_parser"] = parser_name
                return normalized
            parse_attempts.append((parser_name, "Decoded payload was not a JSON object"))
        except json.JSONDecodeError as exc:
            parse_attempts.append((parser_name, f"{type(exc).__name__}: {exc}"))

    diagnostic["stream_parse_failures"] += max(len(parse_attempts), 1)
    diagnostic["parse_failures"] += max(len(parse_attempts), 1)
    _emit_diagnostic_event(
        options,
        diagnostic,
        "provider_stream_parse_failed",
        attempted_parsers=[name for name, _ in parse_attempts] or ["json"],
        failures=[f"{name}: {error}" for name, error in parse_attempts] or ["json: empty body"],
        body_preview=_message_response_preview(raw_body),
    )
    raise ValueError("Failed to parse OpenCode /message response stream as JSON")

def _extract_stop_reason(*sources: Any) -> str | None:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("stop_reason", "stopReason", "finish_reason", "finishReason", "status", "state"):
            value = source.get(key)
            if value:
                return str(value)
    return None

def _part_type(part: dict[str, Any]) -> str:
    return str(part.get("type", "")).strip().lower()

def _is_tool_part(part: dict[str, Any]) -> bool:
    part_type = _part_type(part)
    return "tool" in part_type or "function" in part_type or part_type in {"call", "invocation"}

def _extract_usage_tokens(usage: dict[str, Any]) -> tuple[int, int]:
    output = usage.get("output")
    if output is None:
        output = usage.get("completion")
    if output is None:
        output = usage.get("output_tokens", 0)

    reasoning = usage.get("reasoning")
    if reasoning is None:
        reasoning = usage.get("reasoning_output")
    if reasoning is None:
        reasoning = usage.get("reasoning_tokens", 0)

    try:
        output_tokens = int(output or 0)
    except (TypeError, ValueError):
        output_tokens = 0
    try:
        reasoning_tokens = int(reasoning or 0)
    except (TypeError, ValueError):
        reasoning_tokens = 0
    return output_tokens, reasoning_tokens

def _detect_tool_selection(messages: list[dict[str, Any]]) -> tuple[bool, bool]:
    tool_indices: list[int] = []
    assistant_after_tool = False

    for index, msg in enumerate(messages):
        info = msg.get("info", {}) or {}
        role = str(info.get("role", "")).strip().lower()
        parts = msg.get("parts", []) or []
        has_tool_activity = role in {"tool", "function"} or any(_is_tool_part(part) for part in parts)
        if has_tool_activity:
            tool_indices.append(index)

    if not tool_indices:
        return False, False

    last_tool_index = tool_indices[-1]
    for msg in messages[last_tool_index + 1:]:
        info = msg.get("info", {}) or {}
        role = str(info.get("role", "")).strip().lower()
        parts = msg.get("parts", []) or []
        if role == "assistant" and any(part.get("text") or _part_type(part) == "text" for part in parts):
            assistant_after_tool = True
            break

    return True, assistant_after_tool

def _capture_message_snapshot(messages: list[dict[str, Any]], diagnostic: dict[str, Any]) -> None:
    assistant_count = 0
    last_role = None
    for msg in messages:
        info = msg.get("info", {}) or {}
        role = info.get("role")
        if role:
            last_role = str(role)
        if role == "assistant":
            assistant_count += 1

    diagnostic["message_count"] = len(messages)
    diagnostic["assistant_message_count"] = assistant_count
    diagnostic["last_message_role"] = last_role

async def _fetch_provider_catalog(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    response = await client.get("/provider")
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict):
        all_providers = payload.get("all")
        if isinstance(all_providers, list):
            return [item for item in all_providers if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []

def _find_provider_entry(
    providers: list[dict[str, Any]],
    provider_id: str,
) -> dict[str, Any] | None:
    normalized = provider_id.strip().lower()
    for provider in providers:
        if str(provider.get("id", "")).strip().lower() == normalized:
            return provider
    return None

def _provider_catalog_error(provider_id: str, model_id: str, providers: list[dict[str, Any]]) -> RuntimeError:
    provider = _find_provider_entry(providers, provider_id)
    available_provider_ids = sorted(
        str(item.get("id", "")).strip()
        for item in providers
        if item.get("id")
    )

    if provider is None:
        if provider_id.strip().lower() == "ollama":
            local_hint = (
                "Local Ollama is not registered in OpenCode's provider catalog. "
                "EvoSkill expects a project-local provider entry named 'ollama' "
                "using @ai-sdk/openai-compatible and a local baseURL like "
                "http://127.0.0.1:11434/v1."
            )
        else:
            local_hint = "Requested provider is not registered in OpenCode."
        raise RuntimeError(
            f"{local_hint} Requested model: {provider_id}/{model_id}. "
            f"Available providers: {', '.join(available_provider_ids[:12]) or 'none'}"
        )

    models = provider.get("models") or {}
    if model_id not in models:
        available_models = sorted(str(key) for key in models.keys())[:20]
        raise RuntimeError(
            f"OpenCode provider '{provider_id}' is registered but model '{model_id}' is unavailable. "
            f"Available models include: {', '.join(available_models) or 'none'}"
        )

    return RuntimeError("Unknown provider catalog resolution failure")

async def _ensure_provider_model_available(
    client: httpx.AsyncClient,
    options: dict[str, Any],
    diagnostic: dict[str, Any],
) -> None:
    provider_id = str(options.get("provider_id", "anthropic"))
    model_id = str(options.get("model_id", "claude-sonnet-4-6"))
    providers = await _fetch_provider_catalog(client)
    provider = _find_provider_entry(providers, provider_id)
    if provider is None:
        diagnostic["provider_catalog_checked"] = True
        diagnostic["provider_catalog_available_ids"] = [
            str(item.get("id", "")).strip()
            for item in providers
            if item.get("id")
        ]
        raise _provider_catalog_error(provider_id, model_id, providers)
    models = provider.get("models") or {}
    if model_id not in models:
        diagnostic["provider_catalog_checked"] = True
        diagnostic["provider_catalog_available_ids"] = [
            str(item.get("id", "")).strip()
            for item in providers
            if item.get("id")
        ]
        diagnostic["provider_catalog_available_models"] = sorted(str(key) for key in models.keys())
        raise _provider_catalog_error(provider_id, model_id, providers)
    diagnostic["provider_catalog_checked"] = True

async def _poll_session_messages(
    client: httpx.AsyncClient,
    session_id: str,
    options: dict[str, Any],
    diagnostic: dict[str, Any],
    *,
    poll_interval_sec: float = 1.0,
) -> tuple[list[dict[str, Any]], bool]:
    deadline = time.monotonic() + float(diagnostic.get("request_timeout_sec") or _TIMEOUT)
    last_messages: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        diagnostic["message_poll_iterations"] = int(diagnostic.get("message_poll_iterations", 0) or 0) + 1
        message_response = await client.get(f"/session/{session_id}/message")
        message_response.raise_for_status()
        messages = message_response.json()
        last_messages = messages
        _capture_message_snapshot(messages, diagnostic)
        if _assistant_has_meaningful_output(messages):
            return messages, True
        await asyncio.sleep(poll_interval_sec)
    return last_messages, False

def _suspected_stall_stage(diagnostic: dict[str, Any], *, has_assistant_output: bool) -> str:
    if not diagnostic.get("provider_request_started"):
        return "before_request"
    if diagnostic.get("tool_selection_started") and not diagnostic.get("tool_selection_completed"):
        return "tool_selection_deadlock"
    if diagnostic.get("tool_selection_completed") and not has_assistant_output:
        return "post_action_no_return"
    if diagnostic.get("parse_failures", 0):
        return "parse_retry_loop"
    if diagnostic.get("empty_success_observed"):
        return "unknown"
    if diagnostic.get("provider_http_headers_received") and not diagnostic.get("provider_first_chunk_received"):
        return "stream_wait"
    if diagnostic.get("provider_request_started") and not diagnostic.get("provider_http_headers_received"):
        return "provider_wait"
    return "unknown"

def _write_diagnostic_summary(diagnostic: dict[str, Any]) -> None:
    summary = {
        key: _safe_json(value)
        for key, value in diagnostic.items()
        if key not in {"event_path", "summary_path"}
    }
    _append_jsonl(Path(diagnostic["summary_path"]), summary)

async def _execute_query_via_async_poll(
    client: httpx.AsyncClient,
    options: dict[str, Any],
    diagnostic: dict[str, Any],
    query: str,
) -> list[Any]:
    session_response = await client.post("/session", json={})
    session_response.raise_for_status()
    session_id = session_response.json()["id"]
    diagnostic["session_id"] = session_id

    body = _build_message_body(options, query)

    diagnostic["payload_size_bytes"] = len(json.dumps(body, sort_keys=True).encode("utf-8"))
    request_started_at = time.monotonic()
    diagnostic["provider_request_started"] = True
    _emit_diagnostic_event(
        options,
        diagnostic,
        "provider_http_request_start",
        base_url=diagnostic.get("server_base_url"),
        provider_base_url=diagnostic.get("provider_base_url"),
        provider_mode=diagnostic.get("provider_mode"),
        adapter_used=diagnostic.get("provider_adapter"),
        model_name=diagnostic.get("model_name"),
        timeout_sec=diagnostic.get("request_timeout_sec"),
        payload_size_bytes=diagnostic.get("payload_size_bytes"),
        retry_count=diagnostic.get("retry_count"),
        agent=options.get("agent"),
        dispatch_style="async_poll",
    )

    response_task = asyncio.create_task(client.post(f"/session/{session_id}/message", json=body))
    messages: list[dict[str, Any]] = []
    completed = False

    try:
        while True:
            if response_task.done() and not diagnostic.get("provider_http_headers_received"):
                response = response_task.result()
                diagnostic["provider_http_headers_received"] = True
                diagnostic["provider_response_started"] = True
                diagnostic["response_header_latency_sec"] = _elapsed_seconds(request_started_at)
                _emit_diagnostic_event(
                    options,
                    diagnostic,
                    "provider_http_headers_received",
                    status_code=response.status_code,
                    content_type=response.headers.get("content-type"),
                    transfer_encoding=response.headers.get("transfer-encoding"),
                    content_length=response.headers.get("content-length"),
                    header_latency_sec=diagnostic.get("response_header_latency_sec"),
                )
                response.raise_for_status()
                if response.text.strip():
                    diagnostic["provider_first_chunk_received"] = True
                    diagnostic["first_chunk_latency_sec"] = _elapsed_seconds(request_started_at)
                    _emit_diagnostic_event(
                        options,
                        diagnostic,
                        "provider_first_chunk_received",
                        first_chunk_latency_sec=diagnostic.get("first_chunk_latency_sec"),
                        first_chunk_size_bytes=len(response.content),
                    )
                diagnostic["provider_stream_complete"] = True
                diagnostic["provider_response_completed"] = True
                diagnostic["provider_response_latency_sec"] = _elapsed_seconds(request_started_at)
                chat_info = _parse_message_response_body(response.content, options, diagnostic)
                message_response = await client.get(f"/session/{session_id}/message")
                message_response.raise_for_status()
                messages = message_response.json()
                _capture_message_snapshot(messages, diagnostic)
                return [{
                    "session_id": session_id,
                    "chat_info": chat_info,
                    "messages": messages,
                    "diagnostics": diagnostic,
                }]

            messages, completed = await _poll_session_messages(
                client,
                session_id,
                options,
                diagnostic,
            )
            if completed:
                break
            if response_task.done():
                break

        if response_task.done() and not diagnostic.get("provider_http_headers_received"):
            response = response_task.result()
            diagnostic["provider_http_headers_received"] = True
            diagnostic["provider_response_started"] = True
            diagnostic["response_header_latency_sec"] = _elapsed_seconds(request_started_at)
            _emit_diagnostic_event(
                options,
                diagnostic,
                "provider_http_headers_received",
                status_code=response.status_code,
                content_type=response.headers.get("content-type"),
                transfer_encoding=response.headers.get("transfer-encoding"),
                content_length=response.headers.get("content-length"),
                header_latency_sec=diagnostic.get("response_header_latency_sec"),
            )
            response.raise_for_status()

        if completed:
            if response_task.done():
                response = response_task.result()
                diagnostic["provider_http_headers_received"] = True
                diagnostic["provider_response_started"] = True
                diagnostic["response_header_latency_sec"] = diagnostic.get("response_header_latency_sec") or _elapsed_seconds(request_started_at)
                if response.text.strip():
                    diagnostic["provider_first_chunk_received"] = True
                    diagnostic["first_chunk_latency_sec"] = diagnostic.get("first_chunk_latency_sec") or _elapsed_seconds(request_started_at)
            else:
                response_task.cancel()
                try:
                    await response_task
                except BaseException:
                    pass
            diagnostic["provider_stream_complete"] = True
            diagnostic["provider_response_completed"] = True
            diagnostic["provider_response_latency_sec"] = _elapsed_seconds(request_started_at)
            _emit_diagnostic_event(
                options,
                diagnostic,
                "provider_stream_complete",
                chunk_count=0,
                total_bytes=0,
                response_latency_sec=diagnostic.get("provider_response_latency_sec"),
                completion_source="message_poll",
            )
            return [{
                "session_id": session_id,
                "chat_info": {},
                "messages": messages,
                "diagnostics": diagnostic,
            }]

        if not response_task.done():
            response_task.cancel()
            try:
                await response_task
            except BaseException:
                pass
        if int(diagnostic.get("assistant_message_count", 0) or 0) > 0:
            raise RuntimeError(
                "OpenCode persisted only an empty assistant placeholder before timeout. "
                "This indicates provider processing started but no assistant text was materialized."
            )
        raise TimeoutError(
            "OpenCode /message dispatch did not produce assistant output before timeout."
        )
    finally:
        if not response_task.done():
            response_task.cancel()
            try:
                await response_task
            except BaseException:
                pass

def _kill_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    for _ in range(10):
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass

def _kill_all_opencode_servers() -> None:
    """Kill all OpenCode serve processes on this machine."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/IM", "opencode.exe"],
                capture_output=True,
                timeout=5,
            )
            subprocess.run(
                ["taskkill", "/F", "/IM", "node.exe"],
                capture_output=True,
                timeout=5,
            )
        else:
            subprocess.run(
                ["pkill", "-f", "opencode serve"],
                capture_output=True,
                timeout=5,
            )
    except Exception:
        pass

def _resolve_opencode_executable() -> str:
    """Return a launchable OpenCode executable path across platforms."""
    candidates: list[str] = []

    direct = shutil.which("opencode")
    if direct:
        candidates.append(direct)

    if os.name == "nt":
        direct_cmd = shutil.which("opencode.cmd")
        if direct_cmd:
            candidates.append(direct_cmd)
        appdata = os.environ.get("APPDATA", "").strip()
        if appdata:
            npm_dir = Path(appdata) / "npm"
            candidates.extend([
                str(npm_dir / "opencode.cmd"),
                str(npm_dir / "opencode.ps1")
            ])

    seen: set[str] = set()
    for candidate in candidates:
        resolved = str(Path(candidate).resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        if Path(resolved).exists():
            return resolved

    raise FileNotFoundError(
        "OpenCode executable not found. Install `opencode-ai` and ensure "
        "`opencode` or `opencode.cmd` is reachable."
    )

def shutdown_project_server(project_root: str | Path | None) -> None:
    key = _resolve_key(project_root)
    pid = _SERVER_PIDS.pop(key, None)
    if pid is not None:
        _kill_pid(pid)
    handle = _SERVER_LOG_HANDLES.pop(key, None)
    if handle is not None:
        try:
            handle.close()
        except Exception:
            pass
    _SERVER_PORTS.pop(key, None)
    _SERVER_SIGNATURES.pop(key, None)
    _SPAWNED_THIS_RUN.discard(key)

def shutdown_all_servers() -> None:
    for key in list(set(_SERVER_PORTS) | set(_SERVER_PIDS) | set(_SPAWNED_THIS_RUN)):
        shutdown_project_server(key)

def _wait_for_port(port: int, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.5)

def _push_provider_auth(base_url: str) -> None:
    """Push any configured provider API keys into the OpenCode auth store."""
    for provider, env_vars in PROVIDER_ENV_KEYS.items():
        for env_name in env_vars:
            key = os.environ.get(env_name)
            if key:
                try:
                    httpx.put(
                        f"{base_url}/auth/{provider}",
                        json={"type": "api", "key": key},
                        timeout=5,
                    )
                except Exception:
                    pass
                break

def _ensure_server(options: dict[str, Any]) -> str:
    """Return ``http://127.0.0.1:<port>`` for a running OpenCode server."""
    key = _resolve_key(options.get("cwd"))
    signature = _server_signature(options)

    # Create a diagnostic dictionary for server events
    diagnostic = _build_message_diagnostic(options, base_url="", query="")
    diagnostic["run_id"] = uuid.uuid4().hex
    diagnostic["probe_type"] = "server_management"
    diagnostic["request_kind"] = "server_start"

    # DEBUG: Log server reuse decision
    if key in _SPAWNED_THIS_RUN:
        reuse = _SERVER_SIGNATURES.get(key) == signature
        _emit_diagnostic_event(options, diagnostic, "server_reuse_decision", reuse=reuse, signature_match=_SERVER_SIGNATURES.get(key) == signature, current_signature=signature, cached_signature=_SERVER_SIGNATURES.get(key))
        if reuse:
            port = _SERVER_PORTS.get(key)
            if port is not None:
                return f"http://127.0.0.1:{port}"
        else:
            shutdown_project_server(key)

    # DEBUG: Log server restart decision
    _emit_diagnostic_event(options, diagnostic, "server_restart", reason="signature mismatch or new key")
    _kill_all_opencode_servers()
    time.sleep(0.5)

    port = _find_free_port()
    env = dict(os.environ)
    apply_provider_auth_env(options.get("provider_id"), env)

    # Use official OpenCode config injection path via OPENCODE_CONFIG_CONTENT
    # Build config in the same format as ollama launch opencode --config
    config = {
        "provider": {
            "ollama": {
                "name": "Ollama (Local)",
                "npm": "@ai-sdk/openai-compatible",
                "options": {
                    "baseURL": "http://127.0.0.1:11434/v1",
                    "apiKey": "ollama"
                },
                "models": {
                    "gpt-oss:20b": {"name": "gpt-oss:20b"},
                    "qwen3-coder:30b": {"name": "qwen3-coder:30b"}
                }
            }
        }
    }

    # DEBUG: Log effective config being injected
    _emit_diagnostic_event(options, diagnostic, "server_config_injected", config=json.dumps(config, sort_keys=True))

    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config, sort_keys=True)

    # DEBUG: Log presence of OPENCODE_CONFIG_CONTENT in env
    _emit_diagnostic_event(options, diagnostic, "env_opencode_config_present", value="OPENCODE_CONFIG_CONTENT" in env)
    executable = _resolve_opencode_executable()
    command = [executable, "serve", "--port", str(port), "--hostname", "127.0.0.1"]

    stdout_target: Any = subprocess.DEVNULL
    stderr_target: Any = subprocess.DEVNULL
    server_log_path = options.get("_evoskill_server_log_path")
    if server_log_path:
        log_path = Path(str(server_log_path))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("a", encoding="utf-8")
        _SERVER_LOG_HANDLES[key] = log_handle
        stdout_target = log_handle
        stderr_target = subprocess.STDOUT
        command.extend([
            "--print-logs",
            "--log-level",
            str(options.get("_evoskill_server_log_level", "DEBUG")),
        ])

    proc = subprocess.Popen(
        command,
        cwd=options.get("cwd"),
        env=env,
        stdout=stdout_target,
        stderr=stderr_target,
        start_new_session=(os.name != "nt"),
        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
        shell=False,
    )

    _SERVER_PORTS[key] = port
    _SERVER_PIDS[key] = proc.pid
    _SPAWNED_THIS_RUN.add(key)
    _SERVER_SIGNATURES[key] = signature
    _wait_for_port(port)

    base_url = f"http://127.0.0.1:{port}"
    _push_provider_auth(base_url)
    return base_url

async def execute_query(options: dict[str, Any], query: str) -> list[Any]:
    if not isinstance(options, dict):
        raise TypeError(f"OpenCode executor requires dict options, got {type(options)}")

    ensure_provider_api_key(options.get("provider_id"))
    base_url = _ensure_server(options)
    diagnostic = _build_message_diagnostic(options, base_url=base_url, query=query)
    request_timeout_sec = float(options.get("_evoskill_request_timeout_sec", _TIMEOUT) or _TIMEOUT)

    # DEBUG: Log /message handler entry point
    _emit_diagnostic_event(options, diagnostic, "message_handler_enter", request_kind="message", model_name=_message_model_name(options))

    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=request_timeout_sec) as client:
            await _ensure_provider_model_available(client, options, diagnostic)
            if options.get("_evoskill_async_message_polling"):
                return await _execute_query_via_async_poll(
                    client,
                    options,
                    diagnostic,
                    query,
                )
            session_response = await client.post("/session", json={})
            session_response.raise_for_status()
            session_id = session_response.json()["id"]
            diagnostic["session_id"] = session_id

            body = _build_message_body(options, query)

            diagnostic["payload_size_bytes"] = len(
                json.dumps(body, sort_keys=True).encode("utf-8")
            )

            request_started_at = time.monotonic()
            diagnostic["provider_request_started"] = True
            _emit_diagnostic_event(
                options,
                diagnostic,
                "provider_http_request_start",
                base_url=base_url,
                provider_base_url=diagnostic.get("provider_base_url"),
                provider_mode=diagnostic.get("provider_mode"),
                adapter_used=diagnostic.get("provider_adapter"),
                model_name=diagnostic.get("model_name"),
                timeout_sec=diagnostic.get("request_timeout_sec"),
                payload_size_bytes=diagnostic.get("payload_size_bytes"),
                retry_count=diagnostic.get("retry_count"),
            )

            request = client.build_request(
                "POST",
                f"/session/{session_id}/message",
                json=body,
            )

            # DEBUG: Log provider dispatch start
            _emit_diagnostic_event(options, diagnostic, "provider_dispatch_start", request_method="POST", request_url=f"/session/{session_id}/message")

            response = await client.send(request, stream=True)
            try:
                diagnostic["provider_http_headers_received"] = True
                diagnostic["provider_response_started"] = True
                diagnostic["response_header_latency_sec"] = _elapsed_seconds(request_started_at)
                _emit_diagnostic_event(
                    options,
                    diagnostic,
                    "provider_http_headers_received",
                    status_code=response.status_code,
                    content_type=response.headers.get("content-type"),
                    transfer_encoding=response.headers.get("transfer-encoding"),
                    content_length=response.headers.get("content-length"),
                    header_latency_sec=diagnostic.get("response_header_latency_sec"),
                )
                response.raise_for_status()

                raw_chunks: list[bytes] = []
                chunk_count = 0
                total_bytes = 0
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    chunk_count += 1
                    total_bytes += len(chunk)
                    if not diagnostic["provider_first_chunk_received"]:
                        diagnostic["provider_first_chunk_received"] = True
                        diagnostic["first_chunk_latency_sec"] = _elapsed_seconds(request_started_at)
                        _emit_diagnostic_event(
                            options,
                            diagnostic,
                            "provider_first_chunk_received",
                            first_chunk_latency_sec=diagnostic.get("first_chunk_latency_sec"),
                            first_chunk_size_bytes=len(chunk),
                        )
                    raw_chunks.append(chunk)

                diagnostic["provider_stream_complete"] = True
                diagnostic["provider_response_completed"] = True
                diagnostic["provider_response_latency_sec"] = _elapsed_seconds(request_started_at)
                _emit_diagnostic_event(
                    options,
                    diagnostic,
                    "provider_stream_complete",
                    chunk_count=chunk_count,
                    total_bytes=total_bytes,
                    response_latency_sec=diagnostic.get("provider_response_latency_sec"),
                )
                try:
                    chat_info = _parse_message_response_body(
                        b"".join(raw_chunks),
                        options,
                        diagnostic,
                    )
                except ValueError:
                    diagnostic["message_poll_iterations"] = 1
                    try:
                        message_response = await client.get(f"/session/{session_id}/message")
                        message_response.raise_for_status()
                        snapshot_messages = message_response.json()
                        _capture_message_snapshot(snapshot_messages, diagnostic)
                    except Exception as snapshot_exc:
                        diagnostic["message_snapshot_exception"] = (
                            f"{type(snapshot_exc).__name__}: {snapshot_exc}"
                        )
                    raise
                finally:
                    await response.aclose()

                diagnostic["message_poll_iterations"] = 1
                message_response = await client.get(f"/session/{session_id}/message")
                message_response.raise_for_status()
                messages = message_response.json()
                _capture_message_snapshot(messages, diagnostic)
                if not raw_chunks and int(diagnostic.get("assistant_message_count", 0) or 0) == 0:
                    diagnostic["empty_success_observed"] = True
                    raise RuntimeError(
                        "OpenCode /message returned HTTP 200 with an empty body and persisted no assistant message. "
                        "This usually means the server hit an internal error after committing response headers."
                    )
            except Exception as exc:
                # DEBUG: Log provider dispatch exception
                _emit_diagnostic_event(options, diagnostic, "provider_dispatch_exception", exception_type=type(exc).__name__, exception=str(exc))
                diagnostic["provider_exception_type"] = type(exc).__name__
                diagnostic["provider_exception"] = str(exc)
                diagnostic["suspected_stall_stage"] = _suspected_stall_stage(
                    diagnostic,
                    has_assistant_output=False,
                )
                _emit_diagnostic_event(
                    options,
                    diagnostic,
                    "provider_request_exception",
                    exception_type=diagnostic["provider_exception_type"],
                    exception=diagnostic["provider_exception"],
                    suspected_stall_stage=diagnostic["suspected_stall_stage"],
                )
                _write_diagnostic_summary(diagnostic)
                raise

            return [{
                "session_id": session_id,
                "chat_info": chat_info,
                "messages": messages,
                "diagnostics": diagnostic,
            }]

    except Exception as exc:
        # DEBUG: Log provider dispatch exception
        _emit_diagnostic_event(options, diagnostic, "provider_dispatch_exception", exception_type=type(exc).__name__, exception=str(exc))
        diagnostic["provider_exception_type"] = type(exc).__name__
        diagnostic["provider_exception"] = str(exc)
        diagnostic["suspected_stall_stage"] = _suspected_stall_stage(
            diagnostic,
            has_assistant_output=False,
        )
        _emit_diagnostic_event(
            options,
            diagnostic,
            "provider_request_exception",
            exception_type=diagnostic["provider_exception_type"],
            exception=diagnostic["provider_exception"],
            suspected_stall_stage=diagnostic["suspected_stall_stage"],
        )
        _write_diagnostic_summary(diagnostic)
        raise

def parse_response(
    messages: list[Any],
    response_model: Type[BaseModel],
    get_options: Callable[[], Any],
) -> dict[str, Any]:
    payload = messages[0]
    all_msgs: list[dict[str, Any]] = payload.get("messages", [])
    diagnostic: dict[str, Any] = dict(payload.get("diagnostics") or {})

    assistant_info: dict[str, Any] = {}
    assistant_parts: list[dict[str, Any]] = []
    for msg in reversed(all_msgs):
        info = msg.get("info", {})
        if info.get("role") == "assistant":
            assistant_info = info
            assistant_parts = msg.get("parts", [])
            break

    result_text = "".join(
        part.get("text", "")
        for part in assistant_parts
        if part.get("type") == "text"
    )

    output = None
    parse_error = None
    parse_attempt_failed = False
    raw_structured = assistant_info.get("structured")
    if raw_structured is None and assistant_info.get("structured_output") is not None:
        raw_structured = assistant_info.get("structured_output")

    if raw_structured is not None:
        try:
            output = response_model.model_validate(raw_structured)
        except (ValidationError, TypeError, ValueError) as exc:
            parse_error = f"{type(exc).__name__}: {exc}"
            parse_attempt_failed = True

    if output is None and result_text.strip():
        text = result_text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            lines = lines[1:] if lines[0].startswith("```") else lines
            lines = lines[:-1] if lines and lines[-1].strip() == "```" else lines
            text = "\n".join(lines).strip()
        try:
            parsed = json.loads(text)
            output = response_model.model_validate(parsed)
            raw_structured = parsed
            parse_error = None
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
            if parse_error is None:
                parse_error = f"{type(exc).__name__}: {exc}"
            parse_attempt_failed = True

    if output is None and parse_error is None:
        parse_error = "No structured output returned (context limit likely exceeded)"
        parse_attempt_failed = True

    cost = assistant_info.get("cost", 0.0) or 0.0
    usage = assistant_info.get("tokens", {}) or {}

    opts = get_options()
    model = _message_model_name(opts) if isinstance(opts, dict) else "unknown"
    tools = list(opts.get("tools", {}).keys()) if isinstance(opts, dict) and opts.get("tools") else []

    session_id = payload.get("session_id", "unknown")
    tool_selection_started, tool_selection_completed = _detect_tool_selection(all_msgs)
    assistant_output_tokens, assistant_reasoning_tokens = _extract_usage_tokens(usage)
    has_assistant_output = bool(result_text.strip() or raw_structured is not None)

    if diagnostic:
        diagnostic["session_id"] = session_id
        diagnostic["stop_reason"] = _extract_stop_reason(assistant_info, payload.get("chat_info"))
        diagnostic["tool_selection_started"] = tool_selection_started
        diagnostic["tool_selection_completed"] = tool_selection_completed
        diagnostic["assistant_output_tokens"] = assistant_output_tokens
        diagnostic["assistant_reasoning_tokens"] = assistant_reasoning_tokens
        diagnostic["completion_observed"] = has_assistant_output
        if parse_attempt_failed:
            diagnostic["parse_failures"] = int(diagnostic.get("parse_failures", 0) or 0) + 1
        if isinstance(opts, dict):
            _emit_diagnostic_event(
                opts,
                diagnostic,
                "provider_stream_parse_failed",
                parse_error=parse_error,
                structured_output_present=raw_structured is not None,
                assistant_text_present=bool(result_text.strip()),
            )
        diagnostic["suspected_stall_stage"] = _suspected_stall_stage(
            diagnostic,
            has_assistant_output=has_assistant_output,
        )
        _write_diagnostic_summary(diagnostic)

    return {
        "uuid": session_id,
        "session_id": session_id,
        "model": model,
        "tools": tools,
        "duration_ms": 0,
        "total_cost_usd": cost,
        "num_turns": 1,
        "usage": usage,
        "result": result_text,
        "is_error": parse_error is not None,
        "output": output,
        "parse_error": parse_error,
        "raw_structured_output": raw_structured,
        "messages": messages,
        "diagnostics": diagnostic or None,
    }

async def run_diagnostic_probe(
    options: dict[str, Any],
    *,
    prompt: str = "say hello",
    include_minimal_tools: bool = False,
) -> dict[str, Any]:
    """Run a tiny provider-path probe using the same provider/model/cwd as real runs."""
    if not isinstance(options, dict):
        raise TypeError(f"OpenCode diagnostic probe requires dict options, got {type(options)}")

    probe_options = dict(options)
    probe_options["system"] = (
        "Reply with compact JSON matching the schema. "
        "Set reply to a short hello."
    )
    probe_options["format"] = {
        "type": "json_schema",
        "schema": _DiagnosticProbeResponse.model_json_schema(),
    }
    probe_options["tools"] = {"read": True} if include_minimal_tools else {}
    probe_options.setdefault("mode", "build")

    probe_messages = await execute_query(probe_options, prompt)
    return parse_response(probe_messages, _DiagnosticProbeResponse, lambda: probe_options)

async def run_minimal_reply_smoke(
    *,
    prompt: str = "this is a test give me a reply",
    project_root: str | Path | None = None,
    model: str | None = None,
    timeout_seconds: float = 45.0,
    server_log_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run the smallest truthful EvoSkill+OpenCode one-shot reply path.

    This uses the real OpenCode server/session/message flow, but swaps in a
    tiny runtime-only agent and polls session state for assistant text instead
    of requiring structured-output/tool/build semantics.
    """
    from .options import build_opencode_minimal_reply_options

    options = build_opencode_minimal_reply_options(
        project_root=project_root,
        model=model,
        timeout_seconds=timeout_seconds,
    )
    if server_log_path is not None:
        options["_evoskill_server_log_path"] = str(server_log_path)

    messages = await execute_query(options, prompt)
    payload = messages[0]
    all_msgs = payload.get("messages", [])
    reply = _extract_assistant_text(all_msgs)
    if not reply:
        raise RuntimeError("Minimal OpenCode smoke completed without assistant reply text.")

    diagnostics = dict(payload.get("diagnostics") or {})
    diagnostics["completion_observed"] = True
    diagnostics["suspected_stall_stage"] = "unknown"
    _write_diagnostic_summary(diagnostics)

    return {
        "prompt": prompt,
        "reply": reply,
        "model": options["model"],
        "messages": all_msgs,
        "diagnostics": diagnostics,
        "options": options,
    }

atexit.register(shutdown_all_servers)