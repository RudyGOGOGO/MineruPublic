"""
Search YouTube using enhanced perception mode (OCR + SoM).

This demonstrates the enhanced perception pipeline on a real website,
where OCR can detect text in video thumbnails, overlays, and other
visually-rendered content that the DOM may not expose.

Setup:
    uv pip install "paddleocr>=2.7" "paddlepaddle>=2.6"

Usage:
    uv run python examples/perception_youtube_search.py
"""

import asyncio

from dotenv import load_dotenv

from browser_use import Agent
from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession
from browser_use.llm.claude_code import ChatClaudeCode

load_dotenv()


async def main():
	profile = BrowserProfile(
		perception_mode='enhanced',
		headless=False,
	)
	session = BrowserSession(browser_profile=profile)
	agent = Agent(
		task='Go to youtube.com and search for "browser use ai agent demo"',
		llm=ChatClaudeCode(model='sonnet'),
		browser_session=session,
	)
	await agent.run()


if __name__ == '__main__':
	asyncio.run(main())
