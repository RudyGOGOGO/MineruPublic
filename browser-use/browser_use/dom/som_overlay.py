import base64
import io

from PIL import Image, ImageDraw, ImageFont

from browser_use.dom.views import UnifiedElement

MARKER_RADIUS = 14
COLOR_INTERACTIVE = (0, 180, 0, 200)  # Semi-transparent green
COLOR_STATIC = (140, 140, 140, 200)  # Semi-transparent gray
COLOR_OCR_ONLY = (0, 120, 200, 200)  # Semi-transparent blue (OCR-only elements)
COLOR_TEXT = (255, 255, 255)  # White
LABEL_BG = (0, 0, 0, 160)  # Dark background for labels
LABEL_MAX_LEN = 25
LABEL_OFFSET_X = 18


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
	"""Load font with macOS/Linux fallback."""
	for path in ['/System/Library/Fonts/Helvetica.ttc', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']:
		try:
			return ImageFont.truetype(path, size)
		except (OSError, IOError):
			continue
	return ImageFont.load_default()


def render_som_overlay(
	screenshot: Image.Image,
	elements: list[UnifiedElement],
	device_pixel_ratio: float = 1.0,
	max_width: int = 1024,
) -> str:
	"""
	Draw SoM markers on screenshot, return compressed PNG base64.

	Elements have centers in CSS pixels. Screenshot is in device pixels.
	Scale centers by device_pixel_ratio to get screenshot pixel coordinates.
	"""
	img = screenshot.copy().convert('RGBA')
	overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
	draw = ImageDraw.Draw(overlay)

	font_id = _load_font(12)
	font_label = _load_font(10)

	for el in elements:
		# Convert CSS pixels → screenshot pixels
		cx = int(el.center[0] * device_pixel_ratio)
		cy = int(el.center[1] * device_pixel_ratio)

		# Choose color
		if el.source == 'ocr':
			color = COLOR_OCR_ONLY
		elif el.is_interactive:
			color = COLOR_INTERACTIVE
		else:
			color = COLOR_STATIC

		# Draw circle marker
		r = MARKER_RADIUS
		draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)

		# Draw ID number centered in circle
		id_text = str(el.id)
		bbox = draw.textbbox((0, 0), id_text, font=font_id)
		tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
		draw.text((cx - tw // 2, cy - th // 2), id_text, fill=COLOR_TEXT, font=font_id)

		# Draw truncated text label
		label = el.text[:LABEL_MAX_LEN]
		if len(el.text) > LABEL_MAX_LEN:
			label += '...'
		label_x = cx + LABEL_OFFSET_X
		label_y = cy - 6

		lbox = draw.textbbox((0, 0), label, font=font_label)
		lw, lh = lbox[2] - lbox[0], lbox[3] - lbox[1]
		draw.rectangle([label_x - 2, label_y - 1, label_x + lw + 2, label_y + lh + 1], fill=LABEL_BG)
		draw.text((label_x, label_y), label, fill=COLOR_TEXT, font=font_label)

	# Composite and convert
	result = Image.alpha_composite(img, overlay).convert('RGB')

	# Resize if needed
	if result.width > max_width:
		ratio = max_width / result.width
		new_size = (max_width, int(result.height * ratio))
		result = result.resize(new_size, Image.LANCZOS)

	# Encode to PNG base64 (PNG is lossless — better for numbered markers,
	# and matches the image/png MIME type used in get_user_message())
	buf = io.BytesIO()
	result.save(buf, format='PNG')
	return base64.b64encode(buf.getvalue()).decode('utf-8')
