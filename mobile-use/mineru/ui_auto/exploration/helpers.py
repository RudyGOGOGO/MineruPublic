"""Shared utility functions for observer, classifier, and orchestrator modules.

These helpers are referenced across multiple Phase 4 modules to avoid
circular imports and code duplication.
"""

from __future__ import annotations

from mineru.ui_auto.exploration.discovery import parse_bounds
from mineru.ui_auto.exploration.types import FeatureNode


def extract_key_text(elements: list[dict] | list) -> str:
    """Extract the most representative text from a list of elements.

    Used to label passive_event nodes and populate expected_ui_text in triggers.
    Returns the longest non-empty text among the first 5 elements.
    """
    texts = []
    for elem in elements[:5]:
        # Handle both raw element dicts and ElementState objects
        if isinstance(elem, dict):
            text = elem.get("text", "") or elem.get("state", {})
            if isinstance(text, dict):
                text = text.get("text", "")
            elif hasattr(text, "text"):
                text = text.text
        else:
            text = getattr(elem, "text", "")
        if isinstance(text, str):
            text = text.strip()
        else:
            text = ""
        if text:
            texts.append(text)

    if not texts:
        return "unknown"
    return max(texts, key=len)


def quantize_bounds(
    bounds: dict,
    screen_width: int = 1080,
    screen_height: int = 2400,
) -> str:
    """Convert exact pixel bounds to a coarse quadrant label.

    Used for last_observed_state in cross-app triggers. Coarse enough
    that small pixel shifts don't produce different values.

    Returns one of:
    "top-left", "top-center", "top-right",
    "center-left", "center", "center-right",
    "bottom-left", "bottom-center", "bottom-right"
    """
    if isinstance(bounds, str):
        bounds = parse_bounds(bounds)
    center_x = (bounds.get("left", 0) + bounds.get("right", screen_width)) / 2
    center_y = (bounds.get("top", 0) + bounds.get("bottom", screen_height)) / 2

    third_x = screen_width / 3
    third_y = screen_height / 3
    col = "left" if center_x < third_x else ("right" if center_x > 2 * third_x else "center")
    row = "top" if center_y < third_y else ("bottom" if center_y > 2 * third_y else "center")

    if row == "center" and col == "center":
        return "center"
    return f"{row}-{col}"


def update_median(existing_median: int, new_value: int) -> int:
    """Approximate a running median using exponential moving average.

    True running median requires storing all values. This approximation
    is good enough for latency tracking -- we care about magnitude, not precision.
    """
    if existing_median == 0:
        return new_value
    # EMA with alpha=0.3 gives more weight to recent observations
    return int(existing_median * 0.7 + new_value * 0.3)


def compute_coverage(
    elements: list,
    screen_width: int = 1080,
    screen_height: int = 2400,
) -> float:
    """Compute what fraction of the screen area is covered by the union bounding box of elements.

    Used by classify_screen_transition() for modal detection (coverage < 0.80)
    and by classify_passive_event() for screen_change detection (coverage > 0.80).
    """
    if not elements:
        return 0.0

    screen_area = screen_width * screen_height
    if screen_area == 0:
        return 0.0

    # Extract bounds -- handle both raw dicts and ElementState objects
    all_bounds = []
    for e in elements:
        if isinstance(e, dict):
            b = parse_bounds(e.get("bounds"))
        else:
            b = getattr(e, "bounds", {})
            if isinstance(b, str):
                b = parse_bounds(b)
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


def find_child_by_identity(
    parent_node: FeatureNode,
    identity_key: str,
) -> FeatureNode | None:
    """Find an existing child node that matches the given identity key.

    Used for upserting dynamic_element nodes -- if a child with this identity
    already exists, we update it instead of creating a duplicate.
    """
    for child in parent_node.children:
        if child.identity_key == identity_key:
            return child
    return None


def find_current_screen_node(
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
