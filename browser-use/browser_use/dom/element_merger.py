from browser_use.dom.views import UnifiedElement

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
