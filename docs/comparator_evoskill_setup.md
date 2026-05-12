# Comparator EvoSkill Setup

## Local OpenCode/Ollama models

- Default local benchmark model: `ollama/gpt-oss:20b`
- Secondary comparison model: `ollama/qwen3-coder:30b`

`qwen2.5-coder:14b` was not the intended comparator default. It only appeared in the recent local-Ollama support patch and associated tests, so the OpenCode local path is now explicit about the intended models.

## Local provider behavior

- EvoSkill now resolves `ollama` to `ollama/gpt-oss:20b` when the caller explicitly selects the local Ollama provider without a model suffix.
- EvoSkill auto-registers a project-local OpenCode `provider.ollama` entry with the exact requested model.
- Local Ollama auth now accepts the normal placeholder token `ollama`, so localhost runs do not require exporting a fake secret just to pass harness auth preflight.

## Probe command

```powershell
python scripts/run_opencode_ollama_probes.py --timeout-sec 45
python scripts/run_evoskill_inline_agent_probe.py
python scripts/run_evoskill_factor_isolation.py
```

Artifacts written by that probe:

- `artifacts/ollama_direct_latency_probe.json`
- `artifacts/opencode_message_latency_probe.json`
- `artifacts/evoskill_provider_path_comparison.json`
- `artifacts/evoskill_inline_agent_probe.json`
- `artifacts/EVOSKILL_INLINE_AGENT_SUMMARY.md`
- `artifacts/evoskill_factor_isolation.json`
- `artifacts/EVOSKILL_FACTOR_ISOLATION_SUMMARY.md`

## OpenCode request-path diff

The narrowest current diagnosis comes from the bounded request-shape probe ladder in:

- `artifacts/evoskill_request_factor_probes.json`
- `artifacts/evoskill_request_path_diff.json`
- `artifacts/EVOSKILL_REQUEST_DIFF_SUMMARY.md`

Working minimal OpenCode request shape on this machine:

- model: `ollama/qwen3-coder:30b`
- inline agent: `evoskill-reply`
- no body-level `system`
- no `mode`
- no JSON-schema `format`
- no tools
- very small payload (`163` bytes)

First failing shift from that working path:

- remove the inline agent
- move instructions into a body-level `system` prompt

That first no-agent/body-system probe is the earliest non-completed step in the scripted ladder:

- `working_minimal_async_agent` -> completed
- `minimal_async_no_agent_body_system` -> `provider_wait`

After that shift, the larger benchmark-facing shape also fails in the same stage:

- `mode=build`
- JSON-schema `format`
- 6-tool surface
- larger prompt and payload

Important nuance:

- dispatch style by itself is not the root cause, because the tiny inline-agent path completed in both `async_poll` and `stream_wait`
- the current blocker is request-shape compatibility inside the OpenCode local Ollama `/message` path after leaving the tiny inline-agent form

## Inline-agent request-shape boundary

The newer inline-agent probe narrows the local boundary further on `ollama/qwen3-coder:30b`:

- `agent + model + parts` with an inline benchmark prompt still gets provider response start.
- The inline-agent prompt can carry benchmark-facing instructions without using body-level `system`.
- The next cliff is the first real tool-capable or body-augmented benchmark shape:
  - tool permissions enabled in the inline agent
  - body-level `tools`
  - body-level `mode=build`
  - body-level JSON-schema `format`

Those variants all regress to:

- request dispatch starts
- no response headers arrive
- assistant placeholder persists
- final classification: `provider_wait`

## Latest factor-isolation note

The ultra-narrow factor-isolation rerun did not hold the old control steady:

- the known-good minimal inline-agent control timed out before response headers
- the matching direct Ollama tiny-prompt streamed check also timed out in the same run window

So the latest pass does not support blaming a later request field with high confidence. The current narrowest blocker is:

- minimal OpenCode inline-agent control: `ReadTimeout`
- direct Ollama tiny-prompt control: `ReadTimeout`
