#!/usr/bin/env python3
"""Generate an exploration quality report for an app.

Usage:
    python scripts/exploration_report.py ./lessons/com.android.settings/

Outputs a human-readable report with coverage, redundancy, staleness,
version drift, and re-exploration suggestions.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mineru.ui_auto.exploration.metrics import (
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

    # Compute metrics (without device context -- no current version check)
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
        print(f"    - Exploration is healthy -- no action needed")

    print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/exploration_report.py <app_lessons_dir>")
        print("  e.g.: python scripts/exploration_report.py ./lessons/com.android.settings/")
        sys.exit(1)
    generate_report(Path(sys.argv[1]))
