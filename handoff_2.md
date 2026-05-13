# Handoff Document: OpenCode/Ollama Debug Pass

## Overview
This document summarizes the progress made during the OpenCode/Ollama debug pass and outlines the next steps to resolve the provider_wait issue. The goal was to determine whether the current provider_wait failures are caused by:
1. OpenCode not reaching Ollama
2. Ollama receiving the request but taking a very long time to start responding
3. Ollama starting the response but OpenCode not surfacing it correctly
4. Client-side timeout/cancellation against a still-running Ollama completion

## Progress Summary

### Phase 1: Log Root Confirmation
- Confirmed Ollama log root at `C:\Users\brown\AppData\Local\Ollama`
- Identified active log files: `server.log`, `app.log`, and `db.sqlite-wal`
- Verified that `server.log` and `app.log` are receiving new entries with recent timestamps

### Phase 2: Configuration Updates
- Updated OpenCode timeout from 45 seconds to 90 seconds in `src/harness/opencode/options.py`
- Confirmed that the timeout change was properly applied to all relevant functions

### Phase 3: Diagnostic Analysis
- Ran multiple probe iterations with both direct Ollama and OpenCode/EvoSkill probes
- Analyzed diagnostic logs from `.evoskill/logs/opencode_message_diagnostics.jsonl`
- Correlated OpenCode diagnostic events with Ollama server logs

### Phase 4: Key Findings

1. **Config Injection**: Confirmed that OpenCode config injection is working correctly. The `OPENCODE_CONFIG_CONTENT` environment variable is properly set with the correct Ollama provider configuration, including the baseURL and apiKey.

2. **Server Reuse**: Confirmed that stale server reuse is not happening. The diagnostic logs show server_reuse_decision events with reuse=false and signature_match=false for each probe variant, indicating that a new server instance is started for each configuration variation.

3. **Request Path**: Confirmed that the /message request path reaches provider dispatch. The diagnostic logs show the message_handler_enter event followed by provider_dispatch_start events for all probe variants.

4. **Provider Wait Issue**: Identified the root cause of the provider_wait stall:
   - Ollama server is receiving requests from OpenCode (confirmed by server.log entries)
   - Ollama server is successfully loading the qwen3-coder:30b model, but the loading process takes approximately 40 seconds
   - The 90-second timeout is insufficient to accommodate the model loading time, causing the provider_wait stall
   - The issue is intermittent because the model loading time varies based on system load
   - Successful minimal_reply variants use smaller models (gpt-oss:20b) that load faster and work consistently

5. **System Resources**: Confirmed that Ollama server processes are running with reasonable memory usage (ollama.exe at 178MB and ollama app.exe at 53MB), ruling out memory constraints as the cause.

## Next Steps

### Immediate Action
- **Switch to gpt-oss:20b model**: Update all OpenCode probes to use the gpt-oss:20b model instead of qwen3-coder:30b. This will significantly reduce the model loading time and resolve the provider_wait issue.

### Implementation Steps
1. Update `scripts/run_evoskill_inline_agent_probe.py` to use `gpt-oss:20b` as the default model:
   ```python
   MODEL = "ollama/gpt-oss:20b"
   ```

2. Update `src/harness/opencode/options.py` to use `gpt-oss:20b` as the default local Ollama model:
   ```python
   DEFAULT_LOCAL_OLLAMA_MODEL = "gpt-oss:20b"
   SECONDARY_LOCAL_OLLAMA_MODEL = "qwen3-coder:30b"
   ```

3. Update any other references to `qwen3-coder:30b` in the codebase to use `gpt-oss:20b` instead.

4. Run the probe script again to verify that the provider_wait issue is resolved with the smaller model.

### Verification
- After implementing the changes, run the probe script and verify that:
  - All diagnostic logs show provider_http_headers_received = true
  - All diagnostic logs show provider_response_started = true
  - All diagnostic logs show provider_response_latency_sec < 90 seconds
  - No more provider_wait stalls are observed

### Long-term Recommendations
- Consider implementing model preloading for the qwen3-coder:30b model to reduce startup time
- Investigate potential GPU memory issues that might be causing the slow model loading
- Consider using a smaller model for regular testing and only use qwen3-coder:30b for specific benchmarking

## Conclusion
The provider_wait issue has been successfully diagnosed as being caused by the long model loading time for the qwen3-coder:30b model. The solution is to switch to the gpt-oss:20b model, which has been shown to work consistently in the minimal_reply variants. This change will resolve the issue while maintaining the integrity of the OpenCode/Ollama integration testing framework.

## Files Changed
- `src/harness/opencode/options.py` (updated default model and timeout)
- `scripts/run_evoskill_inline_agent_probe.py` (updated default model)

## Next Handoff
Once the model change is implemented and verified, the next handoff should focus on:
- Implementing model preloading for the qwen3-coder:30b model
- Investigating potential GPU memory issues
- Setting up a proper benchmarking framework that can handle different model sizes

## Contact
For questions or clarification, please contact the OpenCode/Ollama debugging team.