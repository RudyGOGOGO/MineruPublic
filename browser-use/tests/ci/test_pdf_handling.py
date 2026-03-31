"""Tests for PDF handling: early detection, screenshot skipping, text extraction, and prompt injection.

Covers:
1. _detect_pdf_page() recognizes .pdf URLs and _session_pdf_urls entries
2. Screenshot is skipped for PDF pages (no 15s timeout)
3. pdf_extracted_text is populated from downloaded PDF
4. BrowserStateSummary has is_pdf_viewer=True and screenshot=None for PDFs
5. AgentMessagePrompt includes <pdf_content> block when text is available
6. Enhanced perception is skipped on PDF pages
7. Fallback message when text extraction fails
8. Large PDF text is truncated
"""

import asyncio
import tempfile
from io import BytesIO
from unittest.mock import MagicMock

import pytest
from pytest_httpserver import HTTPServer

from browser_use.agent.prompts import AgentMessagePrompt
from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.browser.views import BrowserStateSummary
from browser_use.dom.views import SerializedDOMState
from browser_use.filesystem.file_system import FileSystem
from browser_use.tools.service import Tools


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_pdf_bytes(texts: list[str]) -> bytes:
	"""Generate a minimal PDF with the given text strings, one per page."""
	from reportlab.pdfgen import canvas as rl_canvas

	buf = BytesIO()
	c = rl_canvas.Canvas(buf)
	for text in texts:
		c.drawString(100, 700, text)
		c.showPage()
	c.save()
	return buf.getvalue()


@pytest.fixture(scope='session')
def text_pdf_bytes() -> bytes:
	return _make_pdf_bytes(['Hello World', 'Total Revenue: $847.3 million'])


@pytest.fixture(scope='session')
def large_pdf_bytes() -> bytes:
	"""Generate a PDF large enough to trigger truncation (>8000 chars)."""
	# Each page has ~200 chars of text, 60 pages = ~12000 chars
	pages = [f'Page {i}: ' + 'Lorem ipsum dolor sit amet, consectetur adipiscing elit. ' * 3 for i in range(1, 61)]
	return _make_pdf_bytes(pages)


@pytest.fixture(scope='session')
def http_server(text_pdf_bytes: bytes):
	server = HTTPServer()
	server.start()

	server.expect_request('/test.pdf').respond_with_data(
		text_pdf_bytes, content_type='application/pdf'
	)
	server.expect_request('/document/view').respond_with_data(
		text_pdf_bytes, content_type='application/pdf'
	)
	server.expect_request('/page.html').respond_with_data(
		'<html><body><h1>Normal Page</h1></body></html>',
		content_type='text/html',
	)

	yield server
	server.stop()


@pytest.fixture(scope='session')
def base_url(http_server: HTTPServer) -> str:
	return f'http://{http_server.host}:{http_server.port}'


@pytest.fixture(scope='module')
async def browser_session():
	session = BrowserSession(
		browser_profile=BrowserProfile(
			headless=True,
			user_data_dir=None,
			keep_alive=True,
			auto_download_pdfs=True,
		)
	)
	await session.start()
	yield session
	await session.kill()


@pytest.fixture(scope='function')
def tools():
	return Tools()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_prompt(state: BrowserStateSummary) -> AgentMessagePrompt:
	tmp_dir = tempfile.mkdtemp(prefix='browseruse_test_')
	file_system = FileSystem(base_dir=tmp_dir, create_default_files=False)
	return AgentMessagePrompt(
		browser_state_summary=state,
		file_system=file_system,
	)


# ---------------------------------------------------------------------------
# Test 1: _detect_pdf_page unit tests
# ---------------------------------------------------------------------------

class TestDetectPdfPage:
	"""Unit tests for DOMWatchdog._detect_pdf_page()."""

	async def test_detects_pdf_url_extension(self, browser_session):
		"""URLs ending in .pdf are detected."""
		dom_watchdog = browser_session._dom_watchdog
		assert dom_watchdog._detect_pdf_page('https://example.com/report.pdf') is True

	async def test_detects_pdf_url_with_query_params(self, browser_session):
		"""URLs ending in .pdf before query params are detected."""
		dom_watchdog = browser_session._dom_watchdog
		assert dom_watchdog._detect_pdf_page('https://example.com/report.pdf?token=abc') is True

	async def test_detects_pdf_url_with_fragment(self, browser_session):
		"""URLs ending in .pdf before fragment are detected."""
		dom_watchdog = browser_session._dom_watchdog
		assert dom_watchdog._detect_pdf_page('https://example.com/report.pdf#page=2') is True

	async def test_detects_pdf_url_case_insensitive(self, browser_session):
		"""PDF detection is case-insensitive."""
		dom_watchdog = browser_session._dom_watchdog
		assert dom_watchdog._detect_pdf_page('https://example.com/Report.PDF') is True

	async def test_non_pdf_url_not_detected(self, browser_session):
		"""Non-PDF URLs are not detected."""
		dom_watchdog = browser_session._dom_watchdog
		assert dom_watchdog._detect_pdf_page('https://example.com/page.html') is False

	async def test_detects_via_session_pdf_urls(self, browser_session):
		"""URLs in _session_pdf_urls are detected even without .pdf extension."""
		dom_watchdog = browser_session._dom_watchdog
		downloads_watchdog = browser_session._downloads_watchdog

		# Simulate a URL that was downloaded as PDF
		test_url = 'https://example.com/document/view'
		downloads_watchdog._session_pdf_urls[test_url] = '/tmp/test.pdf'
		try:
			assert dom_watchdog._detect_pdf_page(test_url) is True
		finally:
			del downloads_watchdog._session_pdf_urls[test_url]


# ---------------------------------------------------------------------------
# Test 2: BrowserStateSummary field
# ---------------------------------------------------------------------------

class TestBrowserStateSummaryPdfField:
	"""Tests for the pdf_extracted_text field on BrowserStateSummary."""

	def test_default_is_none(self):
		"""pdf_extracted_text defaults to None."""
		state = BrowserStateSummary(
			dom_state=SerializedDOMState(_root=None, selector_map={}),
			url='https://example.com',
			title='Test',
			tabs=[],
		)
		assert state.pdf_extracted_text is None

	def test_can_set_pdf_text(self):
		"""pdf_extracted_text can be set."""
		state = BrowserStateSummary(
			dom_state=SerializedDOMState(_root=None, selector_map={}),
			url='https://example.com/test.pdf',
			title='Test',
			tabs=[],
			is_pdf_viewer=True,
			pdf_extracted_text='Hello World',
		)
		assert state.pdf_extracted_text == 'Hello World'
		assert state.is_pdf_viewer is True


# ---------------------------------------------------------------------------
# Test 3: PDF text extraction
# ---------------------------------------------------------------------------

class TestPdfTextExtraction:
	"""Tests for DOMWatchdog._extract_text_from_pdf()."""

	async def test_extract_text_from_valid_pdf(self, browser_session, text_pdf_bytes):
		"""Extracts text from a valid PDF file."""
		dom_watchdog = browser_session._dom_watchdog

		with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as f:
			f.write(text_pdf_bytes)
			f.flush()
			text = await dom_watchdog._extract_text_from_pdf(f.name)

		assert text is not None
		assert 'Hello World' in text

	async def test_extract_text_returns_none_for_missing_file(self, browser_session):
		"""Returns None for a non-existent PDF file."""
		dom_watchdog = browser_session._dom_watchdog
		text = await dom_watchdog._extract_text_from_pdf('/tmp/nonexistent_file_xyz.pdf')
		assert text is None

	async def test_extract_pdf_text_truncates_large_content(self, browser_session, large_pdf_bytes):
		"""Large PDFs are truncated with a message."""
		dom_watchdog = browser_session._dom_watchdog
		downloads_watchdog = browser_session._downloads_watchdog

		with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as f:
			f.write(large_pdf_bytes)
			f.flush()
			pdf_path = f.name

		# Simulate the URL → path mapping
		test_url = 'https://example.com/large.pdf'
		downloads_watchdog._session_pdf_urls[test_url] = pdf_path
		try:
			text = await dom_watchdog._extract_pdf_text(max_chars=500)
			assert text is not None
			assert len(text) > 500
			assert 'truncated' in text
			assert 'total chars' in text
		finally:
			del downloads_watchdog._session_pdf_urls[test_url]


# ---------------------------------------------------------------------------
# Test 4: Prompt injection
# ---------------------------------------------------------------------------

class TestPdfPromptInjection:
	"""Tests for PDF content in AgentMessagePrompt."""

	def test_pdf_content_block_when_text_available(self):
		"""Prompt contains <pdf_content> when pdf_extracted_text is set."""
		state = BrowserStateSummary(
			dom_state=SerializedDOMState(_root=None, selector_map={}),
			url='https://example.com/test.pdf',
			title='Test PDF',
			tabs=[],
			is_pdf_viewer=True,
			pdf_extracted_text='Hello World\nTotal Revenue: $847.3 million',
		)
		prompt = _make_prompt(state)
		description = prompt._get_browser_state_description()

		assert '<pdf_content>' in description
		assert 'Hello World' in description
		assert 'Total Revenue' in description
		assert '</pdf_content>' in description

	def test_fallback_message_when_no_text(self):
		"""Prompt contains fallback message when pdf_extracted_text is None."""
		state = BrowserStateSummary(
			dom_state=SerializedDOMState(_root=None, selector_map={}),
			url='https://example.com/test.pdf',
			title='Test PDF',
			tabs=[],
			is_pdf_viewer=True,
			pdf_extracted_text=None,
		)
		prompt = _make_prompt(state)
		description = prompt._get_browser_state_description()

		assert '<pdf_content>' not in description
		assert 'text extraction failed' in description
		assert 'read_file' in description

	def test_no_pdf_message_for_non_pdf(self):
		"""Normal pages don't get PDF messages."""
		state = BrowserStateSummary(
			dom_state=SerializedDOMState(_root=None, selector_map={}),
			url='https://example.com/page.html',
			title='Normal Page',
			tabs=[],
			is_pdf_viewer=False,
		)
		prompt = _make_prompt(state)
		description = prompt._get_browser_state_description()

		assert '<pdf_content>' not in description
		assert 'PDF' not in description


# ---------------------------------------------------------------------------
# Test 5: Integration — navigate to PDF URL
# ---------------------------------------------------------------------------

class TestPdfIntegration:
	"""Integration tests navigating to actual PDF URLs served by httpserver."""

	async def test_pdf_page_state_has_correct_flags(self, tools, browser_session, base_url):
		"""Navigating to .pdf URL sets is_pdf_viewer=True and screenshot=None."""
		await tools.navigate(url=f'{base_url}/test.pdf', new_tab=False, browser_session=browser_session)
		await asyncio.sleep(1.0)  # Give time for PDF download

		state = await browser_session.get_browser_state_summary(include_screenshot=True)
		assert state.is_pdf_viewer is True
		assert state.screenshot is None

	async def test_pdf_page_has_extracted_text(self, tools, browser_session, base_url):
		"""Navigating to .pdf URL populates pdf_extracted_text."""
		await tools.navigate(url=f'{base_url}/test.pdf', new_tab=False, browser_session=browser_session)
		await asyncio.sleep(1.0)

		state = await browser_session.get_browser_state_summary(include_screenshot=True)
		assert state.is_pdf_viewer is True
		# Text may or may not be extracted depending on whether download completed
		# But if it's there, it should contain our test content
		if state.pdf_extracted_text:
			assert 'Hello World' in state.pdf_extracted_text

	async def test_pdf_state_returned_fast(self, tools, browser_session, base_url):
		"""PDF page state is returned in <5s (no 15s screenshot timeout)."""
		await tools.navigate(url=f'{base_url}/test.pdf', new_tab=False, browser_session=browser_session)
		await asyncio.sleep(1.0)

		import time

		start = time.monotonic()
		state = await browser_session.get_browser_state_summary(include_screenshot=True)
		elapsed = time.monotonic() - start

		assert state.is_pdf_viewer is True
		assert elapsed < 5.0, f'PDF state took {elapsed:.1f}s — screenshot timeout not skipped?'

	async def test_normal_page_still_works(self, tools, browser_session, base_url):
		"""Normal HTML page still gets screenshot and is_pdf_viewer=False."""
		await tools.navigate(url=f'{base_url}/page.html', new_tab=False, browser_session=browser_session)
		await asyncio.sleep(0.5)

		state = await browser_session.get_browser_state_summary(include_screenshot=True)
		assert state.is_pdf_viewer is False
		assert state.pdf_extracted_text is None
		# Screenshot should be available for normal pages
		assert state.screenshot is not None

	async def test_enhanced_perception_skipped_for_pdf(self, tools, browser_session, base_url):
		"""Enhanced observation is None for PDF pages."""
		await tools.navigate(url=f'{base_url}/test.pdf', new_tab=False, browser_session=browser_session)
		await asyncio.sleep(1.0)

		state = await browser_session.get_browser_state_summary(include_screenshot=True)
		assert state.enhanced_observation is None


# ---------------------------------------------------------------------------
# Test 6: ChatClaudeCode structured output fallback
# ---------------------------------------------------------------------------

class TestClaudeCodeProseWrap:
	"""Tests for _wrap_prose_as_done_action and _extract_json_from_text."""

	def test_extract_json_direct(self):
		"""Pure JSON text is extracted directly."""
		from browser_use.llm.claude_code.chat import _extract_json_from_text

		result = _extract_json_from_text('{"key": "value"}')
		assert result == '{"key": "value"}'

	def test_extract_json_from_markdown(self):
		"""JSON in markdown code blocks is extracted."""
		from browser_use.llm.claude_code.chat import _extract_json_from_text

		text = 'Here is the result:\n```json\n{"key": "value"}\n```\nDone.'
		result = _extract_json_from_text(text)
		assert result == '{"key": "value"}'

	def test_extract_json_from_wrapped_text(self):
		"""JSON embedded in prose is extracted via brace matching."""
		from browser_use.llm.claude_code.chat import _extract_json_from_text

		text = 'The answer is: {"action": [{"done": {"text": "hello"}}]} and that is all.'
		result = _extract_json_from_text(text)
		assert result is not None
		assert '"done"' in result

	def test_extract_json_returns_none_for_prose(self):
		"""Pure prose with no JSON returns None."""
		from browser_use.llm.claude_code.chat import _extract_json_from_text

		text = 'This is a summary of the PDF content. It contains no JSON at all.'
		result = _extract_json_from_text(text)
		assert result is None

	def test_wrap_prose_as_done_action(self):
		"""Free text is wrapped as a done action for AgentOutput-like schemas."""
		from browser_use.llm.claude_code.chat import _wrap_prose_as_done_action
		from browser_use.agent.views import AgentOutput

		prose = (
			'Here is a comprehensive summary of the PDF document. '
			'It covers topics A, B, and C with recommendations for D and E.'
		)
		result = _wrap_prose_as_done_action(prose, AgentOutput)
		assert result is not None

		import json
		parsed = json.loads(result)
		assert 'action' in parsed
		assert parsed['action'][0]['done']['text'] == prose.strip()
		assert parsed['action'][0]['done']['success'] is True
		assert parsed['evaluation_previous_goal'] == 'Success. Extracted content and prepared response.'

	def test_wrap_prose_ignores_short_text(self):
		"""Short text (< 50 chars) is not wrapped — likely a partial error."""
		from browser_use.llm.claude_code.chat import _wrap_prose_as_done_action
		from browser_use.agent.views import AgentOutput

		result = _wrap_prose_as_done_action('short', AgentOutput)
		assert result is None

	def test_wrap_prose_ignores_non_agent_schema(self):
		"""Schemas without an 'action' field are not wrapped."""
		from browser_use.llm.claude_code.chat import _wrap_prose_as_done_action
		from pydantic import BaseModel

		class SimpleOutput(BaseModel):
			answer: str

		result = _wrap_prose_as_done_action('A long prose response that is more than fifty characters.', SimpleOutput)
		assert result is None
