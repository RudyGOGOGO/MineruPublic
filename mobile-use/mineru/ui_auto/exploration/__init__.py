"""App self-learning exploration module.

Provides automated goal generation and exploration loops that systematically
discover an app's feature surface using the existing agent graph.
"""

from mineru.ui_auto.exploration.cross_app_lessons import (
    generate_cross_app_lessons,
    write_cross_app_lessons,
)
from mineru.ui_auto.exploration.discovery import discover_features, parse_bounds
from mineru.ui_auto.exploration.goal_generator import generate_smart_goal
from mineru.ui_auto.exploration.helpers import (
    compute_coverage,
    extract_key_text,
    find_child_by_identity,
    find_current_screen_node,
    quantize_bounds,
    update_median,
)
from mineru.ui_auto.exploration.identity import (
    build_identity_index,
    compute_element_identity_key,
    compute_identity_diff,
)
from mineru.ui_auto.exploration.metrics import (
    ExplorationMetrics,
    compute_exploration_metrics,
    get_installed_app_version,
)
from mineru.ui_auto.exploration.observer import observe_for_cross_app_event
from mineru.ui_auto.exploration.orchestrator import (
    MasterOrchestrator,
    record_cross_app_trigger,
)
from mineru.ui_auto.exploration.passive_classifier import classify_passive_event
from mineru.ui_auto.exploration.planner import generate_exploration_goal, pick_next_node
from mineru.ui_auto.exploration.runner import (
    initialize_exploration,
    navigate_to_app_home,
    run_exploration_session,
    run_exploration_task,
)
from mineru.ui_auto.exploration.safety import check_exploration_guard
from mineru.ui_auto.exploration.screen_classifier import (
    classify_screen_transition,
    extract_new_elements,
)
from mineru.ui_auto.exploration.state import (
    compute_tree_stats,
    load_exploration_state,
    save_exploration_state,
)
from mineru.ui_auto.exploration.types import (
    CrossAppTrigger,
    ElementDiff,
    ElementState,
    ExplorationState,
    ExplorationTaskResult,
    FeatureNode,
    ObserverResult,
    SessionSummary,
    capture_screen_signature,
)

__all__ = [
    # Types
    "capture_screen_signature",
    "CrossAppTrigger",
    "ElementDiff",
    "ElementState",
    "ExplorationMetrics",
    "ExplorationState",
    "ExplorationTaskResult",
    "FeatureNode",
    "ObserverResult",
    "SessionSummary",
    # Discovery & Planning
    "discover_features",
    "generate_exploration_goal",
    "generate_smart_goal",
    "pick_next_node",
    # Runner & Session
    "initialize_exploration",
    "navigate_to_app_home",
    "run_exploration_session",
    "run_exploration_task",
    # State
    "compute_tree_stats",
    "load_exploration_state",
    "save_exploration_state",
    # Safety
    "check_exploration_guard",
    # Screen Classification
    "classify_screen_transition",
    "extract_new_elements",
    # Identity & Diffing
    "build_identity_index",
    "compute_element_identity_key",
    "compute_identity_diff",
    # Observer & Passive Events
    "classify_passive_event",
    "observe_for_cross_app_event",
    # Helpers
    "compute_coverage",
    "extract_key_text",
    "find_child_by_identity",
    "find_current_screen_node",
    "quantize_bounds",
    "update_median",
    # Orchestrator
    "MasterOrchestrator",
    "record_cross_app_trigger",
    # Metrics
    "compute_exploration_metrics",
    "get_installed_app_version",
    # Cross-App Lessons
    "generate_cross_app_lessons",
    "write_cross_app_lessons",
]
