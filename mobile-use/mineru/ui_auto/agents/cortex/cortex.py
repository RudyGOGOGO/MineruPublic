import json
import re
from pathlib import Path

from jinja2 import Template
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from mineru.ui_auto.agents.cortex.types import CortexOutput
from mineru.ui_auto.agents.planner.utils import get_current_subgoal
from mineru.ui_auto.constants import EXECUTOR_MESSAGES_KEY
from mineru.ui_auto.context import MobileUseContext
from mineru.ui_auto.controllers.controller_factory import create_device_controller
from mineru.ui_auto.graph.state import State
from mineru.ui_auto.services.llm import get_llm, invoke_llm_with_timeout_message, with_fallback
from mineru.ui_auto.services.telemetry import telemetry
from mineru.ui_auto.tools.index import (
    EXECUTOR_WRAPPERS_TOOLS,
    VIDEO_RECORDING_WRAPPERS,
    format_tools_list,
)
from mineru.ui_auto.utils.conversations import get_screenshot_message_for_llm
from mineru.ui_auto.utils.decorators import wrap_with_callbacks
from mineru.ui_auto.utils.logger import get_logger

logger = get_logger(__name__)


class CortexNode:
    def __init__(self, ctx: MobileUseContext):
        self.ctx = ctx

    @wrap_with_callbacks(
        before=lambda: logger.info("Starting Cortex Agent..."),
        on_success=lambda _: logger.success("Cortex Agent"),
        on_failure=lambda _: logger.error("Cortex Agent"),
    )
    async def __call__(self, state: State):
        executor_feedback = get_executor_agent_feedback(state)

        current_locked_app_package = (
            self.ctx.execution_setup.get_locked_app_package() if self.ctx.execution_setup else None
        )

        executor_wrappers = list(EXECUTOR_WRAPPERS_TOOLS)
        if self.ctx.video_recording_enabled:
            executor_wrappers.extend(VIDEO_RECORDING_WRAPPERS)

        system_message = Template(
            Path(__file__).parent.joinpath("cortex.md").read_text(encoding="utf-8")
        ).render(
            platform=self.ctx.device.mobile_platform.value,
            initial_goal=state.initial_goal,
            subgoal_plan=state.subgoal_plan,
            current_subgoal=get_current_subgoal(state.subgoal_plan),
            executor_feedback=executor_feedback,
            executor_tools_list=format_tools_list(ctx=self.ctx, wrappers=executor_wrappers),
            locked_app_package=current_locked_app_package,
            gui_owl_enabled=self.ctx.gui_owl_enabled,
            perception_mode=self.ctx.perception_mode,
            focused_app=state.focused_app_info,
            active_lessons=state.active_lessons,
        )
        messages = [
            SystemMessage(content=system_message),
            HumanMessage(
                content="Here are my device info:\n"
                + self.ctx.device.to_str()
                + f"Device date: {state.device_date}\n"
                if state.device_date
                else "" + f"Focused app info: {state.focused_app_info}\n"
                if state.focused_app_info
                else ""
            ),
        ]
        for thought in state.agents_thoughts:
            messages.append(AIMessage(content=thought))

        if state.latest_ui_hierarchy:
            ui_hierarchy_dict: list[dict] = state.latest_ui_hierarchy
            ui_hierarchy_str = json.dumps(ui_hierarchy_dict, indent=2, ensure_ascii=False)
            messages.append(HumanMessage(content="Here is the UI hierarchy:\n" + ui_hierarchy_str))

        if state.latest_screenshot:
            controller = create_device_controller(self.ctx)
            compressed_image_base64 = controller.get_compressed_b64_screenshot(
                state.latest_screenshot
            )
            messages.append(get_screenshot_message_for_llm(compressed_image_base64))

            # Enhanced perception: replace screenshot with SoM version and inject element list
            if self.ctx.perception_mode == "enhanced" and state.som_screenshot_b64:
                messages[-1] = get_screenshot_message_for_llm(state.som_screenshot_b64)
                if state.unified_element_list:
                    messages.insert(-1, HumanMessage(
                        content=f"Unified Element List ({state.unified_element_count} elements):\n"
                                f"{state.unified_element_list}"
                    ))

        llm = get_llm(ctx=self.ctx, name="cortex", temperature=1).with_structured_output(
            CortexOutput
        )
        llm_fallback = get_llm(
            ctx=self.ctx, name="cortex", use_fallback=True, temperature=1
        ).with_structured_output(CortexOutput)
        response: CortexOutput = await with_fallback(
            main_call=lambda: invoke_llm_with_timeout_message(llm.ainvoke(messages)),
            fallback_call=lambda: invoke_llm_with_timeout_message(llm_fallback.ainvoke(messages)),
        )  # type: ignore

        EMPTY_STRING_TOKENS = ["{}", "[]", "null", "", "None"]

        if response.decisions in EMPTY_STRING_TOKENS:
            response.decisions = None
        if response.goals_completion_reason in EMPTY_STRING_TOKENS:
            response.goals_completion_reason = None

        thought_parts = []
        if response.decisions_reason:
            thought_parts.append(f"Decisions reason: {response.decisions_reason}")
        if response.goals_completion_reason:
            thought_parts.append(f"Goals completion reason: {response.goals_completion_reason}")

        agent_thought = "\n\n".join(thought_parts)

        # Phase 2: Parse lesson feedback from decisions_reason
        applied_ids: list[str] = []
        failed_ids: list[str] = []
        if response.decisions_reason:
            applied_ids = re.findall(
                r'applied_lesson:\s*"([^"]+)"', response.decisions_reason
            )
            failed_ids = re.findall(
                r'lesson_failed:\s*"([^"]+)"', response.decisions_reason
            )

        # Capture cortex decision telemetry (only non-sensitive flags)
        telemetry.capture_cortex_decision(
            task_id=self.ctx.trace_id,
            has_decisions=response.decisions is not None,
            has_goals_completion=response.goals_completion_reason is not None,
            completed_subgoals_count=len(response.complete_subgoals_by_ids or []),
        )

        return await state.asanitize_update(
            ctx=self.ctx,
            update={
                "agents_thoughts": [agent_thought],
                "structured_decisions": response.decisions,
                "complete_subgoals_by_ids": response.complete_subgoals_by_ids,
                "latest_ui_hierarchy": None,
                "latest_screenshot": None,
                "focused_app_info": None,
                "device_date": None,
                "last_tool_name": None,
                "last_tool_status": None,
                "applied_lesson_ids": applied_ids,
                "failed_lesson_ids": failed_ids,
                # Executor related fields
                EXECUTOR_MESSAGES_KEY: [RemoveMessage(id=REMOVE_ALL_MESSAGES)],
                "cortex_last_thought": agent_thought,
            },
            agent="cortex",
        )


def get_executor_agent_feedback(state: State) -> str:
    if state.structured_decisions is None:
        return "None."
    executor_tool_messages = [m for m in state.executor_messages if isinstance(m, ToolMessage)]
    return (
        f"Latest UI decisions:\n{state.structured_decisions}"
        + "\n\n"
        + f"Executor feedback:\n{executor_tool_messages}"
    )
