from collections.abc import Callable

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from mineru.ui_auto.context import MobileUseContext


class ToolWrapper(BaseModel):
    tool_fn_getter: Callable[[MobileUseContext], BaseTool]
    on_success_fn: Callable[..., str]
    on_failure_fn: Callable[..., str]


class CompositeToolWrapper(ToolWrapper):
    composite_tools_fn_getter: Callable[[MobileUseContext], list[BaseTool]]


def guard_exploration_action(ctx: MobileUseContext, target_text: str | None) -> str | None:
    """Check if a tap/press action should be blocked in exploration mode.

    Call this from tap/press_key tool implementations before executing the action.
    Returns None if allowed, or an error message string if blocked.

    Usage in a tool:
        blocked = guard_exploration_action(ctx, target_text)
        if blocked:
            return blocked  # Return error to the LLM
    """
    # Lazy import to avoid circular dependency:
    # tool_wrapper → exploration.safety → exploration.__init__ → runner → sdk.agent → tools
    from mineru.ui_auto.exploration.safety import check_exploration_guard

    return check_exploration_guard(ctx, target_text)
