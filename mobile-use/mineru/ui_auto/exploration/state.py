"""Persistence and crash recovery for exploration state.

Handles loading, saving, and recovering exploration state from
_exploration.json files. Uses atomic writes to prevent corruption.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from mineru.ui_auto.exploration.types import ExplorationState, FeatureNode
from mineru.ui_auto.utils.logger import get_logger

logger = get_logger(__name__)


def load_exploration_state(lessons_dir: Path, app_package: str) -> ExplorationState | None:
    """Load persisted exploration state from _exploration.json.

    Returns None if no prior exploration exists for this app.
    Recovers from crashed sessions by resetting in_progress nodes to pending.
    """
    path = lessons_dir / app_package / "_exploration.json"
    if not path.exists():
        return None
    state = ExplorationState(**json.loads(path.read_text()))
    _reset_in_progress_nodes(state.root)
    pruned = _prune_noise_nodes(state.root)
    if pruned:
        logger.info(f"Pruned {pruned} noise nodes from loaded exploration state")
        save_exploration_state(state, lessons_dir, app_package)
    return state


def save_exploration_state(
    state: ExplorationState,
    lessons_dir: Path,
    app_package: str,
) -> None:
    """Persist exploration state to _exploration.json.

    Called after every goal completion to ensure crash recovery loses
    at most one goal's worth of progress. Uses atomic write (write to
    temp file, then rename) to prevent corruption on Ctrl+C.
    """
    dir_path = lessons_dir / app_package
    dir_path.mkdir(parents=True, exist_ok=True)

    target = dir_path / "_exploration.json"
    tmp = dir_path / "_exploration.json.tmp"

    tmp.write_text(state.model_dump_json(indent=2))
    tmp.rename(target)


def _prune_noise_nodes(node: FeatureNode) -> int:
    """Remove system UI noise nodes from the tree on load.

    Applies the current discovery filters retroactively so that noise nodes
    persisted by older code versions are cleaned up automatically on resume,
    without requiring a manual reset.

    Returns the total number of nodes pruned (including descendants).
    """
    from mineru.ui_auto.exploration.discovery import (
        _is_dialog_control,
        _is_non_navigational_text,
        _is_system_ui_noise,
    )

    pruned = 0
    clean_children = []
    for child in node.children:
        if (_is_system_ui_noise(child.label)
                or _is_dialog_control(child.label)
                or _is_non_navigational_text(child.label)):
            # Count this node and all descendants
            def _count(n: FeatureNode) -> int:
                return 1 + sum(_count(c) for c in n.children)
            pruned += _count(child)
        else:
            pruned += _prune_noise_nodes(child)
            clean_children.append(child)
    node.children = clean_children
    return pruned


def _reset_in_progress_nodes(node: FeatureNode) -> None:
    """Reset any nodes stuck in 'in_progress' back to 'pending'.

    This happens when a session crashes mid-goal. The node was marked
    in_progress before the agent started but never updated to explored/failed.
    """
    if node.status == "in_progress":
        node.status = "pending"
    for child in node.children:
        _reset_in_progress_nodes(child)


@dataclass
class TreeStats:
    total: int = 0
    explored: int = 0
    pending: int = 0
    failed: int = 0
    skipped: int = 0
    deep_limit: int = 0


def compute_tree_stats(node: FeatureNode) -> TreeStats:
    """Recursively compute exploration statistics for the feature tree."""
    stats = TreeStats()

    def _walk(n: FeatureNode) -> None:
        stats.total += 1
        if n.status == "explored":
            stats.explored += 1
        elif n.status in ("pending", "in_progress"):
            stats.pending += 1
        elif n.status == "failed":
            stats.failed += 1
        elif n.status == "skipped":
            stats.skipped += 1
        elif n.status == "deep_limit":
            stats.deep_limit += 1
        for child in n.children:
            _walk(child)

    _walk(node)
    return stats


def count_lines(path: Path) -> int:
    """Count lines in a file. Returns 0 if the file does not exist.

    Used to compute lessons_recorded by diffing line counts before/after a session.
    """
    if not path.exists():
        return 0
    return sum(1 for _ in path.open())
