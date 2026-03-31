import asyncio
from typing import Annotated

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langchain_core.tools.base import BaseTool, InjectedToolCallId
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

from mineru.ui_auto.constants import EXECUTOR_MESSAGES_KEY
from mineru.ui_auto.context import MobileUseContext
from mineru.ui_auto.controllers.unified_controller import UnifiedMobileController
from mineru.ui_auto.graph.state import State
from mineru.ui_auto.tools.tool_wrapper import ToolWrapper
from mineru.ui_auto.tools.types import Target
from mineru.ui_auto.tools.utils import has_valid_selectors, validate_coordinates_bounds
from mineru.ui_auto.utils.logger import get_logger

logger = get_logger(__name__)


async def _gui_owl_locate(ctx: MobileUseContext, target: Target) -> tuple[int, int] | None:
    """Use GUI Owl VLM to visually locate an element. Returns (pixel_x, pixel_y) or None."""
    from mineru.ui_auto.tools.mobile.find_element_by_vision import (
        _call_gui_owl,
        _parse_coordinates,
        _prepare_screenshot_for_model,
    )
    from mineru.ui_auto.controllers.controller_factory import create_device_controller

    # Build a description from the target
    desc_parts = []
    if target.text:
        desc_parts.append(f"the element with text '{target.text}'")
    if target.resource_id:
        # Extract readable name from resource_id like "com.android.settings:id/title"
        short_id = target.resource_id.split("/")[-1] if "/" in target.resource_id else target.resource_id
        desc_parts.append(f"the '{short_id}' element")
    if not desc_parts:
        return None
    element_description = " or ".join(desc_parts)

    try:
        controller = create_device_controller(ctx)
        screen_data = await controller.get_screen_data()
        model_b64, orig_w, orig_h = _prepare_screenshot_for_model(screen_data.base64)

        logger.info(f"[GUI Owl] Visually locating: {element_description}")
        raw_output = await asyncio.to_thread(_call_gui_owl, model_b64, element_description)
        logger.info(f"[GUI Owl] Raw output: {raw_output}")

        coords = _parse_coordinates(raw_output)
        if coords is None:
            logger.warning(f"[GUI Owl] Could not parse coordinates from: {raw_output}")
            return None

        grid_x, grid_y = coords
        pixel_x = int((grid_x / 1000.0) * ctx.device.device_width)
        pixel_y = int((grid_y / 1000.0) * ctx.device.device_height)
        pixel_x = max(0, min(pixel_x, ctx.device.device_width - 1))
        pixel_y = max(0, min(pixel_y, ctx.device.device_height - 1))

        logger.info(f"[GUI Owl] Found element at pixel ({pixel_x}, {pixel_y})")
        return pixel_x, pixel_y
    except Exception as e:
        logger.warning(f"[GUI Owl] Vision lookup failed: {e}")
        return None


def get_tap_tool(ctx: MobileUseContext) -> BaseTool:
    @tool
    async def tap(
        agent_thought: str,
        target: Target,
        tool_call_id: Annotated[str, InjectedToolCallId],
        state: Annotated[State, InjectedState],
    ):
        """
        Taps on a UI element identified by the 'target' object.

        The 'target' object allows specifying an element by its resource_id
        (with an optional index), its bounds, or its text content (with an optional index).
        The tool uses a fallback strategy, trying the locators in that order.
        """
        # Track all attempts for better error reporting
        attempts: list[dict] = []
        success = False
        successful_selector: str | None = None

        # Validate target has at least one selector
        if not has_valid_selectors(target):
            attempts.append(
                {
                    "selector": "none",
                    "error": "No valid selector provided (need bounds, resource_id, or text)",
                }
            )

        controller = UnifiedMobileController(ctx)

        # -1. Enhanced perception: tap by element_id from unified element list
        if not success and ctx.perception_mode == "enhanced" and target.element_id is not None:
            unified_elements = state.unified_elements
            if unified_elements:
                element = next(
                    (el for el in unified_elements if el.id == target.element_id), None
                )
                if element:
                    cx, cy = element.center
                    selector_info = f"element_id={target.element_id} ({cx}, {cy})"
                    try:
                        logger.info(f"Attempting tap with {selector_info}")
                        result = await controller.tap_at(x=cx, y=cy)
                        if result.error is None:
                            success = True
                            successful_selector = selector_info
                        else:
                            logger.warning(f"Tap with {selector_info} failed: {result.error}")
                            attempts.append({"selector": selector_info, "error": result.error})
                    except Exception as e:
                        logger.warning(f"Exception during tap with {selector_info}: {e}")
                        attempts.append({"selector": selector_info, "error": str(e)})
                else:
                    attempts.append({
                        "selector": f"element_id={target.element_id}",
                        "error": f"Element #{target.element_id} not found in unified elements",
                    })

        # 0. If GUI Owl is enabled, use VLM to visually locate the element first
        if not success and ctx.gui_owl_enabled:
            owl_coords = await _gui_owl_locate(ctx, target)
            if owl_coords:
                pixel_x, pixel_y = owl_coords
                selector_info = f"GUI Owl vision ({pixel_x}, {pixel_y})"
                try:
                    logger.info(f"Attempting tap with {selector_info}")
                    result = await controller.tap_at(x=pixel_x, y=pixel_y)
                    if result.error is None:
                        success = True
                        successful_selector = selector_info
                    else:
                        logger.warning(f"Tap with {selector_info} failed: {result.error}")
                        attempts.append({"selector": selector_info, "error": result.error})
                except Exception as e:
                    logger.warning(f"Exception during tap with {selector_info}: {e}")
                    attempts.append({"selector": selector_info, "error": str(e)})

        # 1. Try with COORDINATES FIRST (visual approach)
        if not success and target.bounds:
            center = target.bounds.get_center()
            selector_info = f"coordinates ({center.x}, {center.y})"

            # Validate bounds before attempting
            bounds_error = validate_coordinates_bounds(
                target, ctx.device.device_width, ctx.device.device_height
            )
            if bounds_error:
                logger.warning(f"Coordinates out of bounds: {bounds_error}")
                attempts.append(
                    {"selector": selector_info, "error": f"Out of bounds: {bounds_error}"}
                )
            else:
                try:
                    center_point = target.bounds.get_center()
                    logger.info(f"Attempting tap with {selector_info}")
                    result = await controller.tap_at(x=center_point.x, y=center_point.y)
                    if result.error is None:
                        success = True
                        successful_selector = selector_info
                    else:
                        error_msg = result.error
                        logger.warning(f"Tap with {selector_info} failed: {error_msg}")
                        attempts.append({"selector": selector_info, "error": error_msg})
                except Exception as e:
                    logger.warning(f"Exception during tap with {selector_info}: {e}")
                    attempts.append({"selector": selector_info, "error": str(e)})

        # 2. If coordinates failed or weren't provided, try with resource_id
        if not success and target.resource_id:
            selector_info = f"resource_id='{target.resource_id}' (index={target.resource_id_index})"
            try:
                logger.info(f"Attempting tap with {selector_info}")
                result = await controller.tap_element(
                    resource_id=target.resource_id,
                    index=target.resource_id_index or 0,
                )
                if result.error is None:
                    success = True
                    successful_selector = selector_info
                else:
                    error_msg = result.error
                    logger.warning(f"Tap with {selector_info} failed: {error_msg}")
                    attempts.append({"selector": selector_info, "error": error_msg})
            except Exception as e:
                logger.warning(f"Exception during tap with {selector_info}: {e}")
                attempts.append({"selector": selector_info, "error": str(e)})

        # 3. If resource_id failed or wasn't provided, try with text (last resort)
        if not success and target.text:
            selector_info = f"text='{target.text}' (index={target.text_index})"
            try:
                logger.info(f"Attempting tap with {selector_info}")
                result = await controller.tap_element(
                    text=target.text,
                    index=target.text_index or 0,
                )
                if result.error is None:
                    success = True
                    successful_selector = selector_info
                else:
                    error_msg = result.error
                    logger.warning(f"Tap with {selector_info} failed: {error_msg}")
                    attempts.append({"selector": selector_info, "error": error_msg})
            except Exception as e:
                logger.warning(f"Exception during tap with {selector_info}: {e}")
                attempts.append({"selector": selector_info, "error": str(e)})

        # Build result message
        if success:
            agent_outcome = tap_wrapper.on_success_fn(successful_selector)
        else:
            # Build detailed failure message with all attempts
            failure_details = "; ".join([f"{a['selector']}: {a['error']}" for a in attempts])
            agent_outcome = tap_wrapper.on_failure_fn(failure_details)

        tool_message = ToolMessage(
            tool_call_id=tool_call_id,
            content=agent_outcome,
            additional_kwargs={"attempts": attempts} if not success else {},
            status="success" if success else "error",
        )
        return Command(
            update=await state.asanitize_update(
                ctx=ctx,
                update={
                    "agents_thoughts": [agent_thought, agent_outcome],
                    EXECUTOR_MESSAGES_KEY: [tool_message],
                },
                agent="executor",
            ),
        )

    return tap


tap_wrapper = ToolWrapper(
    tool_fn_getter=get_tap_tool,
    on_success_fn=lambda selector_info: f"Tap on element with {selector_info} was successful.",
    on_failure_fn=lambda failure_details: f"Failed to tap on element. Attempts: {failure_details}",
)
