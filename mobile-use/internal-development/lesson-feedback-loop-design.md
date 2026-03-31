# Lesson Feedback Loop & Human Demo-Driven Exploration

> **Status:** Draft
> **Scope:** Improve lesson-learned quality via semantic dedup, implicit reinforcement, LLM compaction, and human-demo bootstrap
> **Constraint:** Keep existing UIAutomator+OCR+SoM+LLM perception pipeline unchanged; no local VLM model
> **Inspired by:** PhoneClaw (OpenPhone) ExperienceLog, Memory-First, and Learning Mode

---

## 1. Problem Statement

The current lesson-learned and self-exploration systems have two fundamental issues:

**Lessons don't improve over time.** The feedback loop is broken at multiple points:
- Reinforcement requires Cortex to explicitly cite lesson IDs via regex (`applied_lesson: "nav-a3f1"`), but LLMs do this unreliably — implicit lesson usage is invisible
- No compaction — JSONL grows with noise, and the 500-token budget fills with low-value entries
- Dedup is syntactic only (path fingerprint + normalized goal text), missing semantic duplicates like "Open WiFi settings" vs "Go to WiFi configuration"
- Scorer's strongest signal (+3.0 for activity match) is never used because Contextor passes `current_activity=None`

**Autonomous exploration is too expensive for what it produces.** Each deep node costs a full agent pipeline (up to 50 steps, multiple LLM calls), exploring ~5-10 nodes before budget/rate limits hit. Generated lessons all get `confidence=0.6` regardless of verification. There is no way to bootstrap with human knowledge.

### Current Feedback Flow (Broken)

```
Contextor loads lessons (with missing screen context)
    ↓
Cortex receives 500-token lesson text in prompt
    ↓
Cortex MAY cite "applied_lesson: <id>" in decisions_reason  ← unreliable
    ↓
Contextor regex-extracts IDs → update_lesson_feedback()
    ↓
Bayesian confidence update (rarely triggered)
    ↓
No compaction → JSONL grows indefinitely
```

### Proposed Feedback Flow

```
Contextor loads lessons (with activity + key_elements)
    ↓
Cortex receives compact, high-quality lesson text
    ↓
Action-based implicit reinforcement (no LLM citation needed)
    ↓
Confidence updates every cycle (always triggered when lessons loaded)
    ↓
Periodic LLM compaction → lean, high-confidence knowledge base
    ↓
Human demos inject pre-verified seeds with confidence=0.9
```

---

## 2. Design Overview

Four independent improvements, each deployable separately:

| # | Feature | What It Solves | LLM Cost |
|---|---------|---------------|----------|
| A | Semantic deduplication | Near-duplicate lessons waste token budget | Zero (Jaccard) or 1 embedding call per lesson |
| B | Implicit reinforcement | Broken feedback loop — confidence never updates | Zero |
| C | LLM compaction | Noisy JSONL with 20+ low-value lessons per app | 1 LLM call per compaction (~infrequent) |
| D | Human demo-driven exploration | Cold-start problem, expensive autonomous exploration | 1 LLM call per demo tap (~10-30 per demo session) |

---

## 3. Feature A: Semantic Deduplication

### Current State

Two dedup mechanisms exist, both syntactic:

1. **Path fingerprint** (`recorder.py:45-57`): MD5 hash of `(action, target_text)` sequence — catches exact replays
2. **Goal normalization** (`recorder.py:17-42`): Lowercase + strip punctuation + replace app package — catches minor phrasing diffs

Both miss semantic duplicates:
- "Navigate to WiFi settings" vs "Open the WiFi configuration page"
- "Tap the hamburger menu" vs "Open the navigation drawer"

### Proposed: Jaccard Word-Overlap Similarity

Use token-level Jaccard similarity (zero API cost, no embeddings needed):

```python
# New file: mineru/ui_auto/lessons/similarity.py

import re
from functools import lru_cache

STOP_WORDS = frozenset({
    "the", "a", "an", "to", "on", "in", "for", "is", "and", "or", "of",
    "with", "this", "that", "it", "from", "by", "at", "be", "as", "if",
    "tap", "click", "press", "open", "go", "navigate",  # action verbs (low signal)
})

def tokenize(text: str) -> set[str]:
    """Lowercase, strip punctuation, remove stop words."""
    tokens = set(re.findall(r"[a-z0-9]+", text.lower()))
    return tokens - STOP_WORDS

def jaccard_similarity(a: str, b: str) -> float:
    """Token-level Jaccard similarity, stop-words removed."""
    ta, tb = tokenize(a), tokenize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)

SEMANTIC_DEDUP_THRESHOLD = 0.55  # Tuned: catches paraphrases, avoids false merges
```

### Integration Points

**In `loader.py:load_and_compact_lessons()`**, add a Pass 3 after existing Pass 2:

```python
# Pass 3: Semantic dedup (Jaccard word overlap)
# Compare each lesson's (summary + context.goal) against all others of same type
# If similarity > SEMANTIC_DEDUP_THRESHOLD → merge into higher-confidence entry
deduped = _semantic_dedup_pass(compacted_lessons)
```

**Merge rule**: When two lessons are semantically similar:
- Keep the one with higher `confidence` (or higher `occurrences` as tiebreaker)
- Sum `occurrences` and `applied_success/failure` counters
- Keep the more recent `last_seen`
- Keep the longer/more-specific `lesson` text

**In `recorder.py:record_success_path()`**, add semantic check before the existing two-tier dedup:

```python
# Before existing path-fingerprint and goal-normalization checks:
for existing in existing_lessons:
    if existing.type == "success_path":
        sim = jaccard_similarity(
            normalize_for_comparison(goal),
            normalize_for_comparison(existing.context.goal)
        )
        if sim > SEMANTIC_DEDUP_THRESHOLD:
            # Merge: increment occurrences, keep newer path
            _record_merge_update(existing, new_lesson)
            return
```

### Why Jaccard Over Embeddings

- Zero API cost — runs locally, no network calls
- Sufficient for navigation-domain text where key nouns (WiFi, Settings, hamburger) carry most signal
- Stop-word removal + action-verb removal makes it robust to phrasing variations
- Embeddings can be added later as an optional enhancement if Jaccard proves insufficient

---

## 4. Feature B: Implicit Reinforcement

### Current State (Broken)

The feedback loop requires Cortex to explicitly write `applied_lesson: "nav-a3f1"` in its `decisions_reason` field. This is parsed by regex in `cortex.py:132-141`:

```python
applied_ids = re.findall(r'applied_lesson:\s*"([^"]+)"', response.decisions_reason)
failed_ids = re.findall(r'lesson_failed:\s*"([^"]+)"', response.decisions_reason)
```

**Why this fails:**
- LLMs inconsistently follow formatting instructions for metadata annotations
- When Cortex implicitly follows a lesson (uses the navigation path without citing it), no reinforcement occurs
- Most lesson applications are invisible → confidence stagnates at 0.5

### Proposed: Action-Based Implicit Reinforcement

Instead of requiring Cortex to cite lesson IDs, detect reinforcement by comparing **what the agent actually did** with **what lessons suggested**.

#### Algorithm

After each successful action, compare the executed action against all loaded lessons:

```python
# New function in: mineru/ui_auto/lessons/reinforcement.py

def detect_implicit_reinforcement(
    executed_action: str,           # e.g., "tap"
    executed_target: str | None,    # e.g., "Wi-Fi" or "Network & internet"
    screen_changed: bool | None,
    tool_status: str,               # "success" or "error"
    active_lesson_entries: list[LessonEntry],  # parsed from active_lessons
) -> tuple[list[str], list[str]]:
    """Return (reinforced_ids, weakened_ids) based on action matching."""

    reinforced: list[str] = []
    weakened: list[str] = []

    for lesson in active_lesson_entries:
        match = _action_matches_lesson(executed_action, executed_target, lesson)
        if not match:
            continue

        if tool_status == "success" and screen_changed is not False:
            reinforced.append(lesson.id)
        elif tool_status == "error" or screen_changed is False:
            weakened.append(lesson.id)

    return reinforced, weakened
```

#### Matching Logic

```python
def _action_matches_lesson(
    action: str,
    target: str | None,
    lesson: LessonEntry,
) -> bool:
    """Check if the executed action aligns with a lesson's content."""

    if not target:
        return False

    target_lower = target.lower().strip()

    # Match against success_path steps
    if lesson.type == "success_path" and lesson.path:
        for step in lesson.path:
            if (step.action == action and step.target_text
                    and _text_matches(target_lower, step.target_text.lower())):
                return True

    # Match against mistake lessons (negative: if agent taps what mistake warned about)
    if lesson.type == "mistake":
        if (lesson.context.action_attempted == action
                and lesson.context.goal
                and target_lower in lesson.context.goal.lower()):
            return True  # Agent is repeating a known mistake

    # Match against strategy suggestions
    if lesson.type == "strategy" and lesson.suggested_strategy:
        if target_lower in lesson.suggested_strategy.lower():
            return True

    return False


def _text_matches(target: str, lesson_text: str) -> bool:
    """Fuzzy text match: exact substring or high token overlap."""
    if target in lesson_text or lesson_text in target:
        return True
    return jaccard_similarity(target, lesson_text) > 0.7
```

#### Integration

**In `contextor.py`**, replace the explicit-ID feedback processing (lines 260-276) with implicit detection:

```python
# BEFORE (broken):
if state.applied_lesson_ids or state.failed_lesson_ids:
    update_lesson_feedback(applied_ids=state.applied_lesson_ids, ...)

# AFTER (implicit):
if state.active_lessons and state.last_tool_name and state.last_tool_status:
    reinforced_ids, weakened_ids = detect_implicit_reinforcement(
        executed_action=state.last_tool_name,
        executed_target=state.last_tool_target,  # NEW: add target text to state
        screen_changed=screen_changed,
        tool_status=state.last_tool_status,
        active_lesson_entries=state.active_lesson_entries,  # NEW: parsed entries
    )
    if reinforced_ids or weakened_ids:
        await update_lesson_feedback(
            lessons_dir=ctx.lessons_dir,
            app_package=current_app_package,
            applied_ids=reinforced_ids,
            failed_ids=weakened_ids,
            screen_changed=screen_changed,
            tool_status=state.last_tool_status,
        )
```

#### State Changes

Add to `graph/state.py`:

```python
# New fields:
last_tool_target: str | None = None          # Text of element that was tapped/interacted with
active_lesson_entries: list[dict] = []        # Parsed LessonEntry dicts (not just formatted text)
```

**Populate `last_tool_target`**: In executor, when a tap tool is called, extract the target text from the tool arguments and store it in state.

**Populate `active_lesson_entries`**: In Contextor, when loading lessons, keep both the formatted text (for Cortex prompt) and the raw entries (for implicit reinforcement).

#### Keep Explicit Citations as Bonus

Don't remove the regex-based parsing — keep it as a supplementary signal. If Cortex explicitly cites a lesson, that's stronger evidence than implicit matching:

```python
# Explicit citation = strong signal (weight 1.0)
# Implicit action match = moderate signal (weight 0.5)
# Both present = strongest signal (weight 1.0)
```

Implementation: Add a `reinforcement_weight` parameter to `update_lesson_feedback()`:

```python
async def update_lesson_feedback(
    ...,
    applied_ids: list[str],
    failed_ids: list[str],
    ...,
    weight: float = 1.0,  # NEW: 0.5 for implicit, 1.0 for explicit
):
    # applied_success += weight (instead of += 1)
```

---

## 5. Feature C: LLM-Powered Compaction

### Current State

No compaction exists. The only cleanup is:
- Auto-rewrite when `raw > compacted * 1.5` (dedup-only, no quality filtering)
- Hard staleness cutoff (30/60/90 days)

An app explored over 5 sessions can accumulate 50+ lessons, most low-value:
- Transient mistakes that were already corrected
- Near-duplicate success paths from slightly different starting points
- Strategies that were superseded by better approaches

### Proposed: Periodic LLM Consolidation

When an app accumulates more than `COMPACT_THRESHOLD` lessons (default: 20), invoke the LLM to consolidate into a lean, high-confidence knowledge base.

#### Compaction Trigger

```python
# In loader.py:load_lessons_for_app(), after dedup passes:

COMPACT_THRESHOLD = 20
COMPACT_TARGET = 10

if len(compacted_lessons) > COMPACT_THRESHOLD:
    compacted_lessons = await compact_with_llm(
        lessons=compacted_lessons,
        app_package=app_package,
        target_count=COMPACT_TARGET,
        ctx=ctx,
    )
    _rewrite_compacted(lessons_path, compacted_lessons)
```

#### Compaction Algorithm

```python
# New file: mineru/ui_auto/lessons/compactor.py

COMPACT_SYSTEM_PROMPT = """\
You are a lesson compactor for a mobile automation agent. You will receive a list of
lessons recorded from past sessions with a specific Android app.

Your task: consolidate these into {target_count} or fewer high-quality lessons.

Rules:
1. MERGE near-duplicate lessons (same screen, same action, different wording) into one.
   Sum their occurrence counts and keep the highest confidence.
2. REMOVE low-value lessons:
   - Mistakes that were corrected (confidence rose to >0.8 after initial failure)
   - Keystroke-level events ("typed 'hello' in search box")
   - Overly specific lessons that won't transfer ("tapped the 3rd item in the list")
3. GENERALIZE coordinates when present:
   - "tap at (540, 1200)" → "tap the bottom navigation bar area"
4. KEEP high-value lessons:
   - Navigation paths with ≥3 steps
   - Lessons with high confidence (>0.7) AND high reinforcement (seen ≥3x)
   - Failure patterns that repeat (occurrences ≥2)
5. For each output lesson, preserve the original fields: type, category, summary, lesson,
   suggested_strategy, confidence, occurrences.
   Set confidence to the MAX of merged lessons.
   Set occurrences to the SUM of merged lessons.

Output: JSON array of consolidated lessons (same schema as input, minus id/timestamps).
"""

async def compact_with_llm(
    lessons: list[LessonEntry],
    app_package: str,
    target_count: int,
    ctx: MobileUseContext,
) -> list[LessonEntry]:
    """Invoke LLM to consolidate lessons into a lean set."""

    from mineru.ui_auto.services.llm import get_llm

    llm = get_llm(ctx, name="compactor", temperature=0.2)

    # Serialize lessons to JSON (strip timestamps/IDs — LLM doesn't need them)
    lessons_json = [_lesson_to_compact_dict(l) for l in lessons]

    prompt = COMPACT_SYSTEM_PROMPT.format(target_count=target_count)
    user_msg = f"App: {app_package}\n\nLessons ({len(lessons_json)}):\n{json.dumps(lessons_json, indent=2)}"

    response = await llm.ainvoke([
        SystemMessage(content=prompt),
        HumanMessage(content=user_msg),
    ])

    compacted = _parse_compact_response(response.content, lessons)

    logger.info(f"📦 Compacted {len(lessons)} → {len(compacted)} lessons for {app_package}")
    return compacted
```

#### Compaction Frequency

- **Trigger**: Only when `load_lessons_for_app()` is called AND lesson count > threshold
- **Cooldown**: Track `last_compacted_at` in `_meta.json`; skip if compacted within last 24 hours
- **Budget guard**: Max 1 compaction per app per session (prevent compaction loops)

#### Safety: Original Backup

Before compacting, write the original JSONL to `_pre_compact_backup.jsonl`:

```python
async def compact_with_llm(...):
    # Backup before destructive rewrite
    backup_path = lessons_path.with_name("_pre_compact_backup.jsonl")
    if not backup_path.exists():  # Only keep first backup
        shutil.copy2(lessons_path, backup_path)
    ...
```

#### LLM Config for Compactor

Add to `config.py` LLM presets:

```python
"compactor": {
    "provider": "claude",        # Use Claude CLI (same as other agents)
    "temperature": 0.2,          # Low creativity, high fidelity
    "max_tokens": 4096,          # Enough for ~10 consolidated lessons
}
```

The compactor is a non-critical utility agent — can use whatever model is configured, including Haiku for cost efficiency.

---

## 6. Feature D: Human Demo-Driven Exploration

### Motivation

Autonomous exploration costs 5-15 LLM calls per node and produces `confidence=0.6` lessons. A 2-minute human demo of an app's main flows can produce the same knowledge at 1 LLM call per tap with `confidence=0.9`.

### Architecture

```
User operates phone normally
         │
         ├─── adb shell getevent (streams raw touch events)
         │         ↓
         │    Touch event parser (x, y, timestamp)
         │
         ├─── adb screencap polling (~4 fps)
         │         ↓
         │    Frame diff detector (skip unchanged frames)
         │
         └─── adb shell uiautomator dump (on each detected tap)
                   ↓
              UI hierarchy snapshot (element labels, resource IDs)
                   ↓
         ┌─────────────────────────────────┐
         │  Correlate tap → UI element     │
         │  (find element at tap coords)   │
         └─────────┬───────────────────────┘
                   ↓
         ┌─────────────────────────────────┐
         │  LLM lesson extraction          │
         │  (batch: every 5-10 taps)       │
         │  "What navigation pattern do    │
         │   these actions represent?"     │
         └─────────┬───────────────────────┘
                   ↓
         success_path lessons (confidence=0.9)
              +
         feature tree nodes (status=explored)
```

### Why `getevent` Over Computer Vision

PhoneClaw uses OpenCV HoughCircles to detect iOS "Show Touches" indicators. This requires:
- Enabling "Show Touches" in iOS accessibility settings
- Computer vision pipeline (OpenCV dependency)
- Tuning circle detection parameters per device resolution

Android provides a much better option: **`adb shell getevent`** streams raw kernel input events with exact coordinates. No CV needed, no "Show Touches" needed, pixel-perfect accuracy.

### New CLI Command

```
ui-auto learn-demo --app com.example.app [--duration 180] [--lessons-dir ./lessons]
```

**Flow:**
1. Launch app on device
2. Print instructions: "Operate your phone normally. Press Ctrl+C when done."
3. Start capture (getevent + screencap polling in background)
4. On Ctrl+C: stop capture, process recording
5. Extract lessons via LLM analysis
6. Optionally update feature tree

### Implementation

#### New File: `mineru/ui_auto/exploration/demo_recorder.py`

```python
"""Human demo recording via ADB input events + screenshots."""

import asyncio
import re
import time
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from mineru.ui_auto.context import MobileUseContext

# Constants
SCREENCAP_INTERVAL = 0.25           # 4 fps
GETEVENT_TOUCH_DOWN = 0x14a         # BTN_TOUCH down
ABS_MT_POSITION_X = 0x35
ABS_MT_POSITION_Y = 0x36
ABS_MT_TRACKING_ID = 0x39

# --- Data Structures ---

@dataclass
class TouchEvent:
    """A single tap detected from getevent stream."""
    timestamp: float                 # Unix timestamp
    raw_x: int                       # Raw device coordinates
    raw_y: int
    norm_x: float                    # Normalized [0, 1]
    norm_y: float
    pixel_x: int                     # Screen pixel coordinates
    pixel_y: int
    duration_ms: float = 0           # Touch duration (down → up)

@dataclass
class DemoFrame:
    """A screenshot frame with optional associated tap."""
    idx: int
    timestamp: float
    screenshot_path: Path            # PNG file path
    screenshot_b64: str | None = None
    width: int = 0
    height: int = 0
    tap: TouchEvent | None = None    # Tap that occurred near this frame
    hierarchy_xml: str | None = None # UIAutomator dump at this moment
    matched_element: dict | None = None  # UI element at tap coordinates

@dataclass
class DemoRecording:
    """Complete demo recording with frames and extracted taps."""
    app_package: str
    started_at: float
    ended_at: float = 0
    frames: list[DemoFrame] = field(default_factory=list)
    taps: list[TouchEvent] = field(default_factory=list)
    screen_width: int = 0
    screen_height: int = 0
    input_device: str = ""           # e.g., /dev/input/event4
```

#### Touch Event Capture

```python
class GeteventCapture:
    """Capture touch events from Android via adb shell getevent."""

    def __init__(self, device_id: str):
        self.device_id = device_id
        self.process: subprocess.Popen | None = None
        self.events: list[TouchEvent] = []
        self._running = False
        self._input_device: str | None = None  # auto-detected
        self._screen_size: tuple[int, int] = (0, 0)
        self._input_max: tuple[int, int] = (0, 0)  # raw axis maximums

    async def detect_touch_device(self) -> str:
        """Find the touchscreen input device.

        Runs 'getevent -il' and looks for ABS_MT_POSITION_X capability.
        Returns device path like '/dev/input/event4'.
        """
        result = subprocess.run(
            ["adb", "-s", self.device_id, "shell", "getevent", "-il"],
            capture_output=True, text=True, timeout=5,
        )
        # Parse output to find device with ABS_MT_POSITION_X
        current_device = None
        for line in result.stdout.splitlines():
            if line.startswith("add device"):
                current_device = line.split(":")[-1].strip()
            if "ABS_MT_POSITION_X" in line and current_device:
                # Also extract max value for coordinate normalization
                match = re.search(r"max\s+(\d+)", line)
                if match:
                    self._input_max = (int(match.group(1)), self._input_max[1])
                return current_device
        raise RuntimeError("No touchscreen input device found")

    async def detect_input_range(self, device: str) -> tuple[int, int]:
        """Get the max X and Y values for the input device.

        Needed to normalize raw getevent coordinates to screen pixels.
        Raw coords use the digitizer's range (e.g., 0-32767), NOT screen pixels.
        """
        result = subprocess.run(
            ["adb", "-s", self.device_id, "shell", "getevent", "-il"],
            capture_output=True, text=True, timeout=5,
        )
        max_x = max_y = 0
        in_device = False
        for line in result.stdout.splitlines():
            if device in line:
                in_device = True
            elif line.startswith("add device") and in_device:
                break
            if in_device:
                if "ABS_MT_POSITION_X" in line:
                    m = re.search(r"max\s+(\d+)", line)
                    if m: max_x = int(m.group(1))
                elif "ABS_MT_POSITION_Y" in line:
                    m = re.search(r"max\s+(\d+)", line)
                    if m: max_y = int(m.group(1))
        return max_x, max_y

    async def start(self):
        """Start streaming getevent in background."""
        self._input_device = await self.detect_touch_device()
        self._input_max = await self.detect_input_range(self._input_device)
        self._screen_size = await self._get_screen_size()

        self.process = subprocess.Popen(
            ["adb", "-s", self.device_id, "shell", "getevent", "-lt", self._input_device],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self._running = True
        # Start reader thread
        self._reader_task = asyncio.create_task(self._read_events())

    async def _read_events(self):
        """Parse getevent stream into TouchEvent objects.

        getevent -lt output format:
        [  timestamp] /dev/input/event4: EV_ABS ABS_MT_TRACKING_ID  0000007a
        [  timestamp] /dev/input/event4: EV_ABS ABS_MT_POSITION_X   00001a2b
        [  timestamp] /dev/input/event4: EV_ABS ABS_MT_POSITION_Y   00002c3d
        [  timestamp] /dev/input/event4: EV_KEY BTN_TOUCH            DOWN
        [  timestamp] /dev/input/event4: EV_SYN SYN_REPORT           00000000
        ...
        [  timestamp] /dev/input/event4: EV_KEY BTN_TOUCH            UP
        """
        current_x = current_y = 0
        touch_down_time = None

        while self._running and self.process:
            line = await asyncio.get_event_loop().run_in_executor(
                None, self.process.stdout.readline
            )
            if not line:
                break

            # Parse timestamp
            ts_match = re.match(r"\[\s*([\d.]+)\]", line)
            if not ts_match:
                continue
            ts = float(ts_match.group(1))

            if "ABS_MT_POSITION_X" in line:
                val = int(line.strip().split()[-1], 16)
                current_x = val
            elif "ABS_MT_POSITION_Y" in line:
                val = int(line.strip().split()[-1], 16)
                current_y = val
            elif "BTN_TOUCH" in line and "DOWN" in line:
                touch_down_time = ts
            elif "BTN_TOUCH" in line and "UP" in line:
                if touch_down_time is not None:
                    duration_ms = (ts - touch_down_time) * 1000
                    # Only record taps (< 500ms); ignore long-press and swipes
                    if duration_ms < 500:
                        max_x, max_y = self._input_max
                        sw, sh = self._screen_size
                        norm_x = current_x / max_x if max_x > 0 else 0
                        norm_y = current_y / max_y if max_y > 0 else 0
                        pixel_x = int(norm_x * sw)
                        pixel_y = int(norm_y * sh)

                        event = TouchEvent(
                            timestamp=touch_down_time,
                            raw_x=current_x,
                            raw_y=current_y,
                            norm_x=round(norm_x, 4),
                            norm_y=round(norm_y, 4),
                            pixel_x=pixel_x,
                            pixel_y=pixel_y,
                            duration_ms=round(duration_ms, 1),
                        )
                        self.events.append(event)
                    touch_down_time = None

    async def stop(self) -> list[TouchEvent]:
        """Stop capture and return all detected taps."""
        self._running = False
        if self.process:
            self.process.terminate()
            self.process.wait(timeout=3)
        return self.events

    async def _get_screen_size(self) -> tuple[int, int]:
        """Get screen resolution via 'adb shell wm size'."""
        result = subprocess.run(
            ["adb", "-s", self.device_id, "shell", "wm", "size"],
            capture_output=True, text=True, timeout=5,
        )
        m = re.search(r"(\d+)x(\d+)", result.stdout)
        if m:
            return int(m.group(1)), int(m.group(2))
        return 1080, 2400  # Default fallback
```

#### Screenshot Polling

```python
class ScreenshotPoller:
    """Capture screenshots at ~4 fps during demo."""

    def __init__(self, device_id: str, output_dir: Path, interval: float = SCREENCAP_INTERVAL):
        self.device_id = device_id
        self.output_dir = output_dir
        self.interval = interval
        self.frames: list[DemoFrame] = []
        self._running = False
        self._idx = 0

    async def start(self):
        self._running = True
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self):
        while self._running:
            t0 = time.monotonic()
            try:
                frame_path = self.output_dir / f"frame_{self._idx:04d}.png"
                # Capture screenshot via adb
                proc = await asyncio.create_subprocess_exec(
                    "adb", "-s", self.device_id, "exec-out", "screencap", "-p",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                png_data, _ = await proc.communicate()
                if png_data and len(png_data) > 100:  # Valid PNG
                    frame_path.write_bytes(png_data)
                    self.frames.append(DemoFrame(
                        idx=self._idx,
                        timestamp=time.time(),
                        screenshot_path=frame_path,
                    ))
                    self._idx += 1
            except Exception:
                pass  # Skip frame on error

            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0, self.interval - elapsed))

    async def stop(self) -> list[DemoFrame]:
        self._running = False
        return self.frames
```

#### Tap-to-Element Correlation

After recording, correlate each tap with the nearest screenshot and UI hierarchy:

```python
async def correlate_taps_with_ui(
    taps: list[TouchEvent],
    frames: list[DemoFrame],
    device_id: str,
    ctx: MobileUseContext,
) -> list[DemoFrame]:
    """For each tap, find the closest screenshot frame and dump UI hierarchy.

    Returns frames enriched with tap data and matched UI elements.
    """
    tap_frames: list[DemoFrame] = []

    for tap in taps:
        # Find closest frame BEFORE the tap (what user saw when tapping)
        closest = None
        for frame in frames:
            if frame.timestamp <= tap.timestamp:
                closest = frame
            else:
                break

        if closest is None:
            continue

        closest.tap = tap

        # We already have the screenshot; now identify what element was tapped
        # Use the existing UIAutomator hierarchy if available, or match by coordinates
        if closest.hierarchy_xml:
            closest.matched_element = _find_element_at_coords(
                closest.hierarchy_xml, tap.pixel_x, tap.pixel_y
            )

        tap_frames.append(closest)

    return tap_frames


def _find_element_at_coords(hierarchy_xml: str, x: int, y: int) -> dict | None:
    """Find the deepest (most specific) UI element containing (x, y).

    Parses UIAutomator XML, finds all elements whose bounds contain the point,
    returns the smallest (deepest) one.
    """
    import xml.etree.ElementTree as ET
    from mineru.ui_auto.exploration.discovery import parse_bounds

    root = ET.fromstring(hierarchy_xml)
    best = None
    best_area = float("inf")

    for node in root.iter("node"):
        bounds_str = node.get("bounds", "")
        if not bounds_str:
            continue
        bounds = parse_bounds(bounds_str)
        if bounds is None:
            continue

        left, top, right, bottom = bounds["left"], bounds["top"], bounds["right"], bounds["bottom"]
        if left <= x <= right and top <= y <= bottom:
            area = (right - left) * (bottom - top)
            if area < best_area:
                best_area = area
                best = {
                    "text": node.get("text", ""),
                    "resource_id": node.get("resource-id", ""),
                    "class": node.get("class", ""),
                    "content_desc": node.get("content-desc", ""),
                    "bounds": {"left": left, "top": top, "right": right, "bottom": bottom},
                }

    return best
```

#### Hierarchy Snapshots During Recording

Taking a `uiautomator dump` at every frame would be too slow (~500ms each). Instead, snapshot only when a tap is detected:

```python
class DemoRecorder:
    """Orchestrates getevent capture + screenshot polling + hierarchy snapshots."""

    def __init__(self, ctx: MobileUseContext, app_package: str, output_dir: Path):
        self.ctx = ctx
        self.app_package = app_package
        self.output_dir = output_dir
        self.getevent = GeteventCapture(ctx.device_id)
        self.poller = ScreenshotPoller(ctx.device_id, output_dir / "screenshots")
        self._last_tap_count = 0
        self._hierarchy_cache: dict[int, str] = {}  # tap_idx → xml

    async def start(self):
        await self.getevent.start()
        await self.poller.start()
        # Monitor for new taps and snapshot hierarchy
        self._monitor_task = asyncio.create_task(self._monitor_taps())

    async def _monitor_taps(self):
        """When a new tap is detected, capture UI hierarchy."""
        while True:
            await asyncio.sleep(0.5)
            current_count = len(self.getevent.events)
            if current_count > self._last_tap_count:
                # New tap(s) detected — snapshot hierarchy
                for i in range(self._last_tap_count, current_count):
                    try:
                        xml = await self._dump_hierarchy()
                        self._hierarchy_cache[i] = xml
                    except Exception:
                        pass  # Best-effort
                self._last_tap_count = current_count

    async def _dump_hierarchy(self) -> str:
        """Capture UIAutomator hierarchy XML."""
        proc = await asyncio.create_subprocess_exec(
            "adb", "-s", self.ctx.device_id, "shell",
            "uiautomator", "dump", "/dev/tty",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode("utf-8", errors="replace")

    async def stop(self) -> DemoRecording:
        taps = await self.getevent.stop()
        frames = await self.poller.stop()

        # Enrich taps with hierarchy data
        for i, tap in enumerate(taps):
            if i in self._hierarchy_cache:
                # Find matching frame and attach hierarchy
                for frame in frames:
                    if frame.tap is None and abs(frame.timestamp - tap.timestamp) < 0.5:
                        frame.tap = tap
                        frame.hierarchy_xml = self._hierarchy_cache[i]
                        frame.matched_element = _find_element_at_coords(
                            self._hierarchy_cache[i], tap.pixel_x, tap.pixel_y
                        )
                        break

        return DemoRecording(
            app_package=self.app_package,
            started_at=frames[0].timestamp if frames else 0,
            ended_at=frames[-1].timestamp if frames else 0,
            frames=frames,
            taps=taps,
            screen_width=self.getevent._screen_size[0],
            screen_height=self.getevent._screen_size[1],
            input_device=self.getevent._input_device or "",
        )
```

#### LLM Lesson Extraction

Process the recording in batches (not per-frame like PhoneClaw — we have hierarchy data, so we can be more efficient):

```python
# New file: mineru/ui_auto/exploration/demo_analyzer.py

DEMO_ANALYSIS_PROMPT = """\
You are analyzing a human demonstration of the Android app "{app_package}".

The user performed {tap_count} taps. For each tap, you have:
- The UI element that was tapped (text, resource-id, class)
- The screen context (what activity was showing)

Analyze the sequence and extract reusable navigation lessons.

For each distinct navigation path (sequence of taps leading to a destination):
1. Describe the goal in natural language (e.g., "Navigate to WiFi settings")
2. List the steps as a path: tap('Element A') → tap('Element B') → ...
3. Note any important observations (element was scrollable, required waiting, etc.)

Rules:
- Merge consecutive taps in the same screen into a single path
- Skip taps that appear to be mistakes (immediately followed by back)
- Skip keyboard input taps (typing in text fields)
- Focus on NAVIGATION knowledge that helps reach specific screens

Output: JSON array of lessons:
[
  {{
    "goal": "Navigate to WiFi settings",
    "steps": [
      {{"action": "tap", "target_text": "Settings", "target_resource_id": "...", "result": "Opened Settings"}},
      {{"action": "tap", "target_text": "Network & internet", "target_resource_id": "...", "result": "Opened Network settings"}},
      {{"action": "tap", "target_text": "Wi-Fi", "target_resource_id": "...", "result": "Opened WiFi list"}}
    ],
    "observation": "WiFi is 2 taps from Settings home"
  }}
]
"""

async def extract_lessons_from_demo(
    recording: DemoRecording,
    ctx: MobileUseContext,
) -> list[LessonEntry]:
    """Analyze demo recording and extract success_path lessons."""

    from mineru.ui_auto.services.llm import get_llm

    # Build tap summary for LLM
    tap_summaries = []
    for i, tap in enumerate(recording.taps):
        # Find the frame with this tap's hierarchy
        element_desc = "unknown element"
        for frame in recording.frames:
            if frame.tap is tap and frame.matched_element:
                el = frame.matched_element
                element_desc = (
                    f"text='{el['text']}', resource_id='{el['resource_id']}', "
                    f"class='{el['class']}', content_desc='{el['content_desc']}'"
                )
                break

        tap_summaries.append(f"Tap {i+1}: ({tap.pixel_x}, {tap.pixel_y}) → {element_desc}")

    llm = get_llm(ctx, name="compactor", temperature=0.2)  # Reuse compactor config

    prompt = DEMO_ANALYSIS_PROMPT.format(
        app_package=recording.app_package,
        tap_count=len(recording.taps),
    )
    user_msg = "Tap sequence:\n" + "\n".join(tap_summaries)

    response = await llm.ainvoke([
        SystemMessage(content=prompt),
        HumanMessage(content=user_msg),
    ])

    # Parse response into LessonEntry objects
    lessons = _parse_demo_lessons(response.content, recording.app_package)
    return lessons


def _parse_demo_lessons(response_text: str, app_package: str) -> list[LessonEntry]:
    """Parse LLM response into LessonEntry objects with confidence=0.9."""
    import json
    from datetime import datetime, timezone
    from mineru.ui_auto.lessons.types import LessonEntry, LessonContext, PathStep, ScreenSignature

    # Extract JSON from response
    try:
        # Handle markdown code blocks
        text = response_text.strip()
        if "```" in text:
            text = text.split("```json")[-1].split("```")[0].strip()
            if not text:
                text = response_text.split("```")[1].split("```")[0].strip()
        raw_lessons = json.loads(text)
    except (json.JSONDecodeError, IndexError):
        return []

    entries = []
    for raw in raw_lessons:
        steps = []
        for step in raw.get("steps", []):
            steps.append(PathStep(
                action=step.get("action", "tap"),
                target_text=step.get("target_text"),
                target_resource_id=step.get("target_resource_id"),
                result=step.get("result", ""),
            ))

        if len(steps) < 2:
            continue  # Skip single-step "lessons"

        entry_id = f"demo-{hash(raw.get('goal', '')) & 0xffff:04x}"
        now = datetime.now(timezone.utc)

        entry = LessonEntry(
            id=entry_id,
            type="success_path",
            category="navigation",
            summary=raw.get("goal", ""),
            context=LessonContext(
                goal=raw.get("goal", ""),
                screen_signature=ScreenSignature(activity="", key_elements=[]),
                action_attempted="navigation",
                what_happened=raw.get("observation", "Demonstrated by user"),
            ),
            lesson=raw.get("observation", ""),
            suggested_strategy=f"Follow path: {' → '.join(s.target_text or '?' for s in steps)}",
            confidence=0.9,          # Human-verified
            occurrences=1,
            applied_success=1,       # Pre-credit: human succeeded
            applied_failure=0,
            created=now,
            last_seen=now,
            path=steps,
            deprecated=False,
        )
        entries.append(entry)

    return entries
```

#### Feature Tree Integration

Optionally update the exploration feature tree with demo-discovered nodes:

```python
async def update_tree_from_demo(
    lessons: list[LessonEntry],
    app_package: str,
    lessons_dir: Path,
):
    """Mark demonstrated paths as explored in the feature tree."""
    from mineru.ui_auto.exploration.state import load_exploration_state, save_exploration_state
    from mineru.ui_auto.exploration.helpers import find_child_by_identity

    state = load_exploration_state(lessons_dir, app_package)
    if state is None:
        return  # No tree yet — demo lessons still stored in JSONL

    for lesson in lessons:
        if not lesson.path:
            continue
        # Walk the tree, marking each step's node as explored
        current = state.root
        for step in lesson.path:
            if not step.target_text:
                continue
            child = find_child_by_identity(current, step.target_text)
            if child and child.status == "pending":
                child.status = "explored"
                child.attempt_count = 1

    save_exploration_state(lessons_dir, app_package, state)
```

#### CLI Wiring

Add to `main.py`:

```python
@app.command("learn-demo")
def learn_demo(
    app_package: Annotated[str, typer.Argument(help="App package to demonstrate")],
    lessons_dir: Annotated[str | None, typer.Option(help="Lessons directory")] = None,
    duration: Annotated[int, typer.Option(help="Max recording duration in seconds")] = 180,
    analyze: Annotated[bool, typer.Option(help="Run LLM analysis after recording")] = True,
):
    """Record a human demo and extract navigation lessons.

    Operate your phone normally while this command captures your taps.
    Press Ctrl+C when done.

    Example:
        ui-auto learn-demo com.google.android.deskclock --duration 120
    """
    asyncio.run(_run_learn_demo(app_package, lessons_dir, duration, analyze))


async def _run_learn_demo(app_package, lessons_dir, duration, analyze):
    # 1. Initialize device context (reuse existing agent init flow)
    # 2. Create DemoRecorder
    # 3. Start recording
    # 4. Wait for Ctrl+C or duration timeout
    # 5. Stop recording
    # 6. If analyze: extract lessons via LLM
    # 7. Store lessons to JSONL with confidence=0.9
    # 8. Optionally update feature tree
    ...
```

---

## 7. Scorer Fix: Pass Activity & Key Elements

This is a prerequisite that amplifies all four features above.

### Current State

`contextor.py:137-142`:
```python
active_lessons = await load_lessons_for_app(
    lessons_dir=ctx.lessons_dir,
    app_package=current_app_package,
    subgoal=subgoal_text,
    current_activity=None,        # ← NEVER SET
    current_key_elements=[],      # ← NEVER SET
)
```

The scorer's strongest signal (activity match: +3.0) and second strongest (key_elements overlap: +0.5 each) are dead code.

### Fix

Extract activity and key elements from the UIAutomator data already available in Contextor:

```python
# In contextor.py, within __call__():

# Extract activity from UI hierarchy (already parsed by perception layer)
current_activity = None
current_key_elements = []

if state.ui_elements:
    # Activity is typically available from the root node's package/class
    # Or from the window info in the hierarchy
    from mineru.ui_auto.exploration.discovery import extract_activity_from_hierarchy
    current_activity = extract_activity_from_hierarchy(state.ui_hierarchy_xml)

    # Key elements: extract text labels of interactive elements on current screen
    current_key_elements = [
        el.get("text", "") for el in state.ui_elements[:20]
        if el.get("text") and len(el.get("text", "")) > 1
    ]

active_lessons = await load_lessons_for_app(
    lessons_dir=ctx.lessons_dir,
    app_package=current_app_package,
    subgoal=subgoal_text,
    current_activity=current_activity,        # NOW SET
    current_key_elements=current_key_elements, # NOW SET
)
```

This requires extracting the current activity name. Options:
1. Parse from UIAutomator XML root node (if available)
2. Use `adb shell dumpsys activity activities | grep mResumedActivity` (reliable but extra shell call)
3. Parse from the screen data already collected by the perception layer

Option 1 is preferred (zero extra cost — data already captured).

---

## 8. Data Flow After All Changes

```
                    ┌──────────────────────────────────┐
                    │  Human Demo (learn-demo command)  │
                    │  getevent + screencap + hierarchy │
                    └──────────┬───────────────────────┘
                               │ LLM analysis
                               ▼
                    ┌──────────────────────────┐
                    │  High-confidence lessons  │
                    │  (confidence=0.9)         │
                    └──────────┬───────────────┘
                               │
        ┌──────────────────────┼──────────────────────┐
        │                      │                      │
        ▼                      ▼                      ▼
┌───────────────┐  ┌────────────────────┐  ┌──────────────────┐
│ lessons.jsonl │  │  Feature Tree      │  │ Autonomous       │
│ (append-only) │  │  (_exploration)    │  │ Exploration      │
│               │  │  Nodes marked      │  │ (fills gaps)     │
│ Semantic      │  │  "explored"        │  │                  │
│ dedup on      │  │                    │  │ Generates        │
│ write + load  │  │                    │  │ confidence=0.6   │
└───────┬───────┘  └────────────────────┘  └──────┬───────────┘
        │                                         │
        ├─────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────┐
│  load_lessons_for_app()              │
│                                      │
│  1. Load JSONL                       │
│  2. Pass 1: Merge by ID             │
│  3. Pass 2: Merge by normalized text │
│  4. Pass 3: Semantic dedup (Jaccard) │
│  5. LLM compaction (if >20 lessons)  │
│  6. Score with activity + elements   │  ← FIXED
│  7. Format within token budget       │
└───────┬──────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────┐
│  Contextor → Cortex prompt           │
│  (active_lessons + lesson_entries)   │
└───────┬──────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────┐
│  Agent executes action               │
│                                      │
│  Implicit reinforcement:             │
│  Compare action vs loaded lessons    │
│  → reinforced_ids, weakened_ids      │
│  → update_lesson_feedback()          │
│                                      │
│  Explicit citation (bonus):          │
│  Regex "applied_lesson:" still works │
└──────────────────────────────────────┘
```

---

## 9. Changes Required

| File | Change | Risk |
|------|--------|------|
| **NEW** `lessons/similarity.py` | Jaccard similarity + tokenizer | None (new file) |
| **NEW** `lessons/compactor.py` | LLM compaction logic + prompt | Low (new file, isolated) |
| **NEW** `lessons/reinforcement.py` | Implicit reinforcement detection | Low (new file, isolated) |
| **NEW** `exploration/demo_recorder.py` | GeteventCapture + ScreenshotPoller + DemoRecorder | Low (new file) |
| **NEW** `exploration/demo_analyzer.py` | LLM lesson extraction from demo + tree update | Low (new file) |
| `lessons/loader.py` | Add Pass 3 (semantic dedup), compaction trigger | Medium (modifies loading pipeline) |
| `lessons/recorder.py` | Add semantic dedup check in `record_success_path()` | Low (adds early-return check) |
| `agents/contextor/contextor.py` | Extract activity + key_elements; replace feedback with implicit | Medium (modifies feedback loop) |
| `graph/state.py` | Add `last_tool_target`, `active_lesson_entries` fields | Low (additive) |
| `main.py` | Add `learn-demo` command | Low (new command) |
| `config.py` | Add `compactor` LLM preset | Low (additive) |

**What does NOT change:**
- Perception pipeline (UIAutomator + OCR + SoM)
- Cortex/Planner/Executor agent logic
- Tool implementations
- Existing exploration runner (BFS/DFS)
- Device controllers

---

## 10. Implementation Plan

### Phase 1: Scorer Fix + Implicit Reinforcement (Foundation)
1. Extract activity/key_elements in Contextor → pass to `load_lessons_for_app()`
2. Add `last_tool_target` and `active_lesson_entries` to State
3. Implement `reinforcement.py` with action-matching logic
4. Wire implicit reinforcement in Contextor (replace broken explicit-only path)
5. Test: run a task that has existing lessons, verify confidence updates

### Phase 2: Semantic Dedup + Compaction (Quality)
6. Implement `similarity.py` (Jaccard tokenizer)
7. Add Pass 3 semantic dedup in `loader.py`
8. Add semantic check in `recorder.py:record_success_path()`
9. Implement `compactor.py` (LLM consolidation)
10. Add compaction trigger in `loader.py` with cooldown
11. Test: accumulate 25+ lessons for an app, verify compaction reduces to ~10

### Phase 3: Human Demo Mode (Bootstrap)
12. Implement `GeteventCapture` — test with `adb shell getevent` on real device
13. Implement `ScreenshotPoller` — test screenshot capture rate
14. Implement `DemoRecorder` (orchestrator) with hierarchy snapshots
15. Implement `demo_analyzer.py` (LLM lesson extraction)
16. Wire `learn-demo` CLI command
17. Test: record 2-min demo of Clock app, verify extracted lessons

### Phase 4: Integration + Feature Tree (Polish)
18. Add `update_tree_from_demo()` for feature tree integration
19. Test full flow: demo → lessons → autonomous exploration fills gaps
20. Add compaction history to `_meta.json`
21. Add `--no-analyze` flag to `learn-demo` for recording-only mode

---

## 11. Rejected Alternatives

### Embedding-Based Dedup (Deferred)
Using OpenAI `text-embedding-3-small` or similar for cosine similarity (like PhoneClaw). Deferred because:
- Requires API key configuration and network calls
- Jaccard with stop-word removal is sufficient for navigation-domain text
- Can be added later as a `similarity.py` enhancement without changing callers

### Computer Vision Tap Detection (Rejected for Android)
PhoneClaw uses OpenCV HoughCircles to detect iOS "Show Touches" indicators. Rejected because:
- Android `getevent` provides exact kernel-level coordinates (no CV needed)
- No dependency on "Show Touches" setting
- More accurate (pixel-perfect vs circle-center estimation)
- Simpler implementation (text parsing vs image processing)

### Per-Frame VLM Analysis (Rejected)
PhoneClaw sends each tap frame as a base64 image to the VLM. Rejected because:
- We don't use VLM — our pipeline is UIAutomator+OCR+SoM
- Hierarchy XML + element correlation gives richer data than screenshot analysis
- Batch processing (all taps → 1 LLM call) is more efficient than per-frame

### Memory-First Task Bypass (Deferred)
PhoneClaw's "instant answer from user profile" feature. Deferred because:
- Mineru is a task-execution agent, not a Q&A assistant
- The equivalent ("path-first execution" — replay known success_path directly) depends on having reliable, high-confidence lessons first
- Revisit after Phases 1-3 produce a trustworthy lesson base

### Real-Time Demo Analysis (Rejected)
Analyzing each tap during recording (real-time) vs after recording (batch). Rejected because:
- LLM calls during recording would introduce latency and interfere with natural phone operation
- Batch analysis after recording produces better lessons (full context of the tap sequence)
- Simpler implementation (recording and analysis are separate concerns)
