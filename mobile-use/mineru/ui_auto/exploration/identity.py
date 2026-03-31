"""Element identity computation for identity-based UI diffing.

Core anti-bloat module for dynamic UIs (maps, live status, timers).
Elements are matched by stable identity (resource-id, content-desc prefix)
rather than exact bounds, preventing duplicate tree nodes for moving elements.
"""

from __future__ import annotations

from mineru.ui_auto.exploration.discovery import parse_bounds
from mineru.ui_auto.exploration.types import ElementDiff, ElementState


def compute_element_identity_key(elem: dict, activity: str) -> str:
    """Compute a stable identity key for an element.

    Uses properties that identify WHICH element this is, ignoring
    properties that describe its current STATE (position, dynamic text).

    Priority order:
    1. resource-id (most stable -- e.g., "com.app:id/child_avatar")
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
        # Take first 3 words as stable prefix -- dynamic suffixes like
        # "- Driving" or "- 2 min ago" are stripped
        prefix = " ".join(content_desc.split()[:3])
        return f"{activity}:{elem.get('className', '')}:{prefix}"

    # Priority 3: structural fallback (className + index in parent)
    class_name = elem.get("className", "unknown")
    index = elem.get("index", 0)
    return f"{activity}:{class_name}:idx{index}"


def build_identity_index(
    hierarchy: list[dict],
    activity: str,
) -> dict[str, ElementState]:
    """Build an index mapping identity keys to element state.

    The identity key captures WHO the element is (stable across state changes).
    The ElementState captures WHAT its current state is (bounds, text, icon).
    """
    index: dict[str, ElementState] = {}
    for elem in hierarchy:
        identity_key = compute_element_identity_key(elem, activity)
        state = ElementState(
            bounds=parse_bounds(elem.get("bounds")),
            text=(elem.get("text") or "").strip(),
            content_desc=(elem.get("content-desc") or "").strip(),
            checked=elem.get("checked", False),
        )
        index[identity_key] = state
    return index


def compute_identity_diff(
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
            updated.append({
                "identity_key": key,
                "old_state": baseline[key],
                "new_state": current[key],
            })
        else:
            unchanged.append({"identity_key": key})

    return ElementDiff(
        appeared=[{"identity_key": k, "state": current[k]} for k in appeared_keys],
        updated=updated,
        disappeared=[{"identity_key": k, "state": baseline[k]} for k in disappeared_keys],
        unchanged=unchanged,
    )
