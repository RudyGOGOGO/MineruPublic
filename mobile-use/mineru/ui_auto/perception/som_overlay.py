from PIL import Image, ImageDraw, ImageFont

from .models import UnifiedElement

# Marker appearance
MARKER_RADIUS = 14
COLOR_CLICKABLE = (0, 180, 0, 200)         # Semi-transparent green
COLOR_NON_CLICKABLE = (140, 140, 140, 200)  # Semi-transparent gray
COLOR_TEXT = (255, 255, 255)                # White
LABEL_MAX_CHARS = 25
LABEL_OFFSET_X = 18                        # Pixels to the right of the circle


def annotate_with_markers(
    screenshot: Image.Image,
    elements: list[UnifiedElement],
) -> Image.Image:
    """
    Draw numbered SoM markers on a copy of the screenshot.
    Returns a new PIL Image with markers drawn.
    """
    img = screenshot.copy().convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 12)
        font_small = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 10)
    except (OSError, IOError):
        font = ImageFont.load_default()
        font_small = font

    for el in elements:
        cx, cy = el.center
        color = COLOR_CLICKABLE if el.clickable else COLOR_NON_CLICKABLE

        # Circle marker
        draw.ellipse(
            (cx - MARKER_RADIUS, cy - MARKER_RADIUS,
             cx + MARKER_RADIUS, cy + MARKER_RADIUS),
            fill=color, outline=(255, 255, 255, 255), width=1,
        )

        # Element ID number (centered in circle)
        id_text = str(el.id)
        bbox = draw.textbbox((0, 0), id_text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((cx - tw // 2, cy - th // 2), id_text, fill=COLOR_TEXT, font=font)

        # Label to the right (with dark background for readability)
        label = el.text[:LABEL_MAX_CHARS]
        if label:
            lx, ly = cx + LABEL_OFFSET_X, cy - 6
            lbbox = draw.textbbox((lx, ly), label, font=font_small)
            draw.rectangle(
                (lbbox[0] - 2, lbbox[1] - 1, lbbox[2] + 2, lbbox[3] + 1),
                fill=(0, 0, 0, 160),
            )
            draw.text((lx, ly), label, fill=(255, 255, 200), font=font_small)

    return Image.alpha_composite(img, overlay).convert("RGB")
