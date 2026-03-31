"""LangChain BaseChatModel wrapper around Claude Code CLI (`claude -p`).

Uses the user's Claude Max subscription via the local `claude` CLI binary,
avoiding the need for a separate Anthropic API key.
"""

import asyncio
import json
import re
import subprocess
import uuid
from copy import deepcopy
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import BaseTool
from pydantic import BaseModel


def _format_messages(messages: list[BaseMessage]) -> str:
    """Convert LangChain messages to a single prompt string for claude -p."""
    parts = []
    for m in messages:
        if isinstance(m, SystemMessage):
            parts.append(f"[System]\n{m.content}")
        elif isinstance(m, HumanMessage):
            parts.append(f"[User]\n{m.content}")
        elif isinstance(m, AIMessage):
            if m.tool_calls:
                calls = json.dumps(m.tool_calls, indent=2)
                parts.append(f"[Assistant Tool Calls]\n{calls}")
            elif m.content:
                parts.append(f"[Assistant]\n{m.content}")
        elif isinstance(m, ToolMessage):
            parts.append(f"[Tool Result for {m.tool_call_id}]\n{m.content}")
        else:
            parts.append(str(m.content))
    return "\n\n".join(parts)


def _extract_json(text: str) -> str:
    """Extract JSON from text that may contain markdown code fences or extra text."""
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        return match.group(1).strip()

    for pattern in [r"\{.*\}", r"\[.*\]"]:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            return match.group(0)

    return text.strip()


def _call_claude_cli(
    prompt: str,
    timeout_seconds: int = 300,
    model: str | None = None,
    system_prompt: str | None = None,
    disable_tools: bool = False,
) -> str:
    """Call claude CLI and return the response text.

    Args:
        prompt: The prompt text to send.
        timeout_seconds: Timeout for the CLI call.
        model: Optional model override.
        system_prompt: If provided, replaces the CLI's built-in system prompt
            (~11k tokens) with this lightweight one. Use for simple text-in/text-out
            calls that don't need the full agent system prompt.
        disable_tools: If True, passes --tools "" to disable built-in tool definitions.
            Reduces token overhead for calls that don't need tool use.
    """
    import logging
    logging.getLogger(__name__).info(
        f"[Claude CLI] Sending prompt ({len(prompt)} chars, ~{len(prompt)//4} tokens)"
        + (" [lightweight]" if system_prompt else "")
    )
    import tempfile
    cwd = tempfile.gettempdir()

    cmd = [
        "claude", "-p",
        "--output-format", "text",
        "--no-session-persistence",
    ]
    if model:
        cmd.extend(["--model", model])
    if system_prompt is not None:
        cmd.extend(["--system-prompt", system_prompt])
    if disable_tools:
        cmd.extend(["--tools", ""])
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=cwd,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "Claude CLI not found. Install it with: npm install -g @anthropic-ai/claude-code"
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Claude CLI timed out after {timeout_seconds}s")

    if result.returncode != 0:
        error_detail = result.stderr.strip() or result.stdout.strip()[:500]
        raise RuntimeError(
            f"Claude CLI failed (exit {result.returncode}): {error_detail}"
        )

    return result.stdout.strip()


def _tool_to_schema(tool: BaseTool) -> dict:
    """Convert a LangChain BaseTool to a simplified schema dict for the prompt.

    Strips runtime-injected parameters (InjectedState, InjectedToolCallId) and
    removes their associated $defs to keep tool schemas compact. This prevents
    the full State/message model hierarchy from bloating every tool definition.
    """
    schema = {"name": tool.name, "description": tool.description}
    if not (hasattr(tool, "args_schema") and tool.args_schema):
        return schema

    params = tool.args_schema.model_json_schema()

    # Strip injected runtime parameters
    injected = {"tool_call_id", "state"}
    props = params.get("properties", {})
    for param in injected:
        props.pop(param, None)
    if "required" in params:
        params["required"] = [r for r in params["required"] if r not in injected]

    # Remove $defs no longer referenced after stripping injected params
    if "$defs" in params:
        remaining = json.dumps({k: v for k, v in params.items() if k != "$defs"})
        used = set()
        for name in params["$defs"]:
            if f"$defs/{name}" in remaining:
                used.add(name)
        # Resolve transitive refs within used $defs
        changed = True
        while changed:
            changed = False
            for name in list(used):
                def_str = json.dumps(params["$defs"][name])
                for other in params["$defs"]:
                    if other not in used and f"$defs/{other}" in def_str:
                        used.add(other)
                        changed = True
        params["$defs"] = {k: v for k, v in params["$defs"].items() if k in used}
        if not params["$defs"]:
            del params["$defs"]

    schema["parameters"] = params
    return schema


def _build_tools_prompt(tools_schemas: list[dict]) -> str:
    """Build the tool-use instruction to append to prompts."""
    tools_json = json.dumps(tools_schemas, indent=2)
    return (
        "\n\nYou have access to the following tools. To use them, respond with a JSON object "
        "containing a \"tool_calls\" array. Each tool call must have \"name\" (tool name) and "
        "\"args\" (object with the tool's parameters).\n\n"
        "Example response format:\n"
        '{"tool_calls": [{"name": "tool_name", "args": {"param1": "value1"}}]}\n\n'
        "IMPORTANT: Respond with ONLY the JSON object. No text before or after.\n\n"
        f"Available tools:\n{tools_json}"
    )


class _ClaudeCLIStructuredOutput(Runnable):
    """A Runnable that calls Claude CLI and parses the output into a Pydantic model.

    Uses lightweight mode by default: replaces the CLI's ~11k built-in system prompt
    with a minimal JSON-output instruction, and disables built-in tools.
    """

    def __init__(self, schema: type[BaseModel], timeout_seconds: int = 300, model_name: str = "claude"):
        self.schema = schema
        self.timeout_seconds = timeout_seconds
        self.model_name = model_name

    def invoke(self, input: Any, config: RunnableConfig | None = None, **kwargs: Any) -> Any:
        messages = input if isinstance(input, list) else [input]

        # Extract SystemMessages to pass via --system-prompt flag
        system_parts = []
        other_messages = []
        for m in messages:
            if isinstance(m, SystemMessage):
                system_parts.append(str(m.content))
            else:
                other_messages.append(m)

        prompt = _format_messages(other_messages) if other_messages else _format_messages(messages)

        schema_json = json.dumps(self.schema.model_json_schema(), indent=2)
        full_prompt = (
            f"{prompt}\n\n"
            f"IMPORTANT: You MUST respond with ONLY a valid JSON object that conforms to this schema. "
            f"Do NOT include any text before or after the JSON. Do NOT use markdown code fences.\n\n"
            f"JSON Schema:\n{schema_json}"
        )

        # Build a lightweight system prompt combining any extracted SystemMessages
        # with the JSON output instruction
        system_prompt = "\n".join(system_parts) if system_parts else None
        if system_prompt:
            system_prompt += "\n\nAlways respond with valid JSON matching the provided schema."
        else:
            system_prompt = "You are a helpful assistant. Always respond with valid JSON matching the provided schema."

        model = self.model_name if self.model_name != "claude" else None
        content = _call_claude_cli(
            full_prompt, self.timeout_seconds, model=model,
            system_prompt=system_prompt, disable_tools=True,
        )
        json_str = _extract_json(content)

        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError:
            raise ValueError(
                f"Claude CLI returned invalid JSON.\nRaw output:\n{content}"
            )

        return self.schema.model_validate(parsed)

    async def ainvoke(self, input: Any, config: RunnableConfig | None = None, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self.invoke, input, config, **kwargs)


class ChatClaudeCLIWithTools(BaseChatModel):
    """A ChatClaudeCLI variant that includes tool definitions in prompts
    and parses tool calls from responses."""

    model_name: str = "claude"
    timeout_seconds: int = 300
    tools_schemas: list[dict] = []
    tools_prompt_suffix: str = ""

    @property
    def _llm_type(self) -> str:
        return "claude-cli-tools"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        # Extract SystemMessages to pass via --system-prompt flag,
        # replacing the CLI's heavy ~11k built-in system prompt
        system_parts = []
        other_messages = []
        for m in messages:
            if isinstance(m, SystemMessage):
                system_parts.append(str(m.content))
            else:
                other_messages.append(m)

        prompt = _format_messages(other_messages) + self.tools_prompt_suffix
        system_prompt = "\n".join(system_parts) if system_parts else "You are a helpful assistant."
        model = self.model_name if self.model_name != "claude" else None
        content = _call_claude_cli(
            prompt, self.timeout_seconds, model=model,
            system_prompt=system_prompt, disable_tools=True,
        )

        # Try to parse tool calls from the response
        json_str = _extract_json(content)
        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError:
            # Not JSON — return as plain text response
            message = AIMessage(content=content)
            return ChatResult(generations=[ChatGeneration(message=message)])

        # Handle tool_calls format
        tool_calls_data = []
        if isinstance(parsed, dict) and "tool_calls" in parsed:
            tool_calls_data = parsed["tool_calls"]
        elif isinstance(parsed, list):
            tool_calls_data = parsed
        elif isinstance(parsed, dict) and "name" in parsed:
            # Single tool call
            tool_calls_data = [parsed]

        if tool_calls_data:
            tool_calls = []
            for tc in tool_calls_data:
                tool_calls.append({
                    "name": tc.get("name", ""),
                    "args": tc.get("args", tc.get("arguments", {})),
                    "id": tc.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                    "type": "tool_call",
                })
            message = AIMessage(content="", tool_calls=tool_calls)
        else:
            message = AIMessage(content=content)

        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return await asyncio.to_thread(
            self._generate, messages, stop, run_manager, **kwargs
        )


class ChatClaudeCLI(BaseChatModel):
    """LangChain chat model that delegates to the `claude` CLI subprocess.

    This uses `claude -p <prompt> --output-format text` which leverages
    the user's authenticated Claude Code session (e.g. Max 5X plan).

    Set lightweight=True for simple text-in/text-out calls that don't need the
    full agent system prompt (~11k tokens). In lightweight mode:
    - SystemMessages are extracted and passed via --system-prompt flag
    - Built-in tools are disabled via --tools ""
    - This reduces overhead from ~22k to ~1k tokens per call
    """

    model_name: str = "claude"
    timeout_seconds: int = 300
    lightweight: bool = False

    @property
    def _llm_type(self) -> str:
        return "claude-cli"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        model = self.model_name if self.model_name != "claude" else None

        if self.lightweight:
            # Extract SystemMessage and pass via --system-prompt flag
            # to replace the CLI's heavy built-in system prompt
            system_parts = []
            other_messages = []
            for m in messages:
                if isinstance(m, SystemMessage):
                    system_parts.append(str(m.content))
                else:
                    other_messages.append(m)
            system_prompt = "\n".join(system_parts) if system_parts else "You are a helpful assistant."
            prompt = _format_messages(other_messages) if other_messages else _format_messages(messages)
            content = _call_claude_cli(
                prompt, self.timeout_seconds, model=model,
                system_prompt=system_prompt, disable_tools=True,
            )
        else:
            prompt = _format_messages(messages)
            content = _call_claude_cli(prompt, self.timeout_seconds, model=model)

        message = AIMessage(content=content)
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return await asyncio.to_thread(
            self._generate, messages, stop, run_manager, **kwargs
        )

    def bind_tools(
        self,
        tools: list[Any],
        *,
        parallel_tool_calls: bool = True,
        **kwargs: Any,
    ) -> "ChatClaudeCLIWithTools":
        """Return a new model instance that includes tool definitions in prompts."""
        tools_schemas = []
        for tool in tools:
            if isinstance(tool, BaseTool):
                tools_schemas.append(_tool_to_schema(tool))
            elif isinstance(tool, dict):
                tools_schemas.append(tool)
            elif hasattr(tool, "model_json_schema"):
                tools_schemas.append({
                    "name": getattr(tool, "__name__", "tool"),
                    "parameters": tool.model_json_schema(),
                })

        suffix = _build_tools_prompt(tools_schemas)
        if parallel_tool_calls:
            suffix += "\nYou MAY call multiple tools in a single response."

        return ChatClaudeCLIWithTools(
            model_name=self.model_name,
            timeout_seconds=self.timeout_seconds,
            tools_schemas=tools_schemas,
            tools_prompt_suffix=suffix,
        )

    def with_structured_output(
        self,
        schema: type[BaseModel] | dict | Any,
        *,
        include_raw: bool = False,
        **kwargs: Any,
    ) -> Runnable:
        """Return a Runnable that calls Claude CLI and parses output into the given schema."""
        if isinstance(schema, dict):
            from pydantic import create_model
            fields = {}
            for key, value in schema.get("properties", {}).items():
                field_type = str
                if value.get("type") == "integer":
                    field_type = int
                elif value.get("type") == "boolean":
                    field_type = bool
                elif value.get("type") == "array":
                    field_type = list
                fields[key] = (field_type, ...)
            schema = create_model("DynamicSchema", **fields)

        return _ClaudeCLIStructuredOutput(
            schema=schema,
            timeout_seconds=self.timeout_seconds,
            model_name=self.model_name,
        )
