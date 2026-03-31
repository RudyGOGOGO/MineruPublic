"""Screen transition classification for self-learning exploration.

Classifies whether a screen transition is a full navigation, modal overlay,
passive event, or unchanged — critical for building an accurate feature tree.
"""

from __future__ import annotations

from mineru.ui_auto.exploration.discovery import parse_bounds


def classify_screen_transition(
    parent_hierarchy: list[dict],
    current_hierarchy: list[dict],
    screen_width: int,
    screen_height: int,
    agent_performed_action: bool = True,
) -> str:
    """Classify whether a screen transition is a full navigation, modal overlay,
    passive event, or unchanged.

    Args:
        parent_hierarchy: UI elements from the screen before the action
        current_hierarchy: UI elements from the screen after the action
        screen_width: Device screen width in pixels
        screen_height: Device screen height in pixels
        agent_performed_action: Whether the local agent just performed a tap/action.
            If False, any detected change is classified as a passive event (cross-app
            trigger, push notification, background update) rather than a navigational child.

    Returns: "full_screen", "modal", "passive_event", or "unchanged"
    """
    if not agent_performed_action:
        # UI changed without a local action — use identity-based diffing
        # to avoid false positives from dynamic elements like map avatars.
        from mineru.ui_auto.exploration.identity import (
            build_identity_index,
            compute_identity_diff,
        )

        activity = current_hierarchy[0].get("activity", "") if current_hierarchy else ""
        diff = compute_identity_diff(
            build_identity_index(parent_hierarchy, activity),
            build_identity_index(current_hierarchy, activity),
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
    min_x = min(parse_bounds(e["bounds"]).get("left", 0) for e in new_elements)
    min_y = min(parse_bounds(e["bounds"]).get("top", 0) for e in new_elements)
    max_x = max(parse_bounds(e["bounds"]).get("right", screen_width) for e in new_elements)
    max_y = max(parse_bounds(e["bounds"]).get("bottom", screen_height) for e in new_elements)

    new_area = (max_x - min_x) * (max_y - min_y)
    coverage_ratio = new_area / screen_area

    if coverage_ratio < 0.80:
        return "modal"
    return "full_screen"


def extract_new_elements(
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


def _bounds_key(elem: dict) -> str:
    """Create a hashable key from element bounds for set comparison."""
    b = parse_bounds(elem.get("bounds"))
    return f"{b.get('left', 0)},{b.get('top', 0)},{b.get('right', 0)},{b.get('bottom', 0)}"
