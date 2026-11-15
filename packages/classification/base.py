"""Document classification contract and base classes.

Defines the expected input, output, and execution contracts for document type
classification models in Recorda.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
import uuid


class DocumentType(str, Enum):
    """Supported document types in Recorda."""
    PURCHASE_ORDER = "PURCHASE_ORDER"
    DELIVERY_CHALLAN = "DELIVERY_CHALLAN"
    GOODS_RECEIPT_NOTE = "GOODS_RECEIPT_NOTE"
    SUPPLIER_INVOICE = "SUPPLIER_INVOICE"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def values(cls) -> List[str]:
        return [item.value for item in cls]


class ClassificationError(Exception):
    """Base exception for classification subsystem errors."""
    pass


class ModelArtifactError(ClassificationError):
    """Raised when a model artifact cannot be found, loaded, or validated."""
    pass


class InvalidClassificationInputError(ClassificationError):
    """Raised when input provided for classification does not meet contract requirements."""
    pass


@dataclass
class ClassificationInput:
    """Standardized input contract for document classification models."""
    raw_text: str
    document_id: Optional[uuid.UUID] = None
    file_path: Optional[str] = None
    mime_type: Optional[str] = None
    page_count: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.raw_text or not self.raw_text.strip():
            raise InvalidClassificationInputError("raw_text cannot be empty for classification")


@dataclass
class ClassificationResult:
    """Standardized output contract from document classification models."""
    predicted_type: str
    confidence: float
    model_name: str
    model_version: str
    probabilities: Dict[str, float] = field(default_factory=dict)
    needs_human_review: bool = False
    review_reasons: List[str] = field(default_factory=list)
    inference_metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "predicted_type": self.predicted_type,
            "confidence": self.confidence,
            "probabilities": self.probabilities,
            "needs_human_review": self.needs_human_review,
            "review_reasons": self.review_reasons,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "inference_metadata": self.inference_metadata,
            "timestamp": self.timestamp.isoformat(),
        }


class BaseDocumentClassifier(ABC):
    """Abstract interface that all classification models (trained or heuristic) must implement."""

    DEFAULT_CONFIDENCE_THRESHOLD: float = 0.80

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Unique identifier or name of the classification model."""
        pass

    @property
    @abstractmethod
    def model_version(self) -> str:
        """Version string of the loaded model or artifact."""
        pass

    @property
    def confidence_threshold(self) -> float:
        """Threshold below which a classification must be flagged for human review."""
        return self.DEFAULT_CONFIDENCE_THRESHOLD

    @abstractmethod
    def predict(self, input_data: ClassificationInput) -> ClassificationResult:
        """Run classification on a single document input."""
        pass

    def predict_batch(self, inputs: List[ClassificationInput]) -> List[ClassificationResult]:
        """Run classification on a batch of inputs."""
        return [self.predict(item) for item in inputs]
