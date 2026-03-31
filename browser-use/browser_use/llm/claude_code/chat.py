"""
ChatClaudeCode: LLM provider that uses the Claude Code CLI subprocess as a backend.

This allows Claude Max subscribers to use browser-use without a separate API key,
by routing LLM calls through `claude --print`.

Images are supported by saving base64 screenshots to temp files and having the CLI's
Read tool process them as images.

Limitations:
- Each call spawns a subprocess (~3-7s overhead per call)
- CLAUDE.md instructions may leak into responses (stripped automatically)
"""

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Any, TypeVar, overload

from pydantic import BaseModel

from browser_use.llm.base import BaseChatModel
from browser_use.llm.exceptions import ModelProviderError
from browser_use.llm.messages import BaseMessage, SystemMessage, UserMessage
from browser_use.llm.views import ChatInvokeCompletion, ChatInvokeUsage

logger = logging.getLogger(__name__)

T = TypeVar('T', bound=BaseModel)

# Pattern to strip token usage tracking injected by CLAUDE.md
_TOKEN_USAGE_PATTERN = re.compile(r'\n*---\n\[Token Usage\].*$', re.DOTALL)


def _extract_text(content: Any) -> str:
	"""Extract text from message content (str or list of content parts)."""
	if content is None:
		return ''
	if isinstance(content, str):
		return content
	parts: list[str] = []
	for part in content:
		if hasattr(part, 'type') and part.type == 'text':
			parts.append(part.text)
		elif hasattr(part, 'type') and part.type == 'refusal':
			parts.append(f'[Refusal] {part.refusal}')
	return '\n'.join(parts)


def _extract_and_save_images(content: Any, temp_dir: str) -> list[str]:
	"""
	Extract base64 images from message content, save to temp files.
	Returns list of file paths for saved images.
	"""
	if content is None or isinstance(content, str):
		return []

	image_paths: list[str] = []
	for part in content:
		if hasattr(part, 'type') and part.type == 'image_url':
			url = part.image_url.url
			if url.startswith('data:'):
				# Extract media type and base64 data
				header, data = url.split(',', 1)
				media_type = part.image_url.media_type or 'image/png'
				ext = media_type.split('/')[-1]
				if ext == 'jpeg':
					ext = 'jpg'

				# Save to temp file
				fd, path = tempfile.mkstemp(suffix=f'.{ext}', dir=temp_dir, prefix='screenshot_')
				os.write(fd, base64.b64decode(data))
				os.close(fd)
				image_paths.append(path)
			elif not url.startswith('http'):
				# Local file path
				image_paths.append(url)
	return image_paths


def _serialize_messages(messages: list[BaseMessage], temp_dir: str | None = None) -> tuple[str | None, str, list[str]]:
	"""
	Serialize messages into (system_prompt, user_prompt, image_paths) for the CLI.

	Extracts system messages into a separate system prompt, flattens the conversation
	into a single text prompt, and saves any base64 images to temp files.
	"""
	system_parts: list[str] = []
	conversation_parts: list[str] = []
	all_image_paths: list[str] = []

	for msg in messages:
		if isinstance(msg, SystemMessage):
			system_parts.append(_extract_text(msg.content))
		else:
			role = msg.role.upper()
			text = _extract_text(msg.content)

			# Extract images from user messages
			if isinstance(msg, UserMessage) and temp_dir:
				image_paths = _extract_and_save_images(msg.content, temp_dir)
				if image_paths:
					all_image_paths.extend(image_paths)
					# Add image file references into the prompt text
					for path in image_paths:
						text += f'\n[See screenshot at: {path}]'

			if text:
				conversation_parts.append(f'[{role}]\n{text}')

	system_prompt = '\n\n'.join(system_parts) if system_parts else None
	user_prompt = '\n\n'.join(conversation_parts)

	return system_prompt, user_prompt, all_image_paths


def _extract_json_from_text(text: str) -> str | None:
	"""
	Extract JSON object from text that may contain conversational wrapping.
	Tries multiple strategies:
	1. Direct parse (text is pure JSON)
	2. Extract from ```json ... ``` markdown blocks
	3. Find the outermost { ... } in the text
	"""
	text = text.strip()

	# Strategy 1: direct parse
	if text.startswith('{'):
		try:
			json.loads(text)
			return text
		except json.JSONDecodeError:
			pass

	# Strategy 2: markdown code block
	json_block_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
	if json_block_match:
		candidate = json_block_match.group(1).strip()
		try:
			json.loads(candidate)
			return candidate
		except json.JSONDecodeError:
			pass

	# Strategy 3: find outermost braces
	start = text.find('{')
	if start != -1:
		depth = 0
		in_string = False
		escape = False
		for i in range(start, len(text)):
			c = text[i]
			if escape:
				escape = False
				continue
			if c == '\\' and in_string:
				escape = True
				continue
			if c == '"' and not escape:
				in_string = not in_string
				continue
			if in_string:
				continue
			if c == '{':
				depth += 1
			elif c == '}':
				depth -= 1
				if depth == 0:
					candidate = text[start:i + 1]
					try:
						json.loads(candidate)
						return candidate
					except json.JSONDecodeError:
						break

	return None


def _wrap_prose_as_done_action(text: str, output_format: type) -> str | None:
	"""Wrap free-text prose as a done action JSON if the output schema supports it.

	When --json-schema enforcement fails and the model returns a prose answer,
	this wraps it as {"action": [{"done": {"text": "...", "success": true}}]}
	so the agent can proceed instead of retrying.

	Only activates when:
	1. The text is clearly prose (>50 chars, no JSON found)
	2. The output schema has an 'action' field (i.e., it's an AgentOutput-like schema)
	3. The schema's action items support a 'done' action
	"""
	if not text or len(text.strip()) < 50:
		return None

	# Check if output_format has an 'action' field in its schema
	try:
		schema = output_format.model_json_schema()
	except Exception:
		return None

	if 'action' not in schema.get('properties', {}):
		return None

	# Build a minimal valid AgentOutput with the prose wrapped in a done action
	done_output = {
		'evaluation_previous_goal': 'Success. Extracted content and prepared response.',
		'memory': 'Processed available content and produced final answer.',
		'next_goal': 'Deliver final answer to user.',
		'action': [{'done': {'text': text.strip(), 'success': True}}],
	}
	return json.dumps(done_output)


@dataclass
class ChatClaudeCode:
	"""
	LLM provider using the Claude Code CLI (`claude --print`) as backend.

	Requires Claude Code CLI installed and authenticated (e.g., via Claude Max plan).

	Usage:
		from browser_use.llm.claude_code import ChatClaudeCode

		llm = ChatClaudeCode(model='sonnet')
		agent = Agent(task='...', llm=llm)
		await agent.run()
	"""

	model: str = 'sonnet'
	"""Model alias or full name (e.g., 'sonnet', 'opus', 'claude-sonnet-4-6')."""

	claude_cli_path: str | None = None
	"""Path to claude CLI binary. Auto-detected if None."""

	timeout: float = 120.0
	"""Timeout in seconds for each CLI call."""

	extra_args: list[str] = field(default_factory=list)
	"""Additional CLI arguments to pass to claude."""

	_cli_path_resolved: str | None = field(default=None, init=False, repr=False)

	@property
	def provider(self) -> str:
		return 'claude-code'

	@property
	def name(self) -> str:
		return f'claude-code/{self.model}'

	@property
	def model_name(self) -> str:
		return self.model

	def _get_cli_path(self) -> str:
		if self._cli_path_resolved:
			return self._cli_path_resolved

		path = self.claude_cli_path or shutil.which('claude')
		if not path:
			raise ModelProviderError(
				message='Claude Code CLI not found. Install it or set claude_cli_path.',
				model=self.name,
			)
		self._cli_path_resolved = path
		return path

	def _build_command(
		self,
		system_prompt: str | None,
		json_schema: dict[str, Any] | None,
		has_images: bool = False,
	) -> list[str]:
		cmd = [
			self._get_cli_path(),
			'--print',
			'--output-format', 'json',
			'--model', self.model,
			'--no-session-persistence',
			# NOTE: --bare would skip CLAUDE.md but also breaks OAuth/keychain auth
			# for Claude Max users. Instead, we run the subprocess from a temp cwd
			# to avoid project CLAUDE.md auto-discovery.
		]

		# Enable Read tool when images need to be processed, otherwise disable all tools
		if has_images:
			cmd.extend(['--tools', 'Read'])
			cmd.extend(['--allowedTools', 'Read'])
			cmd.extend(['--permission-mode', 'bypassPermissions'])
		else:
			cmd.extend(['--tools', ''])

		# Always pass --system-prompt to replace the CLI's built-in ~11k system prompt.
		# Even an empty string is better than loading the default Claude Code system prompt.
		cmd.extend(['--system-prompt', system_prompt or ''])

		# Only use --json-schema in single-turn mode (no tools).
		# When tools are enabled, structured output is enforced via system prompt instead.
		if json_schema and not has_images:
			cmd.extend(['--json-schema', json.dumps(json_schema)])

		cmd.extend(self.extra_args)

		return cmd

	def _parse_usage(self, raw_usage: dict[str, Any] | None) -> ChatInvokeUsage | None:
		if not raw_usage:
			return None
		input_tokens = raw_usage.get('input_tokens', 0)
		cached = raw_usage.get('cache_read_input_tokens', 0)
		cache_creation = raw_usage.get('cache_creation_input_tokens', 0)
		output_tokens = raw_usage.get('output_tokens', 0)
		return ChatInvokeUsage(
			prompt_tokens=input_tokens + cached + cache_creation,
			prompt_cached_tokens=cached or None,
			prompt_cache_creation_tokens=cache_creation or None,
			prompt_image_tokens=None,
			completion_tokens=output_tokens,
			total_tokens=input_tokens + cached + cache_creation + output_tokens,
		)

	def _clean_result_text(self, text: str) -> str:
		"""Strip CLAUDE.md token tracking suffix and other noise from result text."""
		return _TOKEN_USAGE_PATTERN.sub('', text).strip()

	async def _run_cli(
		self,
		prompt: str,
		system_prompt: str | None = None,
		json_schema: dict[str, Any] | None = None,
		has_images: bool = False,
	) -> dict[str, Any]:
		cmd = self._build_command(system_prompt, json_schema, has_images=has_images)

		try:
			# Run from temp dir to avoid loading project CLAUDE.md (~3k wasted tokens).
			# Global ~/.claude/CLAUDE.md (~300 tokens) still loads — acceptable tradeoff
			# vs breaking OAuth/keychain auth with --bare.
			process = await asyncio.create_subprocess_exec(
				*cmd,
				stdin=asyncio.subprocess.PIPE,
				stdout=asyncio.subprocess.PIPE,
				stderr=asyncio.subprocess.PIPE,
				cwd=tempfile.gettempdir(),
			)

			stdout, stderr = await asyncio.wait_for(
				process.communicate(input=prompt.encode('utf-8')),
				timeout=self.timeout,
			)
		except asyncio.TimeoutError:
			process.kill()
			raise ModelProviderError(
				message=f'Claude CLI timed out after {self.timeout}s',
				model=self.name,
			)
		except Exception as e:
			raise ModelProviderError(
				message=f'Failed to run Claude CLI: {e}',
				model=self.name,
			) from e

		stdout_text = stdout.decode('utf-8').strip()

		if not stdout_text:
			stderr_text = stderr.decode('utf-8').strip()
			raise ModelProviderError(
				message=f'Claude CLI returned empty output. stderr: {stderr_text}',
				model=self.name,
			)

		try:
			return json.loads(stdout_text)
		except json.JSONDecodeError as e:
			raise ModelProviderError(
				message=f'Failed to parse Claude CLI JSON output: {e}\nRaw output: {stdout_text[:500]}',
				model=self.name,
			) from e

	def _parse_structured_output(self, response: dict[str, Any], output_format: type[T]) -> T:
		"""Parse structured output from CLI response, handling both single-turn and multi-turn modes."""
		# Try structured_output field first (populated in single-turn --json-schema mode)
		structured = response.get('structured_output')
		if structured is not None:
			return output_format.model_validate(structured)

		# Multi-turn mode: extract JSON from result text
		result_text = self._clean_result_text(response.get('result', ''))

		# Try to extract JSON from potentially wrapped text
		json_str = _extract_json_from_text(result_text)
		if json_str is not None:
			try:
				return output_format.model_validate_json(json_str)
			except Exception:
				pass

		# Last resort: try direct parse of full text
		try:
			return output_format.model_validate_json(result_text)
		except Exception:
			pass

		# Fallback: if the model returned prose instead of JSON, try to wrap it as a done action.
		# This happens when --json-schema enforcement fails (e.g., model ignores schema with
		# large context like PDF content) and the model produces a valid answer in free text.
		done_json = _wrap_prose_as_done_action(result_text, output_format)
		if done_json is not None:
			try:
				return output_format.model_validate_json(done_json)
			except Exception:
				pass

		raise ModelProviderError(
			message=f'Failed to parse structured output: {result_text[:500]}',
			model=self.name,
		)

	@overload
	async def ainvoke(
		self, messages: list[BaseMessage], output_format: None = None, **kwargs: Any
	) -> ChatInvokeCompletion[str]: ...

	@overload
	async def ainvoke(
		self, messages: list[BaseMessage], output_format: type[T], **kwargs: Any
	) -> ChatInvokeCompletion[T]: ...

	async def ainvoke(
		self, messages: list[BaseMessage], output_format: type[T] | None = None, **kwargs: Any
	) -> ChatInvokeCompletion[T] | ChatInvokeCompletion[str]:
		# Create temp dir for any base64 images
		temp_dir = tempfile.mkdtemp(prefix='browser_use_claude_')
		image_paths: list[str] = []
		try:
			system_prompt, user_prompt, image_paths = _serialize_messages(messages, temp_dir=temp_dir)
			has_images = len(image_paths) > 0

			if has_images:
				# Prepend instruction to read screenshot files
				read_instructions = 'IMPORTANT: Read the screenshot file(s) referenced below using the Read tool to view them before responding.\n\n'
				user_prompt = read_instructions + user_prompt

			json_schema = None
			if output_format is not None:
				json_schema = output_format.model_json_schema()

				# Reinforce structured output in system prompt. --json-schema is the primary
				# enforcement but can fail when the model is distracted by large content
				# (e.g., PDF text). System prompt reinforcement gives a persistent signal.
				schema_reinforcement = (
					'\n\nCRITICAL OUTPUT REQUIREMENT: You MUST respond with ONLY a valid JSON object. '
					'Do NOT include any text, explanation, or markdown before or after the JSON. '
					'Your entire response must be parseable as JSON.'
				)
				if system_prompt:
					system_prompt += schema_reinforcement
				else:
					system_prompt = schema_reinforcement.strip()

				# When tools are enabled (images), --json-schema won't work.
				# Embed full schema in the prompt as well.
				if has_images:
					schema_instruction = (
						'\n\nIMPORTANT: You MUST respond with ONLY a valid JSON object (no other text before or after) '
						f'conforming to this JSON schema:\n{json.dumps(json_schema)}'
					)
					user_prompt += schema_instruction

			response = await self._run_cli(
				prompt=user_prompt,
				system_prompt=system_prompt,
				json_schema=json_schema,
				has_images=has_images,
			)
		finally:
			# Clean up temp image files
			for path in image_paths:
				try:
					os.unlink(path)
				except OSError:
					pass
			try:
				os.rmdir(temp_dir)
			except OSError:
				pass

		if response.get('is_error'):
			raise ModelProviderError(
				message=f'Claude CLI error: {response.get("result", "unknown error")}',
				model=self.name,
			)

		usage = self._parse_usage(response.get('usage'))
		stop_reason = response.get('stop_reason')

		if output_format is not None:
			completion = self._parse_structured_output(response, output_format)
			return ChatInvokeCompletion(completion=completion, usage=usage, stop_reason=stop_reason)
		else:
			result_text = self._clean_result_text(response.get('result', ''))
			return ChatInvokeCompletion(completion=result_text, usage=usage, stop_reason=stop_reason)
