# EvoSkill OpenCode Request Diff Summary

- Model tested: `ollama/qwen3-coder:30b`
- Timeout per probe: `45.0s`

## Working minimal path

- Probe: `working_minimal_async_agent`
- Dispatch style: `async_poll`
- Agent: `evoskill-reply`
- Mode: `None`
- Format present: `False`
- Tool surface size: `0`
- Payload size: `163` bytes
- Classification: `completed`

## Failing benchmark-facing path

- Probe: `benchmark_like_stream_wait`
- Dispatch style: `stream_wait`
- Agent: `None`
- Mode: `build`
- Format present: `True`
- Tool surface size: `6`
- Payload size: `963` bytes
- Classification: `provider_wait`

## Key differences found

- `agent`: working=`evoskill-reply` failing=`None`
- `body_keys`: working=`['agent', 'model', 'parts']` failing=`['format', 'mode', 'model', 'parts', 'system', 'tools']`
- `body_preview`: working=`{'agent': 'evoskill-reply', 'mode': None, 'has_system': False, 'has_format': False, 'has_tools': False}` failing=`{'agent': None, 'mode': 'build', 'has_system': True, 'has_format': True, 'has_tools': True}`
- `body_system_prompt_len`: working=`0` failing=`231`
- `dispatch_style`: working=`async_poll` failing=`stream_wait`
- `format_present`: working=`False` failing=`True`
- `format_required_count`: working=`None` failing=`2`
- `format_type`: working=`None` failing=`json_schema`
- `inline_agent_names`: working=`['evoskill-reply']` failing=`[]`
- `inline_agent_prompt_len`: working=`106` failing=`None`
- `mode`: working=`None` failing=`build`
- `payload_size_bytes`: working=`163` failing=`963`
- `polling_enabled`: working=`True` failing=`False`
- `tool_names`: working=`[]` failing=`['bash', 'edit', 'glob', 'grep', 'read', 'write']`
- `tool_surface_size`: working=`0` failing=`6`
- `user_prompt_len`: working=`30` failing=`233`

## Factor-probe conclusion

- First non-completed probe: `minimal_async_no_agent_body_system`
- Most likely cause: `agent_inline_config`

## Current answer

- The working minimal path succeeds in both `async_poll` and `stream_wait` when it stays inside the tiny inline-agent request shape.
- The first scripted failure appears when the request stops using the inline agent shape and switches to a body-level `system` prompt with no inline agent:
  - `working_minimal_async_agent` -> completed
  - `minimal_async_no_agent_body_system` -> `provider_wait`
- After that first cliff, adding benchmark-facing fields such as `mode=build`, JSON-schema `format`, a 6-tool surface, and a larger prompt keeps the request in the same `provider_wait` stall state.
- A later ad hoc retry showed that simply reintroducing an inline agent on top of the benchmark-like request was not enough to restore provider response start, so there is no safe one-line fix yet.
