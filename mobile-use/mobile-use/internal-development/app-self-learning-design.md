# Design: App Self-Learning Mode

## Problem

The lesson-learned memory system records mistakes, strategies, and navigation paths — but only when a human gives the agent a specific task. The agent never explores on its own. After 50 real tasks on Settings, it knows 50 narrow paths. The other 200 screens and features remain terra incognita.

**What's missing:** A mode where the agent systematically explores an app's features, building comprehensive navigation knowledge *before* real tasks arrive. Like a new employee spending their first week learning where everything is.

### Concrete Example

A QA team wants the agent to run test cases on a banking app. Today:

```
Task 1: "Transfer $50 to Alice"    → 14 steps, 3 wrong turns, succeeds
Task 2: "Check account balance"    → 8 steps, 1 wrong turn, succeeds
Task 3: "Update mailing address"   → 22 steps, 6 wrong turns, times out
```

With self-learning (after a 30-minute exploration session):

```
Task 1: "Transfer $50 to Alice"    → 6 steps, 0 wrong turns, succeeds
Task 2: "Check account balance"    → 3 steps, 0 wrong turns, succeeds
Task 3: "Update mailing address"   → 9 steps, 0 wrong turns, succeeds
```

The agent already explored the app, recorded every navigation path, and knows where every feature lives. Real tasks become fast lookups instead of blind exploration.

---

## Design Overview

### What Self-Learning Is

A **multi-session exploration workflow** that:
1. Discovers an app's feature surface by crawling its UI hierarchy
2. Generates exploration goals automatically ("Open the Transfers screen and identify all options")
3. Executes each goal using the existing agent graph (Planner → Cortex → Executor → etc.)
4. Records lessons (success paths, UI mappings, mistakes) via the existing lesson-learned system
5. Tracks progress in a persistent exploration plan that survives session boundaries
6. Resumes from where it left off when a new session starts
7. **Supports dual-app cluster mode** for Parent/Child app ecosystems — a Master Orchestrator coordinates two agents exploring linked apps, capturing cross-app triggers and passive events

### What Self-Learning Is NOT

- Not a separate agent or graph — it reuses the existing agent graph unchanged
- Not a crawler/scraper — it navigates like a user, using the same tools (tap, swipe, back)
- Not ML training — no model fine-tuning, no gradient updates. "Learning" means accumulating structured lessons in JSONL files
- Not exhaustive — it uses heuristics and depth limits to avoid combinatorial explosion (e.g., doesn't try every permutation of a form)

---

## Architecture

### Single-App Mode

```
┌─────────────────────────────────────────────────────┐
│                  Self-Learning CLI                    │
│   ui-auto learn <app_package> [--sessions N]         │
│              [--budget-minutes M]                     │
└───────────────────┬─────────────────────────────────┘
                    │
        ┌───────────▼───────────────┐
        │   Exploration Planner     │
        │                           │
        │  1. Load exploration      │
        │     state (or init)       │
        │  2. Discover features     │
        │     from current screen   │
        │  3. Generate next goal    │
        │  4. Dispatch to agent     │
        │  5. Record outcome        │
        │  6. Update exploration    │
        │     state                 │
        │  7. Repeat or stop        │
        └───────────┬───────────────┘
                    │
        ┌───────────▼───────────────┐
        │   Existing Agent Graph    │
        │   (Planner → Cortex →    │
        │    Executor → Tools)      │
        │                           │
        │   + lesson-learned memory │
        │     (records as it goes)  │
        └───────────┬───────────────┘
                    │
        ┌───────────▼───────────────┐
        │   Exploration State       │
        │   (persisted to disk)     │
        │                           │
        │   - Feature tree          │
        │   - Visited/unvisited     │
        │   - Session history       │
        │   - Depth/budget tracking │
        └───────────────────────────┘
```

### Dual-App Cluster Mode

For Parent/Child app ecosystems (e.g., Family Safety apps), a Master Orchestrator coordinates two Exploration Planners on separate devices/sessions:

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                          Self-Learning CLI (Cluster)                          │
│   ui-auto learn-cluster --primary <parent_app> --secondary <child_app>       │
│              [--sessions N] [--budget-minutes M]                              │
└─────────────────────────────┬────────────────────────────────────────────────┘
                              │
               ┌──────────────▼──────────────┐
               │     Master Orchestrator      │
               │                              │
               │  1. Initialize both agents   │
               │  2. Coordinate goal dispatch │
               │  3. Trigger Observer Mode    │
               │     on secondary after       │
               │     primary actions          │
               │  4. Correlate cross-app      │
               │     events                   │
               │  5. Record cross-app edges   │
               └──────┬──────────────┬────────┘
                      │              │
        ┌─────────────▼──┐    ┌──────▼─────────────┐
        │  Primary Agent  │    │  Secondary Agent    │
        │  (Parent App)   │    │  (Child App)        │
        │                 │    │                     │
        │  Active Mode:   │    │  Dual Mode:         │
        │  Normal explore │    │  - Active: normal   │
        │  loop           │    │    explore loop     │
        │                 │    │  - Observer: poll    │
        │                 │    │    for async events  │
        └────────┬────────┘    └──────────┬──────────┘
                 │                        │
        ┌────────▼────────┐    ┌──────────▼──────────┐
        │ Primary Tree    │    │ Secondary Tree       │
        │ + cross_app_    │    │ + passive_events     │
        │   triggers      │    │ + cross_app_triggers │
        └─────────────────┘    └──────────────────────┘
```

**Key difference from single-app mode:** When the Primary Agent performs an action that may affect the Secondary App (e.g., enabling a parental control), the Master Orchestrator pauses the Primary, switches the Secondary Agent into Observer Mode, and waits for cross-app effects before continuing.

### Key Principle: Reuse, Don't Rebuild

The existing agent graph already knows how to navigate, tap, swipe, go back, record lessons, and handle errors. Self-learning is a **goal generation loop around the existing agent** — not a parallel system.

---

## Exploration State

### Feature Tree

The core data structure is a **feature tree** — a hierarchical map of an app's navigable surfaces. Each node represents a screen or feature area. The tree is built incrementally as the agent explores.

```json
{
  "app_package": "com.android.settings",
  "app_name": "Settings",
  "created": "2026-03-26T10:00:00Z",
  "last_session": "2026-03-26T11:30:00Z",
  "sessions_completed": 3,
  "root": {
    "id": "root",
    "label": "Settings main screen",
    "activity": "com.android.settings/.Settings",
    "key_elements": ["Network & internet", "Connected devices", "Apps", "Display", "Battery"],
    "status": "explored",
    "children": [
      {
        "id": "network",
        "label": "Network & internet",
        "activity": "com.android.settings/.SubSettings",
        "key_elements": ["Wi-Fi", "Mobile network", "Hotspot", "Airplane mode"],
        "nav_action": "tap('Network & internet')",
        "status": "explored",
        "children": [
          {
            "id": "wifi",
            "label": "Wi-Fi",
            "nav_action": "tap('Wi-Fi')",
            "status": "explored",
            "children": []
          },
          {
            "id": "mobile_network",
            "label": "Mobile network",
            "nav_action": "tap('Mobile network')",
            "status": "pending",
            "children": []
          }
        ]
      },
      {
        "id": "display",
        "label": "Display",
        "nav_action": "tap('Display')",
        "status": "pending",
        "children": []
      }
    ]
  }
}
```

### Node Statuses

| Status | Meaning |
|--------|---------|
| `pending` | Discovered but not yet visited |
| `in_progress` | Currently being explored (set at session start, guards against crashes) |
| `explored` | Agent navigated to this screen, discovered its children, recorded paths |
| `skipped` | Intentionally skipped (requires login, destructive action, etc.) |
| `failed` | Agent attempted but could not reach this screen (3 attempts) |
| `deep_limit` | Skipped because depth limit was reached |

### Node Types

| Type | Meaning |
|------|---------|
| `screen` | Standard full-screen navigation target (default) |
| `modal` | Bottom sheet, dialog, or overlay (<80% screen coverage). Has `dismiss_action` |
| `list_container` | RecyclerView/ListView — children are sampled, not exhaustive |
| `passive_event` | UI element that appeared without a direct local action (e.g., push notification, banner triggered by another app). Not a navigational child |
| `dynamic_element` | A tracked element whose state updates asynchronously (e.g., map avatar, live status indicator). Stored once, upserted on subsequent observations — never duplicated |

### Cross-App Triggers (Cluster Mode)

In dual-app cluster mode, nodes can carry `cross_app_triggers` — edges that connect actions in one app to observable effects in another:

```json
{
  "id": "enable_screen_time",
  "label": "Enable Screen Time",
  "nav_action": "tap('Enable Screen Time')",
  "status": "explored",
  "cross_app_triggers": [
    {
      "target_app": "com.example.child",
      "expected_event": "banner_notification",
      "expected_ui_text": "Screen Time is now active",
      "element_identity_key": null,
      "reliability_score": 0.9,
      "latency_ms": 2500,
      "observed_count": 9,
      "attempted_count": 10
    },
    {
      "target_app": "com.example.parent",
      "expected_event": "element_updated",
      "expected_ui_text": null,
      "element_identity_key": "com.example.parent/.MapActivity:ImageView:child_avatar",
      "last_observed_state": {
        "text": "Driving",
        "bounds_quadrant": "center"
      },
      "reliability_score": 1.0,
      "latency_ms": 3200,
      "observed_count": 5,
      "attempted_count": 5
    }
  ]
}
```

Fields:
- `target_app` — package name of the app where the effect is expected
- `expected_event` — event classification: `banner_notification`, `screen_change`, `element_appeared`, `element_disappeared`, **`element_updated`**
- `expected_ui_text` — text pattern to match in the target app's UI hierarchy (optional, used for verification)
- `element_identity_key` — stable identity for dynamic elements (see "Element Identity Keys" below). Used for `element_updated` events to match the same logical element across observations. `null` for non-identity events like banners
- `last_observed_state` — for `element_updated` triggers only: the most recent observed state (text, bounds quadrant). Upserted, not appended
- `reliability_score` — fraction of times the event was observed (auto-computed from `observed_count / attempted_count`)
- `latency_ms` — median time between the triggering action and the observed event
- `observed_count` / `attempted_count` — raw observation data for computing reliability

These triggers are recorded by the Master Orchestrator when Observer Mode detects a state change on the secondary app following a primary app action. They are stored in both apps' feature trees (as trigger on the primary, as `passive_event`/`dynamic_element` node on the secondary).

### Element Identity Keys

Dynamic UI elements (map avatars, live status indicators, countdown timers) change their `bounds` and `text` continuously but represent the same logical entity. To prevent tree bloat, elements are matched by **identity** rather than exact position/text.

An element's identity key is computed from stable properties that don't change when the element's state updates:

```python
def _compute_element_identity_key(elem: dict, activity: str) -> str:
    """Compute a stable identity key for an element.

    Uses properties that identify WHICH element this is, ignoring
    properties that describe its current STATE (position, dynamic text).

    Priority order:
    1. resource-id (most stable — e.g., "com.app:id/child_avatar")
    2. content-desc prefix (e.g., "Child Avatar" from "Child Avatar - Driving")
    3. className + structural position (fallback)
    """
    # Priority 1: resource-id is the gold standard
    resource_id = elem.get("resource-id", "").strip()
    if resource_id:
        return f"{activity}:{resource_id}"

    # Priority 2: content-desc prefix (strip dynamic suffixes)
    content_desc = elem.get("content-desc", "").strip()
    if content_desc:
        # Take first 3 words as stable prefix — dynamic suffixes like
        # "- Driving" or "- 2 min ago" are stripped
        prefix = " ".join(content_desc.split()[:3])
        return f"{activity}:{elem.get('className', '')}:{prefix}"

    # Priority 3: structural fallback (className + index in parent)
    class_name = elem.get("className", "unknown")
    index = elem.get("index", 0)
    return f"{activity}:{class_name}:idx{index}"
```

The identity key is **separate** from the structural ID used for feature tree node dedup. The structural ID identifies a position in the tree; the identity key identifies a logical entity that may move around.

### Type Definitions

All models live in `mineru/ui_auto/exploration/types.py`. This is the single source of truth for every type referenced in this document.

```python
from __future__ import annotations

from pydantic import BaseModel, Field


# ── Screen Signature ─────────────────────────────────────────

class ScreenSignature(BaseModel):
    """Fingerprint of a screen's layout, used for home-reset verification.

    Captured once during initialization (the app's home screen) and compared
    against the current screen after back-navigation to verify we've returned
    to the starting point. Matching uses activity + 60% key_elements overlap
    (see _signatures_match()).
    """

    activity: str | None = None                     # Android activity name (e.g., "com.android.settings/.Settings")
    key_elements: list[str] = []                    # Top visible text elements (first MAX_ELEMENTS_PER_SCREEN texts)
    element_count: int = 0                          # Total interactive elements on screen


def capture_screen_signature(
    app_info: str | None,
    ui_elements: list[dict],
    max_elements: int = 30,
) -> ScreenSignature:
    """Capture a screen signature from UI hierarchy data.

    Args:
        app_info: Current foreground activity/package string
        ui_elements: Flat list of UI element dicts from get_screen_data()
        max_elements: Maximum elements to include in key_elements

    Returns:
        ScreenSignature with activity, key text elements, and element count
    """
    key_texts = []
    for elem in ui_elements[:max_elements]:
        text = (elem.get("text") or "").strip()
        if text and len(text) <= 60:
            key_texts.append(text)

    return ScreenSignature(
        activity=app_info,
        key_elements=key_texts,
        element_count=len(ui_elements),
    )


# ── Feature Tree ──────────────────────────────────────────────

class FeatureNode(BaseModel):
    """A single navigable surface in the app — a screen, modal, list, or dynamic element."""

    id: str                                         # Structural ID (hash of activity+class+index+quadrant)
    label: str                                      # Human-readable label ("Wi-Fi", "Network & internet")
    activity: str | None = None                     # Android activity name (e.g., "com.android.settings/.SubSettings")
    key_elements: list[str] = []                    # Top visible text elements on this screen
    nav_action: str | None = None                   # How to reach this node from parent (e.g., "tap('Wi-Fi')")
    dismiss_action: str | None = None               # How to close this node (e.g., "back", "tap outside") — modals only
    status: str = "pending"                         # pending | in_progress | explored | skipped | failed | deep_limit
    node_type: str = "screen"                       # screen | modal | list_container | passive_event | dynamic_element
    children: list[FeatureNode] = []                # Child nodes discovered from this screen
    attempt_count: int = 0                          # Number of exploration attempts (failed nodes retry up to 3)
    skip_reason: str | None = None                  # Why this node was skipped ("auth_required", "dangerous_label")
    is_list_sample: bool = False                    # True if this node was sampled from a RecyclerView/ListView
    identity_key: str | None = None                 # Stable identity for dynamic_element nodes (resource-id based)
    cross_app_triggers: list[CrossAppTrigger] = []  # Cluster mode: effects observed in other apps after acting on this node


class ExplorationState(BaseModel):
    """Persisted state for an app's exploration progress. Stored in _exploration.json."""

    app_package: str                                # e.g., "com.android.settings"
    app_name: str | None = None                     # Human-readable app name
    created: str | None = None                      # ISO 8601 timestamp of first session
    last_session: str | None = None                 # ISO 8601 timestamp of most recent session
    sessions_completed: int = 0
    root: FeatureNode                               # The feature tree root (app home screen)
    home_signature: ScreenSignature | None = None   # Captured at init for home-reset verification
    sessions: list[SessionSummary] = []             # One entry per completed session


class SessionSummary(BaseModel):
    """Summary of a single exploration session, appended after each session completes."""

    started: str                                    # ISO 8601
    ended: str                                      # ISO 8601
    goals_attempted: int = 0
    goals_completed: int = 0
    goals_failed: int = 0
    nodes_discovered: int = 0
    lessons_recorded: int = 0
    strategy: str = "breadth_first"                 # breadth_first | depth_first


# ── Cross-App (Cluster Mode) ─────────────────────────────────

class CrossAppTrigger(BaseModel):
    """An observed causal edge: acting on this node caused an effect in another app."""

    target_app: str                                 # Package name where effect was observed
    expected_event: str                             # banner_notification | screen_change | element_appeared
                                                    #   | element_disappeared | element_updated
    expected_ui_text: str | None = None             # Text pattern for verification (optional)
    element_identity_key: str | None = None         # For element_updated: stable identity of the affected element
    last_observed_state: dict | None = None         # For element_updated: {"text": "...", "bounds_quadrant": "..."}
    reliability_score: float = 0.0                  # observed_count / attempted_count
    latency_ms: int = 0                             # Median time between trigger action and observed effect
    observed_count: int = 0
    attempted_count: int = 0


class ElementState(BaseModel):
    """Snapshot of a single element's mutable state — used for identity-based diffing."""

    bounds: dict = {}                               # {"left": int, "top": int, "right": int, "bottom": int}
    text: str = ""
    content_desc: str = ""
    checked: bool = False


class ElementDiff(BaseModel):
    """Result of two-tiered identity-based diffing between two UI hierarchy snapshots."""

    appeared: list[dict] = []                       # Elements with new identity keys
    updated: list[dict] = []                        # Same identity key, different ElementState
    disappeared: list[dict] = []                    # Identity keys no longer present
    unchanged: list[dict] = []                      # Same identity, same state

    @property
    def has_changes(self) -> bool:
        return bool(self.appeared or self.updated or self.disappeared)


class ObserverResult(BaseModel):
    """Return value from observe_for_cross_app_event()."""

    detected: bool = False
    event_type: str | None = None                   # Same enum as CrossAppTrigger.expected_event
    new_elements: list[dict] = []                   # Elements with new identity keys (appeared)
    updated_elements: list[dict] = []               # Elements with same identity, different state
    disappeared_elements: list[dict] = []           # Elements whose identity key vanished
    latency_ms: int | None = None                   # Time from baseline capture to first detected change
    snapshot: list[dict] | None = None              # Full UI hierarchy at time of detection


class ExplorationTaskResult(BaseModel):
    """Structured result from running one exploration goal through the agent.

    Wraps the agent's raw output with the UI state captured at task completion,
    which the exploration loop needs to discover children and classify transitions.
    """

    success: bool = False
    final_ui_hierarchy: list[dict] = []             # UI elements on screen when task ended
    final_activity: str | None = None               # Android activity name at task end
    screen_width: int = 1080                        # Device screen width in pixels
    screen_height: int = 2400                       # Device screen height in pixels
    steps_taken: int = 0                            # Number of agent steps executed
    error: str | None = None                        # Error message if task failed
```

### Persistence

```
lessons_dir/
├── com.android.settings/
│   ├── lessons.jsonl           # Lesson-learned entries (existing)
│   ├── _meta.json              # App metadata (existing)
│   └── _exploration.json       # NEW: Feature tree + exploration state
```

The exploration state lives alongside the existing lesson files. This means:
- `--lessons-dir` is the only path the user needs to configure
- Exploration state and lessons are co-located per app
- Existing lesson loading/compaction is unaffected

---

## Exploration Algorithm

### Phase 1: Screen Discovery

When the agent reaches a screen, it extracts navigable elements from the UI hierarchy. This is the **definitive** implementation — it incorporates Issue 1 (list-view sampling + structural IDs) and Issue 2 (dangerous action filtering). The earlier simple version in the Problem section is superseded.

```python
# ── discovery.py ──────────────────────────────────────────────

# Classes that represent toggles, inputs, or other non-navigational widgets.
# These produce state changes, not screen transitions, so we skip them.
TOGGLE_INPUT_CLASSES = {
    "Switch", "ToggleButton", "CheckBox", "RadioButton",
    "EditText", "AutoCompleteTextView", "Spinner",
    "SeekBar", "RatingBar", "CompoundButton",
}

# Labels on UI elements that would trigger destructive/transactional actions.
# Nodes with these labels are created with status="skipped".
SKIP_LABELS = {
    "delete", "remove", "reset", "factory reset", "erase",
    "format", "sign out", "log out", "uninstall",
    "clear data", "clear storage", "clear cache",
    "send", "submit", "confirm purchase", "pay",
}

# Maximum elements to process per screen (matches capture_screen_signature limit)
MAX_ELEMENTS_PER_SCREEN = 30


def discover_features(
    ui_hierarchy: list[dict],
    current_activity: str,
) -> list[FeatureNode]:
    """Extract navigable feature nodes from a screen's UI hierarchy.

    Two-pass approach (Issue 1):
    1. Detect list containers (RecyclerView, ListView). For each, sample
       only 1-2 items. This prevents Gmail-inbox-style explosion.
    2. Discover standalone interactive elements (buttons, menu items)
       outside list containers.

    Uses structural IDs (activity + className + index + bounds quadrant)
    instead of text-based IDs to prevent duplicate nodes for dynamic content.

    Args:
        ui_hierarchy: Flat list of UI element dicts from get_screen_data()
        current_activity: Current Android activity name

    Returns:
        List of FeatureNode with status="pending" (or "skipped" for dangerous labels)
    """
    features = []
    seen_texts: set[str] = set()
    list_containers = _detect_list_containers(ui_hierarchy)

    for i, elem in enumerate(ui_hierarchy):
        if i >= MAX_ELEMENTS_PER_SCREEN:
            break

        text = (elem.get("text") or "").strip()
        if not text or len(text) > 60 or text in seen_texts:
            continue

        clickable = elem.get("clickable", False)
        class_name = elem.get("className", "")

        if _is_toggle_or_input(class_name):
            continue
        if _is_status_bar_element(elem):
            continue

        # Check dangerous labels — create skipped node, don't silently drop
        if _is_dangerous(text):
            seen_texts.add(text)
            features.append(FeatureNode(
                id=_make_structural_id(elem, current_activity),
                label=text,
                nav_action=f"tap('{text}')",
                status="skipped",
                skip_reason="dangerous_label",
            ))
            continue

        # List-view sampling (Issue 1): only take first 2 items per container
        container = _find_parent_container(elem, list_containers)
        if container is not None:
            if container["sampled_count"] >= 2:
                continue
            container["sampled_count"] += 1

        if clickable and text:
            seen_texts.add(text)
            features.append(FeatureNode(
                id=_make_structural_id(elem, current_activity),
                label=text,
                nav_action=f"tap('{text}')",
                status="pending",
                is_list_sample=container is not None,
            ))

    return features


def _is_toggle_or_input(class_name: str) -> bool:
    """Return True if this element's class indicates a toggle, input, or non-navigational widget."""
    return any(tc in class_name for tc in TOGGLE_INPUT_CLASSES)


def _is_status_bar_element(elem: dict) -> bool:
    """Return True if this element is in the Android status bar (top ~25px).

    Status bar elements (clock, battery, signal) are not navigational and
    should be excluded from feature discovery.
    """
    bounds = elem.get("bounds", {})
    top = bounds.get("top", 100)
    bottom = bounds.get("bottom", 100)
    # Status bar is typically 0-66px on modern Android (24dp * 2.75 density)
    return top < 5 and bottom < 75


def _is_dangerous(text: str) -> bool:
    """Return True if tapping this element could cause irreversible damage."""
    return text.lower().strip() in SKIP_LABELS


def _find_parent_container(
    elem: dict,
    list_containers: list[dict],
) -> dict | None:
    """Check if an element is geometrically inside a detected list container.

    Uses bounding-box containment: if the element's bounds are fully within
    a container's bounds, it belongs to that container.
    """
    elem_bounds = elem.get("bounds", {})
    if not elem_bounds:
        return None

    for container in list_containers:
        cb = container["bounds"]
        if (elem_bounds.get("left", 0) >= cb.get("left", 0)
                and elem_bounds.get("top", 0) >= cb.get("top", 0)
                and elem_bounds.get("right", 9999) <= cb.get("right", 9999)
                and elem_bounds.get("bottom", 9999) <= cb.get("bottom", 9999)):
            return container

    return None


def _detect_list_containers(ui_hierarchy: list[dict]) -> list[dict]:
    """Identify RecyclerView/ListView containers for list-view sampling.

    A container is treated as a list if its className matches a known
    list widget. We don't require 3+ children because the hierarchy is
    flattened — we rely on className alone.

    Note: This scans the FULL ui_hierarchy list regardless of element ordering.
    The flattened accessibility tree may place container elements after their
    children. Since _find_parent_container() uses bounding-box containment
    (not parent-child indices), the container's position in the list does not
    matter — as long as it is detected here, its children will be matched
    geometrically during the feature discovery pass.
    """
    LIST_CLASSES = {"RecyclerView", "ListView", "AbsListView"}
    containers = []

    for elem in ui_hierarchy:
        class_name = elem.get("className", "")
        if any(lc in class_name for lc in LIST_CLASSES):
            containers.append({
                "bounds": elem.get("bounds", {}),
                "class": class_name,
                "sampled_count": 0,
            })

    return containers


def _make_structural_id(
    elem: dict,
    activity: str,
    screen_width: int = 1080,
    screen_height: int = 2400,
) -> str:
    """Generate a node ID from structural properties, not text content.

    Uses: activity + className + index-in-parent + bounds quadrant.
    Two emails in the same list position get the same structural ID.

    Screen dimensions are used to compute the quadrant threshold. Default
    values (1080x2400) match the most common Android resolution but callers
    should pass actual device dimensions from ExplorationTaskResult.
    """
    import hashlib

    class_name = elem.get("className", "unknown")
    index = elem.get("index", 0)
    bounds = elem.get("bounds", {})
    center_x = (bounds.get("left", 0) + bounds.get("right", 0)) / 2
    center_y = (bounds.get("top", 0) + bounds.get("bottom", 0)) / 2
    quadrant = f"{'t' if center_y < screen_height / 2 else 'b'}{'l' if center_x < screen_width / 2 else 'r'}"

    raw = f"{activity}:{class_name}:{index}:{quadrant}"
    return hashlib.md5(raw.encode()).hexdigest()[:8]
```

### Phase 2: Goal Generation

For each unvisited node, the exploration planner generates a natural language goal:

```python
def generate_exploration_goal(node: FeatureNode, parent_path: list[str]) -> str:
    """Generate a natural language goal for exploring a feature node.

    The goal tells the agent to navigate to the screen, observe what's there,
    and interact with key elements to understand their function.
    """
    path_description = " > ".join(parent_path + [node.label])

    return (
        f"Navigate to {path_description}. "
        f"Once there, observe all available options and interactive elements on the screen. "
        f"Do not change any settings or trigger any destructive actions. "
        f"Identify what actions are available (buttons, menu items, toggles, links)."
    )
```

### Phase 3: Exploration Loop

This section references `navigate_to_app_home()` and `classify_screen_transition()` which are defined later in the document (see "Issue 3: Hard Reset Fallback" and "Issue 4: Modal and Overlay Detection" respectively). Both are part of `runner.py` — they appear later for narrative flow but are called within the exploration loop below.

#### Agent Task Wrapper

The exploration loop needs to run the existing agent and capture the final UI state. This wrapper bridges between the exploration planner and the existing `Agent.run_task()` API:

```python
# ── runner.py ─────────────────────────────────────────────────

from mineru.ui_auto.sdk.agent import Agent
from mineru.ui_auto.sdk.builders import AgentConfigBuilder
from mineru.ui_auto.controllers.controller_factory import create_device_controller
from mineru.ui_auto.controllers.platform_specific_commands_controller import (
    get_current_foreground_package_async,
)


async def run_exploration_task(
    goal: str,
    app_package: str,
    ctx: MobileUseContext,
    agent: Agent,
    max_steps: int = 20,
) -> ExplorationTaskResult:
    """Run a single exploration goal through the existing agent graph.

    Wraps Agent.run_task() to:
    1. Lock the agent to the target app
    2. Cap the recursion limit (max_steps)
    3. Capture the final UI hierarchy after the agent finishes
    4. Return a structured ExplorationTaskResult

    The Agent instance is created once per session (in run_exploration_session)
    and reused across goals. This avoids re-initializing LLM connections per goal.
    """
    controller = create_device_controller(ctx)

    try:
        # Run the agent with app lock and step cap.
        # Build a TaskRequest using the SDK builder pattern, then pass it
        # to run_task(). The builder sets locked_app_package and max_steps
        # on the request object — do NOT also pass them as kwargs.
        request = (
            agent.new_task(goal)
                .with_locked_app_package(app_package)
                .with_max_steps(max_steps)
                .build()
        )
        await agent.run_task(request=request)

        # Capture the screen state after the agent finishes.
        # This is what we feed into discover_features() to find children.
        screen_data = await controller.get_screen_data()
        current_activity_info = await get_current_foreground_package_async(ctx)

        return ExplorationTaskResult(
            success=True,
            final_ui_hierarchy=screen_data.elements if screen_data else [],
            final_activity=current_activity_info,
            screen_width=screen_data.width if screen_data else 1080,
            screen_height=screen_data.height if screen_data else 2400,
        )

    except Exception as e:
        return ExplorationTaskResult(
            success=False,
            error=str(e),
        )
```

#### Session Lifecycle

```python
async def create_exploration_agent(
    ctx: MobileUseContext,
    lessons_dir: Path,
) -> Agent:
    """Create and initialize an Agent instance configured for exploration.

    The agent is created once per session and reused for all goals.
    exploration_mode=True on the context enables the tool-level safety guard.
    """
    ctx.exploration_mode = True
    ctx.lessons_dir = lessons_dir

    config = (
        AgentConfigBuilder()
        .with_lessons_dir(lessons_dir)
        .build()
    )
    agent = Agent(config=config)
    await agent.init(ctx=ctx)
    return agent


def _select_strategy(state: ExplorationState) -> str:
    """Auto-select BFS or DFS based on session history.

    - First session (no prior sessions): BFS to map the top-level structure.
    - Subsequent sessions: DFS to fill in depth within sections.
    """
    if state.sessions_completed == 0:
        return "breadth_first"
    return "depth_first"


async def run_exploration_session(
    app_package: str,
    lessons_dir: Path,
    ctx: MobileUseContext,
    budget_minutes: int = 30,
    max_depth: int = 4,
    strategy: str = "auto",
):
    """Run one exploration session for an app.

    Each session:
    1. Loads or initializes the feature tree
    2. Creates an Agent instance (reused across all goals)
    3. Auto-selects strategy (BFS first session, DFS later) unless overridden
    4. Picks the next unvisited node
    5. Generates a goal and runs the agent
    6. Captures the final UI state and discovers children
    7. Updates the tree and saves after each goal
    8. Repeats until budget exhausted or all nodes explored
    """
    state = load_exploration_state(lessons_dir, app_package)
    if state is None:
        state = await initialize_exploration(app_package, ctx, lessons_dir)

    if strategy == "auto":
        strategy = _select_strategy(state)

    agent = await create_exploration_agent(ctx, lessons_dir)

    controller = create_device_controller(ctx)

    start_time = time.monotonic()
    goals_completed = 0
    goals_failed = 0

    # Track lessons recorded by counting lines in lessons.jsonl before/after
    lessons_path = lessons_dir / app_package / "lessons.jsonl"
    lessons_before = _count_lines(lessons_path)

    while True:
        elapsed = (time.monotonic() - start_time) / 60
        if elapsed >= budget_minutes:
            logger.info(f"Session budget exhausted ({budget_minutes}m). "
                       f"Completed {goals_completed} goals.")
            break

        node, parent_path = pick_next_node(state.root, max_depth, strategy)
        if node is None:
            logger.info("All reachable nodes explored!")
            break

        node.status = "in_progress"
        save_exploration_state(state, lessons_dir, app_package)

        goal = generate_exploration_goal(node, parent_path)
        logger.info(f"Exploring: {' > '.join(parent_path + [node.label])}")

        # Capture the current screen BEFORE dispatching the goal.
        # This baseline is needed for modal detection (classify_screen_transition)
        # and passive event diffing.
        parent_screen_data = await controller.get_screen_data()
        parent_hierarchy = parent_screen_data.elements if parent_screen_data else []

        result = await run_exploration_task(
            goal=goal,
            app_package=app_package,
            ctx=ctx,
            agent=agent,
            max_steps=20,
        )

        if result.success and result.final_ui_hierarchy:
            # Classify the transition to handle modals vs full-screen nav
            transition_type = classify_screen_transition(
                parent_hierarchy=parent_hierarchy,
                current_hierarchy=result.final_ui_hierarchy,
                screen_width=result.screen_width,
                screen_height=result.screen_height,
            )

            if transition_type == "modal":
                node.node_type = "modal"
                node.dismiss_action = "back"
                modal_elements = _extract_new_elements(
                    parent_hierarchy, result.final_ui_hierarchy,
                )
                children = discover_features(modal_elements, result.final_activity or "")
            else:
                children = discover_features(
                    result.final_ui_hierarchy,
                    result.final_activity or "",
                )

            current_depth = len(parent_path) + 1
            if current_depth >= max_depth:
                for child in children:
                    child.status = "deep_limit"
            node.children = _merge_children(node.children, children)
            node.status = "explored"
            goals_completed += 1
        else:
            node.attempt_count += 1
            if node.attempt_count >= 3:
                node.status = "failed"
            else:
                node.status = "pending"
            goals_failed += 1

        await navigate_to_app_home(ctx, app_package, state.home_signature)
        save_exploration_state(state, lessons_dir, app_package)

    # Record session summary
    stats = compute_tree_stats(state.root)
    lessons_after = _count_lines(lessons_path)
    state.sessions.append(SessionSummary(
        started=state.last_session or datetime.now(UTC).isoformat(),
        ended=datetime.now(UTC).isoformat(),
        goals_attempted=goals_completed + goals_failed,
        goals_completed=goals_completed,
        goals_failed=goals_failed,
        nodes_discovered=stats.total,
        lessons_recorded=lessons_after - lessons_before,
        strategy=strategy,
    ))
    state.sessions_completed += 1
    state.last_session = datetime.now(UTC).isoformat()
    save_exploration_state(state, lessons_dir, app_package)

    logger.info(
        f"Session complete. Tree: {stats.total} nodes, "
        f"{stats.explored} explored, {stats.pending} pending, "
        f"{stats.failed} failed."
    )
```

#### Merge and Stats Helpers

```python
# ── helpers used by the exploration loop ──────────────────────

def _merge_children(
    existing: list[FeatureNode],
    discovered: list[FeatureNode],
) -> list[FeatureNode]:
    """Merge newly discovered children with existing children.

    Strategy: match by structural ID. If a discovered child has the same ID
    as an existing child, keep the existing one (preserves status, attempt_count,
    children from prior sessions). New IDs are appended.

    This is critical for multi-session support: session 2 re-discovers the same
    screen elements as session 1, and we must not create duplicates.
    """
    existing_ids = {child.id: child for child in existing}
    merged = list(existing)  # Start with all existing children

    for child in discovered:
        if child.id not in existing_ids:
            merged.append(child)
        # else: keep existing node (it may have status="explored" from prior session)

    return merged


@dataclass
class TreeStats:
    total: int = 0
    explored: int = 0
    pending: int = 0
    failed: int = 0
    skipped: int = 0
    deep_limit: int = 0


def compute_tree_stats(node: FeatureNode) -> TreeStats:
    """Recursively compute exploration statistics for the feature tree."""
    stats = TreeStats()

    def _walk(n: FeatureNode):
        stats.total += 1
        if n.status == "explored":
            stats.explored += 1
        elif n.status == "pending":
            stats.pending += 1
        elif n.status == "failed":
            stats.failed += 1
        elif n.status == "skipped":
            stats.skipped += 1
        elif n.status == "deep_limit":
            stats.deep_limit += 1
        for child in n.children:
            _walk(child)

    _walk(node)
    return stats


def _count_lines(path: Path) -> int:
    """Count lines in a file. Returns 0 if the file does not exist.

    Used to compute lessons_recorded by diffing line counts before/after a session.
    """
    if not path.exists():
        return 0
    return sum(1 for _ in path.open())
```

### Node Selection Strategy

The order in which nodes are explored matters. Two strategies, auto-selected based on session history (see `_select_strategy()` above):

**Breadth-first (first session):**
Explores all top-level menu items before going deeper. This gives the broadest coverage fastest — after one session, the agent knows every main section of the app.

**Depth-first (subsequent sessions):**
Dives deep into one section at a time. After breadth-first has mapped the top level, depth-first fills in the details. This produces complete navigation paths for specific feature areas.

```python
def pick_next_node(
    root: FeatureNode,
    max_depth: int,
    strategy: str = "breadth_first",
) -> tuple[FeatureNode | None, list[str]]:
    """Pick the next unvisited node to explore.

    Returns (node, parent_path) or (None, []) if all nodes are visited.
    """
    if strategy == "breadth_first":
        queue = deque([(root, [])])
        while queue:
            node, path = queue.popleft()
            if node.status == "pending" and len(path) < max_depth:
                return node, path
            for child in node.children:
                queue.append((child, path + [node.label]))
    else:
        def _dfs(node, path) -> tuple[FeatureNode | None, list[str]]:
            if node.status == "pending" and len(path) < max_depth:
                return node, path
            for child in node.children:
                found_node, found_path = _dfs(child, path + [node.label])
                if found_node is not None:
                    return found_node, found_path
            return None, []
        return _dfs(root, [])

    return None, []
```

---

## Safety Guards

Self-learning must be non-destructive. The agent is exploring, not testing — it should observe, not modify.

### 1. Read-Only Exploration Goal Phrasing

Every generated goal includes: *"Do not change any settings or trigger any destructive actions."*

This instructs the Cortex to observe and navigate but not toggle, delete, or submit. However, this is a **soft constraint only** — see Guard 6 for the hard enforcement layer.

### 2. Dangerous Action Blocklist (Discovery Layer)

Certain UI elements are skipped during feature discovery:

```python
SKIP_LABELS = {
    "delete", "remove", "reset", "factory reset", "erase",
    "format", "sign out", "log out", "uninstall",
    "clear data", "clear storage", "clear cache",
    "send", "submit", "confirm purchase", "pay",
}

def _is_dangerous(text: str) -> bool:
    return text.lower().strip() in SKIP_LABELS
```

Nodes with dangerous labels get `status = "skipped"` and are never explored.

### 3. Locked App Mode

**Single-app mode:** Self-learning uses the existing app-lock feature to prevent the agent from leaving the target app:

```python
task.with_locked_app_package(app_package)
```

If the agent accidentally navigates away (e.g., taps a deep link), the Contextor's app-lock verification relaunches the correct app.

**Cluster mode:** Each agent is locked to its own app. The Master Orchestrator manages cross-app coordination — individual agents never switch apps themselves. If the Primary Agent's action causes a system notification visible on the primary device, the notification is noted but not tapped (that's the Secondary Agent's domain).

### 4. Step Limit Per Goal

Each exploration goal has a `max_steps=20` cap. If the agent can't reach the target screen in 20 steps, the goal fails and is retried later. This prevents infinite loops on unreachable screens.

### 5. Authentication Barrier Detection

Some screens require login/auth (e.g., "Sign in to access this feature"). The agent detects this heuristically:

```python
AUTH_INDICATORS = [
    "sign in", "log in", "enter password", "enter pin",
    "verify your identity", "authentication required",
    "create account",
]

def _is_auth_barrier(ui_hierarchy: list[dict]) -> bool:
    texts = [elem.get("text", "").lower() for elem in ui_hierarchy[:50]]
    combined = " ".join(texts)
    return any(indicator in combined for indicator in AUTH_INDICATORS)
```

When detected, the node is marked `skipped` with reason `"auth_required"`. The user can optionally pre-authenticate before running self-learning.

### 6. Tool-Level Action Guard (Hard Enforcement)

The prompt-based safety (Guard 1) and discovery blocklist (Guard 2) are soft/medium constraints. **Guard 6 is the hard enforcement layer**: when `exploration_mode=True` on `MobileUseContext`, the tap and press_key tool wrappers check targets against `EXPLORATION_BLOCKED_ACTIONS` and return a system error that the LLM cannot override. See "Issue 2: Tool-Level Action Guard" below for full details.

### 7. Home Reset with Force-Stop Fallback

Between exploration goals, the agent resets to the app's home screen. If graceful back-navigation fails (modal traps, deep WebViews, system dialogs), the runner falls back to `am force-stop` + cold relaunch. See "Issue 3: Hard Reset Fallback" below for full details.

---

## Observer Mode (Cluster Mode Only)

When the Master Orchestrator detects that a Primary Agent action may have cross-app effects, it switches the Secondary Agent into **Observer Mode** — a passive polling loop that watches for UI changes without taking any actions.

### When Observer Mode Activates

The Master Orchestrator triggers Observer Mode when:
1. The Primary Agent completes an action on a node that has known `cross_app_triggers` (from prior sessions)
2. The Primary Agent taps an element whose label matches a configurable trigger-hint list (e.g., "send", "share", "enable", "notify", "invite")
3. Manually configured trigger points in the cluster config

### Observer Mode Loop

```python
# ── observer.py ───────────────────────────────────────────────

# Minimum time to observe after first detecting a change.
# This prevents the observer from returning after catching only a timestamp
# update while missing the real event (e.g., avatar movement) that arrives 2s later.
SETTLE_WINDOW_SECONDS = 2.0


async def observe_for_cross_app_event(
    ctx: MobileUseContext,
    app_package: str,
    baseline_hierarchy: list[dict],
    current_activity: str,
    timeout_seconds: float = 10.0,
    poll_interval_seconds: float = 0.5,
) -> ObserverResult:
    """Poll the secondary app's UI for state changes triggered by the primary app.

    Uses two-tiered diffing: first checks element identity (who changed),
    then element state (what changed). This prevents dynamic elements like
    map avatars from creating duplicate events on every position update.

    After detecting the first change, continues polling for SETTLE_WINDOW_SECONDS
    to aggregate related changes (e.g., avatar move + status text change + timestamp
    update that arrive within a 2-second burst). Returns the final aggregated diff.

    Args:
        ctx: Device context for the secondary app's device
        app_package: Secondary app package to observe
        baseline_hierarchy: UI hierarchy snapshot taken before the primary action
        current_activity: Current activity name for identity key computation
        timeout_seconds: How long to wait for the FIRST change before giving up
        poll_interval_seconds: How frequently to poll the UI hierarchy

    Returns:
        ObserverResult with detected changes, timing, and classification
    """
    controller = create_device_controller(ctx)
    start_time = time.monotonic()

    baseline_index = _build_identity_index(baseline_hierarchy, current_activity)

    first_change_time: float | None = None
    latest_diff: ElementDiff | None = None
    latest_hierarchy: list[dict] | None = None

    while True:
        elapsed = time.monotonic() - start_time

        # Timeout: no change detected within the window
        if first_change_time is None and elapsed >= timeout_seconds:
            break

        # Settle window: enough time has passed since first change
        if first_change_time is not None:
            since_first = time.monotonic() - first_change_time
            if since_first >= SETTLE_WINDOW_SECONDS:
                break

        await asyncio.sleep(poll_interval_seconds)

        try:
            screen_data = await controller.get_screen_data()
            if screen_data is None or not screen_data.elements:
                continue  # Device temporarily unavailable — skip this poll cycle
            current_hierarchy = screen_data.elements
        except Exception:
            logger.warning("get_screen_data() failed during Observer Mode poll, skipping cycle")
            continue

        current_index = _build_identity_index(current_hierarchy, current_activity)

        diff = _compute_identity_diff(baseline_index, current_index)

        if diff.has_changes:
            if first_change_time is None:
                first_change_time = time.monotonic()
            # Always keep the latest diff — it accumulates all changes since baseline
            latest_diff = diff
            latest_hierarchy = current_hierarchy

    if latest_diff is not None and first_change_time is not None:
        elapsed_ms = int((first_change_time - start_time) * 1000)
        event_type = classify_passive_event(latest_diff, latest_hierarchy or [])

        return ObserverResult(
            detected=True,
            event_type=event_type,
            new_elements=latest_diff.appeared,
            updated_elements=latest_diff.updated,
            disappeared_elements=latest_diff.disappeared,
            latency_ms=elapsed_ms,
            snapshot=latest_hierarchy,
        )

    return ObserverResult()


def _build_identity_index(
    hierarchy: list[dict],
    activity: str,
) -> dict[str, ElementState]:
    """Build an index mapping identity keys to element state.

    The identity key captures WHO the element is (stable across state changes).
    The ElementState captures WHAT its current state is (bounds, text, icon).
    """
    index = {}
    for elem in hierarchy:
        identity_key = _compute_element_identity_key(elem, activity)
        state = ElementState(
            bounds=elem.get("bounds", {}),
            text=(elem.get("text") or "").strip(),
            content_desc=(elem.get("content-desc") or "").strip(),
            checked=elem.get("checked", False),
        )
        index[identity_key] = state
    return index


    # ElementDiff is defined in types.py (see Type Definitions section above).
    # Do not redefine it here — import from mineru.ui_auto.exploration.types.


def _compute_identity_diff(
    baseline: dict[str, ElementState],
    current: dict[str, ElementState],
) -> ElementDiff:
    """Diff two hierarchy snapshots by element identity, not by bounds.

    This is the core anti-bloat mechanism for dynamic UIs:
    - A map avatar that moved from (100,200) to (150,250) has the SAME identity key
      (same resource-id), so it's classified as 'updated' not 'appeared'.
    - A new push notification banner has a NEW identity key, so it's 'appeared'.
    """
    baseline_keys = set(baseline.keys())
    current_keys = set(current.keys())

    appeared_keys = current_keys - baseline_keys
    disappeared_keys = baseline_keys - current_keys
    common_keys = baseline_keys & current_keys

    updated = []
    unchanged = []
    for key in common_keys:
        if baseline[key] != current[key]:
            updated.append({"identity_key": key, "old_state": baseline[key], "new_state": current[key]})
        else:
            unchanged.append({"identity_key": key})

    return ElementDiff(
        appeared=[{"identity_key": k, "state": current[k]} for k in appeared_keys],
        updated=updated,
        disappeared=[{"identity_key": k, "state": baseline[k]} for k in disappeared_keys],
        unchanged=unchanged,
    )
```

### Shared Helpers

These utility functions are referenced across observer, classifier, and orchestrator modules:

```python
# ── helpers.py ────────────────────────────────────────────────

import statistics


def _extract_key_text(elements: list[dict] | list) -> str:
    """Extract the most representative text from a list of elements.

    Used to label passive_event nodes and populate expected_ui_text in triggers.
    Returns the longest non-empty text among the first 5 elements.
    """
    texts = []
    for elem in elements[:5]:
        # Handle both raw element dicts and ElementState objects
        if isinstance(elem, dict):
            text = elem.get("text", "") or elem.get("state", {}).get("text", "")
        else:
            text = getattr(elem, "text", "")
        text = text.strip()
        if text:
            texts.append(text)

    if not texts:
        return "unknown"
    return max(texts, key=len)


def _quantize_bounds(
    bounds: dict,
    screen_width: int = 1080,
    screen_height: int = 2400,
) -> str:
    """Convert exact pixel bounds to a coarse quadrant label.

    Used for last_observed_state in cross-app triggers. Coarse enough
    that small pixel shifts don't produce different values.

    Screen dimensions default to 1080x2400 but callers should pass actual
    device dimensions when available. Returns one of:
    "top-left", "top-center", "top-right",
    "center-left", "center", "center-right",
    "bottom-left", "bottom-center", "bottom-right"
    """
    center_x = (bounds.get("left", 0) + bounds.get("right", screen_width)) / 2
    center_y = (bounds.get("top", 0) + bounds.get("bottom", screen_height)) / 2

    third_x = screen_width / 3
    third_y = screen_height / 3
    col = "left" if center_x < third_x else ("right" if center_x > 2 * third_x else "center")
    row = "top" if center_y < third_y else ("bottom" if center_y > 2 * third_y else "center")

    if row == "center" and col == "center":
        return "center"
    return f"{row}-{col}"


def _update_median(existing_median: int, new_value: int) -> int:
    """Approximate a running median using exponential moving average.

    True running median requires storing all values. This approximation
    is good enough for latency tracking — we care about magnitude, not precision.
    """
    if existing_median == 0:
        return new_value
    # EMA with alpha=0.3 gives more weight to recent observations
    return int(existing_median * 0.7 + new_value * 0.3)


def _compute_coverage(elements: list, screen_width: int = 1080, screen_height: int = 2400) -> float:
    """Compute what fraction of the screen area is covered by the union bounding box of elements.

    Used by classify_screen_transition() for modal detection (coverage < 0.80)
    and by classify_passive_event() for screen_change detection (coverage > 0.80).
    """
    if not elements:
        return 0.0

    screen_area = screen_width * screen_height
    if screen_area == 0:
        return 0.0

    # Extract bounds — handle both raw dicts and ElementState objects
    all_bounds = []
    for e in elements:
        if isinstance(e, dict):
            b = e.get("bounds", {})
        else:
            b = getattr(e, "bounds", {})
        if b:
            all_bounds.append(b)

    if not all_bounds:
        return 0.0

    min_x = min(b.get("left", 0) for b in all_bounds)
    min_y = min(b.get("top", 0) for b in all_bounds)
    max_x = max(b.get("right", screen_width) for b in all_bounds)
    max_y = max(b.get("bottom", screen_height) for b in all_bounds)

    union_area = (max_x - min_x) * (max_y - min_y)
    return union_area / screen_area


def _find_child_by_identity(
    parent_node: FeatureNode,
    identity_key: str,
) -> FeatureNode | None:
    """Find an existing child node that matches the given identity key.

    Used for upserting dynamic_element nodes — if a child with this identity
    already exists, we update it instead of creating a duplicate.
    """
    for child in parent_node.children:
        if child.identity_key == identity_key:
            return child
    return None
```

### Passive Event Classification

UI changes that occur without a preceding local action must be classified differently from navigation children. The classifier now distinguishes **genuinely new elements** from **existing elements that updated their state** — critical for live maps, status indicators, and any continuously-updating UI.

```python
def classify_passive_event(
    diff: ElementDiff,
    full_hierarchy: list[dict],
) -> str:
    """Classify a UI change that occurred without a local agent action.

    Uses identity-based diff to distinguish new elements from updated ones:

    Returns one of:
    - "banner_notification": top-of-screen transient banner (push notification, toast)
    - "screen_change": full-screen content replacement (e.g., redirect after remote action)
    - "element_appeared": genuinely new UI element appeared (new identity key)
    - "element_updated": existing element changed state (same identity key, different bounds/text)
    - "element_disappeared": existing element was removed
    - "unknown": could not classify
    """
    # Priority 1: If elements updated but none appeared/disappeared,
    # this is a state mutation (map movement, status change, timer tick)
    if diff.updated and not diff.appeared and not diff.disappeared:
        return "element_updated"

    # Priority 2: If no elements have new identity keys, nothing genuinely new
    if not diff.appeared:
        if diff.disappeared:
            return "element_disappeared"
        if diff.updated:
            return "element_updated"
        return "unknown"

    # From here, we have genuinely new elements (new identity keys)
    new_elements = [e["state"] for e in diff.appeared]

    # Priority 3: Check for top-banner pattern
    top_elements = [e for e in diff.appeared
                    if e["state"].bounds.get("top", 999) < 200]
    if len(top_elements) > 0 and len(top_elements) >= len(diff.appeared) * 0.5:
        return "banner_notification"

    # Priority 4: Check for full-screen change
    total_area = _compute_coverage(new_elements)
    if total_area > 0.80:
        return "screen_change"

    # Priority 5: Mixed scenario — both new and updated elements
    # If updates dominate (>= 2x appeared count), the primary event is an update
    if diff.updated and len(diff.updated) >= len(diff.appeared) * 2:
        return "element_updated"

    return "element_appeared"
```

### Cross-App Trigger Recording

When Observer Mode detects a change, the Master Orchestrator records the relationship. For `element_updated` events, it uses **upsert** semantics — updating the existing trigger's state rather than appending a new entry. This prevents tree bloat from dynamic elements like map avatars:

```python
def record_cross_app_trigger(
    primary_node: FeatureNode,
    secondary_app: str,
    observer_result: ObserverResult,
):
    """Record a cross-app trigger edge on the primary node.

    Called by the Master Orchestrator after Observer Mode detects a
    state change on the secondary app following a primary app action.

    For 'element_updated' events, the match key includes the element's
    identity key — so "Child Avatar moved" always upserts the same trigger
    entry regardless of how many times the avatar moves.
    """
    # Build the match key: for element_updated, include identity key
    # to ensure one trigger entry per logical element, not per observation
    identity_key = None
    if observer_result.event_type == "element_updated" and observer_result.updated_elements:
        identity_key = observer_result.updated_elements[0].get("identity_key")

    existing = _find_matching_trigger(
        primary_node.cross_app_triggers,
        secondary_app,
        observer_result.event_type,
        identity_key,
    )

    if existing:
        existing.attempted_count += 1
        if observer_result.detected:
            existing.observed_count += 1
            existing.latency_ms = _update_median(existing.latency_ms, observer_result.latency_ms)
        existing.reliability_score = existing.observed_count / existing.attempted_count

        # Upsert state for dynamic elements — overwrite, don't append
        if observer_result.event_type == "element_updated" and observer_result.updated_elements:
            latest = observer_result.updated_elements[0]
            existing.last_observed_state = {
                "text": latest["new_state"].text,
                "bounds_quadrant": _quantize_bounds(latest["new_state"].bounds),
            }
    else:
        trigger = CrossAppTrigger(
            target_app=secondary_app,
            expected_event=observer_result.event_type,
            expected_ui_text=_extract_key_text(observer_result.new_elements),
            element_identity_key=identity_key,
            last_observed_state=None,
            reliability_score=1.0 if observer_result.detected else 0.0,
            latency_ms=observer_result.latency_ms or 0,
            observed_count=1 if observer_result.detected else 0,
            attempted_count=1,
        )
        # For element_updated, capture the initial state snapshot
        if observer_result.event_type == "element_updated" and observer_result.updated_elements:
            latest = observer_result.updated_elements[0]
            trigger.last_observed_state = {
                "text": latest["new_state"].text,
                "bounds_quadrant": _quantize_bounds(latest["new_state"].bounds),
            }
        primary_node.cross_app_triggers.append(trigger)


def _find_matching_trigger(
    triggers: list[CrossAppTrigger],
    target_app: str,
    event_type: str,
    identity_key: str | None,
) -> CrossAppTrigger | None:
    """Find an existing trigger entry that matches this observation.

    For element_updated events, matches on (target_app, event_type, identity_key)
    so the same logical element always maps to the same trigger.
    For other events, matches on (target_app, event_type) only.
    """
    for t in triggers:
        if t.target_app != target_app or t.expected_event != event_type:
            continue
        if event_type == "element_updated":
            if t.element_identity_key == identity_key:
                return t
        else:
            return t
    return None
```

### Master Orchestrator Coordination Flow

**Example 1: Banner notification (discrete event)**
```
Primary Agent                    Master Orchestrator              Secondary Agent
─────────────                    ───────────────────              ───────────────
Execute goal:                                                     (idle or own goal)
  tap('Enable Screen Time')
  → action completes            ← notified of action
                                 1. Capture secondary baseline   → snapshot UI hierarchy
                                 2. Pause primary
                                 3. Start Observer Mode          → poll for 10s
                                    ...
                                    (2.5s later, banner appears)
                                 4. Identity diff: new key       → classify as banner_notification
                                 5. Record cross_app_trigger     → element_identity_key=null
                                    on primary node
                                 6. Record passive_event node
                                    in secondary tree
                                 7. Resume primary               → resume own goals
Continue next goal →
```

**Example 2: Map avatar update (continuous/dynamic element)**
```
Primary Agent                    Master Orchestrator              Secondary Agent
─────────────                    ───────────────────              ───────────────
Execute goal:                                                     (viewing live map)
  tap('Start Driving')
  → action completes            ← notified of action
                                 1. Capture secondary baseline   → snapshot map UI hierarchy
                                 2. Pause primary                   (avatar at bounds X, text "Stationary")
                                 3. Start Observer Mode          → poll for 10s
                                    ...
                                    (3.2s later, avatar moves + text changes)
                                 4. Identity diff:               → same identity key (resource-id:
                                    child_avatar key MATCHED         child_avatar), different state
                                    in baseline                  → classify as element_updated
                                 5. Upsert cross_app_trigger:   → element_identity_key =
                                    find existing trigger for       "...MapActivity:child_avatar"
                                    this identity_key,           → last_observed_state =
                                    update last_observed_state      {text: "Driving", quadrant: "center"}
                                 6. Upsert dynamic_element       → find existing node by identity_key,
                                    node in secondary tree          update label (no new node created)
                                 7. Resume primary               → resume own goals
Continue next goal →
```

**Key difference:** In Example 2, if the avatar moves 10 times during observation, the identity-based diff detects the same `child_avatar` key each time. The trigger entry and tree node are **upserted** — one trigger, one node, regardless of movement frequency.

### Master Orchestrator Implementation

The orchestrator is the central coordinator for cluster mode. It manages two independent agents, intercepts trigger-worthy actions, and dispatches Observer Mode.

```python
# ── orchestrator.py ───────────────────────────────────────────

# Labels that suggest an action may have cross-app effects.
# Matched against the nav_action label of each exploration goal.
DEFAULT_TRIGGER_HINTS = {
    "send", "share", "enable", "disable", "notify", "invite",
    "block", "allow", "start", "stop", "approve", "deny",
    "lock", "unlock", "restrict", "permit",
}


class MasterOrchestrator:
    """Coordinates two Exploration Planners for Parent/Child app testing.

    The orchestrator does NOT modify how each agent explores. It wraps
    the exploration loop with trigger detection and Observer Mode dispatch.

    Architecture:
    - Each agent runs its own run_exploration_session() loop independently.
    - The orchestrator intercepts after each goal completion to check for
      trigger-worthy actions and dispatch Observer Mode on the other agent.
    - Agents are unaware of each other. Only the orchestrator sees both trees.
    """

    def __init__(
        self,
        primary_app: str,
        secondary_app: str,
        primary_ctx: MobileUseContext,
        secondary_ctx: MobileUseContext,
        lessons_dir: Path,
        observer_timeout: float = 10.0,
        trigger_hints: set[str] | None = None,
    ):
        self.primary_app = primary_app
        self.secondary_app = secondary_app
        self.primary_ctx = primary_ctx
        self.secondary_ctx = secondary_ctx
        self.lessons_dir = lessons_dir
        self.observer_timeout = observer_timeout
        self.trigger_hints = trigger_hints or DEFAULT_TRIGGER_HINTS

        # State loaded/created during init
        self.primary_state: ExplorationState | None = None
        self.secondary_state: ExplorationState | None = None
        self.primary_agent: Agent | None = None
        self.secondary_agent: Agent | None = None

    async def init(self):
        """Initialize both agents and load/create exploration states."""
        self.primary_agent = await create_exploration_agent(
            self.primary_ctx, self.lessons_dir,
        )
        self.secondary_agent = await create_exploration_agent(
            self.secondary_ctx, self.lessons_dir,
        )

        self.primary_state = load_exploration_state(
            self.lessons_dir, self.primary_app,
        ) or await initialize_exploration(
            self.primary_app, self.primary_ctx, self.lessons_dir,
        )

        self.secondary_state = load_exploration_state(
            self.lessons_dir, self.secondary_app,
        ) or await initialize_exploration(
            self.secondary_app, self.secondary_ctx, self.lessons_dir,
        )

    async def run_cluster_session(
        self,
        budget_minutes: int = 45,
        max_depth: int = 4,
    ):
        """Run one cluster exploration session.

        Strategy:
        1. Primary agent explores one goal at a time.
        2. After each goal, check if the action might trigger a cross-app effect.
        3. If yes, snapshot the secondary's UI, wait with Observer Mode, record triggers.
        4. Then let the secondary agent explore one goal.
        5. Alternate until budget exhausted.

        This interleaved approach ensures both apps get explored while
        cross-app effects are captured at trigger points.
        """
        start_time = time.monotonic()
        primary_strategy = _select_strategy(self.primary_state)
        secondary_strategy = _select_strategy(self.secondary_state)

        while (time.monotonic() - start_time) / 60 < budget_minutes:
            # ── Primary agent: one goal ──
            primary_node, primary_path = pick_next_node(
                self.primary_state.root, max_depth, primary_strategy,
            )
            if primary_node:
                await self._explore_one_goal(
                    node=primary_node,
                    parent_path=primary_path,
                    app_package=self.primary_app,
                    ctx=self.primary_ctx,
                    agent=self.primary_agent,
                    state=self.primary_state,
                )

                # Check for cross-app trigger
                if self._should_observe(primary_node):
                    await self._observe_cross_app_effect(
                        trigger_node=primary_node,
                        trigger_app=self.primary_app,
                        observer_app=self.secondary_app,
                        observer_ctx=self.secondary_ctx,
                        observer_state=self.secondary_state,
                    )

            # ── Secondary agent: one goal ──
            secondary_node, secondary_path = pick_next_node(
                self.secondary_state.root, max_depth, secondary_strategy,
            )
            if secondary_node:
                await self._explore_one_goal(
                    node=secondary_node,
                    parent_path=secondary_path,
                    app_package=self.secondary_app,
                    ctx=self.secondary_ctx,
                    agent=self.secondary_agent,
                    state=self.secondary_state,
                )

                if self._should_observe(secondary_node):
                    await self._observe_cross_app_effect(
                        trigger_node=secondary_node,
                        trigger_app=self.secondary_app,
                        observer_app=self.primary_app,
                        observer_ctx=self.primary_ctx,
                        observer_state=self.primary_state,
                    )

            # Both exhausted?
            if primary_node is None and secondary_node is None:
                logger.info("Both apps fully explored.")
                break

        # Save final states
        save_exploration_state(self.primary_state, self.lessons_dir, self.primary_app)
        save_exploration_state(self.secondary_state, self.lessons_dir, self.secondary_app)

    async def _explore_one_goal(
        self,
        node: FeatureNode,
        parent_path: list[str],
        app_package: str,
        ctx: MobileUseContext,
        agent: Agent,
        state: ExplorationState,
    ):
        """Execute a single exploration goal — same logic as the single-app loop."""
        node.status = "in_progress"
        goal = generate_exploration_goal(node, parent_path)
        logger.info(f"[{app_package}] Exploring: {' > '.join(parent_path + [node.label])}")

        result = await run_exploration_task(
            goal=goal,
            app_package=app_package,
            ctx=ctx,
            agent=agent,
        )

        if result.success and result.final_ui_hierarchy:
            children = discover_features(result.final_ui_hierarchy, result.final_activity or "")
            node.children = _merge_children(node.children, children)
            node.status = "explored"
        else:
            node.attempt_count += 1
            node.status = "failed" if node.attempt_count >= 3 else "pending"

        await navigate_to_app_home(ctx, app_package, state.home_signature)
        save_exploration_state(state, self.lessons_dir, app_package)

    def _should_observe(self, node: FeatureNode) -> bool:
        """Decide whether this node's action might trigger a cross-app effect.

        Three signals:
        1. The node already has known cross_app_triggers (from prior sessions).
        2. The node's label contains a trigger-hint keyword.
        3. (Future) The node is in a manually configured trigger list.
        """
        if node.cross_app_triggers:
            return True

        label_lower = node.label.lower()
        return any(hint in label_lower for hint in self.trigger_hints)

    async def _observe_cross_app_effect(
        self,
        trigger_node: FeatureNode,
        trigger_app: str,
        observer_app: str,
        observer_ctx: MobileUseContext,
        observer_state: ExplorationState,
    ):
        """Dispatch Observer Mode on the observer app after a trigger action.

        1. Snapshot the observer app's current UI as baseline.
        2. Run the observer polling loop.
        3. If a change is detected, record a cross-app trigger on the trigger node
           and upsert a passive_event/dynamic_element node in the observer tree.
        """
        controller = create_device_controller(observer_ctx)
        baseline = (await controller.get_screen_data()).elements
        activity = await get_current_foreground_package_async(observer_ctx) or ""

        logger.info(f"[{trigger_app} → {observer_app}] Observer Mode: "
                    f"watching for cross-app effect after '{trigger_node.label}'")

        result = await observe_for_cross_app_event(
            ctx=observer_ctx,
            app_package=observer_app,
            baseline_hierarchy=baseline,
            current_activity=activity,
            timeout_seconds=self.observer_timeout,
        )

        if result.detected:
            logger.info(f"[{observer_app}] Detected: {result.event_type} "
                       f"(latency={result.latency_ms}ms)")

            # Record trigger on the primary/trigger node
            record_cross_app_trigger(trigger_node, observer_app, result)

            # Upsert in the observer's tree — find the screen node the observer is on
            observer_screen = _find_current_screen_node(observer_state.root, activity)
            if observer_screen and result.event_type == "element_updated":
                for updated in result.updated_elements:
                    identity_key = updated.get("identity_key", "")
                    existing = _find_child_by_identity(observer_screen, identity_key)
                    if existing:
                        existing.label = updated.get("new_state", {}).get("text", existing.label)
                    else:
                        observer_screen.children.append(FeatureNode(
                            id=identity_key,
                            label=updated.get("new_state", {}).get("text", identity_key),
                            node_type="dynamic_element",
                            status="explored",
                            identity_key=identity_key,
                        ))
            elif observer_screen and result.new_elements:
                observer_screen.children.append(FeatureNode(
                    id=f"passive_{len(observer_screen.children)}",
                    label=_extract_key_text(result.new_elements),
                    node_type="passive_event",
                    status="explored",
                ))

            save_exploration_state(observer_state, self.lessons_dir, observer_app)
        else:
            logger.debug(f"[{observer_app}] No cross-app effect detected.")


def _find_current_screen_node(
    root: FeatureNode,
    activity: str,
) -> FeatureNode | None:
    """Find the explored node in the tree that matches the current activity.

    Used to attach passive_event/dynamic_element children to the correct
    screen in the observer's feature tree.
    """
    def _search(node: FeatureNode) -> FeatureNode | None:
        if node.activity == activity and node.status == "explored":
            return node
        for child in node.children:
            found = _search(child)
            if found:
                return found
        return None

    return _search(root)
```

### Two-Device Setup

In cluster mode, two separate `MobileUseContext` instances are needed — one per device. The CLI creates them from ADB device serials:

```python
# In main.py, learn-cluster command:

from mineru.ui_auto.context import MobileUseContext
from mineru.ui_auto.clients.adb_tunnel import AdbTunnel


async def create_device_context(device_serial: str | None = None) -> MobileUseContext:
    """Create a MobileUseContext connected to a specific ADB device.

    If device_serial is None, uses the default connected device.
    This is a thin wrapper around the existing device setup in main.py.
    """
    adb_client = AdbTunnel(device_serial=device_serial)
    await adb_client.connect()
    return MobileUseContext(adb_client=adb_client, device=device_serial)


async def run_learn_cluster(
    primary: str,
    secondary: str,
    primary_device: str | None,
    secondary_device: str | None,
    lessons_dir: Path,
    budget_minutes: int,
    observer_timeout: float,
    trigger_hints: str,
    # ... other CLI args
):
    """Entry point for the learn-cluster CLI command."""
    # Create separate device contexts. If only one device is specified,
    # both apps run on the same device (common for emulator testing).
    primary_ctx = await create_device_context(device_serial=primary_device)
    secondary_ctx = (
        await create_device_context(device_serial=secondary_device)
        if secondary_device and secondary_device != primary_device
        else primary_ctx  # Same device — agents share the context
    )

    hint_set = {h.strip() for h in trigger_hints.split(",")} if trigger_hints else None

    orchestrator = MasterOrchestrator(
        primary_app=primary,
        secondary_app=secondary,
        primary_ctx=primary_ctx,
        secondary_ctx=secondary_ctx,
        lessons_dir=lessons_dir,
        observer_timeout=observer_timeout,
        trigger_hints=hint_set,
    )

    await orchestrator.init()
    await orchestrator.run_cluster_session(
        budget_minutes=budget_minutes,
    )
```

**Same-device cluster mode:** When both apps are on one device, the agents take turns (interleaved by the orchestrator). The device cannot show both apps simultaneously, so Observer Mode must explicitly bring the observer app to the foreground before polling, then restore the trigger app afterward. This is slower but functional for testing.

```python
async def _bring_app_to_foreground(ctx: MobileUseContext, app_package: str):
    """Bring an app to the foreground on a shared device.

    Used in same-device cluster mode to switch between primary and secondary
    apps during Observer Mode. Uses `am start` with LAUNCHER category to
    resume the app's last activity (not cold-launch).
    """
    await ctx.adb_client.shell(
        f"monkey -p {app_package} -c android.intent.category.LAUNCHER 1"
    )
    await asyncio.sleep(1.0)  # Wait for activity transition to complete
```

The `_observe_cross_app_effect()` method checks whether both apps share a device context and wraps the observer call with app-switching:

```python
    async def _observe_cross_app_effect(self, ...):
        same_device = self.primary_ctx is self.secondary_ctx

        if same_device:
            # Switch to the observer app before capturing baseline
            await _bring_app_to_foreground(observer_ctx, observer_app)

        controller = create_device_controller(observer_ctx)
        baseline = (await controller.get_screen_data()).elements
        # ... (observer polling loop as before) ...

        if same_device:
            # Restore the trigger app to foreground for the next goal
            await _bring_app_to_foreground(
                self.primary_ctx if observer_app == self.secondary_app else self.secondary_ctx,
                self.primary_app if observer_app == self.secondary_app else self.secondary_app,
            )
```

---

## Multi-Session Support

### Session Lifecycle

```
Session 1 (30 min):
  - Initialize tree from root screen
  - BFS: explore all top-level items (Settings main menu)
  - Record: 12 success paths, 3 mistakes
  - Save tree: 40 nodes discovered, 12 explored, 28 pending

Session 2 (30 min):
  - Load tree from _exploration.json
  - DFS: dive into "Network & internet" branch
  - Record: 8 more success paths, 2 strategies
  - Save tree: 55 nodes, 28 explored, 22 pending, 5 deep_limit

Session 3 (30 min):
  - Load tree, continue DFS
  - Explore "Display", "Battery", "Storage" branches
  - Record: 10 more success paths
  - Save tree: 55 nodes, 48 explored, 2 pending, 5 deep_limit

Session 4 (15 min):
  - Explore remaining 2 pending nodes
  - All reachable nodes explored — self-learning complete
  - Total: 30 success paths, 5 mistakes, 2 strategies recorded
```

### Resumability

Each session saves the tree after every goal completion. If a session is interrupted (Ctrl+C, timeout, crash), the tree reflects progress up to the last completed goal. The in-progress node reverts to `pending` on next load:

```python
# ── state.py ──────────────────────────────────────────────────

import json
from pathlib import Path


def load_exploration_state(lessons_dir: Path, app_package: str) -> ExplorationState | None:
    """Load persisted exploration state from _exploration.json.

    Returns None if no prior exploration exists for this app.
    Recovers from crashed sessions by resetting in_progress nodes to pending.
    """
    path = lessons_dir / app_package / "_exploration.json"
    if not path.exists():
        return None
    state = ExplorationState(**json.loads(path.read_text()))
    _reset_in_progress_nodes(state.root)
    return state


def save_exploration_state(
    state: ExplorationState,
    lessons_dir: Path,
    app_package: str,
) -> None:
    """Persist exploration state to _exploration.json.

    Called after every goal completion to ensure crash recovery loses
    at most one goal's worth of progress. Uses atomic write (write to
    temp file, then rename) to prevent corruption on Ctrl+C.
    """
    dir_path = lessons_dir / app_package
    dir_path.mkdir(parents=True, exist_ok=True)

    target = dir_path / "_exploration.json"
    tmp = dir_path / "_exploration.json.tmp"

    tmp.write_text(state.model_dump_json(indent=2))
    tmp.rename(target)


def _reset_in_progress_nodes(node: FeatureNode) -> None:
    """Reset any nodes stuck in 'in_progress' back to 'pending'.

    This happens when a session crashes mid-goal. The node was marked
    in_progress before the agent started but never updated to explored/failed.
    """
    if node.status == "in_progress":
        node.status = "pending"
    for child in node.children:
        _reset_in_progress_nodes(child)
```

### Session History

Each session appends a summary to the exploration state:

```json
{
  "sessions": [
    {
      "started": "2026-03-26T10:00:00Z",
      "ended": "2026-03-26T10:32:00Z",
      "goals_attempted": 14,
      "goals_completed": 12,
      "goals_failed": 2,
      "nodes_discovered": 40,
      "lessons_recorded": 15,
      "strategy": "breadth_first"
    }
  ]
}
```

This lets the user track learning progress and decide when to stop.

---

## CLI Interface

### Basic Usage

```bash
# Explore Settings app for 30 minutes (one session)
ui-auto learn com.android.settings \
  --lessons-dir ./lessons \
  --model-provider claude --claude-model claude-sonnet-4-6

# Run 3 sessions of 30 minutes each
ui-auto learn com.android.settings \
  --lessons-dir ./lessons \
  --sessions 3 \
  --model-provider claude

# Custom budget per session
ui-auto learn com.android.settings \
  --lessons-dir ./lessons \
  --budget-minutes 60 \
  --max-depth 5

# Check exploration progress
python scripts/view_exploration.py ./lessons/com.android.settings/
```

### CLI Command Definitions

The existing `main.py` uses `click` for CLI commands. Add `learn` and `learn-cluster` as subcommands:

```python
# In mineru/ui_auto/main.py — add these commands to the existing click group

import click
import asyncio
from pathlib import Path

from mineru.ui_auto.exploration.runner import run_exploration_session
from mineru.ui_auto.exploration.state import load_exploration_state, save_exploration_state


@cli.command()
@click.argument("app_package")
@click.option("--lessons-dir", required=True, type=click.Path(), help="Directory for lessons and exploration state")
@click.option("--sessions", default=1, type=int, help="Number of sessions to run")
@click.option("--budget-minutes", default=30, type=int, help="Time budget per session in minutes")
@click.option("--max-depth", default=4, type=int, help="Maximum depth in the feature tree")
@click.option("--strategy", default="auto", type=click.Choice(["auto", "breadth_first", "depth_first"]),
              help="Node selection strategy")
@click.option("--reset", is_flag=True, help="Discard existing exploration state and start fresh")
@click.option("-m", "--model-provider", default="claude", help="Model provider preset")
@click.option("--claude-model", default=None, help="Claude model ID")
@click.pass_context
def learn(ctx, app_package, lessons_dir, sessions, budget_minutes, max_depth,
          strategy, reset, model_provider, claude_model):
    """Explore an app's UI to build navigation knowledge before real tasks."""
    lessons_path = Path(lessons_dir)

    if reset:
        exploration_file = lessons_path / app_package / "_exploration.json"
        if exploration_file.exists():
            exploration_file.unlink()
            click.echo(f"Reset exploration state for {app_package}")

    async def _run():
        mobile_ctx = await setup_context(ctx, model_provider, claude_model)
        for i in range(sessions):
            click.echo(f"\n{'='*60}")
            click.echo(f"Session {i + 1}/{sessions}")
            click.echo(f"{'='*60}")
            await run_exploration_session(
                app_package=app_package,
                lessons_dir=lessons_path,
                ctx=mobile_ctx,
                budget_minutes=budget_minutes,
                max_depth=max_depth,
                strategy=strategy,
            )

    asyncio.run(_run())


@cli.command("learn-cluster")
@click.option("--primary", required=True, help="Primary app package (active explorer)")
@click.option("--secondary", required=True, help="Secondary app package (observer + explorer)")
@click.option("--primary-device", default=None, help="ADB device serial for primary app")
@click.option("--secondary-device", default=None, help="ADB device serial for secondary app")
@click.option("--lessons-dir", required=True, type=click.Path(), help="Directory for lessons and exploration state")
@click.option("--sessions", default=1, type=int, help="Number of sessions to run")
@click.option("--budget-minutes", default=45, type=int, help="Time budget per session in minutes")
@click.option("--observer-timeout", default=10.0, type=float, help="Seconds to wait for cross-app events")
@click.option("--trigger-hints", default=None,
              help="Comma-separated action labels that trigger Observer Mode")
@click.option("--reset", is_flag=True, help="Discard existing exploration state for both apps")
@click.option("-m", "--model-provider", default="claude", help="Model provider preset")
@click.pass_context
def learn_cluster(ctx, primary, secondary, primary_device, secondary_device,
                  lessons_dir, sessions, budget_minutes, observer_timeout,
                  trigger_hints, reset, model_provider):
    """Explore two linked apps (Parent/Child) with cross-app event detection."""
    lessons_path = Path(lessons_dir)

    if reset:
        for pkg in [primary, secondary]:
            exploration_file = lessons_path / pkg / "_exploration.json"
            if exploration_file.exists():
                exploration_file.unlink()
                click.echo(f"Reset exploration state for {pkg}")

    async def _run():
        await run_learn_cluster(
            primary=primary,
            secondary=secondary,
            primary_device=primary_device,
            secondary_device=secondary_device,
            lessons_dir=lessons_path,
            budget_minutes=budget_minutes,
            observer_timeout=observer_timeout,
            trigger_hints=trigger_hints,
        )

    asyncio.run(_run())
```

### CLI Flags Reference (Single-App Mode)

```
ui-auto learn [OPTIONS] APP_PACKAGE

Arguments:
  APP_PACKAGE                   The Android package to explore (e.g., com.android.settings)

Options:
  --lessons-dir TEXT            Directory for lessons and exploration state [required]
  --sessions INT                Number of sessions to run [default: 1]
  --budget-minutes INT          Time budget per session in minutes [default: 30]
  --max-depth INT               Maximum depth in the feature tree [default: 4]
  --strategy TEXT               Node selection: 'auto', 'breadth_first', or 'depth_first' [default: auto]
  --reset                       Discard existing exploration state and start fresh
  -m, --model-provider TEXT     Model provider preset [default: claude]
  --claude-model TEXT           Claude model ID
```

### CLI Flags Reference (Cluster Mode)

```
ui-auto learn-cluster [OPTIONS]

Options:
  --primary TEXT                Primary app package (active explorer) [required]
  --secondary TEXT              Secondary app package (observer + explorer) [required]
  --primary-device TEXT         ADB device serial for primary app [required if 2 devices]
  --secondary-device TEXT       ADB device serial for secondary app [required if 2 devices]
  --lessons-dir TEXT            Directory for lessons and exploration state [required]
  --sessions INT                Number of sessions to run [default: 1]
  --budget-minutes INT          Time budget per session in minutes [default: 45]
  --observer-timeout FLOAT      Seconds to wait for cross-app events [default: 10.0]
  --trigger-hints TEXT          Comma-separated action labels that trigger Observer Mode
                                [default: send,share,enable,disable,notify,invite,block,allow]
  --reset                       Discard existing exploration state for both apps
  -m, --model-provider TEXT     Model provider preset [default: claude]
```

### Progress Viewer Script

```bash
python scripts/view_exploration.py ./lessons/com.android.settings/
```

Output:
```
=== com.android.settings — Exploration Progress ===

Sessions: 2 completed (total 58 min)
Nodes:    55 total | 28 explored | 22 pending | 5 deep_limit | 0 failed
Lessons:  20 success_paths | 3 mistakes | 2 strategies

Feature Tree:
  [OK] Settings main screen
    [OK] Network & internet
      [OK] Wi-Fi
      [OK] Mobile network
      [OK] Hotspot & tethering
      [  ] Airplane mode
    [OK] Connected devices
      [OK] Bluetooth
      [  ] NFC
    [  ] Display
      ...
    [  ] Battery
    [  ] Storage
    [!!] Accounts (skipped: auth_required)
    [XX] Developer options (failed: 3 attempts)

Legend: [OK] explored  [  ] pending  [!!] skipped  [XX] failed  [~~] deep_limit
```

---

## Data Flow

### How Self-Learning Feeds Into Real Tasks

Self-learning and real tasks use the **exact same lesson files**. No separate "knowledge base" — everything goes into `lessons.jsonl`.

```
Self-Learning Session                    Real Task
─────────────────────                    ─────────
explore "Network & internet"             "Turn on WiFi"
  → records success_path:                  → Contextor loads lessons
    tap('Network & internet')              → Cortex sees:
    tap('Wi-Fi')                              "Known navigation paths:
  → records ui_mapping:                        Network & internet →
    "Wi-Fi toggle is at top                    Wi-Fi [spa-a1b2]"
     of Wi-Fi screen"                      → Follows proven path
                                           → 3 steps, 0 wrong turns
```

### Lesson Types Recorded During Self-Learning

| Type | What triggers it | Example |
|------|-----------------|---------|
| `success_path` | Agent navigates to a screen via subgoal completion | `tap('Network & internet') → tap('Wi-Fi')` |
| `mistake` | Agent taps something with no effect, or tool fails | "Tap had no visible effect on SubSettings" |
| `strategy` | Agent gets stuck then breaks through | "Use scroll to find element below fold" |
| `ui_mapping` | (future) Screen element catalog | "Wi-Fi toggle is a Switch at [0.5, 0.15]" |

---

## Exploration Depth and Scope

### Depth Limit

Default `max_depth=4` means the agent explores up to 4 levels deep from the app's main screen:

```
Level 0: App home (Settings main)
Level 1: Top-level sections (Network, Display, Battery, ...)
Level 2: Sub-sections (Wi-Fi, Bluetooth, Brightness, ...)
Level 3: Detail screens (Wi-Fi network list, Bluetooth devices, ...)
Level 4: (limit) — discovered but not explored
```

This covers the vast majority of navigable screens. Deeper levels (settings within settings within settings) are rare and have diminishing returns.

### Breadth Limit

No explicit breadth limit per screen, but multiple mechanisms prevent combinatorial explosion:
- **List-view sampling** — `RecyclerView`/`ListView` containers are detected; only 1-2 items are sampled, the rest are `skipped_redundant` (see Issue 1)
- **Structural dedup** — node IDs are structural (activity + className + position), not text-based, so "Email from Alice" and "Email from Bob" in the same list position don't create separate nodes
- Screen discovery caps at 30 elements per screen (matches `capture_screen_signature`)
- `SKIP_LABELS` filters out dangerous actions
- Step limit per goal (20) prevents runaway exploration

### Estimated Time Per App

| App Complexity | Screens | Sessions (30 min each) | Total Time |
|---------------|---------|----------------------|------------|
| Simple (Calculator, Clock) | 10-20 | 1 | 30 min |
| Medium (Settings, Files) | 40-80 | 2-3 | 1-1.5 hr |
| Complex (Gmail, Chrome) | 100-200 | 4-6 | 2-3 hr |
| Very Complex (Banking, Social) | 200+ | 6-10 | 3-5 hr |

---

## Implementation Plan

### Phase 1: Core Exploration Loop (MVP)

**Files to create:**
- `mineru/ui_auto/exploration/__init__.py` — Module exports
- `mineru/ui_auto/exploration/types.py` — `FeatureNode`, `ExplorationState`, `SessionSummary`
- `mineru/ui_auto/exploration/discovery.py` — `discover_features()`, safety filters
- `mineru/ui_auto/exploration/planner.py` — `pick_next_node()`, `generate_exploration_goal()`
- `mineru/ui_auto/exploration/runner.py` — `run_exploration_session()`, session lifecycle
- `mineru/ui_auto/exploration/state.py` — Load/save `_exploration.json`, crash recovery
- `scripts/view_exploration.py` — Progress viewer

**Files to modify:**
- `mineru/ui_auto/main.py` — Add `learn` subcommand
- `mineru/ui_auto/sdk/agent.py` — Add `run_exploration_task()` method (thin wrapper around `run_task` with locked app + max steps)

**Estimated effort:** 3-4 days

### Phase 2: Smart Goal Generation

Replace template-based goals with LLM-generated goals that consider:
- What the agent already knows about this area (from lessons)
- What's likely behind this element (inference from label + context)
- Priority ordering (most useful features first)

This requires one LLM call per goal generation but produces more natural and effective exploration goals.

**Files:**
- `mineru/ui_auto/exploration/goal_generator.py` — LLM-powered goal generation with context

**Estimated effort:** 1-2 days

### Phase 3: Exploration Quality Metrics

Add automated quality assessment:
- Coverage score (explored / total discovered)
- Path redundancy (how many duplicate paths exist)
- Staleness detection (how old are the exploration paths vs. app version)
- Suggested re-exploration (when app version changes significantly)

**Files:**
- `mineru/ui_auto/exploration/metrics.py` — Coverage, redundancy, staleness scoring
- `scripts/exploration_report.py` — Generate exploration quality report

**Estimated effort:** 1-2 days

---

## Design Decisions and Rationale

### Q: Why not use a dedicated crawler instead of the agent?

A crawler would be faster but would not record *meaningful* navigation paths. The agent's Cortex reasons about what it sees, the Planner breaks goals into subgoals, and the lesson system records the structured path. A crawler would just enumerate screens without understanding them.

More importantly: the paths recorded by the agent during self-learning are *exactly the same format* the agent uses during real tasks. There's no translation layer.

### Q: Why BFS first, then DFS?

BFS in session 1 maps the app's top-level structure — the "table of contents." This is immediately useful: most real tasks start with "go to section X." DFS in later sessions fills in the details within each section. If the user only runs one session, they get maximum breadth of knowledge.

### Q: Why not explore everything in one long session?

- **Device battery** — 3+ hours of continuous screen interaction drains the device
- **App state drift** — long sessions accumulate side effects (notifications, cached state)
- **LLM cost** — each exploration goal costs ~1-2k tokens; a full app might be 200+ goals
- **Crash recovery** — shorter sessions mean less lost progress on failure
- **User flexibility** — run one quick session before a demo, or schedule overnight exploration

### Q: Why max_depth=4?

Empirical observation: most Android apps have 3-4 levels of navigation depth from the home screen. Level 5+ is rare (usually only in complex settings or configuration screens). `max_depth=4` covers ~95% of navigable screens while avoiding exponential blowup.

The user can increase it with `--max-depth 6` for deeply nested apps.

### Q: Why store exploration state in the lessons_dir?

- **Single configuration point** — user only sets `--lessons-dir`
- **Co-location** — exploration state and lessons for the same app are in the same directory
- **Portability** — copy the app's folder to share both exploration progress and lessons
- **Cleanup** — deleting the app folder removes everything

### Q: What about apps that require authentication?

Self-learning works best on the unauthenticated portions of an app (settings, help, about). For authenticated features, the user should:
1. Manually log in on the device before running self-learning
2. Use `--locked-app-package` to prevent the agent from navigating away
3. Auth-gated screens that the agent can't reach are marked `skipped: auth_required`

Future enhancement: support pre-auth scripts that run before exploration.

### Q: Why a Master Orchestrator instead of a single multi-app agent?

A single agent switching between two apps would need to manage two app-lock contexts, two feature trees, and interleaved navigation — dramatically increasing complexity and failure modes. The Master Orchestrator pattern keeps each agent simple (single-app, existing code path) and coordinates at the boundaries. Each agent is unaware of the other; only the orchestrator sees the full picture.

This also means **single-app mode is unaffected** — the orchestrator is an optional layer on top, not a modification to the core loop.

### Q: Why identity-based diffing instead of just debouncing Observer Mode?

Debouncing (e.g., "only record the first change and ignore the rest for 5s") would suppress duplicate events but lose information. If a map avatar moves AND a notification banner appears in the same observation window, debouncing would capture one and drop the other.

Identity-based diffing preserves the full picture: it distinguishes "avatar moved" (upsert) from "banner appeared" (append) in the same polling cycle. It's also deterministic — the same hierarchy diff always produces the same classification, regardless of polling frequency.

The identity key priority (`resource-id` > `content-desc` prefix > structural fallback) was chosen because `resource-id` is assigned by developers and is the most stable identifier across app versions. `content-desc` is next because accessibility labels change less frequently than layout positions. The structural fallback is fragile but covers edge cases where neither is available.

### Q: Why polling-based Observer Mode instead of Android event listeners?

Android's `UiAutomation` accessibility event stream could theoretically detect UI changes in real time. However: (1) accessibility events are noisy and require complex filtering, (2) the agent's device controller already supports `get_screen_data()` reliably, (3) polling at 500ms intervals is sufficient for the latencies we care about (push notifications arrive in 1-5 seconds), and (4) polling reuses existing infrastructure with zero new dependencies.

### Q: How does this interact with the existing lesson system?

**Zero changes to the lesson system.** Self-learning generates goals → the agent executes them → lessons are recorded via the existing `record_success_path`, `record_no_effect_mistake`, `record_strategy` functions. The lesson system doesn't know or care whether the goal came from a human or from self-learning.

This is the key architectural insight: **self-learning is just automated goal generation.** Everything downstream is reused.

---

## Issues Addressed from Review

| # | Issue | Severity | How Fixed |
|---|-------|----------|-----------|
| 1 | **Dynamic content explosion** — text-based `_make_id` treats every email/transaction/video as a unique feature node, causing exponential tree growth | Critical | Screen structural fingerprinting + list-view sampling. `discover_features()` detects `RecyclerView`/`ListView` containers, samples 1-2 items, marks the rest `skipped_redundant`. Dedup by structural signature, not text. |
| 2 | **Read-only safety is prompt-only** — LLM can drift and ignore "do not change settings" instruction during long tool-use loops | Critical | Tool-level action guard. New `exploration_mode` flag on `MobileUseContext` enables a hard blocklist check in the tap/press_key tool wrappers. Blocked actions return a system error, not a soft prompt suggestion. |
| 3 | **Fragile home reset** — `navigate_to_app_home()` can fail on modals, deep webviews, permission dialogs, leaving corrupted state for next goal | High | Hard-reset fallback. If home screen signature not verified within 3 back-presses, force-stop the app via `am force-stop` and cold-relaunch. Cheaper and more reliable than graceful backout. |
| 4 | **Modals corrupt feature tree** — bottom sheets, popups, and overlays create merged UI hierarchies that get misread as full-screen children | High | Screen coverage detection. Before `discover_features()`, compute the bounding-box coverage of new elements. If new content covers <80% of the screen, classify as `modal` node type with `dismiss_action` instead of `nav_action`. |
| 5 | **Dynamic map/live-element tree bloat** — Observer Mode uses bounds-based diffing, so a moving map avatar registers as dozens of `element_appeared` events, creating infinite `passive_event` nodes | Critical | Two-tiered identity diffing. Elements matched by stable identity key (`resource-id`, `content-desc` prefix) not by `bounds`. Same identity + different state = `element_updated` (upsert). New identity = `element_appeared` (append). `dynamic_element` node type for live-updating elements; one node per logical entity, upserted on observation. |

---

## Issue 1: Dynamic Content Explosion

### Problem

The original `discover_features()` uses `_make_id(text)` — every list item with unique text becomes a feature node. In Gmail's inbox, 50 emails = 50 nodes. In YouTube's feed, infinite scroll = infinite nodes. The exploration budget is wasted re-exploring identical screens with different data.

### Solution: Structural Screen Fingerprinting + List Sampling

The definitive `discover_features()` implementation (in the "Phase 1: Screen Discovery" section above) incorporates this fix. Key mechanisms:
- `_detect_list_containers()` identifies RecyclerView/ListView widgets
- `_find_parent_container()` checks if elements are inside a list container
- Only 2 items are sampled per container; the rest are skipped
- `_make_structural_id()` uses activity + className + index + bounds quadrant (not text)

### Tree Impact

```
Before (Gmail inbox):                After (Gmail inbox):
  [  ] Email from Alice              [  ] (list item sample 1)
  [  ] Email from Bob                [  ] (list item sample 2)
  [  ] Email from Carol              [~~] 48 more items (skipped_redundant)
  [  ] Email from Dave
  ... (50 nodes, 50 goals)           2 goals instead of 50
```

---

## Issue 2: Tool-Level Action Guard

### Problem

Prompt-based safety ("do not change settings") is a soft constraint. During long exploration sessions with iterative tool calls, the LLM can drift — especially if a "Save" or "Delete" button appears to be the logical next step to complete the exploration goal. Prompt injection from on-screen text is also a risk (e.g., a button labeled "Tap here to continue" that actually triggers a purchase).

### Solution: Hard Blocklist in Tool Execution Layer

Add an `exploration_mode` flag to `MobileUseContext`. When set, the tap and press_key tools check targets against a blocklist **before execution** and return a hard error that the LLM cannot override.

```python
# In MobileUseContext (context.py):
exploration_mode: bool = False  # When True, destructive actions are blocked at tool level

# Blocklist — checked at tool execution time, not discovery time
EXPLORATION_BLOCKED_ACTIONS = {
    # Destructive
    "delete", "remove", "erase", "format", "reset",
    "factory reset", "clear data", "clear storage",
    "uninstall", "wipe",
    # Transactional
    "send", "submit", "pay", "purchase", "confirm",
    "place order", "subscribe", "donate",
    # Auth-mutating
    "sign out", "log out", "deactivate", "close account",
    # State-changing
    "save", "apply", "update", "enable", "disable",
    "turn on", "turn off",
}
```

**In the tap tool wrapper** (`tools/mobile/tap.py` or `tools/tool_wrapper.py`):

```python
async def _check_exploration_guard(ctx: MobileUseContext, target_text: str | None) -> str | None:
    """Return an error message if this action is blocked in exploration mode."""
    if not ctx.exploration_mode or not target_text:
        return None

    normalized = target_text.lower().strip()
    for blocked in EXPLORATION_BLOCKED_ACTIONS:
        if blocked in normalized:
            return (
                f"ACTION_BLOCKED: Cannot tap '{target_text}' in exploration mode. "
                f"Destructive/state-changing actions are disabled during self-learning. "
                f"Navigate to observe this screen but do not interact with '{target_text}'."
            )
    return None
```

**Why tool-level, not prompt-level?**

| Layer | Enforcement | LLM can bypass? | Catches prompt injection? |
|-------|-------------|-----------------|--------------------------|
| Goal phrasing | Soft (instruction in prompt) | Yes (attention drift) | No |
| Discovery filter (SKIP_LABELS) | Medium (node never created) | N/A (no node to explore) | No (only applies to discovery) |
| **Tool-level guard** | **Hard (error before execution)** | **No (system-level check)** | **Yes (checks actual target text)** |

All three layers work together: discovery filters prevent generating goals for dangerous nodes, goal phrasing guides the LLM's intent, and the tool guard is the last line of defense that cannot be bypassed.

---

## Issue 3: Hard Reset Fallback

### Problem

`navigate_to_app_home()` between goals relies on back-navigation and intent launching. This fails when:
- A modal overlay traps focus (permission dialog, rate-this-app popup)
- The app is in a deep WebView that doesn't respond to back presses
- A system dialog is on top (battery optimization, accessibility prompt)
- The app crashed and Android is showing an ANR dialog

If the reset fails, the next exploration goal starts from an unknown screen state, producing garbage results.

### Solution: Verify-then-Force Reset

```python
async def navigate_to_app_home(
    ctx: MobileUseContext,
    app_package: str,
    home_signature: ScreenSignature | None = None,
    max_back_presses: int = 3,
) -> bool:
    """Reset the app to its home screen. Falls back to force-stop if graceful nav fails.

    Strategy:
    1. Press back up to max_back_presses times
    2. After each back press, check if we're on the home screen
    3. If still not home, force-stop and cold-relaunch

    Returns True if home screen was reached.
    """
    controller = create_device_controller(ctx)

    # Attempt graceful back-navigation
    for i in range(max_back_presses):
        await controller.press_back()
        await asyncio.sleep(0.5)

        # Verify: are we on the home screen?
        current_app = await get_current_foreground_package_async(ctx)
        if current_app != app_package:
            # We left the app — force relaunch
            break

        if home_signature:
            screen_data = await controller.get_screen_data()
            current_sig = capture_screen_signature(app_package, screen_data.elements)
            if _signatures_match(current_sig, home_signature):
                return True  # Successfully reached home

    # Graceful nav failed — hard reset
    logger.info(f"Graceful reset failed, force-stopping {app_package}")
    await _force_stop_and_relaunch(ctx, app_package)
    return True


def _signatures_match(sig_a: ScreenSignature, sig_b: ScreenSignature) -> bool:
    """Check if two screen signatures represent the same screen.

    Matches if: same activity AND at least 60% of key_elements overlap.
    Exact text matching is too brittle (dynamic content), but >60% overlap
    indicates the same screen layout.
    """
    if sig_a.activity != sig_b.activity:
        return False
    if not sig_a.key_elements or not sig_b.key_elements:
        return sig_a.activity == sig_b.activity  # Fall back to activity-only match
    overlap = set(sig_a.key_elements) & set(sig_b.key_elements)
    return len(overlap) / max(len(sig_a.key_elements), len(sig_b.key_elements)) >= 0.6


async def _force_stop_and_relaunch(ctx: MobileUseContext, app_package: str):
    """Force-stop the app and cold-launch it. Guaranteed clean state."""
    # am force-stop is fast (~100ms) and kills all app processes
    await ctx.adb_client.shell(f"am force-stop {app_package}")
    await asyncio.sleep(0.5)

    # Cold-launch via monkey (launches the app's main activity)
    await ctx.adb_client.shell(
        f"monkey -p {app_package} -c android.intent.category.LAUNCHER 1"
    )
    await asyncio.sleep(2.0)  # Wait for app to fully render
```

**Why `am force-stop` instead of just relaunching?**

Relaunching on top of a stuck state can produce unpredictable results (activity stack corruption, duplicate instances). `am force-stop` kills all processes and clears the activity stack, guaranteeing a clean slate. The 2-second wait after relaunch accounts for cold-start animation.

### Home Screen Signature Capture

The exploration runner captures the home screen signature during initialization:

```python
async def initialize_exploration(
    app_package: str,
    ctx: MobileUseContext,
    lessons_dir: Path,
) -> ExplorationState:
    """First-time setup: launch app, capture home screen, build root node.

    Args:
        app_package: Android package to explore
        ctx: Device context (needed for controller and ADB access)
        lessons_dir: Where to persist the exploration state
    """
    controller = create_device_controller(ctx)

    # Force-stop and cold-launch for a clean starting state
    await _force_stop_and_relaunch(ctx, app_package)

    # Capture home screen signature for later reset verification
    screen_data = await controller.get_screen_data()
    current_app_info = await get_current_foreground_package_async(ctx)
    home_signature = capture_screen_signature(current_app_info, screen_data.elements)

    # Build root node from home screen
    children = discover_features(screen_data.elements, home_signature.activity or "")
    root = FeatureNode(
        id="root",
        label=f"{app_package} home",
        activity=home_signature.activity,
        key_elements=home_signature.key_elements,
        status="explored",
        children=children,
    )

    state = ExplorationState(
        app_package=app_package,
        root=root,
        home_signature=home_signature,
        sessions=[],
    )
    save_exploration_state(state, lessons_dir, app_package)
    return state
```

---

## Issue 4: Modal and Overlay Detection

### Problem

The feature tree assumes parent → child is a full-screen navigation (tap "Wi-Fi" → Wi-Fi settings screen). But mobile UIs frequently use:
- **Bottom sheets** (e.g., share menu, action picker)
- **Dialogs** (confirmation, info, error)
- **Floating tooltips** (onboarding, feature discovery)
- **Notification overlays** (incoming call, toast)

When a bottom sheet appears, the UI hierarchy contains **both** the background screen and the overlay. `discover_features()` reads the merged hierarchy and creates nodes from both, producing a corrupted tree where background elements are children of the tapped element.

### Solution: Screen Coverage Analysis

Before processing the UI hierarchy, compare it against the parent screen's element set. If the new content covers a small portion of the screen, it's a modal — not a full navigation.

The `FeatureNode` model (see "Type Definitions" section above) includes `node_type` (with `"modal"` as a value) and `dismiss_action` for this purpose.

```python
# ── screen_classifier.py ──────────────────────────────────────

def classify_screen_transition(
    parent_hierarchy: list[dict],
    current_hierarchy: list[dict],
    screen_width: int,
    screen_height: int,
    agent_performed_action: bool = True,
) -> str:
    """Classify whether a screen transition is a full navigation, modal overlay, or passive event.

    Args:
        agent_performed_action: Whether the local agent just performed a tap/action.
            If False, any detected change is classified as a passive event (cross-app
            trigger, push notification, background update) rather than a navigational child.

    Returns: "full_screen", "modal", "passive_event", or "unchanged"
    """
    if not agent_performed_action:
        # UI changed without a local action — use identity-based diffing (Issue 5)
        # to avoid false positives from dynamic elements like map avatars.
        # _bounds_key alone would treat a moved avatar as a new element.
        activity = current_hierarchy[0].get("activity", "") if current_hierarchy else ""
        diff = _compute_identity_diff(
            _build_identity_index(parent_hierarchy, activity),
            _build_identity_index(current_hierarchy, activity),
        )
        if diff.has_changes:
            return "passive_event"
        return "unchanged"
    screen_area = screen_width * screen_height
    if screen_area == 0:
        return "full_screen"  # Can't determine, assume full

    # Compute the bounding box of NEW elements (not present in parent)
    parent_bounds_set = {_bounds_key(e) for e in parent_hierarchy if e.get("bounds")}

    new_elements = [
        e for e in current_hierarchy
        if e.get("bounds") and _bounds_key(e) not in parent_bounds_set
    ]

    if not new_elements:
        return "unchanged"

    # Compute union bounding box of all new elements
    min_x = min(e["bounds"].get("left", 0) for e in new_elements)
    min_y = min(e["bounds"].get("top", 0) for e in new_elements)
    max_x = max(e["bounds"].get("right", screen_width) for e in new_elements)
    max_y = max(e["bounds"].get("bottom", screen_height) for e in new_elements)

    new_area = (max_x - min_x) * (max_y - min_y)
    coverage_ratio = new_area / screen_area

    if coverage_ratio < 0.80:
        return "modal"
    return "full_screen"


def _bounds_key(elem: dict) -> str:
    """Create a hashable key from element bounds for set comparison."""
    b = elem.get("bounds", {})
    return f"{b.get('left',0)},{b.get('top',0)},{b.get('right',0)},{b.get('bottom',0)}"


def _extract_new_elements(
    parent_hierarchy: list[dict],
    current_hierarchy: list[dict],
) -> list[dict]:
    """Extract elements in current that are not in parent (by bounds key).

    Used to isolate modal/overlay elements from the background screen.
    """
    parent_keys = {_bounds_key(e) for e in parent_hierarchy if e.get("bounds")}
    return [
        e for e in current_hierarchy
        if e.get("bounds") and _bounds_key(e) not in parent_keys
    ]
```

### Integration with Exploration Loop

```python
# In the exploration loop, after agent reaches a screen:

transition_type = classify_screen_transition(
    parent_hierarchy=parent_screen_data.elements,
    current_hierarchy=result.final_ui_hierarchy,
    screen_width=result.screen_width,
    screen_height=result.screen_height,
)

if transition_type == "modal":
    # Create a modal node — children are the modal's elements only
    node.node_type = "modal"
    node.dismiss_action = "back"  # Most modals dismiss on back

    # Only discover features from NEW elements (not background)
    modal_elements = _extract_new_elements(
        parent_screen_data.elements,
        result.final_ui_hierarchy,
    )
    children = discover_features(modal_elements, result.final_activity)
    node.children = _merge_children(node.children, children)

elif transition_type == "full_screen":
    # Normal full-screen navigation — existing logic
    children = discover_features(
        result.final_ui_hierarchy,
        result.final_activity,
    )
    node.children = _merge_children(node.children, children)

elif transition_type == "passive_event":
    # UI changed without a local action — external trigger (notification, cross-app effect)
    # Classify whether this is a genuinely new element or an existing element updating
    diff = _compute_identity_diff(
        _build_identity_index(parent_screen_data.elements, result.final_activity),
        _build_identity_index(result.final_ui_hierarchy, result.final_activity),
    )
    sub_event = classify_passive_event(diff, result.final_ui_hierarchy)

    if sub_event == "element_updated":
        # Existing element changed state (e.g., map avatar moved, status icon changed)
        # Upsert a dynamic_element node — do NOT create a new node per observation
        for updated in diff.updated:
            identity_key = updated["identity_key"]
            existing_node = _find_child_by_identity(node, identity_key)
            if existing_node:
                # Update in place — no new tree node
                existing_node.label = updated["new_state"].text or existing_node.label
            else:
                # First observation: create one dynamic_element node
                dynamic_node = FeatureNode(
                    id=identity_key,
                    label=updated["new_state"].text or identity_key,
                    node_type="dynamic_element",
                    status="explored",
                    identity_key=identity_key,
                    children=[],
                )
                node.children = _merge_children(node.children, [dynamic_node])
    else:
        # Genuinely new element (banner, new badge, etc.) — create passive_event node
        passive_node = FeatureNode(
            id=_make_structural_id(diff.appeared[0]["state"], result.final_activity) if diff.appeared else "passive",
            label=_extract_key_text([e["state"] for e in diff.appeared]) if diff.appeared else "unknown event",
            node_type="passive_event",
            status="explored",
            children=[],
        )
        node.children = _merge_children(node.children, [passive_node])

elif transition_type == "unchanged":
    # Tap had no effect (already handled by lesson-learned mistake recording)
    node.status = "explored"  # Mark as explored but no children
```

### Tree Representation

```
Feature Tree (with modal awareness):
  [OK] Settings main screen
    [OK] Network & internet
      [OK] Wi-Fi
        [OK] (modal) Add network           ← bottom sheet, dismiss=back
          [  ] Network name input
          [  ] Security dropdown
        [OK] Saved networks
      [OK] Mobile network
    [OK] About phone
      [OK] (modal) Legal information        ← dialog overlay
        [  ] Open source licenses
        [  ] Google legal
```

---

## Issue 5: Dynamic Map / Live-Element Tree Bloat

### Problem

Observer Mode's baseline diffing uses `_bounds_key()` — an exact match on element bounding boxes. On a live map screen (core to our localization/family-tracking app), this breaks catastrophically:

1. A child device triggers a location update → parent's map avatar shifts `bounds` by a few pixels
2. The activity icon changes from "Stationary" to "Driving"
3. The "Last updated" timestamp changes

Under bounds-based diffing, every shifted bounding box is classified as a **brand new element**. If the Observer watches a map for 10 seconds while an avatar is moving, it records 20+ `element_appeared` events, creating 20+ `passive_event` nodes in the feature tree.

```
Before (bounds-based diffing, 30s observation):
  [OK] Map screen
    [PE] Avatar at (100,200) text="Stationary"     ← observation 1
    [PE] Avatar at (105,198) text="Stationary"     ← observation 2
    [PE] Avatar at (112,195) text="Walking"        ← observation 3
    [PE] Avatar at (120,190) text="Walking"        ← observation 4
    ... (16 more nodes for the same avatar)
```

### Solution: Two-Tiered Identity Diffing

Replace bounds-based element matching with **identity-based** matching. The diff logic now separates two questions:

1. **WHO is this element?** (Identity Key) — `resource-id`, `content-desc` prefix, or `className + index` fallback
2. **WHAT is its current state?** (State Key) — `bounds`, `text`, `checked`

If an element has the **same identity key** as a baseline element but a **different state**, it's classified as `element_updated` — not `element_appeared`. The tree node and trigger entry are **upserted**, not appended.

```
After (identity-based diffing, 30s observation):
  [OK] Map screen
    [DY] child_avatar — "Walking"                  ← one node, upserted 20 times
```

### Identity Key Priority

| Priority | Source | Example | Stability |
|----------|--------|---------|-----------|
| 1 | `resource-id` | `com.app:id/child_avatar` | Best — developers assign these intentionally |
| 2 | `content-desc` prefix (first 3 words) | `"Child Avatar"` from `"Child Avatar - Driving"` | Good — accessibility labels are semi-stable |
| 3 | `className` + `index` | `ImageView:idx3` | Fallback — position-dependent, can shift |

### Tree Storage: Upsert, Don't Append

When `element_updated` is detected:

1. Search `node.children` for an existing child with matching `identity_key`
2. If found → update its `label` with the latest text. **No new node created.**
3. If not found → create one `dynamic_element` node with the `identity_key`. Future observations of the same element upsert this node.

For cross-app triggers, the same upsert logic applies: `record_cross_app_trigger()` matches on `(target_app, event_type, element_identity_key)`. The `last_observed_state` field is overwritten with the most recent observation. The fact that it updates asynchronously is captured; each micro-movement is not.

### Worked Example: Family Tracking Map

**Action:** Primary Agent (Child App) taps "Start Driving"
**Observer:** Secondary Agent (Parent App) is viewing the live map

```
Step 1: Orchestrator captures baseline of parent map screen
        Baseline identity index:
          "MapActivity:child_avatar"  → {bounds: (100,200), text: "Stationary"}
          "MapActivity:last_updated"  → {bounds: (50,800),  text: "2 min ago"}
          "MapActivity:search_bar"    → {bounds: (0,0),     text: "Search"}

Step 2: Primary taps 'Start Driving', Orchestrator starts Observer Mode

Step 3: 3.2s later, poll detects changes:
        Current identity index:
          "MapActivity:child_avatar"  → {bounds: (150,250), text: "Driving"}    ← UPDATED
          "MapActivity:last_updated"  → {bounds: (50,800),  text: "Just now"}   ← UPDATED
          "MapActivity:search_bar"    → {bounds: (0,0),     text: "Search"}     ← UNCHANGED

Step 4: Identity diff:
          appeared: []          (no new identity keys)
          updated: [
            {key: "child_avatar", old: "Stationary", new: "Driving"},
            {key: "last_updated", old: "2 min ago",  new: "Just now"},
          ]
          disappeared: []

Step 5: classify_passive_event(diff) → "element_updated"
        (updated elements, no new/disappeared — pure state mutation)

Step 6: record_cross_app_trigger:
          Find existing trigger for (com.example.parent, element_updated, "MapActivity:child_avatar")
          → Found? Upsert: reliability_score++, last_observed_state = {text: "Driving"}
          → Not found? Create one trigger entry with element_identity_key

Step 7: Upsert secondary tree:
          Find child node with identity_key = "MapActivity:child_avatar"
          → Found? Update label to "Driving". Done.
          → Not found? Create dynamic_element node. Done.

Result: 1 trigger entry, 1 tree node — regardless of how many times the avatar moved
```

---

## Updated Implementation Plan

### Phase 1: Core Single-App Exploration Loop (MVP) — Updated

**Files to create:**
- `mineru/ui_auto/exploration/__init__.py` — Module exports
- `mineru/ui_auto/exploration/types.py` — `ScreenSignature`, `capture_screen_signature()`, `FeatureNode` (with `node_type`, `dismiss_action`, `is_list_sample`, `cross_app_triggers`), `ExplorationState`, `SessionSummary`, `CrossAppTrigger`, `ElementState`, `ElementDiff`, `ObserverResult`, `ExplorationTaskResult`
- `mineru/ui_auto/exploration/discovery.py` — `discover_features()` with list-container detection, structural ID generation, safety filters
- `mineru/ui_auto/exploration/screen_classifier.py` — `classify_screen_transition()`, modal detection, coverage analysis, passive event classification
- `mineru/ui_auto/exploration/planner.py` — `pick_next_node()`, `generate_exploration_goal()`
- `mineru/ui_auto/exploration/runner.py` — `run_exploration_session()`, session lifecycle, hard-reset fallback
- `mineru/ui_auto/exploration/state.py` — Load/save `_exploration.json`, crash recovery, home signature storage
- `mineru/ui_auto/exploration/safety.py` — `EXPLORATION_BLOCKED_ACTIONS`, `check_exploration_guard()`, tool-level enforcement
- `scripts/view_exploration.py` — Progress viewer

**Files to modify:**
- `mineru/ui_auto/main.py` — Add `learn` and `learn-cluster` subcommands (see CLI Command Definitions section)
- `mineru/ui_auto/context.py` — Add `exploration_mode: bool = False`
- `mineru/ui_auto/tools/tool_wrapper.py` — Add exploration guard check before tool execution
- `mineru/ui_auto/sdk/agent.py` — Add `run_exploration_task()` method

**Estimated effort:** 5-6 days (was 3-4, +2 days for issues 1-4)

### Phase 2: Smart Goal Generation

**Depends on:** Phase 1 complete

Replace template-based goals with LLM-generated goals that consider exploration context.

**Files to create:**
- `mineru/ui_auto/exploration/goal_generator.py` — LLM-powered goal generation

**Key function:**

```python
async def generate_smart_goal(
    node: FeatureNode,
    parent_path: list[str],
    existing_lessons: list[dict],
    sibling_nodes: list[FeatureNode],
    llm_service: LLMService,
) -> str:
    """Generate a context-aware exploration goal using an LLM call.

    Unlike the template-based generate_exploration_goal(), this considers:
    - What the agent already knows about this area (from lessons)
    - What sibling nodes have been explored (to avoid redundant work)
    - What's likely behind this element (inference from label + context)
    - Priority ordering (user-facing features over developer settings)

    The LLM call costs ~500-1000 tokens per goal (prompt + completion).
    """
    path_description = " > ".join(parent_path + [node.label])

    sibling_labels = [s.label for s in sibling_nodes if s.status == "explored"]
    relevant_lessons = [l for l in existing_lessons
                       if any(word in l.get("text", "").lower()
                             for word in node.label.lower().split())][:3]

    prompt = (
        f"You are exploring a mobile app. You need to navigate to: {path_description}\n\n"
        f"Already explored siblings: {', '.join(sibling_labels) or 'none'}\n"
        f"Relevant prior knowledge:\n"
        + "\n".join(f"  - {l.get('text', '')[:100]}" for l in relevant_lessons)
        + "\n\n"
        f"Generate a concise exploration goal. The agent should:\n"
        f"1. Navigate to {node.label}\n"
        f"2. Observe all available options and interactive elements\n"
        f"3. NOT change any settings or trigger destructive actions\n"
        f"4. Focus on features not yet covered by prior knowledge\n"
    )

    response = await llm_service.generate(prompt, max_tokens=150)
    return response.text
```

**Fallback:** If the LLM call fails or times out, fall back to `generate_exploration_goal()` (template-based).

**Estimated effort:** 1-2 days

### Phase 3: Exploration Quality Metrics

**Depends on:** Phase 1 complete

**Files to create:**
- `mineru/ui_auto/exploration/metrics.py` — Coverage, redundancy, staleness scoring
- `scripts/exploration_report.py` — Generate exploration quality report

**Files to modify:**
- `mineru/ui_auto/exploration/types.py` — Add `app_version` field to `ExplorationState`
- `mineru/ui_auto/exploration/runner.py` — Capture app version during `initialize_exploration()` using existing `_meta.json`

#### App Version Integration

The codebase already tracks app versions via `lessons/recorder.py`:
- `extract_app_version(ui_hierarchy)` — opportunistic extraction from UIAutomator root node
- `update_app_meta(lessons_dir, app_package, app_version)` — writes to `_meta.json`
- `LessonEntry.app_version` — per-lesson version tagging from `_meta.json`

Phase 3 reuses this infrastructure. Add `app_version` to `ExplorationState`:

```python
# In types.py — add to ExplorationState:
class ExplorationState(BaseModel):
    # ... existing fields ...
    app_version: str | None = None              # App version when exploration started (from _meta.json)
```

Capture it during initialization:

```python
# In runner.py — add to initialize_exploration(), after capturing home screen:
from mineru.ui_auto.lessons.recorder import extract_app_version, update_app_meta

# Opportunistically extract app version from the home screen hierarchy
app_version = extract_app_version(screen_data.elements)
if app_version:
    await update_app_meta(lessons_dir, app_package, app_version)
else:
    # Fall back to _meta.json (may have been captured by prior task runs)
    meta_path = lessons_dir / app_package / "_meta.json"
    if meta_path.exists():
        import json
        meta = json.loads(meta_path.read_text())
        app_version = meta.get("app_version")

state = ExplorationState(
    # ... existing fields ...
    app_version=app_version,
)
```

To get the *current* installed version for staleness comparison, add a helper that queries the device directly via `adb shell dumpsys package`:

```python
# In metrics.py:

async def get_installed_app_version(
    ctx: MobileUseContext,
    app_package: str,
) -> str | None:
    """Get the currently installed version of an app from the device.

    Uses `adb shell dumpsys package` which is more reliable than UIAutomator
    extraction (which only works when the app's root node exposes version info).

    Falls back to _meta.json if ADB is unavailable (e.g., iOS or cloud device).
    """
    try:
        from mineru.ui_auto.controllers.platform_specific_commands_controller import get_adb_device

        device = get_adb_device(ctx)
        output = str(device.shell(f"dumpsys package {app_package} | grep versionName"))
        # Output format: "    versionName=14.2.1"
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("versionName="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass

    # Fallback: read from _meta.json
    if ctx.lessons_dir:
        meta_path = ctx.lessons_dir / app_package / "_meta.json"
        if meta_path.exists():
            import json
            meta = json.loads(meta_path.read_text())
            return meta.get("app_version")

    return None
```

#### Metrics Computation

```python
# ── metrics.py ─────────────────────────────────────────────────

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from mineru.ui_auto.context import MobileUseContext
from mineru.ui_auto.exploration.state import compute_tree_stats
from mineru.ui_auto.exploration.types import ExplorationState, FeatureNode


@dataclass
class ExplorationMetrics:
    coverage_score: float                       # explored / reachable nodes (0.0–1.0)
    path_redundancy: float                      # fraction of duplicate paths (0.0–1.0)
    staleness_days: int                         # days since last exploration session
    app_version_at_exploration: str | None       # version when exploration ran
    current_app_version: str | None              # currently installed version
    version_changed: bool                        # True if versions differ
    suggested_re_explore: list[str] = field(default_factory=list)  # branches worth revisiting


async def compute_exploration_metrics(
    state: ExplorationState,
    lessons_dir: Path,
    ctx: MobileUseContext | None = None,
) -> ExplorationMetrics:
    """Compute quality metrics for an exploration run.

    Args:
        state: The exploration state to analyze
        lessons_dir: Path to the lessons directory (for loading lessons.jsonl)
        ctx: Optional device context (needed for current app version via ADB)

    Returns:
        ExplorationMetrics with coverage, redundancy, staleness, version, and
        re-explore suggestions.
    """
    # ── Coverage ──────────────────────────────────────────────
    stats = compute_tree_stats(state.root)
    reachable = stats.total - stats.skipped - stats.deep_limit
    coverage = stats.explored / reachable if reachable > 0 else 0.0

    # ── Path Redundancy ───────────────────────────────────────
    redundancy = _compute_path_redundancy(lessons_dir, state.app_package)

    # ── Staleness ─────────────────────────────────────────────
    staleness_days = _compute_staleness_days(state)

    # ── App Version ───────────────────────────────────────────
    app_version_at_exploration = state.app_version
    current_version: str | None = None
    if ctx is not None:
        current_version = await get_installed_app_version(ctx, state.app_package)
    version_changed = (
        app_version_at_exploration is not None
        and current_version is not None
        and app_version_at_exploration != current_version
    )

    # ── Re-explore Suggestions ────────────────────────────────
    suggested = _find_re_explore_branches(state.root)

    return ExplorationMetrics(
        coverage_score=coverage,
        path_redundancy=redundancy,
        staleness_days=staleness_days,
        app_version_at_exploration=app_version_at_exploration,
        current_app_version=current_version,
        version_changed=version_changed,
        suggested_re_explore=suggested,
    )


def _compute_staleness_days(state: ExplorationState) -> int:
    """Compute days since the last exploration session.

    Returns 0 if no sessions have been completed (freshly initialized).
    """
    if not state.last_session:
        return 0
    try:
        last = datetime.fromisoformat(state.last_session)
        now = datetime.now(UTC)
        return max(0, (now - last).days)
    except (ValueError, TypeError):
        return 0


def _compute_path_redundancy(lessons_dir: Path, app_package: str) -> float:
    """Compute the fraction of success_path lessons that are redundant.

    Two success_path lessons are "redundant" if they share >80% of their
    navigation steps (by action + target_text). High redundancy (>0.3)
    suggests the agent is re-learning known paths instead of exploring
    new areas.

    Reads lessons.jsonl synchronously (file is typically <1MB). Only
    considers lessons of type "success_path" that have a non-empty path.

    Returns 0.0 if there are fewer than 2 success_path lessons.
    """
    jsonl_path = lessons_dir / app_package / "lessons.jsonl"
    if not jsonl_path.exists():
        return 0.0

    # Load success_path lessons with paths
    paths: list[list[str]] = []
    for line in jsonl_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "success_path":
            continue
        path_steps = entry.get("path")
        if not path_steps or not isinstance(path_steps, list):
            continue
        # Normalize each step to "action:target_text" for comparison
        normalized = [
            f"{s.get('action', '')}:{s.get('target_text', '')}"
            for s in path_steps
        ]
        paths.append(normalized)

    if len(paths) < 2:
        return 0.0

    # Count how many paths are >80% similar to at least one other path
    redundant_count = 0
    for i, path_a in enumerate(paths):
        for j, path_b in enumerate(paths):
            if i >= j:
                continue
            overlap = _step_overlap(path_a, path_b)
            if overlap > 0.80:
                redundant_count += 1
                break  # path_a is redundant, no need to check more

    return redundant_count / len(paths)


def _step_overlap(path_a: list[str], path_b: list[str]) -> float:
    """Compute the fraction of steps shared between two paths.

    Uses set intersection over the longer path's length. Order-independent
    because the same steps may appear at different positions when the agent
    takes a slightly different route.
    """
    if not path_a or not path_b:
        return 0.0
    set_a = set(path_a)
    set_b = set(path_b)
    intersection = len(set_a & set_b)
    return intersection / max(len(set_a), len(set_b))


def _find_re_explore_branches(root: FeatureNode) -> list[str]:
    """Find branches where >50% of children are failed or skipped.

    These are candidates for re-exploration after an app update —
    previously-blocked screens may now be accessible.

    Returns a list of node labels (e.g., ["Accounts", "Developer options"]).
    """
    suggestions: list[str] = []

    def _walk(node: FeatureNode) -> None:
        if not node.children:
            return
        fail_skip = sum(
            1 for c in node.children if c.status in ("failed", "skipped")
        )
        if len(node.children) >= 2 and fail_skip / len(node.children) > 0.50:
            suggestions.append(node.label)
        for child in node.children:
            _walk(child)

    _walk(root)
    return suggestions
```

#### Exploration Report Script

```python
#!/usr/bin/env python3
# ── scripts/exploration_report.py ──────────────────────────────
"""Generate an exploration quality report for an app.

Usage:
    python scripts/exploration_report.py ./lessons/com.android.settings/

Outputs a human-readable report with coverage, redundancy, staleness,
version drift, and re-exploration suggestions.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mineru.ui_auto.exploration.metrics import (
    ExplorationMetrics,
    _compute_path_redundancy,
    _compute_staleness_days,
    _find_re_explore_branches,
)
from mineru.ui_auto.exploration.state import compute_tree_stats
from mineru.ui_auto.exploration.types import ExplorationState


def generate_report(app_dir: Path) -> None:
    """Generate and print an exploration quality report."""
    exploration_file = app_dir / "_exploration.json"
    if not exploration_file.exists():
        print(f"No exploration state found at {exploration_file}")
        sys.exit(1)

    state = ExplorationState(**json.loads(exploration_file.read_text()))
    stats = compute_tree_stats(state.root)

    # Compute metrics (without device context — no current version check)
    reachable = stats.total - stats.skipped - stats.deep_limit
    coverage = stats.explored / reachable if reachable > 0 else 0.0
    redundancy = _compute_path_redundancy(app_dir.parent, state.app_package)
    staleness = _compute_staleness_days(state)
    re_explore = _find_re_explore_branches(state.root)

    # Print report
    print(f"\n{'=' * 60}")
    print(f"  Exploration Quality Report: {state.app_package}")
    print(f"{'=' * 60}\n")

    # Coverage
    coverage_pct = coverage * 100
    coverage_status = "GOOD" if coverage >= 0.85 else ("FAIR" if coverage >= 0.60 else "LOW")
    print(f"  Coverage:    {coverage_pct:.1f}% ({stats.explored}/{reachable} reachable nodes) [{coverage_status}]")

    # Redundancy
    redundancy_pct = redundancy * 100
    redundancy_status = "OK" if redundancy <= 0.30 else "HIGH"
    print(f"  Redundancy:  {redundancy_pct:.1f}% of paths are duplicates [{redundancy_status}]")

    # Staleness
    if staleness == 0:
        staleness_status = "FRESH"
    elif staleness <= 30:
        staleness_status = "OK"
    else:
        staleness_status = "STALE"
    print(f"  Staleness:   {staleness} days since last session [{staleness_status}]")

    # Version
    if state.app_version:
        print(f"  App Version: {state.app_version} (at exploration time)")
    else:
        print(f"  App Version: unknown")

    # Node breakdown
    print(f"\n  Node Breakdown:")
    print(f"    Total:      {stats.total}")
    print(f"    Explored:   {stats.explored}")
    print(f"    Pending:    {stats.pending}")
    print(f"    Failed:     {stats.failed}")
    print(f"    Skipped:    {stats.skipped}")
    print(f"    Deep Limit: {stats.deep_limit}")

    # Sessions
    print(f"\n  Sessions:     {state.sessions_completed}")
    if state.sessions:
        total_goals = sum(s.goals_attempted for s in state.sessions)
        total_lessons = sum(s.lessons_recorded for s in state.sessions)
        print(f"    Goals attempted: {total_goals}")
        print(f"    Lessons recorded: {total_lessons}")

    # Re-explore suggestions
    if re_explore:
        print(f"\n  Suggested Re-exploration:")
        for branch in re_explore:
            print(f"    - {branch} (>50% of children failed/skipped)")

    # Recommendations
    print(f"\n  Recommendations:")
    if coverage < 0.60:
        print(f"    - Run more exploration sessions to increase coverage")
    if redundancy > 0.30:
        print(f"    - Agent is re-learning known paths; consider DFS strategy to explore new areas")
    if staleness > 30 and state.app_version:
        print(f"    - Exploration is stale; re-run with --reset if app has been updated")
    if re_explore:
        print(f"    - Re-explore failed branches after app update (may now be accessible)")
    if coverage >= 0.85 and redundancy <= 0.30 and staleness <= 30:
        print(f"    - Exploration is healthy — no action needed")

    print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/exploration_report.py <app_lessons_dir>")
        print("  e.g.: python scripts/exploration_report.py ./lessons/com.android.settings/")
        sys.exit(1)
    generate_report(Path(sys.argv[1]))
```

#### Example Report Output

```
============================================================
  Exploration Quality Report: com.android.settings
============================================================

  Coverage:    82.1% (32/39 reachable nodes) [FAIR]
  Redundancy:  15.0% of paths are duplicates [OK]
  Staleness:   3 days since last session [OK]
  App Version: 14.2.1 (at exploration time)

  Node Breakdown:
    Total:      55
    Explored:   32
    Pending:    7
    Failed:     2
    Skipped:    9
    Deep Limit: 5

  Sessions:     3
    Goals attempted: 41
    Lessons recorded: 28

  Suggested Re-exploration:
    - Accounts (>50% of children failed/skipped)

  Recommendations:
    - Run more exploration sessions to increase coverage
    - Re-explore failed branches after app update (may now be accessible)
```

**Estimated effort:** 2-3 days (was 1-2, +1 day for version integration + report script)

### Phase 4: Dual-App Cluster Mode (NEW)

**Depends on:** Phase 1 complete (single-app loop must be stable)

**Files to create:**
- `mineru/ui_auto/exploration/orchestrator.py` — `MasterOrchestrator`: coordinates two `ExplorationPlanner` instances, trigger detection, Observer Mode dispatch, cross-app event correlation. Full implementation in the "Master Orchestrator Implementation" section above.
- `mineru/ui_auto/exploration/observer.py` — `observe_for_cross_app_event()`: identity-based polling loop with settle window, two-tiered diffing (identity key vs. state key), timeout handling
- `mineru/ui_auto/exploration/passive_classifier.py` — `classify_passive_event()`: `banner_notification` vs. `screen_change` vs. `element_appeared` vs. **`element_updated`** detection. Uses `ElementDiff` (identity-based) instead of raw element lists
- `mineru/ui_auto/exploration/identity.py` — `_compute_element_identity_key()`, `_build_identity_index()`, `_compute_identity_diff()`. Core anti-bloat module for dynamic UIs (maps, live status, timers)
- `mineru/ui_auto/exploration/helpers.py` — `_extract_key_text()`, `_quantize_bounds()`, `_update_median()`, `_compute_coverage()`, `_find_child_by_identity()`, `_find_current_screen_node()`

**Files to modify:**
- `mineru/ui_auto/exploration/screen_classifier.py` — Add `agent_performed_action` parameter to `classify_screen_transition()`, use identity-based diffing in passive_event branch
- `mineru/ui_auto/exploration/runner.py` — Update `passive_event` handling to use identity-based diffing and upsert `dynamic_element` nodes
- `mineru/ui_auto/main.py` — Add `learn-cluster` subcommand with `--primary`, `--secondary`, `--observer-timeout`, `--trigger-hints` flags

**Key design decisions:**
- Each agent runs its own exploration loop independently; the orchestrator only intervenes at trigger points
- Observer Mode is non-blocking for the secondary agent — it pauses the secondary's active exploration briefly, then resumes
- Cross-app triggers are recorded bidirectionally (as trigger on primary, as `passive_event`/`dynamic_element` on secondary)
- `reliability_score` is auto-computed; low-reliability triggers (<0.3 after 5+ attempts) are downgraded and eventually pruned
- **Identity-based diffing** prevents tree bloat from dynamic elements: map avatars, live counters, status icons are matched by `resource-id` / `content-desc` prefix (identity key), not by `bounds` (state key). Same identity → upsert existing node. New identity → create new node
- **Upsert semantics** for `element_updated` triggers: one trigger entry per logical element per primary action, with `last_observed_state` overwritten on each observation. No append-per-movement
- **Settle window** (2s) in Observer Mode prevents premature return on timestamp-only changes while waiting for the real cross-app event

**Estimated effort:** 5-6 days (was 4-5, +1 day for identity diffing + dynamic element handling)

### Phase 5: Cross-App Lesson Integration (NEW)

**Depends on:** Phase 4 complete

Cross-app triggers need to be surfaced during real tasks so the agent knows "action X in App A causes effect Y in App B."

**Files to create:**
- `mineru/ui_auto/exploration/cross_app_lessons.py` — Generate cross-app lesson entries from trigger data

**Key function:**

```python
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
    lessons = []

    def _walk(node: FeatureNode, path: list[str]):
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
            text += f"typical latency: {trigger.latency_ms/1000:.1f}s)"

            lessons.append({
                "type": "cross_app_trigger",
                "app_package": app_package,
                "text": text,
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
```

**Files to modify:**
- `mineru/ui_auto/lessons/loader.py` — Add `"cross_app_trigger"` to the recognized lesson types. The existing `load_lessons()` function reads `lessons.jsonl` line by line; cross-app trigger entries are loaded alongside regular lessons with no special handling needed.
- `mineru/ui_auto/agents/contextor/contextor.py` — When building the Cortex's context for a task that mentions a second app (detected from goal text), include cross-app trigger lessons from that app's `lessons.jsonl`. The Contextor's `_build_lessons_context()` method gets a new filter: `if task mentions app B, also load lessons from app B's directory`.

**Estimated effort:** 2-3 days

---

## Test Strategy

Each module can be tested in isolation with mock UI hierarchies. Device tests are only needed for the runner and orchestrator.

### Unit Tests (No Device Required)

```python
# tests/ui_auto/exploration/test_discovery.py
# ─── Test fixtures: mock UI hierarchies ───

SETTINGS_HIERARCHY = [
    {"text": "Network & internet", "clickable": True, "className": "TextView",
     "bounds": {"left": 0, "top": 100, "right": 1080, "bottom": 200}, "index": 0},
    {"text": "Connected devices", "clickable": True, "className": "TextView",
     "bounds": {"left": 0, "top": 200, "right": 1080, "bottom": 300}, "index": 1},
    {"text": "", "clickable": False, "className": "Switch",
     "bounds": {"left": 900, "top": 100, "right": 1080, "bottom": 200}, "index": 2},
    {"text": "12:34", "clickable": False, "className": "TextView",
     "bounds": {"left": 0, "top": 0, "right": 100, "bottom": 50}, "index": 0},  # status bar
]

GMAIL_INBOX_HIERARCHY = [
    {"text": "Email from Alice", "clickable": True, "className": "TextView",
     "bounds": {"left": 0, "top": 100, "right": 1080, "bottom": 200}, "index": 0},
    {"text": "Email from Bob", "clickable": True, "className": "TextView",
     "bounds": {"left": 0, "top": 200, "right": 1080, "bottom": 300}, "index": 1},
    {"text": "Email from Carol", "clickable": True, "className": "TextView",
     "bounds": {"left": 0, "top": 300, "right": 1080, "bottom": 400}, "index": 2},
    # All inside a RecyclerView
    {"text": "", "clickable": False, "className": "RecyclerView",
     "bounds": {"left": 0, "top": 50, "right": 1080, "bottom": 2400}, "index": 0},
]


def test_discover_features_basic():
    nodes = discover_features(SETTINGS_HIERARCHY, "com.android.settings/.Settings")
    labels = [n.label for n in nodes]
    assert "Network & internet" in labels
    assert "Connected devices" in labels
    assert "12:34" not in labels       # status bar element filtered
    assert len([n for n in nodes if n.label == ""]) == 0  # Switch filtered


def test_discover_features_list_sampling():
    nodes = discover_features(GMAIL_INBOX_HIERARCHY, "com.google.android.gm/.ConversationList")
    # Only 2 items sampled from the RecyclerView, not all 3
    assert len([n for n in nodes if n.is_list_sample]) <= 2


def test_discover_features_dangerous_labels():
    hierarchy = [
        {"text": "Factory reset", "clickable": True, "className": "Button",
         "bounds": {"left": 0, "top": 100, "right": 500, "bottom": 200}, "index": 0},
    ]
    nodes = discover_features(hierarchy, "com.android.settings/.Settings")
    assert nodes[0].status == "skipped"
    assert nodes[0].skip_reason == "dangerous_label"


# tests/ui_auto/exploration/test_identity.py

MAP_BASELINE = [
    {"resource-id": "com.app:id/child_avatar", "text": "Stationary", "className": "ImageView",
     "bounds": {"left": 100, "top": 200, "right": 150, "bottom": 250}, "index": 0},
    {"resource-id": "com.app:id/search_bar", "text": "Search", "className": "EditText",
     "bounds": {"left": 0, "top": 0, "right": 1080, "bottom": 80}, "index": 0},
]

MAP_AFTER_MOVE = [
    {"resource-id": "com.app:id/child_avatar", "text": "Driving", "className": "ImageView",
     "bounds": {"left": 150, "top": 250, "right": 200, "bottom": 300}, "index": 0},  # moved + text changed
    {"resource-id": "com.app:id/search_bar", "text": "Search", "className": "EditText",
     "bounds": {"left": 0, "top": 0, "right": 1080, "bottom": 80}, "index": 0},  # unchanged
]


def test_identity_diff_detects_update_not_new():
    baseline = _build_identity_index(MAP_BASELINE, "MapActivity")
    current = _build_identity_index(MAP_AFTER_MOVE, "MapActivity")
    diff = _compute_identity_diff(baseline, current)
    assert len(diff.appeared) == 0      # No genuinely new elements
    assert len(diff.updated) == 1       # Avatar updated (same resource-id, different bounds+text)
    assert len(diff.unchanged) == 1     # Search bar unchanged
    assert diff.updated[0]["identity_key"] == "MapActivity:com.app:id/child_avatar"


def test_classify_passive_event_element_updated():
    baseline = _build_identity_index(MAP_BASELINE, "MapActivity")
    current = _build_identity_index(MAP_AFTER_MOVE, "MapActivity")
    diff = _compute_identity_diff(baseline, current)
    event_type = classify_passive_event(diff, MAP_AFTER_MOVE)
    assert event_type == "element_updated"


# tests/ui_auto/exploration/test_planner.py

def test_pick_next_node_bfs():
    root = FeatureNode(id="root", label="Home", status="explored", children=[
        FeatureNode(id="a", label="A", status="pending"),
        FeatureNode(id="b", label="B", status="explored", children=[
            FeatureNode(id="b1", label="B1", status="pending"),
        ]),
    ])
    node, path = pick_next_node(root, max_depth=4, strategy="breadth_first")
    assert node.id == "a"  # BFS picks the first level-1 pending node


def test_pick_next_node_dfs():
    root = FeatureNode(id="root", label="Home", status="explored", children=[
        FeatureNode(id="a", label="A", status="explored", children=[
            FeatureNode(id="a1", label="A1", status="pending"),
        ]),
        FeatureNode(id="b", label="B", status="pending"),
    ])
    node, path = pick_next_node(root, max_depth=4, strategy="depth_first")
    assert node.id == "a1"  # DFS goes deep before wide


def test_merge_children_dedup():
    existing = [FeatureNode(id="abc123", label="Wi-Fi", status="explored")]
    discovered = [
        FeatureNode(id="abc123", label="Wi-Fi", status="pending"),  # same ID
        FeatureNode(id="def456", label="Bluetooth", status="pending"),  # new
    ]
    merged = _merge_children(existing, discovered)
    assert len(merged) == 2
    # Existing node preserved (status=explored, not overwritten to pending)
    assert next(n for n in merged if n.id == "abc123").status == "explored"
```

### Integration Tests (Require Device or Emulator)

```
tests/ui_auto/exploration/test_runner_integration.py     — run_exploration_task() with a real app
tests/ui_auto/exploration/test_observer_integration.py   — observe_for_cross_app_event() with two apps
tests/ui_auto/exploration/test_orchestrator_integration.py — full cluster session (requires 2 emulators or devices)
```

These use `@pytest.mark.device` and are excluded from CI by default. Run with `pytest -m device`.

---

## Platform Scope

### V1: Android Only

All code examples in this document use Android-specific APIs:
- `am force-stop` / `monkey` for app lifecycle
- ADB device serials for multi-device setup
- `get_current_foreground_package_async()` for activity detection
- `resource-id` for element identity keys

### Future: iOS Support

iOS self-learning would require platform-specific implementations for:

| Capability | Android | iOS Equivalent |
|-----------|---------|---------------|
| App reset | `am force-stop` + `monkey` | `idb terminate` + `idb launch` via `ios_client` |
| Foreground check | `dumpsys activity` | `idb list-apps --state=running` |
| Element identity | `resource-id` | `accessibilityIdentifier` (priority 1), `label` prefix (priority 2) |
| UI hierarchy | `uiautomator dump` | `XCTest` via WDA |
| Multi-device | ADB serial | UDID via `idb` |

The exploration loop, feature tree, identity diffing, and Observer Mode are **platform-agnostic**. Only the controller calls (`create_device_controller`, `get_screen_data`, `press_back`, `force-stop`) need iOS variants — and these already exist in the codebase (`ios_controller.py`, `wda_client.py`).

**Recommendation:** Abstract the 4 platform-specific operations behind the existing `MobileDeviceController` protocol. Add iOS implementations in Phase 1 if iOS testing is planned, or defer to a Phase 6.

---

## Future Enhancements (Out of Scope for V1)

1. **Parallel exploration** — run multiple device instances exploring different branches simultaneously
2. **Regression detection** — re-explore after app update, flag navigation paths that broke
3. **Exploration scheduling** — run self-learning as a nightly cron job
4. **UI mapping generation** — record element positions and types (not just navigation paths) for richer lesson data
5. **Exploration transfer** — share exploration state + lessons across team members via git
6. **Multi-app clusters (3+ apps)** — extend Master Orchestrator beyond dual-app to N-app coordination (e.g., Parent + Child + School portal)
7. **Cross-app trigger replay** — deterministic replay of cross-app trigger sequences for regression testing
8. **iOS self-learning** — platform-specific controller implementations (see Platform Scope above)
