"""
Demo: Agent with OCR + SoM on canvas-only content invisible to DOM.

Runs the full browser-use agent with enhanced perception against pages
where all meaningful content is rendered on <canvas> elements. The DOM
sees only empty canvas tags — only OCR can read the text.

Three scenarios served via a local HTTP server:
  1. Stock chart with price labels, dates, volume
  2. Financial report rendered as canvas (simulated PDF viewer)
  3. Dashboard with KPI cards and pie chart

Prerequisites:
    Install OCR extras:  uv add browser-use[ocr]

Usage:
    uv run python examples/perception_ocr_som_demo.py
"""

import asyncio
import logging
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler

from browser_use import Agent, BrowserSession
from browser_use.browser.profile import BrowserProfile
from browser_use.llm.claude_code import ChatClaudeCode

logging.basicConfig(level=logging.INFO)


# ---------------------------------------------------------------------------
# Canvas-only HTML scenarios
# ---------------------------------------------------------------------------

SCENARIOS: dict[str, str] = {}

SCENARIOS['stock_chart'] = """<!DOCTYPE html>
<html><head><style>
body { font-family: system-ui; background: #1a1a2e; color: #eee; margin: 0; padding: 20px; }
</style></head><body>
<h2>AAPL — Live Chart</h2>
<canvas id="chart" width="800" height="400"></canvas>
<script>
const ctx = document.getElementById('chart').getContext('2d');
ctx.fillStyle = '#16213e'; ctx.fillRect(0, 0, 800, 400);
ctx.strokeStyle = '#1a3a5c'; ctx.lineWidth = 0.5;
for (let y = 50; y < 400; y += 50) { ctx.beginPath(); ctx.moveTo(40, y); ctx.lineTo(780, y); ctx.stroke(); }

const prices = [185, 188, 186, 192, 197, 195, 201, 198, 205, 210];
ctx.strokeStyle = '#00d4aa'; ctx.lineWidth = 2; ctx.beginPath();
prices.forEach((p, i) => {
  const x = 40 + i * 78, y = 380 - (p - 180) * 10;
  i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
});
ctx.stroke();

ctx.fillStyle = '#00d4aa'; ctx.font = 'bold 20px monospace';
ctx.fillText('$210.45', 650, 60);
ctx.font = '14px monospace'; ctx.fillStyle = '#66ddaa';
ctx.fillText('+2.8% (+$5.73)', 650, 85);

ctx.font = '10px monospace'; ctx.fillStyle = '#445566';
ctx.fillText('Vol: 82.4M', 40, 395);
ctx.fillText('Avg Vol: 64.1M', 160, 395);

ctx.fillStyle = '#667788'; ctx.font = '11px monospace';
['Mar 17','Mar 18','Mar 19','Mar 20','Mar 21','Mar 22','Mar 23','Mar 24','Mar 25','Mar 26'].forEach((d, i) => {
  ctx.fillText(d, 25 + i * 78, 380 + 15);
});
</script>
</body></html>"""

SCENARIOS['pdf_report'] = """<!DOCTYPE html>
<html><head><style>
body { font-family: system-ui; background: #525659; margin: 0; padding: 20px; }
</style></head><body>
<canvas id="pdf" width="700" height="500"></canvas>
<script>
const ctx = document.getElementById('pdf').getContext('2d');
ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, 700, 500);
ctx.fillStyle = '#1a1a1a'; ctx.font = 'bold 22px serif';
ctx.fillText('Annual Financial Report - FY2025', 50, 60);
ctx.font = '12px serif'; ctx.fillStyle = '#666';
ctx.fillText('Confidential - For Internal Distribution Only', 50, 85);
ctx.strokeStyle = '#ccc'; ctx.beginPath(); ctx.moveTo(50, 100); ctx.lineTo(650, 100); ctx.stroke();
ctx.fillStyle = '#333'; ctx.font = '14px serif';
const lines = [
  'Total Revenue: $847.3 million (up 23% YoY)',
  'Operating Income: $312.1 million (margin: 36.8%)',
  'Net Income: $248.6 million (EPS: $4.72)',
  'Free Cash Flow: $289.4 million',
  '',
  'Key Highlights:',
  '  Cloud segment grew 41% to $523M, now 62% of total revenue',
  '  Enterprise customer count reached 12,400 (+18% YoY)',
  '  R&D investment increased to $198M (23.4% of revenue)',
  '  Employee headcount: 4,850 (net +620 in FY2025)',
];
lines.forEach((line, i) => { ctx.fillText(line, 50, 130 + i * 22); });
</script>
</body></html>"""

SCENARIOS['dashboard'] = """<!DOCTYPE html>
<html><head><style>
body { font-family: system-ui; background: #fff; margin: 0; padding: 20px; }
</style></head><body>
<canvas id="dash" width="800" height="350"></canvas>
<script>
const ctx = document.getElementById('dash').getContext('2d');
ctx.fillStyle = '#f0f4ff'; ctx.fillRect(10, 10, 240, 150);
ctx.fillStyle = '#1a56db'; ctx.font = 'bold 16px system-ui'; ctx.fillText('Revenue', 30, 45);
ctx.font = 'bold 32px system-ui'; ctx.fillText('$12.4M', 30, 90);
ctx.fillStyle = '#059669'; ctx.font = '14px system-ui'; ctx.fillText('18.3% vs Q3', 30, 120);

ctx.fillStyle = '#f0fdf4'; ctx.fillRect(270, 10, 240, 150);
ctx.fillStyle = '#166534'; ctx.font = 'bold 16px system-ui'; ctx.fillText('Active Users', 290, 45);
ctx.font = 'bold 32px system-ui'; ctx.fillText('847K', 290, 90);
ctx.fillStyle = '#059669'; ctx.font = '14px system-ui'; ctx.fillText('24.1% vs Q3', 290, 120);

ctx.fillStyle = '#fef2f2'; ctx.fillRect(530, 10, 240, 150);
ctx.fillStyle = '#991b1b'; ctx.font = 'bold 16px system-ui'; ctx.fillText('Churn Rate', 550, 45);
ctx.font = 'bold 32px system-ui'; ctx.fillText('3.2%', 550, 90);
ctx.fillStyle = '#dc2626'; ctx.font = '14px system-ui'; ctx.fillText('0.8% vs Q3', 550, 120);

ctx.fillStyle = '#333'; ctx.font = '13px system-ui';
ctx.fillText('Enterprise 60%', 480, 250);
ctx.fillText('SMB 25%', 480, 275);
ctx.fillText('Consumer 15%', 480, 300);
</script>
</body></html>"""


# ---------------------------------------------------------------------------
# Local HTTP server
# ---------------------------------------------------------------------------

class ScenarioHandler(SimpleHTTPRequestHandler):
	scenarios: dict[str, str] = {}

	def do_GET(self):
		key = self.path.lstrip('/')
		if key in self.scenarios:
			content = self.scenarios[key].encode()
			self.send_response(200)
			self.send_header('Content-Type', 'text/html; charset=utf-8')
			self.send_header('Content-Length', str(len(content)))
			self.end_headers()
			self.wfile.write(content)
		else:
			self.send_error(404)

	def log_message(self, format, *args):
		pass


def start_server() -> tuple[HTTPServer, int]:
	ScenarioHandler.scenarios = SCENARIOS
	server = HTTPServer(('127.0.0.1', 0), ScenarioHandler)
	port = server.server_address[1]
	threading.Thread(target=server.serve_forever, daemon=True).start()
	return server, port


# ---------------------------------------------------------------------------
# Run agent on each scenario
# ---------------------------------------------------------------------------

async def run_scenario(session: BrowserSession, name: str, url: str, task: str):
	print(f'\n{"=" * 70}')
	print(f' SCENARIO: {name}')
	print(f'{"=" * 70}')

	agent = Agent(
		task=task,
		llm=ChatClaudeCode(model='sonnet'),
		browser_session=session,
	)
	result = await agent.run()
	print(f'\n  RESULT: {result.final_result()}')


async def main():
	server, port = start_server()
	base = f'http://127.0.0.1:{port}'
	print(f'Local test server on {base}')

	profile = BrowserProfile(
		headless=False,
		keep_alive=True,
		perception_mode='enhanced',
	)
	session = BrowserSession(browser_profile=profile)

	await session.start()
	try:
		await run_scenario(
			session,
			'Stock Chart — Canvas Price Labels',
			f'{base}/stock_chart',
			(
				f'Navigate to {base}/stock_chart\n'
				'This page has a stock chart rendered entirely on a <canvas> element. '
				'The DOM cannot see the text — only OCR can read it.\n'
				'Tell me: the current price, percentage change, volume, '
				'and the date range shown on the X-axis.'
			),
		)

		await run_scenario(
			session,
			'Financial Report — Canvas-Rendered PDF',
			f'{base}/pdf_report',
			(
				f'Navigate to {base}/pdf_report\n'
				'This page simulates a PDF rendered on canvas. '
				'All the financial data is painted on canvas, invisible to DOM.\n'
				'Tell me: the total revenue, operating income, net income, '
				'and list all key highlights mentioned.'
			),
		)

		await run_scenario(
			session,
			'Dashboard — Canvas KPI Cards',
			f'{base}/dashboard',
			(
				f'Navigate to {base}/dashboard\n'
				'This page has a dashboard with KPI cards and a pie chart, '
				'all rendered on canvas. DOM sees nothing.\n'
				'Tell me: the revenue figure, active users count, churn rate, '
				'and the market segment breakdown from the pie chart.'
			),
		)

		print(f'\n{"=" * 70}')
		print(' All scenarios complete.')
		print(f'{"=" * 70}')

	finally:
		await session.stop()
		server.shutdown()


if __name__ == '__main__':
	asyncio.run(main())
