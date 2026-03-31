import base64
import io
import re

from PIL import Image

from .models import UnifiedElement, EnhancedScreenData
from .ocr_engine import detect_text_regions
from .element_merge import merge_and_dedup
from .som_overlay import annotate_with_markers


def enhance_screen_data(
    screenshot_b64: str,
    ui_elements: list[dict],
    width: int,
    height: int,
) -> EnhancedScreenData:
    """
    Enhanced perception pipeline. Takes existing UIAutomator data + screenshot,
    adds OCR detection, merges both sources, draws SoM overlay.

    Called ONLY when perception_mode == "enhanced".
    """
    # Step 1: Decode screenshot
    screenshot_image = _b64_to_pil(screenshot_b64)

    # Step 2: Convert UIAutomator element dicts to UnifiedElement
    ua_elements = _convert_uiautomator_elements(ui_elements)

    # Step 3: Run PaddleOCR on screenshot
    ocr_elements = detect_text_regions(screenshot_image)

    # Step 4: Merge & dedup (UIAutomator preferred on overlap)
    unified = merge_and_dedup(ua_elements, ocr_elements)

    # Step 5: Draw SoM overlay on screenshot copy
    som_image = annotate_with_markers(screenshot_image, unified)

    # Step 6: Compress SoM image to JPEG base64
    som_b64 = _compress_to_jpeg_b64(som_image, quality=50)

    # Step 7: Format element list text
    element_list_text = _format_element_list(unified)

    return EnhancedScreenData(
        som_screenshot_b64=som_b64,
        elements=unified,
        element_list_text=element_list_text,
        element_count=len(unified),
    )


def _b64_to_pil(b64_string: str) -> Image.Image:
    """Decode base64 string to PIL Image."""
    image_data = base64.b64decode(b64_string)
    return Image.open(io.BytesIO(image_data))


def _compress_to_jpeg_b64(image: Image.Image, quality: int = 50, max_width: int = 540) -> str:
    """Compress PIL Image to JPEG base64 string, resizing if wider than max_width."""
    if image.width > max_width:
        ratio = max_width / image.width
        new_height = int(image.height * ratio)
        image = image.resize((max_width, new_height), Image.LANCZOS)

    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _convert_uiautomator_elements(ui_elements: list[dict]) -> list[UnifiedElement]:
    """Convert ui-auto UIAutomator element dicts to UnifiedElement list."""
    elements = []
    for el in ui_elements:
        text = el.get("text", "") or el.get("content-desc", "") or el.get("accessibilityText", "") or ""
        if not text.strip():
            continue

        bounds = _parse_bounds(el.get("bounds", ""))
        if bounds is None:
            continue

        left, top, right, bottom = bounds
        cx = (left + right) // 2
        cy = (top + bottom) // 2

        clickable = el.get("clickable", "false") == "true"
        resource_id = el.get("resource-id") or None
        class_name = el.get("class") or None

        elements.append(UnifiedElement(
            id=0,  # Assigned later during merge
            text=text.strip(),
            bounds=(left, top, right, bottom),
            center=(cx, cy),
            clickable=clickable,
            source="uiautomator",
            resource_id=resource_id,
            class_name=class_name,
            confidence=1.0,
        ))
    return elements


def _parse_bounds(bounds_str: str) -> tuple[int, int, int, int] | None:
    """Parse UIAutomator bounds string '[x1,y1][x2,y2]' to (left, top, right, bottom)."""
    if not bounds_str:
        return None
    match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)),
            int(match.group(3)), int(match.group(4)))


def _format_element_list(elements: list[UnifiedElement]) -> str:
    """Format elements as numbered text list for the LLM."""
    lines = []
    for el in elements:
        clickable_str = "clickable" if el.clickable else "non-clickable"
        source_str = f" [ocr]" if el.source == "ocr" else ""
        lines.append(
            f'#{el.id} "{el.text}" ({clickable_str}) '
            f'center=({el.center[0]},{el.center[1]}){source_str}'
        )
    return "\n".join(lines)
