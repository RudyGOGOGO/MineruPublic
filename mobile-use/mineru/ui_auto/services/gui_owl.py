"""LangChain ChatModel wrapper for local GUI Owl VLM via mlx-vlm server.

The mlx-vlm server exposes an OpenAI-compatible /v1/chat/completions endpoint
but does NOT support function calling or structured output (response_format).
This wrapper uses prompt-based JSON extraction for structured output and tool
calling, similar to the Claude CLI wrapper approach.

For small models (8B) that struggle with strict JSON formatting, the wrapper
includes a lenient fallback that constructs valid schema objects from
free-text responses.
"""

import json
import logging
import re
import types
import uuid
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, SecretStr

logger = logging.getLogger(__name__)


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


def _lenient_parse_to_schema(text: str, schema: type[BaseModel]) -> BaseModel:
    """Best-effort construction of a Pydantic model from free-text when JSON parsing fails.

    Inspects the schema fields and fills them from the raw text:
    - str fields get the full text
    - list[str] fields get an empty list
    - Optional fields get None
    This ensures the agent pipeline doesn't crash on a small model's formatting failure.
    """
    field_values: dict[str, Any] = {}
    for field_name, field_info in schema.model_fields.items():
        annotation = field_info.annotation

        # Handle Optional types (str | None via UnionType)
        is_optional = False
        args = getattr(annotation, "__args__", ())
        if isinstance(annotation, types.UnionType):
            is_optional = type(None) in args
            real_types = [a for a in args if a is not type(None)]
            annotation = real_types[0] if real_types else str

        if annotation is str:
            # For reason/decision string fields, use the full text as content
            if "reason" in field_name:
                field_values[field_name] = text
            elif "decision" in field_name:
                # decisions_reason gets text, decisions itself stays None
                field_values[field_name] = None
            elif is_optional:
                field_values[field_name] = None
            else:
                field_values[field_name] = text
        elif annotation is list or (hasattr(annotation, "__origin__") and annotation.__origin__ is list):
            field_values[field_name] = field_info.default_factory() if field_info.default_factory else []
        elif is_optional:
            field_values[field_name] = None
        else:
            if field_info.default is not None:
                field_values[field_name] = field_info.default
            elif field_info.default_factory:
                field_values[field_name] = field_info.default_factory()
            else:
                field_values[field_name] = None

    return schema.model_validate(field_values)


def _build_json_example(schema: type[BaseModel]) -> str:
    """Build a concrete JSON example from a Pydantic schema for the prompt."""
    example: dict[str, Any] = {}
    for field_name, field_info in schema.model_fields.items():
        desc = field_info.description or field_name
        annotation = field_info.annotation
        args = getattr(annotation, "__args__", ())

        # Handle Optional (str | None via UnionType)
        if isinstance(annotation, types.UnionType):
            real_types = [a for a in args if a is not type(None)]
            annotation = real_types[0] if real_types else str

        if annotation is str:
            example[field_name] = f"<{desc}>"
        elif annotation == list or (hasattr(annotation, "__origin__") and getattr(annotation, "__origin__", None) is list):
            example[field_name] = []
        elif annotation == bool:
            example[field_name] = False
        elif annotation == int:
            example[field_name] = 0
        else:
            example[field_name] = None
    return json.dumps(example, indent=2)


def _tool_to_schema(tool: Any) -> dict:
    """Convert a LangChain BaseTool or dict to a simplified schema dict."""
    if isinstance(tool, dict):
        return tool
    schema: dict = {"name": tool.name, "description": tool.description}
    if hasattr(tool, "args_schema") and tool.args_schema:
        schema["parameters"] = tool.args_schema.model_json_schema()
    return schema


def _build_tools_system_prompt(tools_schemas: list[dict]) -> str:
    """Build tool-use instructions to inject as a system message."""
    tools_json = json.dumps(tools_schemas, indent=2)
    return (
        "You have access to the following tools. To use them, respond with a JSON object "
        'containing a "tool_calls" array. Each tool call must have "name" (tool name) and '
        '"args" (object with the tool\'s parameters).\n\n'
        "Example response format:\n"
        '{"tool_calls": [{"name": "tool_name", "args": {"param1": "value1"}}]}\n\n'
        "IMPORTANT: Respond with ONLY the JSON object. No text before or after.\n\n"
        f"Available tools:\n{tools_json}"
    )


class _GuiOwlStructuredOutput(Runnable):
    """Calls GUI Owl and parses the response into a Pydantic model via prompt injection.

    Uses a two-stage approach:
    1. Try to parse strict JSON from the response
    2. If that fails, construct a best-effort schema object from the free text
    """

    def __init__(self, llm: "ChatGuiOwl", schema: type[BaseModel]):
        self.llm = llm
        self.schema = schema

    def _build_schema_instruction(self) -> str:
        example_json = _build_json_example(self.schema)
        return (
            "\n\n## RESPONSE FORMAT (MANDATORY)\n"
            "You MUST respond with ONLY a valid JSON object. No explanation, no markdown, no text "
            "before or after. Just the raw JSON.\n\n"
            f"Required format example:\n{example_json}\n\n"
            "Respond with ONLY the JSON now:"
        )

    def _inject_schema_into_messages(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        """Append structured output instruction as the last human message."""
        instruction = self._build_schema_instruction()
        messages = list(messages)

        # Add as a final human message to make it the last thing the model sees
        messages.append(HumanMessage(content=instruction))
        return messages

    def _parse_response(self, result: str) -> BaseModel:
        """Try JSON parse, fall back to lenient text extraction."""
        json_str = _extract_json(result)
        try:
            parsed = json.loads(json_str)
            return self.schema.model_validate(parsed)
        except (json.JSONDecodeError, Exception):
            pass

        # Lenient fallback: construct schema from free text
        logger.warning(
            f"GUI Owl returned non-JSON response, using lenient parser. "
            f"Raw output (first 200 chars): {result[:200]}"
        )
        return _lenient_parse_to_schema(result, self.schema)

    def invoke(self, input: Any, config: RunnableConfig | None = None, **kwargs: Any) -> Any:
        messages = input if isinstance(input, list) else [input]
        messages = self._inject_schema_into_messages(messages)
        result = self.llm._call_openai(messages)
        return self._parse_response(result)

    async def ainvoke(self, input: Any, config: RunnableConfig | None = None, **kwargs: Any) -> Any:
        messages = input if isinstance(input, list) else [input]
        messages = self._inject_schema_into_messages(messages)
        result = await self.llm._acall_openai(messages)
        return self._parse_response(result)


class ChatGuiOwlWithTools(BaseChatModel):
    """GUI Owl variant that includes tool definitions in prompts and parses tool calls."""

    model_name: str = "MLX_GUI_Owl_8B_16bits"
    base_url: str = "http://127.0.0.1:8080/v1"
    temperature: float = 0.7
    tools_schemas: list[dict] = []
    timeout: int = 300

    @property
    def _llm_type(self) -> str:
        return "gui-owl-tools"

    def _get_client(self) -> ChatOpenAI:
        return ChatOpenAI(
            model=self.model_name,
            temperature=self.temperature,
            api_key=SecretStr("not-needed"),
            base_url=self.base_url,
            max_retries=3,
            timeout=self.timeout,
            streaming=False,
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        # Inject tools instruction as system message
        tools_instruction = _build_tools_system_prompt(self.tools_schemas)
        augmented = list(messages)
        for i, m in enumerate(augmented):
            if isinstance(m, SystemMessage):
                augmented[i] = SystemMessage(content=m.content + "\n\n" + tools_instruction)
                break
        else:
            augmented.insert(0, SystemMessage(content=tools_instruction))

        client = self._get_client()
        result = client.invoke(augmented)
        content = result.content if isinstance(result.content, str) else str(result.content)

        # Try to parse tool calls
        json_str = _extract_json(content)
        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError:
            message = AIMessage(content=content)
            return ChatResult(generations=[ChatGeneration(message=message)])

        tool_calls_data = []
        if isinstance(parsed, dict) and "tool_calls" in parsed:
            tool_calls_data = parsed["tool_calls"]
        elif isinstance(parsed, list):
            tool_calls_data = parsed
        elif isinstance(parsed, dict) and "name" in parsed:
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
        import asyncio
        return await asyncio.to_thread(
            self._generate, messages, stop, run_manager, **kwargs
        )


class ChatGuiOwl(BaseChatModel):
    """LangChain chat model for local GUI Owl VLM via mlx-vlm server.

    Uses the OpenAI-compatible API but overrides with_structured_output and
    bind_tools to use prompt-based approaches since mlx-vlm doesn't support
    function calling or response_format.
    """

    model_name: str = "MLX_GUI_Owl_8B_16bits"
    base_url: str = "http://127.0.0.1:8080/v1"
    temperature: float = 0.7
    timeout: int = 300

    @property
    def _llm_type(self) -> str:
        return "gui-owl"

    def _get_client(self) -> ChatOpenAI:
        return ChatOpenAI(
            model=self.model_name,
            temperature=self.temperature,
            api_key=SecretStr("not-needed"),
            base_url=self.base_url,
            max_retries=3,
            timeout=self.timeout,
            streaming=False,
        )

    def _call_openai(self, messages: list[BaseMessage]) -> str:
        """Call the underlying OpenAI-compatible endpoint and return text."""
        client = self._get_client()
        result = client.invoke(messages)
        return result.content if isinstance(result.content, str) else str(result.content)

    async def _acall_openai(self, messages: list[BaseMessage]) -> str:
        """Async version of _call_openai."""
        import asyncio
        return await asyncio.to_thread(self._call_openai, messages)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        content = self._call_openai(messages)
        message = AIMessage(content=content)
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        import asyncio
        return await asyncio.to_thread(
            self._generate, messages, stop, run_manager, **kwargs
        )

    def bind_tools(
        self,
        tools: list[Any],
        *,
        parallel_tool_calls: bool = True,
        **kwargs: Any,
    ) -> ChatGuiOwlWithTools:
        """Return a new model instance that includes tool definitions in prompts."""
        tools_schemas = []
        for tool in tools:
            tools_schemas.append(_tool_to_schema(tool))

        return ChatGuiOwlWithTools(
            model_name=self.model_name,
            base_url=self.base_url,
            temperature=self.temperature,
            tools_schemas=tools_schemas,
            timeout=self.timeout,
        )

    def with_structured_output(
        self,
        schema: type[BaseModel] | dict | Any,
        *,
        include_raw: bool = False,
        **kwargs: Any,
    ) -> Runnable:
        """Return a Runnable that calls GUI Owl and parses output into the given schema."""
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

        return _GuiOwlStructuredOutput(llm=self, schema=schema)
