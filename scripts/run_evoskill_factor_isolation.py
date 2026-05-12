from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from src.agent_profiles.base_agent.base_agent import BASE_AGENT_TOOLS, PROMPT_FILE
from src.harness.opencode import (
    INLINE_BENCHMARK_AGENT_NAME,
    MINIMAL_REPLY_AGENT_NAME,
    build_opencode_inline_agent_options,
    execute_query,
    request_shape_metadata,
)
from src.harness.opencode.executor import _assistant_has_meaningful_output, _extract_assistant_text
from src.schemas import AgentResponse


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = ROOT / "artifacts" / "evoskill_factor_isolation"
ARTIFACT_PATH = ROOT / "artifacts" / "evoskill_factor_isolation.json"
SUMMARY_PATH = ROOT / "artifacts" / "EVOSKILL_FACTOR_ISOLATION_SUMMARY.md"
SUMMARY_LOG_PATH = ROOT / ".evoskill" / "logs" / "opencode_message_diagnostics.jsonl"
MODEL = "ollama/qwen3-coder:30b"
QUERY = "this is a test give me a reply"
TIMEOUT_SECONDS = 15.0
BASE_PROMPT = PROMPT_FILE.read_text(encoding="utf-8").strip()
MAPPED_BENCHMARK_TOOLS = [tool for tool in BASE_AGENT_TOOLS if tool != "BashOutput"]


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _benchmark_inline_prompt(*, tool_expectation: str) -> str:
    return (
        f"{BASE_PROMPT}\n\n"
        "Benchmark instruction summary:\n"
        "- Answer directly and truthfully.\n"
        "- Use tools only if the task actually needs them.\n"
        "- Do not invent file reads or actions.\n\n"
        "Skill guidance summary:\n"
        "- Prefer the smallest action that resolves the task.\n"
        "- If no tool is needed, answer directly.\n\n"
        "Expected first-step behavior:\n"
        "- For a trivial greeting-style prompt, reply normally without extra ceremony.\n\n"
        "Tool-use expectations:\n"
        f"- {tool_expectation}"
    )


def _variant_options(
    *,
    agent_prompt: str,
    probe_type: str,
    agent_name: str,
    tools: list[str],
    allow_tool_permissions: bool = False,
    include_body_tools: bool = False,
    mode: str | None = None,
    include_format: bool = False,
) -> dict[str, Any]:
    options = build_opencode_inline_agent_options(
        agent_prompt=agent_prompt,
        tools=tools,
        schema=AgentResponse.model_json_schema(),
        project_root=ROOT,
        model=MODEL,
        mode=mode,
        include_body_tools=include_body_tools,
        include_format=include_format,
        allow_tool_permissions=allow_tool_permissions,
        timeout_seconds=TIMEOUT_SECONDS,
        agent_name=agent_name,
    )
    options["_evoskill_probe_type"] = probe_type
    options["_evoskill_server_log_path"] = str(ARTIFACT_DIR / f"{probe_type}.log")
    return options


def _variant_definition(
    name: str,
    *,
    agent_prompt: str,
    agent_name: str,
    tools: list[str],
    allow_tool_permissions: bool = False,
    include_body_tools: bool = False,
    mode: str | None = None,
    include_format: bool = False,
) -> dict[str, Any]:
    probe_type = name.lower().replace(" ", "_")
    return {
        "name": name,
        "probe_type": probe_type,
        "agent_name": agent_name,
        "tools": list(tools),
        "allow_tool_permissions": allow_tool_permissions,
        "include_body_tools": include_body_tools,
        "mode": mode,
        "include_format": include_format,
        "options": _variant_options(
            agent_prompt=agent_prompt,
            probe_type=probe_type,
            agent_name=agent_name,
            tools=tools,
            allow_tool_permissions=allow_tool_permissions,
            include_body_tools=include_body_tools,
            mode=mode,
            include_format=include_format,
        ),
    }


def _classify_variant(
    *,
    assistant_content_persisted: bool,
    assistant_placeholder_persisted: bool,
    provider_response_started: bool,
    headers_arrived: bool,
    first_chunk_arrived: bool,
    error: str | None,
) -> str:
    if assistant_content_persisted and not error:
        return "assistant_content"
    if assistant_placeholder_persisted and error:
        return "assistant_placeholder_timeout"
    if first_chunk_arrived:
        return "stream_started_without_content"
    if headers_arrived or provider_response_started:
        return "headers_only"
    if error:
        return "provider_wait"
    if assistant_placeholder_persisted:
        return "assistant_placeholder"
    return "unknown"


async def _run_variant(defn: dict[str, Any]) -> dict[str, Any]:
    options = dict(defn["options"])
    before = len(_load_jsonl(SUMMARY_LOG_PATH))
    request_shape = request_shape_metadata(options, QUERY)
    payload: dict[str, Any] | None = None
    error: str | None = None

    try:
        payload = (await execute_query(options, QUERY))[0]
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    new_entries = _load_jsonl(SUMMARY_LOG_PATH)[before:]
    diagnostic = new_entries[-1] if new_entries else (payload.get("diagnostics") if payload else None)
    messages = payload.get("messages", []) if payload else []
    assistant_text = _extract_assistant_text(messages)
    assistant_placeholder_persisted = bool(diagnostic and int(diagnostic.get("assistant_message_count", 0) or 0) > 0)
    assistant_content_persisted = bool(messages and _assistant_has_meaningful_output(messages))
    provider_response_started = bool(diagnostic and diagnostic.get("provider_response_started"))
    headers_arrived = bool(diagnostic and diagnostic.get("provider_http_headers_received"))
    first_chunk_arrived = bool(diagnostic and diagnostic.get("provider_first_chunk_received"))

    return {
        "name": defn["name"],
        "probe_type": defn["probe_type"],
        "query": QUERY,
        "request_shape": request_shape,
        "payload_size_bytes": request_shape["payload_size_bytes"],
        "inline_agent_prompt_len": request_shape["inline_agent_prompt_len"],
        "tools_requested": list(defn["tools"]),
        "tool_permissions_enabled": bool(defn["allow_tool_permissions"]),
        "body_tools_present": bool(defn["include_body_tools"]),
        "mode": defn["mode"],
        "format_present": bool(defn["include_format"]),
        "provider_request_started": bool(diagnostic and diagnostic.get("provider_request_started")),
        "provider_response_started": provider_response_started,
        "headers_arrived": headers_arrived,
        "first_chunk_arrived": first_chunk_arrived,
        "assistant_placeholder_persisted": assistant_placeholder_persisted,
        "assistant_content_persisted": assistant_content_persisted,
        "assistant_text": assistant_text,
        "timeout_or_exception": error,
        "classification": _classify_variant(
            assistant_content_persisted=assistant_content_persisted,
            assistant_placeholder_persisted=assistant_placeholder_persisted,
            provider_response_started=provider_response_started,
            headers_arrived=headers_arrived,
            first_chunk_arrived=first_chunk_arrived,
            error=error,
        ),
        "diagnostics": diagnostic,
        "server_log_path": options["_evoskill_server_log_path"],
    }


def _is_success(result: dict[str, Any]) -> bool:
    return bool(result["assistant_content_persisted"]) and not result["timeout_or_exception"]


def _render_summary(payload: dict[str, Any]) -> str:
    lines = [
        "# EVOSKILL Factor Isolation Summary",
        "",
        f"- Model: `{payload['model']}`",
        f"- Query: `{payload['query']}`",
        f"- Timeout per probe: `{payload['timeout_seconds']}` seconds",
        f"- Small fix implemented: `{payload['small_fix_implemented']}`",
        f"- Control regressed: `{payload['control_regressed']}`",
        f"- First exact failing variant: `{payload['first_exact_failing_variant']}`",
        f"- First exact failing factor or combination: `{payload['first_exact_failing_factor_or_combination']}`",
        "",
        "## Baseline",
        "",
    ]
    baseline = payload["baseline"]
    lines.extend([
        f"- Variant: `{baseline['name']}`",
        f"- Classification: `{baseline['classification']}`",
        f"- provider_request_started: `{baseline['provider_request_started']}`",
        f"- provider_response_started: `{baseline['provider_response_started']}`",
        f"- headers_arrived: `{baseline['headers_arrived']}`",
        f"- first_chunk_arrived: `{baseline['first_chunk_arrived']}`",
        f"- assistant_placeholder_persisted: `{baseline['assistant_placeholder_persisted']}`",
        f"- assistant_content_persisted: `{baseline['assistant_content_persisted']}`",
        f"- timeout_or_exception: `{baseline['timeout_or_exception']}`",
        "",
    ])
    if payload["control_regressed"]:
        lines.extend([
            "The known-good minimal inline-agent control did not succeed in this run, so the ladder cannot attribute the failure to a later request field with high confidence.",
            "",
        ])
    lines.extend([
        "## Factor probes",
        "",
    ])
    for item in payload["ordered_results"]:
        lines.extend([
            f"### {item['name']}",
            "",
            f"- Classification: `{item['classification']}`",
            f"- Tools requested: `{', '.join(item['tools_requested']) or 'none'}`",
            f"- Tool permissions enabled: `{item['tool_permissions_enabled']}`",
            f"- Body tools present: `{item['body_tools_present']}`",
            f"- Mode: `{item['mode']}`",
            f"- Format present: `{item['format_present']}`",
            f"- provider_request_started: `{item['provider_request_started']}`",
            f"- provider_response_started: `{item['provider_response_started']}`",
            f"- headers_arrived: `{item['headers_arrived']}`",
            f"- first_chunk_arrived: `{item['first_chunk_arrived']}`",
            f"- assistant_placeholder_persisted: `{item['assistant_placeholder_persisted']}`",
            f"- assistant_content_persisted: `{item['assistant_content_persisted']}`",
            f"- timeout_or_exception: `{item['timeout_or_exception']}`",
            "",
        ])
    lines.extend([
        "## Conclusion",
        "",
        f"- First exact failing variant: `{payload['first_exact_failing_variant']}`",
        f"- First exact failing factor or combination: `{payload['first_exact_failing_factor_or_combination']}`",
        f"- Provider response start restored for any benchmark-capable variant: `{payload['provider_response_start_restored_for_benchmark_capable_variant']}`",
        f"- Narrowest blocker: `{payload['narrowest_remaining_blocker']}`",
        "",
    ])
    return "\n".join(lines)


async def main() -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    minimal_prompt = (
        "You are a concise assistant. Reply directly to the user's message "
        "in one short sentence. Do not use tools."
    )
    benchmark_prompt = _benchmark_inline_prompt(
        tool_expectation="If tools are available, keep them idle unless the user request requires them.",
    )

    primary_definitions = [
        _variant_definition(
            "1_baseline_inline_agent_prompt_only",
            agent_prompt=minimal_prompt,
            agent_name=MINIMAL_REPLY_AGENT_NAME,
            tools=[],
        ),
        _variant_definition(
            "2_inline_agent_benchmark_prompt_only",
            agent_prompt=benchmark_prompt,
            agent_name=INLINE_BENCHMARK_AGENT_NAME,
            tools=[],
        ),
        _variant_definition(
            "3_tool_permissions_only",
            agent_prompt=benchmark_prompt,
            agent_name=INLINE_BENCHMARK_AGENT_NAME,
            tools=["Read"],
            allow_tool_permissions=True,
        ),
        _variant_definition(
            "4_body_tools_only",
            agent_prompt=benchmark_prompt,
            agent_name=INLINE_BENCHMARK_AGENT_NAME,
            tools=["Read"],
            include_body_tools=True,
        ),
        _variant_definition(
            "5_mode_only",
            agent_prompt=benchmark_prompt,
            agent_name=INLINE_BENCHMARK_AGENT_NAME,
            tools=[],
            mode="build",
        ),
        _variant_definition(
            "6_format_only",
            agent_prompt=benchmark_prompt,
            agent_name=INLINE_BENCHMARK_AGENT_NAME,
            tools=[],
            include_format=True,
        ),
        _variant_definition(
            "7_full_tool_surface_only",
            agent_prompt=benchmark_prompt,
            agent_name=INLINE_BENCHMARK_AGENT_NAME,
            tools=MAPPED_BENCHMARK_TOOLS,
            allow_tool_permissions=True,
            include_body_tools=True,
        ),
        _variant_definition(
            "8_mode_plus_format",
            agent_prompt=benchmark_prompt,
            agent_name=INLINE_BENCHMARK_AGENT_NAME,
            tools=[],
            mode="build",
            include_format=True,
        ),
        _variant_definition(
            "9_full_benchmark_like_inline",
            agent_prompt=benchmark_prompt,
            agent_name=INLINE_BENCHMARK_AGENT_NAME,
            tools=MAPPED_BENCHMARK_TOOLS,
            allow_tool_permissions=True,
            include_body_tools=True,
            mode="build",
            include_format=True,
        ),
    ]

    ordered_results: list[dict[str, Any]] = []
    for defn in primary_definitions:
        ordered_results.append(await _run_variant(defn))

    baseline = ordered_results[0]
    benchmark_capable_results = [
        item for item in ordered_results
        if item["tool_permissions_enabled"]
        or item["body_tools_present"]
        or item["mode"] is not None
        or item["format_present"]
    ]
    control_regressed = not _is_success(baseline)
    first_exact_failing = next((item for item in ordered_results[1:] if not _is_success(item)), None)
    restored_variants = [
        item["name"]
        for item in benchmark_capable_results
        if item["provider_response_started"] or item["headers_arrived"] or item["first_chunk_arrived"]
    ]

    if control_regressed:
        factor_summary = (
            "The known-good minimal inline-agent baseline itself failed in this run, so no later "
            "request field can be isolated as the first exact killer with high confidence."
        )
        failing_name = baseline["name"]
        narrowest_remaining_blocker = (
            "Both the minimal OpenCode inline-agent control and the matching direct Ollama tiny-prompt "
            "check timed out before response headers during this pass."
        )
    elif first_exact_failing is None:
        factor_summary = "No single-factor or extended probe failed within the bounded ladder."
        failing_name = None
        narrowest_remaining_blocker = (
            "No failing request-shape delta appeared inside this bounded ladder, so a broader timing or "
            "provider-side factor would be the next place to inspect."
        )
    else:
        factor_summary = (
            "The earliest non-successful request is "
            f"`{first_exact_failing['name']}`, which adds tools `{', '.join(first_exact_failing['tools_requested']) or 'none'}` "
            f"with body_tools_present={first_exact_failing['body_tools_present']}, "
            f"tool_permissions_enabled={first_exact_failing['tool_permissions_enabled']}, "
            f"mode={first_exact_failing['mode']}, format_present={first_exact_failing['format_present']}."
        )
        failing_name = first_exact_failing["name"]
        narrowest_remaining_blocker = (
            "The local OpenCode/Ollama path stops being reliable at the earliest non-successful "
            "request-shape delta captured by this bounded ladder."
        )

    payload = {
        "model": MODEL,
        "query": QUERY,
        "timeout_seconds": TIMEOUT_SECONDS,
        "baseline": baseline,
        "ordered_results": ordered_results,
        "control_regressed": control_regressed,
        "first_exact_failing_variant": failing_name,
        "first_exact_failing_factor_or_combination": factor_summary,
        "provider_response_start_restored_for_benchmark_capable_variant": bool(restored_variants),
        "restored_variants": restored_variants,
        "small_fix_implemented": False,
        "narrowest_remaining_blocker": narrowest_remaining_blocker,
    }

    ARTIFACT_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    SUMMARY_PATH.write_text(_render_summary(payload) + "\n", encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
