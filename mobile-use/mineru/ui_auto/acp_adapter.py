"""ACP adapter for mobile-use (mineru ui-auto).

Bridges the ACP protocol to mobile-use's SDK Agent, allowing DeerFlow
to invoke mobile-use as an autonomous agent for UI automation tasks.

Usage:
  python -m mineru.ui_auto.acp_adapter

DeerFlow config.yaml:
  acp_agents:
    mobile_use:
      command: /path/to/mobile-use/.venv/bin/python
      args: ["-m", "mineru.ui_auto.acp_adapter"]
      description: "Mobile device UI automation - taps, swipes, types, screenshots, test flows on connected Android/iOS"
      auto_approve_permissions: true
      env:
        ANDROID_SERIAL: $ANDROID_SERIAL
"""

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any

import acp
from acp import Agent as ACPAgent, run_agent
from acp.schema import (
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    NewSessionResponse,
    PromptResponse,
    TextContentBlock,
)

from mineru.ui_auto.sdk import Agent as MobileAgent

logger = logging.getLogger(__name__)


class MobileUseACPAdapter(ACPAgent):
    """ACP server that wraps mobile-use SDK Agent."""

    def __init__(self):
        self._conn = None
        self._sessions: dict[str, dict[str, Any]] = {}

    def on_connect(self, conn):
        self._conn = conn

    async def initialize(
        self, protocol_version, client_capabilities=None, client_info=None, **kwargs
    ) -> InitializeResponse:
        logger.info("ACP initialize: protocol=%s client=%s", protocol_version, client_info)
        return InitializeResponse(
            protocol_version=acp.PROTOCOL_VERSION,
            server_info=Implementation(
                name="mobile-use", title="Mobile Use", version="3.6.3"
            ),
        )

    async def new_session(self, cwd, mcp_servers=None, **kwargs) -> NewSessionResponse:
        session_id = str(uuid.uuid4())
        self._sessions[session_id] = {"cwd": cwd}
        logger.info("New session: %s cwd=%s", session_id, cwd)
        return NewSessionResponse(session_id=session_id)

    async def prompt(self, prompt, session_id, **kwargs) -> PromptResponse:
        """Receive task prompt from DeerFlow, run mobile-use, return results."""
        task_text = ""
        for block in prompt:
            if isinstance(block, TextContentBlock) or hasattr(block, "text"):
                task_text += block.text

        logger.info("Received task: %s", task_text[:200])
        cwd = self._sessions.get(session_id, {}).get("cwd", ".")

        if self._conn:
            await self._conn.session_update(
                session_id=session_id,
                update=acp.update_agent_message_text(
                    f"Starting mobile automation: {task_text[:100]}..."
                ),
            )

        try:
            result = await self._run_mobile_task(task_text, cwd, session_id)
        except Exception as e:
            logger.error("Mobile task failed: %s", e)
            result = f"Error running mobile automation: {e}"

        if self._conn:
            await self._conn.session_update(
                session_id=session_id,
                update=acp.update_agent_message_text(result),
            )

        return PromptResponse()

    async def _run_mobile_task(self, goal: str, cwd: str, session_id: str) -> str:
        """Run the actual mobile-use agent with the given goal."""
        output_dir = Path(cwd)
        output_dir.mkdir(parents=True, exist_ok=True)

        agent = MobileAgent()
        await agent.init()

        result = await agent.run_task(
            goal=goal,
            output="Detailed test report with pass/fail status for each step",
        )

        # Save result to workspace so DeerFlow can read it
        report_path = output_dir / "test_report.json"
        report_data = {
            "goal": goal,
            "result": str(result) if result else "Task completed",
            "status": "completed",
        }
        report_path.write_text(json.dumps(report_data, indent=2))

        # Copy traces/screenshots if available
        traces_dir = Path("traces")
        if traces_dir.exists():
            import shutil

            dest = output_dir / "traces"
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(traces_dir, dest)

        return (
            str(result)
            if result
            else "Mobile automation task completed. See test_report.json for details."
        )

    async def load_session(self, cwd, session_id, **kwargs):
        return None

    async def list_sessions(self, **kwargs):
        return ListSessionsResponse(sessions=[])

    async def set_session_mode(self, mode_id, session_id, **kwargs):
        return None

    async def set_session_model(self, model_id, session_id, **kwargs):
        return None

    async def set_config_option(self, config_id, session_id, value, **kwargs):
        return None

    async def authenticate(self, method_id, **kwargs):
        return None

    async def fork_session(self, cwd, session_id, **kwargs):
        return None

    async def resume_session(self, cwd, session_id, **kwargs):
        return None

    async def cancel(self, session_id, **kwargs):
        logger.info("Cancel requested for session %s", session_id)

    async def ext_method(self, method, params):
        return {}

    async def ext_notification(self, method, params):
        pass


async def main():
    logging.basicConfig(level=logging.INFO)
    adapter = MobileUseACPAdapter()
    await run_agent(adapter)


if __name__ == "__main__":
    asyncio.run(main())
