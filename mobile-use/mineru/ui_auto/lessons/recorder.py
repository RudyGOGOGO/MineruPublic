import hashlib
import json
import re
import tempfile
import time
from datetime import datetime, UTC
from pathlib import Path

import aiofiles

from mineru.ui_auto.lessons.types import AppMeta, LessonContext, LessonEntry, PathStep, ScreenSignature
from mineru.ui_auto.utils.logger import get_logger

logger = get_logger(__name__)


def _normalize_goal_for_dedup(goal: str, app_package: str = "") -> str:
    """Normalize a goal string for dedup comparison.

    Strips punctuation, collapses whitespace, lowercases, and replaces
    app name variations with a placeholder so semantically identical
    goals match even when the LLM uses different app name references.
    """
    text = goal.lower()
    # Replace app package name and common app name variations with placeholder
    if app_package:
        text = text.replace(app_package, "APP")
        # Also replace human-readable app name variants
        # e.g., "com.verizon.familybase.parent" → parts: "verizon", "familybase", "parent"
        parts = [p for p in app_package.split(".") if p not in ("com", "android", "google")]
        for part in parts:
            # Match "Verizon FamilyBase app", "Verizon Family app", etc.
            text = re.sub(
                rf"\b{re.escape(part)}\b[\w\s]{{0,30}}\b(app|application)\b",
                "APP",
                text,
            )
    # Strip punctuation (quotes, periods, commas) but keep spaces
    text = re.sub(r"[^\w\s]", " ", text)
    # Collapse whitespace
    text = " ".join(text.split())
    return text


def _compute_path_fingerprint(steps: list) -> str:
    """Compute a fingerprint from path steps for dedup.

    Uses (action, target_text) pairs — two paths with the same tap sequence
    are the same navigation path regardless of goal wording.
    """
    parts = []
    for step in steps:
        if hasattr(step, "action"):
            parts.append(f"{step.action}:{step.target_text or step.target_resource_id or ''}")
        else:
            parts.append(f"{step.get('action', '')}:{step.get('target_text') or step.get('target_resource_id', '')}")
    return hashlib.md5("|".join(parts).encode()).hexdigest()


def _compute_path_fingerprint_from_dicts(steps: list[dict]) -> str:
    """Compute path fingerprint from raw dict steps (from JSONL)."""
    parts = []
    for step in steps:
        parts.append(f"{step.get('action', '')}:{step.get('target_text') or step.get('target_resource_id', '')}")
    return hashlib.md5("|".join(parts).encode()).hexdigest()


# Session-local counter — resets each agent run, not persisted to state or disk.
# This is correct: we want per-session dedup, cross-session dedup happens via JSONL compaction.
_tap_no_effect_counts: dict[str, int] = {}


async def record_lesson(
    lessons_dir: Path,
    app_package: str,
    lesson: LessonEntry,
) -> None:
    """Append a lesson to the app's JSONL file. Concurrent-safe (append-only)."""
    app_dir = lessons_dir / app_package
    app_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = app_dir / "lessons.jsonl"

    # Phase 3: Tag lesson with app version from _meta.json if available
    if lesson.app_version is None:
        try:
            meta_path = app_dir / "_meta.json"
            if meta_path.exists():
                async with aiofiles.open(meta_path) as f:
                    meta = json.loads(await f.read())
                    lesson.app_version = meta.get("app_version")
        except Exception:
            pass  # Version tagging is best-effort

    # Append single line — no read-modify-write, no lock needed
    async with aiofiles.open(jsonl_path, mode="a") as f:
        await f.write(json.dumps(lesson.model_dump(), default=str) + "\n")

    # Update _index.json (lightweight, rare — only on first lesson for new app)
    await _update_index_if_needed(lessons_dir, app_package)


async def record_no_effect_mistake(
    lessons_dir: Path,
    app_package: str,
    screen_signature: ScreenSignature,
    subgoal: str,
) -> None:
    """Record a mistake when a tap tool succeeded but the screen didn't change.
    Only records on 2nd+ occurrence per screen per session to filter false positives."""
    key = f"{app_package}:{screen_signature.activity or 'unknown'}"
    _tap_no_effect_counts[key] = _tap_no_effect_counts.get(key, 0) + 1

    if _tap_no_effect_counts[key] < 2:
        return  # First occurrence — might be legitimate (text field focus, etc.), skip

    category = infer_category(subgoal, "tap")
    lesson = LessonEntry(
        id=generate_lesson_id(category),
        type="mistake",
        category=category,
        summary=f"Tap had no visible effect on screen ({screen_signature.activity or 'unknown'})",
        context=LessonContext(
            goal=subgoal,
            screen_signature=screen_signature,
            action_attempted="tap (succeeded but screen unchanged)",
            what_happened="Tool returned success but screenshot is identical to before the tap",
        ),
        lesson="The tapped element may already be selected, disabled, or non-interactive. "
        "Try an alternative approach.",
        suggested_strategy="Check element state (selected, enabled, clickable) in UI hierarchy "
        "before tapping. Consider using search or swipe instead.",
        confidence=0.5,
        occurrences=1,
        applied_success=0,
        applied_failure=0,
        created=datetime.now(UTC),
        last_seen=datetime.now(UTC),
        deprecated=False,
    )
    await record_lesson(lessons_dir, app_package, lesson)


async def record_mistake_from_tool_failure(
    lessons_dir: Path,
    app_package: str,
    tool_name: str,
    tool_error: str,
    screen_signature: ScreenSignature,
    subgoal: str,
) -> None:
    """Record a mistake when a tool returns status='error'."""
    category = infer_category(subgoal, tool_name)
    lesson = LessonEntry(
        id=generate_lesson_id(category),
        type="mistake",
        category=category,
        summary=f"Tool '{tool_name}' failed: {_truncate(tool_error, 100)}",
        context=LessonContext(
            goal=subgoal,
            screen_signature=screen_signature,
            action_attempted=f"{tool_name} tool call",
            what_happened=f"Tool returned error: {_truncate(tool_error, 200)}",
        ),
        lesson=f"The '{tool_name}' tool failed in this context. "
        "The element may not exist, be off-screen, or have changed.",
        suggested_strategy=f"Verify the target element exists in the UI hierarchy before calling "
        f"{tool_name}. Consider alternative approaches.",
        confidence=0.5,
        occurrences=1,
        applied_success=0,
        applied_failure=0,
        created=datetime.now(UTC),
        last_seen=datetime.now(UTC),
        deprecated=False,
    )
    await record_lesson(lessons_dir, app_package, lesson)


async def record_subgoal_failure(
    lessons_dir: Path,
    app_package: str,
    subgoal_description: str,
    completion_reason: str | None,
    cortex_last_thought: str | None,
) -> None:
    """Record a mistake when a subgoal fails and triggers replanning."""
    category = infer_category(subgoal_description, "subgoal_failure")
    lesson = LessonEntry(
        id=generate_lesson_id(category),
        type="mistake",
        category=category,
        summary=f"Subgoal failed: {_truncate(subgoal_description, 80)}",
        context=LessonContext(
            goal=subgoal_description,
            action_attempted=f"Attempted to complete subgoal: {subgoal_description}",
            what_happened=f"Subgoal failed ({completion_reason or 'no reason given'}). "
            f"Last Cortex thought: {_truncate(cortex_last_thought or 'N/A', 150)}",
        ),
        lesson=f"The approach to '{_truncate(subgoal_description, 60)}' did not work. "
        "Consider breaking it into smaller steps or using an alternative path.",
        suggested_strategy="Try a different approach for this subgoal. "
        "Check if prerequisites are met before attempting.",
        confidence=0.5,
        occurrences=1,
        applied_success=0,
        applied_failure=0,
        created=datetime.now(UTC),
        last_seen=datetime.now(UTC),
        deprecated=False,
    )
    await record_lesson(lessons_dir, app_package, lesson)


async def record_strategy(
    lessons_dir: Path,
    app_package: str,
    strategy_text: str,
    screen_signature: ScreenSignature,
    subgoal: str,
) -> None:
    """Record a strategy that worked well. Phase 3 — called by heuristic detection, not LLM."""
    category = infer_category(subgoal, "strategy")
    lesson = LessonEntry(
        id=generate_lesson_id(category),
        type="strategy",
        category=category,
        summary=strategy_text,
        context=LessonContext(goal=subgoal, screen_signature=screen_signature),
        lesson=strategy_text,
        suggested_strategy=strategy_text,
        confidence=0.5,
        occurrences=1,
        applied_success=0,
        applied_failure=0,
        created=datetime.now(UTC),
        last_seen=datetime.now(UTC),
        deprecated=False,
    )
    await record_lesson(lessons_dir, app_package, lesson)


async def record_success_path(
    lessons_dir: Path,
    app_package: str,
    subgoal_description: str,
    action_trail: list[dict],
) -> None:
    """Record the successful navigation path for a completed subgoal.

    Reads from the structured action_trail (populated by post_executor_tools),
    filters to successful navigation actions, eliminates wrong turns, and
    stores the clean path. Only records paths with >= 2 steps.
    """
    # Convert trail entries to PathSteps (only successful actions)
    steps = [
        PathStep(
            action=entry["action"],
            target_text=entry.get("target_text"),
            target_resource_id=entry.get("target_resource_id"),
            result=(entry.get("agent_thought") or "")[:80],
        )
        for entry in action_trail
        if entry.get("status") == "success"
    ]

    steps = _eliminate_wrong_turns(steps)

    if len(steps) < 2:
        return  # Too trivial to record

    # Write-time dedup: skip if a success_path with the same path steps OR goal exists.
    # Two-tier dedup:
    # 1. Path-based: same sequence of (action, target_text) = same navigation path,
    #    regardless of how the LLM phrased the goal. This is the strongest signal.
    # 2. Goal-based: normalized goal text match (catches rephrased goals with different paths).
    jsonl_path = lessons_dir / app_package / "lessons.jsonl"
    if jsonl_path.exists():
        normalized_goal = _normalize_goal_for_dedup(subgoal_description, app_package)
        path_fingerprint = _compute_path_fingerprint(steps)
        try:
            for line in jsonl_path.read_text().splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("type") != "success_path":
                    continue

                # Tier 1: Path fingerprint match (same tap sequence = same path)
                existing_steps = entry.get("path", [])
                if existing_steps and path_fingerprint == _compute_path_fingerprint_from_dicts(existing_steps):
                    logger.debug(f"Skipping duplicate success_path (same path): {subgoal_description[:60]}")
                    return

                # Tier 2: Goal text match
                existing_goal = entry.get("context", {}).get("goal")
                if (existing_goal
                        and _normalize_goal_for_dedup(existing_goal, app_package) == normalized_goal):
                    logger.debug(f"Skipping duplicate success_path (same goal): {subgoal_description[:60]}")
                    return
        except Exception:
            pass  # Dedup is best-effort; proceed to record

    # Build compact summary from steps
    path_description = " → ".join(
        f"{s.action}('{s.target_text or s.target_resource_id or '?'}')"
        for s in steps
    )
    summary = f"{_truncate(subgoal_description, 50)}: {_truncate(path_description, 100)}"

    category = infer_category(subgoal_description, "success_path")
    lesson = LessonEntry(
        id=generate_lesson_id(category),
        type="success_path",
        category=category,
        summary=summary,
        context=LessonContext(
            goal=subgoal_description,
        ),
        lesson=f"Proven path for '{_truncate(subgoal_description, 60)}': {path_description}",
        suggested_strategy=f"Follow this path: {path_description}",
        path=steps,
        confidence=0.5,
        occurrences=1,
    )
    await record_lesson(lessons_dir, app_package, lesson)


def _eliminate_wrong_turns(steps: list[PathStep]) -> list[PathStep]:
    """Remove wrong-turn pairs using a stack. O(n) single pass."""
    stack: list[PathStep] = []
    for step in steps:
        if step.action == "back":
            if stack:
                stack.pop()
        else:
            stack.append(step)
    return stack


def compute_confidence(applied_success: int, applied_failure: int) -> float:
    """Bayesian-ish confidence with a prior of 0.5."""
    if applied_success + applied_failure == 0:
        return 0.5  # No data — neutral prior
    return applied_success / (applied_success + applied_failure)


async def update_lesson_feedback(
    lessons_dir: Path,
    app_package: str,
    applied_ids: list[str],
    failed_ids: list[str],
    screen_changed: bool | None,
    tool_status: str | None,
) -> None:
    """
    Update confidence counters for lessons the Cortex reported using.
    Appends update entries to JSONL — compaction merges them with originals on next read.
    """
    from mineru.ui_auto.lessons.loader import load_and_compact_lessons

    jsonl_path = lessons_dir / app_package / "lessons.jsonl"
    if not jsonl_path.exists():
        return

    lessons = await load_and_compact_lessons(jsonl_path)
    lessons_by_id = {lesson.id: lesson for lesson in lessons}

    updates: list[LessonEntry] = []
    for lid in applied_ids:
        if lid in lessons_by_id:
            lesson = lessons_by_id[lid]
            # Strategy was followed — did it work?
            if screen_changed and tool_status == "success":
                lesson.applied_success += 1
            else:
                lesson.applied_failure += 1
            lesson.confidence = compute_confidence(
                lesson.applied_success, lesson.applied_failure
            )
            lesson.last_seen = datetime.now(UTC)
            updates.append(lesson)

    for lid in failed_ids:
        if lid in lessons_by_id:
            lesson = lessons_by_id[lid]
            lesson.applied_failure += 1
            lesson.confidence = compute_confidence(
                lesson.applied_success, lesson.applied_failure
            )
            if lesson.applied_failure >= 3 and lesson.confidence < 0.3:
                lesson.deprecated = True
            lesson.last_seen = datetime.now(UTC)
            updates.append(lesson)

    # Append updated entries — compaction on next read will merge with originals
    if updates:
        async with aiofiles.open(jsonl_path, mode="a") as f:
            for lesson in updates:
                await f.write(json.dumps(lesson.model_dump(), default=str) + "\n")


def generate_lesson_id(category: str) -> str:
    """Generate a short, unique lesson ID. Format: nav-a3f1, msg-b2c4, etc."""
    prefix = category[:3]  # nav, msg, sea, med, set, gen
    hash_input = f"{time.time_ns()}"
    short_hash = hashlib.md5(hash_input.encode()).hexdigest()[:4]
    return f"{prefix}-{short_hash}"


def infer_category(subgoal: str, tool_name: str) -> str:
    """Simple keyword-based category inference."""
    keyword_map = {
        "navigation": ["navigate", "open", "go to", "find", "back", "home", "screen"],
        "messaging": ["send", "message", "type", "text", "chat", "reply"],
        "search": ["search", "look for", "find"],
        "media": ["photo", "image", "video", "camera", "gallery", "record"],
        "settings": ["setting", "toggle", "enable", "disable", "turn on", "turn off"],
    }
    subgoal_lower = subgoal.lower()
    for category, keywords in keyword_map.items():
        if any(kw in subgoal_lower for kw in keywords):
            return category
    return "general"


def capture_screen_signature(
    focused_app_info: str | None,
    ui_hierarchy: list[dict] | None,
) -> ScreenSignature:
    """Extract activity name and top-level visible text elements from UI hierarchy."""
    activity = None
    key_elements: list[str] = []

    if ui_hierarchy:
        # Activity name is often in the root node's "activity" or "window" field
        root = ui_hierarchy[0] if ui_hierarchy else {}
        activity = root.get("activity") or root.get("window_title")

        # Collect visible text from top-level interactive elements (tabs, headers, buttons)
        for elem in ui_hierarchy[:30]:  # Only scan top elements for speed
            text = elem.get("text", "").strip()
            if text and len(text) < 50 and elem.get("displayed", True):
                key_elements.append(text)
                if len(key_elements) >= 10:
                    break

    return ScreenSignature(activity=activity, key_elements=key_elements)


def _truncate(text: str, max_len: int) -> str:
    """Truncate text to max_len, adding ellipsis if needed."""
    return text[:max_len] + "..." if len(text) > max_len else text


async def _update_index_if_needed(lessons_dir: Path, app_package: str) -> None:
    """Add app to _index.json if not already present."""
    index_path = lessons_dir / "_index.json"
    index_data: dict = {"apps": {}}
    if index_path.exists():
        async with aiofiles.open(index_path) as f:
            content = await f.read()
            if content.strip():
                index_data = json.loads(content)

    if app_package in index_data.get("apps", {}):
        return  # Already registered

    index_data.setdefault("apps", {})[app_package] = {
        "display_name": app_package.split(".")[-1].title(),
        "lesson_count": 1,
        "last_updated": datetime.now(UTC).isoformat(),
    }
    await _atomic_write_json(index_path, index_data)


async def _rewrite_compacted(jsonl_path: Path, compacted: list[LessonEntry]) -> None:
    """Atomically rewrite a JSONL file with compacted entries (write-to-temp, then rename)."""
    fd, tmp_path = tempfile.mkstemp(
        dir=jsonl_path.parent, suffix=".jsonl.tmp", prefix=".compact_"
    )
    try:
        async with aiofiles.open(tmp_path, mode="w") as f:
            for lesson in compacted:
                await f.write(json.dumps(lesson.model_dump(), default=str) + "\n")
        Path(tmp_path).replace(jsonl_path)  # Atomic rename on POSIX
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise
    finally:
        import os

        os.close(fd)


async def _atomic_write_json(path: Path, data: dict) -> None:
    """Atomically write a JSON file (write-to-temp, then rename)."""
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".json.tmp")
    try:
        async with aiofiles.open(tmp_path, mode="w") as f:
            await f.write(json.dumps(data, indent=2, default=str))
        Path(tmp_path).replace(path)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise
    finally:
        import os

        os.close(fd)


def extract_app_version(ui_hierarchy: list[dict] | None) -> str | None:
    """Opportunistically extract app version from UIAutomator dump.

    Looks for versionName in the root node or common version-related
    fields. Returns None if not found — the system works fine without it.
    """
    if not ui_hierarchy:
        return None
    root = ui_hierarchy[0] if ui_hierarchy else {}
    # UIAutomator dumps sometimes include version info in root node
    for key in ("versionName", "version_name", "app_version"):
        version = root.get(key)
        if version and isinstance(version, str):
            return version
    return None


async def update_app_meta(
    lessons_dir: Path,
    app_package: str,
    app_version: str | None = None,
    version_source: str = "uiautomator_dump",
) -> None:
    """Create or update _meta.json for an app.

    Only writes if the file doesn't exist or if a new version is detected.
    """
    app_dir = lessons_dir / app_package
    app_dir.mkdir(parents=True, exist_ok=True)
    meta_path = app_dir / "_meta.json"

    existing_meta: dict | None = None
    if meta_path.exists():
        async with aiofiles.open(meta_path) as f:
            content = await f.read()
            if content.strip():
                existing_meta = json.loads(content)

    if existing_meta:
        existing_version = existing_meta.get("app_version")
        # Don't overwrite a known version with None (version not detected this cycle)
        if app_version is None and existing_version is not None:
            return
        # Skip write if nothing changed
        if existing_version == app_version:
            return

    meta = AppMeta(
        package=app_package,
        display_name=app_package.split(".")[-1].title(),
        app_version=app_version,
        version_source=version_source if app_version else None,
        last_verified=datetime.now(UTC).isoformat(),
    )
    await _atomic_write_json(meta_path, meta.model_dump())


async def cleanup_stale_lessons(lessons_dir: Path) -> int:
    """Prune stale and deprecated entries from all app JSONL files.

    Returns the total number of entries removed across all apps.
    """
    from mineru.ui_auto.lessons.loader import (
        STALE_OTHER_DAYS,
        STALE_SUCCESS_PATH_DAYS,
        STALE_UI_MAPPING_DAYS,
        load_and_compact_lessons,
    )

    if not lessons_dir.exists():
        return 0

    now = datetime.now(UTC)
    total_removed = 0

    for app_dir in lessons_dir.iterdir():
        if not app_dir.is_dir() or app_dir.name.startswith("_"):
            continue
        jsonl_path = app_dir / "lessons.jsonl"
        if not jsonl_path.exists():
            continue

        lessons = await load_and_compact_lessons(jsonl_path)
        original_count = len(lessons)

        # Filter out stale and deprecated entries
        kept = []
        for lesson in lessons:
            if lesson.deprecated:
                continue
            days_old = (now - lesson.last_seen).days
            if lesson.type == "ui_mapping" and days_old > STALE_UI_MAPPING_DAYS:
                continue
            if lesson.type == "success_path" and days_old > STALE_SUCCESS_PATH_DAYS:
                continue
            if lesson.type not in ("ui_mapping", "success_path") and days_old > STALE_OTHER_DAYS:
                continue
            kept.append(lesson)

        removed = original_count - len(kept)
        if removed > 0:
            await _rewrite_compacted(jsonl_path, kept)
            total_removed += removed

    return total_removed
