"""Base classes and data structures for document-specific extraction."""

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional


@dataclass
class FieldProvenance:
    """Provenance and evidence audit metadata for an individual extracted field."""
    field_name: str
    extracted_value: Any
    raw_source_text: Optional[str] = None
    page_number: Optional[int] = None
    confidence: Optional[float] = None
    extraction_method: str = "PATTERN_MATCH"  # PATTERN_MATCH, KEYWORD_ANCHOR, TABLE_PARSE, DEFAULT_FALLBACK
    bbox: Optional[List[List[float]]] = None

    def to_dict(self) -> Dict[str, Any]:
        val = self.extracted_value
        if isinstance(val, Decimal):
            val = str(val)
        return {
            "field_name": self.field_name,
            "extracted_value": val,
            "raw_source_text": self.raw_source_text,
            "page_number": self.page_number,
            "confidence": round(self.confidence, 4) if self.confidence is not None else None,
            "extraction_method": self.extraction_method,
            "bbox": self.bbox,
        }


@dataclass
class ExtractionWarning:
    """Validation or extraction quality warning."""
    field_name: str
    warning_code: str
    message: str
    raw_value: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ExtractionOutput:
    """Standardized output produced by document-specific extractors."""
    document_type: str
    extractor_version: str
    fields: Dict[str, Any] = field(default_factory=dict)
    line_items: List[Dict[str, Any]] = field(default_factory=list)
    provenance: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    warnings: List[Dict[str, Any]] = field(default_factory=list)
    needs_human_review: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "document_type": self.document_type,
            "extractor_version": self.extractor_version,
            "fields": self.fields,
            "line_items": self.line_items,
            "provenance": self.provenance,
            "warnings": self.warnings,
            "needs_human_review": self.needs_human_review,
        }


class BaseExtractor(ABC):
    """Abstract base class that all document-specific extractors implement."""

    VERSION = "1.0.0"

    @property
    @abstractmethod
    def document_type(self) -> str:
        """The document type handled by this extractor."""
        pass

    @property
    def extractor_version(self) -> str:
        return self.VERSION

    @abstractmethod
    def extract(self, pages: List[Dict[str, Any]]) -> ExtractionOutput:
        """Execute extraction on the OCR pages of a document."""
        pass
