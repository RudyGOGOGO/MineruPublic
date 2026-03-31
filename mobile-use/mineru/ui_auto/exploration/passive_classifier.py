"""Passive event classification for UI changes without local agent action.

Classifies identity-based diffs to distinguish genuinely new elements from
existing elements that updated their state -- critical for live maps,
status indicators, and any continuously-updating UI.
"""

from __future__ import annotations

from mineru.ui_auto.exploration.helpers import compute_coverage
from mineru.ui_auto.exploration.types import ElementDiff


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
    new_elements = []
    for e in diff.appeared:
        state = e.get("state")
        if state is not None:
            if isinstance(state, dict):
                new_elements.append(state)
            else:
                # ElementState object -- convert to dict-like for coverage computation
                new_elements.append({"bounds": state.bounds})

    # Priority 3: Check for top-banner pattern
    top_elements = []
    for e in diff.appeared:
        state = e.get("state")
        if state is not None:
            bounds = state.bounds if hasattr(state, "bounds") else state.get("bounds", {})
            if isinstance(bounds, dict) and bounds.get("top", 999) < 200:
                top_elements.append(e)

    if len(top_elements) > 0 and len(top_elements) >= len(diff.appeared) * 0.5:
        return "banner_notification"

    # Priority 4: Check for full-screen change
    total_area = compute_coverage(new_elements)
    if total_area > 0.80:
        return "screen_change"

    # Priority 5: Mixed scenario -- both new and updated elements
    # If updates dominate (>= 2x appeared count), the primary event is an update
    if diff.updated and len(diff.updated) >= len(diff.appeared) * 2:
        return "element_updated"

    return "element_appeared"
