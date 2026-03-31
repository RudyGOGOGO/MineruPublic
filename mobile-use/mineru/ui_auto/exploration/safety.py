"""Tool-level action guard for self-learning exploration mode.

When exploration_mode=True on MobileUseContext, the tap and press_key tools
check targets against EXPLORATION_BLOCKED_ACTIONS and return a system error
that the LLM cannot override. This is the hard enforcement layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mineru.ui_auto.context import MobileUseContext


# Blocklist — checked at tool execution time, not discovery time
EXPLORATION_BLOCKED_ACTIONS = {
    # Destructive
    "delete", "remove", "erase", "format", "reset",
    "factory reset", "clear data", "clear storage",
    "uninstall", "wipe",
    # Transactional
    "send", "submit", "pay", "purchase", "confirm",
    "place order", "subscribe", "donate",
    # Auth-mutating
    "sign out", "log out", "deactivate", "close account",
    # State-changing
    "save", "apply", "update", "enable", "disable",
    "turn on", "turn off",
}


def check_exploration_guard(ctx: MobileUseContext, target_text: str | None) -> str | None:
    """Return an error message if this action is blocked in exploration mode.

    Called by tap/press_key tool wrappers before execution. Returns None if
    the action is allowed, or a system error string if blocked.

    Args:
        ctx: The current mobile use context
        target_text: The text label of the tap target (e.g., "Delete", "Save")

    Returns:
        Error message string if blocked, None if allowed
    """
    if not ctx.exploration_mode or not target_text:
        return None

    normalized = target_text.lower().strip()
    for blocked in EXPLORATION_BLOCKED_ACTIONS:
        if blocked in normalized:
            return (
                f"ACTION_BLOCKED: Cannot tap '{target_text}' in exploration mode. "
                f"Destructive/state-changing actions are disabled during self-learning. "
                f"Navigate to observe this screen but do not interact with '{target_text}'."
            )
    return None
