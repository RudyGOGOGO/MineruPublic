"""
Compare DOM-only vs Enhanced (OCR + SoM) perception modes.

Demonstrates that OCR detects text rendered on <canvas> elements
that are invisible to the standard CDP DOM pipeline.

Setup:
    uv add browser-use[ocr]

Usage:
    uv run python examples/perception_comparison.py
"""

import asyncio

from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession
from browser_use.dom.perception import enhance_observation

CANVAS_HTML = """<!DOCTYPE html>
<html><body style="font-family: Arial, sans-serif; padding: 20px;">
<h1>Perception Comparison Test</h1>
<button id="action-btn">Click Me</button>
<a href="/next">Next Page</a>
<p>This paragraph is in the DOM.</p>

<canvas id="chart" width="600" height="250" style="border: 1px solid #ccc; margin-top: 20px;"></canvas>
<script>
const ctx = document.getElementById('chart').getContext('2d');
ctx.fillStyle = '#f0f0f0';
ctx.fillRect(0, 0, 600, 250);
ctx.font = 'bold 24px Arial';
ctx.fillStyle = '#333';
ctx.fillText('Q3 Revenue: $4.2M', 30, 60);
ctx.fillText('Growth Rate: +18%', 30, 110);
ctx.font = '16px Arial';
ctx.fillStyle = '#666';
ctx.fillText('This text exists ONLY on the canvas', 30, 170);
ctx.fillText('DOM cannot see any of this content', 30, 200);
</script>
</body></html>"""


async def main():
	profile = BrowserProfile(headless=True)
	session = BrowserSession(browser_profile=profile)

	async with session:
		page = await session.get_current_page()
		await page.set_content(CANVAS_HTML)
		await page.wait_for_load_state('networkidle')
		state = await session.get_state()

		# --- DOM only ---
		print('=' * 70)
		print(' DOM-ONLY ELEMENTS (standard CDP pipeline)')
		print('=' * 70)
		for nid, node in state.dom_state.selector_map.items():
			text = ''
			if node.ax_node and node.ax_node.name:
				text = node.ax_node.name
			print(f'  [{nid}] <{node.tag_name}> "{text}"')
		print(f'\n  Total: {len(state.dom_state.selector_map)} elements')

		# --- Enhanced (OCR + SoM) ---
		obs = await enhance_observation(
			screenshot_b64=state.screenshot,
			dom_state=state.dom_state,
			device_pixel_ratio=1.0,
		)
		assert obs is not None, 'Enhanced observation failed — is paddleocr installed? (uv add browser-use[ocr])'

		print('\n' + '=' * 70)
		print(' UNIFIED ELEMENTS (DOM + OCR merged)')
		print('=' * 70)
		print(obs.element_list_text)
		print(f'\n  Total: {obs.element_count} elements')

		# --- Delta ---
		ocr_only = [e for e in obs.elements if e.source == 'ocr']
		cdp_only = [e for e in obs.elements if e.source == 'cdp']
		print('\n' + '=' * 70)
		print(f' DELTA: OCR found {len(ocr_only)} elements invisible to DOM')
		print('=' * 70)
		for e in ocr_only:
			print(f'  "{e.text}" center={e.center} confidence={e.confidence:.2f}')

		print(f'\n  CDP elements: {len(cdp_only)}')
		print(f'  OCR-only elements: {len(ocr_only)}')
		print(f'  Combined: {obs.element_count}')


if __name__ == '__main__':
	asyncio.run(main())
