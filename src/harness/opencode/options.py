"""OpenCode SDK option building and permission management.

All OpenCode-specific construction logic lives here:
    - Tool name mapping (Claude PascalCase → OpenCode lowercase)
    - Model string parsing ("anthropic/claude-sonnet-4-6" → provider + model)
    - Permission auto-config (opencode.json)
    - The build_opencode_options() function
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from ..model_aliases import DEFAULT_ANTHROPIC_MODEL, normalize_harness_model
from ..utils import resolve_project_root, resolve_data_dirs


DEFAULT_OPENCODE_MODEL = DEFAULT_ANTHROPIC_MODEL
DEFAULT_LOCAL_OLLAMA_MODEL = "gpt-oss:20b"
SECONDARY_LOCAL_OLLAMA_MODEL = "qwen3-coder:30b"
MINIMAL_REPLY_AGENT_NAME = "evoskill-reply"
INLINE_BENCHMARK_AGENT_NAME = "evoskill-inline-benchmark"

CLAUDE_TO_OPENCODE_TOOL = {
    "Read": "read",
    "Write": "write",
    "Bash": "bash",
    "Glob": "glob",
    "Grep": "grep",
    "Edit": "edit",
    "WebFetch": "webfetch",
    "WebSearch": "websearch",
    "TodoWrite": "todowrite",
    "Skill": "skill",
    # OpenCode does not expose a separate BashOutput tool.
    "BashOutput": None,
}


def split_opencode_model(model: str | None) -> tuple[str, str]:
    """Parse 'provider/model' string into (provider_id, model_id)."""
    normalized_input = str(model or "").strip()
    if normalized_input in {"ollama", "ollama/"}:
        return "ollama", DEFAULT_LOCAL_OLLAMA_MODEL

    full = normalize_harness_model("opencode", model)
    if "/" in full:
        return full.split("/", 1)
    return "anthropic", full


def resolve_local_ollama_model(model: str | None = None) -> str:
    """Return a fully-qualified local Ollama model string for OpenCode probes."""
    normalized = str(model or "").strip()
    if not normalized or normalized in {"ollama", "ollama/"}:
        return f"ollama/{DEFAULT_LOCAL_OLLAMA_MODEL}"
    if normalized.startswith("ollama/"):
        return normalized
    return f"ollama/{normalized}"


def _inline_agent_permission_block(
    opencode_tools: dict[str, bool],
    *,
    allow_tool_permissions: bool,
) -> dict[str, str]:
    permission = {
        "*": "deny",
        "doom_loop": "deny",
        "question": "deny",
        "plan_enter": "deny",
        "plan_exit": "deny",
    }
    if allow_tool_permissions:
        for tool_name in sorted(opencode_tools.keys()):
            permission[tool_name] = "allow"
    return permission


def _inline_agent_config(
    *,
    agent_name: str,
    resolved_model: str,
    prompt: str,
    permission: dict[str, str],
    description: str,
) -> str:
    return json.dumps({
        "agent": {
            agent_name: {
                "mode": "primary",
                "description": description,
                "model": resolved_model,
                "prompt": prompt,
                "permission": permission,
            },
        },
    })


def to_opencode_tools(tools: Iterable[str]) -> dict[str, bool]:
    """Map Claude tool names (PascalCase) to OpenCode names (lowercase)."""
    converted: dict[str, bool] = {}
    for tool in tools:
        normalized = CLAUDE_TO_OPENCODE_TOOL.get(tool, tool.lower())
        if normalized is not None:
            converted[normalized] = True
    return converted


def _normalize_permission_block(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        return {"*": value}
    if isinstance(value, dict):
        return dict(value)
    return {}


def _load_opencode_config(root: Path) -> tuple[Path, dict[str, Any]] | None:
    jsonc_path = root / "opencode.jsonc"
    config_path = root / "opencode.json"
    if jsonc_path.exists() and not config_path.exists():
        return None

    config: dict[str, Any] = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
        except json.JSONDecodeError:
            return None

    config.setdefault("$schema", "https://opencode.ai/config.json")
    return config_path, config


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


def ensure_opencode_provider_config(
    project_root: str | Path | None,
    *,
    provider_id: str,
    model_id: str,
) -> None:
    """Ensure project-local provider config exists for local Ollama runs."""
    if str(provider_id).strip().lower() != "ollama":
        return

    root = resolve_project_root(project_root)
    loaded = _load_opencode_config(root)
    if loaded is None:
        return

    config_path, config = loaded
    providers = config.setdefault("provider", {})
    provider = dict(providers.get("ollama") or {})
    provider.setdefault("name", "Ollama (Local)")
    provider.setdefault("npm", "@ai-sdk/openai-compatible")

    options = dict(provider.get("options") or {})
    options.setdefault("baseURL", _resolve_ollama_base_url())
    options.setdefault("apiKey", os.environ.get("OLLAMA_API_KEY") or "ollama")
    provider["options"] = options

    models = dict(provider.get("models") or {})
    models.setdefault(model_id, {"name": model_id})
    provider["models"] = models

    providers["ollama"] = provider
    config["provider"] = providers
    config_path.write_text(json.dumps(config, indent=2) + "\n")


def ensure_opencode_project_permissions(
    project_root: str | Path | None,
    data_dirs: Iterable[str] | None = None,
) -> None:
    """Auto-create/update opencode.json to grant file access to data directories."""
    root = resolve_project_root(project_root)
    resolved_add_dirs = resolve_data_dirs(root, data_dirs)
    if not resolved_add_dirs:
        return

    loaded = _load_opencode_config(root)
    if loaded is None:
        return
    config_path, config = loaded
    permission = _normalize_permission_block(config.get("permission"))
    external_directory = _normalize_permission_block(
        permission.get("external_directory")
    )

    changed = False
    for raw_path in resolved_add_dirs:
        path = str(Path(raw_path).resolve())
        for pattern in (path, f"{path}/**"):
            if external_directory.get(pattern) != "allow":
                external_directory[pattern] = "allow"
                changed = True

    if not changed and config_path.exists():
        return

    permission["external_directory"] = external_directory
    config["permission"] = permission
    config_path.write_text(json.dumps(config, indent=2) + "\n")


def build_opencode_options(
    *,
    system: str,
    schema: dict[str, Any],
    tools: Iterable[str],
    project_root: str | Path | None = None,
    model: str | None = None,
    mode: str = "build",
    data_dirs: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build an options dict for the OpenCode SDK."""
    root = resolve_project_root(project_root)
    requested_model = str(model).strip() if model is not None else None
    provider_id, model_id = split_opencode_model(model)
    resolved_model = f"{provider_id}/{model_id}"
    resolved_add_dirs = resolve_data_dirs(root, data_dirs)
    ensure_opencode_provider_config(
        root,
        provider_id=provider_id,
        model_id=model_id,
    )
    ensure_opencode_project_permissions(root, resolved_add_dirs)

    system_with_dirs = system
    if resolved_add_dirs:
        dirs_note = "\n".join(f"- {path}" for path in resolved_add_dirs)
        system_with_dirs = (
            f"{system.rstrip()}\n\n"
            "Additional accessible data directories are available outside the project root.\n"
            "Use absolute paths when you need to inspect them:\n"
            f"{dirs_note}"
        )

    return {
        "system": system_with_dirs,
        "format": {
            "type": "json_schema",
            "schema": schema,
        },
        "tools": to_opencode_tools(tools),
        "mode": mode,
        "provider_id": provider_id,
        "model_id": model_id,
        "model": resolved_model,
        "requested_model": requested_model,
        "cwd": str(root),
        "add_dirs": resolved_add_dirs,
    }


def build_opencode_inline_agent_options(
    *,
    agent_prompt: str,
    tools: Iterable[str],
    schema: dict[str, Any] | None = None,
    project_root: str | Path | None = None,
    model: str | None = None,
    mode: str | None = None,
    data_dirs: Iterable[str] | None = None,
    include_body_tools: bool = False,
    include_format: bool = False,
    allow_tool_permissions: bool = False,
    timeout_seconds: float = 45.0,
    agent_name: str = INLINE_BENCHMARK_AGENT_NAME,
    description: str = "EvoSkill inline OpenCode benchmark agent.",
) -> dict[str, Any]:
    """Build inline-agent OpenCode options for local request-shape probes.

    This keeps prompt instructions inside the OpenCode agent config instead of
    the message body and optionally adds body tools/mode/format back one factor
    at a time.
    """
    root = resolve_project_root(project_root)
    requested_model = str(model).strip() if model is not None else None
    provider_id, model_id = split_opencode_model(model)
    resolved_model = f"{provider_id}/{model_id}"
    resolved_add_dirs = resolve_data_dirs(root, data_dirs)
    ensure_opencode_provider_config(
        root,
        provider_id=provider_id,
        model_id=model_id,
    )
    ensure_opencode_project_permissions(root, resolved_add_dirs)

    prompt_with_dirs = agent_prompt
    if resolved_add_dirs:
        dirs_note = "\n".join(f"- {path}" for path in resolved_add_dirs)
        prompt_with_dirs = (
            f"{agent_prompt.rstrip()}\n\n"
            "Additional accessible data directories are available outside the project root.\n"
            "Use absolute paths when you need to inspect them:\n"
            f"{dirs_note}"
        )

    opencode_tools = to_opencode_tools(tools)
    options: dict[str, Any] = {
        "provider_id": provider_id,
        "model_id": model_id,
        "model": resolved_model,
        "requested_model": requested_model,
        "cwd": str(root),
        "add_dirs": resolved_add_dirs,
        "agent": agent_name,
        "tools": opencode_tools if include_body_tools else {},
        "_evoskill_async_message_polling": True,
        "_evoskill_opencode_inline_config": _inline_agent_config(
            agent_name=agent_name,
            resolved_model=resolved_model,
            prompt=prompt_with_dirs,
            permission=_inline_agent_permission_block(
                opencode_tools,
                allow_tool_permissions=allow_tool_permissions,
            ),
            description=description,
        ),
        "_evoskill_request_timeout_sec": float(timeout_seconds),
    }
    if mode:
        options["mode"] = mode
    if include_format and schema is not None:
        options["format"] = {
            "type": "json_schema",
            "schema": schema,
        }
    return options


def build_opencode_minimal_reply_options(
    *,
    project_root: str | Path | None = None,
    model: str | None = None,
    timeout_seconds: float = 45.0,
) -> dict[str, Any]:
    """Build the lightest truthful OpenCode local-reply options.

    This keeps the request on the real OpenCode server/session/message path while
    avoiding structured-output, build-mode, and tool-surface complexity.
    """
    options = build_opencode_inline_agent_options(
        agent_prompt=(
            "You are a concise assistant. Reply directly to the user's message "
            "in one short sentence. Do not use tools."
        ),
        tools=[],
        project_root=project_root,
        model=model,
        timeout_seconds=timeout_seconds,
        agent_name=MINIMAL_REPLY_AGENT_NAME,
        description="Minimal EvoSkill one-shot reply agent.",
    )
    options["_evoskill_probe_type"] = "opencode_minimal_reply"
    return options
