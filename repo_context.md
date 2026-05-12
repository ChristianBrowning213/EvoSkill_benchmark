You are working only inside the **EvoSkill** repository.

## Mission

Rebuild EvoSkill’s local OpenCode/Ollama runtime around the **official Ollama → OpenCode config injection path**, then run a **bounded stabilization pass** to determine whether this fixes the current OpenCode provider-path instability.

This is a **runtime stabilization and diagnosis ticket**, not a benchmark-results ticket.

The core hypothesis is that our current EvoSkill local OpenCode setup may differ from the official Ollama/OpenCode launch/config path in a way that is causing the provider-response stall. We want to align with the official path as closely as possible and then retest the smallest control probes.

## Important context

We already know:

* direct Ollama can work
* EvoSkill’s minimal OpenCode one-shot path can work in some windows
* benchmark-facing OpenCode requests still stall
* the latest factor-isolation pass became unreliable because the **control path itself regressed**
* there is an official Ollama/OpenCode integration path using:

  * `ollama launch opencode`
  * `ollama launch opencode --config`
  * `OPENCODE_CONFIG_CONTENT`

We need to test whether **official-style config injection** stabilizes the OpenCode local provider path.

## Critical execution style requirement

Claude Code has a tendency to drift into:

* read loops
* edit loops
* repetitive restarts
* over-wide refactors
* losing track of whether a result is actually improving

So you must use a **multi-agent checking structure** inside your own workflow.

### Required internal roles

Use these roles explicitly and keep them separate in function:

1. **Planner / Scope Guard**

   * Defines the exact bounded plan
   * Prevents scope creep
   * Ensures we do not drift into benchmark work or broad rewrites

2. **Repo Inspector**

   * Reads the EvoSkill code and docs
   * Locates the real OpenCode config/startup path
   * Compares it to the official Ollama/OpenCode path

3. **Runtime Patcher**

   * Makes the smallest code/config changes needed
   * Does not edit until the Planner and Repo Inspector agree on the target

4. **Probe Runner**

   * Runs only the bounded probes requested
   * Records exact commands and outputs
   * Does not “improvise” extra long experiments

5. **Skeptical Auditor**

   * Checks whether results really improved
   * Flags when Claude Code is slipping into loops, broad speculation, or fake progress
   * Forces explicit stop/go decisions after each phase

You do not need to literally print all of this constantly, but you must behave as if these roles are watching one another.

## Anti-loop rules

You must obey all of these:

* Do **not** reread the same files repeatedly unless a change happened
* Do **not** make more than one patch wave before re-evaluating
* Do **not** run broad suites
* Do **not** keep increasing timeouts blindly
* Do **not** keep editing request shape until the control path is known-good
* After each major step, stop and ask:

  * what changed?
  * did the control improve?
  * what exact evidence supports that?
* If a step does not improve evidence quality, stop expanding it
* Prefer one small patch + one small probe over many speculative edits

## Hard scope limits

Do not:

* touch DreamSkillsBench
* run benchmark suites
* fake success
* keep tuning benchmark request shapes before the control is stable
* do broad repo surgery
* silently change model policy for unrelated paths

You may:

* patch EvoSkill’s OpenCode config generation
* patch EvoSkill’s OpenCode startup logic
* patch local provider/config loading
* add small diagnostic helpers
* use the official Ollama/OpenCode launch/config flow where truthful

---

# Task

## Part 1 — Mirror the official Ollama/OpenCode config path

Inspect and use the official path as closely as possible:

* `ollama launch opencode`
* `ollama launch opencode --config`
* `OPENCODE_CONFIG_CONTENT`

### What to determine

* how EvoSkill currently starts/configures OpenCode
* how that differs from the official Ollama launch/config path
* whether EvoSkill should emit inline config in the same shape the official integration expects
* whether local provider/model/timeout/chunkTimeout config can be aligned more closely with the official path

### What to do

If needed, patch EvoSkill so its local OpenCode path:

* generates config in the same practical structure Ollama/OpenCode expects
* uses explicit provider/model settings
* uses explicit timeout/chunkTimeout where supported
* avoids stale/custom config ambiguity

Document the exact config path used.

### Required checkpoint

Before patching, the **Planner**, **Repo Inspector**, and **Skeptical Auditor** must agree on:

* what file(s) control OpenCode config generation
* what the current config path is
* what exact delta exists versus the official path

No patching before that checkpoint.

---

## Part 2 — Explicitly tune local provider timeout policy

Using OpenCode config support, set explicit local timeout policy for the Ollama provider path.

Where supported, include:

* total timeout
* chunk timeout

Keep values reasonable and documented.
This is not to hide slowness; it is to avoid broken hidden defaults or chunk starvation.

### Required checkpoint

After patching timeout/chunkTimeout handling, the **Skeptical Auditor** must confirm:

* we changed config policy, not benchmark semantics
* we did not broaden scope into unrelated runtime changes

---

## Part 3 — Stabilize the control first

Before any tool-capable or benchmark-like probe, rerun only the minimal controls.

### Required controls

1. direct Ollama tiny prompt for:

   * `ollama/qwen3-coder:30b`
   * `ollama/gpt-oss:20b` if useful
2. minimal OpenCode inline-agent control for:

   * `ollama/qwen3-coder:30b`

### For each capture

* request start
* headers received
* first chunk received
* completion/timeout
* provider config used
* model used
* whether assistant content persisted

### Required checkpoint

Do **not** move on until the **Probe Runner** and **Skeptical Auditor** agree on whether the control is healthy.

If the control is still unstable, stop and report that explicitly.

---

## Part 4 — Only if control is healthy, run tiny tool-capable probes

Only if the minimal control works, run the next tiny ladder:

1. inline-agent prompt only
2. inline-agent prompt + tiny tool capability
3. inline-agent prompt + body tools if needed

Do **not** jump straight to benchmark-sized requests.

The point is to see whether official-style config injection restores provider response start once tools enter the picture.

### Required checkpoint

After each rung:

* compare against the prior rung
* state exactly what changed
* state whether provider response start improved or not
* stop the ladder if the first failure boundary is already clear

---

## Part 5 — Diagnose whether config injection fixed anything

You must produce a clear answer to:

* did the official Ollama/OpenCode config path stabilize the control?
* did it restore provider response start for any tiny tool-capable request?
* is the blocker still request-shape-specific, or still broader provider instability?

The answer must be evidence-based, not guessed.

---

## Outputs

Write:

* `artifacts/evoskill_opencode_official_config_probe.json`
* `artifacts/EVOSKILL_OPENCODE_CONFIG_SUMMARY.md`

Update if needed:

* `docs/comparator_evoskill_setup.md`
* `docs/comparator_evoskill_audit.md`
* `docs/comparator_adapter_notes.md`

The summary must clearly state:

* config path used
* whether it now matches the official Ollama/OpenCode path
* timeout/chunkTimeout settings used
* whether the minimal control is healthy
* whether tiny tool-capable requests improved
* narrowest remaining blocker

---

## Success criteria

This ticket is successful if one of these happens:

### Best case

* official-style config injection stabilizes the control
* and restores provider response start for at least one tool-capable tiny request

### Still useful

* control remains unstable
* but we now know the official config path does **not** solve it
* which rules out local misconfiguration as the main cause

Either outcome is valuable.

---

## Final response format

At the end report:

* Summary
* Files changed
* Commands run
* Official-style config path used
* Timeout/chunkTimeout settings used
* Whether the minimal control became healthy
* Whether tiny tool-capable probes improved
* Narrowest remaining blocker
* Recommended next step

## Final behavioral reminder

Do this as a **disciplined bounded investigation**.

That means:

* inspect
* compare
* patch minimally
* probe minimally
* stop and evaluate

Do not spiral into endless edits or endless rereads.
