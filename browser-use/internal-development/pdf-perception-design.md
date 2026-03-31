# PDF Handling Fix for browser-use

**Status**: Design (implementation-ready)
**Date**: 2026-03-26
**Related**: [OCR + SoM design](ocr-som-design.md)

---

## Problem Statement

Chrome's built-in PDF viewer runs as a plugin process outside CDP's reach. When the agent navigates to a PDF URL:

1. `Page.captureScreenshot` times out for 15s (the plugin renders outside the CDP target)
2. DOM returns 0 interactive elements (only Chrome's PDF toolbar)
3. `enhance_observation()` receives `screenshot_b64=None`, returns `None`
4. The agent is told *"PDF viewer cannot be rendered. Use read_file."*
5. The LLM fails to emit valid structured `read_file` action calls (observed: 4 consecutive parse failures)
6. Each step wastes ~30s (15s screenshot timeout + failed LLM retry)

The agent gets completely stuck.

### What OCR + SoM is NOT for

PDFs are a text extraction problem, not a visual perception problem:

```
WRONG:  PDF (text) → pdf.js → canvas (pixels) → OCR → text (lossy, slow)
RIGHT:  PDF (text) → text extraction → text (lossless, fast)
```

The one exception is **scanned/image-only PDFs** where text extraction returns nothing — those genuinely need OCR on rasterized pages, not through a browser canvas.

### Real Problems to Fix

1. **The agent wastes 15s per step** on screenshot timeouts it can never recover from
2. **The agent can't call `read_file`** despite being told to — LLM produces free-text instead of structured actions
3. **PDF detection is URL-only** — misses PDFs served from non-`.pdf` URLs
4. **No early bailout** — the system doesn't know to skip screenshot/perception on PDF pages

---

## Architecture: What Should Happen

```
Agent navigates to .pdf URL
    │
    ▼
DownloadsWatchdog detects PDF (existing, works)
    │
    ├─ Downloads PDF to disk ✓
    ├─ Stores URL→path in _session_pdf_urls ✓
    │
    ▼
DOMWatchdog handles BrowserStateRequestEvent
    │
    ├─ Detects PDF page BEFORE starting screenshot → SKIP screenshot
    ├─ Extracts text from downloaded PDF via FileSystem.read_file_structured()
    ├─ Returns BrowserStateSummary with pdf_extracted_text populated
    │
    ▼
Agent receives state with:
    ├─ is_pdf_viewer = True
    ├─ screenshot = None (expected, not an error)
    ├─ <pdf_content> block with extracted text in the prompt
    │
    ▼
Agent sees the PDF text directly — no action needed, responds in 1 step
```

---

## Implementation

Six fixes across 4 files. Each fix is independently testable.

### Fix 1: Move PDF Detection Before Screenshot (dom_watchdog.py)

**Problem**: Currently, `is_pdf_viewer` is checked at line 457, but the screenshot task is already created and awaited at lines 374-403. By the time we know it's a PDF, the 15s screenshot timeout has already happened.

**Current code flow** (lines 352-457 of `dom_watchdog.py`):

```
Line 352: dom_task = None; screenshot_task = None
Line 366: dom_task = create_task(self._build_dom_tree_without_highlights(...))
Line 376: screenshot_task = create_task(self._capture_clean_screenshot())   ← screenshot starts here
Line 397: screenshot_b64 = await screenshot_task                            ← 15s timeout happens here
...
Line 457: is_pdf_viewer = page_url.endswith('.pdf') or '/pdf/' in page_url  ← too late!
```

**Fix**: Check for PDF BEFORE creating the screenshot task. Insert at line 353, right after `dom_task = None; screenshot_task = None`:

```python
# In on_BrowserStateRequestEvent, at line 353, BEFORE the screenshot task creation:

# --- PDF early detection: skip screenshot for PDF pages ---
# Chrome's PDF plugin runs outside CDP — captureScreenshot will timeout.
# Detect PDF early to avoid wasting 15s per step.
is_pdf_viewer = self._detect_pdf_page(page_url)
skip_screenshot = is_pdf_viewer
```

Then modify the screenshot task creation at line 374:

```python
# BEFORE (line 374):
if event.include_screenshot:

# AFTER:
if event.include_screenshot and not skip_screenshot:
```

Remove the old detection at line 457:

```python
# REMOVE line 457:
# is_pdf_viewer = page_url.endswith('.pdf') or '/pdf/' in page_url
# (already computed above as part of early detection)
```

**New method to add to DOMWatchdog class:**

```python
def _detect_pdf_page(self, url: str) -> bool:
	"""Detect PDF pages by URL pattern or DownloadsWatchdog's PDF tracking."""
	# URL pattern (existing logic, expanded)
	url_lower = url.lower().split('?')[0].split('#')[0]
	if url_lower.endswith('.pdf'):
		return True

	# Check if DownloadsWatchdog already identified this URL as a PDF
	# _session_pdf_urls is dict[str, str] mapping URL → downloaded file path
	downloads_watchdog = getattr(self.browser_session, '_downloads_watchdog', None)
	if downloads_watchdog and hasattr(downloads_watchdog, '_session_pdf_urls'):
		if url in downloads_watchdog._session_pdf_urls:
			return True

	return False
```

**Why use `_session_pdf_urls` instead of `_navigation_content_types`**: The attribute `_navigation_content_types` does not exist in DownloadsWatchdog. However, `_session_pdf_urls: dict[str, str]` (line 65) tracks every URL that was detected as a PDF and downloaded. If the URL is in this dict, it's a PDF. This is more reliable than Content-Type checking because the download detection already handles Content-Type, Content-Disposition, and URL pattern checks.

---

### Fix 2: Auto-Extract PDF Text into State (dom_watchdog.py)

**Where to insert**: After the early PDF detection (Fix 1), still before the screenshot/DOM task await block. Insert after `skip_screenshot = is_pdf_viewer`:

```python
# --- PDF text extraction ---
pdf_extracted_text: str | None = None
if is_pdf_viewer:
	pdf_extracted_text = await self._extract_pdf_text()
```

**New method to add to DOMWatchdog class:**

```python
async def _extract_pdf_text(self, max_chars: int = 8000) -> str | None:
	"""Extract text from the most recently downloaded PDF.

	Uses the existing FileSystem.read_file_structured() which already
	supports PDF text extraction.
	"""
	downloads_watchdog = getattr(self.browser_session, '_downloads_watchdog', None)
	if not downloads_watchdog or not hasattr(downloads_watchdog, '_session_pdf_urls'):
		return None

	# _session_pdf_urls is dict[url, path] — get the most recent entry
	pdf_urls = downloads_watchdog._session_pdf_urls
	if not pdf_urls:
		return None
	pdf_path = list(pdf_urls.values())[-1]

	if not Path(pdf_path).exists():
		self.logger.warning(f'PDF file not found at {pdf_path}')
		return None

	try:
		from browser_use.tools.file_system import FileSystem

		fs = FileSystem(downloads_path=Path(pdf_path).parent)
		# read_file_structured is at file_system.py line 506
		# params: full_filename: str, external_file: bool = False
		# returns: dict with 'message' (str) and 'images' (list[dict] | None)
		result = await fs.read_file_structured(pdf_path, external_file=True)
		text = result.get('message', '')
		if not text or not text.strip():
			return None
		if len(text) > max_chars:
			text = text[:max_chars] + f'\n\n[... truncated, {len(text)} total chars. Use read_file for full content.]'
		return text
	except Exception as e:
		self.logger.warning(f'PDF text extraction failed: {e}')
		return None
```

**Note**: Add `from pathlib import Path` to imports if not already present.

---

### Fix 3: Add `pdf_extracted_text` to BrowserStateSummary (views.py)

**File**: `browser_use/browser/views.py`
**Where**: Inside the `BrowserStateSummary` dataclass (line 90), after the existing `is_pdf_viewer` field (line 107).

```python
# Add after line 107 (is_pdf_viewer):
pdf_extracted_text: str | None = None  # Auto-extracted text from downloaded PDF
```

**Also update**: The `BrowserStateSummary` construction in `dom_watchdog.py`. There are two places it's constructed:

1. **Empty tab case** (around line 335): Add `pdf_extracted_text=None`
2. **Normal case** (around line 494-510): Add `pdf_extracted_text=pdf_extracted_text`

The normal case construction looks like (line ~494):
```python
browser_state = BrowserStateSummary(
    dom_state=content,
    url=page_url,
    title=title,
    ...
    is_pdf_viewer=is_pdf_viewer,
    pdf_extracted_text=pdf_extracted_text,  # ← ADD THIS
    ...
)
```

---

### Fix 4: Include PDF Text in Agent Prompt (prompts.py)

**File**: `browser_use/agent/prompts.py`
**Where**: Lines 292-300, the existing PDF message block.

**BEFORE** (current code, lines 292-300):

```python
# Check if current page is a PDF viewer and add appropriate message
pdf_message = ''
if self.browser_state.is_pdf_viewer:
    pdf_message = (
        'PDF viewer cannot be rendered. In this page, DO NOT use the extract action as PDF content cannot be rendered. '
    )
    pdf_message += (
        'Use the read_file action on the downloaded PDF in available_file_paths to read the full text content.\n\n'
    )
```

**AFTER** (replacement):

```python
# Check if current page is a PDF viewer and add appropriate message
pdf_message = ''
if self.browser_state.is_pdf_viewer:
    if self.browser_state.pdf_extracted_text:
        pdf_message = (
            'This is a PDF document. The extracted text content is shown below. '
            'The browser cannot render this page visually.\n\n'
            '<pdf_content>\n'
            f'{self.browser_state.pdf_extracted_text}\n'
            '</pdf_content>\n\n'
        )
    else:
        pdf_message = (
            'PDF viewer detected but text extraction failed. '
            'Use the read_file action on the downloaded PDF in '
            'available_file_paths to read the content.\n\n'
        )
```

---

### Fix 5: Skip Enhanced Perception on PDF Pages (dom_watchdog.py)

**Where**: Around lines 474-492 where `enhance_observation` is called. The existing code already won't run OCR when `screenshot_b64` is None, but we should be explicit:

```python
# BEFORE (line ~474):
enhanced_observation = None
if self.browser_session.browser_profile.perception_mode == PerceptionMode.ENHANCED:

# AFTER:
enhanced_observation = None
if self.browser_session.browser_profile.perception_mode == PerceptionMode.ENHANCED and not is_pdf_viewer:
```

This makes the skip intentional rather than relying on the `screenshot_b64=None` fallback.

---

### Fix 6: Scanned PDF OCR Fallback (dom_watchdog.py) — Optional

If text extraction returns empty/near-empty content, try OCR on rasterized pages. This is the ONE case where OCR is the right tool for PDFs.

**Modify `_extract_pdf_text`** to add a fallback branch:

```python
async def _extract_pdf_text(self, max_chars: int = 8000) -> str | None:
    # ... (existing code from Fix 2 to get pdf_path) ...

    # Try text extraction first
    text = await self._extract_text_from_pdf(pdf_path)

    # If text extraction yields very little, try OCR on rasterized pages
    if not text or len(text.strip()) < 100:
        ocr_text = await self._ocr_scanned_pdf(pdf_path)
        if ocr_text:
            text = ocr_text

    if not text or not text.strip():
        return None
    if len(text) > max_chars:
        text = text[:max_chars] + f'\n\n[... truncated, {len(text)} total chars. Use read_file for full content.]'
    return text

async def _extract_text_from_pdf(self, pdf_path: str) -> str | None:
    """Extract text using FileSystem (existing PDF reader)."""
    try:
        from browser_use.tools.file_system import FileSystem
        fs = FileSystem(downloads_path=Path(pdf_path).parent)
        result = await fs.read_file_structured(pdf_path, external_file=True)
        return result.get('message', '')
    except Exception as e:
        self.logger.warning(f'PDF text extraction failed: {e}')
        return None

async def _ocr_scanned_pdf(self, pdf_path: str, max_pages: int = 5) -> str | None:
    """Rasterize PDF pages and run OCR. For scanned/image-only PDFs."""
    try:
        from pdf2image import convert_from_path
        from browser_use.dom.ocr_engine import OCREngine

        images = convert_from_path(pdf_path, first_page=1, last_page=max_pages, dpi=200)
        engine = OCREngine.get()
        all_text = []
        for i, img in enumerate(images):
            detections = engine.detect(img, device_pixel_ratio=1.0, confidence_threshold=0.5)
            page_text = ' '.join(d.text for d in detections)
            if page_text:
                all_text.append(f'--- Page {i + 1} ---\n{page_text}')
        return '\n\n'.join(all_text) if all_text else None
    except ImportError:
        return None  # pdf2image not installed, skip silently
    except Exception as e:
        self.logger.warning(f'OCR on scanned PDF failed: {e}')
        return None
```

**Dependencies**: `pdf2image` is optional. If not installed, scanned PDF OCR is silently skipped. Add to `pyproject.toml` as optional: `browser-use[ocr-pdf]` or similar.

---

## Implementation Order

Execute in this exact order. Each step is testable independently.

| Step | Fix | File | What to do | Test |
|------|-----|------|------------|------|
| 1 | Fix 3 | `browser_use/browser/views.py` | Add `pdf_extracted_text: str \| None = None` field to `BrowserStateSummary` after line 107 | Import and instantiate `BrowserStateSummary` with the new field |
| 2 | Fix 1 | `browser_use/browser/watchdogs/dom_watchdog.py` | Add `_detect_pdf_page()` method. Move PDF detection to line 353, before screenshot task. Add `skip_screenshot` guard. Remove old line 457 | Navigate to `.pdf` URL, verify no screenshot timeout |
| 3 | Fix 5 | `browser_use/browser/watchdogs/dom_watchdog.py` | Add `and not is_pdf_viewer` guard to enhanced perception block (~line 474) | Navigate to `.pdf`, verify no OCR attempt |
| 4 | Fix 2 | `browser_use/browser/watchdogs/dom_watchdog.py` | Add `_extract_pdf_text()` method. Call it after PDF detection, pass result to `BrowserStateSummary` constructor | Navigate to text PDF, verify `pdf_extracted_text` is populated |
| 5 | Fix 4 | `browser_use/agent/prompts.py` | Replace lines 292-300 with `<pdf_content>` injection logic | Verify agent state description contains `<pdf_content>` block |
| 6 | Fix 6 | `browser_use/browser/watchdogs/dom_watchdog.py` | Split `_extract_pdf_text` into text-first + OCR fallback | Serve image-only PDF, verify OCR kicks in |
| 7 | Tests | `tests/ci/test_pdf_handling.py` | Full test suite (see below) | `uv run pytest -vxs tests/ci/test_pdf_handling.py` |

---

## Testing Strategy

### Test setup

Use `pytest-httpserver` to serve PDFs. Generate fixture PDFs with `reportlab` or use static fixtures.

```python
# tests/ci/test_pdf_handling.py

import pytest
from pathlib import Path
from pytest_httpserver import HTTPServer

@pytest.fixture
def text_pdf_bytes() -> bytes:
    """Generate a minimal PDF with known text: 'Hello World' on page 1."""
    from reportlab.pdfgen import canvas as rl_canvas
    from io import BytesIO
    buf = BytesIO()
    c = rl_canvas.Canvas(buf)
    c.drawString(100, 700, 'Hello World')
    c.drawString(100, 680, 'Total Revenue: $847.3 million')
    c.showPage()
    c.save()
    return buf.getvalue()

@pytest.fixture
def pdf_server(httpserver: HTTPServer, text_pdf_bytes: bytes):
    httpserver.expect_request('/test.pdf').respond_with_data(
        text_pdf_bytes, content_type='application/pdf'
    )
    httpserver.expect_request('/document/view').respond_with_data(
        text_pdf_bytes, content_type='application/pdf'
    )
    return httpserver
```

### Test cases

1. **No screenshot timeout on PDF page**: Navigate to `/test.pdf`, verify `BrowserStateSummary` is returned in <5s (not 30s)
2. **`pdf_extracted_text` populated**: Navigate to `/test.pdf`, verify `state.pdf_extracted_text` contains 'Hello World'
3. **`is_pdf_viewer` is True**: Navigate to `/test.pdf`, verify `state.is_pdf_viewer == True`
4. **`screenshot` is None (expected)**: Navigate to `/test.pdf`, verify `state.screenshot is None`
5. **Prompt contains `<pdf_content>`**: Build agent prompt from state, verify `<pdf_content>` in output
6. **No enhanced perception on PDF**: Navigate to PDF with `perception_mode='enhanced'`, verify `enhanced_observation is None`
7. **read_file still works**: Verify downloaded PDF is in `available_file_paths` and readable
8. **URL-based detection**: Verify `/test.pdf` is detected
9. **Download-based detection**: Verify `/document/view` (no `.pdf` extension but Content-Type `application/pdf`) is detected via `_session_pdf_urls`
10. **Large PDF truncation**: Create 50-page PDF, verify text is truncated with message

---

## Expected Impact

| Metric | Before | After |
|--------|--------|-------|
| Time per step on PDF page | ~30s (15s timeout + retry) | <2s |
| Agent steps to read PDF | 5+ (all failing) | 1 (text in state) |
| Screenshot timeout errors | Every step | None |
| LLM parse failures | 4/5 steps | 0 |
| PDF text available to agent | Never (can't call read_file) | Immediately in `<pdf_content>` |
| Scanned PDF support | None | OCR on rasterized pages (Fix 6) |

---

## Files Changed Summary

| File | Changes |
|------|---------|
| `browser_use/browser/views.py` | Add `pdf_extracted_text: str \| None = None` to `BrowserStateSummary` |
| `browser_use/browser/watchdogs/dom_watchdog.py` | Add `_detect_pdf_page()`, `_extract_pdf_text()`, `_extract_text_from_pdf()`, `_ocr_scanned_pdf()` methods. Move PDF detection before screenshot task. Add `skip_screenshot` guard. Add `is_pdf_viewer` guard to enhanced perception. Pass `pdf_extracted_text` to `BrowserStateSummary` |
| `browser_use/agent/prompts.py` | Replace lines 292-300 with `<pdf_content>` injection or fallback message |
| `tests/ci/test_pdf_handling.py` | New test file with 10 test cases |
