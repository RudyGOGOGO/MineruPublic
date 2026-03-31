"""Cross-app lesson generation from exploration trigger data.

Converts cross-app triggers discovered during cluster mode exploration
into lesson entries that the Contextor can load during real tasks.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import aiofiles

from mineru.ui_auto.exploration.types import ExplorationState, FeatureNode

logger = logging.getLogger(__name__)


def generate_cross_app_lessons(
    app_package: str,
    state: ExplorationState,
    lessons_dir: Path,
) -> list[dict]:
    """Convert cross-app triggers into lesson entries loadable by the Contextor.

    For each node with cross_app_triggers, generates a lesson entry like:

    {
        "type": "cross_app_trigger",
        "app_package": "com.parent.app",
        "text": "After tapping 'Enable Screen Time' in com.parent.app, "
                "com.child.app shows a banner notification 'Screen Time is now active' "
                "(reliability: 90%, typical latency: 2.5s)",
        "trigger_action": "tap('Enable Screen Time')",
        "trigger_path": "Settings > Parental Controls > Enable Screen Time",
        "target_app": "com.child.app",
        "expected_event": "banner_notification",
        "reliability_score": 0.9,
    }

    These entries are appended to the app's lessons.jsonl and loaded by the
    Contextor alongside regular success_path/mistake/strategy lessons.
    """
    lessons: list[dict] = []

    def _walk(node: FeatureNode, path: list[str]) -> None:
        for trigger in node.cross_app_triggers:
            if trigger.reliability_score < 0.3:
                continue  # Too unreliable to surface

            path_str = " > ".join(path + [node.label])
            text = (
                f"After {node.nav_action or 'acting on ' + node.label} "
                f"(path: {path_str}), "
                f"{trigger.target_app} shows {trigger.expected_event}"
            )
            if trigger.expected_ui_text:
                text += f" with text '{trigger.expected_ui_text}'"
            text += f" (reliability: {trigger.reliability_score:.0%}, "
            text += f"typical latency: {trigger.latency_ms / 1000:.1f}s)"

            lessons.append({
                "type": "cross_app_trigger",
                "app_package": app_package,
                "text": text,
                "summary": text,
                "trigger_action": node.nav_action,
                "trigger_path": path_str,
                "target_app": trigger.target_app,
                "expected_event": trigger.expected_event,
                "reliability_score": trigger.reliability_score,
            })

        for child in node.children:
            _walk(child, path + [node.label])

    _walk(state.root, [])
    return lessons


async def write_cross_app_lessons(
    app_package: str,
    state: ExplorationState,
    lessons_dir: Path,
) -> int:
    """Generate and append cross-app trigger lessons to lessons.jsonl.

    Returns the number of lessons written.
    """
    lessons = generate_cross_app_lessons(app_package, state, lessons_dir)
    if not lessons:
        return 0

    app_dir = lessons_dir / app_package
    app_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = app_dir / "lessons.jsonl"

    async with aiofiles.open(jsonl_path, mode="a") as f:
        for lesson in lessons:
            await f.write(json.dumps(lesson) + "\n")

    logger.info(f"Wrote {len(lessons)} cross-app trigger lessons for {app_package}")
    return len(lessons)
