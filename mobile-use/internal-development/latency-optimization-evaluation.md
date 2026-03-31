# Latency Optimization Design — Breakage Evaluation

> **Evaluation Date:** 2026-03-25
> **Evaluated Against:** `latency-optimization-design.md`

---

## Critical Findings Summary

| # | Issue | Severity | Optimization |
|---|-------|----------|--------------|
| 1 | `stream-json` requires `--verbose` flag (design omits it) | **BLOCKER** | Streaming |
| 2 | Wrong event type: no `"type": "content"` events exist | **BLOCKER** | Streaming |
| 3 | Without `--include-partial-messages`, no token-level streaming occurs | **DESIGN FLAW** | Streaming |
| 4 | `--output-format text` appends CLAUDE.md-injected text to responses | **COMPATIBILITY** | Streaming |
| 5 | Structured output agents need complete JSON — streaming adds overhead for no gain | **DESIGN FLAW** | Streaming |
| 6 | Model tiering needs CLI shorthand validation | **LOW** | Model Tiering |
| 7 | Contextor prefetch has state race condition with convergence gate | **MEDIUM** | Parallelization |

---

## 1. Streaming via Claude CLI — Detailed Breakage Analysis

### BLOCKER 1: Missing `--verbose` Flag

The design proposes:
```python
cmd = ["claude", "-p", "--output-format", "stream-json"]
```

**Actual behavior:**
```
$ echo "hello" | claude -p --output-format stream-json
Error: When using --print, --output-format=stream-json requires --verbose
```

**Fix:** Command must be:
```python
cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
```

### BLOCKER 2: Wrong Event Type Parsing

The design parses events as:
```python
if event.get("type") == "content":
    text = event.get("content", "")
```

**Actual `stream-json` output has these event types (verified empirically):**

```json
// Event 1: System init (metadata)
{"type": "system", "subtype": "init", "session_id": "...", "model": "claude-opus-4-6[1m]", ...}

// Event 2: Assistant message (COMPLETE response, not token-by-token)
{"type": "assistant", "message": {"content": [{"type": "text", "text": "the full response here"}], ...}}

// Event 3: Rate limit info
{"type": "rate_limit_event", "rate_limit_info": {...}}

// Event 4: Final result
{"type": "result", "subtype": "success", "result": "the full response here", ...}
```

**There is NO `"type": "content"` event.** The design's parsing loop would:
1. Read all 4 events
2. Match none of them
3. Return `"".join([])` = empty string
4. **Every agent call returns empty string → complete system failure**

**Correct parsing:**
```python
# Option A: Extract from "result" event (simplest, but waits for completion)
if event.get("type") == "result":
    text = event.get("result", "")

# Option B: Extract from "assistant" event
if event.get("type") == "assistant":
    content = event["message"]["content"]
    text = "".join(c["text"] for c in content if c["type"] == "text")
```

### DESIGN FLAW 3: No Token-Level Streaming Without `--include-partial-messages`

Without `--include-partial-messages`, `stream-json` emits only **4 events total** — the full response arrives in a single `assistant` event, NOT streamed token-by-token.

To get actual incremental tokens:
```python
cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
```

This would emit additional intermediate events with partial content. **However, the format of these partial events is undocumented and may change.** The design assumes a clean `{"type": "content", "content": "..."}` format that does not exist.

**Impact on claimed benefits:**
- "Early abort on malformed output" — **Does NOT work** without partial messages, because the full response arrives in one event
- "TTFT monitoring" — **Does NOT work** without partial messages, because first content event IS the complete response
- Token-by-token `on_token` callback — **Does NOT work** without partial messages

### COMPATIBILITY 4: Response Text Includes Injected Content

When using `--output-format text` (current), Claude CLI returns the model's raw text output. But the user's `CLAUDE.md` injects a token usage tracker:

```
{"name": "test", "value": 42}

---
[Token Usage] ~20k / 1000k tokens used | 2% of context
```

The existing `_extract_json()` function handles this correctly by extracting JSON via regex. But if switching to `stream-json`:
- The `result.result` field contains the same appended text
- The `assistant.message.content[0].text` also contains it
- Any new parsing logic must also strip this suffix

**This is not a blocker but a compatibility requirement** that the design doesn't account for.

### DESIGN FLAW 5: Streaming Adds Overhead for Structured Output Agents

All 5 agents use structured output (Pydantic) or tool calls (JSON):

| Agent | Output Format | Why Streaming Doesn't Help |
|-------|--------------|---------------------------|
| Cortex | `CortexOutput` (Pydantic) | Must have complete JSON to call `schema.model_validate()` |
| Planner | `PlannerOutput` (Pydantic) | Must have complete JSON to call `schema.model_validate()` |
| Orchestrator | `OrchestratorOutput` (Pydantic) | Must have complete JSON to call `schema.model_validate()` |
| Contextor | `ContextorOutput` (Pydantic) | Must have complete JSON to call `schema.model_validate()` |
| Executor | Tool calls (JSON) | Must have complete JSON to dispatch tool calls |

Switching to `stream-json` + `--verbose`:
- Adds `--verbose` overhead (init event, rate limit event, result event parsing)
- Still waits for complete response before useful work can begin
- Adds JSON event parsing layer on top of existing JSON response parsing
- **Net effect: slightly MORE latency, not less**

### Streaming Verdict

**Recommendation: Do NOT implement streaming as designed.** The design has 2 blockers that would cause complete system failure, and even if fixed, provides negligible benefit for this architecture where every agent needs complete JSON.

**What WOULD help instead:**
- Use `--output-format json` (not `stream-json`) to get structured metadata (cost, duration, token counts) without the `--verbose` requirement
- Implement TTFT monitoring by simply timing the subprocess call (wall clock from `Popen` start to first stdout byte)
- If partial streaming is needed in the future, it requires `--include-partial-messages` AND a new incremental JSON parser — this is a much larger effort than designed

---

## 2. Model Tiering — Breakage Analysis

### LOW RISK: CLI Model Name Resolution

The design proposes full model IDs:
```jsonc
"model": "claude-sonnet-4-6-20250514"
"model": "claude-haiku-4-5-20251001"
```

**Claude CLI `--model` help text:**
```
Model for the current session. Provide an alias for the latest model
(e.g. 'sonnet' or 'opus') or a model's full name (e.g. 'claude-sonnet-4-6').
```

**Verified:** CLI accepts both shorthand (`sonnet`, `haiku`, `opus`) and full names.

**Current code issue** (`claude_cli.py:281`):
```python
model = self.model_name if self.model_name != "claude" else None
```

This only passes `--model` when `model_name != "claude"`. If config sets `model: "sonnet"`, it will correctly pass `--model sonnet`. No code change needed here.

### MEDIUM RISK: Haiku May Not Support All Structured Output Schemas

Haiku 4.5 has weaker instruction-following than Sonnet. The current prompt-based structured output approach (injecting JSON schema into the prompt and asking for JSON-only response) may fail more often with Haiku:

- `_ClaudeCLIStructuredOutput` relies on the model following: *"You MUST respond with ONLY a valid JSON object"*
- `ChatClaudeCLIWithTools` relies on: *"Respond with ONLY the JSON object. No text before or after."*
- Haiku is more likely to add prose, markdown fences, or extra text around JSON

**The existing `_extract_json()` function mitigates this** — it handles markdown fences and extracts JSON from surrounding text. But edge cases may increase.

**Recommendation:** The fallback-to-Sonnet pattern already exists and handles this. Monitor parse failure rates per model. Acceptable risk.

### LOW RISK: Claude Max Subscription Model Access

Claude Max plan typically includes access to Opus, Sonnet, and Haiku. No issue expected.

### Model Tiering Verdict

**Safe to implement.** The fallback mechanism already handles model capability gaps. Start with conservative tiering (Sonnet for most, Haiku only for Executor/Outputter) and expand based on observed parse success rates.

---

## 3. Parallelization — Breakage Analysis

### Opportunity A (Contextor Prefetch): MEDIUM RISK — State Race Condition

The design proposes:
```python
graph.add_edge("executor_tools", "contextor_prefetch")   # start I/O early
graph.add_edge("executor_tools", "post_executor_tools")  # parallel
```

**Problem:** `contextor_prefetch` writes to `latest_screenshot`, `latest_ui_hierarchy`, etc. But the `convergence_gate` reads `complete_subgoals_by_ids` and other state to decide whether to `"continue"` or `"end"`. If convergence decides `"end"`, the prefetched data is wasted AND we've triggered unnecessary device I/O.

More critically, **LangGraph state updates from parallel branches merge at the convergence point.** If `contextor_prefetch` writes `latest_screenshot` while `post_executor_tools → summarizer → convergence` runs in parallel, the state merge behavior depends on LangGraph's reducer config:

```python
# state.py — these fields use take_last reducer
latest_screenshot: Annotated[str, take_last]
latest_ui_hierarchy: Annotated[str, take_last]
```

With `take_last`, **the last branch to complete wins**. If `contextor_prefetch` finishes before convergence, and convergence decides `"end"`, the prefetched state is harmlessly ignored (graph ends). If convergence decides `"continue"` and routes to `contextor_analyze`, the prefetched state is available. **This is actually safe** — but only if `contextor_analyze` doesn't re-trigger device I/O.

**Real risk:** The `contextor_prefetch` node takes a screenshot DURING tool execution. If the tool changes the screen (e.g., a tap), the screenshot may capture a **transitional state** rather than the final state after the tool completes.

```
Timeline:
  executor_tools: [tap button] ... [animation in progress] ... [screen settled]
  contextor_prefetch:              [screenshot HERE — mid-animation!]
```

**This would cause Cortex to reason about a stale/intermediate screenshot, leading to incorrect decisions.**

**Fix:** Prefetch must wait for executor_tools to complete before capturing screenshot. This limits the overlap to summarizer + convergence gate only (~0.1s saving — negligible).

**Revised approach:** Instead of true parallelism, use a **pipeline prefetch** — start device connection/session warmup (not screenshot) during tool execution:
```python
async def contextor_warmup_node(state):
    """Pre-warm device connection, no screenshot yet."""
    await state.controller.ensure_connected()
    return {}  # no state changes
```

### Opportunity B (Parallel Executor + Orchestrator): LOW RISK

**State write analysis:**
- Executor writes: `executor_messages`, `agents_thoughts`, `cortex_last_thought`
- Orchestrator writes: `subgoal_plan`, `agents_thoughts`, `complete_subgoals_by_ids`

**Conflict:** Both write to `agents_thoughts` (uses `_add_agent_thoughts` reducer which accumulates). This is safe — order of accumulation doesn't matter.

**BUT:** This opportunity is rarely triggered. Looking at `post_cortex_gate`:
- It routes to `"execute_decisions"` when `structured_decisions` exists
- It routes to `"review_subgoals"` when subgoals are completed OR no decisions
- It does NOT currently route to BOTH simultaneously

For parallel execution, Cortex would need to produce both `structured_decisions` AND `complete_subgoals_by_ids` in the same turn. Looking at Cortex output parsing — this IS possible (Cortex can complete a subgoal AND issue new decisions in one response). But it's an edge case, not the common path.

**Verdict:** Safe to implement, but limited real-world impact.

### Opportunity C (Background Summarizer): SAFE

Summarizer only removes old messages via `RemoveMessage`. No conflict with other state fields. Truly independent.

**Verdict:** Safe, trivial to implement, negligible benefit.

### Opportunity D (Speculative Screenshot Prefetch): SAME RISK AS A

Same transitional-screenshot problem as Opportunity A. The convergence gate runs fast (CPU-only), so speculative screenshot capture during gate evaluation risks capturing a screen that hasn't settled after tool execution.

**Verdict:** Only safe if a delay/settle-wait is added before screenshot capture, which negates the latency benefit.

### Parallelization Verdict

**Opportunity A:** Unsafe as designed (transitional screenshot). Rework to pipeline warmup only.
**Opportunity B:** Safe but rare edge case. Low priority.
**Opportunity C:** Safe. Implement for cleanliness, negligible impact.
**Opportunity D:** Unsafe (same issue as A). Skip or add settle-wait.

---

## 4. Revised Recommendations

### Priority Order (by risk-adjusted ROI)

| Priority | Optimization | Action | Expected Impact |
|----------|-------------|--------|-----------------|
| **1** | Model Tiering | Implement as designed with conservative tiers | **30-40% reduction** |
| **2** | Subprocess Metadata | Switch to `--output-format json` (not stream-json) for cost/duration metrics | **Monitoring only** |
| **3** | Parallelization C | Background summarizer | **<0.1s** |
| **4** | Parallelization B | Parallel executor + orchestrator (when applicable) | **5-15s rare** |
| **5** | Parallelization A | Rework as connection warmup only | **~0.2s** |
| **SKIP** | Streaming (as designed) | Do not implement — 2 blockers, negligible benefit | N/A |
| **SKIP** | Parallelization D | Do not implement — transitional screenshot risk | N/A |

### Net Realistic Impact

With model tiering alone: **~30-40% latency reduction per cycle.**
With all safe parallelization: **additional ~1-5% on top.**

**Model tiering is the clear winner.** It's the simplest change (config-only), lowest risk (fallback already exists), and highest impact.
