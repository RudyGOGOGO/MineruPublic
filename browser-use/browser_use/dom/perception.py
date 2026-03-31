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

		elements.append(
			UnifiedElement(
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
			)
		)

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
		if not screenshot_b64:
			logger.debug('No screenshot available for enhanced perception')
			return None
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
			screenshot,
			merged,
			device_pixel_ratio=device_pixel_ratio,
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
