"""Exploration quality metrics: coverage, redundancy, staleness, version drift.

Provides automated quality assessment for exploration runs, including
suggestions for re-exploration when app versions change or branches have
high failure rates.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from mineru.ui_auto.exploration.state import compute_tree_stats
from mineru.ui_auto.exploration.types import ExplorationState, FeatureNode

logger = logging.getLogger(__name__)


@dataclass
class ExplorationMetrics:
    coverage_score: float                       # explored / reachable nodes (0.0-1.0)
    path_redundancy: float                      # fraction of duplicate paths (0.0-1.0)
    staleness_days: int                         # days since last exploration session
    app_version_at_exploration: str | None       # version when exploration ran
    current_app_version: str | None              # currently installed version
    version_changed: bool                        # True if versions differ
    suggested_re_explore: list[str] = field(default_factory=list)  # branches worth revisiting


async def compute_exploration_metrics(
    state: ExplorationState,
    lessons_dir: Path,
    ctx: "MobileUseContext | None" = None,
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
    # Coverage
    stats = compute_tree_stats(state.root)
    reachable = stats.total - stats.skipped - stats.deep_limit
    coverage = stats.explored / reachable if reachable > 0 else 0.0

    # Path Redundancy
    redundancy = _compute_path_redundancy(lessons_dir, state.app_package)

    # Staleness
    staleness_days = _compute_staleness_days(state)

    # App Version
    app_version_at_exploration = state.app_version
    current_version: str | None = None
    if ctx is not None:
        current_version = await get_installed_app_version(ctx, state.app_package)
    version_changed = (
        app_version_at_exploration is not None
        and current_version is not None
        and app_version_at_exploration != current_version
    )

    # Re-explore Suggestions
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


async def get_installed_app_version(
    ctx: "MobileUseContext",
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
            try:
                meta = json.loads(meta_path.read_text())
                return meta.get("app_version")
            except Exception:
                pass

    return None


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

    These are candidates for re-exploration after an app update --
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
