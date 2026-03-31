"""
Prototype: PDF reading with server-side text extraction (proposed optimization).

Simulates the proposed pdf-perception-design.md optimization:
  1. Downloads the PDF (same as DownloadsWatchdog)
  2. Extracts text server-side (same as proposed Fix 2)
  3. Navigates to a simple page showing "PDF loaded"
  4. Injects the extracted text into the agent task
     (simulates what prompts.py Fix 4 would do via <pdf_content>)

This demonstrates the correct approach: text PDFs should be
extracted as text, not routed through canvas + OCR.

Prerequisites:
    1. Quit Google Chrome completely.

Usage:
    uv run python examples/pdf_agent_optimized_test.py
"""

import asyncio
import logging
import threading
import urllib.request
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from tempfile import TemporaryDirectory

from browser_use import Agent, BrowserSession
from browser_use.browser.profile import BrowserProfile
from browser_use.llm.claude_code import ChatClaudeCode

logging.basicConfig(level=logging.INFO)

PDF_URL = 'https://resources.anthropic.com/hubfs/The-Complete-Guide-to-Building-Skill-for-Claude.pdf?hsLang=en'


def extract_pdf_text(pdf_path: str, max_chars: int = 8000) -> str:
	"""Extract text from PDF — simulates what DOMWatchdog Fix 2 would do."""
	try:
		import pymupdf
		doc = pymupdf.open(pdf_path)
		text_parts = []
		for page in doc:
			text_parts.append(page.get_text())
		text = '\n'.join(text_parts)
		doc.close()
	except ImportError:
		# Fallback to pdfminer if pymupdf not available
		try:
			from pdfminer.high_level import extract_text
			text = extract_text(pdf_path)
		except ImportError:
			return '[Could not extract text — install pymupdf or pdfminer.six]'

	if len(text) > max_chars:
		text = text[:max_chars] + f'\n\n[... truncated, {len(text)} chars total]'
	return text


# Simple page to show in browser while agent reads the PDF text
LANDING_HTML = """<!DOCTYPE html>
<html><head><style>
body { font-family: system-ui; background: #f5f5f5; display: flex;
       justify-content: center; align-items: center; height: 100vh; margin: 0; }
.card { background: #fff; padding: 40px; border-radius: 12px;
        box-shadow: 0 2px 12px rgba(0,0,0,0.1); text-align: center; }
h2 { color: #333; margin-bottom: 8px; }
p { color: #666; }
</style></head><body>
<div class="card">
  <h2>PDF Loaded</h2>
  <p>Text has been extracted and provided to the agent.</p>
  <p style="font-size: 13px; color: #999;">This simulates the proposed pdf-perception-design.md optimization.</p>
</div>
</body></html>"""


class Handler(SimpleHTTPRequestHandler):
	def do_GET(self):
		content = LANDING_HTML.encode()
		self.send_response(200)
		self.send_header('Content-Type', 'text/html')
		self.send_header('Content-Length', str(len(content)))
		self.end_headers()
		self.wfile.write(content)

	def log_message(self, format, *args):
		pass


def start_server() -> tuple[HTTPServer, int]:
	server = HTTPServer(('127.0.0.1', 0), Handler)
	port = server.server_address[1]
	threading.Thread(target=server.serve_forever, daemon=True).start()
	return server, port


async def main():
	with TemporaryDirectory(prefix='pdf-test-') as tmpdir:
		# Step 1: Download PDF (simulates DownloadsWatchdog)
		pdf_path = Path(tmpdir) / 'document.pdf'
		print(f'Downloading PDF...')
		urllib.request.urlretrieve(PDF_URL, pdf_path)
		print(f'Downloaded: {pdf_path.stat().st_size / 1024:.0f} KB')

		# Step 2: Extract text (simulates DOMWatchdog Fix 2)
		print('Extracting text...')
		pdf_text = extract_pdf_text(str(pdf_path))
		print(f'Extracted: {len(pdf_text)} chars')

		# Step 3: Start browser on a landing page
		server, port = start_server()
		profile = BrowserProfile(
			user_data_dir='~/Library/Application Support/Google/Chrome',
			profile_directory='Default',
			executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
			headless=False,
			keep_alive=True,
		)
		session = BrowserSession(browser_profile=profile)
		await session.start()
		await session.navigate_to(f'http://127.0.0.1:{port}/')
		await asyncio.sleep(1.0)

		# Step 4: Run agent with extracted text in the task
		# (simulates what prompts.py Fix 4 would inject as <pdf_content>)
		agent = Agent(
			task=(
				'You are viewing a PDF document. The text has been extracted '
				'and is provided below.\n\n'
				f'<pdf_content>\n{pdf_text}\n</pdf_content>\n\n'
				'Give me a summary of:\n'
				'1. What is this document about?\n'
				'2. List the main sections or topics covered\n'
				'3. Any key recommendations or takeaways mentioned'
			),
			llm=ChatClaudeCode(model='sonnet'),
			browser_session=session,
			max_steps=3,
		)
		result = await agent.run()

		print('\n' + '=' * 70)
		print(' AGENT RESULT (with server-side text extraction)')
		print('=' * 70)
		print(result.final_result())

		await session.stop()
		server.shutdown()


if __name__ == '__main__':
	asyncio.run(main())
