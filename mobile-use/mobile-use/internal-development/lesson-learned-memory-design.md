# Lesson-Learned Memory System — Design Document

## Problem

The agent repeats the same mistakes across sessions: tapping non-responsive elements, navigating to wrong screens, missing the correct button for a known task. There is no cross-session knowledge transfer — every run starts from zero.

## Goal

A lightweight, file-based memory system that:
1. **Records** mistakes and successful strategies per app during execution
2. **Loads** relevant lessons into the Cortex prompt before decision-making
3. Requires **no infrastructure** — just local JSONL/JSON files

---

## Storage Design

### Directory Structure

```
lessons/
├── _index.json                          # app registry (for fast lookup)
├── com.whatsapp/
│   ├── _meta.json                       # app-level metadata
│   └── lessons.jsonl                    # all lessons for this app (append-only)
├── com.google.android.apps.maps/
│   ├── _meta.json
│   └── lessons.jsonl
└── com.android.settings/
    ├── _meta.json
    └── lessons.jsonl
```

**Why single JSONL per app instead of per-category JSON files**: JSONL (one JSON object per line) is naturally safe for concurrent appends — no read-modify-write cycle, no file locking required. Categories are still stored as a field on each lesson for filtering, but we no longer split into separate files. This eliminates the concurrency/corruption risk entirely. Compaction (dedup, eviction) happens on read, not on write.

### `_index.json` — App Registry

Fast lookup to know which apps have lessons. Updated whenever a new app gets its first lesson.

```json
{
  "apps": {
    "com.whatsapp": {
      "display_name": "WhatsApp",
      "lesson_count": 12,
      "last_updated": "2026-03-25T10:30:00Z"
    },
    "com.google.android.apps.maps": {
      "display_name": "Google Maps",
      "lesson_count": 5,
      "last_updated": "2026-03-24T15:00:00Z"
    }
  }
}
```

### `_meta.json` — App Metadata

```json
{
  "package": "com.whatsapp",
  "display_name": "WhatsApp",
  "app_version": "2.24.10.75",
  "version_source": "uiautomator_dump",
  "last_verified": "2026-03-25T10:30:00Z",
  "notes": "Version collected from Settings > Apps > WhatsApp info screen when available"
}
```

**On app version**: UIAutomator dumps sometimes include `versionName` in the root node or app info screens. We attempt to capture it opportunistically (see Implementation section), but do NOT block on it — the system works fine without version info. Version is used for staleness detection (see Staleness & Confidence Decay section).

### Lesson Entry Format (each line in `lessons.jsonl`)

Each line is one self-contained JSON object:

```json
{"id": "nav-001", "type": "mistake", "category": "navigation", "summary": "Tapping 'Chats' tab icon has no response when already on Chats tab", "context": {"goal": "Navigate to a specific chat", "screen_signature": {"activity": "com.whatsapp/.HomeActivity", "key_elements": ["Chats", "Status", "Calls", "Communities"]}, "action_attempted": "tap on Chats tab icon", "what_happened": "No screen change, element existed but tap had no effect"}, "lesson": "When already on the Chats tab, tapping the tab icon does nothing. Instead, scroll the chat list or use the search icon (resource_id: com.whatsapp:id/menuitem_search) to find the conversation.", "suggested_strategy": "Check if tab is already selected before tapping. Look for 'selected=true' in the UI hierarchy for tab elements.", "confidence": 0.83, "occurrences": 3, "applied_success": 5, "applied_failure": 1, "created": "2026-03-20T14:00:00Z", "last_seen": "2026-03-25T10:30:00Z", "deprecated": false}
```

Expanded for readability:

```json
{
  "id": "nav-001",
  "type": "mistake",
  "category": "navigation",
  "summary": "Tapping 'Chats' tab icon has no response when already on Chats tab",
  "context": {
    "goal": "Navigate to a specific chat",
    "screen_signature": {
      "activity": "com.whatsapp/.HomeActivity",
      "key_elements": ["Chats", "Status", "Calls", "Communities"]
    },
    "action_attempted": "tap on Chats tab icon",
    "what_happened": "No screen change, element existed but tap had no effect"
  },
  "lesson": "When already on the Chats tab, tapping the tab icon does nothing. Instead, scroll the chat list or use the search icon (resource_id: com.whatsapp:id/menuitem_search) to find the conversation.",
  "suggested_strategy": "Check if tab is already selected before tapping. Look for 'selected=true' in the UI hierarchy for tab elements.",
  "confidence": 0.83,
  "occurrences": 3,
  "applied_success": 5,
  "applied_failure": 1,
  "created": "2026-03-20T14:00:00Z",
  "last_seen": "2026-03-25T10:30:00Z",
  "deprecated": false
}
```

### Key Fields Explained

| Field | Purpose |
|-------|---------|
| `category` | Soft grouping (navigation, messaging, etc.) — used for write-time classification, **not** for read-time filtering |
| `context.screen_signature` | Structured screen fingerprint for relevance matching at load time |
| `confidence` | Computed: `applied_success / (applied_success + applied_failure)`. Starts at 0.5 for new lessons. |
| `occurrences` | How many times this pattern was observed (mistake re-detected, or strategy re-confirmed) |
| `applied_success` | Times the `suggested_strategy` was followed and succeeded |
| `applied_failure` | Times the `suggested_strategy` was followed but still failed |
| `deprecated` | Set to `true` when a lesson's strategy fails — soft-deletes without data loss |

### Lesson Types

| Type | When Created |
|------|-------------|
| `mistake` | Tool returned error, tap had no effect, wrong screen reached |
| `strategy` | A non-obvious successful approach worth remembering |
| `ui_mapping` | Mapping a user-facing concept to a specific UI element (e.g., "send button" = `com.whatsapp:id/send`) |

---

## Loading Lessons into the Agent

### Where: Cortex Prompt Injection

The Cortex is the decision-maker — it's the natural place to inject lessons. The Contextor already knows the current `focused_app_info` (package name), so it can load lessons at the same time it captures the screen.

### How: Load-All-and-Rank at Contextor Stage

```
Contextor runs
  ├── get_screen_data()          (existing)
  ├── get_foreground_package()   (existing)
  ├── detect_screen_change()     (NEW — compare previous and current screenshot)
  └── load_lessons()             (NEW)
        ├── Read _index.json → check if app has lessons
        ├── Read ALL lessons from lessons.jsonl (single file read)
        ├── Filter: exclude deprecated=true, apply staleness decay
        ├── Rank by relevance score → select top N within token budget
        └── Format into compact text block → store in state
```

**New state fields:**

```python
# In State (graph/state.py)
active_lessons: str | None = None              # Formatted lesson text for current app, take_last reducer
screen_changed: bool | None = None             # Whether screen changed since last cycle. None = unknown (first cycle or resumed), take_last reducer
previous_screenshot_hash: str | None = None    # Perceptual hash of prior screenshot, take_last reducer
```

**Why `None` default instead of `True`**: Using `True` as default could mask a real no-change situation after checkpoint resume. `None` means "unknown" — the recorder only triggers on explicit `screen_changed is False`, never on `None`.

### Lesson Relevance Scoring

Instead of fragile keyword-category matching, we load **all** lessons for the current app and rank them by a composite relevance score:

```python
def score_lesson(
    lesson: LessonEntry,
    current_activity: str | None,
    current_key_elements: list[str],
    subgoal_description: str,
    now: datetime,
) -> float:
    """Score a lesson for relevance to current context. Higher = more relevant."""
    score = 0.0

    # 1. Screen signature match (strongest signal)
    if lesson.context.screen_signature and current_activity:
        sig = lesson.context.screen_signature
        if sig.activity and sig.activity == current_activity:
            score += 3.0  # Same activity — highly relevant
        if sig.key_elements:
            overlap = len(set(sig.key_elements) & set(current_key_elements))
            score += overlap * 0.5  # Each matching element adds relevance

    # 2. Subgoal text overlap with lesson summary (lightweight TF-IDF substitute)
    subgoal_words = set(subgoal_description.lower().split())
    summary_words = set(lesson.summary.lower().split())
    # Remove stop words
    stop_words = {"the", "a", "an", "to", "on", "in", "for", "is", "and", "or", "of", "with"}
    subgoal_words -= stop_words
    summary_words -= stop_words
    if subgoal_words and summary_words:
        overlap_ratio = len(subgoal_words & summary_words) / min(len(subgoal_words), len(summary_words))
        score += overlap_ratio * 2.0

    # 3. Confidence (proven lessons rank higher)
    score += lesson.confidence * 1.0

    # 4. Occurrence count (more validated = more trustworthy), log-scaled
    import math
    score += math.log1p(lesson.occurrences) * 0.5

    # 5. Recency bonus (seen recently = more likely still valid)
    days_since_seen = (now - lesson.last_seen).days
    if days_since_seen <= 7:
        score += 1.0
    elif days_since_seen <= 30:
        score += 0.5

    # 6. Staleness penalty for ui_mapping type (most fragile to app updates)
    if lesson.type == "ui_mapping" and days_since_seen > 14:
        score -= 1.5

    # 7. ui_mapping lessons always get a base boost (universally useful when on-screen)
    if lesson.type == "ui_mapping":
        score += 1.0

    return score
```

**Why load-all-and-rank instead of category matching**: The old keyword-based category matching was fragile — "Open Alice's conversation" doesn't contain the word "navigation" or "messaging". Loading all lessons for an app (typically <50, <10KB) and scoring them against the current screen + subgoal is more robust and still fast (sub-millisecond for 50 lessons).

### Format Injected into Cortex

Appended as a new section in `cortex.md`. The prompt is phased to minimize risk:

**Phase 1 (read-only injection — no feedback reporting):**

```markdown
{% if active_lessons %}

---

## Lessons Learned ({{ focused_app }})

The following lessons were recorded from previous sessions with this app. Use them to avoid known pitfalls and prefer proven strategies.

{{ active_lessons }}

{% endif %}
```

**Phase 2 (adds feedback reporting, once Phase 1 is validated):**

```markdown
{% if active_lessons %}

---

## Lessons Learned ({{ focused_app }})

The following lessons were recorded from previous sessions with this app. Use them to avoid known pitfalls and prefer proven strategies.

If you follow a lesson's suggested strategy, you may optionally include `applied_lesson: "<lesson_id>"` in your `decisions_reason`. If a lesson's strategy does not work, include `lesson_failed: "<lesson_id>"` so it can be updated.

{{ active_lessons }}

{% endif %}
```

**Why phased**: Adding feedback reporting (applied_lesson/lesson_failed) gives the Cortex a secondary task that can split attention and degrade decision quality. Phase 1 starts read-only — the Cortex sees lessons but doesn't have to report back. Phase 2 adds optional (not mandatory) reporting after Phase 1 is validated as stable. The `focused_app` variable is passed via `focused_app=state.focused_app_info` in the template render call in `cortex.py`.

The `active_lessons` text is pre-formatted by the loader as a compact bulleted list:

```
**Mistakes to avoid:**
- Tapping 'Chats' tab when already selected does nothing. Use search icon instead. [nav-001, confidence: 0.83, seen 3x]
- Long-pressing a message opens forward menu, not copy. Use tap > Copy option instead. [msg-003, confidence: 0.75, seen 2x]

**Proven strategies:**
- Use search bar (magnifying glass icon, top-right) to find contacts — faster than scrolling. [nav-002, confidence: 0.91, seen 8x]
- To send an image: tap attachment icon (paperclip) > Gallery > select photo > tap send. [media-001, confidence: 0.88, seen 5x]

**UI mappings:**
- "Send button" = green circle arrow, resource_id: com.whatsapp:id/send, bottom-right of message input [ui-001]
- "Search" = magnifying glass, resource_id: com.whatsapp:id/menuitem_search, top-right toolbar [ui-002]
```

### Token Budget

Cap the injected lessons at **~500 tokens** (~375 words). This is small relative to the Cortex prompt (~2-3k tokens) and screenshot/hierarchy data. Selection uses the relevance score from `score_lesson()` — lessons are sorted by score descending and included until the token budget is exhausted.

If lessons exceed the budget, lower-scored lessons are dropped.

---

## Recording Lessons (Writing)

### When to Record

Recording happens **after tool execution** when specific patterns are detected. This is done in a post-processing step, not by the LLM — keeping it deterministic and free.

**Trigger conditions:**

| Pattern | Lesson Type | Detection Point | Phase |
|---------|-------------|-----------------|-------|
| Tool returns `status="error"` | `mistake` | Contextor checks `last_tool_status == "error"` (set by `post_executor_tools` node) | 1 |
| Tap succeeded but `screen_changed is False` | `mistake` | Contextor checks `screen_changed is False` and `last_tool_name == "tap"` | 1 |
| Subgoal failed and replanning triggered | `mistake` | Planner detects `needs_replan` and records before planning | 1 |
| Cortex reports `applied_lesson` in decisions_reason | update `applied_success` | Post-Cortex regex parse in `CortexNode.__call__()` | 2 |
| Cortex reports `lesson_failed` in decisions_reason | update `applied_failure`, set `deprecated` if threshold met | Post-Cortex regex parse in `CortexNode.__call__()` | 2 |
| Heuristic: subgoal succeeds after prior failure on same screen | `strategy` | Contextor detects screen change after stuck state | 3 |

### Screen Change Detection (Moved to Contextor)

The "tap succeeded but nothing changed" pattern is detected **deterministically in the Contextor**, not by the LLM. This keeps recording free and reliable.

**All screen change detection is gated behind `self.ctx.lessons_dir is not None`** — when lessons are disabled, there is zero performance overhead.

```python
# In Contextor, after capturing new screenshot:
# ONLY runs when self.ctx.lessons_dir is not None

import imagehash
from PIL import Image
import io, base64

def compute_screenshot_hash(screenshot_b64: str) -> str:
    """Compute perceptual hash of screenshot for similarity comparison.
    Uses average_hash (ahash) instead of phash — 2-3x faster, sufficient for screen-level comparison."""
    img = Image.open(io.BytesIO(base64.b64decode(screenshot_b64)))
    return str(imagehash.average_hash(img))

def detect_screen_change(
    previous_hash: str | None,
    current_hash: str | None,
    previous_b64_len: int,
    current_b64_len: int,
    threshold: int = 8,
) -> bool | None:
    """Return True if screen changed, False if unchanged, None if unknown.

    Uses a two-tier approach:
    1. Fast pre-filter: if base64 lengths differ by >5%, screen definitely changed (skip expensive hash)
    2. Perceptual hash comparison for similar-length screenshots
    """
    if previous_hash is None or current_hash is None:
        return None  # No prior screen — unknown (not True, to avoid false assumptions)

    # Fast pre-filter: different screenshot sizes = definitely changed
    if previous_b64_len > 0 and abs(current_b64_len - previous_b64_len) / previous_b64_len > 0.05:
        return True

    # Perceptual hash comparison
    distance = imagehash.hex_to_hash(previous_hash) - imagehash.hex_to_hash(current_hash)
    return distance > threshold
```

**Performance**: The base64 length pre-filter is O(1) and eliminates ~60-70% of cycles where the screen clearly changed (navigation, app switch). For the remaining ~30%, `average_hash` runs in ~3-5ms vs phash's ~10-15ms.

The Contextor stores `screen_changed` and `previous_screenshot_hash` in state. When the recorder sees `screen_changed is False` (explicit False, not None) after a successful tap tool, it records a `mistake` lesson. The `last_tool_name` and `last_tool_status` are set by the `post_executor_tools` node (see below).

### How: Append-Only JSONL Write (Concurrent-Safe)

```python
# New module: mineru/ui_auto/lessons/recorder.py

import aiofiles
import json

async def record_lesson(
    lessons_dir: Path,
    app_package: str,
    lesson: LessonEntry,
) -> None:
    """Append a lesson to the app's JSONL file. Concurrent-safe (append-only)."""
    app_dir = lessons_dir / app_package
    app_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = app_dir / "lessons.jsonl"

    # Append single line — no read-modify-write, no lock needed
    async with aiofiles.open(jsonl_path, mode="a") as f:
        await f.write(json.dumps(lesson.model_dump(), default=str) + "\n")

    # Update _index.json (lightweight, rare — only on first lesson for new app)
    await _update_index_if_needed(lessons_dir, app_package)
```

**Deduplication happens on read, not write.** When loading lessons, the loader compacts duplicates using a two-tier key: **ID match first, then summary match**. This prevents feedback updates (which rewrite the full lesson with the same ID) from being treated as separate entries.

```python
async def load_and_compact_lessons(jsonl_path: Path) -> list[LessonEntry]:
    """Read all lessons, merge duplicates, return compacted list.

    Dedup strategy (two-tier):
    1. If two entries share the same `id`, merge them (handles feedback updates).
    2. If two entries have different IDs but near-identical summaries, merge them
       (handles independently-recorded duplicates). The newer ID is kept.
    """
    raw_lessons = []
    async with aiofiles.open(jsonl_path, mode="r") as f:
        async for line in f:
            line = line.strip()
            if line:
                raw_lessons.append(LessonEntry(**json.loads(line)))

    # Pass 1: Merge by ID (exact match — handles feedback updates)
    by_id: dict[str, LessonEntry] = {}
    for lesson in raw_lessons:
        if lesson.id in by_id:
            _merge_into(by_id[lesson.id], lesson)
        else:
            by_id[lesson.id] = lesson.model_copy()

    # Pass 2: Merge by normalized summary (handles independent duplicates)
    by_summary: dict[str, LessonEntry] = {}
    for lesson in by_id.values():
        key = _normalize_summary(lesson.summary)
        if key in by_summary:
            _merge_into(by_summary[key], lesson)
        else:
            by_summary[key] = lesson

    compacted = list(by_summary.values())

    # If file grew significantly, rewrite compacted version
    if len(raw_lessons) > len(compacted) * 1.5 and len(raw_lessons) > 20:
        await _rewrite_compacted(jsonl_path, compacted)

    return compacted


def _merge_into(existing: LessonEntry, incoming: LessonEntry) -> None:
    """Merge incoming lesson data into existing. Mutates existing in place."""
    existing.occurrences += incoming.occurrences
    existing.applied_success += incoming.applied_success
    existing.applied_failure += incoming.applied_failure
    existing.last_seen = max(existing.last_seen, incoming.last_seen)
    # Keep the newer ID (from feedback updates or later recordings)
    if incoming.last_seen >= existing.last_seen:
        existing.id = incoming.id
    # Recompute confidence
    total = existing.applied_success + existing.applied_failure
    existing.confidence = existing.applied_success / total if total > 0 else 0.5
    # Deprecation is sticky — once deprecated, stays deprecated unless manually cleared
    if incoming.deprecated:
        existing.deprecated = True


def _normalize_summary(summary: str) -> str:
    """Normalize summary for dedup matching."""
    return " ".join(summary.lower().split())
```

### Screen Signature Capture

When recording a lesson, capture a structured screen fingerprint from the current UI state:

```python
def capture_screen_signature(
    focused_app_info: str | None,
    ui_hierarchy: list[dict] | None,
) -> ScreenSignature:
    """Extract activity name and top-level visible text elements from UI hierarchy."""
    activity = None
    key_elements = []

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
```

### Automatic Category Assignment

Derive from the current subgoal description + the tool that triggered it. Category is used for write-time classification and display grouping only — **not** for read-time filtering (see Loading section).

```python
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
```

---

## Staleness & Confidence Decay

### The Problem with Stale Lessons

App UIs change with updates. A `ui_mapping` lesson pointing to `resource_id: com.whatsapp:id/send` may become invalid after a WhatsApp update. Stale lessons are worse than no lessons — they cause the agent to confidently do the wrong thing.

### Staleness Rules (Applied at Load Time)

| Lesson Type | Age Threshold | Action |
|-------------|--------------|--------|
| `ui_mapping` | `last_seen` > 14 days | Priority penalty: score -= 1.5 (pushed below token budget cutoff) |
| `ui_mapping` | `last_seen` > 30 days | Excluded from injection entirely |
| `mistake` / `strategy` | `last_seen` > 60 days | Priority penalty: score -= 1.0 |
| `mistake` / `strategy` | `last_seen` > 90 days | Excluded from injection entirely |
| Any | `deprecated == true` | Excluded from injection entirely |

### Confidence Decay (Negative Feedback Loop)

Confidence is **computed, not static**. It reflects how reliable a lesson's `suggested_strategy` is:

```python
def compute_confidence(applied_success: int, applied_failure: int) -> float:
    """Bayesian-ish confidence with a prior of 0.5."""
    if applied_success + applied_failure == 0:
        return 0.5  # No data — neutral prior
    return applied_success / (applied_success + applied_failure)
```

**How the feedback loop works:**

1. Cortex sees a lesson and follows its `suggested_strategy`
2. Cortex includes `applied_lesson: "nav-002"` in its `decisions_reason`
3. Post-Cortex hook parses this and stores the lesson ID in state
4. After the next Contextor cycle:
   - If `screen_changed == True` and subgoal progressed → `applied_success += 1`
   - If `screen_changed == False` or tool errored → `applied_failure += 1`
5. If `applied_failure >= 3` and `confidence < 0.3` → set `deprecated = true`

**Deprecation is soft-delete**: the lesson stays in the JSONL file (for auditability) but is excluded from future injection. It can be un-deprecated manually by editing the file or automatically if a future occurrence resets the counters.

---

## Scratchpad vs. Lessons: Boundary

The codebase already has a `scratchpad` tool (save_note/read_note/list_notes) for within-session memory. The boundary is:

| | Scratchpad | Lessons |
|---|-----------|---------|
| **Scope** | Within-session | Cross-session |
| **Control** | Agent-initiated (explicit tool calls) | System-initiated (automatic detection) |
| **Lifetime** | Dies with the session | Persists on disk |
| **Content** | Ephemeral observations ("Alice's chat is 3rd from top") | Reusable knowledge ("search is faster than scrolling") |
| **Storage** | In-memory state dict | JSONL files on disk |

These two systems are complementary and do not overlap. The scratchpad is the agent's working memory; lessons are its long-term memory.

---

## Integration Reference

This section provides the missing glue code — the orchestrating functions, state coordination, and utility implementations that connect the individual components described above.

### `load_lessons_for_app()` — The Top-Level Loader

This is the single function the Contextor calls. It orchestrates the full pipeline:

```python
# mineru/ui_auto/lessons/loader.py

from datetime import datetime, timezone
from pathlib import Path

from mineru.ui_auto.lessons.types import LessonEntry, ScreenSignature
from mineru.ui_auto.lessons.scorer import score_lesson

# Staleness hard-cutoffs (excluded entirely, not just penalized)
STALE_UI_MAPPING_DAYS = 30
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

    now = datetime.now(timezone.utc)

    # Step 2: Hard-filter stale and deprecated lessons
    eligible = []
    for lesson in lessons:
        if lesson.deprecated:
            continue
        days_old = (now - lesson.last_seen).days
        if lesson.type == "ui_mapping" and days_old > STALE_UI_MAPPING_DAYS:
            continue
        if lesson.type != "ui_mapping" and days_old > STALE_OTHER_DAYS:
            continue
        eligible.append(lesson)

    if not eligible:
        return None

    # Step 3: Score and rank
    scored = [
        (score_lesson(l, current_activity, current_key_elements, subgoal, now), l)
        for l in eligible
    ]
    scored.sort(key=lambda x: x[0], reverse=True)

    # Step 4: Format within token budget
    return format_lessons_text(scored)


def format_lessons_text(scored_lessons: list[tuple[float, LessonEntry]]) -> str | None:
    """
    Format scored lessons into grouped bulleted text within TOKEN_BUDGET.
    Groups: mistakes → strategies → ui_mappings.
    Returns None if no lessons fit the budget.
    """
    groups: dict[str, list[str]] = {
        "mistake": [],
        "strategy": [],
        "ui_mapping": [],
    }
    running_chars = 0
    max_chars = TOKEN_BUDGET * APPROX_CHARS_PER_TOKEN  # ~2000 chars

    for _score, lesson in scored_lessons:
        bullet = _format_bullet(lesson)
        bullet_chars = len(bullet) + 2  # "- " prefix
        if running_chars + bullet_chars > max_chars:
            break  # Budget exhausted
        groups.setdefault(lesson.type, []).append(bullet)
        running_chars += bullet_chars

    # Build output with group headers
    sections = []
    if groups.get("mistake"):
        sections.append("**Mistakes to avoid:**\n" + "\n".join(f"- {b}" for b in groups["mistake"]))
    if groups.get("strategy"):
        sections.append("**Proven strategies:**\n" + "\n".join(f"- {b}" for b in groups["strategy"]))
    if groups.get("ui_mapping"):
        sections.append("**UI mappings:**\n" + "\n".join(f"- {b}" for b in groups["ui_mapping"]))

    if not sections:
        return None
    return "\n\n".join(sections)


def _format_bullet(lesson: LessonEntry) -> str:
    """Format a single lesson as a compact one-line bullet."""
    meta = f"[{lesson.id}, confidence: {lesson.confidence:.2f}, seen {lesson.occurrences}x]"
    if lesson.type == "ui_mapping":
        return f"{lesson.lesson} {meta}"
    return f"{lesson.summary}. {lesson.suggested_strategy} {meta}"
```

### State Fields for Cross-Node Coordination

State fields are introduced in phases to minimize risk. Phase 1 fields enable read-only lesson loading and screen change detection. Phase 2 fields enable the confidence feedback loop.

**Phase 1 state fields:**

```python
# In State (graph/state.py) — add alongside existing fields

# Lesson loading (set by Contextor, consumed by Cortex)
active_lessons: Annotated[str | None, "Formatted lesson text for Cortex prompt", take_last] = None

# Screen change detection (set by Contextor, consumed by recorder)
screen_changed: Annotated[bool | None, "Whether screen changed since last cycle. None = unknown", take_last] = None
previous_screenshot_hash: Annotated[str | None, "Perceptual hash of prior screenshot", take_last] = None

# Last tool tracking (set by post_executor_tools node, consumed by Contextor for no-effect detection)
last_tool_name: Annotated[str | None, "Name of the last executed tool", take_last] = None
last_tool_status: Annotated[str | None, "Status of last tool: 'success' or 'error'", take_last] = None
```

**Phase 2 state fields (added only after Phase 1 is validated):**

```python
# Lesson feedback (set by Cortex, consumed by next Contextor cycle's recorder)
applied_lesson_ids: Annotated[list[str], "Lesson IDs the Cortex followed this turn", take_last] = []
failed_lesson_ids: Annotated[list[str], "Lesson IDs whose strategies failed this turn", take_last] = []
```

### Post-Executor-Tools Node: Tracking Last Tool

**Problem with the previous approach**: The `ExecutorToolNode` extends LangGraph's `ToolNode` and has no `ctx`. Its `__init__` only takes `tools`, `messages_key`, and `trace_id`. It does not control its own state output — individual tools return `Command(update=...)` which `_combine_tool_outputs()` passes through. Modifying `tool_node.py` to inject state fields is architecturally unsafe.

**Solution**: Add a lightweight `post_executor_tools` node that extracts the last tool's name and status from `executor_messages`. This requires **zero changes to `tool_node.py` or any tool file**.

```python
# In graph/graph.py — new function

from langchain_core.messages import ToolMessage

def post_executor_tools_node(state: State):
    """Extract last tool name and status from executor_messages for lesson recording."""
    tool_messages = [m for m in state.executor_messages if isinstance(m, ToolMessage)]
    if not tool_messages:
        return {}
    last_msg = tool_messages[-1]
    return {
        "last_tool_name": getattr(last_msg, "name", None),
        "last_tool_status": getattr(last_msg, "status", None),
    }
```

**Graph wiring change** — the only topology modification in the entire design:

```python
# In graph/graph.py — replace:
#   graph_builder.add_edge("executor_tools", "summarizer")
# with:
graph_builder.add_node("post_executor_tools", post_executor_tools_node)
graph_builder.add_edge("executor_tools", "post_executor_tools")
graph_builder.add_edge("post_executor_tools", "summarizer")
```

This inserts one node on a linear edge (`executor_tools → summarizer`). No conditional edges change. The `post_executor_gate`'s "skip" path still goes directly to `summarizer`, which is correct — if no tools ran, there's nothing to extract.

**Stale value prevention**: The Cortex must reset `last_tool_name` and `last_tool_status` to `None` in its return update, so they are only non-None when tools actually ran in the current cycle:

```python
# In cortex.py, add to the return update dict (alongside existing "focused_app_info": None, etc.):
"last_tool_name": None,
"last_tool_status": None,
```

### Cortex Output Parsing for Lesson Feedback (Phase 2 Only)

**This section is Phase 2** — do NOT implement until Phase 1 (read-only lesson injection) is validated as stable.

The Cortex reports which lessons it followed or found unhelpful via free-text patterns in `decisions_reason`. Parsing happens in `CortexNode.__call__()`, after the LLM response:

```python
# In cortex.py, after line 127 (agent_thought = "\n\n".join(thought_parts))
# PHASE 2 ONLY — do not add in Phase 1

import re

applied_ids = []
failed_ids = []
if response.decisions_reason:
    # Match: applied_lesson: "nav-002" or applied_lesson: "ui-005"
    applied_ids = re.findall(r'applied_lesson:\s*"([^"]+)"', response.decisions_reason)
    # Match: lesson_failed: "nav-002" or lesson_failed: "ui-005"
    failed_ids = re.findall(r'lesson_failed:\s*"([^"]+)"', response.decisions_reason)

# Include in the return update dict:
# "applied_lesson_ids": applied_ids,
# "failed_lesson_ids": failed_ids,
```

### Contextor: No-Effect Detection and Lesson Recording

The Contextor ties together screen change detection with the tool status from the `post_executor_tools` node:

```python
# In contextor.py __call__(), after screen change detection and before the return:

from mineru.ui_auto.lessons.recorder import record_no_effect_mistake, record_mistake_from_tool_failure

# Record "tap with no effect" mistake
# NOTE: uses `screen_changed is False` (explicit False), not `not screen_changed`
# This prevents triggering on None (unknown/first cycle/resumed)
if (
    self.ctx.lessons_dir
    and screen_changed is False
    and state.last_tool_name == "tap"
    and state.last_tool_status == "success"
    and current_app_package
):
    screen_sig = capture_screen_signature(current_app_package, device_data.elements)
    current_subgoal = get_current_subgoal(state.subgoal_plan)
    await record_no_effect_mistake(
        lessons_dir=self.ctx.lessons_dir,
        app_package=current_app_package,
        screen_signature=screen_sig,
        subgoal=current_subgoal.description if current_subgoal else "",
    )
```

**Phase 2 addition** — lesson feedback processing (add only after Phase 1 is validated):

```python
# PHASE 2 ONLY — Process lesson feedback from previous Cortex cycle
if self.ctx.lessons_dir and current_app_package:
    if state.applied_lesson_ids or state.failed_lesson_ids:
        await update_lesson_feedback(
            lessons_dir=self.ctx.lessons_dir,
            app_package=current_app_package,
            applied_ids=state.applied_lesson_ids,
            failed_ids=state.failed_lesson_ids,
            screen_changed=screen_changed,
            tool_status=state.last_tool_status,
        )
```

### Lesson ID Generation

IDs are `{category_prefix}-{timestamp_hash}`, guaranteed unique per append:

```python
# In mineru/ui_auto/lessons/recorder.py

import hashlib
import time

def generate_lesson_id(category: str) -> str:
    """Generate a short, unique lesson ID. Format: nav-a3f1, msg-b2c4, etc."""
    prefix = category[:3]  # nav, msg, sea, med, set, gen
    hash_input = f"{time.time_ns()}"
    short_hash = hashlib.md5(hash_input.encode()).hexdigest()[:4]
    return f"{prefix}-{short_hash}"
```

### `update_lesson_feedback()` — Processing Applied/Failed Reports

```python
# In mineru/ui_auto/lessons/recorder.py

async def update_lesson_feedback(
    lessons_dir: Path,
    app_package: str,
    applied_ids: list[str],
    failed_ids: list[str],
    screen_changed: bool,
    tool_status: str | None,
) -> None:
    """
    Update confidence counters for lessons the Cortex reported using.
    Appends update entries to JSONL — compaction merges them with originals on next read.
    """
    jsonl_path = lessons_dir / app_package / "lessons.jsonl"
    if not jsonl_path.exists():
        return

    lessons = await load_and_compact_lessons(jsonl_path)
    lessons_by_id = {l.id: l for l in lessons}

    updates = []
    for lid in applied_ids:
        if lid in lessons_by_id:
            lesson = lessons_by_id[lid]
            # Strategy was followed — did it work?
            if screen_changed and tool_status == "success":
                lesson.applied_success += 1
            else:
                lesson.applied_failure += 1
            lesson.confidence = compute_confidence(lesson.applied_success, lesson.applied_failure)
            lesson.last_seen = datetime.now(timezone.utc)
            updates.append(lesson)

    for lid in failed_ids:
        if lid in lessons_by_id:
            lesson = lessons_by_id[lid]
            lesson.applied_failure += 1
            lesson.confidence = compute_confidence(lesson.applied_success, lesson.applied_failure)
            if lesson.applied_failure >= 3 and lesson.confidence < 0.3:
                lesson.deprecated = True
            lesson.last_seen = datetime.now(timezone.utc)
            updates.append(lesson)

    # Append updated entries — compaction on next read will merge with originals
    if updates:
        async with aiofiles.open(jsonl_path, mode="a") as f:
            for lesson in updates:
                await f.write(json.dumps(lesson.model_dump(), default=str) + "\n")
```

### `_update_index_if_needed()` and `_rewrite_compacted()` — Utility Implementations

```python
# In mineru/ui_auto/lessons/recorder.py

import tempfile

async def _update_index_if_needed(lessons_dir: Path, app_package: str) -> None:
    """Add app to _index.json if not already present. Reads full index, adds entry, writes back."""
    index_path = lessons_dir / "_index.json"
    index_data = {"apps": {}}
    if index_path.exists():
        async with aiofiles.open(index_path, mode="r") as f:
            content = await f.read()
            if content.strip():
                index_data = json.loads(content)

    if app_package in index_data.get("apps", {}):
        return  # Already registered — skip (lesson_count updates happen during compaction)

    index_data.setdefault("apps", {})[app_package] = {
        "display_name": app_package.split(".")[-1].title(),
        "lesson_count": 1,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    await _atomic_write_json(index_path, index_data)


async def _rewrite_compacted(jsonl_path: Path, compacted: list[LessonEntry]) -> None:
    """Atomically rewrite a JSONL file with compacted entries (write-to-temp, then rename)."""
    # Write to temp file in same directory (ensures same filesystem for atomic rename)
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
```

### `record_no_effect_mistake()` — Specific Recorder for Tap-No-Effect

Uses a **session-local counter** to avoid false positives. Some taps legitimately don't change the screen (e.g., tapping to focus a text field, tapping an already-selected tab). Only the 2nd+ occurrence on the same screen within a session triggers a JSONL write.

```python
# In mineru/ui_auto/lessons/recorder.py

# Session-local counter — resets each agent run, not persisted to state or disk.
# This is correct: we want per-session dedup, cross-session dedup happens via JSONL compaction.
_tap_no_effect_counts: dict[str, int] = {}

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
        lesson="The tapped element may already be selected, disabled, or non-interactive. Try an alternative approach.",
        suggested_strategy="Check element state (selected, enabled, clickable) in UI hierarchy before tapping. Consider using search or swipe instead.",
        confidence=0.5,
        occurrences=1,
        applied_success=0,
        applied_failure=0,
        created=datetime.now(timezone.utc),
        last_seen=datetime.now(timezone.utc),
        deprecated=False,
    )
    await record_lesson(lessons_dir, app_package, lesson)
```

### `record_mistake_from_tool_failure()` — Recorder for Tool Errors

Called by `ExecutorToolNode` when a tool returns `status="error"`. The tool node has access to the tool name, error message, and current state (which contains screen signature data from the last Contextor cycle).

```python
# In mineru/ui_auto/lessons/recorder.py

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
        lesson=f"The '{tool_name}' tool failed in this context. The element may not exist, be off-screen, or have changed.",
        suggested_strategy=f"Verify the target element exists in the UI hierarchy before calling {tool_name}. Consider alternative approaches.",
        confidence=0.5,
        occurrences=1,
        applied_success=0,
        applied_failure=0,
        created=datetime.now(timezone.utc),
        last_seen=datetime.now(timezone.utc),
        deprecated=False,
    )
    await record_lesson(lessons_dir, app_package, lesson)


def _truncate(text: str, max_len: int) -> str:
    """Truncate text to max_len, adding ellipsis if needed."""
    return text[:max_len] + "..." if len(text) > max_len else text
```

**Tool failure lesson recording** — Since `ExecutorToolNode` has no `ctx` (it only receives `tools`, `messages_key`, `trace_id`), tool failure lessons are recorded in the **Contextor** during the next cycle, not in the tool node. The Contextor checks `state.last_tool_status == "error"` and `state.last_tool_name` (set by the `post_executor_tools` node) and records the lesson with full screen context.

```python
# In contextor.py __call__(), after screen change detection:

# Record tool failure mistake (tool_node doesn't have ctx, so we record here)
if (
    self.ctx.lessons_dir
    and state.last_tool_status == "error"
    and state.last_tool_name
    and current_app_package
):
    # Extract error from last executor message
    from langchain_core.messages import ToolMessage
    tool_messages = [m for m in state.executor_messages if isinstance(m, ToolMessage)]
    if tool_messages:
        last_msg = tool_messages[-1]
        error_msg = str(last_msg.content) if last_msg.status == "error" else ""
        if error_msg:
            screen_sig = capture_screen_signature(current_app_package, device_data.elements)
            current_subgoal = get_current_subgoal(state.subgoal_plan)
            await record_mistake_from_tool_failure(
                lessons_dir=self.ctx.lessons_dir,
                app_package=current_app_package,
                tool_name=state.last_tool_name,
                tool_error=error_msg,
                screen_signature=screen_sig,
                subgoal=current_subgoal.description if current_subgoal else "",
            )
```

**Why Contextor instead of tool_node**: `ExecutorToolNode` extends LangGraph's `ToolNode` and has no `ctx`, no `lessons_dir`, and does not control its state output. Modifying it risks breaking the stable tool execution pipeline. The Contextor runs next in the loop and has full access to `ctx`, screen data, and the `last_tool_name`/`last_tool_status` set by the `post_executor_tools` node.

### `record_strategy()` — Strategy Recording (Phase 3 Only)

**Strategy recording is deferred to Phase 3.** Adding a `learned_strategy` field to `CortexOutput` changes the structured output schema the LLM must conform to, risking output failures with some models. No changes to `CortexOutput` or `cortex/types.py` are made in Phase 1 or Phase 2.

**Phase 3 approach — heuristic, not LLM-initiated**: Instead of asking the Cortex to report strategies (which splits its attention), strategies are detected heuristically:
- If a subgoal succeeds after a previous failure on the same screen, the successful approach (extracted from `agents_thoughts`) is a candidate strategy lesson
- This analysis runs in the Contextor when it detects screen change after a previously-stuck state

The `record_strategy()` function is kept in `recorder.py` for Phase 3 use:

```python
# In mineru/ui_auto/lessons/recorder.py — Phase 3 only

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
        created=datetime.now(timezone.utc),
        last_seen=datetime.now(timezone.utc),
        deprecated=False,
    )
    await record_lesson(lessons_dir, app_package, lesson)
```

### Subgoal Failure Recording — In Planner, Not Convergence Gate

**Problem with the previous approach**: The original design modified `convergence_node` (a plain function returning `{}`, registered with `defer=True`) to a class-based node with async I/O. This risks breaking LangGraph's defer semantics and adds latency to every loop iteration.

**Solution**: Record subgoal failure lessons in the **Planner** during replan. The Planner already:
- Detects replanning (`needs_replan = one_of_them_is_failure(state.subgoal_plan)`)
- Sees the full `subgoal_plan` with failure statuses and `completion_reason`
- Sees all `agents_thoughts` with the execution trace
- Runs only on replan (not every loop iteration)

**Zero changes to `convergence_node`, `convergence_gate`, or graph topology.**

```python
# In planner.py __call__(), after line 35 (needs_replan = one_of_them_is_failure(state.subgoal_plan)):

if needs_replan and self.ctx.lessons_dir:
    from mineru.ui_auto.lessons.recorder import record_subgoal_failure
    from mineru.ui_auto.agents.planner.types import SubgoalStatus

    failed_subgoals = [s for s in state.subgoal_plan if s.status == SubgoalStatus.FAILURE]
    for sg in failed_subgoals:
        await record_subgoal_failure(
            lessons_dir=self.ctx.lessons_dir,
            app_package=state.focused_app_info or "unknown",
            subgoal_description=sg.description,
            completion_reason=sg.completion_reason,
            cortex_last_thought=state.cortex_last_thought,
        )
```

The recorder function:

```python
# In mineru/ui_auto/lessons/recorder.py

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
        suggested_strategy="Try a different approach for this subgoal. Check if prerequisites are met before attempting.",
        confidence=0.5,
        occurrences=1,
        applied_success=0,
        applied_failure=0,
        created=datetime.now(timezone.utc),
        last_seen=datetime.now(timezone.utc),
        deprecated=False,
    )
    await record_lesson(lessons_dir, app_package, lesson)
```

### New Dependencies

The following packages must be added to `pyproject.toml` dependencies:

```toml
# In pyproject.toml [project.dependencies] or [tool.uv.dependencies]:
"imagehash>=4.3.1",    # Perceptual hashing for screen change detection
"Pillow>=10.0.0",      # Image processing (required by imagehash)
"aiofiles>=23.0.0",    # Async file I/O for non-blocking JSONL reads/writes
```

`Pillow` may already be a transitive dependency (check existing deps), but it should be listed explicitly since we directly import from it.

### Complete State Fields (Final List)

All new state fields in one place, organized by implementation phase:

```python
# In State (graph/state.py)

# ============================================================
# PHASE 1: Read path + screen change detection
# ============================================================

# Lesson loading (set by Contextor, consumed by Cortex)
active_lessons: Annotated[str | None, "Formatted lesson text for Cortex prompt", take_last] = None

# Screen change detection (set by Contextor, consumed by recorder)
screen_changed: Annotated[bool | None, "Whether screen changed. None = unknown (first cycle/resumed)", take_last] = None
previous_screenshot_hash: Annotated[str | None, "Perceptual hash of prior screenshot", take_last] = None

# Last tool tracking (set by post_executor_tools node, reset by Cortex)
last_tool_name: Annotated[str | None, "Name of the last executed tool", take_last] = None
last_tool_status: Annotated[str | None, "Status of last tool: 'success' or 'error'", take_last] = None

# ============================================================
# PHASE 2: Confidence feedback loop (add after Phase 1 validated)
# ============================================================

# Lesson feedback (set by Cortex, consumed by next Contextor cycle's recorder)
applied_lesson_ids: Annotated[list[str], "Lesson IDs the Cortex followed this turn", take_last] = []
failed_lesson_ids: Annotated[list[str], "Lesson IDs whose strategies failed this turn", take_last] = []
```

**Removed from previous design**: `last_screen_signature` (unnecessary — Contextor captures screen signature fresh each cycle), `pending_strategy` (strategy recording deferred to Phase 3 heuristic approach), `recorded_failure_subgoal_ids` (subgoal failure recording moved to Planner, which naturally deduplicates via replan flow).

### Pydantic Models (types.py)

```python
# mineru/ui_auto/lessons/types.py

from datetime import datetime
from pydantic import BaseModel

class ScreenSignature(BaseModel):
    activity: str | None = None
    key_elements: list[str] = []

class LessonContext(BaseModel):
    goal: str = ""
    screen_signature: ScreenSignature = ScreenSignature()
    action_attempted: str = ""
    what_happened: str = ""

class LessonEntry(BaseModel):
    id: str
    type: str  # "mistake" | "strategy" | "ui_mapping"
    category: str  # "navigation" | "messaging" | "search" | "media" | "settings" | "general"
    summary: str
    context: LessonContext = LessonContext()
    lesson: str = ""
    suggested_strategy: str = ""
    confidence: float = 0.5
    occurrences: int = 1
    applied_success: int = 0
    applied_failure: int = 0
    created: datetime = datetime.now()
    last_seen: datetime = datetime.now()
    deprecated: bool = False

class AppIndexEntry(BaseModel):
    display_name: str
    lesson_count: int
    last_updated: str

class AppIndex(BaseModel):
    apps: dict[str, AppIndexEntry] = {}
```

---

## Implementation Plan

### Phase 1: Read Path + Screen Change Detection + Basic Write Path

**Zero changes to**: `tool_node.py`, `cortex/types.py` (CortexOutput), `convergence_node`, `convergence_gate`, any tool file in `tools/mobile/`

**New dependencies** (add to `pyproject.toml`):
- `imagehash>=4.3.1`, `Pillow>=10.0.0`, `aiofiles>=23.0.0`

**Files to create:**

1. **`mineru/ui_auto/lessons/__init__.py`** — New module (empty init)
2. **`mineru/ui_auto/lessons/types.py`** — `LessonEntry`, `LessonContext`, `ScreenSignature`, `AppIndexEntry`, `AppIndex` Pydantic models
3. **`mineru/ui_auto/lessons/scorer.py`** — `score_lesson()` relevance scoring
4. **`mineru/ui_auto/lessons/loader.py`** — `load_lessons_for_app()`, `load_and_compact_lessons()`, `format_lessons_text()`, `_format_bullet()`, `_normalize_summary()`, `_merge_into()`
5. **`mineru/ui_auto/lessons/recorder.py`** — `record_lesson()`, `record_no_effect_mistake()` (with session-local dedup counter), `record_mistake_from_tool_failure()`, `record_subgoal_failure()`, `generate_lesson_id()`, `infer_category()`, `capture_screen_signature()`, `_truncate()`, `_update_index_if_needed()`, `_rewrite_compacted()`, `_atomic_write_json()`

**Files to modify:**

6. **`mineru/ui_auto/graph/state.py`** — Add Phase 1 fields: `active_lessons`, `screen_changed` (bool|None, default None), `previous_screenshot_hash`, `last_tool_name`, `last_tool_status`
7. **`mineru/ui_auto/context.py`** — Add `lessons_dir: Path | None = None` to `MobileUseContext`
8. **`mineru/ui_auto/graph/graph.py`** — Add `post_executor_tools_node` function; add node and rewire edge: `executor_tools → post_executor_tools → summarizer`
9. **`mineru/ui_auto/agents/contextor/contextor.py`** — Add `compute_screenshot_hash()` (ahash, gated behind lessons_dir), `detect_screen_change()` (with base64 length pre-filter); call `load_lessons_for_app()`; call `record_no_effect_mistake()` when `screen_changed is False`; call `record_mistake_from_tool_failure()` when `last_tool_status == "error"`
10. **`mineru/ui_auto/agents/cortex/cortex.md`** — Add Phase 1 `{% if active_lessons %}` section (read-only, NO feedback instructions)
11. **`mineru/ui_auto/agents/cortex/cortex.py`** — Pass `focused_app=state.focused_app_info` and `active_lessons=state.active_lessons` to template render; reset `last_tool_name` and `last_tool_status` to `None` in return update
12. **`mineru/ui_auto/agents/planner/planner.py`** — Record subgoal failure lessons on replan (gated behind `self.ctx.lessons_dir`)

### Phase 2: Confidence Feedback Loop

**Prerequisite**: Phase 1 validated as stable with no performance regression.

**Files to modify:**

1. **`mineru/ui_auto/graph/state.py`** — Add Phase 2 fields: `applied_lesson_ids`, `failed_lesson_ids`
2. **`mineru/ui_auto/agents/cortex/cortex.md`** — Replace Phase 1 lessons block with Phase 2 version (adds optional `applied_lesson`/`lesson_failed` reporting)
3. **`mineru/ui_auto/agents/cortex/cortex.py`** — Add regex parsing of `applied_lesson`/`lesson_failed` from `decisions_reason`; add `applied_lesson_ids`, `failed_lesson_ids` to state update
4. **`mineru/ui_auto/agents/contextor/contextor.py`** — Add `update_lesson_feedback()` call when `applied_lesson_ids` or `failed_lesson_ids` are present
5. **`mineru/ui_auto/lessons/recorder.py`** — Add `update_lesson_feedback()`

### Phase 3: Strategy Recording + Version Tracking + Compaction

- Heuristic strategy detection in Contextor (no CortexOutput changes, no `learned_strategy` field)
- `record_strategy()` called by heuristic detector, not LLM output
- Attempt to extract app version from UIAutomator dump when `launch_app` is called
- Store in `_meta.json` per app
- Tag lessons with the version they were recorded on
- Auto-compact JSONL files when read detects >1.5x bloat from duplicates
- Background cleanup job: prune entries exceeding staleness thresholds

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **File-based, not DB** | Zero infrastructure, works offline, easy to inspect/edit/version-control |
| **JSONL instead of JSON arrays** | Append-only writes are concurrent-safe without file locking. Dedup on read is simple and infrequent. |
| **Load-all-and-rank instead of category matching** | Category keyword matching was fragile ("Open Alice's conversation" doesn't match "navigation"). Loading all lessons per app (<50 entries, <10KB) and scoring by screen signature + subgoal overlap is more robust and still sub-millisecond. |
| **Screen signature fingerprinting** | Activity name + top-level visible elements give a structured context for lesson relevance. Lessons about the Chats tab only surface when the agent is on the Chats screen. |
| **Screen change detection in Contextor** | "Tap with no effect" is the most common mistake pattern. Detecting it via perceptual hash comparison in the Contextor (not the LLM) keeps recording deterministic and free. |
| **Confidence = applied_success / (applied_success + applied_failure)** | Creates a negative feedback loop — lessons that don't work lose confidence and get deprioritized or deprecated. Prevents the system from confidently recommending broken strategies. |
| **Soft deprecation, not deletion** | `deprecated: true` excludes from injection but keeps data for auditability. Can be reversed if conditions change. |
| **Inject at Cortex, not Planner** | Cortex makes per-step decisions with current screen context — lessons are most actionable here. Planner works at a higher level. |
| **500-token cap** | Keeps lesson injection small relative to the rest of the Cortex context (screenshot + hierarchy). Avoids attention dilution. |
| **Record deterministically, not via LLM** | Tool failures and screen-unchanged patterns are machine-detectable. No extra LLM call needed for recording. |
| **Dedup on read, not write** | Write path stays simple (append one line). Read path compacts when it detects bloat. No risk of data loss from interrupted writes. |
| **Lessons dir outside the package** | `lessons/` at project root (configurable via `MobileUseContext.lessons_dir`) — not inside `mineru/ui_auto/` — so lessons persist across package updates and can be shared. |
| **post_executor_tools node instead of modifying tool_node.py** | ExecutorToolNode has no `ctx` and extends LangGraph's ToolNode — modifying it risks breaking the stable tool pipeline. A separate node reads `executor_messages` to extract tool name/status with zero changes to existing tool code. |
| **Subgoal failure in Planner, not convergence_node** | convergence_node is a plain function with `defer=True` — converting to class-based with async I/O risks LangGraph defer semantics. Planner already runs on replan with full state access. |
| **Phase 1 read-only, Phase 2 feedback** | Adding reporting obligations (applied_lesson/lesson_failed) to Cortex splits its attention and can degrade decision quality. Phase 1 starts read-only; Phase 2 adds optional reporting after stability is confirmed. |
| **No CortexOutput schema changes** | Adding `learned_strategy` field risks structured output failures. Strategy recording deferred to Phase 3 heuristic approach — no LLM schema changes needed. |
| **screen_changed default None, not True** | `True` default could mask real no-change after checkpoint resume. `None` means unknown; recorder only triggers on explicit `is False`. |
| **average_hash instead of phash** | 2-3x faster, sufficient for screen-level comparison. Base64 length pre-filter eliminates ~60-70% of cycles before hash computation. |
| **Session-local tap-no-effect counter** | First occurrence may be legitimate (text field focus). Only record on 2nd+ occurrence per screen per session to filter false positives. Counter resets each session (correct — cross-session dedup via JSONL compaction). |
| **All lesson code gated behind lessons_dir** | When `MobileUseContext.lessons_dir` is `None`, every lesson code path is a no-op. Zero performance impact on existing workflows. |
| **All recorder calls wrapped in try/except** | Lesson recording does file I/O inside critical graph nodes (Contextor, Planner). An unhandled exception would crash the node and break the agent loop. Every recorder call in an existing node MUST be wrapped: `try: await record_...(...)` / `except Exception as e: logger.warning(f"Lesson recording failed (non-fatal): {e}")`. Lesson recording is never worth crashing the agent. |

---

## Example End-to-End Flow

### Recording a Mistake

1. User goal: "Send 'Hello' to Alice on WhatsApp"
2. Cortex decides: tap "Chats" tab
3. Executor taps it -> tool returns `status="success"`
4. Contextor runs again: captures new screenshot, computes perceptual hash
5. `detect_screen_change()` returns `False` — screenshot is identical
6. Contextor sets `screen_changed = False` in state
7. Recorder sees: last tool was `tap`, `status="success"`, `screen_changed == False`
8. `capture_screen_signature()` extracts `activity: "com.whatsapp/.HomeActivity"`, `key_elements: ["Chats", "Status", "Calls"]`
9. `record_lesson()` appends to `lessons/com.whatsapp/lessons.jsonl`:
   ```json
   {"id": "nav-001", "type": "mistake", "category": "navigation", "summary": "Tapping 'Chats' tab has no effect when already on Chats screen", "context": {"screen_signature": {"activity": "com.whatsapp/.HomeActivity", "key_elements": ["Chats", "Status", "Calls"]}}, "lesson": "Check if already on target tab before tapping. Use search instead.", "confidence": 0.5, "occurrences": 1, "applied_success": 0, "applied_failure": 0, "deprecated": false, ...}
   ```

### Loading a Lesson

1. New session, same goal: "Message Bob on WhatsApp"
2. Contextor detects `focused_app_info = "com.whatsapp"`
3. Contextor captures screen signature: `activity: "com.whatsapp/.HomeActivity"`, `key_elements: ["Chats", "Status", "Calls"]`
4. `load_lessons_for_app()` reads `lessons/com.whatsapp/lessons.jsonl`:
   - Loads all 12 lessons, compacts duplicates -> 10 unique
   - Excludes 1 deprecated lesson, 1 stale ui_mapping -> 8 candidates
   - `score_lesson()` ranks each against current screen + subgoal "Navigate to Bob's conversation"
   - nav-001 scores high: activity matches, key_elements overlap, summary contains "Chats tab"
   - Selects top lessons within 500-token budget
5. Cortex receives the lesson in its system prompt
6. Cortex skips the Chats tab tap and goes directly to search
7. Cortex includes `applied_lesson: "nav-002"` in decisions_reason (using the search strategy)
8. Post-Cortex hook records the application for confidence tracking

### Confidence Decay Example

1. Session N: Cortex follows lesson `ui-005` ("Send button = resource_id: com.whatsapp:id/send")
2. Tap on that resource_id fails — WhatsApp updated and renamed the element
3. Cortex reports `lesson_failed: "ui-005"`
4. Recorder: `applied_failure += 1`, confidence recomputed: `3 / (3 + 1) = 0.75`
5. Session N+1: Same thing happens. Confidence: `3 / (3 + 2) = 0.60`
6. Session N+2: Fails again. Confidence: `3 / (3 + 3) = 0.50`. Plus `last_seen` staleness penalty.
7. Lesson drops below token budget cutoff — effectively self-healed out of the prompt.
8. If it reaches `applied_failure >= 3` and `confidence < 0.3` -> `deprecated = true`

---

## File Size & Cleanup

- Each lesson line is ~300 bytes. 50 lessons per app = ~15KB per JSONL file.
- `_index.json` stays under 1KB for typical usage.
- **Compaction**: When `load_and_compact_lessons()` detects raw lines > 1.5x merged count and > 20 lines, it rewrites the JSONL file with merged entries.
- **Staleness pruning**: Lessons exceeding staleness thresholds (see Staleness Rules table) are excluded at load time. During compaction, entries with `deprecated == true` AND `last_seen` > 90 days are permanently removed.
- **Max lessons per app**: 50. Beyond that, lowest-score lessons are evicted during compaction.
