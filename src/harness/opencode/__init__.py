"""OpenCode harness — option building, server management, and execution."""

from .options import (
    DEFAULT_LOCAL_OLLAMA_MODEL,
    INLINE_BENCHMARK_AGENT_NAME,
    MINIMAL_REPLY_AGENT_NAME,
    SECONDARY_LOCAL_OLLAMA_MODEL,
    build_opencode_inline_agent_options,
    build_opencode_minimal_reply_options,
    build_opencode_options,
    resolve_local_ollama_model,
    to_opencode_tools,
)
from .executor import (
    execute_query,
    parse_response,
    request_shape_metadata,
    run_diagnostic_probe,
    run_minimal_reply_smoke,
    shutdown_project_server,
    shutdown_all_servers,
)
from .skill_utils import normalize_project_skill_frontmatter, ensure_skill_frontmatter

__all__ = [
    "build_opencode_options",
    "build_opencode_inline_agent_options",
    "build_opencode_minimal_reply_options",
    "DEFAULT_LOCAL_OLLAMA_MODEL",
    "INLINE_BENCHMARK_AGENT_NAME",
    "MINIMAL_REPLY_AGENT_NAME",
    "SECONDARY_LOCAL_OLLAMA_MODEL",
    "resolve_local_ollama_model",
    "to_opencode_tools",
    "execute_query",
    "parse_response",
    "request_shape_metadata",
    "run_diagnostic_probe",
    "run_minimal_reply_smoke",
    "shutdown_project_server",
    "shutdown_all_servers",
    "normalize_project_skill_frontmatter",
    "ensure_skill_frontmatter",
]
