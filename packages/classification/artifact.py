"""Model artifact loading and version management for document classification.

Supports loading versioned model artifacts, verifying artifact metadata,
and instantiating the standardized classifier runtime.
"""

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from packages.classification.base import (
    BaseDocumentClassifier,
    ClassificationError,
    ClassificationInput,
    ClassificationResult,
    DocumentType,
    ModelArtifactError,
)


@dataclass
class ArtifactMetadata:
    """Metadata describing a versioned model artifact."""
    model_name: str
    model_version: str
    model_format: str  # e.g., "sklearn", "onnx", "torch", "custom"
    labels: List[str]
    confidence_threshold: float = 0.80
    feature_extractor_version: Optional[str] = None
    created_at: Optional[str] = None
    description: Optional[str] = None
    extra_config: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ArtifactMetadata":
        required = ["model_name", "model_version", "model_format", "labels"]
        for key in required:
            if key not in data:
                raise ModelArtifactError(f"Missing required artifact metadata key: '{key}'")
        return cls(
            model_name=str(data["model_name"]),
            model_version=str(data["model_version"]),
            model_format=str(data["model_format"]),
            labels=list(data["labels"]),
            confidence_threshold=float(data.get("confidence_threshold", 0.80)),
            feature_extractor_version=data.get("feature_extractor_version"),
            created_at=data.get("created_at"),
            description=data.get("description"),
            extra_config=data.get("extra_config", {}),
        )


class ArtifactBasedClassifier(BaseDocumentClassifier):
    """Wrapper classifier executing inference using a loaded versioned model artifact."""

    def __init__(
        self,
        metadata: ArtifactMetadata,
        predict_fn: Callable[[str], Dict[str, float]],
        artifact_path: Optional[Path] = None,
    ):
        self._metadata = metadata
        self._predict_fn = predict_fn
        self._artifact_path = artifact_path

    @property
    def model_name(self) -> str:
        return self._metadata.model_name

    @property
    def model_version(self) -> str:
        return self._metadata.model_version

    @property
    def confidence_threshold(self) -> float:
        return self._metadata.confidence_threshold

    @property
    def labels(self) -> List[str]:
        return self._metadata.labels

    def predict(self, input_data: ClassificationInput) -> ClassificationResult:
        input_data.validate()
        try:
            probabilities = self._predict_fn(input_data.raw_text)
        except Exception as exc:
            raise ClassificationError(f"Model inference failed: {exc}") from exc

        if not probabilities:
            raise ClassificationError("Model returned empty prediction probabilities")

        # Determine top prediction
        best_label, best_conf = max(probabilities.items(), key=lambda item: item[1])

        # Validate against known labels or fallback to UNKNOWN
        predicted_type = best_label if best_label in DocumentType.values() else DocumentType.UNKNOWN.value

        needs_review = False
        review_reasons: List[str] = []

        if best_conf < self.confidence_threshold:
            needs_review = True
            review_reasons.append(
                f"Confidence {best_conf:.3f} is below threshold {self.confidence_threshold:.3f}"
            )

        if predicted_type == DocumentType.UNKNOWN.value:
            needs_review = True
            review_reasons.append("Model classified document as UNKNOWN")

        # Check for ambiguity (e.g. margin between top 2 classes <= 0.10)
        sorted_probs = sorted(probabilities.values(), reverse=True)
        margin = round(sorted_probs[0] - sorted_probs[1], 4)
        if len(sorted_probs) > 1 and margin <= 0.10:
            needs_review = True
            review_reasons.append(
                f"Ambiguous prediction: top two classes within 10% margin ({sorted_probs[0]:.3f} vs {sorted_probs[1]:.3f})"
            )

        return ClassificationResult(
            predicted_type=predicted_type,
            confidence=round(best_conf, 4),
            probabilities={k: round(v, 4) for k, v in probabilities.items()},
            model_name=self.model_name,
            model_version=self.model_version,
            needs_human_review=needs_review,
            review_reasons=review_reasons,
            inference_metadata={
                "artifact_path": str(self._artifact_path) if self._artifact_path else None,
                "model_format": self._metadata.model_format,
            },
        )


class ModelArtifactLoader:
    """Manages discovery, validation, and loading of versioned classification artifacts."""

    @staticmethod
    def compute_checksum(file_path: Path) -> str:
        """Compute SHA-256 checksum of an artifact file."""
        hasher = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    @classmethod
    def load_from_directory(cls, directory_path: Union[str, Path]) -> BaseDocumentClassifier:
        """Load a classification model from an artifact bundle directory.

        Expected directory structure:
            directory/
                artifact_metadata.json (or config.json)
                model_weights.<ext> (or model.pkl, model.onnx, etc.)
        """
        dir_path = Path(directory_path)
        if not dir_path.exists() or not dir_path.is_dir():
            raise ModelArtifactError(f"Model artifact directory does not exist: {dir_path}")

        meta_file = dir_path / "artifact_metadata.json"
        if not meta_file.exists():
            meta_file = dir_path / "config.json"
        if not meta_file.exists():
            raise ModelArtifactError(
                f"Artifact metadata file ('artifact_metadata.json' or 'config.json') not found in {dir_path}"
            )

        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                meta_dict = json.load(f)
            metadata = ArtifactMetadata.from_dict(meta_dict)
        except Exception as exc:
            raise ModelArtifactError(f"Failed to parse artifact metadata: {exc}") from exc

        # Locate weights / bundle file
        weights_file: Optional[Path] = None
        for candidate in ["model.pkl", "model.joblib", "model.onnx", "model.pt", "weights.bin"]:
            p = dir_path / candidate
            if p.exists():
                weights_file = p
                break

        # Factory for loading standard formats when supplied
        if metadata.model_format in ("sklearn", "joblib") and weights_file:
            import joblib

            loaded_model = joblib.load(weights_file)

            def _predict(text: str) -> Dict[str, float]:
                if hasattr(loaded_model, "predict_proba"):
                    probs = loaded_model.predict_proba([text])[0]
                    classes = list(getattr(loaded_model, "classes_", metadata.labels))
                    return {str(c): float(p) for c, p in zip(classes, probs)}
                else:
                    pred = str(loaded_model.predict([text])[0])
                    return {pred: 1.0}

            return ArtifactBasedClassifier(metadata, _predict, artifact_path=dir_path)

        # For generic callable or custom runtime objects
        raise ModelArtifactError(
            f"Unsupported or missing weights for format '{metadata.model_format}' in {dir_path}"
        )
