import math
from datetime import datetime

from mineru.ui_auto.lessons.types import LessonEntry


def score_lesson(
    lesson: LessonEntry,
    current_activity: str | None,
    current_key_elements: list[str],
    subgoal_description: str,
    now: datetime,
) -> float:
    """Score a lesson for relevance to current context. Higher = more relevant."""
    score = 0.0

    # 1. Screen signature match (strongest signal)
    if lesson.context.screen_signature and current_activity:
        sig = lesson.context.screen_signature
        if sig.activity and sig.activity == current_activity:
            score += 3.0  # Same activity — highly relevant
        if sig.key_elements:
            overlap = len(set(sig.key_elements) & set(current_key_elements))
            score += overlap * 0.5  # Each matching element adds relevance

    # 2. Subgoal text overlap with lesson text (lightweight TF-IDF substitute)
    #    Uses both summary AND context.goal for matching — goal text often contains
    #    richer information (e.g., "Contains: Location sharing settings" from tree lessons).
    subgoal_words = set(subgoal_description.lower().split())
    lesson_text = lesson.summary
    if lesson.context.goal:
        lesson_text = f"{lesson_text} {lesson.context.goal}"
    summary_words = set(lesson_text.lower().split())
    stop_words = {"the", "a", "an", "to", "on", "in", "for", "is", "and", "or", "of", "with"}
    subgoal_words -= stop_words
    summary_words -= stop_words
    if subgoal_words and summary_words:
        overlap_ratio = len(subgoal_words & summary_words) / min(
            len(subgoal_words), len(summary_words)
        )
        score += overlap_ratio * 2.0

    # 3. Confidence (proven lessons rank higher)
    score += lesson.confidence * 1.0

    # 4. Occurrence count (more validated = more trustworthy), log-scaled
    score += math.log1p(lesson.occurrences) * 0.5

    # 5. Recency bonus (seen recently = more likely still valid)
    days_since_seen = (now - lesson.last_seen).days
    if days_since_seen <= 7:
        score += 1.0
    elif days_since_seen <= 30:
        score += 0.5

    # 6. Staleness penalty for ui_mapping type (most fragile to app updates)
    if lesson.type == "ui_mapping" and days_since_seen > 14:
        score -= 1.5

    # 6b. Staleness penalty for mistake/strategy (less fragile but still decay)
    if lesson.type in ("mistake", "strategy") and days_since_seen > 60:
        score -= 1.0

    # 7. ui_mapping lessons always get a base boost (universally useful when on-screen)
    if lesson.type == "ui_mapping":
        score += 1.0

    # 7b. success_path lessons get a conditional boost (only on strong subgoal match)
    if lesson.type == "success_path":
        if subgoal_words and summary_words:
            if overlap_ratio > 0.5:
                score += 2.0  # Strong match — this path is very likely relevant

    # 7c. Staleness: success_paths are moderately fragile (UI can change)
    if lesson.type == "success_path" and days_since_seen > 45:
        score -= 1.5

    return score
