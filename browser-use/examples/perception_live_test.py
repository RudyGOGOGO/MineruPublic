"""
Live test: Agent with OCR + SoM on real websites with canvas content.

Runs the full browser-use agent with enhanced perception against real
sites that have canvas-rendered charts (Google Finance). The agent
uses OCR + SoM to read price data, chart labels, and other visual
content that DOM alone cannot see.

Prerequisites:
    1. Quit Google Chrome completely.
    2. Install OCR extras:  uv add browser-use[ocr]

Usage:
    uv run python examples/perception_live_test.py
"""

import asyncio
import logging

from browser_use import Agent, BrowserSession
from browser_use.browser.profile import BrowserProfile
from browser_use.llm.claude_code import ChatClaudeCode

logging.basicConfig(level=logging.INFO)


async def main():
	profile = BrowserProfile(
		headless=False,
		keep_alive=True,
		perception_mode='enhanced',
	)
	session = BrowserSession(browser_profile=profile)
	agent = Agent(
		task=(
			'Go to https://www.google.com/finance/quote/AAPL:NASDAQ\n'
			'Read the stock chart and tell me:\n'
			'1. The current stock price\n'
			'2. The percentage change shown\n'
			'3. Any price labels visible on the chart Y-axis\n'
			'4. The time range shown on the X-axis\n'
			'Note: Some of this data may be rendered on canvas elements '
			'that are only visible through OCR, not the DOM.'
		),
		llm=ChatClaudeCode(model='sonnet'),
		browser_session=session,
	)
	result = await agent.run()

	print('\n' + '=' * 70)
	print(' AGENT RESULT')
	print('=' * 70)
	print(result.final_result())

	print('\nBrowser is still open — verify the chart data visually.')
	print('Press Enter to close...')
	await asyncio.get_event_loop().run_in_executor(None, input)

	await session.stop()


if __name__ == '__main__':
	asyncio.run(main())
