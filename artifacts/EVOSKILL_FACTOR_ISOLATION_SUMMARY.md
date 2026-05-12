# EVOSKILL Factor Isolation Summary

- Model: `ollama/qwen3-coder:30b`
- Query: `this is a test give me a reply`
- Timeout per probe: `15.0` seconds
- Small fix implemented: `False`
- Control regressed: `True`
- First exact failing variant: `1_baseline_inline_agent_prompt_only`
- First exact failing factor or combination: `The known-good minimal inline-agent baseline itself failed in this run, so no later request field can be isolated as the first exact killer with high confidence.`

## Baseline

- Variant: `1_baseline_inline_agent_prompt_only`
- Classification: `assistant_placeholder_timeout`
- provider_request_started: `True`
- provider_response_started: `False`
- headers_arrived: `False`
- first_chunk_arrived: `False`
- assistant_placeholder_persisted: `True`
- assistant_content_persisted: `False`
- timeout_or_exception: `ReadTimeout: `

The known-good minimal inline-agent control did not succeed in this run, so the ladder cannot attribute the failure to a later request field with high confidence.

## Factor probes

### 1_baseline_inline_agent_prompt_only

- Classification: `assistant_placeholder_timeout`
- Tools requested: `none`
- Tool permissions enabled: `False`
- Body tools present: `False`
- Mode: `None`
- Format present: `False`
- provider_request_started: `True`
- provider_response_started: `False`
- headers_arrived: `False`
- first_chunk_arrived: `False`
- assistant_placeholder_persisted: `True`
- assistant_content_persisted: `False`
- timeout_or_exception: `ReadTimeout: `

### 2_inline_agent_benchmark_prompt_only

- Classification: `assistant_placeholder_timeout`
- Tools requested: `none`
- Tool permissions enabled: `False`
- Body tools present: `False`
- Mode: `None`
- Format present: `False`
- provider_request_started: `True`
- provider_response_started: `False`
- headers_arrived: `False`
- first_chunk_arrived: `False`
- assistant_placeholder_persisted: `True`
- assistant_content_persisted: `False`
- timeout_or_exception: `ReadTimeout: `

### 3_tool_permissions_only

- Classification: `assistant_placeholder_timeout`
- Tools requested: `Read`
- Tool permissions enabled: `True`
- Body tools present: `False`
- Mode: `None`
- Format present: `False`
- provider_request_started: `True`
- provider_response_started: `False`
- headers_arrived: `False`
- first_chunk_arrived: `False`
- assistant_placeholder_persisted: `True`
- assistant_content_persisted: `False`
- timeout_or_exception: `ReadTimeout: `

### 4_body_tools_only

- Classification: `assistant_placeholder_timeout`
- Tools requested: `Read`
- Tool permissions enabled: `False`
- Body tools present: `True`
- Mode: `None`
- Format present: `False`
- provider_request_started: `True`
- provider_response_started: `False`
- headers_arrived: `False`
- first_chunk_arrived: `False`
- assistant_placeholder_persisted: `True`
- assistant_content_persisted: `False`
- timeout_or_exception: `ReadTimeout: `

### 5_mode_only

- Classification: `assistant_placeholder_timeout`
- Tools requested: `none`
- Tool permissions enabled: `False`
- Body tools present: `False`
- Mode: `build`
- Format present: `False`
- provider_request_started: `True`
- provider_response_started: `False`
- headers_arrived: `False`
- first_chunk_arrived: `False`
- assistant_placeholder_persisted: `True`
- assistant_content_persisted: `False`
- timeout_or_exception: `ReadTimeout: `

### 6_format_only

- Classification: `assistant_placeholder_timeout`
- Tools requested: `none`
- Tool permissions enabled: `False`
- Body tools present: `False`
- Mode: `None`
- Format present: `True`
- provider_request_started: `True`
- provider_response_started: `False`
- headers_arrived: `False`
- first_chunk_arrived: `False`
- assistant_placeholder_persisted: `True`
- assistant_content_persisted: `False`
- timeout_or_exception: `ReadTimeout: `

### 7_full_tool_surface_only

- Classification: `assistant_placeholder_timeout`
- Tools requested: `Read, Write, Bash, Glob, Grep, Edit, WebFetch, WebSearch, TodoWrite, Skill`
- Tool permissions enabled: `True`
- Body tools present: `True`
- Mode: `None`
- Format present: `False`
- provider_request_started: `True`
- provider_response_started: `False`
- headers_arrived: `False`
- first_chunk_arrived: `False`
- assistant_placeholder_persisted: `True`
- assistant_content_persisted: `False`
- timeout_or_exception: `ReadTimeout: `

### 8_mode_plus_format

- Classification: `assistant_placeholder_timeout`
- Tools requested: `none`
- Tool permissions enabled: `False`
- Body tools present: `False`
- Mode: `build`
- Format present: `True`
- provider_request_started: `True`
- provider_response_started: `False`
- headers_arrived: `False`
- first_chunk_arrived: `False`
- assistant_placeholder_persisted: `True`
- assistant_content_persisted: `False`
- timeout_or_exception: `ReadTimeout: `

### 9_full_benchmark_like_inline

- Classification: `assistant_placeholder_timeout`
- Tools requested: `Read, Write, Bash, Glob, Grep, Edit, WebFetch, WebSearch, TodoWrite, Skill`
- Tool permissions enabled: `True`
- Body tools present: `True`
- Mode: `build`
- Format present: `True`
- provider_request_started: `True`
- provider_response_started: `False`
- headers_arrived: `False`
- first_chunk_arrived: `False`
- assistant_placeholder_persisted: `True`
- assistant_content_persisted: `False`
- timeout_or_exception: `ReadTimeout: `

## Conclusion

- First exact failing variant: `1_baseline_inline_agent_prompt_only`
- First exact failing factor or combination: `The known-good minimal inline-agent baseline itself failed in this run, so no later request field can be isolated as the first exact killer with high confidence.`
- Provider response start restored for any benchmark-capable variant: `False`
- Narrowest blocker: `Both the minimal OpenCode inline-agent control and the matching direct Ollama tiny-prompt check timed out before response headers during this pass.`

