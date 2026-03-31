"""
Ask the browser agent to do anything — just describe what you want in plain English.

Usage:
    uv run python examples/ask_agent.py "find the top mass gainer on amazon and compare prices"
    uv run python examples/ask_agent.py "go to hacker news and summarize the top 3 stories"
    uv run python examples/ask_agent.py "search google flights for NYC to Tokyo next month"
"""

import asyncio
import logging
import sys

from browser_use import Agent, BrowserSession
from browser_use.browser.profile import BrowserProfile
from browser_use.llm.claude_code import ChatClaudeCode

logging.basicConfig(level=logging.INFO, format='%(levelname)-8s [%(name)s] %(message)s')


async def main():
	if len(sys.argv) < 2:
		print('Usage: uv run python examples/ask_agent.py "<your task>"')
		print()
		print('Examples:')
		print('  "find the top mass gainer on amazon and compare prices"')
		print('  "go to hacker news and summarize the top 3 stories"')
		print('  "search google flights for NYC to Tokyo next month"')
		print('  "look up the weather in San Francisco this weekend"')
		sys.exit(1)

	task = ' '.join(sys.argv[1:])

	session = BrowserSession(
		browser_profile=BrowserProfile(
			headless=False,
			keep_alive=True,
		)
	)
	agent = Agent(
		task=task,
		llm=ChatClaudeCode(model='sonnet'),
		browser_session=session,
		max_steps=15,
	)
	result = await agent.run()

	print('\n' + '=' * 70)
	print(result.final_result())
	print('=' * 70)


if __name__ == '__main__':
	asyncio.run(main())
