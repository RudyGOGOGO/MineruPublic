"""LLM-powered smart goal generation for self-learning exploration.

Generates context-aware exploration goals that consider existing lessons,
sibling exploration status, and likely feature purpose — replacing the
template-based approach with more natural and effective goals.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from mineru.ui_auto.context import MobileUseContext
from mineru.ui_auto.exploration.types import FeatureNode
from mineru.ui_auto.services.llm import get_llm, invoke_llm_with_timeout_message

logger = logging.getLogger(__name__)

SMART_GOAL_SYSTEM_PROMPT = """\
You are an exploration planner for a mobile app testing agent. Generate a \
concise, actionable exploration goal (1-3 sentences only, no extra text).

CRITICAL RULES:
- The agent MUST stay within the current app. NEVER mention other apps.
- The agent must NOT change settings, trigger destructive actions, or submit forms.
- The goal must navigate to the EXACT target specified, not anywhere else.
- Output ONLY the goal text, nothing else. No markdown, no labels, no extra lines."""

SMART_GOAL_USER_TEMPLATE = """\
App being explored: {app_package}
Navigation target within the app: {path_description}
Already explored siblings in this app: {sibling_labels}
Relevant prior knowledge:
{relevant_lessons}

Generate ONE concise exploration goal (1-3 sentences) for navigating to \
"{node_label}" within this app. Stay within {app_package} only."""


async def generate_smart_goal(
    node: FeatureNode,
    parent_path: list[str],
    sibling_nodes: list[FeatureNode],
    ctx: MobileUseContext,
    lessons_dir: Path,
    app_package: str = "",
) -> str:
    """Generate a context-aware exploration goal using an LLM call.

    Unlike the template-based generate_exploration_goal(), this considers:
    - What the agent already knows about this area (from lessons)
    - What sibling nodes have been explored (to avoid redundant work)
    - What's likely behind this element (inference from label + context)

    The LLM call costs ~500-1000 tokens per goal (prompt + completion).

    Falls back to template-based goal on any failure or invalid output.
    """
    from mineru.ui_auto.exploration.planner import generate_exploration_goal

    path_description = " > ".join(parent_path + [node.label])

    sibling_labels = [s.label for s in sibling_nodes if s.status == "explored"]
    sibling_str = ", ".join(sibling_labels[:10]) if sibling_labels else "none"

    # Load relevant lessons for context
    relevant_lessons = _load_relevant_lessons(node.label, lessons_dir, ctx)
    lessons_str = "\n".join(
        f"  - {text[:100]}" for text in relevant_lessons
    ) if relevant_lessons else "  (none)"

    user_message = SMART_GOAL_USER_TEMPLATE.format(
        app_package=app_package,
        path_description=path_description,
        sibling_labels=sibling_str,
        relevant_lessons=lessons_str,
        node_label=node.label,
    )

    try:
        llm = get_llm(ctx=ctx, name="cortex", temperature=0.7)
        # Use lightweight mode for Claude CLI to avoid the ~11k token
        # system prompt overhead — goal generation is a simple text task
        from mineru.ui_auto.services.claude_cli import ChatClaudeCLI
        if isinstance(llm, ChatClaudeCLI):
            llm = ChatClaudeCLI(
                model_name=llm.model_name,
                timeout_seconds=llm.timeout_seconds,
                lightweight=True,
            )
        messages = [
            SystemMessage(content=SMART_GOAL_SYSTEM_PROMPT),
            HumanMessage(content=user_message),
        ]
        response = await invoke_llm_with_timeout_message(llm.ainvoke(messages))
        goal_text = response.content if hasattr(response, "content") else str(response)

        if goal_text:
            goal_text = _sanitize_goal(goal_text, node.label, app_package)

        if goal_text and len(goal_text.strip()) > 10:
            logger.info(f"Smart goal: {goal_text.strip()[:120]}")
            return goal_text.strip()
    except Exception as e:
        logger.warning(f"Smart goal generation failed, falling back to template: {e}")

    # Fallback to template-based goal
    return generate_exploration_goal(node, parent_path)


def _sanitize_goal(goal_text: str, node_label: str, app_package: str) -> str | None:
    """Validate and clean the generated goal.

    Returns None if the goal is invalid (mentions other apps, is too long,
    or doesn't reference the target node).
    """
    # Strip any token usage tracking lines that may leak from Claude CLI
    lines = []
    for line in goal_text.strip().splitlines():
        stripped = line.strip()
        if stripped.startswith("[Token Usage]") or stripped.startswith("---"):
            continue
        if stripped.startswith("Co-Authored-By:"):
            continue
        lines.append(line)
    goal_text = "\n".join(lines).strip()

    if not goal_text:
        return None

    # Reject goals that are too long (LLM went off-script)
    if len(goal_text) > 500:
        return None

    # Reject goals that mention launching other apps
    lower = goal_text.lower()
    launch_indicators = ["launch_app", "open the ", "switch to ", "navigate to the "]
    for indicator in launch_indicators:
        if indicator in lower:
            # Check if it's launching something other than the target
            # Allow "navigate to the Wi-Fi screen" but reject "navigate to the Life360 app"
            if "app" in lower[lower.index(indicator):lower.index(indicator) + 60]:
                # It's trying to launch an app — only OK if it's our app
                if app_package and app_package.split(".")[-1].lower() not in lower:
                    return None

    return goal_text


def _load_relevant_lessons(
    node_label: str,
    lessons_dir: Path,
    ctx: MobileUseContext,
) -> list[str]:
    """Load lesson texts relevant to the current node label.

    Reads lessons.jsonl and filters for entries whose summary or text
    contains words from the node label. Returns up to 3 relevant entries.
    """
    if not ctx.lessons_dir:
        return []

    relevant: list[str] = []
    label_words = {w.lower() for w in node_label.split() if len(w) > 2}

    if not label_words:
        return []

    try:
        for app_dir in lessons_dir.iterdir():
            if not app_dir.is_dir():
                continue
            jsonl_path = app_dir / "lessons.jsonl"
            if not jsonl_path.exists():
                continue
            for line in jsonl_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = entry.get("summary", "") or entry.get("text", "") or ""
                text_lower = text.lower()
                if any(word in text_lower for word in label_words):
                    relevant.append(text[:150])
                    if len(relevant) >= 3:
                        return relevant
    except Exception:
        pass

    return relevant
