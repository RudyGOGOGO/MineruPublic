from pathlib import Path

from jinja2 import Template
from langchain_core.messages import HumanMessage, SystemMessage

from mineru.ui_auto.agents.planner.types import PlannerOutput, Subgoal, SubgoalStatus
from mineru.ui_auto.agents.planner.utils import generate_id, one_of_them_is_failure
from mineru.ui_auto.context import MobileUseContext
from mineru.ui_auto.controllers.platform_specific_commands_controller import (
    get_current_foreground_package_async,
)
from mineru.ui_auto.graph.state import State
from mineru.ui_auto.services.llm import get_llm, invoke_llm_with_timeout_message, with_fallback
from mineru.ui_auto.tools.index import (
    EXECUTOR_WRAPPERS_TOOLS,
    VIDEO_RECORDING_WRAPPERS,
    format_tools_list,
)
from mineru.ui_auto.utils.decorators import wrap_with_callbacks
from mineru.ui_auto.utils.logger import get_logger

logger = get_logger(__name__)


class PlannerNode:
    def __init__(self, ctx: MobileUseContext):
        self.ctx = ctx

    @wrap_with_callbacks(
        before=lambda: logger.info("Starting Planner Agent..."),
        on_success=lambda _: logger.success("Planner Agent"),
        on_failure=lambda _: logger.error("Planner Agent"),
    )
    async def __call__(self, state: State):
        needs_replan = one_of_them_is_failure(state.subgoal_plan)

        # Record subgoal failure lessons on replan (gated behind lessons_dir)
        if needs_replan and self.ctx.lessons_dir:
            try:
                from mineru.ui_auto.lessons.recorder import record_subgoal_failure

                failed_subgoals = [
                    s for s in state.subgoal_plan if s.status == SubgoalStatus.FAILURE
                ]
                for sg in failed_subgoals:
                    await record_subgoal_failure(
                        lessons_dir=self.ctx.lessons_dir,
                        app_package=state.focused_app_info or "unknown",
                        subgoal_description=sg.description,
                        completion_reason=sg.completion_reason,
                        cortex_last_thought=state.cortex_last_thought,
                    )
            except Exception as e:
                logger.warning(f"Failed to record subgoal failure lesson: {e}")

        current_locked_app_package = (
            self.ctx.execution_setup.get_locked_app_package() if self.ctx.execution_setup else None
        )
        current_foreground_app = await get_current_foreground_package_async(self.ctx)

        # Load relevant lessons so the planner can use known navigation paths
        planner_lessons: str | None = None
        if self.ctx.lessons_dir and current_foreground_app:
            try:
                from mineru.ui_auto.lessons.loader import load_lessons_for_app

                planner_lessons = await load_lessons_for_app(
                    lessons_dir=self.ctx.lessons_dir,
                    app_package=current_foreground_app,
                    subgoal=state.initial_goal or "",
                    current_activity=None,
                    current_key_elements=[],
                )
                if planner_lessons:
                    logger.info(f"📚 Planner loaded lessons for {current_foreground_app}")
            except Exception:
                pass  # Lessons are optional for planning

        executor_wrappers = list(EXECUTOR_WRAPPERS_TOOLS)
        if self.ctx.video_recording_enabled:
            executor_wrappers.extend(VIDEO_RECORDING_WRAPPERS)

        system_message = Template(
            Path(__file__).parent.joinpath("planner.md").read_text(encoding="utf-8")
        ).render(
            platform=self.ctx.device.mobile_platform.value,
            executor_tools_list=format_tools_list(ctx=self.ctx, wrappers=executor_wrappers),
            locked_app_package=current_locked_app_package,
            current_foreground_app=current_foreground_app,
            video_recording_enabled=self.ctx.video_recording_enabled,
        )
        human_message = Template(
            Path(__file__).parent.joinpath("human.md").read_text(encoding="utf-8")
        ).render(
            action="replan" if needs_replan else "plan",
            initial_goal=state.initial_goal,
            previous_plan="\n".join(str(s) for s in state.subgoal_plan),
            agent_thoughts="\n".join(state.agents_thoughts),
            active_lessons=planner_lessons,
        )
        messages = [
            SystemMessage(content=system_message),
            HumanMessage(content=human_message),
        ]

        llm = get_llm(ctx=self.ctx, name="planner").with_structured_output(PlannerOutput)
        llm_fallback = get_llm(
            ctx=self.ctx, name="planner", use_fallback=True
        ).with_structured_output(PlannerOutput)
        response: PlannerOutput = await with_fallback(
            main_call=lambda: invoke_llm_with_timeout_message(
                llm.ainvoke(messages),
            ),
            fallback_call=lambda: invoke_llm_with_timeout_message(
                llm_fallback.ainvoke(messages),
            ),
        )  # type: ignore
        subgoals_plan = [
            Subgoal(
                id=generate_id(),
                description=subgoal.description,
                status=SubgoalStatus.NOT_STARTED,
                completion_reason=None,
            )
            for subgoal in response.subgoals
        ]
        logger.info("📜 Generated plan:")
        logger.info("\n".join(str(s) for s in subgoals_plan))

        if self.ctx.on_plan_changes:
            await self.ctx.on_plan_changes(subgoals_plan, needs_replan)

        return await state.asanitize_update(
            ctx=self.ctx,
            update={
                "subgoal_plan": subgoals_plan,
            },
            agent="planner",
        )
