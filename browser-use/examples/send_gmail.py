"""
Example: Use your real Chrome profile to interact with logged-in sites.

Launches Chrome using your existing user data directory so that cookies,
sessions, and saved logins carry over.  Combined with enhanced perception
(OCR + SoM) to detect visually-rendered content the DOM may miss.

Prerequisites:
    1. Quit Google Chrome completely (it locks the profile directory).
    2. Install OCR extras:  uv add browser-use[ocr]
    3. Claude Code CLI installed and authenticated.

Usage:
    uv run python examples/send_gmail.py
"""

import asyncio

from browser_use import Agent, BrowserSession
from browser_use.browser.profile import BrowserProfile
from browser_use.llm.claude_code import ChatClaudeCode


async def main():
	profile = BrowserProfile(
		# Point at your real Chrome data dir so logged-in sessions are available.
		# macOS default shown; adjust for Linux (~/.config/google-chrome) or
		# Windows (~\\AppData\\Local\\Google\\Chrome\\User Data).
		user_data_dir='~/Library/Application Support/Google/Chrome',
		profile_directory='Default',  # or 'Profile 1', etc.
		headless=False,
		keep_alive=True,
		perception_mode='enhanced',
	)
	session = BrowserSession(browser_profile=profile)
	agent = Agent(
		task=(
			'Go to Gmail (https://mail.google.com) and tell me the subject '
			'lines of the 3 most recent emails in my inbox.'
		),
		llm=ChatClaudeCode(model='sonnet'),
		browser_session=session,
	)
	result = await agent.run()
	print(result.final_result())


if __name__ == '__main__':
	asyncio.run(main())
