# Handoff: App Self-Learning Design Review — Continue from Round 2

## Context for Next Session

Read this file first, then proceed to the design doc for review.

---

## What Exists

### Lesson-Learned Memory (Complete — Shipped)

The foundation that self-learning builds on. Fully implemented, CLI-wired, device-tested.

- **Code:** `mineru/ui_auto/lessons/` (types.py, recorder.py, loader.py, scorer.py)
- **Graph integration:** `mineru/ui_auto/graph/graph.py` (action trail capture in `post_executor_tools_node`, success path recording in `convergence_node` closure)
- **Orchestrator:** `mineru/ui_auto/agents/orchestrator/orchestrator.py` (sets `pending_success_path_subgoal` flag + `action_trail_subgoal_id` on subgoal completion)
- **Contextor:** `mineru/ui_auto/agents/contextor/contextor.py` (screen change detection, lesson loading, mistake/strategy recording, feedback processing, `last_focused_app`)
- **State:** `mineru/ui_auto/graph/state.py` (4 success-path fields: `action_trail`, `action_trail_subgoal_id`, `last_focused_app`, `pending_success_path_subgoal`)
- **Cortex prompt:** `mineru/ui_auto/agents/cortex/cortex.md` (Lessons Learned section with "Known navigation paths")
- **CLI:** `--lessons-dir` flag + `MOBILE_USE_LESSONS_DIR` env var, wired through `AgentConfig` → `AgentConfigBuilder` → `Agent` → `main.py`
- **Design docs:** `lesson-learned-memory-design.md`, `lesson-learned-success-path-design.md`
- **Viewer:** `scripts/view_lessons.py` (summary + `--raw` modes)

### Self-Learning Design Doc (In Review)

**File:** `mobile-use/internal-development/app-self-learning-design.md`

An automated goal generation loop around the existing agent graph. The agent systematically explores an app's features across multiple sessions, building a persistent feature tree and recording lessons (success paths, mistakes, strategies) via the existing lesson-learned system. Zero changes to the lesson system — self-learning is just automated goal generation; everything downstream is reused.

---

## Review History

### Round 1 — 4 Critical Issues (All Addressed)

| # | Issue | Fix in Design Doc |
|---|-------|-------------------|
| 1 | **Dynamic content explosion** — text-based `_make_id` treats every email/video/transaction as a unique node, causing exponential tree growth | Structural screen fingerprinting (`_make_structural_id` using activity + className + position quadrant). `_detect_list_containers()` identifies RecyclerView/ListView, samples 2 items, marks rest `skipped_redundant`. |
| 2 | **Read-only safety is prompt-only** — LLM can drift and ignore "do not change settings" during long tool-use loops | Tool-level hard enforcement. `exploration_mode` flag on `MobileUseContext`, `EXPLORATION_BLOCKED_ACTIONS` checked in tap/press_key tool wrappers, returns `ACTION_BLOCKED` system error. Three layers: prompt (soft) + discovery filter (medium) + tool guard (hard). |
| 3 | **Fragile home reset** — `navigate_to_app_home()` fails on modals, deep WebViews, system dialogs | Verify-then-force fallback. 3 back-presses with home signature verification → fallback to `am force-stop` + cold relaunch via `monkey`. Home signature captured during `initialize_exploration()`. |
| 4 | **Modals corrupt feature tree** — bottom sheets/popups create merged UI hierarchies misread as full-screen children | `classify_screen_transition()` compares parent vs current bounding boxes. Coverage <80% = modal. `FeatureNode` extended with `node_type` ("screen"/"modal") and `dismiss_action`. Modal-only elements extracted for child discovery. |

All 4 fixes are incorporated into the design doc with complete code snippets, rationale, and tree examples.

---

## Round 2 Review — Suggested Focus Areas

These are open questions and potential gaps not yet addressed in the design:

### 1. Goal Generation Quality
The current design uses template-based goals: `"Navigate to {path}. Observe all options. Do not change settings."` This is simple but possibly too generic — the agent might not know *what* to look for on a complex screen. Questions:
- Is template-based sufficient for MVP, or does the agent need per-screen context?
- Should the goal include what the parent screen looked like (so the agent can verify it arrived at the right place)?
- Should goals for list-item samples be different from goals for menu items?

### 2. Convergence / Completion Criteria
The design says exploration stops when "all reachable nodes explored" or budget exhausted. But:
- How does the user know the exploration is "good enough" without exploring every node?
- Should there be a coverage threshold (e.g., "80% of top-2-levels explored = sufficient")?
- What about screens that change dynamically (notifications, time-dependent content)?

### 3. Re-Exploration After App Update
The design mentions staleness eviction in the lesson system (30/60/90 days) but doesn't address:
- How to detect that the app was updated (version comparison via `_meta.json`?)
- Which tree nodes to invalidate on update (all? only leaf nodes? only failed ones?)
- Should re-exploration be automatic or user-triggered?

### 4. Alternative Route Discovery
Current design: if a node fails 3 times, mark it `failed`. But:
- The node might be reachable via a different path (e.g., search, deep link, different parent)
- Should the planner try alternative routes before giving up?
- How to avoid infinite retry loops if the screen genuinely doesn't exist?

### 5. Cross-Session State Consistency
If the app's UI changes between sessions (A/B test, feature flag, locale change):
- The tree may contain nodes that no longer exist on screen
- Home screen signature may not match
- How to detect stale tree branches vs. temporary UI differences?

### 6. Interaction with Enhanced Perception Mode
The design doesn't mention enhanced perception (OCR + SoM). Questions:
- Should self-learning always use enhanced mode for better element discovery?
- Does OCR-detected text interact with `_make_structural_id` (OCR elements don't have className)?
- Should the feature tree store which perception mode discovered each node?

### 7. Estimated LLM Cost
Each exploration goal runs the full agent graph (Planner + Cortex + Executor). For a medium app (40-80 nodes):
- ~50 goals × ~5 Cortex cycles each × ~2k tokens/cycle = ~500k tokens
- Is this acceptable? Should there be a token budget alongside the time budget?
- Could some exploration be done without the full agent (just UI hierarchy crawling)?

---

## How to Start the Next Session

```
Read mobile-use/internal-development/self-learning-design-review-handoff.md (this file)
Then read mobile-use/internal-development/app-self-learning-design.md (the full design)
Continue review from Round 2 focus areas above.
```

---

## Key File Map

| File | What It Contains |
|------|-----------------|
| `mobile-use/internal-development/app-self-learning-design.md` | **THE DESIGN DOC** — full spec with round 1 fixes |
| `mobile-use/internal-development/lesson-learned-success-path-design.md` | Success path recording design (the lesson system self-learning builds on) |
| `mobile-use/internal-development/lesson-learned-memory-design.md` | Original lesson system design (Phase 1-3) |
| `mineru/ui_auto/lessons/` | Lesson system code (types, recorder, loader, scorer) |
| `mineru/ui_auto/graph/graph.py` | Agent graph with convergence_node recording |
| `mineru/ui_auto/context.py` | MobileUseContext — where `exploration_mode` needs to be added |
| `mineru/ui_auto/tools/tool_wrapper.py` | Where tool-level action guard needs to be added |
| `mineru/ui_auto/main.py` | CLI — where `learn` subcommand needs to be added |
| `scripts/view_lessons.py` | Lesson viewer (exists) |
| `scripts/view_exploration.py` | Exploration tree viewer (not yet created) |

---

## Implementation Status

| Component | Status |
|-----------|--------|
| Lesson-learned memory (Phase 1-3) | Done, device-tested |
| Success path recording | Done, device-tested |
| CLI `--lessons-dir` wiring | Done |
| `scripts/view_lessons.py` | Done |
| Self-learning design doc | Written, round 1 review done |
| Self-learning implementation | Not started (estimated 5-6 days for Phase 1 MVP) |
| `scripts/view_exploration.py` | Not started |
