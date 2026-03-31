"""
Read the current top stories from Hacker News using your logged-in Chrome.

Prerequisites: Quit Google Chrome completely before running.

Usage:
    uv run python examples/hackernews_top_stories.py
"""

import asyncio
import logging

from browser_use import Agent, BrowserSession
from browser_use.browser.profile import BrowserProfile
from browser_use.llm.claude_code import ChatClaudeCode

logging.basicConfig(level=logging.INFO, format='%(levelname)-8s [%(name)s] %(message)s')


async def main():
	session = BrowserSession(
		browser_profile=BrowserProfile(
			headless=False,
			keep_alive=True,
		)
	)
	agent = Agent(
		task=(
			'Go to https://news.ycombinator.com and tell me:\n'
			'1. The top 5 stories right now\n'
			'2. For each: title, points, number of comments\n'
			'3. Which story would be most interesting to a software engineer working on AI agents?'
		),
		llm=ChatClaudeCode(model='sonnet'),
		browser_session=session,
		max_steps=5,
	)
	result = await agent.run()
	print('\n' + '=' * 70)
	print(result.final_result())


if __name__ == '__main__':
	asyncio.run(main())
