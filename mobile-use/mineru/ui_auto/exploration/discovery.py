"""Screen feature discovery for self-learning exploration.

Extracts navigable feature nodes from a screen's UI hierarchy, handling
list-view sampling, structural dedup, and dangerous action filtering.
"""

from __future__ import annotations

import hashlib
import re

from mineru.ui_auto.exploration.types import FeatureNode, MAX_ELEMENTS_SCAN_LIMIT, MAX_FEATURES_PER_SCREEN


def parse_bounds(bounds_value) -> dict:
    """Normalize bounds from any format to a dict with left/top/right/bottom.

    UIAutomator2 returns bounds as a string like "[0,66][1080,2424]".
    Some code paths may already have it as a dict. This handles both.
    Returns an empty dict if parsing fails.
    """
    if isinstance(bounds_value, dict):
        # Already a dict — check for left/top/right/bottom keys
        if "left" in bounds_value:
            return bounds_value
        return {}
    if isinstance(bounds_value, str):
        match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_value)
        if match:
            return {
                "left": int(match.group(1)),
                "top": int(match.group(2)),
                "right": int(match.group(3)),
                "bottom": int(match.group(4)),
            }
    return {}


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
    "call 911", "911", "call emergency", "emergency call",
    "send sos", "tap to send sos", "sos",
    "mark as safe",
}

# Partial matches — skip any label CONTAINING these substrings
SKIP_LABEL_SUBSTRINGS = [
    "call 911", "send sos", "emergency", "sos",
    "delete all", "factory reset", "erase all",
]

# ── System UI / Notification Noise Filtering ───────────────────
# Android notification shade, status bar icons, and connectivity indicators
# leak into the UI hierarchy as text elements. These are never navigation
# targets within the app being explored and must be filtered out.

# Exact matches for system UI labels
SYSTEM_UI_LABELS = {
    "hotspot",
}

# Substring matches — any label containing these is system UI noise
SYSTEM_UI_SUBSTRINGS = [
    "notification:",          # "Pixel Setup notification:", "Android System notification:"
    " percent.",              # "Battery charging, 100 percent."
]

# Regex patterns for system UI noise
SYSTEM_UI_PATTERNS = [
    re.compile(r"^.{0,30}\bnotification:?\s*$", re.IGNORECASE),   # "Life360 notification:"
    re.compile(r"\b(one|two|three|four|five|no)\s+bars?\.?$", re.IGNORECASE),  # "Wifi three bars.", "Verizon , two bars."
    re.compile(r"^(Wifi|Wi-Fi|Bluetooth|Mobile data|Cellular)\s+(signal|connected|disconnected|off|on)", re.IGNORECASE),  # "Wifi signal full."
    re.compile(r"^Battery\s+(charging|full|low|draining)", re.IGNORECASE),  # "Battery charging, 100 percent."
]

# Known list container class names for list-view sampling.
LIST_CLASSES = {"RecyclerView", "ListView", "AbsListView"}


def discover_features(
    ui_hierarchy: list[dict],
    current_activity: str,
    screen_width: int = 1080,
    screen_height: int = 2400,
) -> list[FeatureNode]:
    """Extract navigable feature nodes from a screen's UI hierarchy.

    Two-pass approach:
    1. Detect list containers (RecyclerView, ListView). For each, sample
       only 1-2 items. This prevents Gmail-inbox-style explosion.
    2. Discover standalone interactive elements (buttons, menu items)
       outside list containers.

    Uses structural IDs (activity + className + index + bounds quadrant)
    instead of text-based IDs to prevent duplicate nodes for dynamic content.

    Args:
        ui_hierarchy: Flat list of UI element dicts from get_screen_data()
        current_activity: Current Android activity name
        screen_width: Device screen width in pixels
        screen_height: Device screen height in pixels

    Returns:
        List of FeatureNode with status="pending" (or "skipped" for dangerous labels)
    """
    features: list[FeatureNode] = []
    seen_texts: set[str] = set()
    list_containers = _detect_list_containers(ui_hierarchy)

    for i, elem in enumerate(ui_hierarchy):
        if i >= MAX_ELEMENTS_SCAN_LIMIT:
            break
        if len(features) >= MAX_FEATURES_PER_SCREEN:
            break

        text = (elem.get("text") or "").strip()
        # Fall back to content-desc for icon-only elements (e.g., share, settings icons).
        # These have no text label but do have an accessibility description.
        content_desc = (elem.get("contentDescription") or elem.get("content-desc") or "").strip()
        if not text and content_desc and len(content_desc) <= 60:
            text = content_desc
        if not text or len(text) > 60 or text in seen_texts:
            continue

        class_name = elem.get("className", "")
        # Determine if the element is interactive. Android bottom nav tabs,
        # menu items, and many clickable surfaces report clickable=false on their
        # child TextViews while the parent container handles clicks. Since the
        # agent can tap by coordinates, we accept any text element that has valid
        # bounds — the tap will succeed and the exploration loop will discover
        # whether a screen change occurred (if not, the node gets marked explored
        # with no children).
        has_bounds = bool(parse_bounds(elem.get("bounds")))

        if _is_toggle_or_input(class_name):
            continue
        if _is_status_bar_element(elem):
            continue
        if _is_system_ui_noise(text):
            continue
        if _is_dialog_control(text):
            continue
        if _is_non_navigational_text(text):
            continue

        # Check dangerous labels — create skipped node, don't silently drop
        if _is_dangerous(text):
            seen_texts.add(text)
            features.append(FeatureNode(
                id=_make_structural_id(elem, current_activity, screen_width, screen_height),
                label=text,
                nav_action=f"tap('{text}')",
                status="skipped",
                skip_reason="dangerous_label",
            ))
            continue

        # List-view sampling: only take first 2 items per container
        container = _find_parent_container(elem, list_containers)
        if container is not None:
            if container["sampled_count"] >= 2:
                continue
            container["sampled_count"] += 1

        if has_bounds and text:
            seen_texts.add(text)
            features.append(FeatureNode(
                id=_make_structural_id(elem, current_activity, screen_width, screen_height),
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
    bounds = parse_bounds(elem.get("bounds"))
    top = bounds.get("top", 100)
    bottom = bounds.get("bottom", 100)
    # Status bar is typically 0-66px on modern Android (24dp * 2.75 density)
    return top < 5 and bottom < 75


def _is_system_ui_noise(text: str) -> bool:
    """Return True if text is a system UI / notification element, not app content.

    Filters: notification shade entries, connectivity indicators (wifi/signal bars),
    battery status, hotspot, and other Android system chrome that leaks into
    the flattened UI hierarchy.
    """
    lower = text.lower().strip()

    if lower in SYSTEM_UI_LABELS:
        return True

    if any(sub in lower for sub in SYSTEM_UI_SUBSTRINGS):
        return True

    return any(pat.search(text) for pat in SYSTEM_UI_PATTERNS)


def _is_dialog_control(text: str) -> bool:
    """Return True if text is a generic dialog/modal control, not a navigable feature.

    Dialog controls like "Close", "Cancel", "OK", "Save", "Back" are transient
    UI elements that only exist when a specific dialog is open. Generating
    exploration goals for them wastes LLM budget because the agent can't
    reliably reproduce the dialog state from the home screen.
    """
    lower = text.lower().strip()
    # Exact matches for dialog controls
    dialog_controls = {
        "close", "cancel", "ok", "okay", "done", "save",
        "back", "dismiss", "got it", "not now", "skip",
        "yes", "no", "confirm", "deny", "allow", "block",
        "accept", "decline", "later", "maybe later",
        "close sheet", "drag handle",
        "back, button", "close, button", "navigate up",
    }
    if lower in dialog_controls:
        return True

    # Pattern: "X button" or "X icon" where X is a control verb
    control_verbs = {"close", "back", "cancel", "save", "dismiss", "check mark"}
    for verb in control_verbs:
        if lower.startswith(verb) and ("button" in lower or "icon" in lower):
            return True

    return False


def _is_non_navigational_text(text: str) -> bool:
    """Return True if text is a display value, not a navigable feature label.

    Filters out data display patterns that are never navigation targets:
    - Time patterns (1:44, 8:30 AM, 12:00 PM, 00:00.00, 00h 00m 00s)
    - Single characters (S, M, T)
    - Pure numbers (1, 23, 100)
    - Temperature values (65°, 73°F)
    - Percentage values (10%, 85%)
    - Date patterns (Mon-Fri, Sun Sat, 3/27, 3/28)
    - Day/time labels (Now, Today, 10 AM, 4 PM)
    """
    stripped = text.strip()

    # Single characters — day abbreviations, list bullets, etc.
    if len(stripped) <= 1:
        return True

    # Pure numbers (with optional dots/commas)
    if stripped.replace(".", "").replace(",", "").isdigit():
        return True

    # Time patterns: "1:44", "8:30 AM", "12:00 PM", "9:00"
    if re.match(r"^\d{1,2}:\d{2}(\s*[APap][Mm])?$", stripped):
        return True

    # Stopwatch/duration patterns: "00:00.00", "01:20:07.48", "00h 00m 00s", "1:23.45"
    if re.match(r"^\d{1,2}:\d{2}(:\d{2})?\.\d{2,}$", stripped):
        return True
    if re.match(r"^\d+h\s*\d+m\s*\d+s$", stripped):
        return True

    # Temperature: "65°", "73°F", "73°C", "-5°"
    if re.match(r"^-?\d+°[FCfc]?$", stripped):
        return True

    # Percentage: "10%", "85%"
    if re.match(r"^\d+%$", stripped):
        return True

    # Date patterns: "3/27", "3/28", "12/25", "2026-03-27"
    if re.match(r"^\d{1,2}/\d{1,2}$", stripped):
        return True
    if re.match(r"^\d{4}-\d{2}-\d{2}$", stripped):
        return True

    # Hour-of-day labels: "10 AM", "4 PM", "12 AM"
    if re.match(r"^\d{1,2}\s*[APap][Mm]$", stripped):
        return True

    # Short day-of-week or relative time words
    day_patterns = {
        "mon", "tue", "wed", "thu", "fri", "sat", "sun",
        "mon-fri", "sun sat", "mon-sat", "mon-sun",
        "now", "today", "tomorrow", "yesterday",
    }
    if stripped.lower() in day_patterns:
        return True

    lower = stripped.lower()

    # Street addresses: "1649 Bennet Creek Ovlk", "3288 Ella Way"
    if re.match(r"^\d+\s+[A-Z]", stripped) and len(stripped.split()) >= 2:
        return True

    # "Since" timestamps: "Since 10:24 PM yesterday", "Since 02:04 PM"
    if lower.startswith("since "):
        return True

    # "Last updated" timestamps: "Last updated 7 seconds ago", "Last updated 23 hours ago"
    if lower.startswith("last updated"):
        return True

    # Distance: "0 mile away", "2.5 miles away"
    if re.match(r"^\d+\.?\d*\s*miles?\s+away$", lower):
        return True

    # Notification/status messages (contain "was sent", "were sent")
    if "was sent" in lower or "were sent" in lower:
        return True

    return False


def _is_dangerous(text: str) -> bool:
    """Return True if tapping this element could cause irreversible damage."""
    lower = text.lower().strip()
    if lower in SKIP_LABELS:
        return True
    return any(sub in lower for sub in SKIP_LABEL_SUBSTRINGS)


def _find_parent_container(
    elem: dict,
    list_containers: list[dict],
) -> dict | None:
    """Check if an element is geometrically inside a detected list container.

    Uses bounding-box containment: if the element's bounds are fully within
    a container's bounds, it belongs to that container.
    """
    elem_bounds = parse_bounds(elem.get("bounds"))
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
    containers = []

    for elem in ui_hierarchy:
        class_name = elem.get("className", "")
        if any(lc in class_name for lc in LIST_CLASSES):
            containers.append({
                "bounds": parse_bounds(elem.get("bounds")),
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

    Uses: activity + className + index-in-parent + grid cell (8x8) +
    resource-id (when available, since it's structural, not content).
    Two emails in the same list position get the same structural ID.

    Screen dimensions are used to compute the grid cell.
    """
    class_name = elem.get("className", "unknown")
    index = elem.get("index", 0)
    bounds = parse_bounds(elem.get("bounds"))
    center_x = (bounds.get("left", 0) + bounds.get("right", 0)) / 2
    center_y = (bounds.get("top", 0) + bounds.get("bottom", 0)) / 2
    # Use 8x8 grid for finer spatial discrimination than 2x2 quadrants
    grid_x = int(center_x / max(screen_width, 1) * 8)
    grid_y = int(center_y / max(screen_height, 1) * 8)
    grid_cell = f"{grid_x},{grid_y}"

    # Include resource-id when available — it's structural (not content)
    # and disambiguates elements at the same position
    resource_id = elem.get("resourceId") or elem.get("resource-id") or ""

    raw = f"{activity}:{class_name}:{index}:{grid_cell}:{resource_id}"
    return hashlib.md5(raw.encode()).hexdigest()[:8]
