#!/usr/bin/env python3
"""View exploration progress for an app.

Usage:
    python scripts/view_exploration.py ./lessons/com.android.settings/
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def print_tree(node: dict, indent: int = 0, prefix: str = "") -> None:
    """Recursively print the feature tree with status icons."""
    status = node.get("status", "pending")
    node_type = node.get("node_type", "screen")
    label = node.get("label", "unknown")

    status_icons = {
        "explored": "[OK]",
        "pending": "[  ]",
        "in_progress": "[>>]",
        "skipped": "[!!]",
        "failed": "[XX]",
        "deep_limit": "[~~]",
    }
    icon = status_icons.get(status, "[??]")

    type_prefix = ""
    if node_type == "modal":
        type_prefix = "(modal) "
    elif node_type == "passive_event":
        type_prefix = "(passive) "
    elif node_type == "dynamic_element":
        type_prefix = "(dynamic) "
    elif node_type == "list_container":
        type_prefix = "(list) "

    skip_info = ""
    if status == "skipped" and node.get("skip_reason"):
        skip_info = f" (skipped: {node['skip_reason']})"
    elif status == "failed":
        attempts = node.get("attempt_count", 0)
        skip_info = f" (failed: {attempts} attempts)"

    line = f"{'  ' * indent}{prefix}{icon} {type_prefix}{label}{skip_info}"
    print(line)

    for child in node.get("children", []):
        print_tree(child, indent + 1)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/view_exploration.py <app_exploration_dir>")
        print("  e.g.: python scripts/view_exploration.py ./lessons/com.android.settings/")
        sys.exit(1)

    app_dir = Path(sys.argv[1])
    exploration_file = app_dir / "_exploration.json"

    if not exploration_file.exists():
        # Try treating the argument as a direct file path
        if app_dir.suffix == ".json" and app_dir.exists():
            exploration_file = app_dir
        else:
            print(f"No exploration state found at {exploration_file}")
            sys.exit(1)

    data = json.loads(exploration_file.read_text())

    app_package = data.get("app_package", "unknown")
    sessions = data.get("sessions", [])

    # Compute stats from the tree
    stats = {"total": 0, "explored": 0, "pending": 0, "failed": 0, "skipped": 0, "deep_limit": 0}

    def count_nodes(node: dict) -> None:
        stats["total"] += 1
        s = node.get("status", "pending")
        if s in stats:
            stats[s] += 1
        for child in node.get("children", []):
            count_nodes(child)

    root = data.get("root", {})
    count_nodes(root)

    # Print header
    print(f"\n=== {app_package} — Exploration Progress ===\n")

    total_minutes = 0
    for session in sessions:
        started = session.get("started", "")
        ended = session.get("ended", "")
        if started and ended:
            # Rough estimate
            total_minutes += session.get("goals_attempted", 0) * 2  # ~2 min per goal

    print(f"Sessions: {len(sessions)} completed")
    print(
        f"Nodes:    {stats['total']} total | "
        f"{stats['explored']} explored | "
        f"{stats['pending']} pending | "
        f"{stats['deep_limit']} deep_limit | "
        f"{stats['failed']} failed | "
        f"{stats['skipped']} skipped"
    )

    total_lessons = sum(s.get("lessons_recorded", 0) for s in sessions)
    if total_lessons:
        print(f"Lessons:  {total_lessons} recorded across sessions")

    print(f"\nFeature Tree:")
    print_tree(root, indent=1)

    print(f"\nLegend: [OK] explored  [  ] pending  [!!] skipped  [XX] failed  [~~] deep_limit")


if __name__ == "__main__":
    main()
