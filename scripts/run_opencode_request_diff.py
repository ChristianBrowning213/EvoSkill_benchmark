from __future__ import annotations

import asyncio
import json
import os
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.harness.opencode import (  # noqa: E402
    build_opencode_minimal_reply_options,
    build_opencode_options,
    execute_query,
    request_shape_metadata,
    shutdown_project_server,
    to_opencode_tools,
)
from src.harness.opencode.executor import _extract_assistant_text  # noqa: E402
from src.schemas import AgentResponse  # noqa: E402


ARTIFACTS_DIR = ROOT / "artifacts"
FACTOR_PROBES_PATH = ARTIFACTS_DIR / "evoskill_request_factor_probes.json"
REQUEST_DIFF_PATH = ARTIFACTS_DIR / "evoskill_request_path_diff.json"
SUMMARY_PATH = ARTIFACTS_DIR / "EVOSKILL_REQUEST_DIFF_SUMMARY.md"
SUMMARY_LOG_PATH = ROOT / ".evoskill" / "logs" / "opencode_message_diagnostics.jsonl"

MODEL = "ollama/qwen3-coder:30b"
TIMEOUT_SEC = 45.0

MINIMAL_PROMPT = "this is a test give me a reply"
MINIMAL_SYSTEM = "You are a concise assistant. Reply directly to the user in one short sentence."

BENCHMARK_TOOLS = ["Read", "Write", "Bash", "Edit", "Glob", "Grep"]
BENCHMARK_PROMPT = (
    "You are solving a benchmark-style coding task in the current workspace. "
    "Inspect files, decide what to change, and return a structured final answer "
    "summarizing the fix and reasoning. Do not guess. Use the workspace as source of truth."
)
BENCHMARK_SYSTEM = (
    "You are an engineering agent working on a local repository task. "
    "Use the available tools to inspect files and, when needed, edit the workspace. "
    "Return JSON that matches the required schema with a concise final answer and reasoning."
)

MINIMAL_REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
    },
    "required": ["reply"],
    "additionalProperties": False,
}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


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


def _assistant_snapshot(messages: list[dict[str, Any]]) -> dict[str, Any]:
    assistant_text = _extract_assistant_text(messages)
    for message in reversed(messages):
        info = message.get("info", {}) or {}
        if str(info.get("role", "")).strip().lower() != "assistant":
            continue
        parts = message.get("parts", []) or []
        return {
            "assistant_message_present": True,
            "assistant_parts_count": len(parts),
            "assistant_text_present": bool(assistant_text),
            "assistant_text_preview": assistant_text[:240],
            "assistant_structured_present": info.get("structured") is not None
            or info.get("structured_output") is not None,
        }
    return {
        "assistant_message_present": False,
        "assistant_parts_count": 0,
        "assistant_text_present": False,
        "assistant_text_preview": "",
        "assistant_structured_present": False,
    }


def _probe_classification(diagnostic: dict[str, Any], assistant_text_present: bool) -> str:
    if diagnostic.get("provider_response_completed") and assistant_text_present:
        return "completed"
    if diagnostic.get("provider_response_started") and not assistant_text_present:
        return "post_response_no_materialized_output"
    if diagnostic.get("provider_request_started") and not diagnostic.get("provider_response_started"):
        return "provider_wait"
    if diagnostic.get("parse_failures"):
        return "result_parse"
    return "unknown"


async def _run_probe(
    probe_id: str,
    prompt: str,
    options: dict[str, Any],
) -> dict[str, Any]:
    shutdown_project_server(ROOT)
    previous_count = _read_summary_line_count()
    result: dict[str, Any] = {
        "probe_id": probe_id,
        "started_at_utc": _utc_now(),
        "request_shape": request_shape_metadata(options, prompt),
    }

    try:
        payload = (await execute_query(options, prompt))[0]
        diagnostic = dict(payload.get("diagnostics") or {})
        messages = payload.get("messages") or []
        assistant = _assistant_snapshot(messages)
        result.update({
            "success": True,
            "diagnostic": diagnostic,
            "assistant": assistant,
            "classification": _probe_classification(diagnostic, assistant["assistant_text_present"]),
        })
        return result
    except Exception as exc:
        diagnostic = _read_summary_after(previous_count) or {}
        assistant = {
            "assistant_message_present": False,
            "assistant_parts_count": 0,
            "assistant_text_present": False,
            "assistant_text_preview": "",
            "assistant_structured_present": False,
        }
        result.update({
            "success": False,
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
            "diagnostic": diagnostic,
            "assistant": assistant,
            "classification": _probe_classification(diagnostic, False),
        })
        return result


def _factor_diff(working: dict[str, Any], failing: dict[str, Any]) -> dict[str, Any]:
    working_shape = working["request_shape"]
    failing_shape = failing["request_shape"]
    keys = sorted(set(working_shape) | set(failing_shape))
    diff: dict[str, Any] = {}
    for key in keys:
        if working_shape.get(key) != failing_shape.get(key):
            diff[key] = {
                "working": working_shape.get(key),
                "failing": failing_shape.get(key),
            }
    return diff


def _build_minimal_async_options() -> dict[str, Any]:
    options = build_opencode_minimal_reply_options(
        project_root=ROOT,
        model=MODEL,
        timeout_seconds=TIMEOUT_SEC,
    )
    options["_evoskill_probe_type"] = "request_diff"
    return options


def _build_no_agent_async_options() -> dict[str, Any]:
    return {
        "provider_id": "ollama",
        "model_id": "qwen3-coder:30b",
        "model": MODEL,
        "requested_model": MODEL,
        "cwd": str(ROOT),
        "system": MINIMAL_SYSTEM,
        "tools": {},
        "_evoskill_async_message_polling": True,
        "_evoskill_request_timeout_sec": TIMEOUT_SEC,
        "_evoskill_probe_type": "request_diff",
    }


def _build_minimal_structured_async_options() -> dict[str, Any]:
    options = _build_no_agent_async_options()
    options["mode"] = "build"
    options["format"] = {
        "type": "json_schema",
        "schema": MINIMAL_REPLY_SCHEMA,
    }
    return options


def _build_benchmark_like_async_options() -> dict[str, Any]:
    options = build_opencode_options(
        system=BENCHMARK_SYSTEM,
        schema=AgentResponse.model_json_schema(),
        tools=BENCHMARK_TOOLS,
        project_root=ROOT,
        model=MODEL,
        mode="build",
    )
    options["_evoskill_async_message_polling"] = True
    options["_evoskill_request_timeout_sec"] = TIMEOUT_SEC
    options["_evoskill_probe_type"] = "request_diff"
    return options


def _build_benchmark_like_stream_options() -> dict[str, Any]:
    options = build_opencode_options(
        system=BENCHMARK_SYSTEM,
        schema=AgentResponse.model_json_schema(),
        tools=BENCHMARK_TOOLS,
        project_root=ROOT,
        model=MODEL,
        mode="build",
    )
    options["_evoskill_request_timeout_sec"] = TIMEOUT_SEC
    options["_evoskill_probe_type"] = "request_diff"
    return options


async def main() -> None:
    os.environ.setdefault("OLLAMA_API_BASE", "http://127.0.0.1:11434")

    probes: list[dict[str, Any]] = []

    working_minimal_async = await _run_probe(
        "working_minimal_async_agent",
        MINIMAL_PROMPT,
        _build_minimal_async_options(),
    )
    probes.append(working_minimal_async)

    working_minimal_stream = await _run_probe(
        "working_minimal_stream_agent",
        MINIMAL_PROMPT,
        {key: value for key, value in _build_minimal_async_options().items() if key != "_evoskill_async_message_polling"},
    )
    probes.append(working_minimal_stream)

    minimal_no_agent_async = await _run_probe(
        "minimal_async_no_agent_body_system",
        MINIMAL_PROMPT,
        _build_no_agent_async_options(),
    )
    probes.append(minimal_no_agent_async)

    minimal_structured_async = await _run_probe(
        "minimal_async_no_agent_mode_build_format",
        MINIMAL_PROMPT,
        _build_minimal_structured_async_options(),
    )
    probes.append(minimal_structured_async)

    benchmark_like_async = await _run_probe(
        "benchmark_like_async_poll",
        BENCHMARK_PROMPT,
        _build_benchmark_like_async_options(),
    )
    probes.append(benchmark_like_async)

    benchmark_like_stream = await _run_probe(
        "benchmark_like_stream_wait",
        BENCHMARK_PROMPT,
        _build_benchmark_like_stream_options(),
    )
    probes.append(benchmark_like_stream)

    first_failure = next((probe for probe in probes if probe["classification"] != "completed"), None)
    failing_reference = benchmark_like_stream
    diff = _factor_diff(working_minimal_async, failing_reference)

    factor_summary: list[dict[str, Any]] = []
    for probe in probes:
        diagnostic = probe.get("diagnostic") or {}
        factor_summary.append({
            "probe_id": probe["probe_id"],
            "dispatch_style": probe["request_shape"].get("dispatch_style"),
            "agent": probe["request_shape"].get("agent"),
            "mode": probe["request_shape"].get("mode"),
            "format_present": probe["request_shape"].get("format_present"),
            "tool_surface_size": probe["request_shape"].get("tool_surface_size"),
            "user_prompt_len": probe["request_shape"].get("user_prompt_len"),
            "payload_size_bytes": probe["request_shape"].get("payload_size_bytes"),
            "classification": probe.get("classification"),
            "provider_request_started": diagnostic.get("provider_request_started"),
            "provider_response_started": diagnostic.get("provider_response_started"),
            "provider_response_completed": diagnostic.get("provider_response_completed"),
            "assistant_text_present": probe.get("assistant", {}).get("assistant_text_present"),
            "suspected_stall_stage": diagnostic.get("suspected_stall_stage"),
            "exception_type": probe.get("exception_type") or diagnostic.get("provider_exception_type"),
        })

    most_likely_cause = (
        "dispatch_style_stream_wait"
        if working_minimal_async["classification"] == "completed"
        and working_minimal_stream["classification"] != "completed"
        else (
            "agent_inline_config"
            if minimal_no_agent_async["classification"] != "completed"
            else "combined_benchmark_shape"
        )
    )

    factor_payload = {
        "generated_at_utc": _utc_now(),
        "model": MODEL,
        "timeout_sec": TIMEOUT_SEC,
        "probes": probes,
        "factor_summary": factor_summary,
        "first_non_completed_probe": first_failure["probe_id"] if first_failure else None,
        "most_likely_cause": most_likely_cause,
    }
    FACTOR_PROBES_PATH.write_text(json.dumps(factor_payload, indent=2) + "\n", encoding="utf-8")

    diff_payload = {
        "generated_at_utc": _utc_now(),
        "model": MODEL,
        "working_reference_probe": working_minimal_async["probe_id"],
        "failing_reference_probe": failing_reference["probe_id"],
        "working_reference": {
            "request_shape": working_minimal_async["request_shape"],
            "classification": working_minimal_async["classification"],
            "diagnostic": {
                "provider_request_started": working_minimal_async["diagnostic"].get("provider_request_started"),
                "provider_response_started": working_minimal_async["diagnostic"].get("provider_response_started"),
                "provider_response_completed": working_minimal_async["diagnostic"].get("provider_response_completed"),
                "provider_response_latency_sec": working_minimal_async["diagnostic"].get("provider_response_latency_sec"),
                "message_poll_iterations": working_minimal_async["diagnostic"].get("message_poll_iterations"),
            },
        },
        "failing_reference": {
            "request_shape": failing_reference["request_shape"],
            "classification": failing_reference["classification"],
            "diagnostic": {
                "provider_request_started": failing_reference["diagnostic"].get("provider_request_started"),
                "provider_response_started": failing_reference["diagnostic"].get("provider_response_started"),
                "provider_response_completed": failing_reference["diagnostic"].get("provider_response_completed"),
                "provider_response_latency_sec": failing_reference["diagnostic"].get("provider_response_latency_sec"),
                "message_poll_iterations": failing_reference["diagnostic"].get("message_poll_iterations"),
            },
        },
        "key_differences": diff,
        "most_likely_cause": most_likely_cause,
    }
    REQUEST_DIFF_PATH.write_text(json.dumps(diff_payload, indent=2) + "\n", encoding="utf-8")

    summary_lines = [
        "# EvoSkill OpenCode Request Diff Summary",
        "",
        f"- Model tested: `{MODEL}`",
        f"- Timeout per probe: `{TIMEOUT_SEC}s`",
        "",
        "## Working minimal path",
        "",
        f"- Probe: `{working_minimal_async['probe_id']}`",
        f"- Dispatch style: `{working_minimal_async['request_shape']['dispatch_style']}`",
        f"- Agent: `{working_minimal_async['request_shape']['agent']}`",
        f"- Mode: `{working_minimal_async['request_shape']['mode']}`",
        f"- Format present: `{working_minimal_async['request_shape']['format_present']}`",
        f"- Tool surface size: `{working_minimal_async['request_shape']['tool_surface_size']}`",
        f"- Payload size: `{working_minimal_async['request_shape']['payload_size_bytes']}` bytes",
        f"- Classification: `{working_minimal_async['classification']}`",
        "",
        "## Failing benchmark-facing path",
        "",
        f"- Probe: `{failing_reference['probe_id']}`",
        f"- Dispatch style: `{failing_reference['request_shape']['dispatch_style']}`",
        f"- Agent: `{failing_reference['request_shape']['agent']}`",
        f"- Mode: `{failing_reference['request_shape']['mode']}`",
        f"- Format present: `{failing_reference['request_shape']['format_present']}`",
        f"- Tool surface size: `{failing_reference['request_shape']['tool_surface_size']}`",
        f"- Payload size: `{failing_reference['request_shape']['payload_size_bytes']}` bytes",
        f"- Classification: `{failing_reference['classification']}`",
        "",
        "## Key differences found",
        "",
    ]
    for key, values in diff.items():
        summary_lines.append(
            f"- `{key}`: working=`{values['working']}` failing=`{values['failing']}`"
        )
    summary_lines.extend([
        "",
        "## Factor-probe conclusion",
        "",
        f"- First non-completed probe: `{factor_payload['first_non_completed_probe']}`",
        f"- Most likely cause: `{most_likely_cause}`",
        "",
        "## Current answer",
        "",
        (
            "- The working path succeeds because OpenCode session state is polled while the "
            "message POST is in flight, so assistant content can be recovered even when the "
            "POST response stream itself never yields useful chunks."
        ),
        (
            "- The failing benchmark-facing path adds benchmark shape, but the first decisive "
            "cliff is whether we rely on `stream_wait` instead of `async_poll`."
        ),
    ])
    SUMMARY_PATH.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
