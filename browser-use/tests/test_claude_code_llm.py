"""Quick smoke test for ChatClaudeCode LLM provider."""

import asyncio
import base64

from pydantic import BaseModel

from browser_use.llm.claude_code import ChatClaudeCode
from browser_use.llm.messages import (
	ContentPartImageParam,
	ContentPartTextParam,
	ImageURL,
	SystemMessage,
	UserMessage,
)


class MathAnswer(BaseModel):
	answer: int
	explanation: str


async def test_text_output():
	llm = ChatClaudeCode(model='sonnet')
	result = await llm.ainvoke([
		SystemMessage(content='You are a helpful assistant. Be very brief.'),
		UserMessage(content='What is the capital of France? Reply in one word.'),
	])
	print(f'Text result: {result.completion!r}')
	print(f'Usage: {result.usage}')
	assert 'Paris' in result.completion


async def test_structured_output():
	llm = ChatClaudeCode(model='sonnet')
	result = await llm.ainvoke(
		[
			SystemMessage(content='You are a calculator.'),
			UserMessage(content='What is 17 * 3?'),
		],
		output_format=MathAnswer,
	)
	print(f'Structured result: {result.completion}')
	print(f'Usage: {result.usage}')
	assert isinstance(result.completion, MathAnswer)
	assert result.completion.answer == 51


async def test_image_input():
	"""Test that base64 images are saved to temp files and read by CLI."""
	# Create a simple test image (red square with white text)
	from PIL import Image, ImageDraw
	import io

	img = Image.new('RGB', (200, 100), color='blue')
	d = ImageDraw.Draw(img)
	d.text((50, 40), 'BROWSER-USE', fill='white')
	buf = io.BytesIO()
	img.save(buf, format='PNG')
	b64_data = base64.b64encode(buf.getvalue()).decode('utf-8')

	llm = ChatClaudeCode(model='sonnet')
	result = await llm.ainvoke([
		SystemMessage(content='Describe what you see in the image. Be very brief.'),
		UserMessage(content=[
			ContentPartTextParam(text='What does this image show?'),
			ContentPartImageParam(
				image_url=ImageURL(
					url=f'data:image/png;base64,{b64_data}',
					media_type='image/png',
				)
			),
		]),
	])
	print(f'Image result: {result.completion!r}')
	assert 'BROWSER' in result.completion.upper() or 'blue' in result.completion.lower() or 'text' in result.completion.lower()


async def main():
	print('=== Test 1: Text output ===')
	await test_text_output()
	print('\n=== Test 2: Structured output ===')
	await test_structured_output()
	print('\n=== Test 3: Image input ===')
	await test_image_input()
	print('\nAll tests passed!')


if __name__ == '__main__':
	asyncio.run(main())
