"""
Example: Summarize recent emails via Gemini using your Chrome profile.

Uses your logged-in Chrome session to visit gemini.google.com and ask it
to summarize your recent emails — demonstrating cross-site authenticated
browsing with browser-use.

Prerequisites:
    1. Quit Google Chrome completely (it locks the profile directory).
    2. Claude Code CLI installed and authenticated.

Usage:
    uv run python examples/claude_code_gmail.py
"""

import asyncio

from browser_use import Agent, BrowserSession
from browser_use.browser.profile import BrowserProfile
from browser_use.llm.claude_code import ChatClaudeCode


async def main():
	profile = BrowserProfile(
		user_data_dir='~/Library/Application Support/Google/Chrome',
		profile_directory='Default',
		headless=False,
		keep_alive=True,
	)
	session = BrowserSession(browser_profile=profile)
	agent = Agent(
		task='Go to gemini.google.com and check my recent emails, give me a summary',
		llm=ChatClaudeCode(model='sonnet'),
		browser_session=session,
	)
	result = await agent.run()
	print(result.final_result())


if __name__ == '__main__':
	asyncio.run(main())
