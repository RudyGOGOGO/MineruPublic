import asyncio
import os
from enum import StrEnum
from shutil import which
from typing import Annotated

import typer
from adbutils import AdbClient
from langchain_core.callbacks.base import Callbacks
from rich.console import Console

from mineru.ui_auto.clients.ios_client_config import (
    IdbClientConfig,
    IosClientConfig,
    WdaClientConfig,
)
from mineru.ui_auto.clients.limrun_factory import (
    LimrunInstanceConfig,
    LimrunPlatform,
    create_limrun_android_instance,
    create_limrun_ios_instance,
    delete_limrun_android_instance,
    delete_limrun_ios_instance,
)
from mineru.ui_auto.config import (
    get_claude_llm_config,
    get_gui_owl_llm_config,
    initialize_llm_config,
    settings,
)
from mineru.ui_auto.sdk import Agent
from mineru.ui_auto.sdk.builders import Builders
from mineru.ui_auto.sdk.types.task import AgentProfile
from mineru.ui_auto.services.telemetry import telemetry
from mineru.ui_auto.utils.cli_helpers import display_device_status
from mineru.ui_auto.utils.logger import get_logger
from mineru.ui_auto.utils.video import check_ffmpeg_available

app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)
logger = get_logger(__name__)


class ModelProvider(StrEnum):
    """Model provider preset for ui-auto agent."""

    DEFAULT = "default"
    CLAUDE = "claude"
    GUI_OWL = "gui_owl"


class DeviceType(StrEnum):
    """Device type for ui-auto agent."""

    LOCAL = "local"
    LIMRUN = "limrun"


async def run_automation(
    goal: str,
    locked_app_package: str | None = None,
    test_name: str | None = None,
    traces_output_path_str: str = "traces",
    output_description: str | None = None,
    graph_config_callbacks: Callbacks = [],
    video_recording_tools_enabled: bool = False,
    lessons_dir: str | None = None,
    wda_url: str | None = None,
    wda_timeout: float | None = None,
    wda_auto_start_iproxy: bool | None = None,
    wda_auto_start_wda: bool | None = None,
    wda_project_path: str | None = None,
    wda_startup_timeout: float | None = None,
    idb_host: str | None = None,
    idb_port: int | None = None,
    device_type: DeviceType = DeviceType.LOCAL,
    limrun_platform: LimrunPlatform | None = None,
    model_provider: ModelProvider = ModelProvider.DEFAULT,
    claude_model: str | None = None,
):
    if claude_model:
        settings.CLAUDE_MODEL = claude_model
        logger.info(f"Using Claude model: {claude_model}")

    if model_provider == ModelProvider.GUI_OWL:
        llm_config = get_gui_owl_llm_config()
        logger.info("Using GUI Owl VLM model (local) with Claude fallback")
    elif model_provider == ModelProvider.CLAUDE:
        llm_config = get_claude_llm_config()
        logger.info("Using Claude CLI for all agents")
    else:
        llm_config = initialize_llm_config()
    agent_profile = AgentProfile(name="default", llm_config=llm_config)
    config = Builders.AgentConfig.with_default_profile(profile=agent_profile)
    if video_recording_tools_enabled:
        config.with_video_recording_tools()
    if model_provider == ModelProvider.GUI_OWL:
        config.with_gui_owl()

    # Lesson-learned memory: resolve from CLI flag or env var
    resolved_lessons_dir = lessons_dir or os.getenv("MOBILE_USE_LESSONS_DIR")
    if resolved_lessons_dir:
        config.with_lessons_dir(resolved_lessons_dir)
        logger.info(f"Lesson-learned memory enabled: {resolved_lessons_dir}")

    # Limrun device provisioning
    limrun_instance_id: str | None = None
    limrun_controller = None
    limrun_config: LimrunInstanceConfig | None = None

    if device_type == DeviceType.LIMRUN:
        if limrun_platform is None:
            raise ValueError("--limrun-platform is required when using --device-type limrun")

        logger.info(f"Provisioning Limrun {limrun_platform.value} device...")
        limrun_config = LimrunInstanceConfig()

        if limrun_platform == LimrunPlatform.ANDROID:
            instance, limrun_controller = await create_limrun_android_instance(limrun_config)
            limrun_instance_id = instance.metadata.id
            await limrun_controller.connect()
            config.with_limrun_android_controller(limrun_controller)
        else:
            instance, _, limrun_controller = await create_limrun_ios_instance(limrun_config)
            limrun_instance_id = instance.metadata.id
            # Connection is done in the factory, no need to call connect()
            config.with_limrun_ios_controller(limrun_controller)

        logger.info(f"Limrun {limrun_platform.value} device ready: {limrun_instance_id}")
    else:
        # Build iOS client config from CLI options (local device)
        wda_config = WdaClientConfig.with_overrides(
            wda_url=wda_url,
            timeout=wda_timeout,
            auto_start_iproxy=wda_auto_start_iproxy,
            auto_start_wda=wda_auto_start_wda,
            wda_project_path=wda_project_path,
            wda_startup_timeout=wda_startup_timeout,
        )
        idb_config = IdbClientConfig.with_overrides(host=idb_host, port=idb_port)
        config.with_ios_client_config(IosClientConfig(wda=wda_config, idb=idb_config))

        if settings.ADB_HOST:
            config.with_adb_server(host=settings.ADB_HOST, port=settings.ADB_PORT)

    if graph_config_callbacks:
        config.with_graph_config_callbacks(graph_config_callbacks)

    agent: Agent | None = None
    try:
        agent = Agent(config=config.build())
        await agent.init(
            retry_count=int(os.getenv("MOBILE_USE_HEALTH_RETRIES", 5)),
            retry_wait_seconds=int(os.getenv("MOBILE_USE_HEALTH_DELAY", 2)),
        )

        task = agent.new_task(goal)
        if locked_app_package:
            task.with_locked_app_package(locked_app_package)
        if test_name:
            task.with_name(test_name).with_trace_recording(path=traces_output_path_str)
        if output_description:
            task.with_output_description(output_description)

        agent_thoughts_path = os.getenv("EVENTS_OUTPUT_PATH", None)
        llm_result_path = os.getenv("RESULTS_OUTPUT_PATH", None)
        if agent_thoughts_path:
            task.with_thoughts_output_saving(path=agent_thoughts_path)
        if llm_result_path:
            task.with_llm_output_saving(path=llm_result_path)

        await agent.run_task(request=task.build())
    finally:
        if agent is not None:
            await agent.clean()

        # Cleanup Limrun device
        if limrun_instance_id and limrun_config:
            logger.info(f"Cleaning up Limrun device: {limrun_instance_id}")
            if limrun_controller:
                await limrun_controller.cleanup()
            if limrun_platform == LimrunPlatform.ANDROID:
                await delete_limrun_android_instance(limrun_config, limrun_instance_id)
            else:
                await delete_limrun_ios_instance(limrun_config, limrun_instance_id)


@app.command()
def main(
    goal: Annotated[str, typer.Argument(help="The main goal for the agent to achieve.")],
    test_name: Annotated[
        str | None,
        typer.Option(
            "--test-name",
            "-n",
            help="A name for the test recording. If provided, a trace will be saved.",
        ),
    ] = None,
    traces_path: Annotated[
        str,
        typer.Option(
            "--traces-path",
            "-p",
            help="The path to save the traces.",
        ),
    ] = "traces",
    output_description: Annotated[
        str | None,
        typer.Option(
            "--output-description",
            "-o",
            help=(
                """
                A dict output description for the agent.
                Ex: a JSON schema with 2 keys: type, price
                """
            ),
        ),
    ] = None,
    wda_url: Annotated[
        str | None,
        typer.Option(
            "--wda-url",
            help="Override WebDriverAgent URL (e.g. http://localhost:8100).",
        ),
    ] = None,
    wda_timeout: Annotated[
        float | None,
        typer.Option(
            "--wda-timeout",
            help="Timeout (seconds) for WDA operations.",
        ),
    ] = None,
    wda_auto_start_iproxy: Annotated[
        bool | None,
        typer.Option(
            "--wda-auto-start-iproxy/--no-wda-auto-start-iproxy",
            help="Auto-start iproxy if not running.",
        ),
    ] = None,
    wda_auto_start_wda: Annotated[
        bool | None,
        typer.Option(
            "--wda-auto-start-wda/--no-wda-auto-start-wda",
            help="Auto-build and run WDA via xcodebuild if not responding.",
        ),
    ] = None,
    wda_project_path: Annotated[
        str | None,
        typer.Option(
            "--wda-project-path",
            help="Path to WebDriverAgent.xcodeproj.",
        ),
    ] = None,
    wda_startup_timeout: Annotated[
        float | None,
        typer.Option(
            "--wda-startup-timeout",
            help="Timeout (seconds) while waiting for WDA to start.",
        ),
    ] = None,
    idb_host: Annotated[
        str | None,
        typer.Option(
            "--idb-host",
            help="IDB companion host (for simulators).",
        ),
    ] = None,
    idb_port: Annotated[
        int | None,
        typer.Option(
            "--idb-port",
            help="IDB companion port (for simulators).",
        ),
    ] = None,
    with_video_recording_tools: Annotated[
        bool,
        typer.Option(
            "--with-video-recording-tools",
            help="Enable AI agents to use video recording tools"
            " to analyze dynamic content on the screen.",
        ),
    ] = False,
    device_type: Annotated[
        DeviceType,
        typer.Option(
            "--device-type",
            "-d",
            help="Device type: 'local' for connected devices, 'limrun' for cloud devices.",
        ),
    ] = DeviceType.LOCAL,
    limrun_platform: Annotated[
        LimrunPlatform | None,
        typer.Option(
            "--limrun-platform",
            help="Platform for Limrun cloud device: 'android' or 'ios'. "
            "Required when --device-type is 'limrun'.",
        ),
    ] = None,
    model_provider: Annotated[
        ModelProvider,
        typer.Option(
            "--model-provider",
            "-m",
            help="Model provider preset: 'default' (from config files), "
            "'claude' (Claude CLI for all agents), "
            "'gui_owl' (local GUI Owl VLM for vision agents, Claude for others).",
        ),
    ] = ModelProvider.DEFAULT,
    claude_model: Annotated[
        str | None,
        typer.Option(
            "--claude-model",
            help="Claude model ID to use. "
            "Examples: claude-haiku-4-5-20251001, claude-sonnet-4-6, claude-opus-4-6. "
            "Overrides CLAUDE_MODEL env var.",
        ),
    ] = None,
    lessons_dir: Annotated[
        str | None,
        typer.Option(
            "--lessons-dir",
            help="Directory for lesson-learned memory. "
            "Enables recording mistakes, strategies, and success paths across sessions. "
            "Overrides MOBILE_USE_LESSONS_DIR env var.",
        ),
    ] = None,
):
    """
    Run the Mobile-use agent to automate tasks on a mobile device.
    """
    if with_video_recording_tools:
        check_ffmpeg_available()

    console = Console()

    if device_type == DeviceType.LOCAL:
        adb_client = None
        try:
            if which("adb"):
                adb_client = AdbClient(
                    host=settings.ADB_HOST or "localhost",
                    port=settings.ADB_PORT or 5037,
                )
        except Exception:
            pass  # ADB not available, will only support iOS devices

        display_device_status(console, adb_client=adb_client)
    else:
        if limrun_platform is None:
            console.print(
                "[red]Error: --limrun-platform is required when using --device-type limrun[/red]"
            )
            raise typer.Exit(1)
        console.print(f"[cyan]Using Limrun cloud device ({limrun_platform.value})...[/cyan]")

    # Start telemetry session with CLI context (only non-sensitive flags)
    session_id = telemetry.start_session(
        {
            "source": "cli",
            "has_output_description": output_description is not None,
        }
    )

    error_message = None
    cancelled = False
    try:
        asyncio.run(
            run_automation(
                goal=goal,
                test_name=test_name,
                traces_output_path_str=traces_path,
                output_description=output_description,
                wda_url=wda_url,
                wda_timeout=wda_timeout,
                wda_auto_start_iproxy=wda_auto_start_iproxy,
                wda_auto_start_wda=wda_auto_start_wda,
                wda_project_path=wda_project_path,
                wda_startup_timeout=wda_startup_timeout,
                idb_host=idb_host,
                idb_port=idb_port,
                video_recording_tools_enabled=with_video_recording_tools,
                lessons_dir=lessons_dir,
                device_type=device_type,
                limrun_platform=limrun_platform,
                model_provider=model_provider,
                claude_model=claude_model,
            )
        )
    except KeyboardInterrupt:
        cancelled = True
        error_message = "Task cancelled by user"
    except Exception as e:
        error_message = str(e)
        console.print(
            f"\n[dim]If you need support, please include this session ID: {session_id}[/dim]"
        )
        raise
    finally:
        telemetry.end_session(
            success=error_message is None,
            error=error_message,
        )
        if cancelled:
            raise SystemExit(130)


async def run_learn(
    app_package: str,
    lessons_dir: str,
    sessions: int = 1,
    budget_minutes: int = 30,
    max_depth: int = 4,
    strategy: str = "auto",
    reset: bool = False,
    model_provider: ModelProvider = ModelProvider.DEFAULT,
    claude_model: str | None = None,
) -> None:
    """Run self-learning exploration for an app.

    Creates an Agent, initializes it to set up device connections, then
    builds a MobileUseContext from the agent's internals. The context is
    reused across sessions; the agent is recreated per session inside
    run_exploration_session.
    """
    from pathlib import Path

    from mineru.ui_auto.context import MobileUseContext
    from mineru.ui_auto.exploration.runner import run_exploration_session

    if claude_model:
        settings.CLAUDE_MODEL = claude_model
        logger.info(f"Using Claude model: {claude_model}")

    if model_provider == ModelProvider.GUI_OWL:
        llm_config = get_gui_owl_llm_config()
    elif model_provider == ModelProvider.CLAUDE:
        llm_config = get_claude_llm_config()
    else:
        llm_config = initialize_llm_config()

    agent_profile = AgentProfile(name="default", llm_config=llm_config)
    lessons_path = Path(lessons_dir)

    if reset:
        exploration_file = lessons_path / app_package / "_exploration.json"
        if exploration_file.exists():
            exploration_file.unlink()
            logger.info(f"Reset exploration state for {app_package}")

    # Initialize an agent to set up device connections (ADB, UIAutomator, etc.)
    init_config = Builders.AgentConfig.with_default_profile(profile=agent_profile)
    init_config.with_lessons_dir(str(lessons_path))
    if settings.ADB_HOST:
        init_config.with_adb_server(host=settings.ADB_HOST, port=settings.ADB_PORT)

    init_agent = Agent(config=init_config.build())
    await init_agent.init()

    # Build a MobileUseContext from the agent's initialized device state.
    # The agent sets up _device_context, _adb_client, _ui_adb_client, _ios_client
    # during init(). We extract these to build a context for the exploration loop.
    ctx = MobileUseContext(
        trace_id="exploration",
        device=init_agent._device_context,
        adb_client=init_agent._adb_client,
        ui_adb_client=init_agent._ui_adb_client,
        ios_client=init_agent._ios_client,
        llm_config=agent_profile.llm_config,
        lessons_dir=lessons_path,
        exploration_mode=True,
    )
    await init_agent.clean()

    for i in range(sessions):
        logger.info(f"\n{'=' * 60}")
        logger.info(f"Session {i + 1}/{sessions}")
        logger.info(f"{'=' * 60}")

        # Rebuild config for each session (agent is created inside run_exploration_session)
        session_config = Builders.AgentConfig.with_default_profile(profile=agent_profile)
        session_config.with_lessons_dir(str(lessons_path))
        session_config.with_exploration_mode()
        if settings.ADB_HOST:
            session_config.with_adb_server(host=settings.ADB_HOST, port=settings.ADB_PORT)

        await run_exploration_session(
            app_package=app_package,
            lessons_dir=lessons_path,
            ctx=ctx,
            config_builder=session_config,
            budget_minutes=budget_minutes,
            max_depth=max_depth,
            strategy=strategy,
        )


@app.command("learn")
def learn(
    app_package: Annotated[
        str,
        typer.Argument(help="The Android package to explore (e.g., com.android.settings)."),
    ],
    lessons_dir: Annotated[
        str,
        typer.Option(
            "--lessons-dir",
            help="Directory for lessons and exploration state.",
        ),
    ],
    sessions: Annotated[
        int,
        typer.Option(
            "--sessions",
            help="Number of sessions to run.",
        ),
    ] = 1,
    budget_minutes: Annotated[
        int,
        typer.Option(
            "--budget-minutes",
            help="Time budget per session in minutes.",
        ),
    ] = 30,
    max_depth: Annotated[
        int,
        typer.Option(
            "--max-depth",
            help="Maximum depth in the feature tree.",
        ),
    ] = 4,
    strategy: Annotated[
        str,
        typer.Option(
            "--strategy",
            help="Node selection: 'auto', 'breadth_first', or 'depth_first'.",
        ),
    ] = "auto",
    reset: Annotated[
        bool,
        typer.Option(
            "--reset",
            help="Discard existing exploration state and start fresh.",
        ),
    ] = False,
    model_provider: Annotated[
        ModelProvider,
        typer.Option(
            "--model-provider",
            "-m",
            help="Model provider preset: 'default', 'claude', or 'gui_owl'.",
        ),
    ] = ModelProvider.DEFAULT,
    claude_model: Annotated[
        str | None,
        typer.Option(
            "--claude-model",
            help="Claude model ID to use.",
        ),
    ] = None,
) -> None:
    """Explore an app's UI to build navigation knowledge before real tasks."""
    console = Console()
    console.print(f"[cyan]Starting self-learning exploration for {app_package}...[/cyan]")

    session_id = telemetry.start_session({"source": "cli", "command": "learn"})

    error_message = None
    try:
        asyncio.run(
            run_learn(
                app_package=app_package,
                lessons_dir=lessons_dir,
                sessions=sessions,
                budget_minutes=budget_minutes,
                max_depth=max_depth,
                strategy=strategy,
                reset=reset,
                model_provider=model_provider,
                claude_model=claude_model,
            )
        )
    except KeyboardInterrupt:
        error_message = "Exploration cancelled by user"
        console.print("[yellow]Exploration cancelled. Progress has been saved.[/yellow]")
    except Exception as e:
        error_message = str(e)
        console.print(
            f"\n[dim]If you need support, please include this session ID: {session_id}[/dim]"
        )
        raise
    finally:
        telemetry.end_session(success=error_message is None, error=error_message)


async def run_learn_cluster(
    primary: str,
    secondary: str,
    primary_device: str | None,
    secondary_device: str | None,
    lessons_dir: str,
    sessions: int = 1,
    budget_minutes: int = 45,
    max_depth: int = 4,
    observer_timeout: float = 10.0,
    trigger_hints: str | None = None,
    reset: bool = False,
    model_provider: ModelProvider = ModelProvider.DEFAULT,
    claude_model: str | None = None,
) -> None:
    """Run dual-app cluster exploration for two linked apps."""
    from pathlib import Path

    from mineru.ui_auto.context import MobileUseContext
    from mineru.ui_auto.exploration.orchestrator import MasterOrchestrator

    if claude_model:
        settings.CLAUDE_MODEL = claude_model
        logger.info(f"Using Claude model: {claude_model}")

    if model_provider == ModelProvider.GUI_OWL:
        llm_config = get_gui_owl_llm_config()
    elif model_provider == ModelProvider.CLAUDE:
        llm_config = get_claude_llm_config()
    else:
        llm_config = initialize_llm_config()

    agent_profile = AgentProfile(name="default", llm_config=llm_config)
    lessons_path = Path(lessons_dir)

    if reset:
        for pkg in [primary, secondary]:
            exploration_file = lessons_path / pkg / "_exploration.json"
            if exploration_file.exists():
                exploration_file.unlink()
                logger.info(f"Reset exploration state for {pkg}")

    # Initialize primary agent to get device connections
    primary_config = Builders.AgentConfig.with_default_profile(profile=agent_profile)
    primary_config.with_lessons_dir(str(lessons_path))
    if settings.ADB_HOST:
        primary_config.with_adb_server(host=settings.ADB_HOST, port=settings.ADB_PORT)

    init_agent = Agent(config=primary_config.build())
    await init_agent.init()

    primary_ctx = MobileUseContext(
        trace_id="exploration-primary",
        device=init_agent._device_context,
        adb_client=init_agent._adb_client,
        ui_adb_client=init_agent._ui_adb_client,
        ios_client=init_agent._ios_client,
        llm_config=agent_profile.llm_config,
        lessons_dir=lessons_path,
        exploration_mode=True,
    )
    await init_agent.clean()

    # For secondary device: reuse same context if same device, else create new
    if secondary_device and secondary_device != primary_device:
        # Create a separate agent for the secondary device
        secondary_config = Builders.AgentConfig.with_default_profile(profile=agent_profile)
        secondary_config.with_lessons_dir(str(lessons_path))
        if settings.ADB_HOST:
            secondary_config.with_adb_server(host=settings.ADB_HOST, port=settings.ADB_PORT)

        sec_agent = Agent(config=secondary_config.build())
        await sec_agent.init()

        secondary_ctx = MobileUseContext(
            trace_id="exploration-secondary",
            device=sec_agent._device_context,
            adb_client=sec_agent._adb_client,
            ui_adb_client=sec_agent._ui_adb_client,
            ios_client=sec_agent._ios_client,
            llm_config=agent_profile.llm_config,
            lessons_dir=lessons_path,
            exploration_mode=True,
        )
        await sec_agent.clean()
    else:
        secondary_ctx = primary_ctx  # Same device -- agents share the context

    hint_set = {h.strip() for h in trigger_hints.split(",")} if trigger_hints else None

    for i in range(sessions):
        logger.info(f"\n{'=' * 60}")
        logger.info(f"Cluster Session {i + 1}/{sessions}")
        logger.info(f"{'=' * 60}")

        # Build config builders for each session
        p_config = Builders.AgentConfig.with_default_profile(profile=agent_profile)
        p_config.with_lessons_dir(str(lessons_path))
        if settings.ADB_HOST:
            p_config.with_adb_server(host=settings.ADB_HOST, port=settings.ADB_PORT)

        s_config = Builders.AgentConfig.with_default_profile(profile=agent_profile)
        s_config.with_lessons_dir(str(lessons_path))
        if settings.ADB_HOST:
            s_config.with_adb_server(host=settings.ADB_HOST, port=settings.ADB_PORT)

        orchestrator = MasterOrchestrator(
            primary_app=primary,
            secondary_app=secondary,
            primary_ctx=primary_ctx,
            secondary_ctx=secondary_ctx,
            primary_config_builder=p_config,
            secondary_config_builder=s_config,
            lessons_dir=lessons_path,
            observer_timeout=observer_timeout,
            trigger_hints=hint_set,
        )

        await orchestrator.init()
        await orchestrator.run_cluster_session(
            budget_minutes=budget_minutes,
            max_depth=max_depth,
        )


@app.command("learn-cluster")
def learn_cluster(
    primary: Annotated[
        str,
        typer.Option(
            "--primary",
            help="Primary app package (active explorer).",
        ),
    ],
    secondary: Annotated[
        str,
        typer.Option(
            "--secondary",
            help="Secondary app package (observer + explorer).",
        ),
    ],
    lessons_dir: Annotated[
        str,
        typer.Option(
            "--lessons-dir",
            help="Directory for lessons and exploration state.",
        ),
    ],
    primary_device: Annotated[
        str | None,
        typer.Option(
            "--primary-device",
            help="ADB device serial for primary app.",
        ),
    ] = None,
    secondary_device: Annotated[
        str | None,
        typer.Option(
            "--secondary-device",
            help="ADB device serial for secondary app.",
        ),
    ] = None,
    sessions: Annotated[
        int,
        typer.Option(
            "--sessions",
            help="Number of sessions to run.",
        ),
    ] = 1,
    budget_minutes: Annotated[
        int,
        typer.Option(
            "--budget-minutes",
            help="Time budget per session in minutes.",
        ),
    ] = 45,
    max_depth: Annotated[
        int,
        typer.Option(
            "--max-depth",
            help="Maximum depth in the feature tree.",
        ),
    ] = 4,
    observer_timeout: Annotated[
        float,
        typer.Option(
            "--observer-timeout",
            help="Seconds to wait for cross-app events.",
        ),
    ] = 10.0,
    trigger_hints: Annotated[
        str | None,
        typer.Option(
            "--trigger-hints",
            help="Comma-separated action labels that trigger Observer Mode.",
        ),
    ] = None,
    reset: Annotated[
        bool,
        typer.Option(
            "--reset",
            help="Discard existing exploration state for both apps.",
        ),
    ] = False,
    model_provider: Annotated[
        ModelProvider,
        typer.Option(
            "--model-provider",
            "-m",
            help="Model provider preset: 'default', 'claude', or 'gui_owl'.",
        ),
    ] = ModelProvider.DEFAULT,
    claude_model: Annotated[
        str | None,
        typer.Option(
            "--claude-model",
            help="Claude model ID to use.",
        ),
    ] = None,
) -> None:
    """Explore two linked apps (Parent/Child) with cross-app event detection."""
    console = Console()
    console.print(
        f"[cyan]Starting cluster exploration: {primary} + {secondary}...[/cyan]"
    )

    session_id = telemetry.start_session({"source": "cli", "command": "learn-cluster"})

    error_message = None
    try:
        asyncio.run(
            run_learn_cluster(
                primary=primary,
                secondary=secondary,
                primary_device=primary_device,
                secondary_device=secondary_device,
                lessons_dir=lessons_dir,
                sessions=sessions,
                budget_minutes=budget_minutes,
                max_depth=max_depth,
                observer_timeout=observer_timeout,
                trigger_hints=trigger_hints,
                reset=reset,
                model_provider=model_provider,
                claude_model=claude_model,
            )
        )
    except KeyboardInterrupt:
        error_message = "Cluster exploration cancelled by user"
        console.print("[yellow]Cluster exploration cancelled. Progress has been saved.[/yellow]")
    except Exception as e:
        error_message = str(e)
        console.print(
            f"\n[dim]If you need support, please include this session ID: {session_id}[/dim]"
        )
        raise
    finally:
        telemetry.end_session(success=error_message is None, error=error_message)


def _prompt_telemetry_consent(console: Console) -> None:
    """Prompt user for telemetry consent if not yet configured."""
    if not telemetry.needs_consent:
        return

    console.print()
    console.print("[bold]📊 Help improve ui-auto[/bold]")
    console.print(
        "We collect anonymous usage data to help debug and improve the SDK.\n"
        "No personal data, prompts, or device content is collected.\n"
        "You can change this anytime by setting MOBILE_USE_TELEMETRY_ENABLED=false\n"
    )

    try:
        import inquirer

        questions = [
            inquirer.Confirm(
                "consent",
                message="Enable anonymous telemetry?",
                default=True,
            )
        ]
        answers = inquirer.prompt(questions)
        if answers is not None:
            enabled = answers.get("consent", False)
            telemetry.set_consent(enabled)
            if enabled:
                console.print("[green]✓ Telemetry enabled. Thank you![/green]\n")
            else:
                console.print("[dim]Telemetry disabled.[/dim]\n")
        else:
            telemetry.set_consent(False)
    except (ImportError, KeyboardInterrupt):
        telemetry.set_consent(False)


def cli():
    console = Console()
    _prompt_telemetry_consent(console)
    telemetry.initialize()
    try:
        app()
    finally:
        telemetry.shutdown()


if __name__ == "__main__":
    cli()
