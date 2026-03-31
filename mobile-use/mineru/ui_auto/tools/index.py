from langchain_core.tools import BaseTool

from mineru.ui_auto.context import MobileUseContext
from mineru.ui_auto.tools.mobile.back import back_wrapper
from mineru.ui_auto.tools.mobile.erase_one_char import erase_one_char_wrapper
from mineru.ui_auto.tools.mobile.focus_and_clear_text import focus_and_clear_text_wrapper
from mineru.ui_auto.tools.mobile.focus_and_input_text import focus_and_input_text_wrapper
from mineru.ui_auto.tools.mobile.launch_app import launch_app_wrapper
from mineru.ui_auto.tools.mobile.long_press_on import long_press_on_wrapper
from mineru.ui_auto.tools.mobile.open_link import open_link_wrapper
from mineru.ui_auto.tools.mobile.press_key import press_key_wrapper
from mineru.ui_auto.tools.mobile.stop_app import stop_app_wrapper
from mineru.ui_auto.tools.mobile.swipe import swipe_wrapper
from mineru.ui_auto.tools.mobile.tap import tap_wrapper
from mineru.ui_auto.tools.mobile.video_recording import (
    start_video_recording_wrapper,
    stop_video_recording_wrapper,
)
from mineru.ui_auto.tools.mobile.find_element_by_vision import find_element_by_vision_wrapper
from mineru.ui_auto.tools.mobile.wait_for_delay import wait_for_delay_wrapper
from mineru.ui_auto.tools.scratchpad import (
    list_notes_wrapper,
    read_note_wrapper,
    save_note_wrapper,
)
from mineru.ui_auto.tools.tool_wrapper import CompositeToolWrapper, ToolWrapper

EXECUTOR_WRAPPERS_TOOLS = [
    back_wrapper,
    open_link_wrapper,
    tap_wrapper,
    long_press_on_wrapper,
    swipe_wrapper,
    focus_and_input_text_wrapper,
    erase_one_char_wrapper,
    launch_app_wrapper,
    stop_app_wrapper,
    focus_and_clear_text_wrapper,
    press_key_wrapper,
    wait_for_delay_wrapper,
    # Vision-based element finding (uses local GUI Owl VLM)
    find_element_by_vision_wrapper,
    # Scratchpad tools for persistent memory
    save_note_wrapper,
    read_note_wrapper,
    list_notes_wrapper,
]

VIDEO_RECORDING_WRAPPERS = [
    start_video_recording_wrapper,
    stop_video_recording_wrapper,
]


def get_tools_from_wrappers(
    ctx: "MobileUseContext",
    wrappers: list[ToolWrapper],
) -> list[BaseTool]:
    tools: list[BaseTool] = []
    for wrapper in wrappers:
        if isinstance(wrapper, CompositeToolWrapper):
            tools.extend(wrapper.composite_tools_fn_getter(ctx))
            continue

        tools.append(wrapper.tool_fn_getter(ctx))
    return tools


def format_tools_list(ctx: MobileUseContext, wrappers: list[ToolWrapper]) -> str:
    return ", ".join([tool.name for tool in get_tools_from_wrappers(ctx, wrappers)])
