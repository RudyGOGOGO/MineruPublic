"""Observer Mode for cross-app event detection in cluster mode.

Polls the secondary app's UI for state changes triggered by the primary app,
using identity-based diffing to prevent false positives from dynamic elements.
"""

from __future__ import annotations

import asyncio
import logging
import time

from mineru.ui_auto.context import MobileUseContext
from mineru.ui_auto.controllers.controller_factory import create_device_controller
from mineru.ui_auto.exploration.identity import (
    build_identity_index,
    compute_identity_diff,
)
from mineru.ui_auto.exploration.passive_classifier import classify_passive_event
from mineru.ui_auto.exploration.types import ObserverResult

logger = logging.getLogger(__name__)

# Minimum time to observe after first detecting a change.
# This prevents the observer from returning after catching only a timestamp
# update while missing the real event (e.g., avatar movement) that arrives 2s later.
SETTLE_WINDOW_SECONDS = 2.0


async def observe_for_cross_app_event(
    ctx: MobileUseContext,
    app_package: str,
    baseline_hierarchy: list[dict],
    current_activity: str,
    timeout_seconds: float = 10.0,
    poll_interval_seconds: float = 0.5,
) -> ObserverResult:
    """Poll the secondary app's UI for state changes triggered by the primary app.

    Uses two-tiered diffing: first checks element identity (who changed),
    then element state (what changed). This prevents dynamic elements like
    map avatars from creating duplicate events on every position update.

    After detecting the first change, continues polling for SETTLE_WINDOW_SECONDS
    to aggregate related changes (e.g., avatar move + status text change + timestamp
    update that arrive within a 2-second burst). Returns the final aggregated diff.

    Args:
        ctx: Device context for the secondary app's device
        app_package: Secondary app package to observe
        baseline_hierarchy: UI hierarchy snapshot taken before the primary action
        current_activity: Current activity name for identity key computation
        timeout_seconds: How long to wait for the FIRST change before giving up
        poll_interval_seconds: How frequently to poll the UI hierarchy

    Returns:
        ObserverResult with detected changes, timing, and classification
    """
    controller = create_device_controller(ctx)
    start_time = time.monotonic()

    baseline_index = build_identity_index(baseline_hierarchy, current_activity)

    first_change_time: float | None = None
    latest_diff = None
    latest_hierarchy: list[dict] | None = None

    while True:
        elapsed = time.monotonic() - start_time

        # Timeout: no change detected within the window
        if first_change_time is None and elapsed >= timeout_seconds:
            break

        # Settle window: enough time has passed since first change
        if first_change_time is not None:
            since_first = time.monotonic() - first_change_time
            if since_first >= SETTLE_WINDOW_SECONDS:
                break

        await asyncio.sleep(poll_interval_seconds)

        try:
            screen_data = await controller.get_screen_data()
            if screen_data is None or not screen_data.elements:
                continue  # Device temporarily unavailable -- skip this poll cycle
            current_hierarchy = screen_data.elements
        except Exception:
            logger.warning("get_screen_data() failed during Observer Mode poll, skipping cycle")
            continue

        current_index = build_identity_index(current_hierarchy, current_activity)
        diff = compute_identity_diff(baseline_index, current_index)

        if diff.has_changes:
            if first_change_time is None:
                first_change_time = time.monotonic()
            # Always keep the latest diff -- it accumulates all changes since baseline
            latest_diff = diff
            latest_hierarchy = current_hierarchy

    if latest_diff is not None and first_change_time is not None:
        elapsed_ms = int((first_change_time - start_time) * 1000)
        event_type = classify_passive_event(latest_diff, latest_hierarchy or [])

        return ObserverResult(
            detected=True,
            event_type=event_type,
            new_elements=latest_diff.appeared,
            updated_elements=latest_diff.updated,
            disappeared_elements=latest_diff.disappeared,
            latency_ms=elapsed_ms,
            snapshot=latest_hierarchy,
        )

    return ObserverResult()
