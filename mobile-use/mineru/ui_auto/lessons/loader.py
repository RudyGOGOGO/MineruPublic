import json
from datetime import datetime, UTC
from pathlib import Path

import aiofiles

from mineru.ui_auto.lessons.scorer import score_lesson
from mineru.ui_auto.lessons.types import LessonEntry

# Staleness hard-cutoffs (excluded entirely, not just penalized)
STALE_UI_MAPPING_DAYS = 30
STALE_SUCCESS_PATH_DAYS = 60
STALE_OTHER_DAYS = 90
TOKEN_BUDGET = 500
APPROX_CHARS_PER_TOKEN = 4  # Conservative estimate; no external tokenizer dependency


async def load_lessons_for_app(
    lessons_dir: Path,
    app_package: str,
    subgoal: str,
    current_activity: str | None,
    current_key_elements: list[str],
) -> str | None:
    """
    Load, filter, score, and format lessons for the current app context.
    Returns a formatted text block ready for Cortex injection, or None if no lessons.
    """
    jsonl_path = lessons_dir / app_package / "lessons.jsonl"
    if not jsonl_path.exists():
        return None

    # Step 1: Read and compact (merges duplicates)
    lessons = await load_and_compact_lessons(jsonl_path)
    if not lessons:
        return None

    now = datetime.now(UTC)

    # Step 2: Hard-filter stale and deprecated lessons
    eligible = []
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
        eligible.append(lesson)

    if not eligible:
        return None

    # Step 3: Score and rank
    scored = [
        (score_lesson(lesson, current_activity, current_key_elements, subgoal, now), lesson)
        for lesson in eligible
    ]
    scored.sort(key=lambda x: x[0], reverse=True)

    # Step 4: Format within token budget
    return format_lessons_text(scored)


async def load_and_compact_lessons(jsonl_path: Path) -> list[LessonEntry]:
    """Read all lessons, merge duplicates, return compacted list.

    Dedup strategy (two-tier):
    1. If two entries share the same `id`, merge them (handles feedback updates).
    2. If two entries have different IDs but near-identical summaries, merge them
       (handles independently-recorded duplicates). The newer ID is kept.
    """
    raw_lessons = []
    async with aiofiles.open(jsonl_path) as f:
        async for line in f:
            line = line.strip()
            if line:
                try:
                    raw_lessons.append(LessonEntry(**json.loads(line)))
                except Exception:
                    continue  # Skip malformed lines

    # Pass 1: Merge by ID (exact match — handles feedback updates)
    by_id: dict[str, LessonEntry] = {}
    for lesson in raw_lessons:
        if lesson.id in by_id:
            _merge_into(by_id[lesson.id], lesson)
        else:
            by_id[lesson.id] = lesson.model_copy()

    # Pass 2: Merge by normalized text (independent duplicates)
    by_summary: dict[str, LessonEntry] = {}
    for lesson in by_id.values():
        # For success_path, dedup by goal (summary includes variable path description).
        # For all other types, dedup by summary (stable descriptions).
        if lesson.type == "success_path" and lesson.context.goal:
            key = _normalize_summary(lesson.context.goal)
        else:
            key = _normalize_summary(lesson.summary)
        if key in by_summary:
            _merge_into(by_summary[key], lesson)
        else:
            by_summary[key] = lesson

    compacted = list(by_summary.values())

    # If file grew significantly, rewrite compacted version and update index
    if len(raw_lessons) > len(compacted) * 1.5 and len(raw_lessons) > 20:
        await _rewrite_compacted(jsonl_path, compacted)
        try:
            await _update_index_count(
                jsonl_path.parent.parent, jsonl_path.parent.name, len(compacted)
            )
        except Exception:
            pass  # Index update is best-effort

    return compacted


def format_lessons_text(scored_lessons: list[tuple[float, LessonEntry]]) -> str | None:
    """
    Format scored lessons into grouped bulleted text within TOKEN_BUDGET.
    Groups: mistakes -> success_paths -> strategies -> ui_mappings.
    Uses two-pass budget allocation to guarantee type diversity.
    Returns None if no lessons fit the budget.
    """
    groups: dict[str, list[str]] = {
        "mistake": [],
        "strategy": [],
        "success_path": [],
        "ui_mapping": [],
        "cross_app_trigger": [],
    }
    running_chars = 0
    max_chars = TOKEN_BUDGET * APPROX_CHARS_PER_TOKEN  # ~2000 chars

    # --- Two-pass budget allocation ---
    # Pass 1: Reserve the top-scoring entry from each type (guarantees diversity).
    # Pass 2: Fill remaining budget with the rest, in scored order.

    reserved_by_type: dict[str, tuple[float, LessonEntry] | None] = {
        t: None for t in groups
    }

    rest: list[tuple[float, LessonEntry]] = []
    for item in scored_lessons:
        _score, lesson = item
        t = lesson.type
        if t in reserved_by_type and reserved_by_type[t] is None:
            reserved_by_type[t] = item
        else:
            rest.append(item)

    # Add reserved entries first (one per type that has lessons)
    for _score, lesson in (v for v in reserved_by_type.values() if v):
        bullet = _format_bullet(lesson)
        bullet_chars = len(bullet) + 2  # "- " prefix
        if running_chars + bullet_chars > max_chars:
            break
        groups.setdefault(lesson.type, []).append(bullet)
        running_chars += bullet_chars

    # Add remaining entries by score until budget exhausted
    for _score, lesson in rest:
        bullet = _format_bullet(lesson)
        bullet_chars = len(bullet) + 2
        if running_chars + bullet_chars > max_chars:
            break
        groups.setdefault(lesson.type, []).append(bullet)
        running_chars += bullet_chars

    # Build output with group headers
    sections = []
    if groups.get("mistake"):
        sections.append(
            "**Mistakes to avoid:**\n" + "\n".join(f"- {b}" for b in groups["mistake"])
        )
    if groups.get("success_path"):
        sections.append(
            "**Known navigation paths:**\n"
            + "\n".join(f"- {b}" for b in groups["success_path"])
        )
    if groups.get("strategy"):
        sections.append(
            "**Proven strategies:**\n" + "\n".join(f"- {b}" for b in groups["strategy"])
        )
    if groups.get("ui_mapping"):
        sections.append(
            "**UI mappings:**\n" + "\n".join(f"- {b}" for b in groups["ui_mapping"])
        )
    if groups.get("cross_app_trigger"):
        sections.append(
            "**Cross-app triggers:**\n"
            + "\n".join(f"- {b}" for b in groups["cross_app_trigger"])
        )

    if not sections:
        return None
    return "\n\n".join(sections)


def _format_bullet(lesson: LessonEntry) -> str:
    """Format a single lesson as a compact one-line bullet."""
    meta = f"[{lesson.id}, confidence: {lesson.confidence:.2f}, seen {lesson.occurrences}x]"
    if lesson.type == "success_path" and lesson.path:
        path_str = " → ".join(
            f"{s.action}('{s.target_text or s.target_resource_id or '?'}')"
            for s in lesson.path
        )
        return f"{lesson.context.goal}: {path_str} {meta}"
    if lesson.type == "ui_mapping":
        return f"{lesson.lesson} {meta}"
    return f"{lesson.summary}. {lesson.suggested_strategy} {meta}"


def _normalize_summary(summary: str) -> str:
    """Normalize summary for dedup matching."""
    return " ".join(summary.lower().split())


def _merge_into(existing: LessonEntry, incoming: LessonEntry) -> None:
    """Merge incoming lesson data into existing. Mutates existing in place."""
    # Determine which entry is newer BEFORE mutating last_seen.
    incoming_is_newer = incoming.last_seen > existing.last_seen

    # For success_path: keep the NEWER path (UI may have changed).
    if existing.type == "success_path" and incoming.path is not None:
        if incoming_is_newer:
            existing.path = incoming.path
            existing.lesson = incoming.lesson
            existing.suggested_strategy = incoming.suggested_strategy

    # Keep the newer ID
    if incoming_is_newer:
        existing.id = incoming.id

    existing.occurrences += incoming.occurrences
    existing.applied_success += incoming.applied_success
    existing.applied_failure += incoming.applied_failure
    existing.last_seen = max(existing.last_seen, incoming.last_seen)

    # Recompute confidence
    total = existing.applied_success + existing.applied_failure
    existing.confidence = existing.applied_success / total if total > 0 else 0.5

    # Deprecation is sticky
    if incoming.deprecated:
        existing.deprecated = True


async def _update_index_count(
    lessons_dir: Path, app_package: str, count: int
) -> None:
    """Update lesson_count in _index.json after compaction."""
    index_path = lessons_dir / "_index.json"
    if not index_path.exists():
        return
    async with aiofiles.open(index_path) as f:
        content = await f.read()
    if not content.strip():
        return
    index_data = json.loads(content)
    app_entry = index_data.get("apps", {}).get(app_package)
    if app_entry:
        app_entry["lesson_count"] = count
        app_entry["last_updated"] = datetime.now(UTC).isoformat()
        # Atomic write
        import tempfile

        fd, tmp_path = tempfile.mkstemp(dir=index_path.parent, suffix=".json.tmp")
        try:
            async with aiofiles.open(tmp_path, mode="w") as f:
                await f.write(json.dumps(index_data, indent=2, default=str))
            Path(tmp_path).replace(index_path)
        except Exception:
            Path(tmp_path).unlink(missing_ok=True)
            raise
        finally:
            import os

            os.close(fd)


async def _rewrite_compacted(jsonl_path: Path, compacted: list[LessonEntry]) -> None:
    """Atomically rewrite a JSONL file with compacted entries (write-to-temp, then rename)."""
    import tempfile

    fd, tmp_path = tempfile.mkstemp(
        dir=jsonl_path.parent, suffix=".jsonl.tmp", prefix=".compact_"
    )
    try:
        async with aiofiles.open(tmp_path, mode="w") as f:
            for lesson in compacted:
                await f.write(json.dumps(lesson.model_dump(), default=str) + "\n")
        Path(tmp_path).replace(jsonl_path)  # Atomic rename on POSIX
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)  # Cleanup on failure
        raise
    finally:
        import os

        os.close(fd)
