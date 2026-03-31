"""Tests for the OCR + SoM enhanced perception pipeline."""

import base64
import importlib.util

import pytest
from PIL import Image

from browser_use.dom.element_merger import format_element_list, merge_elements
from browser_use.dom.perception import _cdp_to_unified_elements
from browser_use.dom.som_overlay import render_som_overlay
from browser_use.dom.views import (
	DOMRect,
	EnhancedAXNode,
	EnhancedDOMTreeNode,
	EnhancedSnapshotNode,
	NodeType,
	SerializedDOMState,
	UnifiedElement,
)

# ---- Helpers ----


def _make_el(text: str, center: tuple[int, int], source: str = 'cdp', **kwargs) -> UnifiedElement:
	return UnifiedElement(
		id=0,
		text=text,
		bounds=(0, 0, 10, 10),
		center=center,
		is_interactive=source == 'cdp',
		source=source,
		confidence=1.0 if source == 'cdp' else 0.9,
		**kwargs,
	)


# ---- Test 1: Element merger dedup logic ----


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
	texts = {el.text for el in merged}
	assert 'Chart Title' in texts
	assert 'Submit' in texts


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


def test_merge_case_insensitive_dedup():
	cdp = [_make_el('SUBMIT', (300, 200), backend_node_id=100)]
	ocr = [_make_el('submit', (305, 198), source='ocr')]
	merged = merge_elements(cdp, ocr)
	assert len(merged) == 1
	assert merged[0].source == 'cdp'


def test_merge_empty_lists():
	merged = merge_elements([], [])
	assert merged == []


# ---- Test 2: Element list formatting ----


def test_format_element_list_cdp():
	el = _make_el('Submit', (450, 320), backend_node_id=523, tag_name='button', role='button')
	el.id = 1
	result = format_element_list([el])
	assert '#1 "Submit" (button, clickable) center=(450, 320) [cdp:523]' in result


def test_format_element_list_ocr():
	el = _make_el('Chart Title', (300, 150), source='ocr')
	el.id = 4
	result = format_element_list([el])
	assert '#4 "Chart Title" (non-clickable) center=(300, 150) [ocr:0.90]' in result


def test_format_element_list_truncates_long_text():
	long_text = 'A' * 100
	el = _make_el(long_text, (100, 100), source='ocr')
	el.id = 1
	result = format_element_list([el], max_text_len=80)
	assert '...' in result
	# Should contain the first 80 chars
	assert 'A' * 80 in result


# ---- Test 3: SoM overlay rendering ----


def test_som_overlay_renders_png_base64():
	img = Image.new('RGB', (800, 600), color=(255, 255, 255))
	elements = [
		_make_el('Button', (400, 300), backend_node_id=1),
		_make_el('OCR Text', (200, 100), source='ocr'),
	]
	# Assign IDs like the merger would
	elements[0].id = 1
	elements[1].id = 2
	result = render_som_overlay(img, elements, device_pixel_ratio=1.0)
	# Should be valid base64 PNG
	decoded = base64.b64decode(result)
	assert decoded[:4] == b'\x89PNG'


def test_som_overlay_scales_by_dpr():
	img = Image.new('RGB', (1600, 1200), color=(255, 255, 255))  # 2x DPR
	elements = [_make_el('Test', (400, 300), backend_node_id=1)]  # CSS coords
	elements[0].id = 1
	result = render_som_overlay(img, elements, device_pixel_ratio=2.0)
	assert len(result) > 0  # renders without error
	decoded = base64.b64decode(result)
	assert decoded[:4] == b'\x89PNG'


def test_som_overlay_resizes_large_images():
	img = Image.new('RGB', (2000, 1500), color=(255, 255, 255))
	elements = [_make_el('Test', (100, 100), backend_node_id=1)]
	elements[0].id = 1
	result = render_som_overlay(img, elements, device_pixel_ratio=1.0, max_width=1024)
	decoded = base64.b64decode(result)
	result_img = Image.open(__import__('io').BytesIO(decoded))
	assert result_img.width <= 1024


def test_som_overlay_empty_elements():
	img = Image.new('RGB', (800, 600), color=(255, 255, 255))
	result = render_som_overlay(img, [], device_pixel_ratio=1.0)
	decoded = base64.b64decode(result)
	assert decoded[:4] == b'\x89PNG'


# ---- Test 4: CDP to unified elements conversion ----


@pytest.fixture
def sample_dom_state():
	"""Build a minimal SerializedDOMState with one interactive element."""
	snapshot_node = EnhancedSnapshotNode(
		is_clickable=True,
		cursor_style='pointer',
		bounds=DOMRect(x=100.0, y=200.0, width=80.0, height=30.0),
		clientRects=DOMRect(x=100.0, y=150.0, width=80.0, height=30.0),  # viewport-relative
		scrollRects=None,
		computed_styles=None,
		paint_order=None,
		stacking_contexts=None,
	)
	ax_node = EnhancedAXNode(
		ax_node_id='ax-1',
		ignored=False,
		role='button',
		name='Submit',
		description=None,
		properties=None,
		child_ids=None,
	)
	node = EnhancedDOMTreeNode(
		node_id=1,
		backend_node_id=42,
		node_type=NodeType.ELEMENT_NODE,
		node_name='BUTTON',
		node_value='',
		attributes={},
		is_scrollable=False,
		is_visible=True,
		absolute_position=None,
		target_id='target-1',
		frame_id=None,
		session_id=None,
		content_document=None,
		shadow_root_type=None,
		shadow_roots=None,
		parent_node=None,
		children_nodes=[],
		ax_node=ax_node,
		snapshot_node=snapshot_node,
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


def test_cdp_to_unified_skips_nodes_without_client_rects():
	"""Nodes without clientRects should be skipped."""
	snapshot_node = EnhancedSnapshotNode(
		is_clickable=True,
		cursor_style='pointer',
		bounds=DOMRect(x=100.0, y=200.0, width=80.0, height=30.0),
		clientRects=None,
		scrollRects=None,
		computed_styles=None,
		paint_order=None,
		stacking_contexts=None,
	)
	node = EnhancedDOMTreeNode(
		node_id=2,
		backend_node_id=99,
		node_type=NodeType.ELEMENT_NODE,
		node_name='DIV',
		node_value='',
		attributes={},
		is_scrollable=False,
		is_visible=True,
		absolute_position=None,
		target_id='target-1',
		frame_id=None,
		session_id=None,
		content_document=None,
		shadow_root_type=None,
		shadow_roots=None,
		parent_node=None,
		children_nodes=[],
		ax_node=None,
		snapshot_node=snapshot_node,
	)
	dom_state = SerializedDOMState(_root=None, selector_map={99: node})
	elements = _cdp_to_unified_elements(dom_state)
	assert len(elements) == 0


def test_cdp_to_unified_falls_back_to_attributes():
	"""When ax_node has no name, should fall back to attributes."""
	snapshot_node = EnhancedSnapshotNode(
		is_clickable=True,
		cursor_style='pointer',
		bounds=None,
		clientRects=DOMRect(x=50.0, y=50.0, width=100.0, height=40.0),
		scrollRects=None,
		computed_styles=None,
		paint_order=None,
		stacking_contexts=None,
	)
	ax_node = EnhancedAXNode(
		ax_node_id='ax-2',
		ignored=False,
		role='textbox',
		name='',
		description=None,
		properties=None,
		child_ids=None,
	)
	node = EnhancedDOMTreeNode(
		node_id=3,
		backend_node_id=55,
		node_type=NodeType.ELEMENT_NODE,
		node_name='INPUT',
		node_value='',
		attributes={'placeholder': 'Enter email'},
		is_scrollable=False,
		is_visible=True,
		absolute_position=None,
		target_id='target-1',
		frame_id=None,
		session_id=None,
		content_document=None,
		shadow_root_type=None,
		shadow_roots=None,
		parent_node=None,
		children_nodes=[],
		ax_node=ax_node,
		snapshot_node=snapshot_node,
	)
	dom_state = SerializedDOMState(_root=None, selector_map={55: node})
	elements = _cdp_to_unified_elements(dom_state)
	assert len(elements) == 1
	assert elements[0].text == 'Enter email'


# ---- Test 5: Backward compatibility ----


def test_classic_mode_unchanged():
	"""Default perception_mode=classic should produce no enhanced_observation."""
	from browser_use.browser.profile import BrowserProfile
	from browser_use.dom.perception import PerceptionMode

	profile = BrowserProfile()  # default
	assert profile.perception_mode == PerceptionMode.CLASSIC


def test_enhanced_mode_can_be_set():
	"""perception_mode can be set to enhanced."""
	from browser_use.browser.profile import BrowserProfile
	from browser_use.dom.perception import PerceptionMode

	profile = BrowserProfile(perception_mode='enhanced')
	assert profile.perception_mode == PerceptionMode.ENHANCED


def test_browser_state_summary_defaults_no_enhanced_observation():
	"""BrowserStateSummary should default enhanced_observation to None."""
	from browser_use.browser.views import BrowserStateSummary, TabInfo

	state = BrowserStateSummary(
		dom_state=SerializedDOMState(_root=None, selector_map={}),
		url='https://example.com',
		title='Test',
		tabs=[TabInfo(url='https://example.com', title='Test', target_id='t-1')],
	)
	assert state.enhanced_observation is None


# ---- Test 6: UnifiedElement and EnhancedObservation models ----


def test_unified_element_forbids_extra_fields():
	with pytest.raises(Exception):
		UnifiedElement(
			id=1,
			text='test',
			bounds=(0, 0, 10, 10),
			center=(5, 5),
			is_interactive=True,
			source='cdp',
			confidence=1.0,
			extra_field='not_allowed',
		)


def test_enhanced_observation_model():
	from browser_use.dom.views import EnhancedObservation

	obs = EnhancedObservation(
		som_screenshot_b64='abc123',
		elements=[],
		element_list_text='',
		element_count=0,
	)
	assert obs.element_count == 0
	assert obs.elements == []


# ---- Test 7: Integration — canvas page with OCR ----

HAS_PADDLEOCR = importlib.util.find_spec('paddleocr') is not None


@pytest.fixture
def canvas_page(httpserver):
	"""Serve a page with a canvas element that draws text via JavaScript."""
	httpserver.expect_request('/canvas').respond_with_data(
		"""<!DOCTYPE html>
		<html><body>
		<canvas id="c" width="800" height="300"></canvas>
		<script>
			const ctx = document.getElementById('c').getContext('2d');
			ctx.font = 'bold 48px Arial';
			ctx.fillStyle = '#000';
			ctx.fillText('CANVAS TEXT VISIBLE', 50, 120);
			ctx.fillText('ONLY TO OCR ENGINE', 50, 200);
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
	await session.start()
	try:
		await session.navigate_to(canvas_page)

		# Get browser state to get screenshot and DOM
		state = await session.get_browser_state_summary()

		# The DOM selector_map should have no text for the canvas
		has_canvas_text_in_dom = any(
			'CANVAS' in (node.ax_node.name or '').upper() if node.ax_node else False
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
		ocr_texts = [e.text.upper() for e in obs.elements if e.source == 'ocr']
		assert any('CANVAS' in t or 'OCR' in t for t in ocr_texts), f'OCR should detect canvas text, got: {ocr_texts}'
	finally:
		await session.stop()
