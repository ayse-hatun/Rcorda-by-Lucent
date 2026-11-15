from typing import Callable, List, Optional
from PIL import Image

from packages.ocr.base import BaseOcrEngine, OcrBlock


class DeterministicOcrEngine(BaseOcrEngine):
    """
    Deterministic OCR engine for fast, repeatable unit and integration tests.
    Avoids heavy model downloads during every automated test run while preserving
    identical structure (blocks, coordinates, bounding boxes, confidence scores).
    """

    def __init__(
        self,
        extractor: Optional[Callable[[Image.Image], List[OcrBlock]]] = None,
        default_text: str = "RECORD DOCUMENT 100",
        confidence: float = 0.985,
    ):
        self.extractor = extractor
        self.default_text = default_text
        self.confidence = confidence

    @property
    def engine_name(self) -> str:
        return "DeterministicTestEngine"

    @property
    def engine_version(self) -> Optional[str]:
        return "1.0.0"

    def process_image(self, image: Image.Image) -> List[OcrBlock]:
        if self.extractor is not None:
            return self.extractor(image)

        # Check if image is blank (e.g. solid white/black without features)
        w, h = image.size
        # Generate representative bounding boxes for test
        bbox = [
            [10.0, 10.0],
            [float(w - 10), 10.0],
            [float(w - 10), 40.0],
            [10.0, 40.0],
        ]
        return [
            OcrBlock(
                text=self.default_text,
                confidence=self.confidence,
                bbox=bbox,
            )
        ]
