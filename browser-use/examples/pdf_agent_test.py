"""
Test: How the current browser-use agent handles PDF content end-to-end.

Uses the agent system as-is — no workarounds, no custom perception calls.
This shows the baseline behavior before any PDF perception enhancements:

  1. Agent navigates to a PDF URL
  2. DownloadsWatchdog auto-downloads the PDF
  3. Agent is told "PDF cannot be rendered, use read_file"
  4. Agent reads the downloaded file via read_file action

Run with enhanced perception enabled to see what the OCR pipeline
can (or cannot) capture from the Chrome PDF plugin page.

Prerequisites:
    1. Quit Google Chrome completely.
    2. Install OCR extras:  uv add browser-use[ocr]

Usage:
    uv run python examples/pdf_agent_test.py
"""

import asyncio
import logging

from browser_use import Agent, BrowserSession
from browser_use.browser.profile import BrowserProfile
from browser_use.llm.claude_code import ChatClaudeCode

logging.basicConfig(level=logging.INFO)

PDF_URL = 'https://resources.anthropic.com/hubfs/The-Complete-Guide-to-Building-Skill-for-Claude.pdf?hsLang=en'


async def main():
	profile = BrowserProfile(
		user_data_dir='~/Library/Application Support/Google/Chrome',
		profile_directory='Default',
		executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
		headless=False,
		keep_alive=True,
		perception_mode='enhanced',
	)
	session = BrowserSession(browser_profile=profile)
	agent = Agent(
		task=(
			f'Go to this PDF: {PDF_URL}\n'
			'Read the PDF content and give me a summary of:\n'
			'1. What is this document about?\n'
			'2. List the main sections or topics covered\n'
			'3. Any key recommendations or takeaways mentioned'
		),
		llm=ChatClaudeCode(model='sonnet'),
		browser_session=session,
	)
	result = await agent.run()
	print('\n' + '=' * 70)
	print(' AGENT RESULT')
	print('=' * 70)
	print(result.final_result())


if __name__ == '__main__':
	asyncio.run(main())
