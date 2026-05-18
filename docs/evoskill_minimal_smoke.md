# EvoSkill Minimal OpenCode Smoke

## Install path used

- `uv sync`

## Runtime path used

- Minimal OpenCode one-shot helper: `src.harness.opencode.run_minimal_reply_smoke(...)`
- This stays on the real EvoSkill -> OpenCode -> `/session` -> `/message` path.
- It removes nonessential structured-output, build-mode, and tool-surface complexity for the smoke.

## Command run

- `uv run python scripts/run_evoskill_minimal_smoke.py`

## Prompt

- `this is a test give me a reply`

## Results

### ollama/gpt-oss:20b

- direct Ollama success: `True`
- direct Ollama error: `None`
- OpenCode minimal success: `False`
- OpenCode error: `TimeoutError: OpenCode /message dispatch did not produce assistant output before timeout.`
- reply: `None`

### ollama/qwen3-coder:30b

- direct Ollama success: `True`
- direct Ollama error: `None`
- OpenCode minimal success: `False`
- OpenCode error: `TimeoutError: OpenCode /message dispatch did not produce assistant output before timeout.`
- reply: `None`

## Conclusion

- Successful local EvoSkill/OpenCode one-shot model: `None`
- Reply returned: `None`
- Remaining blocker for preferred model: `TimeoutError: OpenCode /message dispatch did not produce assistant output before timeout.`

