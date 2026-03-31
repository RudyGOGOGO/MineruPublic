# Resume Prompt: Implement Lesson-Learned Memory System

Copy everything below the line into your next Claude Code session.

---

## Task

Implement Phase 1 of the lesson-learned memory system for the mobile UI automation agent. The design doc is fully reviewed, risk-assessed, and ready for implementation.

## Context

- **Design doc**: `mobile-use/internal-development/lesson-learned-memory-design.md` — read this first, it contains all specs, code snippets, and architectural decisions
- **Risk mitigation plan**: `.claude/plans/eventual-weaving-riddle.md` — explains why certain approaches were chosen over alternatives
- The existing agent workflow is stable. All changes must be non-breaking. Every lesson code path is gated behind `ctx.lessons_dir is not None` (no-op when disabled). Every recorder call in existing nodes (Contextor, Planner) must be wrapped in try/except.

## What to implement (Phase 1 only)

### New files to create:
1. `mineru/ui_auto/lessons/__init__.py` — empty
2. `mineru/ui_auto/lessons/types.py` — Pydantic models: `LessonEntry`, `LessonContext`, `ScreenSignature`, `AppIndexEntry`, `AppIndex`
3. `mineru/ui_auto/lessons/scorer.py` — `score_lesson()`
4. `mineru/ui_auto/lessons/loader.py` — `load_lessons_for_app()`, `load_and_compact_lessons()`, `format_lessons_text()`, `_format_bullet()`, `_normalize_summary()`, `_merge_into()`
5. `mineru/ui_auto/lessons/recorder.py` — `record_lesson()`, `record_no_effect_mistake()` (with session-local dedup counter), `record_mistake_from_tool_failure()`, `record_subgoal_failure()`, `generate_lesson_id()`, `infer_category()`, `capture_screen_signature()`, `_truncate()`, `_update_index_if_needed()`, `_rewrite_compacted()`, `_atomic_write_json()`

### Existing files to modify:
6. `mineru/ui_auto/graph/state.py` — Add 5 Phase 1 fields: `active_lessons`, `screen_changed` (bool|None=None), `previous_screenshot_hash`, `last_tool_name`, `last_tool_status`
7. `mineru/ui_auto/context.py` — Add `lessons_dir: Path | None = None` to `MobileUseContext`
8. `mineru/ui_auto/graph/graph.py` — Add `post_executor_tools_node` function + register node + rewire edge: `executor_tools → post_executor_tools → summarizer`
9. `mineru/ui_auto/agents/contextor/contextor.py` — Add gated screen change detection (ahash + base64 length pre-filter), lesson loading, no-effect mistake recording, tool failure mistake recording
10. `mineru/ui_auto/agents/cortex/cortex.md` — Add Phase 1 `{% if active_lessons %}` section (read-only, NO feedback instructions)
11. `mineru/ui_auto/agents/cortex/cortex.py` — Pass `focused_app=state.focused_app_info` and `active_lessons=state.active_lessons` to template render; reset `last_tool_name`/`last_tool_status` to None in return update
12. `mineru/ui_auto/agents/planner/planner.py` — Add gated subgoal failure lesson recording on replan
13. `pyproject.toml` — Add dependencies: `imagehash>=4.3.1`, `Pillow>=10.0.0`, `aiofiles>=23.0.0`

### DO NOT modify:
- `agents/executor/tool_node.py`
- `agents/cortex/types.py` (CortexOutput)
- `convergence_node` or `convergence_gate` in `graph/graph.py`
- Any tool file in `tools/mobile/`

## Key architectural rules

1. **Gate everything**: All lesson code paths check `self.ctx.lessons_dir is not None` before doing any work
2. **Defensive error handling**: All recorder calls in Contextor and Planner wrapped in `try/except Exception` with `logger.warning`
3. **screen_changed is `bool | None = None`**, not `bool = True`. Guards use `screen_changed is False` (explicit), never `not screen_changed`
4. **post_executor_tools node** reads `executor_messages` for last tool name/status — don't modify tool_node.py
5. **Subgoal failure recording** happens in Planner on replan — don't modify convergence_node
6. **Screenshot hashing** uses `average_hash` (not phash), with base64 length pre-filter, lazy import of imagehash/PIL
7. **Cortex prompt Phase 1** is read-only — no applied_lesson/lesson_failed feedback instructions
8. **Tap-no-effect** requires 2+ occurrences per screen per session before recording (session-local counter)
9. **Two-tier dedup on read**: merge by ID first (handles feedback updates), then by normalized summary (handles independent duplicates)

## Implementation order

Start with items 1-5 (new files, no risk), then 6-7 (state + context), then 8 (graph.py — the only topology change), then 9-12 (node modifications), then 13 (deps).

## Verification after implementation

1. Run the agent with `lessons_dir=None` (default) — confirm zero behavior change, no errors
2. Run the agent with `lessons_dir=Path("./lessons")` on a simple task — confirm lessons.jsonl gets created on tool failures / tap-no-effect
3. Run again on the same app — confirm lessons are loaded and appear in Cortex prompt
4. Check that `post_executor_tools` node doesn't add latency (should be <1ms)
5. Grep for any recorder call NOT wrapped in try/except inside Contextor or Planner — fix if found
