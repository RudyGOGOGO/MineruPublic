from .models import UnifiedElement

PROXIMITY_THRESHOLD = 20  # pixels — elements closer than this are considered duplicates


def merge_and_dedup(
    ua_elements: list[UnifiedElement],
    ocr_elements: list[UnifiedElement],
    proximity_threshold: int = PROXIMITY_THRESHOLD,
) -> list[UnifiedElement]:
    """
    Merge UIAutomator + OCR elements with deduplication.
    UIAutomator elements are always preferred (richer metadata).
    OCR elements only added if no UIAutomator duplicate exists.
    """
    merged = list(ua_elements)  # UIAutomator first (higher priority)

    for ocr_el in ocr_elements:
        is_dup = False
        for existing in merged:
            if _is_duplicate(existing, ocr_el, proximity_threshold):
                is_dup = True
                break
        if not is_dup:
            merged.append(ocr_el)

    # Sort in reading order: top-to-bottom, then left-to-right
    merged.sort(key=lambda el: (el.center[1], el.center[0]))

    # Assign sequential IDs: 1, 2, 3, ...
    for i, el in enumerate(merged, start=1):
        el.id = i

    return merged


def _is_duplicate(a: UnifiedElement, b: UnifiedElement, threshold: int) -> bool:
    """Two elements are duplicates if same text AND centers within threshold."""
    if a.text.lower() != b.text.lower():
        return False
    dx = abs(a.center[0] - b.center[0])
    dy = abs(a.center[1] - b.center[1])
    return dx <= threshold and dy <= threshold
