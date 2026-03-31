"""Goal generation and node selection for self-learning exploration.

Provides BFS (breadth-first) and DFS (depth-first) strategies for picking
the next unvisited node, and template-based goal generation for exploration.
"""

from __future__ import annotations

from collections import deque

from mineru.ui_auto.exploration.types import FeatureNode


def pick_next_node(
    root: FeatureNode,
    max_depth: int,
    strategy: str = "breadth_first",
) -> tuple[FeatureNode | None, list[str]]:
    """Pick the next unvisited node to explore.

    Returns (node, parent_path) or (None, []) if all nodes are visited.

    Args:
        root: The feature tree root node
        max_depth: Maximum depth to explore
        strategy: "breadth_first" or "depth_first"
    """
    if strategy == "breadth_first":
        queue: deque[tuple[FeatureNode, list[str]]] = deque([(root, [])])
        while queue:
            node, path = queue.popleft()
            if node.status == "pending" and len(path) < max_depth:
                return node, path
            for child in node.children:
                queue.append((child, path + [node.label]))
    else:
        def _dfs(node: FeatureNode, path: list[str]) -> tuple[FeatureNode | None, list[str]]:
            if node.status == "pending" and len(path) < max_depth:
                return node, path
            for child in node.children:
                found_node, found_path = _dfs(child, path + [node.label])
                if found_node is not None:
                    return found_node, found_path
            return None, []
        return _dfs(root, [])

    return None, []


def generate_exploration_goal(node: FeatureNode, parent_path: list[str]) -> str:
    """Generate a natural language goal for exploring a feature node.

    The goal tells the agent to tap the specific element, navigate to
    the resulting screen, and observe what's there.
    """
    path_description = " > ".join(parent_path + [node.label])

    # Build explicit tap instruction from the nav_action
    tap_instruction = ""
    if node.nav_action:
        tap_instruction = (
            f"First, {node.nav_action} to open '{node.label}'. "
        )

    return (
        f"{tap_instruction}"
        f"Navigate to {path_description}. "
        f"Once you reach the '{node.label}' screen, observe all available options "
        f"and interactive elements. "
        f"Do not change any settings or trigger any destructive actions. "
        f"Identify what actions are available (buttons, menu items, toggles, links)."
    )
