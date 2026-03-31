from dataclasses import dataclass, field


@dataclass
class UnifiedElement:
    """A UI element detected by UIAutomator, OCR, or both."""
    id: int = 0                                    # Sequential: 1, 2, 3, ... (assigned during merge)
    text: str = ""                                 # Display text or content-desc
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)  # (left, top, right, bottom) in pixels
    center: tuple[int, int] = (0, 0)              # (cx, cy) in pixels
    clickable: bool = False                        # From UIAutomator clickable attr
    source: str = "uiautomator"                    # "uiautomator" | "ocr"
    resource_id: str | None = None                 # Preserved from UIAutomator (for fallback)
    class_name: str | None = None                  # Preserved from UIAutomator
    confidence: float = 1.0                        # 1.0 for UIAutomator, OCR confidence for OCR


@dataclass
class EnhancedScreenData:
    """Output of the enhanced perception pipeline."""
    som_screenshot_b64: str                        # Compressed JPEG with SoM markers
    elements: list[UnifiedElement] = field(default_factory=list)  # Merged, deduped, ID-assigned
    element_list_text: str = ""                    # Formatted: '#1 "Settings" (clickable) center=(200,225)'
    element_count: int = 0
