# Comparator EvoSkill Audit

## Current diagnosis

After correcting the local model path to the real local models, the remaining stall is OpenCode request-shape specific rather than model-specific.

- Direct Ollama `gpt-oss:20b` completed both bounded probes.
- Direct Ollama `qwen3-coder:30b` completed both bounded probes.
- The working minimal OpenCode `qwen3-coder:30b` one-shot reply path completed.
- The benchmark-facing OpenCode request shape still stalls at `provider_wait`.

## Direct Ollama probe highlights

- `gpt-oss:20b` plain-text probe: first chunk `0.938s`, completion `3.406s`
- `gpt-oss:20b` structured-text probe: first chunk `1.735s`, completion `7.094s`
- `qwen3-coder:30b` plain-text probe: first chunk `0.500s`, completion `1.422s`
- `qwen3-coder:30b` structured-text probe: first chunk `0.719s`, completion `1.156s`

## OpenCode working-vs-failing highlights

Working minimal path:

- probe: `working_minimal_async_agent`
- provider: `ollama`
- model: `ollama/qwen3-coder:30b`
- inline agent: `evoskill-reply`
- mode: none
- format: none
- tool surface: none
- payload size: `163` bytes
- classification: `completed`
- provider response started and completed

First failing step in the scripted ladder:

- probe: `minimal_async_no_agent_body_system`
- provider: `ollama`
- model: `ollama/qwen3-coder:30b`
- inline agent: none
- body-level `system`: present
- mode: none
- format: none
- tool surface: none
- payload size: `228` bytes
- classification: `provider_wait`

Benchmark-facing failing shape:

- probe: `benchmark_like_stream_wait`
- provider: `ollama`
- model: `ollama/qwen3-coder:30b`
- inline agent: none
- body-level `system`: present
- mode: `build`
- format: `json_schema`
- tool surface: `bash`, `edit`, `glob`, `grep`, `read`, `write`
- payload size: `963` bytes
- classification: `provider_wait`

Failing no-agent/body-system and benchmark-like runs showed the same stall pattern:

- provider catalog check succeeded
- `/message` request dispatch started
- no provider response start
- no assistant output materialized
- suspected stall stage: `provider_wait`
- terminating exception: `ReadTimeout`

## Interpretation

- The corrected local models are healthy through direct Ollama.
- The failure is not tied to the old `qwen2.5-coder:14b` path.
- The failure is not explained by dispatch style alone, because the tiny inline-agent path completed in both `async_poll` and `stream_wait`.
- The failure is not caused by JSON-schema `format` alone, because the first scripted failure happens before `format`, tools, or `mode=build` are added.
- The first decisive cliff is leaving the tiny inline-agent request shape and switching to a body-level `system` request with no inline agent.
- Once the request is in that failing regime, adding benchmark-facing fields (`mode=build`, `format`, tools, larger prompt size) keeps it stalled at the same `provider_wait` stage.

## Current benchmark-readiness conclusion

- EvoSkill is still not comparator-ready for benchmark-facing runs on the local OpenCode/Ollama path.
- The remaining blocker is now narrow and specific:
  - minimal inline-agent request -> works
  - first no-agent/body-system request -> stalls
  - benchmark-like request -> also stalls
- No safe fix has been established yet inside EvoSkill alone.

## Inline-agent benchmark probe update

The follow-up inline-agent probe shows that inline-agent prompt complexity is not the blocker.

Restored variants:

- `1_working_minimal_inline_baseline`
- `2_inline_agent_benchmark_prompt_only`

Those two variants shared the same minimal body shape:

- `agent`
- `model`
- `parts`

They differed only in inline-agent prompt length and content, and both received:

- provider response start
- response headers
- first chunk
- persisted assistant text

The next cliff is narrower than before:

- any real tool-capable inline-agent variant stalls
- any body-level `tools` field stalls
- body-level `mode=build` + `format` also stalls even with no tools

Observed failure pattern for those variants:

- request dispatch starts
- no provider response start
- no headers
- assistant placeholder persists
- `ReadTimeout`
- classification: `assistant_placeholder_only`

## Factor-isolation rerun update

The later ultra-narrow factor-isolation pass did not reproduce the old working control:

- `1_baseline_inline_agent_prompt_only` timed out before response headers
- every later one-factor variant also timed out in the same way
- the matching direct Ollama tiny-prompt streamed probe also timed out before first headers

That means the latest bounded pass does not isolate a new first request-field cliff. Instead it shows a control regression:

- provider request dispatch still starts
- assistant placeholder persistence still occurs
- provider response start does not begin
- no later request field can be blamed with high confidence until the control becomes healthy again
