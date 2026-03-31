# OCR + SoM Observation Enhancement for browser-use

**Status**: Design (implementation-ready)
**Date**: 2025-03-25
**Context**: Adapting mobile-use's OCR + SoM (Set-of-Mark) perception pipeline for browser-use's CDP-based architecture to improve accuracy and generalization.

---

## Problem Statement

browser-use's current observation pipeline relies on CDP DOM extraction + accessibility tree + optional screenshot highlighting. This works well for standard HTML but has blind spots:

| Scenario | CDP DOM | OCR | Impact |
|----------|---------|-----|--------|
| Canvas/WebGL content (charts, games, maps) | No text nodes | Sees rendered text | High |
| Images with embedded text (captchas, scanned docs) | Only `alt` attr | Reads actual text | High |
| PDF viewers rendering as images | Empty DOM | Reads content | Medium |
| Shadow DOM / closed shadow roots | Limited access | Sees rendered output | Medium |
| CSS pseudo-elements (::before, ::after) | Not in DOM text | Visible on screen | Low |
| Dynamic overlays (cookie banners from iframes) | Cross-origin blocked | Sees rendered text | Medium |
| Visually hidden elements (display:none but in DOM) | Reports present | Won't detect | Validation |

Adding OCR as a secondary observation channel and SoM for tighter visual grounding addresses these gaps without replacing the high-fidelity CDP primary channel.

---

## Architecture Overview

```
Screenshot (PNG)
     │
     ├──────────────────────────────────┐
     ▼                                  ▼
  CDP Pipeline (existing)          OCR Pipeline (new)
  ┌─────────────────────┐         ┌─────────────────────┐
  │ DOM Snapshot         │         │ OCR Engine           │
  │ Accessibility Tree   │         │ (PaddleOCR)          │
  │ Paint Order          │         │                     │
  │ Clickable Detection  │         │ → text + bbox +     │
  │                      │         │   confidence        │
  │ → EnhancedDOMTreeNode│         │ → OCRDetection      │
  └──────────┬───────────┘         └──────────┬──────────┘
             │                                │
             ▼                                ▼
       ┌─────────────────────────────────────────┐
       │         Element Merger                   │
       │  • CDP elements = primary (confidence 1) │
       │  • OCR fills gaps (canvas, images, etc.) │
       │  • Dedup by text + proximity (20px)      │
       │  • Sort by reading order (Y, X)          │
       │  • Assign sequential IDs (1, 2, 3...)    │
       └──────────────────┬──────────────────────┘
                          │
                          ▼
       ┌─────────────────────────────────────────┐
       │         SoM Overlay Renderer             │
       │  • Draw numbered circles at centers      │
       │  • Green = clickable, Gray = static      │
       │  • Truncated text labels                 │
       │  • Composite onto screenshot             │
       └──────────────────┬──────────────────────┘
                          │
                          ▼
       ┌─────────────────────────────────────────┐
       │         LLM Message Builder              │
       │  • SoM-annotated screenshot              │
       │  • Unified element list text             │
       │  • DOM tree (existing, unchanged)        │
       └─────────────────────────────────────────┘
```

---

## Implementation Order

Execute in this order. Each step is testable independently.

1. **`browser_use/dom/ocr_engine.py`** — NEW. OCR wrapper, no dependencies on browser-use internals.
2. **`browser_use/dom/views.py`** — MODIFY. Add `Literal` import, `UnifiedElement` and `EnhancedObservation` models.
3. **`browser_use/dom/element_merger.py`** — NEW. Merge logic, depends only on `UnifiedElement`.
4. **`browser_use/dom/som_overlay.py`** — NEW. SoM rendering as PNG, depends only on `UnifiedElement` + PIL.
5. **`browser_use/dom/perception.py`** — NEW. Pipeline orchestrator, wires steps 1-4 together.
6. **`browser_use/browser/profile.py`** — MODIFY. Add `perception_mode` field + env var default.
7. **`browser_use/browser/views.py`** — MODIFY. Add `enhanced_observation` to `BrowserStateSummary`.
8. **`browser_use/browser/watchdogs/dom_watchdog.py`** — MODIFY. Return `device_pixel_ratio` from `_get_page_info()`, call perception pipeline before `BrowserStateSummary` construction.
9. **`browser_use/agent/prompts.py`** — MODIFY. Inject `<unified_elements>` into `state_description` in `get_user_message()`.
10. **`browser_use/agent/message_manager/service.py`** — MODIFY. Replace screenshot with SoM version in `create_state_messages()`.
11. **`browser_use/agent/service.py`** — MODIFY. Inject enhanced prompt section via `extend_system_message` in `Agent.__init__()`.
12. **`browser_use/tools/views.py`** — MODIFY (Phase 2). Add `element_id` to `ClickElementAction`.
13. **`browser_use/tools/service.py`** — MODIFY (Phase 2). Handle `element_id` click resolution in `_register_click_action()`.
14. **Tests** — `tests/ci/test_perception.py`. Write after each step, full suite at end.

---

## Detailed Design

### 1. OCR Engine (`browser_use/dom/ocr_engine.py`) — NEW

```python
import logging
from typing import Literal

import numpy as np
from PIL import Image
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class OCRDetection(BaseModel):
	"""A single text region detected by OCR."""
	text: str
	bounds: tuple[int, int, int, int]  # (left, top, right, bottom) in CSS pixels
	center: tuple[int, int]            # (cx, cy) in CSS pixels
	confidence: float
	source: Literal['ocr'] = 'ocr'


class OCREngine:
	"""Lazy-loaded PaddleOCR singleton. ~2s init, amortized across agent lifetime."""

	_instance: 'OCREngine | None' = None
	_engine: 'PaddleOCR | None' = None

	@classmethod
	def get(cls) -> 'OCREngine':
		if cls._instance is None:
			cls._instance = cls()
		return cls._instance

	def _ensure_engine(self) -> 'PaddleOCR':
		if self._engine is None:
			try:
				from paddleocr import PaddleOCR
			except ImportError:
				raise ImportError(
					'paddleocr is required for enhanced perception mode. '
					'Install it with: pip install browser-use[ocr]'
				)
			self._engine = PaddleOCR(use_angle_cls=True, lang='en', show_log=False, use_gpu=True)
		return self._engine

	def detect(
		self,
		screenshot: Image.Image,
		device_pixel_ratio: float = 1.0,
		confidence_threshold: float = 0.5,
	) -> list[OCRDetection]:
		"""
		Run OCR on screenshot, return detections in CSS pixel coordinates.

		PaddleOCR returns polygon corners in screenshot pixels.
		We convert to CSS pixels by dividing by device_pixel_ratio.
		"""
		engine = self._ensure_engine()
		img_array = np.array(screenshot)

		try:
			results = engine.ocr(img_array, cls=True)
		except Exception as e:
			logger.warning(f'OCR failed (non-fatal): {e}')
			return []

		if not results or not results[0]:
			return []

		detections: list[OCRDetection] = []
		for line in results[0]:
			polygon, (text, confidence) = line
			if confidence < confidence_threshold:
				continue
			text = text.strip()
			if not text:
				continue

			# Polygon → bounding box (in screenshot pixels)
			xs = [p[0] for p in polygon]
			ys = [p[1] for p in polygon]

			# Convert screenshot pixels → CSS pixels
			scale = 1.0 / device_pixel_ratio
			left = int(min(xs) * scale)
			top = int(min(ys) * scale)
			right = int(max(xs) * scale)
			bottom = int(max(ys) * scale)
			cx = (left + right) // 2
			cy = (top + bottom) // 2

			detections.append(OCRDetection(
				text=text,
				bounds=(left, top, right, bottom),
				center=(cx, cy),
				confidence=confidence,
			))

		return detections
```

### 2. Unified Element Model (`browser_use/dom/views.py`) — MODIFY

Add these models at the end of the file. Uses Pydantic BaseModel per project conventions.

```python
# --- Add to browser_use/dom/views.py ---

from typing import Literal

from pydantic import BaseModel, ConfigDict


class UnifiedElement(BaseModel):
	"""Element from either CDP or OCR, with a sequential SoM ID."""
	model_config = ConfigDict(extra='forbid')

	id: int                                     # Sequential SoM ID (1, 2, 3...)
	text: str
	bounds: tuple[int, int, int, int]           # CSS pixels (left, top, right, bottom)
	center: tuple[int, int]                     # CSS pixels (cx, cy)
	is_interactive: bool
	source: Literal['cdp', 'ocr']
	confidence: float                           # 1.0 for CDP, OCR confidence for OCR
	backend_node_id: int | None = None          # CDP only — used for native click targeting
	tag_name: str | None = None                 # CDP only
	role: str | None = None                     # CDP only (AX role)


class EnhancedObservation(BaseModel):
	"""Result of the enhanced perception pipeline (OCR + SoM)."""
	model_config = ConfigDict(extra='forbid')

	som_screenshot_b64: str                     # JPEG with SoM markers
	elements: list[UnifiedElement]              # Merged, deduped, ID-assigned
	element_list_text: str                      # Formatted for LLM
	element_count: int
```

### 3. Element Merger (`browser_use/dom/element_merger.py`) — NEW

```python
PROXIMITY_THRESHOLD_PX = 20  # Same as mobile-use


def _is_duplicate(a: UnifiedElement, b: UnifiedElement, threshold: int) -> bool:
	"""Two elements are duplicates if same text (case-insensitive) and centers within threshold."""
	if a.text.strip().lower() != b.text.strip().lower():
		return False
	return abs(a.center[0] - b.center[0]) <= threshold and abs(a.center[1] - b.center[1]) <= threshold


def merge_elements(
	cdp_elements: list[UnifiedElement],
	ocr_elements: list[UnifiedElement],
	proximity_threshold: int = PROXIMITY_THRESHOLD_PX,
) -> list[UnifiedElement]:
	"""
	Merge CDP and OCR elements. CDP is primary (always kept). OCR fills gaps.

	Steps:
	1. Start with all CDP elements
	2. For each OCR element, skip if any CDP element is a duplicate
	3. Sort merged list by reading order (top→bottom, left→right)
	4. Assign sequential IDs starting from 1
	"""
	merged = list(cdp_elements)

	for ocr_el in ocr_elements:
		is_dup = any(_is_duplicate(ocr_el, cdp_el, proximity_threshold) for cdp_el in cdp_elements)
		if not is_dup:
			merged.append(ocr_el)

	# Sort by reading order
	merged.sort(key=lambda el: (el.center[1], el.center[0]))

	# Assign sequential IDs
	for i, el in enumerate(merged, start=1):
		el.id = i

	return merged


def format_element_list(elements: list[UnifiedElement], max_text_len: int = 80) -> str:
	"""
	Format element list for LLM consumption.

	Output format:
	#1 "Submit" (button, clickable) center=(450,320) [cdp:523]
	#4 "Chart: Q3 Revenue" (non-clickable) center=(300,150) [ocr:0.92]
	"""
	lines: list[str] = []
	for el in elements:
		text = el.text[:max_text_len] + ('...' if len(el.text) > max_text_len else '')
		interactive_label = 'clickable' if el.is_interactive else 'non-clickable'

		if el.tag_name or el.role:
			type_label = el.role or el.tag_name or ''
			desc = f'({type_label}, {interactive_label})'
		else:
			desc = f'({interactive_label})'

		if el.source == 'cdp' and el.backend_node_id is not None:
			source_tag = f'[cdp:{el.backend_node_id}]'
		else:
			source_tag = f'[ocr:{el.confidence:.2f}]'

		lines.append(f'#{el.id} "{text}" {desc} center={el.center} {source_tag}')

	return '\n'.join(lines)
```

### 4. SoM Overlay Renderer (`browser_use/dom/som_overlay.py`) — NEW

```python
import base64
import io

from PIL import Image, ImageDraw, ImageFont

from browser_use.dom.views import UnifiedElement

MARKER_RADIUS = 14
COLOR_INTERACTIVE = (0, 180, 0, 200)     # Semi-transparent green
COLOR_STATIC = (140, 140, 140, 200)       # Semi-transparent gray
COLOR_OCR_ONLY = (0, 120, 200, 200)       # Semi-transparent blue (OCR-only elements)
COLOR_TEXT = (255, 255, 255)              # White
LABEL_BG = (0, 0, 0, 160)               # Dark background for labels
LABEL_MAX_LEN = 25
LABEL_OFFSET_X = 18


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
	"""Load font with macOS/Linux fallback."""
	for path in ['/System/Library/Fonts/Helvetica.ttc', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']:
		try:
			return ImageFont.truetype(path, size)
		except (OSError, IOError):
			continue
	return ImageFont.load_default()


def render_som_overlay(
	screenshot: Image.Image,
	elements: list[UnifiedElement],
	device_pixel_ratio: float = 1.0,
	max_width: int = 1024,
) -> str:
	"""
	Draw SoM markers on screenshot, return compressed JPEG base64.

	Elements have centers in CSS pixels. Screenshot is in device pixels.
	Scale centers by device_pixel_ratio to get screenshot pixel coordinates.
	"""
	img = screenshot.copy().convert('RGBA')
	overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
	draw = ImageDraw.Draw(overlay)

	font_id = _load_font(12)
	font_label = _load_font(10)

	for el in elements:
		# Convert CSS pixels → screenshot pixels
		cx = int(el.center[0] * device_pixel_ratio)
		cy = int(el.center[1] * device_pixel_ratio)

		# Choose color
		if el.source == 'ocr':
			color = COLOR_OCR_ONLY
		elif el.is_interactive:
			color = COLOR_INTERACTIVE
		else:
			color = COLOR_STATIC

		# Draw circle marker
		r = MARKER_RADIUS
		draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)

		# Draw ID number centered in circle
		id_text = str(el.id)
		bbox = draw.textbbox((0, 0), id_text, font=font_id)
		tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
		draw.text((cx - tw // 2, cy - th // 2), id_text, fill=COLOR_TEXT, font=font_id)

		# Draw truncated text label
		label = el.text[:LABEL_MAX_LEN]
		if len(el.text) > LABEL_MAX_LEN:
			label += '...'
		label_x = cx + LABEL_OFFSET_X
		label_y = cy - 6

		lbox = draw.textbbox((0, 0), label, font=font_label)
		lw, lh = lbox[2] - lbox[0], lbox[3] - lbox[1]
		draw.rectangle([label_x - 2, label_y - 1, label_x + lw + 2, label_y + lh + 1], fill=LABEL_BG)
		draw.text((label_x, label_y), label, fill=COLOR_TEXT, font=font_label)

	# Composite and convert
	result = Image.alpha_composite(img, overlay).convert('RGB')

	# Resize if needed
	if result.width > max_width:
		ratio = max_width / result.width
		new_size = (max_width, int(result.height * ratio))
		result = result.resize(new_size, Image.LANCZOS)

	# Encode to PNG base64 (PNG is lossless — better for numbered markers,
	# and matches the image/png MIME type used in get_user_message())
	buf = io.BytesIO()
	result.save(buf, format='PNG')
	return base64.b64encode(buf.getvalue()).decode('utf-8')
```

### 5. Enhanced Perception Pipeline (`browser_use/dom/perception.py`) — NEW

Orchestrates all components. This is the main entry point called from DOMWatchdog.

```python
import base64
import io
import logging
from enum import Enum

from PIL import Image

from browser_use.dom.views import EnhancedObservation, SerializedDOMState, UnifiedElement

logger = logging.getLogger(__name__)


class PerceptionMode(str, Enum):
	CLASSIC = 'classic'
	ENHANCED = 'enhanced'


def _cdp_to_unified_elements(dom_state: SerializedDOMState) -> list[UnifiedElement]:
	"""
	Convert CDP selector_map entries → UnifiedElement list.

	EnhancedDOMTreeNode has:
	- backend_node_id: int
	- tag_name: str
	- ax_node: EnhancedAXNode | None  (has .role, .name)
	- snapshot_node: EnhancedSnapshotNode | None  (has .clientRects: DOMRect with x,y,width,height in viewport coords)
	"""
	elements: list[UnifiedElement] = []

	for backend_node_id, node in dom_state.selector_map.items():
		# Extract viewport-relative bounds from snapshot_node.clientRects
		# NOTE: We use clientRects (viewport coordinates), NOT bounds (document coordinates).
		# bounds is relative to the page origin and ignores scroll position, which would
		# produce wrong SoM marker positions and click coordinates on scrolled pages.
		# clientRects matches what getBoundingClientRect() returns in JS.
		if not node.snapshot_node or not node.snapshot_node.clientRects:
			continue

		b = node.snapshot_node.clientRects
		left, top = int(b.x), int(b.y)
		right, bottom = int(b.x + b.width), int(b.y + b.height)
		cx, cy = (left + right) // 2, (top + bottom) // 2

		# Extract text: prefer AX name, fallback to node text content
		text = ''
		if node.ax_node and node.ax_node.name:
			text = node.ax_node.name
		if not text:
			# Gather text from attributes that commonly hold labels
			for attr in ['value', 'placeholder', 'aria-label', 'alt', 'title']:
				if node.attributes and attr in node.attributes:
					text = str(node.attributes[attr])
					if text:
						break

		role = node.ax_node.role if node.ax_node else None

		elements.append(UnifiedElement(
			id=0,  # Assigned later by merger
			text=text,
			bounds=(left, top, right, bottom),
			center=(cx, cy),
			is_interactive=True,  # Everything in selector_map is interactive
			source='cdp',
			confidence=1.0,
			backend_node_id=backend_node_id,
			tag_name=node.tag_name,
			role=role,
		))

	return elements


async def enhance_observation(
	screenshot_b64: str,
	dom_state: SerializedDOMState,
	device_pixel_ratio: float,
	ocr_confidence_threshold: float = 0.5,
) -> EnhancedObservation | None:
	"""
	Full enhanced perception pipeline:
	1. Decode screenshot
	2. Convert CDP selector_map → UnifiedElement list
	3. Run OCR → OCR UnifiedElement list
	4. Merge & dedup (CDP preferred)
	5. Render SoM overlay
	6. Format element list text
	7. Return EnhancedObservation

	Returns None on failure (non-fatal — caller falls back to classic mode).
	"""
	try:
		# Step 1: Decode screenshot
		img_bytes = base64.b64decode(screenshot_b64)
		screenshot = Image.open(io.BytesIO(img_bytes))

		# Step 2: CDP → UnifiedElement
		cdp_elements = _cdp_to_unified_elements(dom_state)

		# Step 3: OCR → UnifiedElement
		from browser_use.dom.ocr_engine import OCREngine
		ocr_detections = OCREngine.get().detect(
			screenshot,
			device_pixel_ratio=device_pixel_ratio,
			confidence_threshold=ocr_confidence_threshold,
		)
		ocr_elements = [
			UnifiedElement(
				id=0,
				text=d.text,
				bounds=d.bounds,
				center=d.center,
				is_interactive=False,  # OCR can't determine interactivity
				source='ocr',
				confidence=d.confidence,
			)
			for d in ocr_detections
		]

		# Step 4: Merge & dedup
		from browser_use.dom.element_merger import format_element_list, merge_elements
		merged = merge_elements(cdp_elements, ocr_elements)

		# Step 5: Render SoM overlay
		from browser_use.dom.som_overlay import render_som_overlay
		som_b64 = render_som_overlay(
			screenshot, merged, device_pixel_ratio=device_pixel_ratio,
		)

		# Step 6: Format element list
		element_text = format_element_list(merged)

		# Step 7: Return
		return EnhancedObservation(
			som_screenshot_b64=som_b64,
			elements=merged,
			element_list_text=element_text,
			element_count=len(merged),
		)

	except ImportError:
		raise  # Let missing paddleocr propagate as a clear error
	except Exception as e:
		logger.warning(f'Enhanced perception failed (falling back to classic): {e}')
		return None
```

### 6. Integration Points

#### 6a. BrowserProfile (`browser_use/browser/profile.py`) — MODIFY

Add two fields. `extra='ignore'` on the model means this is safe.

```python
# Add import at top:
from browser_use.dom.perception import PerceptionMode

# Add default-value helper near top of file, following the existing
# _get_enable_default_extensions_default() pattern (line 18):
def _get_perception_mode_default() -> str:
	"""Get default perception mode from env var or 'classic'."""
	return os.getenv('BROWSER_USE_PERCEPTION', 'classic')

# Add fields to BrowserProfile class:
	perception_mode: PerceptionMode = Field(
		default_factory=lambda: PerceptionMode(_get_perception_mode_default()),
		description='Perception pipeline mode. "enhanced" enables OCR + SoM overlay. '
		'Override via BROWSER_USE_PERCEPTION env var.',
	)
	ocr_confidence_threshold: float = Field(
		default=0.5,
		ge=0.0,
		le=1.0,
		description='Minimum OCR confidence to include a detection (enhanced mode only).',
	)
```

#### 6b. BrowserStateSummary (`browser_use/browser/views.py`) — MODIFY

Add one optional field to the dataclass.

```python
# Add import:
from browser_use.dom.views import EnhancedObservation

# Add field to BrowserStateSummary:
	enhanced_observation: EnhancedObservation | None = field(default=None, repr=False)
```

#### 6c. DOMWatchdog (`browser_use/browser/watchdogs/dom_watchdog.py`) — MODIFY

**Two changes required.** The `device_pixel_ratio` is computed inside `_get_page_info()` (line ~776) but
discarded — it's not stored in the returned `PageInfo`. The `BrowserStateSummary` is constructed in a
different method: `on_BrowserStateRequestEvent()` (line ~473). We need to thread the value across.

**Change 1: Modify `_get_page_info()` to return `device_pixel_ratio` alongside `PageInfo`.**

The method signature is at line 744. Change the return type and return statement:

```python
# BEFORE (line 744):
async def _get_page_info(self) -> 'PageInfo':

# AFTER:
async def _get_page_info(self) -> tuple['PageInfo', float]:
```

```python
# BEFORE (line 812-814):
		return page_info

# AFTER:
		return page_info, device_pixel_ratio
```

**Change 2: Update the call site in `on_BrowserStateRequestEvent()`.**

The call is at line 434. Update it to unpack the new return value, and update the fallback:

```python
# BEFORE (lines 434-453):
	page_info = await asyncio.wait_for(self._get_page_info(), timeout=1.0)
	...
except Exception as e:
	...
	page_info = PageInfo(...)

# AFTER:
	page_info, device_pixel_ratio = await asyncio.wait_for(self._get_page_info(), timeout=1.0)
	...
except Exception as e:
	...
	device_pixel_ratio = 1.0
	page_info = PageInfo(...)
```

**Change 3: Insert the perception call before `BrowserStateSummary(...)` construction (line ~472).**

`device_pixel_ratio` is now in scope. Insert immediately before line 473:

```python
# --- INSERT at line ~472, before BrowserStateSummary construction ---
from browser_use.dom.perception import PerceptionMode

enhanced_observation = None
if self.browser_session.browser_profile.perception_mode == PerceptionMode.ENHANCED:
	from browser_use.dom.perception import enhance_observation
	try:
		enhanced_observation = await enhance_observation(
			screenshot_b64=screenshot_b64,
			dom_state=content,  # SerializedDOMState
			device_pixel_ratio=device_pixel_ratio,
			ocr_confidence_threshold=self.browser_session.browser_profile.ocr_confidence_threshold,
		)
	except ImportError:
		raise  # Missing paddleocr — must be installed
	except Exception as e:
		self.logger.warning(f'Enhanced perception failed, falling back to classic: {e}')
		enhanced_observation = None
```

**Change 4: Add `enhanced_observation` to the `BrowserStateSummary(...)` constructor at line 473.**

```python
browser_state = BrowserStateSummary(
	dom_state=content,
	url=page_url,
	title=title,
	tabs=tabs_info,
	screenshot=screenshot_b64,
	page_info=page_info,
	pixels_above=0,
	pixels_below=0,
	browser_errors=[],
	is_pdf_viewer=is_pdf_viewer,
	recent_events=self._get_recent_events_str() if event.include_recent_events else None,
	pending_network_requests=pending_requests,
	pagination_buttons=pagination_buttons_data,
	closed_popup_messages=self.browser_session._closed_popup_messages.copy(),
	enhanced_observation=enhanced_observation,  # NEW
)
```

#### 6d. Message Manager (`browser_use/agent/message_manager/service.py`) — MODIFY

**Two files need changes**: `message_manager/service.py` (screenshot replacement) and `agent/prompts.py` (element list injection).

**Change 1: In `message_manager/service.py` → `create_state_messages()`, replace the SoM screenshot.**

Insert at line ~468 (after `screenshots.append(browser_state_summary.screenshot)` and before `effective_use_vision`):

```python
		# --- INSERT at line ~468, after screenshots are collected ---
		# Enhanced perception: replace clean screenshot with SoM-annotated version
		if browser_state_summary.enhanced_observation:
			obs = browser_state_summary.enhanced_observation
			if screenshots:
				screenshots = [obs.som_screenshot_b64]
```

No other changes to `service.py`. The `enhanced_element_text` is injected on the `AgentMessagePrompt` side
(see below), reading directly from `browser_state_summary.enhanced_observation` which is already passed
through as `browser_state_summary`.

**Change 2: In `agent/prompts.py` → `AgentMessagePrompt.__init__()`, no new parameter needed.**

`AgentMessagePrompt` already receives `browser_state_summary` (stored as `self.browser_state`).
The enhanced observation is accessible as `self.browser_state.enhanced_observation`. No constructor change.

**Change 3: In `agent/prompts.py` → `get_user_message()`, inject element list text.**

The method has two return paths (line 388-490):
- **Vision path** (line 428): builds `content_parts` list → `UserMessage(content=content_parts)`
- **Text-only path** (line 490): returns `UserMessage(content=state_description)`

Insert the element list into `state_description` so it appears in BOTH paths, right after the
`<browser_state>` block is built (line ~407). This places it alongside the DOM tree where
the LLM naturally reads element information:

```python
		# EXISTING (line 407):
		state_description += '<browser_state>\n' + self._get_browser_state_description().strip('\n') + '\n</browser_state>\n'

		# --- INSERT after line 407 ---
		# Enhanced perception: add unified element list after browser_state
		if self.browser_state.enhanced_observation:
			obs = self.browser_state.enhanced_observation
			state_description += (
				f'<unified_elements count="{obs.element_count}">\n'
				f'{obs.element_list_text}\n'
				f'</unified_elements>\n'
			)
```

This ensures the element list is:
- Present in both vision and text-only paths (it's in `state_description`, not `content_parts`)
- Adjacent to the DOM tree for natural reading order
- Wrapped in XML tags for structured parsing by the LLM
- Absent when `enhanced_observation` is `None` (classic mode)

**Change 4: Fix MIME type for SoM screenshot.**

The SoM overlay renders as JPEG (`som_overlay.py` line ~403: `result.save(buf, format='JPEG')`),
but `get_user_message()` hardcodes `image/png` in the data URI (line 453):
```python
url=f'data:image/png;base64,{processed_screenshot}'
```

Either fix the data URI to detect format, or change `som_overlay.py` to output PNG.
**Recommended**: Change `som_overlay.py` to output PNG — it avoids touching the existing
screenshot pipeline and PNG is lossless (better for numbered markers):

```python
# In som_overlay.py → render_som_overlay(), replace JPEG encoding:

# BEFORE:
	buf = io.BytesIO()
	result.save(buf, format='JPEG', quality=jpeg_quality)
	return base64.b64encode(buf.getvalue()).decode('utf-8')

# AFTER:
	buf = io.BytesIO()
	result.save(buf, format='PNG')
	return base64.b64encode(buf.getvalue()).decode('utf-8')
```

Remove the `jpeg_quality` parameter from `render_som_overlay()` signature since it's no longer used.

#### 6e. Module Imports

`browser_use/dom/` has no `__init__.py`. The new files (`ocr_engine.py`, `element_merger.py`,
`som_overlay.py`, `perception.py`) are imported via lazy `from browser_use.dom.X import Y` inside
functions (see `perception.py` lines 508-533). This is intentional — it avoids circular imports
and keeps PaddleOCR from loading at module import time.

**No `__init__.py` changes needed.** All cross-module imports use fully-qualified paths.
Verify that these import paths work by running `python -c "from browser_use.dom.perception import PerceptionMode"` after creating the files.

#### 6f. Relationship to Existing Highlights (`browser_use/browser/python_highlights.py`)

**SoM overlay does NOT replace existing highlights.** They serve different purposes:

| System | Purpose | When used |
|--------|---------|-----------|
| `python_highlights.py` | Debug visualization of CDP-detected interactive elements | Always (when use_vision=True) |
| SoM overlay | Unified CDP+OCR element visualization for LLM grounding | Enhanced mode only |

**In enhanced mode**: The SoM overlay screenshot replaces the clean screenshot sent to the LLM. The existing highlights are still applied to the debug screenshot (if debug logging is enabled). No conflict — they write to different images.

#### 6g. Action Grounding — Click Targeting (`browser_use/tools/views.py` + `service.py`) — MODIFY

**The key question**: How does the LLM reference OCR-only elements for clicking?

**Answer**: The LLM already supports coordinate-based clicking via `ClickElementAction(coordinate_x=..., coordinate_y=...)`. The element list text includes `center=(x,y)` for every element. When the LLM sees an OCR-only element like:
```
#4 "Chart: Q3 Revenue" (non-clickable) center=(300,150) [ocr:0.92]
```
It can click it using coordinates: `click(coordinate_x=300, coordinate_y=150)`.

**No schema changes needed for Phase 1.** The existing `ClickElementAction` already supports both index and coordinate modes. The SoM element list gives the LLM the coordinates it needs.

**Phase 2 optimization** (optional): Add `element_id` field to `ClickElementAction` for cleaner targeting.

**Change 1: `browser_use/tools/views.py`** — add one optional field:
```python
class ClickElementAction(BaseModel):
	index: int | None = Field(default=None, ge=1, description='Element index from browser_state')
	coordinate_x: int | None = Field(default=None, description='Horizontal coordinate')
	coordinate_y: int | None = Field(default=None, description='Vertical coordinate')
	element_id: int | None = Field(default=None, ge=1, description='SoM element ID from unified element list')  # NEW
```

**Change 2: `browser_use/tools/service.py`** — modify the click dispatch in `_register_click_action()`.

The click action dispatch is in `_register_click_action()` (line ~1978). The coordinate-enabled path
(line ~1990) dispatches to `self._click_by_index` or `self._click_by_coordinate`. Add `element_id`
resolution before the existing dispatch:

```python
# In _register_click_action(), inside the coordinate-enabled click handler (line ~1990):
async def click(params: ClickElementAction, browser_session: BrowserSession):
	# NEW: Resolve element_id to index or coordinates
	if params.element_id is not None:
		cached_state = browser_session._cached_browser_state_summary
		obs = cached_state.enhanced_observation if cached_state else None
		if obs:
			element = next((e for e in obs.elements if e.id == params.element_id), None)
			if element:
				if element.backend_node_id is not None:
					# CDP element — resolve to index-based click via selector_map lookup
					selector_map = await browser_session.get_selector_map()
					if element.backend_node_id in selector_map:
						params = ClickElementAction(index=element.backend_node_id)
						return await self._click_by_index(params, browser_session)
				# OCR-only or CDP element not in selector_map — fall back to coordinates
				params = ClickElementAction(
					coordinate_x=element.center[0],
					coordinate_y=element.center[1],
				)
				return await self._click_by_coordinate(params, browser_session)
			return ActionResult(error=f'Element ID {params.element_id} not found in unified element list')
		return ActionResult(error='Enhanced observation not available — use index or coordinates instead')

	# EXISTING dispatch logic unchanged:
	if params.index is None and (params.coordinate_x is None or params.coordinate_y is None):
		return ActionResult(error='Must provide either index or both coordinate_x and coordinate_y')
	if params.index is not None:
		return await self._click_by_index(params, browser_session)
	else:
		return await self._click_by_coordinate(params, browser_session)
```

**Key implementation notes:**
- `selector_map` keys ARE `backend_node_id` values (see `serializer.py:713`), so `element.backend_node_id in selector_map` is a direct O(1) lookup
- `self._click_by_index` expects `params.index` to be a `backend_node_id` (confusing naming in the codebase, but `selector_map[params.index]` at `service.py:821` confirms this)
- `self._click_by_coordinate` is a closure stored at `service.py:676`, takes `(params, browser_session)`
- `browser_session._cached_browser_state_summary` is set at `dom_watchdog.py:491` after every state request

#### 6h. System Prompt Changes

**Do NOT modify the static `.md` prompt files.** The system prompt is constructed at agent init time
via `SystemPrompt.__init__()` (in `agent/prompts.py`, line ~27), which does not receive
`perception_mode`. Threading it through would require changes to `SystemPrompt`, the `Agent`
constructor, and every prompt template variant.

Instead, inject the instructions dynamically via `extend_system_message` — the existing mechanism
for appending to the system prompt. This is already wired through `Agent.__init__()`.

**Change: In `browser_use/agent/service.py` → `Agent.__init__()`**, append the enhanced element
instructions when perception mode is active:

```python
# In Agent.__init__(), after SystemPrompt is constructed:
if self.browser_session.browser_profile.perception_mode == PerceptionMode.ENHANCED:
	enhanced_prompt_section = """
## Enhanced Element List

When available, you will see a <unified_elements> section listing all detected elements with:
- `#ID` — element number shown on the screenshot as a colored circle
- `"text"` — the element's text content
- `(type, clickable/non-clickable)` — element type and interactivity
- `center=(x,y)` — viewport coordinates for the element center
- `[cdp:NODE_ID]` — CDP-sourced element (use index-based clicking)
- `[ocr:CONFIDENCE]` — OCR-detected element (use coordinate-based clicking with the center coordinates)

Green circles on the screenshot = clickable elements. Gray = non-clickable. Blue = OCR-detected (not in DOM).
"""
	# Use extend_system_message if already set, otherwise set it
	if extend_system_message:
		extend_system_message += enhanced_prompt_section
	else:
		extend_system_message = enhanced_prompt_section
```

This must be placed BEFORE the `SystemPrompt(... extend_system_message=extend_system_message)` call.

**Why this approach**: `extend_system_message` is an existing `str | None` parameter on both `Agent`
and `SystemPrompt`. It appends to the base prompt. No new plumbing needed, no static files touched,
and the instructions are only present when enhanced mode is active.

---

## File Layout

```
browser_use/dom/
├── ocr_engine.py          # NEW: PaddleOCR wrapper (singleton)
├── som_overlay.py         # NEW: SoM marker renderer (PIL-based)
├── element_merger.py      # NEW: CDP + OCR merge, dedup, format
├── perception.py          # NEW: Pipeline orchestrator
├── service.py             # EXISTING: unchanged
├── views.py               # MODIFY: add UnifiedElement, EnhancedObservation
├── serializer/
│   ├── serializer.py      # EXISTING: unchanged
│   └── clickable_elements.py  # EXISTING: unchanged
└── ...

browser_use/browser/
├── profile.py             # MODIFY: add perception_mode, ocr_confidence_threshold
├── views.py               # MODIFY: add enhanced_observation to BrowserStateSummary
├── python_highlights.py   # EXISTING: unchanged (SoM is separate)
├── watchdogs/
│   └── dom_watchdog.py    # MODIFY: call enhance_observation() post-processing
└── ...

browser_use/agent/
├── service.py             # MODIFY: inject enhanced prompt via extend_system_message
├── prompts.py             # MODIFY: inject element list into state_description
├── message_manager/
│   └── service.py         # MODIFY: replace screenshot with SoM version
└── ...

browser_use/tools/
├── views.py               # MODIFY (Phase 2): add element_id to ClickElementAction
└── service.py             # MODIFY (Phase 2): handle element_id click targeting
```

---

## Dependencies

```toml
# pyproject.toml — optional dependency group
[project.optional-dependencies]
ocr = ["paddleocr>=2.7", "paddlepaddle>=2.6"]
```

OCR is optional. `PerceptionMode.ENHANCED` raises a clear `ImportError` if paddleocr is not installed. Default install is unchanged.

---

## Configuration & Usage

```python
from browser_use import Agent, BrowserSession
from browser_use.browser.profile import BrowserProfile
from browser_use.dom.perception import PerceptionMode

profile = BrowserProfile(
	perception_mode=PerceptionMode.ENHANCED,
	ocr_confidence_threshold=0.5,
)
session = BrowserSession(browser_profile=profile)
agent = Agent(task='...', llm=llm, browser_session=session)
await agent.run()
```

Or via environment variable:
```bash
BROWSER_USE_PERCEPTION=enhanced uv run python my_agent.py
```

The env var support is built into the `perception_mode` field definition via `default_factory`
(see section 6a for the full implementation). No additional code needed — setting the env var
before process start is sufficient.

---

## Error Handling Pattern

Every integration point follows the same pattern — enhanced perception failure is non-fatal:

```python
# In DOMWatchdog (on_BrowserStateRequestEvent, line ~472):
enhanced_observation = None
if self.browser_session.browser_profile.perception_mode == PerceptionMode.ENHANCED:
	from browser_use.dom.perception import enhance_observation
	try:
		enhanced_observation = await enhance_observation(...)
	except ImportError:
		raise  # Missing dependency — must be installed, don't silently ignore
	except Exception as e:
		self.logger.warning(f'Enhanced perception failed, falling back to classic: {e}')
		enhanced_observation = None

# In message_manager/service.py (create_state_messages, line ~468):
# Simple None-check guards the screenshot replacement
if browser_state_summary.enhanced_observation:
	if screenshots:
		screenshots = [browser_state_summary.enhanced_observation.som_screenshot_b64]

# In agent/prompts.py (get_user_message, line ~407):
# Simple None-check guards the element list injection
if self.browser_state.enhanced_observation:
	obs = self.browser_state.enhanced_observation
	state_description += f'<unified_elements>...'
# Otherwise: state_description has no unified_elements block — classic behavior unchanged
```

---

## Backward Compatibility

All changes are additive and opt-in:

- **BrowserProfile** uses `extra='ignore'` — new fields don't break existing instantiations
- **BrowserStateSummary** is a dataclass — new optional field defaults to `None`
- **ClickElementAction** (Phase 2 only) — adding optional `element_id` field is safe (no `extra='forbid'`)
- **AgentOutput** has `extra='forbid'` — NOT modified. Enhanced data flows through BrowserStateSummary
- **Default `perception_mode=CLASSIC`** — zero behavior change unless explicitly opted in
- **Existing highlights** — unchanged, SoM overlay is a separate image pipeline
- **System prompt** — additions are conditional on enhanced mode being active
- **OCR failure** — non-fatal, graceful fallback to classic mode
- **paddleocr** — optional dependency, not required for default install

---

## Rollout Strategy

### Phase 1: OCR Gap-Filling (this design)
- OCR as secondary channel to fill CDP blind spots
- SoM overlay for visual grounding
- Click OCR-only elements via coordinates (existing mechanism)
- CDP remains primary — zero regression risk

### Phase 2: Element ID Targeting
- Add `element_id` to `ClickElementAction`
- Direct SoM ID → click resolution
- Cross-validation logging (CDP text vs OCR text)

### Phase 3: Adaptive Perception
- Auto-detect when OCR is needed (canvas elements, empty DOM regions)
- Skip OCR for simple HTML pages (latency optimization)
- Per-element confidence scoring

---

## Performance Considerations

| Component | Latency (est.) | Notes |
|-----------|---------------|-------|
| PaddleOCR init | ~2s (once) | Singleton, amortized |
| OCR per screenshot | 200-500ms | GPU: ~100ms, CPU: ~500ms |
| Element merge | <1ms | Simple list operations |
| SoM overlay render | 10-50ms | PIL drawing + JPEG compress |
| **Total overhead** | **~200-550ms per step** | vs. ~0ms for classic mode |

Acceptable for browser automation where each step already takes 3-10s (network + LLM inference). OCR adds ~5-15% overhead.

---

## Testing Strategy

Tests go in `tests/ci/test_perception.py`. Per project conventions: no mocks (except LLM), real
objects, pytest-httpserver for test pages, `@pytest.fixture` with no arguments.

### Test 1: Element merger dedup logic

```python
from browser_use.dom.element_merger import merge_elements
from browser_use.dom.views import UnifiedElement


def _make_el(text: str, center: tuple[int, int], source: str = 'cdp', **kwargs) -> UnifiedElement:
	return UnifiedElement(
		id=0, text=text, bounds=(0, 0, 10, 10), center=center,
		is_interactive=source == 'cdp', source=source, confidence=1.0 if source == 'cdp' else 0.9,
		**kwargs,
	)


def test_merge_deduplicates_matching_text_within_proximity():
	cdp = [_make_el('Submit', (300, 200), backend_node_id=100)]
	ocr = [_make_el('Submit', (305, 198), source='ocr')]  # within 20px
	merged = merge_elements(cdp, ocr)
	assert len(merged) == 1
	assert merged[0].source == 'cdp'


def test_merge_keeps_ocr_when_no_cdp_match():
	cdp = [_make_el('Submit', (300, 200), backend_node_id=100)]
	ocr = [_make_el('Chart Title', (500, 100), source='ocr')]
	merged = merge_elements(cdp, ocr)
	assert len(merged) == 2
	assert merged[1].text == 'Chart Title' or merged[0].text == 'Chart Title'


def test_merge_keeps_ocr_when_same_text_but_far():
	cdp = [_make_el('Submit', (300, 200), backend_node_id=100)]
	ocr = [_make_el('Submit', (600, 200), source='ocr')]  # >20px away
	merged = merge_elements(cdp, ocr)
	assert len(merged) == 2


def test_merge_assigns_sequential_ids_in_reading_order():
	cdp = [_make_el('Bottom', (100, 500), backend_node_id=1)]
	ocr = [_make_el('Top', (100, 100), source='ocr')]
	merged = merge_elements(cdp, ocr)
	assert merged[0].text == 'Top' and merged[0].id == 1
	assert merged[1].text == 'Bottom' and merged[1].id == 2
```

### Test 2: SoM overlay renders without error

```python
from PIL import Image

from browser_use.dom.som_overlay import render_som_overlay


def test_som_overlay_renders_png_base64():
	img = Image.new('RGB', (800, 600), color=(255, 255, 255))
	elements = [
		_make_el('Button', (400, 300), backend_node_id=1),
		_make_el('OCR Text', (200, 100), source='ocr'),
	]
	result = render_som_overlay(img, elements, device_pixel_ratio=1.0)
	# Should be valid base64 PNG
	import base64
	decoded = base64.b64decode(result)
	assert decoded[:4] == b'\x89PNG'


def test_som_overlay_scales_by_dpr():
	img = Image.new('RGB', (1600, 1200), color=(255, 255, 255))  # 2x DPR
	elements = [_make_el('Test', (400, 300), backend_node_id=1)]  # CSS coords
	result = render_som_overlay(img, elements, device_pixel_ratio=2.0)
	assert len(result) > 0  # renders without error
```

### Test 3: `_cdp_to_unified_elements` conversion

This test requires building a real `SerializedDOMState` with `EnhancedDOMTreeNode` entries.
Use the real dataclass constructors — no mocks:

```python
from browser_use.dom.perception import _cdp_to_unified_elements
from browser_use.dom.views import (
	DOMRect, EnhancedAXNode, EnhancedDOMTreeNode, EnhancedSnapshotNode,
	NodeType, SerializedDOMState,
)


@pytest.fixture
def sample_dom_state():
	"""Build a minimal SerializedDOMState with one interactive element."""
	snapshot_node = EnhancedSnapshotNode(
		is_clickable=True, cursor_style='pointer',
		bounds=DOMRect(x=100.0, y=200.0, width=80.0, height=30.0),
		clientRects=DOMRect(x=100.0, y=150.0, width=80.0, height=30.0),  # viewport-relative
		scrollRects=None, computed_styles=None,
	)
	ax_node = EnhancedAXNode(
		ax_node_id='ax-1', ignored=False, role='button', name='Submit',
		description=None, properties=None, child_ids=None,
	)
	node = EnhancedDOMTreeNode(
		node_id=1, backend_node_id=42, node_type=NodeType.ELEMENT_NODE,
		node_name='BUTTON', node_value='', attributes={}, is_scrollable=False,
		is_visible=True, shadow_root_type=None, frame_id=None, content_document=None,
		pseudo_type=None, pseudo_identifier=None, parent_node=None, child_nodes=[],
		snapshot_node=snapshot_node, ax_node=ax_node, is_interactive=True,
	)
	selector_map = {42: node}  # key = backend_node_id
	return SerializedDOMState(_root=None, selector_map=selector_map)


def test_cdp_to_unified_uses_client_rects(sample_dom_state):
	elements = _cdp_to_unified_elements(sample_dom_state)
	assert len(elements) == 1
	el = elements[0]
	# Should use clientRects (viewport coords), not bounds (document coords)
	assert el.center == (140, 165)  # (100+180)//2, (150+180)//2
	assert el.source == 'cdp'
	assert el.confidence == 1.0
	assert el.backend_node_id == 42
	assert el.role == 'button'
	assert el.text == 'Submit'
```

**Note**: The `EnhancedDOMTreeNode` constructor may require additional fields not shown here
(check the actual dataclass at `views.py:373`). Adjust the fixture to include all required fields.
Run the test and fix missing fields iteratively — the dataclass will raise `TypeError` for any
missing positional args.

### Test 4: Integration — canvas page with OCR

This test requires `paddleocr` to be installed. Skip if not available:

```python
import pytest

try:
	from paddleocr import PaddleOCR
	HAS_PADDLEOCR = True
except ImportError:
	HAS_PADDLEOCR = False


@pytest.fixture
def canvas_page(httpserver):
	"""Serve a page with a canvas element that draws text via JavaScript."""
	httpserver.expect_request('/canvas').respond_with_data(
		"""<!DOCTYPE html>
		<html><body>
		<canvas id="c" width="400" height="200"></canvas>
		<script>
			const ctx = document.getElementById('c').getContext('2d');
			ctx.font = '24px Arial';
			ctx.fillText('Canvas Text Here', 50, 100);
		</script>
		</body></html>""",
		content_type='text/html',
	)
	return httpserver.url_for('/canvas')


@pytest.mark.skipif(not HAS_PADDLEOCR, reason='paddleocr not installed')
async def test_ocr_detects_canvas_text(canvas_page):
	"""OCR should detect text rendered on a canvas element."""
	from browser_use.browser.profile import BrowserProfile
	from browser_use.browser.session import BrowserSession
	from browser_use.dom.perception import PerceptionMode, enhance_observation

	profile = BrowserProfile(
		headless=True,
		perception_mode=PerceptionMode.ENHANCED,
	)
	session = BrowserSession(browser_profile=profile)
	async with session:
		page = await session.get_current_page()
		await page.goto(canvas_page)
		await page.wait_for_load_state('networkidle')

		# Get browser state to get screenshot and DOM
		state = await session.get_state()

		# The DOM selector_map should have no text for the canvas
		has_canvas_text_in_dom = any(
			'Canvas Text' in (node.ax_node.name or '') if node.ax_node else False
			for node in state.dom_state.selector_map.values()
		)
		assert not has_canvas_text_in_dom, 'Canvas text should NOT be in DOM'

		# Enhanced observation should detect it via OCR
		obs = await enhance_observation(
			screenshot_b64=state.screenshot,
			dom_state=state.dom_state,
			device_pixel_ratio=1.0,
		)
		assert obs is not None
		ocr_texts = [e.text for e in obs.elements if e.source == 'ocr']
		assert any('Canvas' in t for t in ocr_texts), f'OCR should detect canvas text, got: {ocr_texts}'
```

### Test 5: Backward compatibility

```python
async def test_classic_mode_unchanged():
	"""Default perception_mode=classic should produce no enhanced_observation."""
	from browser_use.browser.profile import BrowserProfile
	from browser_use.dom.perception import PerceptionMode

	profile = BrowserProfile()  # default
	assert profile.perception_mode == PerceptionMode.CLASSIC
```

Run the full CI suite with default settings to confirm no regressions:
```bash
uv run pytest -vxs tests/ci
```

### CI considerations

- OCR integration tests (test 4) require `paddleocr` + `paddlepaddle`. Add a CI job variant
  with `uv sync --extra ocr`, or skip these tests in the default CI run via the `skipif` marker.
- Unit tests (tests 1-3, 5) have no extra dependencies and run in the default CI.
