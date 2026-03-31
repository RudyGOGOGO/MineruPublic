"""
Search YouTube for trending AI videos and summarize the top results.

Usage:
    uv run python examples/youtube_trending_tech.py
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
			'Go to https://www.youtube.com/results?search_query=AI+agents+2026 and tell me:\n'
			'1. The top 3 videos shown in search results\n'
			'2. For each: the video title, channel name, view count, and upload date\n'
			'3. A brief guess at what each video covers based on the title'
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
