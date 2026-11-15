import io
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from PIL import Image


class OcrError(Exception):
    """Base exception for OCR pipeline failures."""
    pass


class UnsupportedDocumentFormatError(OcrError):
    """Raised when the document file format cannot be processed by the OCR engine."""
    pass


class CorruptedDocumentError(OcrError):
    """Raised when a document (PDF or image) is malformed or unreadable."""
    pass


@dataclass
class OcrBlock:
    """A recognized text block/line with optional bounding box and confidence score."""
    text: str
    confidence: float = 1.0
    bbox: Optional[List[List[float]]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "confidence": round(self.confidence, 4),
            "bbox": self.bbox,
        }


@dataclass
class OcrPage:
    """The complete OCR result for a single document page."""
    page_number: int  # 1-indexed
    raw_text: str
    blocks: List[OcrBlock] = field(default_factory=list)
    confidence: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page_number": self.page_number,
            "raw_text": self.raw_text,
            "blocks": [b.to_dict() for b in self.blocks],
            "confidence": round(self.confidence, 4),
        }


@dataclass
class OcrResult:
    """Aggregated OCR result across all pages of a document."""
    engine: str
    version: Optional[str]
    total_pages: int
    pages: List[OcrPage]
    full_text: str
    confidence: float
    processed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class BaseOcrEngine(ABC):
    """Abstract interface for pluggable OCR engines."""

    @property
    @abstractmethod
    def engine_name(self) -> str:
        """Name of the OCR engine."""
        raise NotImplementedError

    @property
    @abstractmethod
    def engine_version(self) -> Optional[str]:
        """Version string of the OCR engine."""
        raise NotImplementedError

    @abstractmethod
    def process_image(self, image: Image.Image) -> List[OcrBlock]:
        """Extract text blocks and bounding boxes from a single PIL Image."""
        raise NotImplementedError

    def render_pdf_to_images(self, pdf_bytes: bytes) -> List[Tuple[int, Image.Image]]:
        """
        Convert each page of a PDF into a PIL Image.
        Preserves 1-based page numbering and reading order.
        Never silently skips any page.
        """
        try:
            import pypdfium2 as pdfium
        except ImportError as exc:
            raise OcrError("pypdfium2 is required for PDF rendering") from exc

        try:
            pdf = pdfium.PdfDocument(pdf_bytes)
        except Exception as exc:
            raise CorruptedDocumentError(f"Failed to parse PDF document: {exc}") from exc

        total_pages = len(pdf)
        if total_pages == 0:
            raise CorruptedDocumentError("PDF document contains 0 pages")

        pages: List[Tuple[int, Image.Image]] = []
        for idx in range(total_pages):
            page_number = idx + 1
            try:
                page = pdf[idx]
                # Render at 150-200 DPI equivalent scale (scale=2.0)
                image = page.render(scale=2.0).to_pil()
                pages.append((page_number, image))
            except Exception as exc:
                raise CorruptedDocumentError(
                    f"Failed to render page {page_number} of PDF: {exc}"
                ) from exc

        return pages

    def process_document(self, file_bytes: bytes, file_name: str) -> OcrResult:
        """
        Process any supported document (PDF, PNG, JPG, JPEG).
        Returns aggregated OcrResult with pages and full text.
        """
        ext = ("." + file_name.split(".")[-1]).lower() if "." in file_name else ""
        if ext not in {".pdf", ".png", ".jpg", ".jpeg"}:
            raise UnsupportedDocumentFormatError(f"Unsupported document format: '{ext}'")

        pages_results: List[OcrPage] = []

        if ext == ".pdf":
            page_images = self.render_pdf_to_images(file_bytes)
            for page_num, img in page_images:
                try:
                    blocks = self.process_image(img)
                    page_text = "\n".join(b.text for b in blocks if b.text.strip())
                    page_conf = (
                        sum(b.confidence for b in blocks) / len(blocks)
                        if blocks
                        else 1.0
                    )
                    pages_results.append(
                        OcrPage(
                            page_number=page_num,
                            raw_text=page_text,
                            blocks=blocks,
                            confidence=page_conf,
                        )
                    )
                finally:
                    # Release image memory immediately
                    try:
                        img.close()
                    except Exception:
                        pass
        else:
            # Single image (PNG/JPG/JPEG)
            try:
                img = Image.open(io.BytesIO(file_bytes))
                img.load()
            except Exception as exc:
                raise CorruptedDocumentError(f"Failed to decode image file: {exc}") from exc

            try:
                blocks = self.process_image(img)
                page_text = "\n".join(b.text for b in blocks if b.text.strip())
                page_conf = (
                    sum(b.confidence for b in blocks) / len(blocks) if blocks else 1.0
                )
                pages_results.append(
                    OcrPage(
                        page_number=1,
                        raw_text=page_text,
                        blocks=blocks,
                        confidence=page_conf,
                    )
                )
            finally:
                try:
                    img.close()
                except Exception:
                    pass

        # Compute full text and overall average confidence
        full_text = "\n\n".join(p.raw_text for p in pages_results if p.raw_text.strip())
        all_blocks = [b for p in pages_results for b in p.blocks]
        overall_conf = (
            sum(b.confidence for b in all_blocks) / len(all_blocks)
            if all_blocks
            else 1.0
        )

        return OcrResult(
            engine=self.engine_name,
            version=self.engine_version,
            total_pages=len(pages_results),
            pages=pages_results,
            full_text=full_text,
            confidence=overall_conf,
        )
