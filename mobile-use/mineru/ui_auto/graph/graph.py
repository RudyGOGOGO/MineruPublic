from collections.abc import Sequence
from typing import Literal

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph

from mineru.ui_auto.agents.contextor.contextor import ContextorNode
from mineru.ui_auto.agents.cortex.cortex import CortexNode
from mineru.ui_auto.agents.executor.executor import ExecutorNode
from mineru.ui_auto.agents.executor.tool_node import ExecutorToolNode
from mineru.ui_auto.agents.orchestrator.orchestrator import OrchestratorNode
from mineru.ui_auto.agents.planner.planner import PlannerNode
from mineru.ui_auto.agents.planner.utils import (
    all_completed,
    get_current_subgoal,
    one_of_them_is_failure,
)
from mineru.ui_auto.agents.summarizer.summarizer import SummarizerNode
from mineru.ui_auto.constants import EXECUTOR_MESSAGES_KEY
from mineru.ui_auto.context import MobileUseContext
from mineru.ui_auto.graph.state import State
from mineru.ui_auto.tools.index import (
    EXECUTOR_WRAPPERS_TOOLS,
    VIDEO_RECORDING_WRAPPERS,
    get_tools_from_wrappers,
)
from mineru.ui_auto.utils.logger import get_logger

logger = get_logger(__name__)


def convergence_gate(
    state: State,
) -> Literal["continue", "replan", "end"]:
    """Check if all subgoals are completed at convergence point."""
    logger.info("Starting convergence_gate")

    if one_of_them_is_failure(state.subgoal_plan):
        logger.info("One of the subgoals is in failure state, asking to replan")
        return "replan"

    if all_completed(state.subgoal_plan):
        logger.info("All subgoals are completed, ending the goal")
        return "end"

    if not get_current_subgoal(state.subgoal_plan):
        logger.info("No subgoal running, ending the goal")
        return "end"

    return "continue"


def post_cortex_gate(
    state: State,
) -> Sequence[str]:
    logger.info("Starting post_cortex_gate")
    node_sequence = []

    if len(state.complete_subgoals_by_ids) > 0 or not state.structured_decisions:
        # If subgoals need to be marked as complete, add the path to the orchestrator.
        # The 'or not state.structured_decisions' ensures we don't get stuck if Cortex does nothing.
        node_sequence.append("review_subgoals")

    if state.structured_decisions:
        node_sequence.append("execute_decisions")

    return node_sequence


def post_executor_gate(
    state: State,
) -> Literal["invoke_tools", "skip"]:
    logger.info("Starting post_executor_gate")
    messages = state.executor_messages
    if not messages:
        return "skip"
    last_message = messages[-1]

    if isinstance(last_message, AIMessage):
        tool_calls = getattr(last_message, "tool_calls", None)
        if tool_calls and len(tool_calls) > 0:
            logger.info("[executor] Executing " + str(len(tool_calls)) + " tool calls:")
            for tool_call in tool_calls:
                logger.info("-------------")
                logger.info("[executor] - " + str(tool_call) + "\n")
            logger.info("-------------")
            return "invoke_tools"
        else:
            logger.info("[executor] ❌ No tool calls found")
    return "skip"


def post_executor_tools_node(state: State):
    """Extract last tool name/status and append to action trail for lesson recording."""
    tool_messages = [m for m in state.executor_messages if isinstance(m, ToolMessage)]
    if not tool_messages:
        return {}

    last_msg = tool_messages[-1]
    update = {
        "last_tool_name": getattr(last_msg, "name", None),
        "last_tool_status": getattr(last_msg, "status", None),
    }

    # Append structured action data to trail (for success path recording).
    # Read from AIMessage.tool_calls — these contain the structured args
    # (name, target text, resource_id) that we need.
    ai_messages = [m for m in state.executor_messages if isinstance(m, AIMessage)]
    if ai_messages:
        last_ai = ai_messages[-1]
        tool_calls = getattr(last_ai, "tool_calls", [])
        new_trail_entries = []
        for i, tc in enumerate(tool_calls):
            # Pair with corresponding ToolMessage for status
            tm = tool_messages[i] if i < len(tool_messages) else None
            entry = _build_trail_entry(tc, tm)
            if entry:
                new_trail_entries.append(entry)
        if new_trail_entries:
            update["action_trail"] = state.action_trail + new_trail_entries

    return update


def _build_trail_entry(tool_call: dict, tool_message: ToolMessage | None) -> dict | None:
    """Build a trail entry from a structured tool call + its result.

    Only records navigation-relevant actions (tap, launch_app,
    open_link, back, press_key with Back). Skips swipes (scrolling noise),
    wait_for_delay, video_recording, scratchpad, etc.
    """
    name = tool_call.get("name", "")
    args = tool_call.get("args", {})
    status = getattr(tool_message, "status", None) if tool_message else None

    # Normalize back actions: press_key(Back) and back() are both "back"
    if name == "press_key" and args.get("key", "").lower() == "back":
        action = "back"
    elif name == "back":
        action = "back"
    elif name in ("tap", "long_press_on"):
        action = "tap"
    elif name in ("swipe", "swipe_percentages"):
        return None  # Exclude swipes — scrolling, not navigation choices
    elif name == "launch_app":
        action = "launch_app"
    elif name == "open_link":
        action = "open_link"
    else:
        return None  # Not a navigation action

    # Extract target info from structured args
    target = args.get("target", {})
    target_text = target.get("text") if isinstance(target, dict) else None
    target_resource_id = target.get("resource_id") if isinstance(target, dict) else None

    # For launch_app, the target is the app name
    if name == "launch_app":
        target_text = args.get("app_name")

    # For open_link, the target is the URL
    if name == "open_link":
        target_text = args.get("url")

    return {
        "action": action,
        "target_text": target_text,
        "target_resource_id": target_resource_id,
        "status": status,  # "success" or "error"
        "agent_thought": args.get("agent_thought", ""),
    }


async def get_graph(ctx: MobileUseContext) -> CompiledStateGraph:
    graph_builder = StateGraph(State)

    ## Define nodes
    graph_builder.add_node("planner", PlannerNode(ctx))
    graph_builder.add_node("orchestrator", OrchestratorNode(ctx))

    graph_builder.add_node("contextor", ContextorNode(ctx))

    graph_builder.add_node("cortex", CortexNode(ctx))

    graph_builder.add_node("executor", ExecutorNode(ctx))

    executor_wrappers = list(EXECUTOR_WRAPPERS_TOOLS)
    if ctx.video_recording_enabled:
        executor_wrappers.extend(VIDEO_RECORDING_WRAPPERS)

    executor_tool_node = ExecutorToolNode(
        tools=get_tools_from_wrappers(ctx=ctx, wrappers=executor_wrappers),
        messages_key=EXECUTOR_MESSAGES_KEY,
        trace_id=ctx.trace_id,
    )
    graph_builder.add_node("executor_tools", executor_tool_node)

    graph_builder.add_node("summarizer", SummarizerNode(ctx))

    async def _convergence_node(state: State):
        """Convergence point for parallel execution paths.
        Records success paths and resets the action trail."""
        if (
            ctx.lessons_dir
            and state.pending_success_path_subgoal
            and state.last_focused_app
            and state.action_trail
        ):
            try:
                from mineru.ui_auto.lessons.recorder import record_success_path

                await record_success_path(
                    lessons_dir=ctx.lessons_dir,
                    app_package=state.last_focused_app,
                    subgoal_description=state.pending_success_path_subgoal,
                    action_trail=state.action_trail,
                )
            except Exception as e:
                logger.warning(f"Failed to record success path: {e}")
            # Clear the flag AND reset the trail (for the next subgoal).
            return {
                "pending_success_path_subgoal": None,
                "action_trail": [],
            }
        return {}

    graph_builder.add_node(node="convergence", action=_convergence_node, defer=True)

    ## Linking nodes
    graph_builder.add_edge(START, "planner")
    graph_builder.add_edge("planner", "orchestrator")
    graph_builder.add_edge("orchestrator", "convergence")
    graph_builder.add_edge("contextor", "cortex")
    graph_builder.add_conditional_edges(
        "cortex",
        post_cortex_gate,
        {
            "review_subgoals": "orchestrator",
            "execute_decisions": "executor",
        },
    )
    graph_builder.add_conditional_edges(
        "executor",
        post_executor_gate,
        {"invoke_tools": "executor_tools", "skip": "summarizer"},
    )
    graph_builder.add_node("post_executor_tools", post_executor_tools_node)
    graph_builder.add_edge("executor_tools", "post_executor_tools")
    graph_builder.add_edge("post_executor_tools", "summarizer")

    graph_builder.add_edge("summarizer", "convergence")

    graph_builder.add_conditional_edges(
        source="convergence",
        path=convergence_gate,
        path_map={
            "continue": "contextor",
            "replan": "planner",
            "end": END,
        },
    )

    return graph_builder.compile()
