"""
Scrape today's trending GitHub repositories using your logged-in Chrome.

Prerequisites: Quit Google Chrome completely before running.

Usage:
    uv run python examples/github_trending_repos.py
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
			'Go to https://github.com/trending and tell me:\n'
			'1. The top 5 trending repositories today\n'
			'2. For each: the repo name, language, stars gained today, and a one-line description\n'
			'3. Any patterns you notice (e.g., are they all AI-related?)'
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
