from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

from src.harness.opencode import run_minimal_reply_smoke


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = ROOT / "artifacts" / "evoskill_minimal_smoke"
ARTIFACT_PATH = ROOT / "artifacts" / "evoskill_minimal_smoke.json"
DOC_PATH = ROOT / "docs" / "evoskill_minimal_smoke.md"
PROMPT = "this is a test give me a reply"
MODELS = [
    "ollama/gpt-oss:20b",
    "ollama/qwen3-coder:30b",
]


async def _direct_stream_probe(model_name: str, prompt: str, timeout_seconds: float) -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {
        "model": model_name,
        "timeout_seconds": timeout_seconds,
        "headers_received": False,
        "first_chunk_received": False,
        "completed": False,
        "error": None,
        "reply_preview": None,
    }
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        try:
            async with client.stream(
                "POST",
                "http://127.0.0.1:11434/v1/chat/completions",
                headers={"Authorization": "Bearer ollama"},
                json={
                    "model": model_name.split("/", 1)[1],
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": True,
                    "max_tokens": 64,
                },
            ) as response:
                result["headers_received"] = True
                result["header_latency_sec"] = round(time.monotonic() - started, 3)
                chunks: list[str] = []
                async for chunk in response.aiter_text():
                    if not chunk:
                        continue
                    chunks.append(chunk)
                    if not result["first_chunk_received"]:
                        result["first_chunk_received"] = True
                        result["first_chunk_latency_sec"] = round(time.monotonic() - started, 3)
                result["completed"] = True
                result["completion_latency_sec"] = round(time.monotonic() - started, 3)
                joined = "".join(chunks)
                result["reply_preview"] = joined[:240]
        except Exception as exc:
            result["completion_latency_sec"] = round(time.monotonic() - started, 3)
            result["error"] = f"{type(exc).__name__}: {exc}"
    return result


async def _opencode_minimal_probe(model_name: str, prompt: str, timeout_seconds: float) -> dict[str, Any]:
    server_log_path = ARTIFACT_DIR / f"{model_name.split('/', 1)[1].replace(':', '_')}_server.log"
    started = time.monotonic()
    try:
        result = await run_minimal_reply_smoke(
            project_root=ROOT,
            model=model_name,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            server_log_path=server_log_path,
        )
        diagnostics = result["diagnostics"]
        return {
            "model": model_name,
            "timeout_seconds": timeout_seconds,
            "success": True,
            "reply": result["reply"],
            "latency_sec": round(time.monotonic() - started, 3),
            "diagnostics": diagnostics,
            "server_log_path": str(server_log_path),
        }
    except Exception as exc:
        return {
            "model": model_name,
            "timeout_seconds": timeout_seconds,
            "success": False,
            "latency_sec": round(time.monotonic() - started, 3),
            "error": f"{type(exc).__name__}: {exc}",
            "server_log_path": str(server_log_path),
        }


def _render_doc(payload: dict[str, Any]) -> str:
    lines = [
        "# EvoSkill Minimal OpenCode Smoke",
        "",
        "## Install path used",
        "",
        "- `uv sync`",
        "",
        "## Runtime path used",
        "",
        "- Minimal OpenCode one-shot helper: `src.harness.opencode.run_minimal_reply_smoke(...)`",
        "- This stays on the real EvoSkill -> OpenCode -> `/session` -> `/message` path.",
        "- It removes nonessential structured-output, build-mode, and tool-surface complexity for the smoke.",
        "",
        "## Command run",
        "",
        "- `uv run python scripts/run_evoskill_minimal_smoke.py`",
        "",
        "## Prompt",
        "",
        f"- `{payload['prompt']}`",
        "",
        "## Results",
        "",
    ]
    for result in payload["results"]:
        lines.extend([
            f"### {result['model']}",
            "",
            f"- direct Ollama success: `{result['direct']['completed']}`",
            f"- direct Ollama error: `{result['direct']['error']}`",
            f"- OpenCode minimal success: `{result['opencode']['success']}`",
            f"- OpenCode error: `{result['opencode'].get('error')}`",
            f"- reply: `{result['opencode'].get('reply')}`",
            "",
        ])

    lines.extend([
        "## Conclusion",
        "",
        f"- Successful local EvoSkill/OpenCode one-shot model: `{payload['successful_model']}`",
        f"- Reply returned: `{payload['reply_text']}`",
        f"- Remaining blocker for preferred model: `{payload['preferred_model_blocker']}`",
        "",
    ])
    return "\n".join(lines)


async def main() -> None:
    os.environ.setdefault("OLLAMA_API_BASE", "http://127.0.0.1:11434")
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    for model_name in MODELS:
        direct = await _direct_stream_probe(model_name, PROMPT, 45.0)
        opencode = await _opencode_minimal_probe(model_name, PROMPT, 45.0)
        results.append({
            "model": model_name,
            "direct": direct,
            "opencode": opencode,
        })

    success = next((item for item in results if item["opencode"]["success"]), None)
    payload = {
        "install_path_used": "uv sync",
        "runtime_path_used": "src.harness.opencode.run_minimal_reply_smoke",
        "prompt": PROMPT,
        "results": results,
        "successful_model": success["model"] if success else None,
        "reply_text": success["opencode"].get("reply") if success else None,
        "preferred_model_blocker": next(
            (
                item["opencode"].get("error")
                for item in results
                if item["model"] == "ollama/gpt-oss:20b"
            ),
            None,
        ),
    }

    ARTIFACT_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    DOC_PATH.write_text(_render_doc(payload) + "\n", encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
