"""
Take a random Wikipedia adventure — land on a random article, follow a link, connect the dots.

Prerequisites: Quit Google Chrome completely before running.

Usage:
    uv run python examples/wikipedia_random_adventure.py
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
			'Go to https://en.wikipedia.org/wiki/Special:Random\n'
			'Read the article you land on, then:\n'
			'1. Summarize what the article is about in 2-3 sentences\n'
			'2. Click on the most interesting link within the article\n'
			'3. Summarize that second article too\n'
			'4. Tell me how the two articles are connected'
		),
		llm=ChatClaudeCode(model='sonnet'),
		browser_session=session,
		max_steps=10,
	)
	result = await agent.run()
	print('\n' + '=' * 70)
	print(result.final_result())


if __name__ == '__main__':
	asyncio.run(main())
