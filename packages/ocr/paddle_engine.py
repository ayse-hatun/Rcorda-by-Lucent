import numpy as np
from typing import List, Optional
from PIL import Image

from packages.ocr.base import BaseOcrEngine, OcrBlock, OcrError


class PaddleOcrEngine(BaseOcrEngine):
    """
    PaddleOCR implementation for multi-page document text recognition.
    Loads the underlying neural network weights once per process.
    """

    _instance: Optional["PaddleOcrEngine"] = None
    _paddle_ocr = None

    def __init__(self, lang: str = "en", use_angle_cls: bool = False):
        self.lang = lang
        self.use_angle_cls = use_angle_cls
        self._version = "3.7.0"

    @classmethod
    def get_instance(cls, lang: str = "en") -> "PaddleOcrEngine":
        """Singleton accessor ensuring weights are loaded once per process."""
        if cls._instance is None:
            cls._instance = cls(lang=lang)
        return cls._instance

    @property
    def engine_name(self) -> str:
        return "PaddleOCR"

    @property
    def engine_version(self) -> Optional[str]:
        return self._version

    def _get_ocr(self):
        if self._paddle_ocr is None:
            try:
                import paddleocr
                from paddleocr import PaddleOCR

                self._version = getattr(paddleocr, "__version__", "3.7.0")
                # PaddleOCR initialization
                self._paddle_ocr = PaddleOCR(
                    use_angle_cls=self.use_angle_cls,
                    lang=self.lang,
                )
            except Exception as exc:
                raise OcrError(f"Failed to initialize PaddleOCR engine: {exc}") from exc
        return self._paddle_ocr

    def process_image(self, image: Image.Image) -> List[OcrBlock]:
        """Convert PIL Image to RGB numpy array and perform inference."""
        ocr = self._get_ocr()

        # Ensure image is in RGB mode
        if image.mode != "RGB":
            rgb_img = image.convert("RGB")
        else:
            rgb_img = image

        np_img = np.array(rgb_img)

        try:
            raw_results = ocr.ocr(np_img, cls=self.use_angle_cls)
        except Exception as exc:
            raise OcrError(f"PaddleOCR inference failed: {exc}") from exc

        blocks: List[OcrBlock] = []
        if not raw_results:
            return blocks

        # PaddleOCR returns a list of results (one per image input)
        first_result = raw_results[0] if isinstance(raw_results, list) else raw_results
        if not first_result:
            return blocks

        for item in first_result:
            if not item:
                continue
            # format: [ [ [x1, y1], [x2, y2], [x3, y3], [x4, y4] ], (text, confidence) ]
            try:
                bbox_points, (text, confidence) = item
                formatted_bbox = [
                    [float(pt[0]), float(pt[1])] for pt in bbox_points
                ]
                blocks.append(
                    OcrBlock(
                        text=str(text).strip(),
                        confidence=float(confidence),
                        bbox=formatted_bbox,
                    )
                )
            except Exception:
                # Fallback for unexpected formats
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    text = str(item[1])
                    blocks.append(OcrBlock(text=text.strip(), confidence=1.0))

        return blocks
