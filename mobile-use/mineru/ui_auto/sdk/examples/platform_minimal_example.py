"""
Platform Usage - Minitap SDK with API Key Example

This example demonstrates how to use the ui-auto SDK via the Minitap platform:
- Agent with mineru_api_key
- PlatformTaskRequest with platform-provided task_id
- All task configuration (goal, output format, etc.) managed by platform UI

Platform Model:
- API key provides authentication and agent configuration
- task_id references pre-configured task from platform UI
- No goal, output_format, profile selection needed in code
- Everything bound to task_id + api_key combination

Run:
- python src/ui_auto/sdk/examples/platform_minimal_example.py
"""

import asyncio

from mineru.ui_auto.sdk import Agent
from mineru.ui_auto.sdk.types import PlatformTaskRequest


async def main() -> None:
    """
    Main execution function demonstrating mineru platform usage pattern.

    Visit https://platform.ui-auto.ai to create a task, customize your profiles,
    and get your API key.
    Pass the api_key parameter to the agent.init() method
    ...or set MINITAP_API_KEY environment variable.
    """
    agent = Agent()
    await agent.init(api_key="<your-mineru-api-key>")  # or set MINITAP_API_KEY env variable
    result = await agent.run_task(
        request=PlatformTaskRequest(
            task="<your-task-name>",
            profile="<your-profile-name>",
        ),
        locked_app_package="<locked-app-package>",  # optional
    )
    print(result)
    await agent.clean()


if __name__ == "__main__":
    asyncio.run(main())
