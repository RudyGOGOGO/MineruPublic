"""Master Orchestrator for dual-app cluster mode exploration.

Coordinates two independent exploration agents (Parent/Child apps),
intercepts trigger-worthy actions, and dispatches Observer Mode to
detect cross-app effects.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from mineru.ui_auto.context import MobileUseContext
from mineru.ui_auto.controllers.controller_factory import create_device_controller
from mineru.ui_auto.controllers.platform_specific_commands_controller import (
    get_current_foreground_package_async,
)
from mineru.ui_auto.exploration.discovery import discover_features
from mineru.ui_auto.exploration.helpers import (
    extract_key_text,
    find_child_by_identity,
    find_current_screen_node,
    quantize_bounds,
    update_median,
)
from mineru.ui_auto.exploration.observer import observe_for_cross_app_event
from mineru.ui_auto.exploration.planner import generate_exploration_goal, pick_next_node
from mineru.ui_auto.exploration.runner import (
    _merge_children,
    _select_strategy,
    create_exploration_agent,
    initialize_exploration,
    navigate_to_app_home,
    run_exploration_task,
)
from mineru.ui_auto.exploration.state import (
    load_exploration_state,
    save_exploration_state,
)
from mineru.ui_auto.exploration.types import (
    CrossAppTrigger,
    ExplorationState,
    FeatureNode,
    ObserverResult,
)
from mineru.ui_auto.sdk.agent import Agent
from mineru.ui_auto.sdk.builders import AgentConfigBuilder

logger = logging.getLogger(__name__)

# Labels that suggest an action may have cross-app effects.
# Matched against the nav_action label of each exploration goal.
DEFAULT_TRIGGER_HINTS = {
    "send", "share", "enable", "disable", "notify", "invite",
    "block", "allow", "start", "stop", "approve", "deny",
    "lock", "unlock", "restrict", "permit",
}


class MasterOrchestrator:
    """Coordinates two Exploration Planners for Parent/Child app testing.

    The orchestrator does NOT modify how each agent explores. It wraps
    the exploration loop with trigger detection and Observer Mode dispatch.

    Architecture:
    - Each agent runs its own exploration goals independently.
    - The orchestrator intercepts after each goal completion to check for
      trigger-worthy actions and dispatch Observer Mode on the other agent.
    - Agents are unaware of each other. Only the orchestrator sees both trees.
    """

    def __init__(
        self,
        primary_app: str,
        secondary_app: str,
        primary_ctx: MobileUseContext,
        secondary_ctx: MobileUseContext,
        primary_config_builder: AgentConfigBuilder,
        secondary_config_builder: AgentConfigBuilder,
        lessons_dir: Path,
        observer_timeout: float = 10.0,
        trigger_hints: set[str] | None = None,
    ):
        self.primary_app = primary_app
        self.secondary_app = secondary_app
        self.primary_ctx = primary_ctx
        self.secondary_ctx = secondary_ctx
        self.primary_config_builder = primary_config_builder
        self.secondary_config_builder = secondary_config_builder
        self.lessons_dir = lessons_dir
        self.observer_timeout = observer_timeout
        self.trigger_hints = trigger_hints or DEFAULT_TRIGGER_HINTS

        # State loaded/created during init
        self.primary_state: ExplorationState | None = None
        self.secondary_state: ExplorationState | None = None
        self.primary_agent: Agent | None = None
        self.secondary_agent: Agent | None = None

    async def init(self) -> None:
        """Initialize both agents and load/create exploration states."""
        self.primary_agent = await create_exploration_agent(
            self.primary_ctx, self.primary_config_builder,
        )
        self.secondary_agent = await create_exploration_agent(
            self.secondary_ctx, self.secondary_config_builder,
        )

        self.primary_state = load_exploration_state(
            self.lessons_dir, self.primary_app,
        ) or await initialize_exploration(
            self.primary_app, self.primary_ctx, self.lessons_dir,
        )

        self.secondary_state = load_exploration_state(
            self.lessons_dir, self.secondary_app,
        ) or await initialize_exploration(
            self.secondary_app, self.secondary_ctx, self.lessons_dir,
        )

    async def cleanup(self) -> None:
        """Clean up both agents."""
        if self.primary_agent:
            await self.primary_agent.clean()
        if self.secondary_agent:
            await self.secondary_agent.clean()

    async def run_cluster_session(
        self,
        budget_minutes: int = 45,
        max_depth: int = 4,
    ) -> None:
        """Run one cluster exploration session.

        Strategy:
        1. Primary agent explores one goal at a time.
        2. After each goal, check if the action might trigger a cross-app effect.
        3. If yes, snapshot the secondary's UI, wait with Observer Mode, record triggers.
        4. Then let the secondary agent explore one goal.
        5. Alternate until budget exhausted.

        This interleaved approach ensures both apps get explored while
        cross-app effects are captured at trigger points.
        """
        start_time = time.monotonic()
        primary_strategy = _select_strategy(self.primary_state)
        secondary_strategy = _select_strategy(self.secondary_state)

        try:
            while (time.monotonic() - start_time) / 60 < budget_minutes:
                # -- Primary agent: one goal --
                primary_node, primary_path = pick_next_node(
                    self.primary_state.root, max_depth, primary_strategy,
                )
                if primary_node:
                    await self._explore_one_goal(
                        node=primary_node,
                        parent_path=primary_path,
                        app_package=self.primary_app,
                        ctx=self.primary_ctx,
                        agent=self.primary_agent,
                        state=self.primary_state,
                    )

                    # Check for cross-app trigger
                    if self._should_observe(primary_node):
                        await self._observe_cross_app_effect(
                            trigger_node=primary_node,
                            trigger_app=self.primary_app,
                            observer_app=self.secondary_app,
                            observer_ctx=self.secondary_ctx,
                            observer_state=self.secondary_state,
                        )

                # -- Secondary agent: one goal --
                secondary_node, secondary_path = pick_next_node(
                    self.secondary_state.root, max_depth, secondary_strategy,
                )
                if secondary_node:
                    await self._explore_one_goal(
                        node=secondary_node,
                        parent_path=secondary_path,
                        app_package=self.secondary_app,
                        ctx=self.secondary_ctx,
                        agent=self.secondary_agent,
                        state=self.secondary_state,
                    )

                    if self._should_observe(secondary_node):
                        await self._observe_cross_app_effect(
                            trigger_node=secondary_node,
                            trigger_app=self.secondary_app,
                            observer_app=self.primary_app,
                            observer_ctx=self.primary_ctx,
                            observer_state=self.primary_state,
                        )

                # Both exhausted?
                if primary_node is None and secondary_node is None:
                    logger.info("Both apps fully explored.")
                    break

            # Save final states
            save_exploration_state(self.primary_state, self.lessons_dir, self.primary_app)
            save_exploration_state(self.secondary_state, self.lessons_dir, self.secondary_app)

            # Write cross-app trigger lessons to lessons.jsonl for both apps
            # so the Contextor can load them during real tasks
            from mineru.ui_auto.exploration.cross_app_lessons import write_cross_app_lessons

            await write_cross_app_lessons(
                self.primary_app, self.primary_state, self.lessons_dir,
            )
            await write_cross_app_lessons(
                self.secondary_app, self.secondary_state, self.lessons_dir,
            )
        finally:
            await self.cleanup()

    async def _explore_one_goal(
        self,
        node: FeatureNode,
        parent_path: list[str],
        app_package: str,
        ctx: MobileUseContext,
        agent: Agent,
        state: ExplorationState,
    ) -> None:
        """Execute a single exploration goal -- same logic as the single-app loop."""
        node.status = "in_progress"
        goal = generate_exploration_goal(node, parent_path)
        logger.info(f"[{app_package}] Exploring: {' > '.join(parent_path + [node.label])}")

        result = await run_exploration_task(
            goal=goal,
            app_package=app_package,
            ctx=ctx,
            agent=agent,
        )

        if result.success and result.final_ui_hierarchy:
            children = discover_features(
                result.final_ui_hierarchy,
                result.final_activity or "",
                result.screen_width,
                result.screen_height,
            )
            node.children = _merge_children(node.children, children)
            node.status = "explored"
        else:
            node.attempt_count += 1
            node.status = "failed" if node.attempt_count >= 3 else "pending"

        await navigate_to_app_home(ctx, app_package, state.home_signature)
        save_exploration_state(state, self.lessons_dir, app_package)

    def _should_observe(self, node: FeatureNode) -> bool:
        """Decide whether this node's action might trigger a cross-app effect.

        Three signals:
        1. The node already has known cross_app_triggers (from prior sessions).
        2. The node's label contains a trigger-hint keyword.
        3. (Future) The node is in a manually configured trigger list.
        """
        if node.cross_app_triggers:
            return True

        label_lower = node.label.lower()
        return any(hint in label_lower for hint in self.trigger_hints)

    async def _observe_cross_app_effect(
        self,
        trigger_node: FeatureNode,
        trigger_app: str,
        observer_app: str,
        observer_ctx: MobileUseContext,
        observer_state: ExplorationState,
    ) -> None:
        """Dispatch Observer Mode on the observer app after a trigger action.

        1. Snapshot the observer app's current UI as baseline.
        2. Run the observer polling loop.
        3. If a change is detected, record a cross-app trigger on the trigger node
           and upsert a passive_event/dynamic_element node in the observer tree.
        """
        same_device = self.primary_ctx is self.secondary_ctx

        if same_device:
            # Switch to the observer app before capturing baseline
            await _bring_app_to_foreground(observer_ctx, observer_app)

        controller = create_device_controller(observer_ctx)
        try:
            screen_data = await controller.get_screen_data()
            baseline = screen_data.elements if screen_data else []
        except Exception:
            logger.warning(f"Failed to capture baseline for {observer_app}")
            return

        activity = await get_current_foreground_package_async(observer_ctx) or ""

        logger.info(
            f"[{trigger_app} -> {observer_app}] Observer Mode: "
            f"watching for cross-app effect after '{trigger_node.label}'"
        )

        result = await observe_for_cross_app_event(
            ctx=observer_ctx,
            app_package=observer_app,
            baseline_hierarchy=baseline,
            current_activity=activity,
            timeout_seconds=self.observer_timeout,
        )

        if result.detected:
            logger.info(
                f"[{observer_app}] Detected: {result.event_type} "
                f"(latency={result.latency_ms}ms)"
            )

            # Record trigger on the primary/trigger node
            record_cross_app_trigger(trigger_node, observer_app, result)

            # Upsert in the observer's tree
            observer_screen = find_current_screen_node(observer_state.root, activity)
            if observer_screen and result.event_type == "element_updated":
                for updated in result.updated_elements:
                    identity_key = updated.get("identity_key", "")
                    existing = find_child_by_identity(observer_screen, identity_key)
                    if existing:
                        new_state = updated.get("new_state")
                        if new_state:
                            new_text = new_state.text if hasattr(new_state, "text") else new_state.get("text", "")
                            if new_text:
                                existing.label = new_text
                    else:
                        new_state = updated.get("new_state")
                        new_text = ""
                        if new_state:
                            new_text = new_state.text if hasattr(new_state, "text") else new_state.get("text", "")
                        observer_screen.children.append(FeatureNode(
                            id=identity_key,
                            label=new_text or identity_key,
                            node_type="dynamic_element",
                            status="explored",
                            identity_key=identity_key,
                        ))
            elif observer_screen and result.new_elements:
                observer_screen.children.append(FeatureNode(
                    id=f"passive_{len(observer_screen.children)}",
                    label=extract_key_text(result.new_elements),
                    node_type="passive_event",
                    status="explored",
                ))

            save_exploration_state(observer_state, self.lessons_dir, observer_app)
        else:
            logger.debug(f"[{observer_app}] No cross-app effect detected.")

        if same_device:
            # Restore the trigger app to foreground for the next goal
            restore_app = self.primary_app if observer_app == self.secondary_app else self.secondary_app
            restore_ctx = self.primary_ctx if observer_app == self.secondary_app else self.secondary_ctx
            await _bring_app_to_foreground(restore_ctx, restore_app)


def record_cross_app_trigger(
    primary_node: FeatureNode,
    secondary_app: str,
    observer_result: ObserverResult,
) -> None:
    """Record a cross-app trigger edge on the primary node.

    Called by the Master Orchestrator after Observer Mode detects a
    state change on the secondary app following a primary app action.

    For 'element_updated' events, the match key includes the element's
    identity key -- so "Child Avatar moved" always upserts the same trigger
    entry regardless of how many times the avatar moves.
    """
    # Build the match key: for element_updated, include identity key
    identity_key = None
    if observer_result.event_type == "element_updated" and observer_result.updated_elements:
        identity_key = observer_result.updated_elements[0].get("identity_key")

    existing = _find_matching_trigger(
        primary_node.cross_app_triggers,
        secondary_app,
        observer_result.event_type,
        identity_key,
    )

    if existing:
        existing.attempted_count += 1
        if observer_result.detected:
            existing.observed_count += 1
            existing.latency_ms = update_median(
                existing.latency_ms, observer_result.latency_ms or 0,
            )
        existing.reliability_score = (
            existing.observed_count / existing.attempted_count
            if existing.attempted_count > 0 else 0.0
        )

        # Upsert state for dynamic elements -- overwrite, don't append
        if observer_result.event_type == "element_updated" and observer_result.updated_elements:
            latest = observer_result.updated_elements[0]
            new_state = latest.get("new_state")
            if new_state:
                text = new_state.text if hasattr(new_state, "text") else new_state.get("text", "")
                bounds = new_state.bounds if hasattr(new_state, "bounds") else new_state.get("bounds", {})
                existing.last_observed_state = {
                    "text": text,
                    "bounds_quadrant": quantize_bounds(bounds) if bounds else "center",
                }
    else:
        trigger = CrossAppTrigger(
            target_app=secondary_app,
            expected_event=observer_result.event_type or "unknown",
            expected_ui_text=extract_key_text(observer_result.new_elements),
            element_identity_key=identity_key,
            last_observed_state=None,
            reliability_score=1.0 if observer_result.detected else 0.0,
            latency_ms=observer_result.latency_ms or 0,
            observed_count=1 if observer_result.detected else 0,
            attempted_count=1,
        )
        # For element_updated, capture the initial state snapshot
        if observer_result.event_type == "element_updated" and observer_result.updated_elements:
            latest = observer_result.updated_elements[0]
            new_state = latest.get("new_state")
            if new_state:
                text = new_state.text if hasattr(new_state, "text") else new_state.get("text", "")
                bounds = new_state.bounds if hasattr(new_state, "bounds") else new_state.get("bounds", {})
                trigger.last_observed_state = {
                    "text": text,
                    "bounds_quadrant": quantize_bounds(bounds) if bounds else "center",
                }
        primary_node.cross_app_triggers.append(trigger)


def _find_matching_trigger(
    triggers: list[CrossAppTrigger],
    target_app: str,
    event_type: str | None,
    identity_key: str | None,
) -> CrossAppTrigger | None:
    """Find an existing trigger entry that matches this observation.

    For element_updated events, matches on (target_app, event_type, identity_key)
    so the same logical element always maps to the same trigger.
    For other events, matches on (target_app, event_type) only.
    """
    for t in triggers:
        if t.target_app != target_app or t.expected_event != event_type:
            continue
        if event_type == "element_updated":
            if t.element_identity_key == identity_key:
                return t
        else:
            return t
    return None


async def _bring_app_to_foreground(ctx: MobileUseContext, app_package: str) -> None:
    """Bring an app to the foreground on a shared device.

    Used in same-device cluster mode to switch between primary and secondary
    apps during Observer Mode. Uses `monkey` with LAUNCHER category to
    resume the app's last activity (not cold-launch).
    """
    try:
        from mineru.ui_auto.controllers.platform_specific_commands_controller import get_adb_device

        device = get_adb_device(ctx)
        device.shell(
            f"monkey -p {app_package} -c android.intent.category.LAUNCHER 1"
        )
        await asyncio.sleep(1.0)  # Wait for activity transition to complete
    except Exception as e:
        logger.warning(f"Failed to bring {app_package} to foreground: {e}")
