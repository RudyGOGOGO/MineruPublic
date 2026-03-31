from __future__ import annotations

from pydantic import BaseModel, Field

from mineru.ui_auto.lessons.types import ScreenSignature


# ── Screen Signature Helpers ─────────────────────────────────

# Maximum elements to SCAN from the hierarchy. Set high enough to reach
# bottom navigation tabs which often appear late in the flattened tree.
# The output is capped by dedup (seen_texts) and filters, not this limit.
MAX_ELEMENTS_SCAN_LIMIT = 200

# Maximum feature nodes to OUTPUT per screen (prevents tree explosion).
MAX_FEATURES_PER_SCREEN = 30


def capture_screen_signature(
    app_info: str | None,
    ui_elements: list[dict],
    max_elements: int = MAX_FEATURES_PER_SCREEN,
) -> ScreenSignature:
    """Capture a screen signature from UI hierarchy data.

    Args:
        app_info: Current foreground activity/package string
        ui_elements: Flat list of UI element dicts from get_screen_data()
        max_elements: Maximum elements to include in key_elements

    Returns:
        ScreenSignature with activity and key text elements
    """
    key_texts = []
    for elem in ui_elements[:max_elements]:
        text = (elem.get("text") or "").strip()
        if text and len(text) <= 60:
            key_texts.append(text)

    return ScreenSignature(
        activity=app_info,
        key_elements=key_texts,
    )


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


class ExplorationState(BaseModel):
    """Persisted state for an app's exploration progress. Stored in _exploration.json."""

    app_package: str                                # e.g., "com.android.settings"
    app_name: str | None = None                     # Human-readable app name
    app_version: str | None = None                  # App version when exploration started (from _meta.json)
    created: str | None = None                      # ISO 8601 timestamp of first session
    last_session: str | None = None                 # ISO 8601 timestamp of most recent session
    sessions_completed: int = 0
    root: FeatureNode                               # The feature tree root (app home screen)
    home_signature: ScreenSignature | None = None   # Captured at init for home-reset verification
    global_nav_labels: list[str] = []               # Labels of persistent nav elements (tabs, toolbar)
                                                    # — filtered from ALL child discoveries to prevent duplication
    sessions: list[SessionSummary] = []             # One entry per completed session


# ── Element Diffing (Observer Mode) ──────────────────────────

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
