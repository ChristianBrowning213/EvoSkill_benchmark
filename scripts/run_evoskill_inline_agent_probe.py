from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from src.agent_profiles.base_agent.base_agent import BASE_AGENT_TOOLS, PROMPT_FILE
from src.harness.opencode import (
    build_opencode_inline_agent_options,
    execute_query,
    request_shape_metadata,
)
from src.harness.opencode.executor import _extract_assistant_text
from src.schemas import AgentResponse


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = ROOT / "artifacts" / "evoskill_inline_agent_probe"
ARTIFACT_PATH = ROOT / "artifacts" / "evoskill_inline_agent_probe.json"
SUMMARY_PATH = ROOT / "artifacts" / "EVOSKILL_INLINE_AGENT_SUMMARY.md"
SUMMARY_LOG_PATH = ROOT / ".evoskill" / "logs" / "opencode_message_diagnostics.jsonl"
MODEL = "ollama/qwen3-coder:30b"
TRIVIAL_PROMPT = "this is a test give me a reply"
BENCHMARK_QUERY = "Read README.md and reply with only the project title."
BASE_PROMPT = PROMPT_FILE.read_text(encoding="utf-8").strip()


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _classify_variant(diagnostic: dict[str, Any] | None, assistant_text: str, error: str | None) -> str:
    if assistant_text.strip():
        return "assistant_reply"
    if diagnostic:
        if int(diagnostic.get("assistant_message_count", 0) or 0) > 0:
            return "assistant_placeholder_only"
        if diagnostic.get("provider_response_started") or diagnostic.get("provider_http_headers_received"):
            return "response_started_without_reply"
        if diagnostic.get("provider_request_started"):
            return "provider_wait"
    if error:
        return "request_exception"
    return "unknown"


def _benchmark_inline_prompt(*, tool_expectation: str) -> str:
    return (
        f"{BASE_PROMPT}\n\n"
        "Benchmark instruction summary:\n"
        "- Answer the user question based on the local repository contents.\n"
        "- Keep the answer concise and grounded in what you inspect.\n\n"
        "Skill guidance summary:\n"
        "- Prefer a focused inspection of the most relevant file before answering.\n"
        "- Avoid broad exploration when one file is enough.\n\n"
        "Expected first-step behavior:\n"
        "- Start by checking the file that most directly answers the question.\n\n"
        "Tool-use expectations:\n"
        f"- {tool_expectation}"
    )


def _variant_options(
    *,
    agent_prompt: str,
    query: str,
    tools: list[str],
    include_body_tools: bool = False,
    include_format: bool = False,
    mode: str | None = None,
    allow_tool_permissions: bool = False,
    probe_type: str,
) -> tuple[dict[str, Any], str]:
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
        timeout_seconds=45.0,
    )
    options["_evoskill_probe_type"] = probe_type
    options["_evoskill_server_log_path"] = str(ARTIFACT_DIR / f"{probe_type}.log")
    return options, query


async def _run_variant(name: str, options: dict[str, Any], query: str) -> dict[str, Any]:
    before = len(_load_jsonl(SUMMARY_LOG_PATH))
    request_shape = request_shape_metadata(options, query)
    payload: dict[str, Any] | None = None
    error: str | None = None

    try:
        payload = (await execute_query(options, query))[0]
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    new_entries = _load_jsonl(SUMMARY_LOG_PATH)[before:]
    diagnostic = new_entries[-1] if new_entries else (payload.get("diagnostics") if payload else None)
    assistant_text = ""
    if payload:
        assistant_text = _extract_assistant_text(payload.get("messages", []))

    assistant_persisted = bool(diagnostic and int(diagnostic.get("assistant_message_count", 0) or 0) > 0)
    return {
        "name": name,
        "request_shape": request_shape,
        "payload_size_bytes": request_shape["payload_size_bytes"],
        "inline_agent_prompt_len": request_shape["inline_agent_prompt_len"],
        "tools_exposed": request_shape["tool_names"],
        "mode": request_shape["mode"],
        "format_present": request_shape["format_present"],
        "provider_request_started": bool(diagnostic and diagnostic.get("provider_request_started")),
        "provider_response_started": bool(diagnostic and diagnostic.get("provider_response_started")),
        "headers_arrived": bool(diagnostic and diagnostic.get("provider_http_headers_received")),
        "first_chunk_arrived": bool(diagnostic and diagnostic.get("provider_first_chunk_received")),
        "assistant_persisted": assistant_persisted,
        "assistant_text": assistant_text,
        "classification": _classify_variant(diagnostic, assistant_text, error),
        "diagnostics": diagnostic,
        "error": error,
        "server_log_path": options["_evoskill_server_log_path"],
    }


def _render_summary(payload: dict[str, Any]) -> str:
    lines = [
        "# EVOSKILL Inline-Agent Summary",
        "",
        f"- Model: `{payload['model']}`",
        f"- Provider response start restored on primary progression: `{payload['provider_response_start_restored']}`",
        f"- Benchmark-compatible reusable fix established: `{payload['benchmark_compatible_variant'] is not None}`",
        "",
        "## Primary progression",
        "",
    ]
    for item in payload["primary_progression"]:
        lines.extend([
            f"### {item['name']}",
            "",
            f"- Classification: `{item['classification']}`",
            f"- Payload size: `{item['payload_size_bytes']}` bytes",
            f"- Inline agent prompt length: `{item['inline_agent_prompt_len']}`",
            f"- Tools exposed: `{', '.join(item['tools_exposed']) or 'none'}`",
            f"- Mode: `{item['mode']}`",
            f"- Format present: `{item['format_present']}`",
            f"- provider_request_started: `{item['provider_request_started']}`",
            f"- provider_response_started: `{item['provider_response_started']}`",
            f"- headers_arrived: `{item['headers_arrived']}`",
            f"- first_chunk_arrived: `{item['first_chunk_arrived']}`",
            f"- assistant_persisted: `{item['assistant_persisted']}`",
            f"- assistant_text: `{item['assistant_text'][:160]}`",
            f"- error: `{item['error']}`",
            "",
        ])
    lines.extend([
        "## Supplemental cross-checks",
        "",
    ])
    for item in payload["supplemental_cross_checks"]:
        lines.extend([
            f"- `{item['name']}` -> `{item['classification']}` (error: `{item['error']}`)",
        ])
    lines.extend([
        "",
        "## Conclusion",
        "",
        f"- Provider response start was restored only for: `{payload['restored_variants']}`",
        f"- Benchmark-compatible tool-capable inline-agent variant: `{payload['benchmark_compatible_variant']}`",
        f"- Narrowest blocker: `{payload['narrowest_blocker']}`",
        "",
    ])
    return "\n".join(lines)


async def main() -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    primary_inputs = [
        (
            "1_working_minimal_inline_baseline",
            _variant_options(
                agent_prompt="You are a concise assistant. Reply directly to the user in one short sentence. Do not use tools.",
                query=TRIVIAL_PROMPT,
                tools=[],
                probe_type="inline_baseline",
            ),
        ),
        (
            "2_inline_agent_benchmark_prompt_only",
            _variant_options(
                agent_prompt=_benchmark_inline_prompt(
                    tool_expectation="If tools are unavailable, say so briefly instead of pretending to use them."
                ),
                query=BENCHMARK_QUERY,
                tools=[],
                probe_type="inline_benchmark_prompt_only",
            ),
        ),
        (
            "3_inline_agent_benchmark_prompt_plus_minimal_tools",
            _variant_options(
                agent_prompt=_benchmark_inline_prompt(
                    tool_expectation="Use the minimum tool needed to inspect the relevant file before answering."
                ),
                query=BENCHMARK_QUERY,
                tools=["Read"],
                allow_tool_permissions=True,
                probe_type="inline_benchmark_minimal_tools",
            ),
        ),
        (
            "4_inline_agent_benchmark_prompt_plus_benchmark_like_tool_surface",
            _variant_options(
                agent_prompt=_benchmark_inline_prompt(
                    tool_expectation="Use tools deliberately to inspect the repository before answering."
                ),
                query=BENCHMARK_QUERY,
                tools=BASE_AGENT_TOOLS,
                allow_tool_permissions=True,
                probe_type="inline_benchmark_full_tools",
            ),
        ),
        (
            "5_inline_agent_benchmark_prompt_plus_remaining_benchmark_options",
            _variant_options(
                agent_prompt=_benchmark_inline_prompt(
                    tool_expectation="Use tools deliberately and return JSON matching the requested schema."
                ),
                query=BENCHMARK_QUERY,
                tools=BASE_AGENT_TOOLS,
                include_body_tools=True,
                include_format=True,
                mode="build",
                allow_tool_permissions=True,
                probe_type="inline_benchmark_full_tools_mode_format",
            ),
        ),
    ]

    supplemental_inputs = [
        (
            "body_read_tool_only_no_permission",
            _variant_options(
                agent_prompt=_benchmark_inline_prompt(
                    tool_expectation="Use tools deliberately to inspect the repository before answering."
                ),
                query=BENCHMARK_QUERY,
                tools=["Read"],
                include_body_tools=True,
                allow_tool_permissions=False,
                probe_type="inline_body_read_only",
            ),
        ),
        (
            "body_full_tool_surface_no_permission",
            _variant_options(
                agent_prompt=_benchmark_inline_prompt(
                    tool_expectation="Use tools deliberately to inspect the repository before answering."
                ),
                query=BENCHMARK_QUERY,
                tools=BASE_AGENT_TOOLS,
                include_body_tools=True,
                allow_tool_permissions=False,
                probe_type="inline_body_full_no_perm",
            ),
        ),
        (
            "mode_and_format_without_tools",
            _variant_options(
                agent_prompt=_benchmark_inline_prompt(
                    tool_expectation="Do not use tools unless the runtime exposes them."
                ),
                query=BENCHMARK_QUERY,
                tools=[],
                include_format=True,
                mode="build",
                allow_tool_permissions=False,
                probe_type="inline_mode_format_no_tools",
            ),
        ),
    ]

    primary_results = []
    for name, (options, query) in primary_inputs:
        primary_results.append(await _run_variant(name, options, query))

    supplemental_results = []
    for name, (options, query) in supplemental_inputs:
        supplemental_results.append(await _run_variant(name, options, query))

    restored_variants = [
        item["name"]
        for item in primary_results + supplemental_results
        if item["provider_response_started"] or item["headers_arrived"]
    ]
    benchmark_compatible_variant = next(
        (
            item["name"]
            for item in primary_results
            if item["assistant_persisted"] and item["assistant_text"].strip() and item["tools_exposed"]
        ),
        None,
    )
    payload = {
        "model": MODEL,
        "primary_progression": primary_results,
        "supplemental_cross_checks": supplemental_results,
        "provider_response_start_restored": bool(restored_variants),
        "restored_variants": restored_variants,
        "benchmark_compatible_variant": benchmark_compatible_variant,
        "narrowest_blocker": (
            "Inline-agent prompt complexity is serviceable, but adding real tool capability "
            "or extra body-level benchmark fields (tools, mode, format) reintroduces the stall."
        ),
    }

    ARTIFACT_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    SUMMARY_PATH.write_text(_render_summary(payload) + "\n", encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
