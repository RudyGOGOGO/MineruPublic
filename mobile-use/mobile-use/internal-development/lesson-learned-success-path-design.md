# Enhancement: Success Path Recording for Lesson-Learned Memory

## Problem

The current lesson-learned system only records **negative signals** (mistakes, tool failures, subgoal failures) and **breakthrough strategies** (when the agent gets stuck then unstuck). It does not record **successful navigation paths** — the correct sequence of screens and actions that achieved a goal.

### Concrete Example

The agent needs to change a profile's location sharing setting:
1. Taps "Account" icon → wrong screen (Profile page) → goes back
2. Taps "Privacy" icon → wrong screen (Data settings) → goes back
3. Taps "Location" icon → correct screen → taps "Sharing" → done

**Current system records:** Almost nothing. Each wrong-screen tap "succeeded" (screen changed), no tool errors occurred, and the subgoal eventually completed. The agent starts from zero next time.

**With this enhancement:** On subgoal completion, the system records: "To change location sharing: tapped Location icon → tapped Sharing toggle." Next run, Cortex sees this as a proven path and goes directly.

---

## Is Recording Correct Choices an Anti-Pattern?

**No.** Evaluation:

| Concern | Assessment |
|---------|------------|
| **Storage bloat** — every success generates data | Mitigated by: only recording on subgoal completion (not every action), compaction on read, and staleness eviction. A subgoal typically produces 1 success path record, not N. |
| **Overfitting** — agent blindly follows recorded paths even when UI changes | Mitigated by: confidence decay, app version tagging, staleness cutoffs, and screen signature matching. If the path doesn't match current UI, it gets low relevance score. |
| **Noise** — trivial successes (e.g., "opened Settings") add no value | Mitigated by: minimum action threshold (paths with < 2 navigation steps are not recorded) and the scorer naturally deprioritizes generic lessons. |
| **Contradicts "learn from failure" principle** | No — it complements it. The current system tells the agent what NOT to do; this tells it what TO do. Both signals are needed for efficient learning. This is standard in reinforcement learning (positive + negative reward). |
| **Stale paths mislead** | Same risk as existing mistake lessons — handled by the same staleness/confidence mechanisms already in place. |

**Verdict:** Not an anti-pattern. It's the missing positive-reinforcement half of the learning system. The key is recording at the right granularity (navigation path, not individual taps) and keeping it compact.

---

## Design

### New Lesson Type: `success_path`

Added alongside existing types (`mistake`, `strategy`, `ui_mapping`):

```json
{
  "id": "spa-f2a1",
  "type": "success_path",
  "category": "navigation",
  "summary": "Change location sharing: Location → Sharing toggle",
  "context": {
    "goal": "Change profile's location sharing setting",
    "screen_signature": {
      "activity": "com.android.settings/.Settings",
      "key_elements": ["Network & internet", "Connected devices", "Apps", "Location"]
    }
  },
  "lesson": "To change location sharing in Settings: tap 'Location' (not Account or Privacy), then tap the sharing toggle on the Location screen.",
  "suggested_strategy": "Navigate: Settings → Location → Sharing. The Location icon is in the main settings list, not under Account or Privacy.",
  "path": [
    {
      "action": "tap",
      "target_text": "Location",
      "target_resource_id": "android:id/title",
      "result": "navigated to Location settings"
    },
    {
      "action": "tap",
      "target_text": "Location sharing",
      "target_resource_id": "android:id/title",
      "result": "opened sharing toggle"
    }
  ],
  "confidence": 0.5,
  "occurrences": 1,
  "applied_success": 0,
  "applied_failure": 0,
  "created": "2026-03-25T12:00:00Z",
  "last_seen": "2026-03-25T12:00:00Z",
  "deprecated": false,
  "app_version": "15"
}
```

### New Data Types

```python
class PathStep(BaseModel):
    """A single step in a successful navigation path."""
    action: str = ""                    # Tool name: tap, launch_app, open_link, back
    target_text: str | None = None      # Text of the tapped element
    target_resource_id: str | None = None  # resource_id if available
    result: str = ""                    # Brief description of what happened

class LessonEntry(BaseModel):
    # ... existing fields ...
    path: list[PathStep] | None = None  # Only populated for type="success_path"
```

The `path` field is `None` for all existing lesson types — no schema break, fully backward compatible.

---

## Recording: When, Where, and What

### Data Source: Action Trail (new state field), NOT `agents_thoughts`

**Problem with `agents_thoughts`:** The Cortex `decisions_reason` is free-form natural language with no consistent format. Parsing target text via regex (e.g., looking for quoted strings after "tap") is fragile — the Cortex sometimes quotes, sometimes doesn't, sometimes describes elements without naming them. The actual structured tool call data (`{'name': 'tap', 'args': {'target': {'text': 'Location'}}}`) lives in `executor_messages`, but those are **cleared every Cortex cycle** (cortex.py line 166: `EXECUTOR_MESSAGES_KEY: [RemoveMessage(id=REMOVE_ALL_MESSAGES)]`).

**Solution:** Introduce a lightweight **action trail** — a list of `PathStep` entries accumulated in graph state. Each cycle, the `post_executor_tools` node (which already reads `executor_messages` to extract `last_tool_name`/`last_tool_status`) also extracts the structured action data and appends it to the trail. This captures tool call details while they're still available, before the Cortex clears them.

### New State Fields

```python
# In State (graph/state.py):

# lesson-learned: success path recording
action_trail: Annotated[
    list[dict], "Accumulated tool calls for current subgoal", take_last
] = []
action_trail_subgoal_id: Annotated[
    str | None, "Subgoal ID the current action_trail belongs to", take_last
] = None
last_focused_app: Annotated[
    str | None, "Last known focused_app_info (survives Cortex nulling)", take_last
] = None
pending_success_path_subgoal: Annotated[
    str | None, "Subgoal description to record on next Contextor cycle", take_last
] = None
```

**Why `last_focused_app`?** The Cortex **nulls `focused_app_info`** in its state update (cortex.py line 159) before the Orchestrator runs. The Contextor needs the app package to know where to store the lesson. `last_focused_app` is set by the Contextor (which always has fresh device data) and is NOT nulled by the Cortex.

**Why `action_trail_subgoal_id`?** In multi-subgoal plans, we must know which actions belong to which subgoal. When the Orchestrator starts a new subgoal, it resets the trail with the new subgoal's ID. On completion, only actions matching the current subgoal ID are recorded.

**Why `pending_success_path_subgoal`?** The Orchestrator and Executor run **in parallel** after `post_cortex_gate` (LangGraph fans out when the gate returns a `Sequence`). Recording in the Orchestrator would read an incomplete trail. Instead, the Orchestrator sets this flag with the completed subgoal's description, and the Contextor records the path on the **next cycle** — after convergence, when the trail is guaranteed complete.

### Recording Flow

**Critical constraint:** `post_cortex_gate` (graph.py) returns a `Sequence` — when the Cortex both completes a subgoal AND issues decisions, the Orchestrator and Executor branches run **in parallel** in LangGraph. This means the Orchestrator cannot record the success path because `post_executor_tools` may still be appending to the action trail concurrently. The trail would be incomplete.

**Solution:** Record the success path in the **`convergence_node`** (which runs after both parallel branches merge, with `defer=True` barrier semantics), not in the Orchestrator or Contextor. The Orchestrator sets a `pending_success_path_subgoal` flag and `action_trail_subgoal_id`; `convergence_node` checks the flag and records the path when the trail is guaranteed complete.

The Orchestrator's role is limited to: (1) setting `action_trail_subgoal_id` when starting a new subgoal, and (2) setting a `pending_success_path_subgoal` flag when a subgoal completes.

```
                    ┌──────────────────────────────────────┐
                    │          Contextor Node               │
                    │  - sets last_focused_app              │
                    │  - loads lessons                      │
                    │  (no recording logic here)            │
                    └──────────────┬───────────────────────┘
                                   │
                    ┌──────────────▼───────────────────────┐
                    │          Cortex Node                  │
                    │  - nulls focused_app_info             │
                    │  (action_trail survives)              │
                    └──────────────┬───────────────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │ (parallel)                              │ (parallel)
 ┌────────────▼─────────────┐              ┌────────────▼─────────────┐
 │   Executor + ToolNode    │              │    Orchestrator Node      │
 │   (runs tool calls)      │              │  - on subgoal complete:   │
 └────────────┬─────────────┘              │    set pending flag +     │
              │                            │    set action_trail_      │
 ┌────────────▼─────────────┐              │    subgoal_id             │
 │  post_executor_tools     │              │  (does NOT write          │
 │  - extracts tool name    │              │   action_trail or reset)  │
 │  - appends to            │              └───────────────────────────┘
 │    action_trail           │
 └────────────┬─────────────┘
              │
              ▼
        convergence (defer=True — waits for BOTH branches)
          - IF pending_success_path_subgoal:
              record_success_path()
              reset action_trail = []
              clear pending flag
          - trail is guaranteed complete here
              │
              ▼
         convergence_gate → continue/replan/end
              │
              ▼
         Contextor (next cycle — clean state)
```

### Step 1: Contextor sets `last_focused_app`

In `contextor.py`, after getting `current_app_package`:

```python
# In ContextorNode.__call__(), in the return update dict:
"last_focused_app": current_app_package,
```

This is set **unconditionally** (not gated behind `lessons_dir`) since it's a trivial assignment. But the downstream consumers (recording) are still gated.

### Step 2: `post_executor_tools` appends to action trail

**Replace the existing `post_executor_tools_node` function** (defined at line 100 in `graph.py`, module-level) with the version below. Also add the new `_build_trail_entry` helper function **immediately after** `post_executor_tools_node` (both are module-level functions, not inside `get_graph`):

```python
def post_executor_tools_node(state: State):
    """Extract last tool name/status and append to action trail for lesson recording."""
    tool_messages = [m for m in state.executor_messages if isinstance(m, ToolMessage)]
    if not tool_messages:
        return {}

    last_msg = tool_messages[-1]
    update = {
        "last_tool_name": getattr(last_msg, "name", None),
        "last_tool_status": getattr(last_msg, "status", None),
    }

    # Append structured action data to trail (for success path recording).
    # Read from AIMessage.tool_calls — these contain the structured args
    # (name, target text, resource_id) that we need.
    ai_messages = [m for m in state.executor_messages if isinstance(m, AIMessage)]
    if ai_messages:
        last_ai = ai_messages[-1]
        tool_calls = getattr(last_ai, "tool_calls", [])
        new_trail_entries = []
        for i, tc in enumerate(tool_calls):
            # Pair with corresponding ToolMessage for status
            tm = tool_messages[i] if i < len(tool_messages) else None
            entry = _build_trail_entry(tc, tm)
            if entry:
                new_trail_entries.append(entry)
        if new_trail_entries:
            update["action_trail"] = state.action_trail + new_trail_entries

    return update


def _build_trail_entry(tool_call: dict, tool_message: ToolMessage | None) -> dict | None:
    """Build a trail entry from a structured tool call + its result.

    Only records navigation-relevant actions (tap, launch_app,
    open_link, back, press_key with Back). Skips swipes (scrolling noise),
    wait_for_delay, video_recording, scratchpad, etc.
    """
    name = tool_call.get("name", "")
    args = tool_call.get("args", {})
    status = getattr(tool_message, "status", None) if tool_message else None

    # Normalize back actions: press_key(Back) and back() are both "back"
    if name == "press_key" and args.get("key", "").lower() == "back":
        action = "back"
    elif name == "back":
        action = "back"
    elif name in ("tap", "long_press_on"):
        action = "tap"
    elif name in ("swipe", "swipe_percentages"):
        return None  # Exclude swipes — they represent scrolling, not navigation
                     # choices. Including them would pollute paths with noise
                     # (e.g., 3 swipes to find an element before tapping it).
                     # The tap that follows the scroll IS the navigation choice.
    elif name == "launch_app":
        action = "launch_app"
    elif name == "open_link":
        action = "open_link"
    else:
        return None  # Not a navigation action

    # Extract target info from structured args
    target = args.get("target", {})
    target_text = target.get("text") if isinstance(target, dict) else None
    target_resource_id = target.get("resource_id") if isinstance(target, dict) else None

    # For launch_app, the target is the app name
    if name == "launch_app":
        target_text = args.get("app_name")

    # For open_link, the target is the URL
    if name == "open_link":
        target_text = args.get("url")

    return {
        "action": action,
        "target_text": target_text,
        "target_resource_id": target_resource_id,
        "status": status,  # "success" or "error"
        "agent_thought": args.get("agent_thought", ""),
    }
```

**Why this works:** The `post_executor_tools` node runs **after** the ToolNode and **before** the Cortex clears `executor_messages`. The `AIMessage.tool_calls` contain the exact structured args (`target.text`, `target.resource_id`, `app_name`, etc.) — no regex parsing needed.

### Step 3: Orchestrator sets pending flag and new subgoal ID (does NOT reset trail)

**Critical rule:** The Orchestrator must **never** write to `action_trail` because it runs **in parallel** with `post_executor_tools` (which appends to `action_trail`). Both use the `take_last` reducer — if both write, the result is non-deterministic. Whichever branch finishes last wins.

The Orchestrator's only trail-related responsibilities:
1. Set `pending_success_path_subgoal` when a subgoal completes
2. Set `action_trail_subgoal_id` to the new subgoal's ID (when starting a next subgoal)

The **trail reset** (`action_trail = []`) happens in the `convergence_node` after recording — never in the Orchestrator.

Update `_get_state_update` to accept extra fields:

```python
async def _get_state_update(
    ctx: MobileUseContext,
    state: State,
    thoughts: list[str],
    update_plan: bool = False,
    extra_update: dict | None = None,
):
    update = {
        "agents_thoughts": thoughts,
        "complete_subgoals_by_ids": [],
    }
    if update_plan:
        update["subgoal_plan"] = state.subgoal_plan
        if ctx.on_plan_changes:
            await ctx.on_plan_changes(state.subgoal_plan, False)
    if extra_update:
        update.update(extra_update)
    return await state.asanitize_update(ctx=ctx, update=update, agent="orchestrator")
```

In `OrchestratorNode.__call__()`, there are **6 return paths**. All call `_get_state_update`. Here is the `extra_update` for each:

**Path 1: First/next subgoal start (line 48-50)** — set new subgoal ID:
```python
trail_update = {}
if self.ctx.lessons_dir and new_subgoal:
    trail_update = {"action_trail_subgoal_id": new_subgoal.id}
return await _get_state_update(..., extra_update=trail_update)
```

**Path 2: No subgoals to examine (line 57-59)** — no trail changes (no subgoal transition):
```python
return await _get_state_update(...)
# extra_update omitted — defaults to None, no trail fields modified
```

**Path 3: Replanning/failure (line 91-93)** — no trail changes (subgoal failed, trail is discarded on next start):
```python
return await _get_state_update(...)
# extra_update omitted — the failed subgoal's trail will be
# overwritten when the Orchestrator starts the next subgoal (Path 1)
```

**Path 4: All completed (line 102-104)** — pending flag only:
```python
return await _get_state_update(..., extra_update=pending_update)
```

**Path 5: Current subgoal not yet complete (line 108-110)** — no trail changes (subgoal still running):
```python
return await _get_state_update(...)
# extra_update omitted — trail continues accumulating for this subgoal
```

**Path 6: Current completed, next subgoal starts (line 115-117)** — pending flag + new subgoal ID:
```python
trail_update = {"action_trail_subgoal_id": new_subgoal.id}
trail_update.update(pending_update)
return await _get_state_update(..., extra_update=trail_update)
```

### Step 4: Orchestrator sets pending flag on subgoal completion

In `OrchestratorNode.__call__()`, after `complete_subgoals_by_ids` (line 95-97):

```python
state.subgoal_plan = complete_subgoals_by_ids(
    subgoals=state.subgoal_plan,
    ids=response.completed_subgoal_ids,
)

# Signal convergence_node to record the success path.
# We cannot record here because post_executor_tools may still be
# appending to action_trail in the parallel Executor branch.
# We also cannot reset action_trail here — same race condition.
pending_update = {}
if self.ctx.lessons_dir:
    for subgoal_id in response.completed_subgoal_ids:
        if state.action_trail_subgoal_id == subgoal_id:
            completed_subgoal = next(
                (s for s in state.subgoal_plan if s.id == subgoal_id), None
            )
            if completed_subgoal:
                pending_update["pending_success_path_subgoal"] = (
                    completed_subgoal.description
                )
                break  # One subgoal per trail
```

Then include `pending_update` in `extra_update` for each return path (see Step 3 above).

**Why not record here?** `post_cortex_gate` returns `["review_subgoals", "execute_decisions"]` as a `Sequence`, which LangGraph fans out in parallel. The Executor → ToolNode → `post_executor_tools` branch appends the final actions to `action_trail` concurrently.

**Why not reset `action_trail` here?** Same parallel execution problem. If the Orchestrator writes `action_trail = []` while `post_executor_tools` writes `action_trail = [...entries...]`, the `take_last` reducer picks whichever branch finishes last — non-deterministic. The trail reset must happen in `convergence_node`, which runs AFTER both branches complete.

**Why check `action_trail_subgoal_id == subgoal_id`?** In multi-subgoal plans, the action trail belongs to a specific subgoal. If the Cortex completes multiple subgoals at once, we only flag the one whose trail we tracked.

### Step 5: `convergence_node` records success path and resets trail

The `convergence_node` is the **single point of recording**. It runs after both parallel branches (Orchestrator and Executor) have completed (`defer=True` ensures barrier semantics). This guarantees the action trail is complete.

No recording happens in the Contextor — `convergence_node` handles all cases:
- **"continue" case**: convergence_node records, resets trail, clears flag → Contextor runs next cycle with clean state
- **"end" case**: convergence_node records, graph terminates → path is captured before exit

```python
# In get_graph(), replace convergence_node with a closure that captures ctx:

async def _convergence_node(state: State):
    """Convergence point for parallel execution paths.
    Records success paths and resets the action trail."""
    if (
        ctx.lessons_dir
        and state.pending_success_path_subgoal
        and state.last_focused_app
        and state.action_trail
    ):
        try:
            from mineru.ui_auto.lessons.recorder import record_success_path

            await record_success_path(
                lessons_dir=ctx.lessons_dir,
                app_package=state.last_focused_app,
                subgoal_description=state.pending_success_path_subgoal,
                action_trail=state.action_trail,
            )
        except Exception as e:
            logger.warning(f"Failed to record success path: {e}")
        # Clear the flag AND reset the trail (for the next subgoal).
        # This is the ONLY place action_trail is reset — never in
        # the Orchestrator (which runs parallel with post_executor_tools).
        return {
            "pending_success_path_subgoal": None,
            "action_trail": [],
        }
    return {}

graph_builder.add_node(node="convergence", action=_convergence_node, defer=True)
```

**Why a closure?** `convergence_node` currently takes only `state`. Using a closure captures `ctx` from the enclosing `get_graph()` scope — no LangGraph signature change needed.

**Why `last_focused_app`?** The convergence node doesn't query the device. `last_focused_app` was set by the Contextor in the previous cycle and is not cleared by the Cortex.

**Why reset `action_trail` here?** This is the only safe place to reset the trail. The convergence_node runs after both parallel branches complete — no race condition with `post_executor_tools`. The Orchestrator already set `action_trail_subgoal_id` to the new subgoal's ID, so new trail entries from the next cycle will be correctly tagged.

---

## Wrong-Turn Elimination

### What Constitutes a "Back" Action

The `_build_trail_entry` function normalizes all back-navigation variants:

| Agent Action | Normalized To | Detection |
|-------------|---------------|-----------|
| `back()` tool | `"back"` | `name == "back"` |
| `press_key(key="Back")` | `"back"` | `name == "press_key" and args.key == "Back"` |

Swipes are **excluded from the trail entirely** (see `_build_trail_entry` — returns `None` for swipe actions). They represent scrolling, not navigation choices, so they never appear in the wrong-turn elimination input.

### Algorithm

```python
def _eliminate_wrong_turns(steps: list[PathStep]) -> list[PathStep]:
    """Remove wrong-turn pairs (forward action followed by back).

    Uses a stack approach: push forward actions, pop on back.
    Handles nested wrong turns naturally.

    Example:
      [tap A, tap B, back, tap C] → [tap A, tap C]
      [tap A, tap B, back, tap C, back] → stack trace:
        push A → push B → pop B (back) → push C → pop C (back) → result: [A]
    """
    stack: list[PathStep] = []
    for step in steps:
        if step.action == "back":
            if stack:
                stack.pop()  # Remove the preceding forward action
            # else: back with nothing to undo — discard silently
        else:
            stack.append(step)
    return stack
```

**Why stack instead of repeated scan?** The original design used a while-loop that repeatedly scanned for back-pairs. A stack processes in a single O(n) pass and handles nested wrong turns naturally (A → B → C → back → back removes both C and B).

### Only record actions with `status="success"`

Failed tool calls (element not found, app not launched) should not appear in the success path. Filter them out before wrong-turn elimination:

```python
# In record_success_path():
steps = [
    PathStep(
        action=entry["action"],
        target_text=entry.get("target_text"),
        target_resource_id=entry.get("target_resource_id"),
        result=entry.get("agent_thought", "")[:80],
    )
    for entry in action_trail
    if entry.get("status") == "success"  # Only successful actions
]
steps = _eliminate_wrong_turns(steps)
```

---

## New Recorder Function

```python
async def record_success_path(
    lessons_dir: Path,
    app_package: str,
    subgoal_description: str,
    action_trail: list[dict],
) -> None:
    """Record the successful navigation path for a completed subgoal.

    Reads from the structured action_trail (populated by post_executor_tools),
    filters to successful navigation actions, eliminates wrong turns, and
    stores the clean path. Only records paths with >= 2 steps.
    """
    # Convert trail entries to PathSteps (only successful actions)
    steps = [
        PathStep(
            action=entry["action"],
            target_text=entry.get("target_text"),
            target_resource_id=entry.get("target_resource_id"),
            result=(entry.get("agent_thought") or "")[:80],
        )
        for entry in action_trail
        if entry.get("status") == "success"
    ]

    steps = _eliminate_wrong_turns(steps)

    if len(steps) < 2:
        return  # Too trivial to record

    # Build compact summary from steps
    path_description = " → ".join(
        f"{s.action}('{s.target_text or s.target_resource_id or '?'}')"
        for s in steps
    )
    summary = f"{_truncate(subgoal_description, 50)}: {_truncate(path_description, 100)}"

    category = infer_category(subgoal_description, "success_path")
    lesson = LessonEntry(
        id=generate_lesson_id(category),
        type="success_path",
        category=category,
        summary=summary,
        context=LessonContext(
            goal=subgoal_description,
        ),
        lesson=f"Proven path for '{_truncate(subgoal_description, 60)}': {path_description}",
        suggested_strategy=f"Follow this path: {path_description}",
        path=steps,
        confidence=0.5,
        occurrences=1,
    )
    await record_lesson(lessons_dir, app_package, lesson)


def _truncate(s: str, max_len: int) -> str:
    """Truncate string to max_len, adding ellipsis if needed."""
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."


def _eliminate_wrong_turns(steps: list[PathStep]) -> list[PathStep]:
    """Remove wrong-turn pairs using a stack. O(n) single pass."""
    stack: list[PathStep] = []
    for step in steps:
        if step.action == "back":
            if stack:
                stack.pop()
        else:
            stack.append(step)
    return stack
```

---

## Loading: How Success Paths Appear in Cortex

### Formatting

**Replace the entire existing `format_lessons_text()` function** in `loader.py` with the following. The existing function has a single loop over `scored_lessons` that fills the budget linearly. This replacement adds the `success_path` group and a two-pass budget allocation to prevent type crowding:

```python
def format_lessons_text(scored_lessons: list[tuple[float, LessonEntry]]) -> str | None:
    """
    Format scored lessons into grouped bulleted text within TOKEN_BUDGET.
    Groups: mistakes -> success_paths -> strategies -> ui_mappings.
    Uses two-pass budget allocation to guarantee type diversity.
    Returns None if no lessons fit the budget.
    """
    groups: dict[str, list[str]] = {
        "mistake": [],
        "strategy": [],
        "success_path": [],   # NEW
        "ui_mapping": [],
    }
    running_chars = 0
    max_chars = TOKEN_BUDGET * APPROX_CHARS_PER_TOKEN  # ~2000 chars

    # --- Two-pass budget allocation ---
    # Pass 1: Reserve the top-scoring entry from each type (guarantees diversity).
    # Pass 2: Fill remaining budget with the rest, in scored order.

    reserved_by_type: dict[str, tuple[float, LessonEntry] | None] = {
        t: None for t in groups
    }

    rest: list[tuple[float, LessonEntry]] = []
    for item in scored_lessons:
        _score, lesson = item
        t = lesson.type
        if t in reserved_by_type and reserved_by_type[t] is None:
            reserved_by_type[t] = item
        else:
            rest.append(item)

    # Add reserved entries first (one per type that has lessons)
    for _score, lesson in (v for v in reserved_by_type.values() if v):
        bullet = _format_bullet(lesson)
        bullet_chars = len(bullet) + 2  # "- " prefix
        if running_chars + bullet_chars > max_chars:
            break
        groups.setdefault(lesson.type, []).append(bullet)
        running_chars += bullet_chars

    # Add remaining entries by score until budget exhausted
    for _score, lesson in rest:
        bullet = _format_bullet(lesson)
        bullet_chars = len(bullet) + 2
        if running_chars + bullet_chars > max_chars:
            break
        groups.setdefault(lesson.type, []).append(bullet)
        running_chars += bullet_chars

    # Build output with group headers
    sections = []
    if groups.get("mistake"):
        sections.append(
            "**Mistakes to avoid:**\n" + "\n".join(f"- {b}" for b in groups["mistake"])
        )
    if groups.get("success_path"):
        sections.append(
            "**Known navigation paths:**\n"
            + "\n".join(f"- {b}" for b in groups["success_path"])
        )
    if groups.get("strategy"):
        sections.append(
            "**Proven strategies:**\n" + "\n".join(f"- {b}" for b in groups["strategy"])
        )
    if groups.get("ui_mapping"):
        sections.append(
            "**UI mappings:**\n" + "\n".join(f"- {b}" for b in groups["ui_mapping"])
        )

    if not sections:
        return None
    return "\n\n".join(sections)
```

**Note:** `success_path` is rendered **between** mistakes and strategies — after "what not to do" and before "general strategies." This ordering gives the Cortex the most actionable info first: avoid X, then follow this specific path, then try these general approaches.

**Why reserved slots?** Without this, high-scoring success paths (or any single type) could fill the entire 500-token budget, leaving zero room for mistake or strategy lessons. The two-pass approach guarantees that the top lesson from each type is always included (if budget allows), then fills the remaining space by score. This prevents any single lesson type from crowding out the others.

### Bullet Format for Success Paths

**Replace the existing `_format_bullet()` function** in `loader.py` with the following (adds the `success_path` branch before the existing `ui_mapping` branch):

```python
def _format_bullet(lesson: LessonEntry) -> str:
    """Format a single lesson as a compact one-line bullet."""
    meta = f"[{lesson.id}, confidence: {lesson.confidence:.2f}, seen {lesson.occurrences}x]"
    if lesson.type == "success_path" and lesson.path:
        path_str = " → ".join(
            f"{s.action}('{s.target_text or s.target_resource_id or '?'}')"
            for s in lesson.path
        )
        return f"{lesson.context.goal}: {path_str} {meta}"
    if lesson.type == "ui_mapping":
        return f"{lesson.lesson} {meta}"
    return f"{lesson.summary}. {lesson.suggested_strategy} {meta}"
```

### Example Cortex Prompt Injection

```markdown
## Lessons Learned (com.android.settings)

**Mistakes to avoid:**
- Tap had no visible effect on screen (SubSettings). Check element state before tapping. [nav-a3f1, confidence: 0.60, seen 2x]

**Known navigation paths:**
- Change location sharing: tap('Location') → tap('Location sharing') [spa-f2a1, confidence: 0.75, seen 3x]

**Proven strategies:**
- Use search instead of scrolling through long lists [gen-b4c2, confidence: 0.80, seen 4x]
```

---

## Scoring Adjustments

In `scorer.py`, add handling for `success_path` type. Insert the following **after the existing rule 7** (the `ui_mapping` base boost block, which is the last rule in `score_lesson()`). The code references `subgoal_words`, `summary_words`, `overlap_ratio`, and `days_since_seen` — all computed earlier in the function by rules 2 and 5:

```python
    # 7b. success_path lessons get a conditional boost (only on strong subgoal match)
    if lesson.type == "success_path":
        # No unconditional base boost — success_path must earn its rank via
        # subgoal relevance. This prevents high-scoring success paths from
        # crowding out critical mistake lessons in the token budget.
        if subgoal_words and summary_words:
            if overlap_ratio > 0.5:
                score += 2.0  # Strong match — this path is very likely relevant

    # 7c. Staleness: success_paths are moderately fragile (UI can change)
    if lesson.type == "success_path" and days_since_seen > 45:
        score -= 1.5
```

**Rationale for conditional-only boost:** A success path is highly actionable when it matches the current subgoal, but should not blindly outrank mistake lessons. Without a base boost, a success path for an unrelated subgoal won't crowd out screen-matched mistakes (+3.0 from activity match). The +2.0 strong-match boost ensures relevant paths still rank highly when they matter.

---

## Staleness & Compaction

### Staleness Cutoff

| Type | Hard Cutoff (days) | Rationale |
|------|--------------------|-----------|
| `ui_mapping` | 30 | Most fragile — element IDs/positions change with app updates |
| `success_path` | 60 | **NEW** — navigation structure changes less often than element IDs, but still evolves |
| `mistake` | 90 | General knowledge, less tied to specific UI |
| `strategy` | 90 | General knowledge |

Add to `loader.py` (alongside existing `STALE_UI_MAPPING_DAYS` and `STALE_OTHER_DAYS`):
```python
STALE_SUCCESS_PATH_DAYS = 60
```

Update the staleness filter in **both** `load_lessons_for_app()` (loader.py) **and** `cleanup_stale_lessons()` (recorder.py):

```python
# Shared staleness filter logic — used in both locations:
if lesson.type == "ui_mapping" and days_old > STALE_UI_MAPPING_DAYS:
    continue
if lesson.type == "success_path" and days_old > STALE_SUCCESS_PATH_DAYS:
    continue
if lesson.type not in ("ui_mapping", "success_path") and days_old > STALE_OTHER_DAYS:
    continue
```

**Important:** `cleanup_stale_lessons()` in `recorder.py` already imports `STALE_OTHER_DAYS` and `STALE_UI_MAPPING_DAYS` from `loader.py`. Add `STALE_SUCCESS_PATH_DAYS` to that import:

```python
# In recorder.py cleanup_stale_lessons():
from mineru.ui_auto.lessons.loader import (
    STALE_OTHER_DAYS,
    STALE_SUCCESS_PATH_DAYS,  # NEW
    STALE_UI_MAPPING_DAYS,
    load_and_compact_lessons,
)
```

Without this, `success_path` lessons would fall through to the `STALE_OTHER_DAYS = 90` catch-all during disk cleanup, surviving 30 days longer than the intended 60-day cutoff.

### Compaction

Success paths use the same JSONL append + compact-on-read strategy. Two `success_path` entries with the same normalized goal text are merged — the newer path replaces the older one (navigation routes may change), while counters are summed.

**Dedup key for success_path:** The existing Pass 2 dedup in `load_and_compact_lessons` uses `_normalize_summary(lesson.summary)` as the merge key. This works for mistake/strategy/ui_mapping because their summaries are stable descriptions. But `success_path` summaries include the path description (e.g., `"Change location sharing: tap('Location') → tap('Location sharing')"`), which changes when the agent takes a different route. Two success paths for the **same goal** via **different routes** would never merge, causing unbounded accumulation.

**Fix:** Use `_normalize_summary(lesson.context.goal)` as the dedup key for `success_path` type instead of the summary. In `load_and_compact_lessons()`, find the existing Pass 2 loop (the `by_text` dict loop that calls `_normalize_summary(lesson.summary)`) and replace it:

**Existing code to replace:**
```python
# Pass 2: Merge by normalized text (independent duplicates)
by_text: dict[str, LessonEntry] = {}
for lesson in merged:
    key = _normalize_summary(lesson.summary)
    if key in by_text:
        _merge_into(by_text[key], lesson)
    else:
        by_text[key] = lesson
```

**Replace with:**
```python
# Pass 2: Merge by normalized text (independent duplicates)
by_text: dict[str, LessonEntry] = {}
for lesson in merged:
    # For success_path, dedup by goal (summary includes variable path description).
    # For all other types, dedup by summary (stable descriptions).
    if lesson.type == "success_path" and lesson.context.goal:
        key = _normalize_summary(lesson.context.goal)
    else:
        key = _normalize_summary(lesson.summary)
    if key in by_text:
        _merge_into(by_text[key], lesson)
    else:
        by_text[key] = lesson
```

Merge rule in `_merge_into` — the path override must happen **before** `last_seen` is updated to `max()`:

```python
def _merge_into(existing: LessonEntry, incoming: LessonEntry) -> None:
    """Merge incoming lesson data into existing. Mutates existing in place."""
    # Determine which entry is newer BEFORE mutating last_seen.
    incoming_is_newer = incoming.last_seen > existing.last_seen

    # For success_path: keep the NEWER path (UI may have changed).
    if existing.type == "success_path" and incoming.path is not None:
        if incoming_is_newer:
            existing.path = incoming.path
            existing.lesson = incoming.lesson
            existing.suggested_strategy = incoming.suggested_strategy

    # Keep the newer ID
    if incoming_is_newer:
        existing.id = incoming.id

    existing.occurrences += incoming.occurrences
    existing.applied_success += incoming.applied_success
    existing.applied_failure += incoming.applied_failure
    existing.last_seen = max(existing.last_seen, incoming.last_seen)

    # Recompute confidence
    total = existing.applied_success + existing.applied_failure
    existing.confidence = existing.applied_success / total if total > 0 else 0.5

    # Deprecation is sticky
    if incoming.deprecated:
        existing.deprecated = True
```

**Note on the existing `_merge_into` bug:** The original code updates `existing.last_seen = max(...)` first, then checks `if incoming.last_seen >= existing.last_seen:` to decide the newer ID. After the max assignment, `existing.last_seen` is already the max, making that check unreliable (always `True` for `>=`). The fix above captures `incoming_is_newer` into a boolean **before** any mutations, then uses it for both the path override and the ID update. This is a pre-existing bug that should be fixed regardless of this enhancement.

**Behavioral impact of the fix:** Previously, the ID was always overwritten to the latest-appended entry. After the fix, IDs are only updated when the incoming entry is genuinely newer by `last_seen` timestamp. This is correct behavior — the Cortex references lesson IDs in `applied_lesson_ids` and `failed_lesson_ids` for feedback. After compaction, the surviving ID must be the one the Cortex most recently saw (i.e., the newest). Since `last_seen` is set at recording time and newer entries have later timestamps, `incoming_is_newer` correctly identifies the ID the Cortex would reference. No feedback lookup breakage.

---

## Integration Points

### 1. State (`graph/state.py`)

Add 4 new fields:

```python
# lesson-learned: success path recording
action_trail: Annotated[
    list[dict], "Accumulated tool calls for current subgoal", take_last
] = []
action_trail_subgoal_id: Annotated[
    str | None, "Subgoal ID the current action_trail belongs to", take_last
] = None
last_focused_app: Annotated[
    str | None, "Last known focused_app_info (survives Cortex nulling)", take_last
] = None
pending_success_path_subgoal: Annotated[
    str | None, "Subgoal description to record on next Contextor cycle", take_last
] = None
```

### 2. Contextor (`agents/contextor/contextor.py`)

Add `last_focused_app` to the return update:
```python
"last_focused_app": current_app_package,  # NEW: survives Cortex nulling
```

No recording logic in the Contextor — `convergence_node` handles all recording.

### 3. Graph (`graph/graph.py`)

Two changes:

**a)** Update `post_executor_tools_node` to append to the action trail (see "Step 2" above).

**b)** Replace `convergence_node` with a closure that records success paths and resets the trail (see "Step 5" above). This is the **single point of recording** — it handles both "continue" and "end" cases because it runs before `convergence_gate` decides.

### 4. Orchestrator (`agents/orchestrator/orchestrator.py`)

- Set `action_trail_subgoal_id` when starting a new subgoal (does NOT reset `action_trail` — convergence_node does that)
- Set `pending_success_path_subgoal` flag on subgoal completion (does NOT record directly — parallel execution with Executor)
- Update `_get_state_update` to accept `extra_update` dict

(See "Step 3" and "Step 4" above for full code.)

### 5. Recorder (`lessons/recorder.py`)

- Add `record_success_path()` and `_eliminate_wrong_turns()` (see "New Recorder Function" above)
- Update `cleanup_stale_lessons()` to import `STALE_SUCCESS_PATH_DAYS` from loader and add `success_path` staleness check

### 6. Loader (`lessons/loader.py`)

- Add `STALE_SUCCESS_PATH_DAYS = 60`
- Update `format_lessons_text()`: add `"success_path": []` to groups dict, add reserved-slot two-pass budget allocation, add `success_path` rendering section
- Update `_format_bullet()` for `success_path` type
- Update staleness filter in `load_lessons_for_app()`
- Fix `_merge_into` ordering bug (capture `incoming_is_newer` before mutations)
- Update Pass 2 dedup in `load_and_compact_lessons`: use `context.goal` as dedup key for `success_path` type (summary includes variable path text)

### 7. Scorer (`scorer.py`)

- Add `success_path` conditional scoring: +2.0 on strong subgoal match only (no unconditional base boost — prevents crowding out mistakes)
- Add staleness penalty for `success_path` after 45 days

### 8. Types (`lessons/types.py`)

- Add `PathStep` model (fields: `action`, `target_text`, `target_resource_id`, `result` — no `screen` field)
- Add `path: list[PathStep] | None = None` to `LessonEntry`

### 9. Cortex prompt (`agents/cortex/cortex.md`)

Update the Lessons Learned section description (lines 125-131 of cortex.md). **Preserve the existing feedback reporting instructions** (`applied_lesson`/`lesson_failed`) — only add the "Known navigation paths" bullet:

```markdown
## Lessons Learned ({{ focused_app }})

The following lessons were recorded from previous sessions with this app:
- **Known navigation paths** show proven routes to achieve goals — follow them when available
- **Mistakes to avoid** describe actions that failed before
- **Proven strategies** describe approaches that worked

If you follow a lesson's suggested strategy, you may optionally include `applied_lesson: "<lesson_id>"` in your `decisions_reason`. If a lesson's strategy does not work, include `lesson_failed: "<lesson_id>"` so it can be updated.

{{ active_lessons }}
```

**Important:** The feedback instructions (applied_lesson/lesson_failed) are critical for the Phase 2 confidence feedback loop. Do NOT remove them — only add the new "Known navigation paths" bullet to the existing description list.

---

## What This Design Does NOT Do (Intentional Scope Limits)

1. **Does not record every successful tap** — only navigation paths for completed subgoals with >= 2 steps. This prevents storage bloat.
2. **Does not use LLM to summarize paths** — uses structured tool call data from `executor_messages`. Zero LLM cost at recording time.
3. **Does not create a graph/tree of app navigation** — each path is an independent lesson. Simpler, works with existing JSONL model, and doesn't require a new storage format.
4. **Does not replace the agent's reasoning** — the path is a suggestion in the Cortex prompt, not a forced sequence. The agent still verifies each step against the current screen.
5. **Does not record swipes** — swipes represent scrolling, not navigation choices. Including them would pollute paths with noise (e.g., 3 scroll-down swipes before tapping an element). The tap after the scroll IS the meaningful step.
6. **Does not treat swipe-as-back** — only explicit `back()` and `press_key(Back)` are treated as back actions for wrong-turn elimination.

---

## Issues Addressed from Review

This design addresses all issues identified across both review rounds:

**Round 1 — Original 7 breakage issues:**

| # | Issue | How Fixed |
|---|-------|-----------|
| 1 | `focused_app_info` is NULL in Orchestrator (Cortex clears it) | New `last_focused_app` state field set by Contextor, not cleared by Cortex. Contextor uses fresh `current_app_package` directly. |
| 2 | `agents_thoughts` has no subgoal boundaries | Replaced with `action_trail` + `action_trail_subgoal_id` — trail is reset per subgoal |
| 3 | `back` detection is incomplete (press_key, swipe) | `_build_trail_entry` normalizes `press_key(Back)` and `back()` to `"back"`. Swipe excluded entirely (not navigation). |
| 4 | Cortex reason parsing is fragile (free-form text) | Eliminated — uses structured `AIMessage.tool_calls` args (`target.text`, `target.resource_id`) from executor, not Cortex text |
| 5 | `format_lessons_text` silently drops `success_path` | Design includes explicit `groups["success_path"]` init + rendering section + `_format_bullet` handler |
| 6 | `_merge_into` comparison bug (always overwrites) | `incoming_is_newer` captured BEFORE any mutations. Used for both path override and ID update. |
| 7 | Multi-subgoal completion — can't map trail to subgoal | `action_trail_subgoal_id` tracks which subgoal the trail belongs to. Only records if IDs match |

**Round 2 — Additional issues found:**

| # | Issue | How Fixed |
|---|-------|-----------|
| A | Orchestrator and Executor run in parallel — trail incomplete when recording | Recording moved to **convergence_node** (runs after both parallel branches complete, `defer=True`). Orchestrator only sets `pending_success_path_subgoal` flag and `action_trail_subgoal_id`. Trail reset also happens in convergence_node (not Orchestrator) to avoid `take_last` race condition on `action_trail`. |
| C | `_merge_into` ID update is dead code after `last_seen = max()` | `incoming_is_newer` boolean captured before mutations, used consistently for both path and ID updates |
| E | Swipes pollute trail with noise | Swipes excluded from `_build_trail_entry` entirely (`return None`). Taps/launch_app/open_link/back are the meaningful navigation steps. |

**Round 3 — Workflow breakage analysis:**

| # | Issue | How Fixed |
|---|-------|-----------|
| F | `_merge_into` bug fix changes ID-overwrite behavior — could break feedback lookups | Verified safe: `incoming_is_newer` picks the entry with the latest `last_seen`, which matches the ID the Cortex most recently referenced in `applied_lesson_ids`. Added behavioral impact note to design. |
| G | Token budget competition — success_path base boost (+1.5) crowds out mistake lessons | Removed unconditional base boost. Success paths now earn rank only via subgoal overlap (+2.0 conditional). Added reserved-slot mechanism in `format_lessons_text`: Pass 1 reserves top entry per type, Pass 2 fills remaining budget by score. |
| H | `cleanup_stale_lessons` in `recorder.py` doesn't know about `STALE_SUCCESS_PATH_DAYS` — success_path entries persist 30 days past intended cutoff | Added `STALE_SUCCESS_PATH_DAYS` to the import in `cleanup_stale_lessons()` and updated its filter logic. Both `loader.load_lessons_for_app()` and `recorder.cleanup_stale_lessons()` now use the same 60-day cutoff. |
| I | Pass 2 compaction dedup uses `_normalize_summary()` — success_path summaries include variable path text, so same-goal/different-route paths never merge (unbounded accumulation) | For `success_path` type, dedup key is `_normalize_summary(lesson.context.goal)` instead of summary. Other types unchanged. |
| J | `PathStep.screen` field is never populated — dead schema, misleading example JSON | Removed `screen` field from `PathStep` model and example JSON. The action trail doesn't have access to per-step screen/activity info, so the field would always be `null`. |

**Round 4 — Implementation clarity gaps:**

| # | Gap | How Fixed |
|---|-----|-----------|
| K | Recording flow diagram shows Contextor as recording point (stale from pre-Round-2) | Diagram and surrounding text updated: `convergence_node` is the single recording point. Contextor box now says "(no recording logic here)". Diagram shows convergence_node recording and resetting trail. |
| L | Cortex prompt replacement silently drops `applied_lesson`/`lesson_failed` feedback instructions | Preserved existing feedback instructions. Design now explicitly says "only add the new bullet, do NOT remove feedback instructions." |
| M | Only 3 of 6 orchestrator return paths specified for `extra_update` | All 6 paths now listed with explicit `extra_update` handling: Paths 2, 3, 5 omit `extra_update` (defaults to `None`) with rationale for each. |
| N | `format_lessons_text` shown as snippet, not complete replacement | Now shows complete replacement function with `def`, return type, docstring, and `if not sections: return None`. Explicit instruction: "Replace the entire existing function." |
| O | `load_and_compact_lessons` Pass 2 dedup change not anchored to existing code | Now shows old→new diff: existing code block to find, then replacement code block. |
| P | `post_executor_tools_node` has no replace instruction; `_build_trail_entry` placement unspecified | Added: "Replace existing function at line 100 in graph.py" and "Add `_build_trail_entry` immediately after, both module-level." |
| Q | Scorer insertion point ambiguous for `success_path` rules | Added: "Insert after existing rule 7 (ui_mapping base boost)." Noted variable dependencies on rules 2 and 5. |
| R | `_truncate` helper used in `record_success_path` but not defined anywhere | Added `_truncate()` function definition in the New Recorder Function section, placed between `record_success_path` and `_eliminate_wrong_turns`. |
| S | `_format_bullet` shown as complete function but no explicit "replace" instruction | Added: "Replace the existing `_format_bullet()` function" to match the pattern used for other full-replacement functions. |

---

## File Changes Summary

| File | Change |
|------|--------|
| `graph/state.py` | Add `action_trail`, `action_trail_subgoal_id`, `last_focused_app`, `pending_success_path_subgoal` fields |
| `graph/graph.py` | Update `post_executor_tools_node` to append to `action_trail` via `_build_trail_entry`. Replace `convergence_node` with closure that records success paths and resets trail. |
| `agents/contextor/contextor.py` | Add `last_focused_app` to return update |
| `agents/orchestrator/orchestrator.py` | Set `action_trail_subgoal_id` on new subgoal (no trail reset). Set `pending_success_path_subgoal` flag on completion (no direct recording). Update `_get_state_update` signature. |
| `lessons/types.py` | Add `PathStep` model (no `screen` field — not available at trail capture time), add `path` field to `LessonEntry` |
| `lessons/recorder.py` | Add `record_success_path()`, `_eliminate_wrong_turns()`. Update `cleanup_stale_lessons()` to import and use `STALE_SUCCESS_PATH_DAYS`. |
| `lessons/loader.py` | Add `STALE_SUCCESS_PATH_DAYS = 60`. Update `format_lessons_text()` with reserved-slot two-pass budget allocation + `success_path` group + rendering. Update `_format_bullet()` for `success_path` type. Fix `_merge_into` ordering with `incoming_is_newer`. Update staleness filters. Update Pass 2 dedup in `load_and_compact_lessons` to use `context.goal` as key for `success_path` type. |
| `lessons/scorer.py` | Add `success_path` conditional scoring: +2.0 on strong subgoal match (no unconditional base boost), -1.5 staleness after 45 days |
| `agents/cortex/cortex.md` | Update Lessons Learned section description |

No new files. No new dependencies. Fully backward compatible — existing JSONL files work unchanged since `path` defaults to `None`.
