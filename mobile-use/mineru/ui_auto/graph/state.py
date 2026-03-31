from typing import Annotated

from langchain_core.messages import AIMessage, AnyMessage
from langgraph.graph import add_messages
from pydantic import BaseModel

from mineru.ui_auto.agents.planner.types import Subgoal
from mineru.ui_auto.config import AgentNode
from mineru.ui_auto.context import MobileUseContext
from mineru.ui_auto.utils.logger import get_logger
from mineru.ui_auto.utils.recorder import record_interaction

logger = get_logger(__name__)


def take_last(a, b):
    return b


class State(BaseModel):
    messages: Annotated[list[AnyMessage], "Sequential messages", add_messages]
    remaining_steps: Annotated[int | None, "Remaining steps before the task is completed"] = None

    # planner related keys
    initial_goal: Annotated[str, "Initial goal given by the user"]

    # orchestrator related keys
    subgoal_plan: Annotated[list[Subgoal], "The current plan, made of subgoals"]

    # contextor related keys
    latest_ui_hierarchy: Annotated[
        list[dict] | None, "Latest UI hierarchy of the device", take_last
    ]
    latest_screenshot: Annotated[str | None, "Latest screenshot base64 of the device", take_last]
    focused_app_info: Annotated[str | None, "Focused app info", take_last]
    device_date: Annotated[str | None, "Date of the device", take_last]

    # cortex related keys
    structured_decisions: Annotated[
        str | None,
        "Structured decisions made by the cortex, for the executor to follow",
        take_last,
    ]
    complete_subgoals_by_ids: Annotated[
        list[str],
        "List of subgoal IDs to complete",
        take_last,
    ]

    # executor related keys
    executor_messages: Annotated[list[AnyMessage], "Sequential Executor messages", add_messages]
    cortex_last_thought: Annotated[str | None, "Last thought of the cortex for the executor"]

    # common keys
    agents_thoughts: Annotated[
        list[str],
        "All thoughts and reasons that led to actions (why a tool was called, expected outcomes..)",
        take_last,
    ]

    # enhanced perception keys (only populated when perception_mode == "enhanced")
    som_screenshot_b64: Annotated[str | None, "SoM-annotated screenshot base64", take_last] = None
    unified_element_list: Annotated[str | None, "Formatted unified element list text", take_last] = None
    unified_element_count: Annotated[int | None, "Number of unified elements detected", take_last] = None
    unified_elements: Annotated[list | None, "UnifiedElement objects for tap lookup", take_last] = None

    # scratchpad for explicit memory
    scratchpad: Annotated[
        dict[str, str],
        "Persistent key-value storage for notes the agent can save and retrieve",
        take_last,
    ] = {}

    # lesson-learned memory (Phase 1)
    active_lessons: Annotated[
        str | None, "Formatted lesson text for Cortex prompt", take_last
    ] = None
    screen_changed: Annotated[
        bool | None, "Whether screen changed. None = unknown (first cycle/resumed)", take_last
    ] = None
    previous_screenshot_hash: Annotated[
        str | None, "Perceptual hash of prior screenshot", take_last
    ] = None
    last_tool_name: Annotated[str | None, "Name of the last executed tool", take_last] = None
    last_tool_status: Annotated[
        str | None, "Status of last tool: 'success' or 'error'", take_last
    ] = None

    # lesson-learned memory (Phase 3: heuristic strategy detection)
    consecutive_no_change_count: Annotated[
        int, "Consecutive cycles where screen_changed was False", take_last
    ] = 0

    # lesson-learned memory (Phase 2: confidence feedback loop)
    applied_lesson_ids: Annotated[
        list[str], "Lesson IDs the Cortex followed this turn", take_last
    ] = []
    failed_lesson_ids: Annotated[
        list[str], "Lesson IDs whose strategies failed this turn", take_last
    ] = []

    # lesson-learned: success path recording
    action_trail: Annotated[
        list[dict], "Accumulated tool calls for current subgoal", take_last
    ] = []
    action_trail_subgoal_id: Annotated[
        str | None, "Subgoal ID the current action_trail belongs to", take_last
    ] = None
    last_focused_app: Annotated[
        str | None, "Last known focused_app_info (survives Cortex nulling)", take_last
    ] = None
    pending_success_path_subgoal: Annotated[
        str | None, "Subgoal description to record on next convergence", take_last
    ] = None

    async def asanitize_update(
        self,
        ctx: MobileUseContext,
        update: dict,
        agent: AgentNode | None = None,
    ):
        """
        Sanitizes the state update to ensure it is valid and apply side effect logic where required.
        The agent is required if the update contains the "agents_thoughts" key.
        """
        updated_agents_thoughts: str | list[str] | None = update.get("agents_thoughts", None)
        if updated_agents_thoughts is not None:
            if isinstance(updated_agents_thoughts, str):
                updated_agents_thoughts = [updated_agents_thoughts]
            elif isinstance(updated_agents_thoughts, list):
                updated_agents_thoughts = [t for t in updated_agents_thoughts if t is not None]
            else:
                raise ValueError("agents_thoughts must be a str or list[str]")

            if agent is None:
                raise ValueError("Agent is required when updating the 'agents_thoughts' key")
            update["agents_thoughts"] = await _add_agent_thoughts(
                ctx=ctx,
                old=self.agents_thoughts,
                new=updated_agents_thoughts,
                agent=agent,
            )
        return update


async def _add_agent_thoughts(
    ctx: MobileUseContext,
    old: list[str],
    new: list[str],
    agent: AgentNode,
) -> list[str]:
    if ctx.on_agent_thought:
        for thought in new:
            await ctx.on_agent_thought(agent, thought)

    named_thoughts = [f"[{agent}] {thought}" for thought in new]
    if (
        ctx.execution_setup
        and ctx.execution_setup.traces_path is not None
        and ctx.execution_setup.trace_name is not None
    ):
        await record_interaction(ctx, response=AIMessage(content=str(named_thoughts)))
    return old + named_thoughts
