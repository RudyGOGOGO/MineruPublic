import numpy as np
from PIL import Image

from .models import UnifiedElement

# Lazy singleton — PaddleOCR is slow to initialize
_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        from paddleocr import PaddleOCR
        _engine = PaddleOCR(
            use_angle_cls=True,
            lang="en",
            show_log=False,
            use_gpu=True,  # Falls back to CPU if no GPU available
        )
    return _engine


def detect_text_regions(image: Image.Image, confidence_threshold: float = 0.5) -> list[UnifiedElement]:
    """
    Run PaddleOCR on a PIL Image, return list of UnifiedElements.
    Gracefully returns [] on any failure (OCR is non-fatal).
    """
    try:
        engine = _get_engine()
        img_array = np.array(image)
        results = engine.ocr(img_array, cls=True)

        elements = []
        for line in results[0] or []:
            polygon, (text, confidence) = line

            if confidence < confidence_threshold:
                continue
            if not text.strip():
                continue

            xs = [p[0] for p in polygon]
            ys = [p[1] for p in polygon]
            left, top = int(min(xs)), int(min(ys))
            right, bottom = int(max(xs)), int(max(ys))
            cx = (left + right) // 2
            cy = (top + bottom) // 2

            elements.append(UnifiedElement(
                id=0,  # Assigned later during merge
                text=text.strip(),
                bounds=(left, top, right, bottom),
                center=(cx, cy),
                clickable=False,  # OCR can't determine clickability
                source="ocr",
                resource_id=None,
                class_name=None,
                confidence=confidence,
            ))
        return elements
    except Exception:
        return []  # Graceful degradation
