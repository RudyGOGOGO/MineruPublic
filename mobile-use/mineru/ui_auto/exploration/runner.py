"""Session lifecycle and agent wrapper for self-learning exploration.

Contains the core exploration loop: initialize → pick node → generate goal →
run agent → discover children → save state → repeat.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from mineru.ui_auto.context import MobileUseContext
from mineru.ui_auto.utils.logger import get_logger
from mineru.ui_auto.controllers.controller_factory import create_device_controller
from mineru.ui_auto.controllers.platform_specific_commands_controller import (
    get_adb_device,
    get_current_foreground_package_async,
)
from mineru.ui_auto.controllers.unified_controller import UnifiedMobileController
from mineru.ui_auto.exploration.discovery import discover_features, parse_bounds
from mineru.ui_auto.exploration.goal_generator import generate_smart_goal
from mineru.ui_auto.exploration.planner import generate_exploration_goal, pick_next_node
from mineru.ui_auto.exploration.screen_classifier import (
    classify_screen_transition,
    extract_new_elements,
)
from mineru.ui_auto.exploration.state import (
    TreeStats,
    compute_tree_stats,
    count_lines,
    load_exploration_state,
    save_exploration_state,
)
from mineru.ui_auto.exploration.types import (
    ExplorationState,
    ExplorationTaskResult,
    FeatureNode,
    SessionSummary,
    capture_screen_signature,
)
from mineru.ui_auto.lessons.types import ScreenSignature
from mineru.ui_auto.sdk.agent import Agent
from mineru.ui_auto.sdk.builders import AgentConfigBuilder
from mineru.ui_auto.services.llm import get_llm, invoke_llm_with_timeout_message

logger = get_logger(__name__)


# ── Authentication Barrier Detection ─────────────────────────

AUTH_INDICATORS = [
    "sign in", "log in", "enter password", "enter pin",
    "verify your identity", "authentication required",
    "create account",
]


UNRECOVERABLE_ERROR_PATTERNS = [
    "hit your limit",
    "rate limit",
    "quota exceeded",
    "billing",
    "insufficient_quota",
    "api key",
    "authentication",
    "unauthorized",
    "403",
    "429",
]

# Errors where the node itself is unreachable — mark as failed immediately,
# don't retry (wastes budget on transient/unreachable content)
NODE_UNREACHABLE_PATTERNS = [
    "recursion limit",
    "recursion_limit",
    "max steps",
]


def _is_unrecoverable_error(error_msg: str) -> bool:
    """Check if an error indicates the session should stop entirely.

    Rate limits, billing issues, and auth failures affect all subsequent
    goals — retrying will just waste time and accumulate failures.
    """
    return any(pattern in error_msg for pattern in UNRECOVERABLE_ERROR_PATTERNS)


def _is_auth_barrier(ui_hierarchy: list[dict]) -> bool:
    """Check if the current screen is an authentication barrier."""
    texts = [elem.get("text", "").lower() for elem in ui_hierarchy[:50]]
    combined = " ".join(texts)
    return any(indicator in combined for indicator in AUTH_INDICATORS)


# ── Agent Task Wrapper ───────────────────────────────────────

async def run_exploration_task(
    goal: str,
    app_package: str,
    ctx: MobileUseContext,
    agent: Agent,
    max_steps: int = 50,
) -> ExplorationTaskResult:
    """Run a single exploration goal through the existing agent graph.

    Wraps Agent.run_task() to:
    1. Lock the agent to the target app
    2. Cap the recursion limit (max_steps)
    3. Capture the final UI hierarchy after the agent finishes
    4. Return a structured ExplorationTaskResult

    The Agent instance is created once per session (in run_exploration_session)
    and reused across goals. This avoids re-initializing LLM connections per goal.
    """
    controller = create_device_controller(ctx)

    try:
        # Build a TaskRequest using the SDK builder pattern.
        request = (
            agent.new_task(goal)
                .with_locked_app_package(app_package)
                .with_max_steps(max_steps)
                .build()
        )
        await agent.run_task(request=request)

        # Capture the screen state after the agent finishes.
        screen_data = await controller.get_screen_data()
        current_activity_info = await get_current_foreground_package_async(ctx)

        return ExplorationTaskResult(
            success=True,
            final_ui_hierarchy=screen_data.elements if screen_data else [],
            final_activity=current_activity_info,
            screen_width=screen_data.width if screen_data else 1080,
            screen_height=screen_data.height if screen_data else 2400,
        )

    except Exception as e:
        logger.warning(f"Exploration task failed: {e}")
        return ExplorationTaskResult(
            success=False,
            error=str(e),
        )


# ── Session Lifecycle ────────────────────────────────────────

async def create_exploration_agent(
    ctx: MobileUseContext,
    config_builder: AgentConfigBuilder,
) -> Agent:
    """Create and initialize an Agent instance configured for exploration.

    The agent is created once per session and reused for all goals.
    exploration_mode=True on the context enables the tool-level safety guard.
    """
    ctx.exploration_mode = True

    agent = Agent(config=config_builder.build())
    await agent.init()
    return agent


def _select_strategy(state: ExplorationState) -> str:
    """Auto-select BFS or DFS based on session history.

    - First session (no prior sessions): BFS to map the top-level structure.
    - Subsequent sessions: DFS to fill in depth within sections.
    """
    if state.sessions_completed == 0:
        return "breadth_first"
    return "depth_first"


def _merge_children(
    existing: list[FeatureNode],
    discovered: list[FeatureNode],
) -> list[FeatureNode]:
    """Merge newly discovered children with existing children.

    Strategy: match by structural ID. If a discovered child has the same ID
    as an existing child, keep the existing one (preserves status, attempt_count,
    children from prior sessions). New IDs are appended.
    """
    existing_ids = {child.id: child for child in existing}
    merged = list(existing)

    for child in discovered:
        if child.id not in existing_ids:
            merged.append(child)

    return merged


async def run_exploration_session(
    app_package: str,
    lessons_dir: Path,
    ctx: MobileUseContext,
    config_builder: AgentConfigBuilder,
    budget_minutes: int = 30,
    max_depth: int = 4,
    strategy: str = "auto",
) -> None:
    """Run one exploration session for an app.

    Each session:
    1. Loads or initializes the feature tree
    2. Creates an Agent instance (reused across all goals)
    3. Auto-selects strategy (BFS first session, DFS later) unless overridden
    4. Picks the next unvisited node
    5. Generates a goal and runs the agent
    6. Captures the final UI state and discovers children
    7. Updates the tree and saves after each goal
    8. Repeats until budget exhausted or all nodes explored
    """
    state = load_exploration_state(lessons_dir, app_package)
    if state is None:
        state = await initialize_exploration(app_package, ctx, lessons_dir)

    if strategy == "auto":
        strategy = _select_strategy(state)

    agent = await create_exploration_agent(ctx, config_builder)
    controller = create_device_controller(ctx)

    start_time = time.monotonic()
    goals_completed = 0
    goals_failed = 0
    stop_reason = "budget_exhausted"  # Default; overridden at each break

    # Track lessons recorded by counting lines in lessons.jsonl before/after
    lessons_path = lessons_dir / app_package / "lessons.jsonl"
    lessons_before = count_lines(lessons_path)

    # ── Root children batch exploration ─────────────────────────
    # After initialization, we're on the app home screen where all root
    # children (typically bottom nav tabs) are visible. Explore them ALL
    # directly via tap instead of spinning up the full agent for each tab.
    # This saves ~5-8 LLM calls per tab (typically 4-5 tabs = 20-40 calls).
    pending_root_children = [
        c for c in state.root.children if c.status == "pending"
    ]
    if pending_root_children:
        batch_completed, batch_failed, _ = await _explore_children_directly(
            parent_node=state.root,
            app_package=app_package,
            ctx=ctx,
            controller=controller,
            state=state,
            lessons_dir=lessons_dir,
            max_depth=max_depth,
            current_depth=0,
            is_tab_navigation=True,  # Tabs: don't press back
        )
        goals_completed += batch_completed
        goals_failed += batch_failed

        # ── Shallow exploration detection ───────────────────────
        # Direct-tap can only discover text-labeled elements. Screens with
        # icon-only navigation (hamburger menus, FABs), WebViews, or custom
        # gestures appear "empty" to direct-tap. Demote these back to pending
        # so the LLM agent can do a deeper exploration.
        _demote_shallow_nodes(state.root)

        # Navigate home after exploring all tabs
        await navigate_to_app_home(ctx, app_package, state.home_signature)
        save_exploration_state(state, lessons_dir, app_package)

    try:
        while True:
            elapsed = (time.monotonic() - start_time) / 60
            if elapsed >= budget_minutes:
                logger.info(
                    f"Session budget exhausted ({budget_minutes}m). "
                    f"Completed {goals_completed} goals."
                )
                stop_reason = "budget_exhausted"
                break

            node, parent_path = pick_next_node(state.root, max_depth, strategy)
            if node is None:
                logger.info("All reachable nodes explored!")
                stop_reason = "all_explored"
                break

            # Skip nodes whose ancestors lead to external apps — exploring
            # them would navigate outside the target app and waste steps
            if _has_external_app_ancestor(state.root, node):
                node.status = "skipped"
                node.skip_reason = "parent_is_external_app"
                logger.info(f"Skipping '{node.label}': parent leads to external app")
                save_exploration_state(state, lessons_dir, app_package)
                continue

            node.status = "in_progress"
            save_exploration_state(state, lessons_dir, app_package)

            # Capture the current screen BEFORE the agent runs.
            # This baseline is needed for modal detection.
            try:
                parent_screen_data = await controller.get_screen_data()
                parent_hierarchy = parent_screen_data.elements if parent_screen_data else []
            except Exception:
                parent_hierarchy = []

            # Full agent path: validate goal, generate goal, run agent pipeline
            sibling_nodes = [
                c for c in (state.root.children if not parent_path else
                            _find_siblings(state.root, node))
            ]

            # ── Goal validation ───────────────────────────────────
            # Cheap LLM pre-check: is this node reachable from home?
            # Saves 30 agent steps when the target is a transient dialog
            # control, a confirmation prompt, or an unreproducible UI state.
            is_valid = await _validate_exploration_goal(
                node=node,
                parent_path=parent_path,
                ctx=ctx,
                app_package=app_package,
            )
            if not is_valid:
                node.status = "skipped"
                node.skip_reason = "goal_validation_failed"
                logger.info(
                    f"Skipping '{node.label}': goal validation rejected "
                    f"(likely unreachable from home)"
                )
                save_exploration_state(state, lessons_dir, app_package)
                continue

            try:
                goal = await generate_smart_goal(
                    node=node,
                    parent_path=parent_path,
                    sibling_nodes=sibling_nodes,
                    ctx=ctx,
                    lessons_dir=lessons_dir,
                    app_package=app_package,
                )
            except Exception:
                goal = generate_exploration_goal(node, parent_path)
            logger.info(f"Exploring: {' > '.join(parent_path + [node.label])}")

            result = await run_exploration_task(
                goal=goal,
                app_package=app_package,
                ctx=ctx,
                agent=agent,
                max_steps=50,
            )

            if result.success and result.final_ui_hierarchy:
                # Check if the agent navigated outside the target app.
                current_fg = result.final_activity or ""
                if app_package not in current_fg:
                    node.status = "explored"
                    node.skip_reason = f"external_app: {current_fg}"
                    logger.info(
                        f"Node '{node.label}' navigated to external app "
                        f"({current_fg}), skipping child discovery"
                    )
                    goals_completed += 1
                elif _is_auth_barrier(result.final_ui_hierarchy):
                    node.status = "skipped"
                    node.skip_reason = "auth_required"
                    logger.info(f"Skipping {node.label}: authentication required")
                else:
                    children, _ = _discover_children(
                        node, parent_hierarchy, result, state,
                    )

                    # Filter out persistent navigation elements (bottom tabs, etc.)
                    ancestor_labels = _collect_ancestor_labels(state.root, node, state.global_nav_labels)
                    children = [
                        c for c in children
                        if c.label not in ancestor_labels
                    ]

                    current_depth = len(parent_path) + 1
                    if current_depth >= max_depth:
                        for child in children:
                            child.status = "deep_limit"
                    node.children = _merge_children(node.children, children)
                    node.status = "explored"
                    goals_completed += 1

                    # ── Immediate children exploration ──────────────────
                    # We're already on the screen where children are visible.
                    # Explore all pending children via direct tap instead of
                    # going home and re-navigating (saves ~5-8 LLM calls each).
                    if current_depth < max_depth:
                        batch_completed, batch_failed, _ = await _explore_children_directly(
                            parent_node=node,
                            app_package=app_package,
                            ctx=ctx,
                            controller=controller,
                            state=state,
                            lessons_dir=lessons_dir,
                            max_depth=max_depth,
                            current_depth=current_depth,
                        )
                        goals_completed += batch_completed
                        goals_failed += batch_failed

                    # ── Sibling exploration ──────────────────────────────
                    # Press back to the parent screen, then explore pending
                    # siblings directly (avoids going home and re-navigating).
                    parent = _find_parent_node(state.root, node)
                    if parent and parent.id != "root":
                        pending_siblings = [
                            s for s in parent.children
                            if s.status == "pending"
                        ]
                        if pending_siblings:
                            await controller.press_back()
                            await asyncio.sleep(0.5)
                            sib_completed, sib_failed, _ = await _explore_children_directly(
                                parent_node=parent,
                                app_package=app_package,
                                ctx=ctx,
                                controller=controller,
                                state=state,
                                lessons_dir=lessons_dir,
                                max_depth=max_depth,
                                current_depth=current_depth - 1,
                            )
                            goals_completed += sib_completed
                            goals_failed += sib_failed
            else:
                error_msg = (result.error or "").lower()

                # Session-ending errors (rate limits, auth) — stop entirely
                if _is_unrecoverable_error(error_msg):
                    logger.warning(
                        f"Unrecoverable error detected, ending session: {result.error}"
                    )
                    node.status = "pending"
                    save_exploration_state(state, lessons_dir, app_package)
                    stop_reason = f"unrecoverable_error: {result.error}"
                    break

                # Node-level failures (recursion limit, max steps) — mark failed
                # immediately, don't retry (the node is likely unreachable/transient)
                if any(p in error_msg for p in NODE_UNREACHABLE_PATTERNS):
                    logger.info(
                        f"Node '{node.label}' unreachable (hit step limit), marking failed"
                    )
                    node.status = "failed"
                else:
                    node.attempt_count += 1
                    if node.attempt_count >= 3:
                        node.status = "failed"
                    else:
                        node.status = "pending"
                goals_failed += 1

            await navigate_to_app_home(ctx, app_package, state.home_signature)
            save_exploration_state(state, lessons_dir, app_package)

        # Record session summary
        stats = compute_tree_stats(state.root)
        lessons_after = count_lines(lessons_path)
        state.sessions.append(SessionSummary(
            started=state.last_session or datetime.now(UTC).isoformat(),
            ended=datetime.now(UTC).isoformat(),
            goals_attempted=goals_completed + goals_failed,
            goals_completed=goals_completed,
            goals_failed=goals_failed,
            nodes_discovered=stats.total,
            lessons_recorded=lessons_after - lessons_before,
            strategy=strategy,
        ))
        state.sessions_completed += 1
        state.last_session = datetime.now(UTC).isoformat()
        save_exploration_state(state, lessons_dir, app_package)

        logger.info(
            f"Session complete. Tree: {stats.total} nodes, "
            f"{stats.explored} explored, {stats.pending} pending, "
            f"{stats.failed} failed."
        )

        # Generate navigation lessons from explored tree paths
        # so real tasks benefit from exploration knowledge
        from mineru.ui_auto.exploration.tree_to_lessons import generate_tree_lessons
        tree_lessons = generate_tree_lessons(state, lessons_dir)
        if tree_lessons:
            logger.info(f"Generated {tree_lessons} navigation lessons from exploration tree")

        # Write handoff file for session continuity
        _write_handoff(
            state=state,
            stats=stats,
            lessons_dir=lessons_dir,
            app_package=app_package,
            goals_completed=goals_completed,
            goals_failed=goals_failed,
            stop_reason=stop_reason,
        )
    finally:
        await agent.clean()


# ── Direct Children Exploration ───────────────────────────────

async def _tap_element_directly(
    node: FeatureNode,
    app_package: str,
    ctx: MobileUseContext,
) -> ExplorationTaskResult:
    """Directly tap an element on the current screen without the agent pipeline.

    Saves ~5-8 LLM calls per node by bypassing planner/orchestrator/cortex/executor.
    Returns success=False if the element isn't found on the current screen.
    """
    controller = create_device_controller(ctx)
    unified = UnifiedMobileController(ctx)

    screen_data = await controller.get_screen_data()
    if not screen_data or not screen_data.elements:
        return ExplorationTaskResult(success=False, error="no_screen_data")

    # Find the target element by label match
    target_elem = None
    for elem in screen_data.elements:
        text = (elem.get("text") or "").strip()
        if text == node.label:
            target_elem = elem
            break
        # Also match by content-desc for icon-only elements
        content_desc = (
            elem.get("contentDescription") or elem.get("content-desc") or ""
        ).strip()
        if content_desc == node.label:
            target_elem = elem
            break

    if not target_elem:
        return ExplorationTaskResult(
            success=False, error=f"element_not_found: '{node.label}'"
        )

    bounds = parse_bounds(target_elem.get("bounds"))
    if not bounds:
        return ExplorationTaskResult(success=False, error="no_bounds")
    x = (bounds.get("left", 0) + bounds.get("right", 0)) // 2
    y = (bounds.get("top", 0) + bounds.get("bottom", 0)) // 2

    tap_result = await unified.tap_at(x, y)
    if tap_result.error:
        return ExplorationTaskResult(success=False, error=f"tap_failed: {tap_result.error}")

    await asyncio.sleep(1.0)  # Wait for screen transition

    current_activity = await get_current_foreground_package_async(ctx)
    result_screen = await controller.get_screen_data()
    return ExplorationTaskResult(
        success=True,
        final_ui_hierarchy=result_screen.elements if result_screen else [],
        final_activity=current_activity,
        screen_width=result_screen.width if result_screen else 1080,
        screen_height=result_screen.height if result_screen else 2400,
    )


def _discover_children(
    node: FeatureNode,
    parent_hierarchy: list[dict],
    result: ExplorationTaskResult,
    state: ExplorationState,
) -> tuple[list[FeatureNode], str]:
    """Run transition classification and feature discovery on an exploration result.

    Shared between the full-agent path and the direct-tap path.
    Returns (children, transition_type) where transition_type is one of:
    "modal", "passive_event", or "full_screen".
    """
    transition_type = classify_screen_transition(
        parent_hierarchy=parent_hierarchy,
        current_hierarchy=result.final_ui_hierarchy,
        screen_width=result.screen_width,
        screen_height=result.screen_height,
    )

    if transition_type == "modal":
        node.node_type = "modal"
        node.dismiss_action = "back"
        modal_elements = extract_new_elements(
            parent_hierarchy, result.final_ui_hierarchy,
        )
        return discover_features(
            modal_elements,
            result.final_activity or "",
            result.screen_width,
            result.screen_height,
        ), transition_type
    elif transition_type == "passive_event":
        children: list[FeatureNode] = []
        try:
            from mineru.ui_auto.exploration.identity import (
                build_identity_index,
                compute_identity_diff,
            )
            from mineru.ui_auto.exploration.passive_classifier import (
                classify_passive_event,
            )
            from mineru.ui_auto.exploration.helpers import (
                extract_key_text,
                find_child_by_identity,
            )

            diff = compute_identity_diff(
                build_identity_index(parent_hierarchy, result.final_activity or ""),
                build_identity_index(result.final_ui_hierarchy, result.final_activity or ""),
            )
            sub_event = classify_passive_event(diff, result.final_ui_hierarchy)

            if sub_event == "element_updated":
                for updated in diff.updated:
                    ik = updated["identity_key"]
                    existing_node = find_child_by_identity(node, ik)
                    if existing_node:
                        new_state = updated.get("new_state")
                        if new_state:
                            t = new_state.text if hasattr(new_state, "text") else ""
                            if t:
                                existing_node.label = t
                    else:
                        new_state = updated.get("new_state")
                        t = ""
                        if new_state:
                            t = new_state.text if hasattr(new_state, "text") else ""
                        children.append(FeatureNode(
                            id=ik,
                            label=t or ik,
                            node_type="dynamic_element",
                            status="explored",
                            identity_key=ik,
                        ))
            elif diff.appeared:
                from mineru.ui_auto.exploration.discovery import _make_structural_id
                children.append(FeatureNode(
                    id=_make_structural_id(
                        diff.appeared[0].get("state", {}),
                        result.final_activity or "",
                    ) if isinstance(diff.appeared[0].get("state"), dict) else "passive",
                    label=extract_key_text(
                        [e.get("state", {}) for e in diff.appeared]
                    ),
                    node_type="passive_event",
                    status="explored",
                ))
        except Exception as e:
            logger.warning(f"Passive event handling failed: {e}")
        return children, transition_type
    else:
        return discover_features(
            result.final_ui_hierarchy,
            result.final_activity or "",
            result.screen_width,
            result.screen_height,
        ), transition_type


async def _explore_children_directly(
    parent_node: FeatureNode,
    app_package: str,
    ctx: MobileUseContext,
    controller,
    state: ExplorationState,
    lessons_dir: Path,
    max_depth: int,
    current_depth: int,
    is_tab_navigation: bool = False,
) -> tuple[int, int]:
    """Recursively explore all pending children of a node via direct tap.

    Builds the entire reachable feature tree without any LLM calls by:
    1. Tapping each pending child element on the current screen
    2. Capturing the resulting screen and discovering grandchildren
    3. Recursing into the grandchildren (if under max_depth)
    4. Pressing back to return to the parent screen

    For tab navigation (is_tab_navigation=True): no back press between
    children since tabs are always visible at the bottom.

    Nodes that fail direct tap (element not found, back navigation broken)
    are left as "pending" for the full agent to handle later.

    Returns (goals_completed, goals_failed, app_exited) where app_exited
    is True if the app was exited during exploration (caller should stop too).
    """
    pending_children = [c for c in parent_node.children if c.status == "pending"]
    if not pending_children:
        return 0, 0, False

    completed = 0
    failed = 0
    app_exited = False
    consecutive_failures = 0
    max_consecutive_failures = 3  # Stop exploring siblings after 3 failures in a row
    depth_prefix = "  " * current_depth

    logger.info(
        f"{depth_prefix}⚡ Direct exploration: {len(pending_children)} children of "
        f"'{parent_node.label}' at depth {current_depth}"
    )

    for child in pending_children:
        if consecutive_failures >= max_consecutive_failures:
            logger.info(
                f"{depth_prefix}  ⚡ Stopping: {consecutive_failures} consecutive failures, "
                f"leaving remaining siblings as pending for LLM agent"
            )
            break

        child.status = "in_progress"
        save_exploration_state(state, lessons_dir, app_package)

        # Capture parent screen before tap (for modal detection)
        try:
            pre_tap_data = await controller.get_screen_data()
            pre_tap_hierarchy = pre_tap_data.elements if pre_tap_data else []
        except Exception:
            pre_tap_hierarchy = []

        result = await _tap_element_directly(
            node=child, app_package=app_package, ctx=ctx,
        )

        screen_changed = False  # Default: assume screen didn't change

        if result.success and result.final_ui_hierarchy:
            current_fg = result.final_activity or ""
            if app_package not in current_fg:
                child.status = "explored"
                child.skip_reason = f"external_app: {current_fg}"
                logger.info(f"{depth_prefix}  ⚡ '{child.label}' → external app ({current_fg})")
                completed += 1
                screen_changed = True  # Left the app entirely
            elif _is_auth_barrier(result.final_ui_hierarchy):
                child.status = "skipped"
                child.skip_reason = "auth_required"
                screen_changed = True
            else:
                grandchildren, transition = _discover_children(
                    child, pre_tap_hierarchy, result, state,
                )
                ancestor_labels = _collect_ancestor_labels(state.root, child, state.global_nav_labels)
                grandchildren = [
                    c for c in grandchildren if c.label not in ancestor_labels
                ]
                child_depth = current_depth + 1
                if child_depth >= max_depth:
                    for gc in grandchildren:
                        gc.status = "deep_limit"
                child.children = _merge_children(child.children, grandchildren)
                child.status = "explored"
                completed += 1
                consecutive_failures = 0  # Reset on success

                pending_gc = [gc for gc in child.children if gc.status == "pending"]
                logger.info(
                    f"{depth_prefix}  ⚡ '{child.label}' → explored "
                    f"({len(grandchildren)} children, {len(pending_gc)} pending) "
                    f"[{transition}]"
                )

                # ── Recurse into grandchildren ──────────────────────
                # We're on the child's screen where grandchildren are visible.
                # Explore them before pressing back, building the tree deeper.
                if child_depth < max_depth and pending_gc:
                    gc_completed, gc_failed, gc_exited = await _explore_children_directly(
                        parent_node=child,
                        app_package=app_package,
                        ctx=ctx,
                        controller=controller,
                        state=state,
                        lessons_dir=lessons_dir,
                        max_depth=max_depth,
                        current_depth=child_depth,
                    )
                    completed += gc_completed
                    failed += gc_failed
                    if gc_exited:
                        # App exited during deeper recursion — we're no longer
                        # on the right screen. Break out of this level too.
                        app_exited = True
                        save_exploration_state(state, lessons_dir, app_package)
                        break

                # Track whether we need to press back
                screen_changed = transition in ("full_screen", "modal")
        else:
            logger.info(
                f"{depth_prefix}  ⚡ '{child.label}' → direct tap failed "
                f"({result.error}), leaving as pending"
            )
            child.status = "pending"  # Will be picked up by full agent later
            failed += 1
            consecutive_failures += 1
            screen_changed = False  # Tap failed, screen didn't change

        # Return to parent screen after exploring a child.
        if not is_tab_navigation and screen_changed:
            # Check if parent is a tab — if so, re-tap it (always visible)
            parent_is_tab = any(
                t.label == parent_node.label for t in state.root.children
            )
            if parent_is_tab:
                # Re-tap the parent tab — fast and reliable, no back needed
                await _tap_element_directly(
                    node=parent_node, app_package=app_package, ctx=ctx,
                )
                await asyncio.sleep(0.5)
            else:
                # Try back press for deeper nodes
                await controller.press_back()
                await asyncio.sleep(0.5)

                # If back exited the app, stop exploring siblings at this
                # level. Don't try to recover — remaining siblings will be
                # handled by the LLM agent. This avoids the exit/relaunch
                # loop that wastes time.
                try:
                    current_fg = await get_current_foreground_package_async(ctx)
                    if not current_fg or app_package not in current_fg:
                        logger.info(
                            f"{depth_prefix}  Back exited app — "
                            f"leaving remaining siblings as pending for LLM"
                        )
                        app_exited = True
                        save_exploration_state(state, lessons_dir, app_package)
                        break  # Exit sibling loop — propagate up
                except Exception:
                    pass

        save_exploration_state(state, lessons_dir, app_package)

    return completed, failed, app_exited


# ── Handoff File ─────────────────────────────────────────────

def _write_handoff(
    state: ExplorationState,
    stats: TreeStats,
    lessons_dir: Path,
    app_package: str,
    goals_completed: int,
    goals_failed: int,
    stop_reason: str,
) -> None:
    """Write a handoff file summarizing session state for the next session.

    The handoff file (_handoff.md) is a human-readable markdown file that:
    - Summarizes what was explored and what's left
    - Records why the session stopped
    - Lists the next nodes to explore
    - Provides the CLI command to resume

    Written alongside _exploration.json in the app's lessons directory.
    """
    app_dir = lessons_dir / app_package
    app_dir.mkdir(parents=True, exist_ok=True)
    handoff_path = app_dir / "_handoff.md"

    pending_nodes = _collect_nodes_by_status(state.root, "pending")
    failed_nodes = _collect_nodes_by_status(state.root, "failed")
    skipped_nodes = _collect_nodes_by_status(state.root, "skipped")

    reachable = stats.total - stats.skipped - stats.deep_limit
    coverage = stats.explored / reachable if reachable > 0 else 0.0

    lines = [
        f"# Exploration Handoff: {app_package}",
        f"",
        f"**Last session:** {state.last_session or 'unknown'}",
        f"**Sessions completed:** {state.sessions_completed}",
        f"**Stop reason:** {stop_reason}",
        f"",
        f"## Progress",
        f"",
        f"- Coverage: {coverage:.0%} ({stats.explored}/{reachable} reachable nodes)",
        f"- Explored: {stats.explored}",
        f"- Pending: {len(pending_nodes)}",
        f"- Failed: {stats.failed}",
        f"- Skipped: {stats.skipped}",
        f"- Deep limit: {stats.deep_limit}",
        f"- Total: {stats.total}",
        f"",
        f"## Last Session",
        f"",
        f"- Goals completed: {goals_completed}",
        f"- Goals failed: {goals_failed}",
        f"",
    ]

    if pending_nodes:
        lines.append("## Next Nodes to Explore")
        lines.append("")
        for label, path in pending_nodes[:15]:
            lines.append(f"- {path} > **{label}**")
        if len(pending_nodes) > 15:
            lines.append(f"- ... and {len(pending_nodes) - 15} more")
        lines.append("")

    if failed_nodes:
        lines.append("## Failed Nodes (3 attempts exhausted)")
        lines.append("")
        for label, path in failed_nodes:
            lines.append(f"- {path} > **{label}**")
        lines.append("")

    if skipped_nodes:
        lines.append("## Skipped Nodes")
        lines.append("")
        for label, path in skipped_nodes:
            lines.append(f"- {path} > **{label}**")
        lines.append("")

    lines.extend([
        "## Resume Command",
        "",
        "```bash",
        f"ui-auto learn {app_package} \\",
        f"  --lessons-dir {lessons_dir} \\",
        f"  --model-provider claude --claude-model claude-sonnet-4-6",
        "```",
        "",
        f"Add `--reset` to discard progress and start fresh.",
        "",
    ])

    handoff_path.write_text("\n".join(lines))
    logger.info(f"Handoff written to {handoff_path}")


def _collect_nodes_by_status(
    root: FeatureNode,
    status: str,
) -> list[tuple[str, str]]:
    """Collect nodes with a given status, returning (label, path) tuples."""
    results: list[tuple[str, str]] = []
    # in_progress nodes are effectively pending (reset on load), so include them
    match_statuses = {status, "in_progress"} if status == "pending" else {status}

    def _walk(node: FeatureNode, path: str) -> None:
        if node.status in match_statuses and node.id != "root":
            results.append((node.label, path))
        for child in node.children:
            child_path = f"{path} > {node.label}" if path else node.label
            _walk(child, child_path)

    _walk(root, "")
    return results


# ── Initialization ───────────────────────────────────────────

async def initialize_exploration(
    app_package: str,
    ctx: MobileUseContext,
    lessons_dir: Path,
) -> ExplorationState:
    """First-time setup: launch app, capture home screen, build root node.

    Args:
        app_package: Android package to explore
        ctx: Device context (needed for controller and ADB access)
        lessons_dir: Where to persist the exploration state
    """
    controller = create_device_controller(ctx)

    # Force-stop and cold-launch for a clean starting state
    await _force_stop_and_relaunch(ctx, app_package)

    # Wait for the app to actually be in the foreground before capturing screen data.
    # The monkey launch can take a few seconds for heavier apps.
    for _attempt in range(5):
        current_app_info = await get_current_foreground_package_async(ctx)
        if current_app_info and app_package in current_app_info:
            break
        logger.info(f"Waiting for {app_package} to reach foreground (current: {current_app_info})")
        await asyncio.sleep(1.0)
    else:
        # Last resort: try launching again
        logger.warning(f"{app_package} not in foreground after 5s, retrying launch")
        await _force_stop_and_relaunch(ctx, app_package)
        await asyncio.sleep(2.0)
        current_app_info = await get_current_foreground_package_async(ctx)

    # Capture home screen signature for later reset verification
    screen_data = await controller.get_screen_data()
    home_signature = capture_screen_signature(current_app_info, screen_data.elements)

    # Opportunistically extract app version from the home screen hierarchy
    app_version: str | None = None
    try:
        from mineru.ui_auto.lessons.recorder import extract_app_version, update_app_meta

        app_version = extract_app_version(screen_data.elements)
        if app_version:
            await update_app_meta(lessons_dir, app_package, app_version)
        else:
            # Fall back to _meta.json (may have been captured by prior task runs)
            import json as _json
            meta_path = lessons_dir / app_package / "_meta.json"
            if meta_path.exists():
                meta = _json.loads(meta_path.read_text())
                app_version = meta.get("app_version")
    except Exception as e:
        logger.warning(f"App version extraction failed: {e}")

    # Detect bottom nav tabs: scan ALL screens by tapping each tab.
    # This ensures root children are the actual tabs (not content from
    # whichever tab the app happened to open on after force-stop).
    unified = UnifiedMobileController(ctx)
    all_tab_labels: list[str] = []
    tab_children: dict[str, FeatureNode] = {}  # label → node

    tab_elements = _detect_bottom_nav_tabs(
        screen_data.elements, screen_data.width, screen_data.height,
    )

    if tab_elements:
        logger.info(f"Found {len(tab_elements)} bottom nav tabs: {[t[0] for t in tab_elements]}")
        all_tab_labels = [t[0] for t in tab_elements]

        # Create root children from tabs and tap each to discover their content
        for tab_label, tab_bounds in tab_elements:
            tab_node = FeatureNode(
                id=_make_structural_id_from_bounds(
                    tab_label, tab_bounds, home_signature.activity or "",
                    screen_data.width, screen_data.height,
                ),
                label=tab_label,
                nav_action=f"tap('{tab_label}')",
                status="pending",
            )
            tab_children[tab_label] = tab_node
    else:
        logger.info("No bottom nav tabs detected, using standard discovery")

    # Build root node
    if tab_children:
        children = list(tab_children.values())
    else:
        # Fallback: standard discovery from current screen
        children = discover_features(
            screen_data.elements,
            home_signature.activity or "",
            screen_data.width,
            screen_data.height,
        )
        all_tab_labels = [c.label for c in children]

    root = FeatureNode(
        id="root",
        label=f"{app_package} home",
        activity=home_signature.activity,
        key_elements=home_signature.key_elements,
        status="explored",
        children=children,
    )

    state = ExplorationState(
        app_package=app_package,
        app_version=app_version,
        root=root,
        home_signature=home_signature,
        global_nav_labels=all_tab_labels,
        created=datetime.now(UTC).isoformat(),
        sessions=[],
    )
    save_exploration_state(state, lessons_dir, app_package)
    logger.info(
        f"Initialized exploration for {app_package}: "
        f"{len(children)} features discovered on home screen"
    )
    return state


# ── Home Reset ───────────────────────────────────────────────

async def navigate_to_app_home(
    ctx: MobileUseContext,
    app_package: str,
    home_signature: ScreenSignature | None = None,
    max_back_presses: int = 3,
) -> bool:
    """Reset the app to its home screen. Falls back to force-stop if graceful nav fails.

    Strategy:
    1. Press back up to max_back_presses times
    2. After each back press, check if we're on the home screen
    3. If still not home, force-stop and cold-relaunch

    Returns True if home screen was reached.
    """
    controller = create_device_controller(ctx)

    # Attempt graceful back-navigation
    for _ in range(max_back_presses):
        await controller.press_back()
        await asyncio.sleep(0.5)

        # Verify: are we on the home screen?
        current_app = await get_current_foreground_package_async(ctx)
        if current_app != app_package:
            # We left the app — force relaunch
            break

        if home_signature:
            try:
                screen_data = await controller.get_screen_data()
                current_sig = capture_screen_signature(current_app, screen_data.elements)
                if _signatures_match(current_sig, home_signature):
                    return True  # Successfully reached home
            except Exception:
                pass

    # Graceful nav failed — hard reset
    logger.info(f"Graceful reset failed, force-stopping {app_package}")
    await _force_stop_and_relaunch(ctx, app_package)
    return True


def _signatures_match(sig_a: ScreenSignature, sig_b: ScreenSignature) -> bool:
    """Check if two screen signatures represent the same screen.

    Matches if: same activity AND at least 60% of key_elements overlap.
    """
    if sig_a.activity != sig_b.activity:
        return False
    if not sig_a.key_elements or not sig_b.key_elements:
        return sig_a.activity == sig_b.activity
    overlap = set(sig_a.key_elements) & set(sig_b.key_elements)
    return len(overlap) / max(len(sig_a.key_elements), len(sig_b.key_elements)) >= 0.6


def _has_external_app_ancestor(root: FeatureNode, target: FeatureNode) -> bool:
    """Check if any ancestor of the target node leads to an external app.

    If a parent was marked with skip_reason containing 'external_app' AND
    has no children (meaning it navigated away before discovering anything),
    all its descendants are unreachable. However, if the ancestor has children,
    those children were discovered while still in-app — the external app exit
    happened at the end of that node's exploration, not before child discovery.
    """
    def _find_path(node: FeatureNode, path: list[FeatureNode]) -> list[FeatureNode] | None:
        if node.id == target.id:
            return path
        for child in node.children:
            result = _find_path(child, path + [node])
            if result is not None:
                return result
        return None

    path = _find_path(root, [])
    if not path:
        return False
    return any(
        n.skip_reason and "external_app" in n.skip_reason
        and len(n.children) == 0  # Only block if no children were discovered in-app
        for n in path
    )


def _collect_ancestor_labels(
    root: FeatureNode,
    target: FeatureNode,
    global_nav_labels: list[str] | None = None,
) -> set[str]:
    """Collect labels of all ancestors and their siblings for a target node.

    Used to filter out persistent navigation elements (bottom tabs, toolbar items)
    that appear on every screen. If a discovered child has the same label as a
    root-level sibling or any ancestor, it's a navigation element — not a new feature.

    global_nav_labels: labels detected during init as persistent nav elements
    (e.g., bottom tab labels). These are ALWAYS filtered regardless of tree position.
    """
    labels: set[str] = set()

    # Always include global nav labels (bottom tabs detected at init)
    if global_nav_labels:
        labels.update(global_nav_labels)

    # Always include the root's direct children labels (top-level nav)
    for child in root.children:
        labels.add(child.label)

    # Walk up the path to collect ancestor labels
    def _find_path(node: FeatureNode, path: list[FeatureNode]) -> list[FeatureNode] | None:
        if node.id == target.id:
            return path
        for child in node.children:
            result = _find_path(child, path + [node])
            if result is not None:
                return result
        return None

    path = _find_path(root, [])
    if path:
        for ancestor in path:
            labels.add(ancestor.label)
            # Also add siblings of each ancestor
            for sibling in ancestor.children:
                labels.add(sibling.label)

    return labels


def _find_siblings(root: FeatureNode, target: FeatureNode) -> list[FeatureNode]:
    """Find sibling nodes of the target node in the feature tree."""
    def _search(node: FeatureNode) -> list[FeatureNode]:
        for child in node.children:
            if child.id == target.id:
                return node.children
            result = _search(child)
            if result:
                return result
        return []
    return _search(root)


def _demote_shallow_nodes(root: FeatureNode) -> int:
    """Demote "explored" nodes with 0 children back to "pending".

    Direct-tap marks nodes as explored even when the tap didn't discover
    anything — common for icon-only buttons, WebViews, or screens that
    require scrolling/gestures. These shallow nodes should be re-explored
    by the full LLM agent which can see screenshots, scroll, and understand
    non-text elements.

    Skips: root node, nodes with skip_reason (external apps, auth, dangerous),
    and nodes at depth 0 (tabs — genuinely might have no children).
    """
    demoted = 0

    def _walk(node: FeatureNode, depth: int) -> None:
        nonlocal demoted
        for child in node.children:
            if (
                child.status == "explored"
                and len(child.children) == 0
                and not child.skip_reason
                and depth >= 1  # Don't demote tab-level nodes
            ):
                child.status = "pending"
                demoted += 1
            _walk(child, depth + 1)

    _walk(root, 0)
    if demoted:
        logger.info(f"Demoted {demoted} shallow nodes back to pending for LLM exploration")
    return demoted


def _find_parent_node(root: FeatureNode, target: FeatureNode) -> FeatureNode | None:
    """Find the parent node of a target in the feature tree."""
    for child in root.children:
        if child.id == target.id and child.label == target.label:
            return root
        result = _find_parent_node(child, target)
        if result is not None:
            return result
    return None


GOAL_VALIDATION_PROMPT = """\
You are evaluating whether an exploration goal is achievable for a mobile app \
testing agent. The agent starts from the app's home screen every time.

App: {app_package}
Target navigation path: {path_description}
Target element: "{node_label}"

Answer ONLY "valid" or "invalid" followed by a one-line reason.

Mark as "invalid" if ANY of these apply:
- The target is a generic dialog control (Close, Cancel, OK, Save, Done, Back, \
Drag handle) that only exists inside a transient dialog/modal/bottom sheet
- The target is a confirmation message or prompt text, not a tappable feature
- The path requires reproducing a specific transient UI state (e.g., opening a \
dialog, triggering an error) that can't be reliably reached by sequential navigation
- The target label looks like dynamic content (timestamps, percentages, counts) \
rather than a stable UI feature

Mark as "valid" if:
- The target is a real app feature, screen, menu item, or settings option
- It can be reached by sequential navigation from the home screen (tap tabs, \
scroll, tap menu items)"""


async def _validate_exploration_goal(
    node: FeatureNode,
    parent_path: list[str],
    ctx: MobileUseContext,
    app_package: str,
) -> bool:
    """Lightweight LLM pre-check: is this exploration goal achievable?

    Costs ~300-500 tokens. Saves 30 full agent steps when the target is
    unreachable (transient dialogs, confirmation prompts, dynamic content).

    Returns True if the goal is valid, False if it should be skipped.
    """
    from langchain_core.messages import HumanMessage

    path_description = " > ".join(parent_path + [node.label])

    prompt = GOAL_VALIDATION_PROMPT.format(
        app_package=app_package,
        path_description=path_description,
        node_label=node.label,
    )

    try:
        llm = get_llm(ctx=ctx, name="cortex", temperature=0.0)
        from mineru.ui_auto.services.claude_cli import ChatClaudeCLI
        if isinstance(llm, ChatClaudeCLI):
            llm = ChatClaudeCLI(
                model_name=llm.model_name,
                timeout_seconds=30,
                lightweight=True,
            )
        response = await invoke_llm_with_timeout_message(
            llm.ainvoke([HumanMessage(content=prompt)])
        )
        answer = (response.content if hasattr(response, "content") else str(response)).strip().lower()
        logger.info(f"Goal validation for '{node.label}': {answer[:80]}")
        return answer.startswith("valid")
    except Exception as e:
        logger.warning(f"Goal validation failed, assuming valid: {e}")
        return True  # Fail open — let the agent try


def _detect_bottom_nav_tabs(
    elements: list[dict],
    screen_width: int = 1080,
    screen_height: int = 2400,
) -> list[tuple[str, dict]]:
    """Detect bottom navigation tabs from UI hierarchy.

    Two-tier approach:
    1. Look for a BottomNavigationView or TabLayout container, then extract
       text children inside its bounds. This is reliable when the container
       class is present in the hierarchy.
    2. Fall back to position-based heuristic (bottom 15%) with strict
       filtering: skip non-navigational text, system UI noise, numeric/
       dynamic labels, and require at least 2 candidates.

    Returns list of (label, bounds_dict) tuples.
    """
    from mineru.ui_auto.exploration.discovery import (
        _is_non_navigational_text,
        _is_system_ui_noise,
    )

    # ── Tier 1: Container-based detection ─────────────────────────
    NAV_CONTAINER_CLASSES = {
        "BottomNavigationView", "BottomNavigationItemView",
        "NavigationBarView", "NavigationBarItemView",
        "TabLayout", "TabView",
    }

    nav_containers = []
    for elem in elements:
        class_name = elem.get("className", "")
        resource_id = elem.get("resourceId") or elem.get("resource-id") or ""
        if (any(nc in class_name for nc in NAV_CONTAINER_CLASSES)
                or "bottom_nav" in resource_id.lower()
                or "bottom_navigation" in resource_id.lower()):
            bounds = parse_bounds(elem.get("bounds"))
            if bounds:
                nav_containers.append(bounds)

    if nav_containers:
        # Find text elements inside the nav container bounds
        tab_elements = []
        seen = set()
        for elem in elements:
            text = (elem.get("text") or "").strip()
            content_desc = (elem.get("contentDescription") or elem.get("content-desc") or "").strip()
            label = text or (content_desc if content_desc and len(content_desc) <= 30 else "")
            if not label or len(label) > 30 or label in seen:
                continue
            bounds = parse_bounds(elem.get("bounds"))
            if not bounds:
                continue
            # Check if this element is inside any nav container
            for cb in nav_containers:
                if (bounds.get("top", 0) >= cb.get("top", 0) - 20
                        and bounds.get("bottom", 9999) <= cb.get("bottom", 9999) + 20
                        and bounds.get("left", 0) >= cb.get("left", 0) - 20
                        and bounds.get("right", 9999) <= cb.get("right", 9999) + 20):
                    seen.add(label)
                    tab_elements.append((label, bounds))
                    break

        if len(tab_elements) >= 2:
            return tab_elements

    # ── Tier 2: Position-based fallback with strict filtering ─────
    bottom_threshold = screen_height * 0.85
    tab_elements = []
    seen = set()
    for elem in elements:
        text = (elem.get("text") or "").strip()
        content_desc = (elem.get("contentDescription") or elem.get("content-desc") or "").strip()
        label = text or (content_desc if content_desc and len(content_desc) <= 30 else "")
        if not label or len(label) > 30 or label in seen:
            continue

        # Skip dynamic/noise content that isn't a real tab
        if _is_non_navigational_text(label):
            continue
        if _is_system_ui_noise(label):
            continue
        # Skip labels that look like data values (prices, percentages, etc.)
        if re.match(r"^[\$€£¥]?\d", label) and len(label) <= 10:
            continue

        bounds = parse_bounds(elem.get("bounds"))
        if not bounds:
            continue
        center_y = (bounds.get("top", 0) + bounds.get("bottom", 0)) / 2
        if center_y >= bottom_threshold:
            seen.add(label)
            tab_elements.append((label, bounds))

    # Position-based needs at least 2 tabs to be credible
    if len(tab_elements) >= 2:
        return tab_elements

    return []


def _make_structural_id_from_bounds(
    label: str,
    bounds: dict,
    activity: str,
    screen_width: int,
    screen_height: int,
) -> str:
    """Generate a structural ID from label, bounds, and activity.

    Used during init for tab elements where we have bounds but not the full
    element dict that _make_structural_id expects.
    """
    import hashlib
    center_x = (bounds.get("left", 0) + bounds.get("right", 0)) / 2
    center_y = (bounds.get("top", 0) + bounds.get("bottom", 0)) / 2
    grid_x = int(center_x / max(screen_width, 1) * 8)
    grid_y = int(center_y / max(screen_height, 1) * 8)
    raw = f"{activity}:tab:{grid_x},{grid_y}"
    return hashlib.md5(raw.encode()).hexdigest()[:8]


async def _force_stop_and_relaunch(ctx: MobileUseContext, app_package: str) -> None:
    """Force-stop the app and cold-launch it. Guaranteed clean state."""
    try:
        device = get_adb_device(ctx)
        # am force-stop is fast (~100ms) and kills all app processes
        device.shell(f"am force-stop {app_package}")
        await asyncio.sleep(0.5)

        # Cold-launch via monkey (launches the app's main activity)
        device.shell(
            f"monkey -p {app_package} -c android.intent.category.LAUNCHER 1"
        )
        await asyncio.sleep(2.0)  # Wait for app to fully render
    except Exception as e:
        logger.warning(f"Force-stop/relaunch failed for {app_package}: {e}")
