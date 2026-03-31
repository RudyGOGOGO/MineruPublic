"""Convert explored feature tree paths into success_path lessons.

Bridges the gap between exploration knowledge (_exploration.json) and the
lesson system (lessons.jsonl) so that real tasks benefit from exploration.

For each explored node in the tree, generates a success_path lesson with
the navigation steps from root to that node. Deduplicates against existing
lessons using the same normalization as recorder.py.

Member-specific labels (profile names like "RudyWork", "Grace") are
generalized to role-based placeholders ("{member}", "{family}") so
lessons transfer across users with different profile names.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from mineru.ui_auto.exploration.types import ExplorationState, FeatureNode
from mineru.ui_auto.lessons.recorder import _normalize_goal_for_dedup
from mineru.ui_auto.utils.logger import get_logger

logger = get_logger(__name__)


def generate_tree_lessons(
    state: ExplorationState,
    lessons_dir: Path,
) -> int:
    """Generate success_path lessons from the explored feature tree.

    Walks the tree, collects all explored nodes with depth >= 2 (at least
    tab → feature), and creates a navigation lesson for each path that
    doesn't already exist in lessons.jsonl.

    Member-specific labels are generalized so lessons work for any user.

    Returns the number of new lessons written.
    """
    app_package = state.app_package
    jsonl_path = lessons_dir / app_package / "lessons.jsonl"
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    # Detect member-specific labels from the tree structure.
    # These are dynamic names (profiles, devices) that vary per user.
    member_labels = _detect_member_labels(state.root)

    # Load existing normalized goals for dedup
    existing_goals: set[str] = set()
    if jsonl_path.exists():
        for line in jsonl_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("type") == "success_path" and entry.get("context", {}).get("goal"):
                    normalized = _normalize_goal_for_dedup(entry["context"]["goal"], app_package)
                    existing_goals.add(normalized)
            except (json.JSONDecodeError, Exception):
                continue

    # Collect all explored paths from the tree
    paths = _collect_explored_paths(state.root, [], [])

    # Generate lessons for paths with depth >= 2
    new_lessons = []
    for node, ancestors, nav_actions in paths:
        if len(ancestors) < 2:
            continue  # Skip root and tab-level (too shallow to be useful)

        goal = _build_goal(node, ancestors, app_package, member_labels)
        normalized_goal = _normalize_goal_for_dedup(goal, app_package)

        if normalized_goal in existing_goals:
            continue
        existing_goals.add(normalized_goal)

        steps = _build_path_steps(ancestors, node, member_labels)
        if len(steps) < 2:
            continue

        path_desc = " → ".join(f"tap('{s['target_text'] or '?'}')" for s in steps)
        summary = f"{goal[:60]}...: {path_desc}"

        lesson = {
            "id": f"tree-{hashlib.md5(normalized_goal.encode()).hexdigest()[:4]}",
            "type": "success_path",
            "category": "navigation",
            "summary": summary[:200],
            "context": {
                "goal": goal,
                "screen_signature": {
                    "activity": node.activity,
                    "key_elements": node.key_elements[:5],
                },
                "action_attempted": "",
                "what_happened": "",
            },
            "lesson": f"Proven exploration path: {path_desc}",
            "suggested_strategy": f"Follow this path: {path_desc}",
            "confidence": 0.6,
            "occurrences": 1,
            "applied_success": 0,
            "applied_failure": 0,
            "created": datetime.now(UTC).isoformat(),
            "last_seen": datetime.now(UTC).isoformat(),
            "path": steps,
            "deprecated": False,
            "app_version": state.app_version,
        }
        new_lessons.append(lesson)

    # Append to lessons.jsonl
    if new_lessons:
        with open(jsonl_path, "a") as f:
            for lesson in new_lessons:
                f.write(json.dumps(lesson) + "\n")
        logger.info(
            f"Generated {len(new_lessons)} navigation lessons from "
            f"exploration tree for {app_package}"
        )

    return len(new_lessons)


# ── Member Label Detection & Generalization ────────────────────


# Common structural UI labels that are never member/person names.
# Used to avoid false positives when detecting dynamic user content.
# Only includes UNIVERSAL Android/app patterns — no app-specific features.
STRUCTURAL_LABELS = {
    # Navigation tabs / sections
    "home", "settings", "more", "notifications", "about", "legal",
    "account", "support", "help", "search", "favorites", "history",
    "profile", "messages", "chat", "contacts", "activity",
    # Common sub-features
    "details", "edit", "devices", "location history", "more settings",
    "privacy", "security", "general", "preferences", "feedback",
    "terms of service", "privacy policy", "licenses",
    # Common action labels that aren't person names
    "change", "leave", "join", "invite", "add", "remove", "create",
    "share", "export", "import", "upgrade", "subscribe", "manage",
}

# Multi-word structural labels — checked via substring matching.
# These prevent false-positive member detection for action-oriented labels.
STRUCTURAL_LABEL_PREFIXES = [
    "change ", "leave ", "join ", "invite ", "add ", "remove ",
    "create ", "manage ", "upgrade ", "subscribe ", "share ",
    "schedule ", "customize ", "how ", "learn ",
]


def _detect_member_labels(root: FeatureNode) -> dict[str, str]:
    """Detect member/family-specific labels from the tree and assign placeholders.

    Two-pass approach:
    1. Scan direct children of profile-like tabs for member names
    2. Scan the entire tree for labels matching known member name patterns
       (possessives like "X's Phone", labels reusing detected base names)

    Returns a dict mapping original label → generalized placeholder.
    """
    member_map: dict[str, str] = {}
    base_names: set[str] = set()  # Raw names without suffixes like "(Me)"

    # ── Pass 1: Profile tab children ──────────────────────────────
    for tab in root.children:
        tab_lower = tab.label.lower()
        if not ("profile" in tab_lower or "family" in tab_lower or "member" in tab_lower):
            continue

        member_idx = 0
        for child in tab.children:
            label = child.label
            label_lower = label.lower()

            if label_lower in STRUCTURAL_LABELS:
                continue
            if any(label_lower.startswith(p) for p in STRUCTURAL_LABEL_PREFIXES):
                continue
            if re.match(r"^\(\d+\)$", label) or re.match(r"^\d+$", label):
                continue

            member_idx += 1
            if "(me)" in label_lower or "(admin)" in label_lower:
                member_map[label] = "{my_profile}"
            else:
                member_map[label] = f"{{family_member_{member_idx}}}"

            # Extract the base name (without parenthetical suffix)
            base = re.sub(r"\s*\(.*?\)\s*$", "", label).strip()
            if base and len(base) >= 2:
                base_names.add(base)

            _detect_device_labels(child, member_map, base_names)

            # Scan ALL descendants for member names — profiles can be
            # nested under grouping nodes like "(2)" or family name nodes
            _collect_member_names_recursive(child, member_map, base_names)

    # ── Pass 2: Scan entire tree for labels containing base names ─
    # This catches "RudyHome" appearing under Home > Safe Walk, etc.
    if base_names:
        _scan_for_base_names(root, base_names, member_map)

    # ── Pass 3: Detect saved location names ───────────────────────
    # User-created location names under "Saved Locations" parents
    _detect_saved_location_names(root, member_map)

    if member_map:
        logger.info(
            f"Detected {len(member_map)} member-specific labels: "
            f"{list(member_map.items())[:8]}"
        )

    return member_map


def _collect_member_names_recursive(
    node: FeatureNode,
    member_map: dict[str, str],
    base_names: set[str],
) -> None:
    """Recursively find nodes that look like member profiles by their children.

    A node is a member profile if it has children matching the profile pattern:
    at least 2 of {Edit, Details, Location history, Devices, More settings}.
    The node's label is then a member name.
    """
    profile_child_patterns = {
        "edit", "details", "location history", "devices",
        "more settings", "user profile picture", "driving insights",
        "safety alerts",
    }

    for child in node.children:
        if child.label in member_map:
            _collect_member_names_recursive(child, member_map, base_names)
            continue

        # Check if this child's children match the profile pattern
        child_labels = {gc.label.lower() for gc in child.children}
        profile_matches = child_labels & profile_child_patterns
        if len(profile_matches) >= 2 and child.label.lower() not in STRUCTURAL_LABELS:
            # This node IS a member profile
            label = child.label
            if label not in member_map:
                member_map[label] = "{family_member}"
                base = re.sub(r"\s*\(.*?\)\s*$", "", label).strip()
                if base and len(base) >= 2:
                    base_names.add(base)

        _collect_member_names_recursive(child, member_map, base_names)


def _detect_saved_location_names(
    node: FeatureNode,
    member_map: dict[str, str],
) -> None:
    """Detect user-created saved location/place names.

    Scans for nodes under parents whose label contains "saved location",
    "places", "addresses", or "bookmarks" — common patterns across apps
    for user-created location lists.
    """
    location_parent_patterns = [
        "saved location", "saved place", "my place", "my address",
        "favorite place", "bookmarked location", "boundary alert",
    ]

    for child in node.children:
        child_lower = child.label.lower()

        if any(pat in child_lower for pat in location_parent_patterns):
            for loc_child in child.children:
                loc_label = loc_child.label
                loc_lower = loc_label.lower()
                if (loc_lower not in STRUCTURAL_LABELS
                        and loc_label not in member_map
                        and not any(loc_lower.startswith(p) for p in STRUCTURAL_LABEL_PREFIXES)
                        and not loc_lower.startswith(("add ", "create ", "new ", "upgrade", "saved "))
                        and "verizon" not in loc_lower
                        and not re.match(r"^\(?\d+\)?$", loc_label)
                        # Skip section headers/descriptions (typically > 25 chars)
                        and len(loc_label) <= 25):
                    member_map[loc_label] = "{saved_location}"

        _detect_saved_location_names(child, member_map)


def _scan_for_base_names(
    node: FeatureNode,
    base_names: set[str],
    member_map: dict[str, str],
) -> None:
    """Recursively scan tree for labels matching known member base names."""
    for child in node.children:
        label = child.label
        if label not in member_map and label.lower() not in STRUCTURAL_LABELS:
            for base in base_names:
                if label == base or label.startswith(base + "'"):
                    # Exact match or possessive — this is the same member
                    # Find what placeholder the base name maps to
                    placeholder = None
                    for orig, ph in member_map.items():
                        orig_base = re.sub(r"\s*\(.*?\)\s*$", "", orig).strip()
                        if orig_base == base:
                            placeholder = ph
                            break
                    if placeholder:
                        member_map[label] = placeholder
                    else:
                        member_map[label] = "{family_member}"
                    break
        _scan_for_base_names(child, base_names, member_map)


def _detect_device_labels(
    member_node: FeatureNode,
    member_map: dict[str, str],
    base_names: set[str] | None = None,
) -> None:
    """Detect device-specific labels in a member's subtree."""
    for child in member_node.children:
        label = child.label
        label_lower = label.lower()

        # Device patterns: "RudyHome's Phone", "Grace's iPad"
        if "'s " in label and any(
            dev in label_lower for dev in ["phone", "ipad", "tablet", "device", "watch"]
        ):
            member_map[label] = "{member_device}"
            # Extract the owner name as a base name (e.g., "RudyHome" from "RudyHome's Phone")
            owner = label.split("'s ")[0].strip()
            if owner and len(owner) >= 2 and base_names is not None:
                base_names.add(owner)
            continue

        # Phone numbers
        if re.match(r"^\d{3}[-.]?\d{3}[-.]?\d{4}$", label):
            member_map[label] = "{phone_number}"
            continue

        # Check deeper children too
        _detect_device_labels(child, member_map, base_names)


def _generalize_label(label: str, member_labels: dict[str, str]) -> str:
    """Replace member/location-specific text with generic placeholders."""
    if label in member_labels:
        return member_labels[label]

    # Address patterns: "Near 3278 Ella Way, Suwanee, GA"
    if re.match(r"^Near \d+\s+", label):
        return "{near_address}"

    # Location history with member name: "RudyHome's location history"
    if label.endswith("'s location history"):
        return "{member}'s location history"

    # Dynamic string wrappers: "Family member DynamicString(value=RudyHome)"
    if "DynamicString" in label:
        return "{dynamic_text}"

    # Also check if the label CONTAINS a member name (e.g., "RudyWork's driving insights")
    for original, placeholder in member_labels.items():
        # Strip suffixes like " (Me)", " (Admin)" for substring matching
        base_name = re.sub(r"\s*\(.*?\)\s*$", "", original).strip()
        if base_name and len(base_name) >= 3 and base_name in label:
            return label.replace(base_name, placeholder.strip("{}"))

    return label


# ── Path Collection & Building ─────────────────────────────────


def _collect_explored_paths(
    node: FeatureNode,
    ancestors: list[FeatureNode],
    nav_actions: list[str],
) -> list[tuple[FeatureNode, list[FeatureNode], list[str]]]:
    """Walk the tree and collect explored nodes with their ancestor paths."""
    results = []

    if node.status == "explored" and node.id != "root":
        results.append((node, list(ancestors), list(nav_actions)))

    for child in node.children:
        results.extend(
            _collect_explored_paths(
                child,
                ancestors + [node],
                nav_actions + [child.nav_action or f"tap('{child.label}')"],
            )
        )

    return results


def _build_goal(
    node: FeatureNode,
    ancestors: list[FeatureNode],
    app_package: str,
    member_labels: dict[str, str],
) -> str:
    """Build a natural language goal for navigating to this node.

    Includes child labels so the scorer can match on what's accessible
    FROM this screen (e.g., "Settings" mentions "Location sharing settings"
    so a goal about location sharing will match).

    Member-specific labels are generalized so the goal matches
    regardless of profile name.
    """
    # Build path like "Home > Settings > Location sharing settings"
    labels = [_generalize_label(a.label, member_labels) for a in ancestors[1:]]
    labels.append(_generalize_label(node.label, member_labels))
    path_str = " > ".join(labels)

    # Describe the destination
    if len(labels) >= 3:
        goal = (
            f"Navigate to {path_str} in the app. "
            f"From the home screen, go to {labels[0]}, "
            f"then {labels[1]}, and open {labels[-1]}."
        )
    elif len(labels) == 2:
        goal = f"Navigate to {labels[1]} under {labels[0]} in the app."
    else:
        goal = f"Navigate to {labels[-1]} in the app."

    # Append child labels so scorer can match on accessible features
    child_labels = [
        _generalize_label(c.label, member_labels)
        for c in node.children
        if c.status in ("explored", "pending", "deep_limit")
        and c.label.lower() not in STRUCTURAL_LABELS
    ]
    if child_labels:
        # Cap at 8 to keep lesson compact within token budget
        sample = child_labels[:8]
        goal += f" Contains: {', '.join(sample)}."
        if len(child_labels) > 8:
            goal += f" And {len(child_labels) - 8} more."

    return goal


def _build_path_steps(
    ancestors: list[FeatureNode],
    target: FeatureNode,
    member_labels: dict[str, str],
) -> list[dict]:
    """Build path steps from ancestors to the target node.

    Each step is a tap action on the next node in the path.
    Skips the root node (no action needed to be on home screen).
    Member-specific labels are generalized.
    """
    steps = []

    # Build steps for each ancestor after root (the tabs and intermediate screens)
    all_nodes = list(ancestors[1:]) + [target]  # Skip root
    for node in all_nodes:
        generalized = _generalize_label(node.label, member_labels)
        step = {
            "action": "tap",
            "target_text": generalized,
            "target_resource_id": None,
            "result": f"Navigate to '{generalized}'",
        }
        steps.append(step)

    return steps
