# Comparator Adapter Notes

## Why the wrong model was being used

`qwen2.5-coder:14b` was not coming from an older hidden benchmark default. It came from the recent local-Ollama OpenCode support work:

- a local-Ollama registration test in `tests/test_harness.py`
- a missing-provider-model runtime test in `tests/test_opencode_runtime.py`

The local comparator path now makes the intended choices explicit:

- preferred local default: `ollama/gpt-oss:20b`
- secondary comparison model: `ollama/qwen3-coder:30b`

## What changed

- Local OpenCode model resolution now treats explicit `ollama` selection as `ollama/gpt-oss:20b`.
- Project-local OpenCode provider registration keeps the exact requested Ollama model observable.
- OpenCode diagnostics now record requested model, resolved model, probe type, timeout, and completion state.
- Local Ollama auth preflight no longer blocks on a missing fake API key.
- OpenCode request-shape probes now record:
  - inline-agent presence
  - body-level `system`
  - `mode`
  - `format`
  - tool surface
  - payload size
  - dispatch style

## Remaining blocker

Direct Ollama is healthy, and the tiny inline-agent OpenCode request also works on `ollama/qwen3-coder:30b`.

The narrowest request-shape blocker is now:

- working path: tiny inline-agent request (`agent=evoskill-reply`)
- restored path: inline benchmark prompt with no tools and no extra body fields
- first failing inline path: add real tool capability or extra body-level benchmark fields

Important nuance:

- `mode=build` is still present on the benchmark-facing failing path, but it is not the first observed cliff in the bounded factor ladder
- dispatch style is also not the first cliff, because the tiny inline-agent request completed in both `async_poll` and `stream_wait`

Inline-agent probe outcome:

- inline-agent prompt complexity by itself is serviceable
- actual tool capability still triggers the stall
- body-level `tools` also trigger the stall
- body-level `mode=build` + JSON-schema `format` trigger the stall even without tools

So the next fix target should stay inside the OpenCode local Ollama `/message` handling for tool-capable benchmark-shaped requests, not prompt length or instruction complexity.

Latest note from the ultra-narrow factor-isolation rerun:

- the minimal inline-agent control itself regressed to `ReadTimeout`
- a matching direct Ollama tiny-prompt streamed call also timed out before headers

So the current top blocker is no longer a clean request-field delta. It is a control-path reliability problem that must be steady again before another factor ladder can confidently attribute the first killer field.
