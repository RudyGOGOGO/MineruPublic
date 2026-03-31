from pathlib import Path

from jinja2 import Template
from langchain_core.messages import HumanMessage, SystemMessage

from mineru.ui_auto.agents.contextor.types import AppLockVerificationOutput, ContextorOutput
from mineru.ui_auto.agents.planner.types import Subgoal
from mineru.ui_auto.agents.planner.utils import get_current_subgoal
from mineru.ui_auto.context import MobileUseContext
from mineru.ui_auto.controllers.controller_factory import create_device_controller
from mineru.ui_auto.controllers.platform_specific_commands_controller import (
    get_current_foreground_package_async,
    get_device_date,
)
from mineru.ui_auto.graph.state import State
from mineru.ui_auto.services.llm import get_llm, invoke_llm_with_timeout_message, with_fallback
from mineru.ui_auto.utils.app_launch_utils import launch_app_with_retries
from mineru.ui_auto.utils.decorators import wrap_with_callbacks
from mineru.ui_auto.utils.logger import get_logger

logger = get_logger(__name__)


class ContextorNode:
    def __init__(self, ctx: MobileUseContext):
        self.ctx = ctx

    @wrap_with_callbacks(
        before=lambda: logger.info("Starting Contextor Agent"),
        on_success=lambda _: logger.success("Contextor Agent"),
        on_failure=lambda _: logger.error("Contextor Agent"),
    )
    async def __call__(self, state: State):
        device_controller = create_device_controller(self.ctx)
        device_data = await device_controller.get_screen_data()
        current_app_package = await get_current_foreground_package_async(self.ctx)
        device_date = get_device_date(self.ctx)
        agent_outcome: str | None = None

        if self.ctx.execution_setup and self.ctx.execution_setup.app_lock_status:
            locked_app_package = self.ctx.execution_setup.app_lock_status.locked_app_package
            should_verify_app_lock = (
                self.ctx.execution_setup.app_lock_status.locked_app_initial_launch_success
            )
            if should_verify_app_lock:
                if current_app_package:
                    try:
                        verification: AppLockVerificationOutput = (
                            await self._handle_app_lock_verification(
                                state=state,
                                current_app_package=current_app_package,
                                locked_app_package=locked_app_package,
                            )
                        )
                        agent_outcome = verification.to_optional_message()
                    except Exception as e:
                        logger.error(f"Failed to verify app lock: {e}")
                else:
                    logger.warning(
                        f"App lock feature is setup for {locked_app_package}, "
                        "but could not determine current app, skipping"
                    )
            else:
                logger.warning(
                    f"App lock feature is setup for {locked_app_package}, "
                    "but initial launch was not successful, skipping"
                )

        # Enhanced perception: run OCR + merge + SoM when enabled
        enhanced_update = {}
        if self.ctx.perception_mode == "enhanced":
            try:
                from mineru.ui_auto.perception import enhance_screen_data
                enhanced = enhance_screen_data(
                    screenshot_b64=device_data.base64,
                    ui_elements=device_data.elements,
                    width=device_data.width,
                    height=device_data.height,
                )
                enhanced_update = {
                    "som_screenshot_b64": enhanced.som_screenshot_b64,
                    "unified_element_list": enhanced.element_list_text,
                    "unified_element_count": enhanced.element_count,
                    "unified_elements": enhanced.elements,
                }
                logger.info(f"Enhanced perception: {enhanced.element_count} unified elements detected")
            except Exception as e:
                logger.error(f"Enhanced perception failed, falling back to classic: {e}")

        # Lesson-learned memory: screen change detection, lesson loading, mistake recording
        # All gated behind self.ctx.lessons_dir is not None
        lessons_update: dict = {}
        if self.ctx.lessons_dir is not None:
            screen_changed: bool | None = None
            current_hash: str | None = None

            # Screen change detection (ahash + base64 length pre-filter)
            if device_data.base64:
                try:
                    current_hash = _compute_screenshot_hash(device_data.base64)
                    screen_changed = _detect_screen_change(
                        previous_hash=state.previous_screenshot_hash,
                        current_hash=current_hash,
                        previous_b64_len=len(state.latest_screenshot or ""),
                        current_b64_len=len(device_data.base64),
                    )
                except Exception as e:
                    logger.warning(f"Screen change detection failed: {e}")

            lessons_update["screen_changed"] = screen_changed
            lessons_update["previous_screenshot_hash"] = current_hash

            # Phase 3: Opportunistic app version extraction + meta update
            if current_app_package:
                try:
                    from mineru.ui_auto.lessons.recorder import (
                        extract_app_version,
                        update_app_meta,
                    )

                    app_version = extract_app_version(device_data.elements)
                    await update_app_meta(
                        lessons_dir=self.ctx.lessons_dir,
                        app_package=current_app_package,
                        app_version=app_version,
                    )
                except Exception as e:
                    logger.warning(f"App meta update failed: {e}")

            # Load lessons for current app
            active_lessons: str | None = None
            if current_app_package:
                try:
                    from mineru.ui_auto.lessons.loader import load_lessons_for_app

                    current_subgoal = get_current_subgoal(state.subgoal_plan)
                    active_lessons = await load_lessons_for_app(
                        lessons_dir=self.ctx.lessons_dir,
                        app_package=current_app_package,
                        subgoal=current_subgoal.description if current_subgoal else "",
                        current_activity=None,
                        current_key_elements=[],
                    )
                    if active_lessons:
                        logger.info(
                            f"📚 Loaded lessons for {current_app_package} "
                            f"({len(active_lessons)} chars)"
                        )
                except Exception as e:
                    logger.warning(f"Lesson loading failed: {e}")

            # Load cross-app trigger lessons when the task mentions another app.
            # Scan the goal text for other app package dirs that have lessons.
            if active_lessons is not None and current_app_package:
                try:
                    goal_text = state.initial_goal or ""
                    subgoal_text = ""
                    current_subgoal = get_current_subgoal(state.subgoal_plan)
                    if current_subgoal:
                        subgoal_text = current_subgoal.description or ""
                    combined_text = f"{goal_text} {subgoal_text}".lower()

                    # Check if any other app packages are mentioned in the goal
                    for app_dir in self.ctx.lessons_dir.iterdir():
                        if not app_dir.is_dir():
                            continue
                        other_pkg = app_dir.name
                        if other_pkg == current_app_package:
                            continue
                        # Check if this other app is referenced in the task
                        if other_pkg in combined_text or other_pkg.split(".")[-1] in combined_text:
                            try:
                                cross_lessons = await load_lessons_for_app(
                                    lessons_dir=self.ctx.lessons_dir,
                                    app_package=other_pkg,
                                    subgoal=subgoal_text,
                                    current_activity=None,
                                    current_key_elements=[],
                                )
                                if cross_lessons:
                                    active_lessons = (
                                        active_lessons + "\n\n"
                                        + f"**Cross-app context ({other_pkg}):**\n"
                                        + cross_lessons
                                    )
                            except Exception:
                                pass
                except Exception as e:
                    logger.warning(f"Cross-app lesson loading failed: {e}")

            lessons_update["active_lessons"] = active_lessons

            # Record "tap with no effect" mistake
            # NOTE: uses `screen_changed is False` (explicit False), not `not screen_changed`
            if (
                screen_changed is False
                and state.last_tool_name == "tap"
                and state.last_tool_status == "success"
                and current_app_package
            ):
                try:
                    from mineru.ui_auto.lessons.recorder import (
                        capture_screen_signature,
                        record_no_effect_mistake,
                    )

                    screen_sig = capture_screen_signature(
                        current_app_package, device_data.elements
                    )
                    current_subgoal = get_current_subgoal(state.subgoal_plan)
                    await record_no_effect_mistake(
                        lessons_dir=self.ctx.lessons_dir,
                        app_package=current_app_package,
                        screen_signature=screen_sig,
                        subgoal=current_subgoal.description if current_subgoal else "",
                    )
                except Exception as e:
                    logger.warning(f"Failed to record no-effect mistake: {e}")

            # Record tool failure mistake
            if (
                state.last_tool_status == "error"
                and state.last_tool_name
                and current_app_package
            ):
                try:
                    from langchain_core.messages import ToolMessage

                    from mineru.ui_auto.lessons.recorder import (
                        capture_screen_signature,
                        record_mistake_from_tool_failure,
                    )

                    tool_messages = [
                        m for m in state.executor_messages if isinstance(m, ToolMessage)
                    ]
                    if tool_messages:
                        last_msg = tool_messages[-1]
                        error_msg = (
                            str(last_msg.content) if last_msg.status == "error" else ""
                        )
                        if error_msg:
                            screen_sig = capture_screen_signature(
                                current_app_package, device_data.elements
                            )
                            current_subgoal = get_current_subgoal(state.subgoal_plan)
                            await record_mistake_from_tool_failure(
                                lessons_dir=self.ctx.lessons_dir,
                                app_package=current_app_package,
                                tool_name=state.last_tool_name,
                                tool_error=error_msg,
                                screen_signature=screen_sig,
                                subgoal=current_subgoal.description
                                if current_subgoal
                                else "",
                            )
                except Exception as e:
                    logger.warning(f"Failed to record tool failure mistake: {e}")

            # Phase 2: Process lesson feedback from previous Cortex cycle
            if current_app_package and (
                state.applied_lesson_ids or state.failed_lesson_ids
            ):
                try:
                    from mineru.ui_auto.lessons.recorder import update_lesson_feedback

                    await update_lesson_feedback(
                        lessons_dir=self.ctx.lessons_dir,
                        app_package=current_app_package,
                        applied_ids=state.applied_lesson_ids,
                        failed_ids=state.failed_lesson_ids,
                        screen_changed=screen_changed,
                        tool_status=state.last_tool_status,
                    )
                except Exception as e:
                    logger.warning(f"Failed to process lesson feedback: {e}")

            # Phase 3: Heuristic strategy detection
            # When the agent was stuck (consecutive no-changes >= 2) and now
            # the screen changed, the last action broke through — record it
            # as a strategy lesson.
            if screen_changed is False:
                lessons_update["consecutive_no_change_count"] = (
                    state.consecutive_no_change_count + 1
                )
            elif screen_changed is True:
                if (
                    state.consecutive_no_change_count >= 2
                    and current_app_package
                    and state.agents_thoughts
                ):
                    try:
                        from mineru.ui_auto.lessons.recorder import (
                            capture_screen_signature,
                            record_strategy,
                        )

                        # Extract the last cortex thought as the strategy
                        strategy_text = _extract_strategy_from_thoughts(
                            state.agents_thoughts
                        )
                        if strategy_text:
                            screen_sig = capture_screen_signature(
                                current_app_package, device_data.elements
                            )
                            current_subgoal = get_current_subgoal(
                                state.subgoal_plan
                            )
                            await record_strategy(
                                lessons_dir=self.ctx.lessons_dir,
                                app_package=current_app_package,
                                strategy_text=strategy_text,
                                screen_signature=screen_sig,
                                subgoal=(
                                    current_subgoal.description
                                    if current_subgoal
                                    else ""
                                ),
                            )
                    except Exception as e:
                        logger.warning(
                            f"Failed to record strategy: {e}"
                        )
                lessons_update["consecutive_no_change_count"] = 0
            else:
                # screen_changed is None (unknown) — don't change counter
                pass

        # In exploration mode, trim the UI hierarchy to reduce token cost.
        # Strip layout containers that carry no useful information for the LLM.
        # This saves ~12k tokens per Cortex call on typical screens.
        ui_elements = device_data.elements
        screenshot_b64 = device_data.base64
        if self.ctx.exploration_mode:
            ui_elements = _trim_hierarchy_for_exploration(ui_elements)
            # Skip screenshot in exploration — the hierarchy is sufficient
            # for navigation decisions, and the screenshot adds ~10k tokens.
            screenshot_b64 = None

        return await state.asanitize_update(
            ctx=self.ctx,
            update={
                "latest_ui_hierarchy": ui_elements,
                "latest_screenshot": screenshot_b64,
                "focused_app_info": current_app_package,
                "last_focused_app": current_app_package,
                "screen_size": (device_data.width, device_data.height),
                "device_date": device_date,
                "agents_thoughts": [agent_outcome],
                **enhanced_update,
                **lessons_update,
            },
            agent="contextor",
        )

    async def _handle_app_lock_verification(
        self, state: State, current_app_package: str, locked_app_package: str
    ) -> AppLockVerificationOutput:
        """Verify app lock compliance and decide whether to relaunch the locked app."""
        if not self.ctx.execution_setup or not self.ctx.execution_setup.app_lock_status:
            return AppLockVerificationOutput(
                package_name=locked_app_package,
                reasoning="App lock feature is not setup",
                status="error",
            )

        app_lock_status = self.ctx.execution_setup.app_lock_status
        locked_app_package = app_lock_status.locked_app_package

        if current_app_package == locked_app_package:
            logger.info(f"App lock verified: current app matches locked app ({locked_app_package})")
            return AppLockVerificationOutput(
                package_name=locked_app_package,
                status="already_in_foreground",
            )

        logger.warning(
            f"App lock violation detected: expected '{locked_app_package}', "
            f"but current app is '{current_app_package}'"
        )

        decision: ContextorOutput = await self._invoke_contextor_llm(
            initial_goal=state.initial_goal,
            subgoal_plan=state.subgoal_plan,
            agents_thoughts=state.agents_thoughts,
            locked_app_package=locked_app_package,
            current_app_package=current_app_package,
        )

        if decision.should_relaunch_app:
            logger.info(f"Relaunching locked app: {locked_app_package}")
            success, error = await launch_app_with_retries(self.ctx, app_package=locked_app_package)
            if not success:
                logger.error(f"Failed to relaunch {locked_app_package}: {error}")
                return AppLockVerificationOutput(
                    package_name=locked_app_package,
                    reasoning=f"Failed to relaunch app: {error}",
                    status="error",
                )
            return AppLockVerificationOutput(
                package_name=locked_app_package,
                reasoning=decision.reasoning,
                status="relaunched",
            )

        logger.info(f"Allowing app deviation to: {current_app_package}")
        return AppLockVerificationOutput(
            package_name=locked_app_package,
            reasoning=decision.reasoning,
            status="allowed_deviation",
        )

    async def _invoke_contextor_llm(
        self,
        initial_goal: str,
        subgoal_plan: list[Subgoal],
        agents_thoughts: list[str],
        locked_app_package: str,
        current_app_package: str,
    ) -> ContextorOutput:
        """Invoke the LLM to decide whether to relaunch the locked app."""

        MAX_AGENTS_THOUGHTS = 25

        system_message = Template(
            Path(__file__).parent.joinpath("contextor.md").read_text(encoding="utf-8")
        ).render(
            task_goal=initial_goal,
            subgoal_plan="\n".join([str(subgoal) for subgoal in subgoal_plan]),
            locked_app_package=locked_app_package,
            current_app_package=current_app_package,
            agents_thoughts=agents_thoughts[:MAX_AGENTS_THOUGHTS],
        )

        messages = [
            SystemMessage(content=system_message),
            HumanMessage(content="Please make your decision."),
        ]

        llm = get_llm(ctx=self.ctx, name="contextor").with_structured_output(ContextorOutput)
        llm_fallback = get_llm(
            ctx=self.ctx, name="contextor", use_fallback=True
        ).with_structured_output(ContextorOutput)

        response: ContextorOutput = await with_fallback(
            main_call=lambda: invoke_llm_with_timeout_message(llm.ainvoke(messages)),
            fallback_call=lambda: invoke_llm_with_timeout_message(llm_fallback.ainvoke(messages)),
        )  # type: ignore

        return response


def _compute_screenshot_hash(screenshot_b64: str) -> str:
    """Compute perceptual hash of screenshot for similarity comparison.

    Uses average_hash (ahash) instead of phash — 2-3x faster,
    sufficient for screen-level comparison. Lazy import of
    imagehash/PIL to avoid overhead when lessons are disabled.
    """
    import base64
    import io

    import imagehash
    from PIL import Image

    img = Image.open(io.BytesIO(base64.b64decode(screenshot_b64)))
    return str(imagehash.average_hash(img))


def _detect_screen_change(
    previous_hash: str | None,
    current_hash: str | None,
    previous_b64_len: int,
    current_b64_len: int,
    threshold: int = 8,
) -> bool | None:
    """Return True if screen changed, False if unchanged, None if unknown.

    Uses a two-tier approach:
    1. Fast pre-filter: if base64 lengths differ by >5%, screen
       definitely changed (skip expensive hash)
    2. Perceptual hash comparison for similar-length screenshots
    """
    if previous_hash is None or current_hash is None:
        return None  # No prior screen — unknown (not True, to avoid false assumptions)

    # Fast pre-filter: different screenshot sizes = definitely changed
    if previous_b64_len > 0 and abs(current_b64_len - previous_b64_len) / previous_b64_len > 0.05:
        return True

    # Perceptual hash comparison
    import imagehash

    distance = imagehash.hex_to_hash(previous_hash) - imagehash.hex_to_hash(current_hash)
    return distance > threshold


def _extract_strategy_from_thoughts(agents_thoughts: list[str]) -> str | None:
    """Extract the last Cortex decision as the successful strategy text.

    When the agent breaks out of a stuck state, the most recent Cortex
    thought contains the decision that worked. We extract it as a
    compact strategy description.
    """
    # Walk backwards to find the last Cortex thought
    for thought in reversed(agents_thoughts):
        if thought.startswith("[cortex]"):
            # Extract the decisions reason part
            text = thought.removeprefix("[cortex]").strip()
            if "Decisions reason:" in text:
                reason = text.split("Decisions reason:", 1)[1]
                # Trim at next section if present
                for sep in [
                    "Goals completion reason:",
                    "\n\n",
                ]:
                    if sep in reason:
                        reason = reason.split(sep, 1)[0]
                reason = reason.strip()
                if reason and len(reason) > 10:
                    # Truncate to keep it concise
                    return reason[:200]
            elif text and len(text) > 10:
                return text[:200]
    return None


def _trim_hierarchy_for_exploration(elements: list) -> list:
    """Strip non-informational elements from the UI hierarchy for exploration.

    During exploration, the LLM only needs elements that are:
    - Interactive (clickable, focusable, checkable)
    - Informational (have text, content-desc, or resource-id)

    Layout containers (FrameLayout, LinearLayout, ViewGroup) with no text,
    no content-desc, and no resource-id are pure structural wrappers that
    carry zero information for navigation decisions. Removing them typically
    reduces the hierarchy from ~80 elements to ~15-20, saving ~12k tokens.
    """
    trimmed = []
    for elem in elements:
        if not isinstance(elem, dict):
            trimmed.append(elem)
            continue

        text = (elem.get("text") or "").strip()
        content_desc = (elem.get("content-desc") or elem.get("accessibilityText") or "").strip()
        resource_id = (elem.get("resource-id") or elem.get("resourceName") or "").strip()
        clickable = elem.get("clickable") in (True, "true")
        focusable = elem.get("focusable") in (True, "true")
        checkable = elem.get("checkable") in (True, "true")
        selected = elem.get("selected") in (True, "true")

        # Keep if it has user-visible information or is interactive.
        # resource-id alone is NOT enough — empty containers like
        # navigation_bar_item_icon_container have resource-ids but carry
        # zero information for navigation decisions.
        has_content = text or content_desc
        is_interactive = clickable or selected
        if has_content or is_interactive:
            # Also strip verbose fields to save more tokens
            slim = {}
            for key in ("text", "resource-id", "resourceName", "class", "className",
                        "content-desc", "accessibilityText", "bounds",
                        "clickable", "checkable", "focusable", "selected",
                        "checked", "enabled", "index", "package"):
                if key in elem:
                    val = elem[key]
                    # Skip false/empty values to reduce size
                    if val in (False, "false", "", None):
                        continue
                    slim[key] = val
            trimmed.append(slim)

    return trimmed
