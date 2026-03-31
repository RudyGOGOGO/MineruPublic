# Latency Optimization Design: Model Tiering

> **Status:** Draft
> **Scope:** Model tiering for non-critical agents
> **Constraint:** Must use Claude CLI subprocess (no direct API access)

---

## 1. Current Baseline

### Architecture

```
START → planner → orchestrator → convergence
                                     ↓
                              (conditional)
                              ├─ contextor → cortex → executor → executor_tools → summarizer → convergence
                              ├─ planner (replan)
                              └─ END
```

### Per-Cycle Latency Breakdown (typical)

| Phase              | Component              | Estimated Time | Type        |
|--------------------|------------------------|----------------|-------------|
| Contextor          | Screenshot + OCR + UI  | 1-3s           | Device I/O  |
| Contextor          | LLM (conditional)      | 5-15s          | LLM         |
| Cortex             | LLM (reasoning)        | 10-30s         | LLM         |
| Executor           | LLM (tool selection)   | 5-15s          | LLM         |
| Executor Tools     | Tool execution         | 1-5s           | Device I/O  |
| Summarizer         | Message cleanup        | <0.1s          | CPU         |
| Orchestrator       | LLM (conditional)      | 5-15s          | LLM         |

**Total per cycle:** ~25-80s, dominated by 3-4 sequential LLM calls via Claude CLI subprocess.

### Current Config & Problem

```jsonc
// llm-config.override.jsonc — ALL agents use same model
{ "provider": "claude", "model": "claude" }  // defaults to whatever Claude CLI resolves
```

All agents use the same model (currently Opus via CLI default). Non-critical agents like Executor and Contextor pay the same latency cost as Cortex despite needing far less reasoning capability.

Additionally, `get_claude_llm_config()` (used when `--model-provider claude`) applies a single `settings.CLAUDE_MODEL` to every agent, meaning the `--claude-model` CLI flag overrides all agents uniformly — no per-agent differentiation exists.

**Relevant code path:**
```
CLI: --claude-model sonnet
  → main.py:80      settings.CLAUDE_MODEL = "sonnet"
  → main.py:87      get_claude_llm_config()
  → config.py:361   claude_fb = LLM(provider="claude", model=settings.CLAUDE_MODEL)  # same for ALL
  → config.py:367   LLMConfig(planner=claude_with_fb, orchestrator=claude_with_fb, ...)  # identical
```

---

## 2. Approach

Modify `get_claude_llm_config()` to automatically tier agents based on the user-specified `--claude-model` flag. The user picks the "primary" model (used for critical agents), and non-critical agents are auto-downgraded to a faster model.

### Tiering Rule

```
User selects:    Tier 1 (critical)          Tier 2 (non-critical)
─────────────    ───────────────────        ─────────────────────
opus         →   opus                       sonnet
sonnet       →   sonnet                     haiku
haiku        →   haiku                      haiku (no downgrade possible)
```

### Edge Case: The Literal String `"claude"`

`ChatClaudeCLI` has a guard at three call sites (`_generate`, `_ClaudeCLIStructuredOutput.invoke`, `ChatClaudeCLIWithTools._generate`):

```python
model = self.model_name if self.model_name != "claude" else None
```

When `model_name == "claude"`, the `--model` flag is **not passed** to the subprocess, and the CLI falls back to its own default (currently Opus). This means:
- If `settings.CLAUDE_MODEL` is the literal string `"claude"`, both Tier 1 and Tier 2 would resolve to `"claude"`, the guard would skip `--model` for all agents, and **tiering silently does nothing**.
- This can happen if: the user passes `--claude-model claude`, or the override config sets `"model": "claude"` (which is the current override config value).

The design handles this by **normalizing** `"claude"` to a concrete model name before tier lookup. See `_resolve_claude_model()` below.

---

## 3. Agent Tier Assignments

| Agent            | Tier | Rationale |
|------------------|------|-----------|
| **Cortex**       | 1    | Core reasoning — multi-step UI decisions, directly impacts task success |
| **Planner**      | 1    | Goal decomposition — needs strong reasoning to break tasks into subgoals |
| **Hopper**       | 1    | Needs large context window (256k+) — Haiku's context may be insufficient |
| **Executor**     | 2    | Mechanical tool dispatch — translates structured decisions into tool calls |
| **Orchestrator** | 2    | Subgoal bookkeeping — mostly status transitions with occasional judgment |
| **Contextor**    | 2    | Rarely invokes LLM (only app lock verification) — fast model sufficient |
| **Outputter**    | 2    | Formatting only — no reasoning required |

---

## 4. Code Changes

### 4.1 Model Normalization and Tier Mapping (`config.py`)

```python
# The Claude CLI default model when no --model flag is passed.
# ChatClaudeCLI skips --model when model_name == "claude", which makes the CLI
# use its own default. We need to know what that default resolves to so we can
# compute the correct Tier 2 downgrade.
CLAUDE_CLI_DEFAULT_MODEL = "opus"

# Tier downgrade mapping: given a primary model shorthand, what's the fast tier?
CLAUDE_TIER_MAP: dict[str, str] = {
    "opus": "sonnet",
    "claude-opus-4-6": "sonnet",
    "sonnet": "haiku",
    "claude-sonnet-4-6": "haiku",
    "claude-sonnet-4-6-20250514": "haiku",
    "haiku": "haiku",  # no further downgrade
    "claude-haiku-4-5": "haiku",
    "claude-haiku-4-5-20251001": "haiku",
}


def _resolve_claude_model(model: str) -> str:
    """Normalize the model string to a concrete CLI shorthand.

    The literal string "claude" is special — ChatClaudeCLI treats it as
    "don't pass --model, let the CLI pick its default". We resolve it to
    a concrete name so that tier mapping works correctly.

    Also normalizes full model IDs to shorthand so the --model flag is
    always passed explicitly, making behavior predictable regardless of
    the CLI's default.
    """
    # "claude" means "CLI default" — resolve to concrete name
    if model == "claude":
        return CLAUDE_CLI_DEFAULT_MODEL

    # Normalize full IDs to shorthand so --model flag is always passed
    NORMALIZE_MAP = {
        "claude-opus-4-6": "opus",
        "claude-sonnet-4-6": "sonnet",
        "claude-sonnet-4-6-20250514": "sonnet",
        "claude-haiku-4-5": "haiku",
        "claude-haiku-4-5-20251001": "haiku",
    }
    return NORMALIZE_MAP.get(model, model)


def _get_fast_model(primary_model: str) -> str:
    """Return the fast-tier model for a given primary model."""
    return CLAUDE_TIER_MAP.get(primary_model, primary_model)
```

### 4.2 Updated `get_claude_llm_config()` (`config.py`)

```python
def get_claude_llm_config() -> LLMConfig:
    """Returns an LLM config using Claude CLI with automatic model tiering.

    Tier 1 (critical): Cortex, Planner, Hopper — use settings.CLAUDE_MODEL
    Tier 2 (non-critical): Executor, Orchestrator, Contextor, Outputter — auto-downgraded
    Fallback for Tier 2: escalates to Tier 1 model on failure
    """
    primary = _resolve_claude_model(settings.CLAUDE_MODEL)
    fast = _get_fast_model(primary)

    tier1 = LLMWithFallback(
        provider="claude",
        model=primary,
        fallback=LLM(provider="claude", model=primary),
    )
    tier2 = LLMWithFallback(
        provider="claude",
        model=fast,
        fallback=LLM(provider="claude", model=primary),  # escalate on failure
    )

    logger.info(f"Claude model tiering: tier1={primary}, tier2={fast}")

    return LLMConfig(
        planner=tier1,
        orchestrator=tier2,
        contextor=tier2,
        cortex=tier1,
        executor=tier2,
        utils=LLMConfigUtils(
            outputter=tier2,
            hopper=tier1,
        ),
    )
```

### 4.3 No CLI Changes Needed

The existing `--claude-model` flag continues to work as before:
- `--claude-model opus` → Cortex/Planner/Hopper get Opus, others get Sonnet
- `--claude-model sonnet` → Cortex/Planner/Hopper get Sonnet, others get Haiku
- `--claude-model haiku` → All agents get Haiku (no downgrade possible)
- `--claude-model claude` → resolves to Opus (CLI default) → same as `--claude-model opus`
- `--claude-model claude-sonnet-4-6-20250514` → normalizes to `sonnet` → same as `--claude-model sonnet`
- No flag → `settings.CLAUDE_MODEL` default is `"claude-haiku-4-5-20251001"` → normalizes to `haiku` → no tiering (preserves current behavior)

Users who want uniform model behavior can pass `--claude-model haiku` to disable tiering.

### 4.4 Optional: `--no-model-tiering` Flag

If users need to force uniform model assignment (e.g., for debugging):

```python
# main.py — add flag
no_model_tiering: Annotated[
    bool,
    typer.Option(
        "--no-model-tiering",
        help="Disable automatic model tiering. All agents use the same model.",
    ),
] = False,

# main.py — usage
if model_provider == ModelProvider.CLAUDE:
    if no_model_tiering:
        llm_config = get_claude_llm_config_uniform()  # current behavior
    else:
        llm_config = get_claude_llm_config()           # new tiered behavior
```

This is optional — can be added later if users request it.

---

## 5. Config File Path (`--model-provider default`)

When using `--model-provider default` (reads `llm-config.override.jsonc`), tiering is already possible via the config file. No code changes needed for this path — users can manually set per-agent models in the override file.

The code change in Section 4 only affects the `--model-provider claude` path (i.e., `get_claude_llm_config()`).

---

## 6. Latency Impact Estimate

### Model Speed Reference

| Model             | Typical TTFT | Typical Generation (500 tokens) |
|-------------------|--------------|-------------------------------|
| Claude Opus 4.6   | 3-8s         | 15-30s                        |
| Claude Sonnet 4.6 | 1-3s         | 5-12s                         |
| Claude Haiku 4.5  | 0.5-1s       | 2-5s                          |

### Scenario A: `--claude-model opus` (current default)

| Agent        | Current (Opus) | Tiered              | Saving     |
|--------------|----------------|---------------------|------------|
| Cortex       | 10-30s         | 10-30s (Opus)       | 0s         |
| Planner      | 10-30s         | 10-30s (Opus)       | 0s         |
| Executor     | 5-15s          | 1-3s (Sonnet)       | ~4-12s     |
| Orchestrator | 5-15s          | 1-3s (Sonnet)       | ~4-12s     |
| Contextor    | 5-15s          | 1-3s (Sonnet)       | ~4-12s     |

**Saving: ~12-36s per cycle (~30-45%)**

### Scenario B: `--claude-model sonnet`

| Agent        | Current (Sonnet) | Tiered             | Saving     |
|--------------|------------------|--------------------|------------|
| Cortex       | 5-12s            | 5-12s (Sonnet)     | 0s         |
| Planner      | 5-12s            | 5-12s (Sonnet)     | 0s         |
| Executor     | 5-12s            | 2-5s (Haiku)       | ~3-7s      |
| Orchestrator | 5-12s            | 2-5s (Haiku)       | ~3-7s      |
| Contextor    | 5-12s            | 2-5s (Haiku)       | ~3-7s      |

**Saving: ~9-21s per cycle (~25-35%)**

### Scenario C: `--claude-model haiku`

No tiering possible — all agents already on fastest model. **Saving: 0s.**

### Task-Level Impact (10-cycle task)

| Scenario | Current     | Tiered       | Reduction |
|----------|-------------|--------------|-----------|
| Opus     | 250-800s    | 130-520s     | ~35%      |
| Sonnet   | 150-450s    | 100-310s     | ~30%      |
| Haiku    | 70-200s     | 70-200s      | 0%        |

---

## 7. Quality Guardrails

### Fallback Escalation

Tier 2 agents automatically escalate to the Tier 1 model on failure. This is built into the `LLMWithFallback` config — the existing `with_fallback()` pattern handles it with no new code.

### Success Rate Monitoring

Track per-agent parse success rate to detect quality regression:

```python
# In with_fallback():
logger.info(f"[{agent_name}] primary={model} success={not used_fallback}")
```

### A/B Evaluation

Run same tasks with uniform model vs. tiered config, compare:
- Task completion rate
- Average cycles to completion
- Tool call accuracy (executor)
- Fallback trigger rate per agent

### Promotion Rule

**If a Tier 2 agent's fallback rate exceeds ~20%**, promote it to Tier 1 — the overhead of two LLM calls (fail + retry) negates the speed benefit.

---

## 8. Risk: Haiku Structured Output Compliance

Haiku 4.5 has weaker instruction-following than Sonnet. The current prompt-based structured output approach (injecting JSON schema and asking for JSON-only response) may produce more parse failures with Haiku.

**Mitigations already in place:**
- `_extract_json()` handles markdown fences and extracts JSON from surrounding prose
- `with_fallback()` retries with the Tier 1 model on any failure
- Executor's `_build_tools_prompt()` includes explicit JSON-only instructions

---

## 9. Changes Required

| File | Change | Risk |
|------|--------|------|
| `config.py` | Add `_resolve_claude_model()`, `CLAUDE_TIER_MAP`, `_get_fast_model()` | None |
| `config.py` | Rewrite `get_claude_llm_config()` with tiered logic | Low |
| `config.py` | Add `CLAUDE_CLI_DEFAULT_MODEL` constant (update if CLI default changes) | Low |
| `main.py` | (Optional) Add `--no-model-tiering` flag | None |

No changes to: `claude_cli.py`, `llm-config.override.jsonc`, `graph.py`, `state.py`, or any agent code.

---

## 10. Implementation Plan

1. Add `CLAUDE_CLI_DEFAULT_MODEL`, `_resolve_claude_model()`, `CLAUDE_TIER_MAP`, `_get_fast_model()` to `config.py`
2. Rewrite `get_claude_llm_config()` with tiered logic
3. Test all input forms resolve correctly:
   - `--claude-model sonnet` → Tier 1: sonnet, Tier 2: haiku
   - `--claude-model opus` → Tier 1: opus, Tier 2: sonnet
   - `--claude-model haiku` → Tier 1: haiku, Tier 2: haiku (no tiering)
   - `--claude-model claude` → resolves to opus → Tier 1: opus, Tier 2: sonnet
   - `--claude-model claude-sonnet-4-6-20250514` → normalizes to sonnet → Tier 2: haiku
   - No flag (default `settings.CLAUDE_MODEL = "claude-haiku-4-5-20251001"`) → normalizes to haiku → no tiering
4. Verify `--model` flag is passed in subprocess by checking log: `[Claude CLI] ... --model sonnet`
5. Run benchmark: 5 identical tasks, compare completion rate + cycle count + fallback rate
6. Tune tier assignments based on results (promote agents with high fallback rates)

### Rollback

Revert `get_claude_llm_config()` to the original uniform implementation. Single function change.

---

## 11. Rejected Alternatives

### Streaming via Claude CLI
Evaluated and rejected. `--output-format stream-json` requires `--verbose`, the actual event format differs from what was designed (no token-level `"type": "content"` events), and all agents require complete JSON for Pydantic parsing — making streaming overhead with no benefit. See `latency-optimization-evaluation.md`.

### Agent Parallelization
Evaluated and rejected. Contextor prefetch during tool execution would capture screenshots mid-animation (transitional screen state), causing Cortex to reason on stale data. Parallel executor + orchestrator is safe but only triggers on a rare edge case. See `latency-optimization-evaluation.md`.

### Config-File-Only Tiering (Option A)
Rejected because `get_claude_llm_config()` ignores `llm-config.override.jsonc` entirely — it hardcodes `settings.CLAUDE_MODEL` for all agents. Config-file tiering only works with `--model-provider default`, which most Claude CLI users don't use.
