# Mineru UI-Auto

LangGraph-based agent framework for autonomous Android device control. Customized fork of Minitap's mobile-use with enhanced perception, multi-model support, and learning capabilities.

## Codebase Structure

- `mineru/ui_auto/` — Main source code
  - `agents/` — LangGraph agent nodes (contextor, cortex, planner, orchestrator, executor, hopper, outputter)
  - `controllers/` — Device abstraction (Android ADB, iOS, cloud/Limrun)
  - `tools/` — Agent tools (tap, swipe, back, launch_app, etc.)
  - `services/` — LLM providers (`llm.py`), telemetry
  - `sdk/` — Public SDK (Agent, Builders, TaskRequest)
  - `graph/` — LangGraph state and graph definition
  - `config.py` — Settings, LLM config presets
  - `context.py` — `MobileUseContext` (shared state across agents)
  - `main.py` — CLI entry point (Typer)
  - `lessons/` — Lesson-learned memory system
  - `exploration/` — App self-learning exploration system
  - `perception/` — Enhanced perception (OCR + SoM overlay)
- `scripts/` — Utility scripts (view_lessons, view_exploration, exploration_report)
- `tests/` — Test suites

## Key Patterns

- **CLI**: Typer (NOT Click). Commands: `main` (run task), `learn` (self-learning), `learn-cluster` (dual-app)
- **Agent API**: `Agent(config=builder.build())`, `await agent.init()`, `agent.new_task(goal).with_locked_app_package().with_max_steps().build()`, `await agent.run_task(request=task)`
- **Device access**: `create_device_controller(ctx)` returns controller with `get_screen_data()`, `press_back()`
- **ADB shell**: `get_adb_device(ctx).shell("command")`
- **LLM calls**: `get_llm(ctx, name="cortex", temperature=0.7)` from `services/llm.py`, with `invoke_llm_with_timeout_message()` and `with_fallback()`
- **Context**: `MobileUseContext` has `adb_client`, `ui_adb_client`, `device`, `llm_config`, `lessons_dir`, `exploration_mode`
- **Async**: Most agent/controller code is async. CLI commands use `asyncio.run()`

## Lesson-Learned Memory

Per-app JSONL files that record mistakes, strategies, and navigation paths so the agent improves across runs.

- **Types**: `lessons/types.py` — `LessonEntry`, `PathStep`, `ScreenSignature`, `AppMeta`
- **Recording**: `lessons/recorder.py` — `record_no_effect_mistake()`, `record_mistake_from_tool_failure()`, `record_strategy()`, `record_success_path()`, `update_lesson_feedback()`, `extract_app_version()`, `update_app_meta()`
- **Loading**: `lessons/loader.py` — `load_lessons_for_app()` filters, scores, and formats lessons within a 500-token budget with type-diversity guarantees. Recognized types: `mistake`, `strategy`, `success_path`, `ui_mapping`, `cross_app_trigger`
- **Scoring**: `lessons/scorer.py` — Multi-signal relevance scoring (screen match, subgoal overlap, confidence, recency)
- **Integration**: Contextor loads lessons each cycle and injects them into the Cortex prompt. Cortex reports `applied_lesson_ids` and `failed_lesson_ids` for feedback.
- **Storage**: `lessons_dir/<app_package>/lessons.jsonl` (lessons), `_meta.json` (app version)
- **Staleness**: ui_mapping 30d, success_path 60d, other 90d

## App Self-Learning (Exploration)

Automated goal generation loop that explores an app's UI to build navigation knowledge before real tasks arrive. All code in `exploration/`.

### Architecture

A **feature tree** (`_exploration.json`) tracks every screen, modal, and dynamic element discovered. The exploration loop picks unvisited nodes, generates goals, runs the existing agent, discovers children from the final screen, and saves state after each goal.

### Key Files

- `types.py` — `FeatureNode` (tree node with status/children/cross_app_triggers), `ExplorationState`, `CrossAppTrigger`, `ElementDiff`, `ObserverResult`, `ExplorationTaskResult`
- `discovery.py` — `discover_features()` with list-view sampling (RecyclerView/ListView → 2 samples), structural IDs, dangerous label filtering
- `planner.py` — `pick_next_node()` (BFS/DFS), `generate_exploration_goal()` (template-based)
- `goal_generator.py` — `generate_smart_goal()` (LLM-powered with lesson context, falls back to template)
- `runner.py` — `run_exploration_session()` (main loop), `initialize_exploration()`, `navigate_to_app_home()` (back-press + force-stop fallback), `run_exploration_task()` (agent wrapper)
- `state.py` — `load_exploration_state()` / `save_exploration_state()` (atomic writes), crash recovery
- `safety.py` — `check_exploration_guard()` — tool-level blocklist enforced when `ctx.exploration_mode=True`
- `screen_classifier.py` — `classify_screen_transition()` — modal (<80% coverage) vs full_screen vs passive_event
- `identity.py` — `compute_element_identity_key()` (resource-id > content-desc prefix > className+index), `build_identity_index()`, `compute_identity_diff()` — prevents dynamic element bloat
- `observer.py` — `observe_for_cross_app_event()` — polling with 2s settle window for cluster mode
- `passive_classifier.py` — `classify_passive_event()` — banner/screen_change/element_appeared/updated/disappeared
- `helpers.py` — `extract_key_text()`, `quantize_bounds()`, `update_median()`, `compute_coverage()`, `find_child_by_identity()`, `find_current_screen_node()`
- `orchestrator.py` — `MasterOrchestrator` for dual-app cluster mode with Observer Mode and cross-app trigger recording (upsert semantics)
- `metrics.py` — `compute_exploration_metrics()`, `get_installed_app_version()`, coverage/redundancy/staleness scoring
- `cross_app_lessons.py` — `generate_cross_app_lessons()` converts triggers to lesson entries

### Session Handoff and Resumption

Exploration is multi-session by design. Per-app files in `lessons/<app_package>/`:
- `_exploration.json` — Full feature tree with node statuses (loaded by `load_exploration_state()`, resets `in_progress` → `pending` on load for crash recovery)
- `_handoff.md` — Human-readable summary: coverage, stop reason, pending nodes, resume command. Written by `_write_handoff()` at end of every session
- `lessons.jsonl` — Accumulated lessons (persist across sessions)

Resume flow: re-run same `ui-auto learn` command → loads `_exploration.json` → `pick_next_node()` finds next pending node → continues from where it stopped.

Unrecoverable errors (rate limits, quota exhausted) are detected by `_is_unrecoverable_error()` in runner.py — the session stops immediately, the current node stays `pending`, and the handoff file records the stop reason.

### Important Constraints

- **Circular import warning**: `tool_wrapper.py` must use lazy imports for anything in `exploration/` (tool_wrapper -> exploration -> runner -> sdk.agent -> tools -> tool_wrapper)
- **ScreenSignature** lives in `lessons/types.py` (reused by exploration, not redefined)
- **Bounds format**: UIAutomator2 returns bounds as dicts (`{left, top, right, bottom}`) but raw `uiautomator dump` uses strings (`"[0,66][1080,2424]"`). `parse_bounds()` in `discovery.py` normalizes both formats — always use it when accessing element bounds in exploration code
- **Element scan limit**: `MAX_ELEMENTS_SCAN_LIMIT=200` scans up to 200 elements from the hierarchy (bottom nav tabs appear late, around index 40-70). Output capped at `MAX_FEATURES_PER_SCREEN=30` nodes
- **Design doc**: `mobile-use/internal-development/app-self-learning-design.md` — authoritative reference for all exploration behavior
