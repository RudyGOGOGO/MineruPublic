"""Tool that uses the local GUI Owl VLM to visually locate UI elements on screen.

GUI Owl is a visual grounding model: given a screenshot and an element description,
it returns [x, y] coordinates on a 1000x1000 grid. This tool converts those to
actual pixel coordinates on the device screen.

Use this when the accessibility tree (resource_id / text) cannot find an element,
e.g. icon-only buttons, images, or visually-described elements.
"""

import base64
import io
import re
from typing import Annotated

import httpx
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langchain_core.tools.base import InjectedToolCallId
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from PIL import Image

from mineru.ui_auto.config import settings
from mineru.ui_auto.constants import EXECUTOR_MESSAGES_KEY
from mineru.ui_auto.context import MobileUseContext
from mineru.ui_auto.controllers.controller_factory import create_device_controller
from mineru.ui_auto.graph.state import State
from mineru.ui_auto.tools.tool_wrapper import ToolWrapper
from mineru.ui_auto.utils.logger import get_logger

logger = get_logger(__name__)

# GUI Owl system prompt - proven to produce reliable [x, y] output
_SYSTEM_PROMPT = (
    "You are an expert GUI automation agent. "
    "Analyze the provided smartphone screenshot carefully. "
    "Locate the exact center of the UI element described by the user. "
    "Output ONLY the point coordinates in the format [x, y], "
    "where x and y are scaled to a 1000x1000 grid (values between 0 and 999). "
    "Do NOT output any other text, explanation, or formatting. Just [x, y]."
)

# Max image dimension sent to model (avoids MLX framework cropping bug)
_MAX_IMAGE_DIMENSION = 980


def _prepare_screenshot_for_model(screenshot_b64: str) -> tuple[str, int, int]:
    """Resize screenshot to fit model limits, return (b64, original_w, original_h)."""
    if screenshot_b64.startswith("data:image"):
        screenshot_b64 = screenshot_b64.split(",", 1)[1]

    image_data = base64.b64decode(screenshot_b64)
    original_img = Image.open(io.BytesIO(image_data))
    original_width, original_height = original_img.size

    # Resize for model (keep aspect ratio, max 980px longest side)
    model_img = original_img.convert("RGB")
    model_img.thumbnail((_MAX_IMAGE_DIMENSION, _MAX_IMAGE_DIMENSION), Image.Resampling.LANCZOS)

    buffered = io.BytesIO()
    model_img.save(buffered, format="JPEG", quality=90)
    model_b64 = base64.b64encode(buffered.getvalue()).decode("utf-8")

    return model_b64, original_width, original_height


def _parse_coordinates(model_output: str) -> tuple[float, float] | None:
    """Extract [x, y] from model output. Returns None if parsing fails."""
    match = re.search(r"\[([0-9.]+),\s*([0-9.]+)\]", model_output)
    if not match:
        return None
    x = float(match.group(1))
    y = float(match.group(2))
    if 0 <= x <= 1000 and 0 <= y <= 1000:
        return x, y
    return None


def _call_gui_owl(image_b64: str, element_description: str) -> str:
    """Send a request to the GUI Owl VLM server and return raw text response."""
    base_url = settings.GUI_OWL_BASE_URL.rstrip("/")
    # Ensure we hit /chat/completions
    url = f"{base_url}/chat/completions"

    payload = {
        "model": settings.GUI_OWL_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Find and point to: {element_description}"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_b64}",
                            "detail": "high",
                        },
                    },
                ],
            },
        ],
        "temperature": 0.0,
        "max_tokens": 50,
    }

    response = httpx.post(url, json=payload, timeout=300)
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def get_find_element_by_vision_tool(ctx: MobileUseContext):
    @tool
    async def find_element_by_vision(
        agent_thought: str,
        element_description: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
        state: Annotated[State, InjectedState],
    ) -> Command:
        """
        Use the GUI Owl vision model to visually locate a UI element on the current screen.

        This tool takes a screenshot, sends it to a local VLM (Vision Language Model),
        and returns the pixel coordinates of the described element.

        Use this when:
        - An element has no resource_id or text in the UI hierarchy
        - You need to find an icon-only button or image
        - The accessibility tree doesn't contain the element you need
        - You want to visually verify where an element is located

        Args:
            element_description: A clear description of the UI element to find.
                Examples: "the WiFi toggle switch", "the red delete button",
                "the search icon in the top right", "the Settings gear icon"

        Returns:
            The pixel coordinates (x, y) of the element center on the device screen,
            which can be used with the tap tool's bounds parameter.
        """
        try:
            # 1. Take a fresh screenshot
            controller = create_device_controller(ctx)
            screen_data = await controller.get_screen_data()
            screenshot_b64 = screen_data.base64
            device_width = ctx.device.device_width
            device_height = ctx.device.device_height

            # 2. Prepare image for model
            model_b64, orig_w, orig_h = _prepare_screenshot_for_model(screenshot_b64)
            logger.info(
                f"Sending screenshot to GUI Owl VLM "
                f"(original: {orig_w}x{orig_h}, device: {device_width}x{device_height})"
            )

            # 3. Call GUI Owl
            import asyncio
            raw_output = await asyncio.to_thread(_call_gui_owl, model_b64, element_description)
            logger.info(f"GUI Owl raw output: {raw_output}")

            # 4. Parse coordinates
            coords = _parse_coordinates(raw_output)
            if coords is None:
                agent_outcome = find_element_by_vision_wrapper.on_failure_fn(
                    f"Could not parse coordinates from model output: '{raw_output}'. "
                    f"The model may not have found the element '{element_description}' on screen."
                )
                tool_message = ToolMessage(
                    tool_call_id=tool_call_id,
                    content=agent_outcome,
                    status="error",
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

            # 5. Map 1000x1000 grid to device pixels
            grid_x, grid_y = coords
            pixel_x = int((grid_x / 1000.0) * device_width)
            pixel_y = int((grid_y / 1000.0) * device_height)

            # Clamp to screen bounds
            pixel_x = max(0, min(pixel_x, device_width - 1))
            pixel_y = max(0, min(pixel_y, device_height - 1))

            agent_outcome = find_element_by_vision_wrapper.on_success_fn(
                element_description, pixel_x, pixel_y, device_width, device_height
            )

        except httpx.ConnectError:
            agent_outcome = find_element_by_vision_wrapper.on_failure_fn(
                "GUI Owl VLM server is not running. "
                f"Start it with: mlx_vlm.server --port 8080 "
                f"(expected at {settings.GUI_OWL_BASE_URL})"
            )
        except Exception as e:
            agent_outcome = find_element_by_vision_wrapper.on_failure_fn(str(e))

        status = "success" if "Found" in agent_outcome else "error"
        tool_message = ToolMessage(
            tool_call_id=tool_call_id,
            content=agent_outcome,
            status=status,
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

    return find_element_by_vision


find_element_by_vision_wrapper = ToolWrapper(
    tool_fn_getter=get_find_element_by_vision_tool,
    on_success_fn=lambda desc, x, y, w, h: (
        f"Found '{desc}' at pixel coordinates ({x}, {y}) on a {w}x{h} screen. "
        f"You can now use the tap tool with bounds: "
        f'{{\"x\": {x}, \"y\": {y}, \"width\": 1, \"height\": 1}} to tap this element.'
    ),
    on_failure_fn=lambda details: f"Failed to find element by vision. {details}",
)
