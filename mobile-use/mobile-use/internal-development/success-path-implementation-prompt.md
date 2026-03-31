# Resume Prompt: Implement Success Path Recording Enhancement

Copy everything below the line into your next Claude Code session.

---

## Task

Implement the success path recording enhancement for the lesson-learned memory system. This adds the positive-reinforcement half — recording successful navigation paths on subgoal completion so the agent can follow proven routes in future runs.

## Context

- **Design doc**: `mobile-use/internal-development/lesson-learned-success-path-design.md` — read this FIRST. It contains all specs, complete code snippets, old→new diffs, architectural decisions, and 4 rounds of reviewed issues (A-S).
- **Existing lesson system**: Phase 1-2 are already implemented. The lesson module lives at `mineru/ui_auto/lessons/` with `types.py`, `recorder.py`, `loader.py`, `scorer.py`. The graph, contextor, orchestrator, and cortex already have lesson-learned integration.
- All changes are non-breaking and backward compatible. New behavior is gated behind `ctx.lessons_dir`. Existing JSONL files work unchanged (`path` defaults to `None`).

## What to implement

### Files to modify (9 files, 0 new files):

**1. `mineru/ui_auto/lessons/types.py`**
- Add `PathStep` model (fields: `action`, `target_text`, `target_resource_id`, `result` — NO `screen` field)
- Add `path: list[PathStep] | None = None` to `LessonEntry`

**2. `mineru/ui_auto/graph/state.py`**
- Add 4 new state fields (all `take_last` reducer, all with defaults):
  - `action_trail: list[dict] = []`
  - `action_trail_subgoal_id: str | None = None`
  - `last_focused_app: str | None = None`
  - `pending_success_path_subgoal: str | None = None`

**3. `mineru/ui_auto/agents/contextor/contextor.py`**
- Add `"last_focused_app": current_app_package` to the return update dict (unconditional, not gated behind lessons_dir)

**4. `mineru/ui_auto/graph/graph.py`** — Three changes:
- **a)** Replace the existing `post_executor_tools_node` function (module-level, line 100) with the new version that appends to `action_trail`
- **b)** Add new `_build_trail_entry` helper function immediately after `post_executor_tools_node` (module-level)
- **c)** Replace the existing module-level `convergence_node` function (line 34, currently returns `{}`) with a closure `_convergence_node` defined inside `get_graph()`. The closure captures `ctx` and: records success paths when `pending_success_path_subgoal` is set, then resets `action_trail` and clears the flag. Update the `add_node` call accordingly.

**5. `mineru/ui_auto/agents/orchestrator/orchestrator.py`** — Three changes:
- **a)** Add `extra_update: dict | None = None` parameter to `_get_state_update` (default `None`, merges into update dict before `asanitize_update`)
- **b)** Add `pending_update` logic after `complete_subgoals_by_ids` call (~line 95-98): loop `response.completed_subgoal_ids`, check `action_trail_subgoal_id` match, set `pending_success_path_subgoal` flag
- **c)** Update all 6 return paths to pass `extra_update`:
  - Path 1 (line 48-50, first subgoal start): `extra_update={"action_trail_subgoal_id": new_subgoal.id}` if lessons_dir set
  - Path 2 (line 57-59, no subgoals): omit `extra_update`
  - Path 3 (line 91-93, replanning): omit `extra_update`
  - Path 4 (line 102-104, all completed): `extra_update=pending_update`
  - Path 5 (line 108-110, not yet complete): omit `extra_update`
  - Path 6 (line 115-117, completed + next): `extra_update={**pending_update, "action_trail_subgoal_id": new_subgoal.id}`

**6. `mineru/ui_auto/lessons/recorder.py`** — Two additions + one modification:
- Add `record_success_path()` async function
- Add `_truncate()` helper function
- Add `_eliminate_wrong_turns()` function
- Modify `cleanup_stale_lessons()`: add `STALE_SUCCESS_PATH_DAYS` to import from loader, add `success_path` type check in staleness filter

**7. `mineru/ui_auto/lessons/loader.py`** — Six changes:
- Add `STALE_SUCCESS_PATH_DAYS = 60` constant
- Replace `format_lessons_text()` entirely with two-pass budget allocation version (reserved-slot mechanism for type diversity)
- Replace `_format_bullet()` with version that handles `success_path` type
- Replace `_merge_into()` with version using `incoming_is_newer` (fixes pre-existing bug + adds path merge)
- Update staleness filter in `load_lessons_for_app()` to add `success_path` check
- Update Pass 2 dedup in `load_and_compact_lessons()`: use `context.goal` as dedup key for `success_path` type (NOT summary, which includes variable path text)

**8. `mineru/ui_auto/lessons/scorer.py`**
- Add rules 7b and 7c after existing rule 7 (ui_mapping base boost):
  - 7b: `success_path` +2.0 conditional boost on strong subgoal match (>50% word overlap). NO unconditional base boost.
  - 7c: `success_path` -1.5 staleness penalty after 45 days

**9. `mineru/ui_auto/agents/cortex/cortex.md`**
- In the Lessons Learned section (lines 125-131): add "Known navigation paths" bullet to the description list
- **PRESERVE the existing `applied_lesson`/`lesson_failed` feedback instructions** — do NOT remove them

### DO NOT modify:
- `agents/executor/tool_node.py`
- `agents/cortex/types.py` (CortexOutput)
- Any tool file in `tools/mobile/`
- `convergence_gate` logic in `graph/graph.py` (only replace `convergence_node`)

## Key architectural rules

1. **Recording happens in `convergence_node` only** — NOT in Orchestrator (parallel execution race) or Contextor. The convergence_node runs after both parallel branches merge (`defer=True`), guaranteeing the action trail is complete.
2. **Orchestrator never writes `action_trail`** — it only writes `pending_success_path_subgoal` and `action_trail_subgoal_id`. The trail is appended by `post_executor_tools` and reset by `convergence_node`. Both branches use `take_last` reducer — dual writers cause non-determinism.
3. **Swipes excluded from trail** — `_build_trail_entry` returns `None` for swipe/swipe_percentages. They represent scrolling, not navigation choices.
4. **Wrong-turn elimination** — stack-based O(n): push forward actions, pop on back. Applied AFTER filtering to `status="success"` only.
5. **Minimum 2 steps** to record — paths with < 2 navigation steps are too trivial.
6. **Dedup by `context.goal` for success_path** — NOT by summary (which includes variable path text). Other types still dedup by summary.
7. **No unconditional scorer boost** — success_path earns rank via subgoal relevance only (+2.0 conditional). Reserved-slot mechanism in `format_lessons_text` guarantees type diversity in token budget.
8. **`_merge_into` ordering** — capture `incoming_is_newer = incoming.last_seen > existing.last_seen` BEFORE any mutations. Use it for both path override and ID update.

## Implementation order

1. Start with `types.py` (add PathStep + path field — no risk, purely additive)
2. Then `state.py` (add 4 fields — no risk, all have defaults)
3. Then `contextor.py` (one-liner — add `last_focused_app`)
4. Then `recorder.py` (add 3 new functions + modify cleanup — isolated, no graph changes)
5. Then `loader.py` (6 changes — most complex file, but all self-contained)
6. Then `scorer.py` (add 2 rules — simple append)
7. Then `graph.py` (3 changes — `post_executor_tools_node`, `_build_trail_entry`, `convergence_node` closure)
8. Then `orchestrator.py` (3 changes — `_get_state_update` signature, pending flag logic, 6 return paths)
9. Finally `cortex.md` (add one bullet — preserve feedback instructions)

## Verification after implementation

1. **Backward compat**: Run with `lessons_dir=None` — confirm zero behavior change, no errors
2. **No-op when no completion**: Run a single-subgoal task that doesn't complete — confirm `action_trail` accumulates but no success_path is recorded
3. **Recording on completion**: Run a multi-step task to completion — confirm `lessons.jsonl` gets a `success_path` entry with clean path (wrong turns eliminated)
4. **Loading**: Run again on the same app — confirm success_path appears under "Known navigation paths" in Cortex prompt
5. **Dedup**: Record same goal twice via different routes — confirm compaction merges them by goal (not summary)
6. **Budget diversity**: With many lessons of mixed types — confirm reserved-slot mechanism includes at least one of each type
7. **Existing tests**: Run existing lesson-learned tests to confirm no regressions
8. **Grep check**: Verify all `record_success_path` calls are wrapped in try/except (the convergence_node call should be)
