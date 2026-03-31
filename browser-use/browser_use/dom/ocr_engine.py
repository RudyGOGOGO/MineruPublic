from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
	from paddleocr import PaddleOCR

import numpy as np
from PIL import Image
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class OCRDetection(BaseModel):
	"""A single text region detected by OCR."""

	text: str
	bounds: tuple[int, int, int, int]  # (left, top, right, bottom) in CSS pixels
	center: tuple[int, int]  # (cx, cy) in CSS pixels
	confidence: float
	source: Literal['ocr'] = 'ocr'


class OCREngine:
	"""Lazy-loaded PaddleOCR singleton. ~2s init, amortized across agent lifetime."""

	_instance: 'OCREngine | None' = None
	_engine: 'PaddleOCR | None' = None
	_is_v3: bool = False

	@classmethod
	def get(cls) -> 'OCREngine':
		if cls._instance is None:
			cls._instance = cls()
		return cls._instance

	def _ensure_engine(self) -> 'PaddleOCR':
		if self._engine is None:
			# Suppress PaddleX connectivity check before importing paddleocr
			import os

			os.environ.setdefault('PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK', 'True')
			try:
				from paddleocr import PaddleOCR
			except ImportError:
				raise ImportError(
					'paddleocr is required for enhanced perception mode. Install it with: pip install browser-use[ocr]'
				)

			import logging as _logging
			import warnings

			for _name in ('ppocr', 'paddlex', 'paddle', 'paddleocr'):
				_logging.getLogger(_name).setLevel(_logging.WARNING)
			warnings.filterwarnings('ignore', message='.*ccache.*')
			warnings.filterwarnings('ignore', message='.*recompiling.*')
			try:
				# PaddleOCR v2.x API
				self._engine = PaddleOCR(use_angle_cls=True, lang='en', show_log=False, use_gpu=True)
				self._is_v3 = False
			except (TypeError, ValueError):
				# PaddleOCR v3.x removed show_log, use_gpu, and renamed use_angle_cls
				self._engine = PaddleOCR(use_textline_orientation=True, lang='en')
				self._is_v3 = True
		return self._engine

	def _parse_v2_results(self, results: list, scale: float, confidence_threshold: float) -> list[OCRDetection]:
		"""Parse PaddleOCR v2 format: [[polygon, (text, confidence)], ...]"""
		if not results or not results[0]:
			return []

		detections: list[OCRDetection] = []
		for line in results[0]:
			polygon, (text, confidence) = line
			if confidence < confidence_threshold:
				continue
			text = text.strip()
			if not text:
				continue

			xs = [p[0] for p in polygon]
			ys = [p[1] for p in polygon]
			left = int(min(xs) * scale)
			top = int(min(ys) * scale)
			right = int(max(xs) * scale)
			bottom = int(max(ys) * scale)
			cx = (left + right) // 2
			cy = (top + bottom) // 2

			detections.append(
				OCRDetection(
					text=text,
					bounds=(left, top, right, bottom),
					center=(cx, cy),
					confidence=confidence,
				)
			)
		return detections

	def _parse_v3_results(self, results: list, scale: float, confidence_threshold: float) -> list[OCRDetection]:
		"""Parse PaddleOCR v3 format: [{'rec_texts': [...], 'rec_scores': [...], 'rec_polys': [...]}]"""
		if not results:
			return []

		detections: list[OCRDetection] = []
		for page_result in results:
			texts = page_result.get('rec_texts', [])
			scores = page_result.get('rec_scores', [])
			polys = page_result.get('rec_polys', [])

			for text, confidence, polygon in zip(texts, scores, polys):
				if confidence < confidence_threshold:
					continue
				text = text.strip()
				if not text:
					continue

				# polygon is a numpy array of shape (4, 2) with [x, y] corners
				xs = polygon[:, 0]
				ys = polygon[:, 1]
				left = int(float(min(xs)) * scale)
				top = int(float(min(ys)) * scale)
				right = int(float(max(xs)) * scale)
				bottom = int(float(max(ys)) * scale)
				cx = (left + right) // 2
				cy = (top + bottom) // 2

				detections.append(
					OCRDetection(
						text=text,
						bounds=(left, top, right, bottom),
						center=(cx, cy),
						confidence=confidence,
					)
				)
		return detections

	def detect(
		self,
		screenshot: Image.Image,
		device_pixel_ratio: float = 1.0,
		confidence_threshold: float = 0.5,
	) -> list[OCRDetection]:
		"""
		Run OCR on screenshot, return detections in CSS pixel coordinates.

		PaddleOCR returns polygon corners in screenshot pixels.
		We convert to CSS pixels by dividing by device_pixel_ratio.
		"""
		engine = self._ensure_engine()
		img_array = np.array(screenshot)
		scale = 1.0 / device_pixel_ratio

		try:
			if self._is_v3:
				results = engine.ocr(img_array)
			else:
				results = engine.ocr(img_array, cls=True)
		except Exception as e:
			logger.warning(f'OCR failed (non-fatal): {e}')
			return []

		try:
			if self._is_v3:
				return self._parse_v3_results(results, scale, confidence_threshold)
			else:
				return self._parse_v2_results(results, scale, confidence_threshold)
		except Exception as e:
			logger.warning(f'OCR result parsing failed (non-fatal): {e}')
			return []
