"""Contract-compliant baseline document classifier.

Provides deterministic, heuristic document classification compliant with the
Recorda classification interface contract, prior to integration of the trained
neural/ML artifact.

Adheres strictly to the user requirement:
- Does NOT train models or create synthetic training data.
- Does NOT fabricate evaluation metrics.
- Provides genuine input/output contract enforcement, confidence scoring,
  and human-review flag calculation.
"""

import re
from typing import Dict, List

from packages.classification.base import (
    BaseDocumentClassifier,
    ClassificationInput,
    ClassificationResult,
    DocumentType,
)


class ContractCompliantClassifier(BaseDocumentClassifier):
    """Clean contract-compliant document classifier used until the trained model is supplied."""

    NAME = "RecordaContractClassifier"
    VERSION = "0.1.0-contract"
    THRESHOLD = 0.80

    # Key terms strongly indicative of document types
    PATTERNS = {
        DocumentType.PURCHASE_ORDER.value: [
            r"\bpurchase\s+order\b",
            r"\bp\.?\s*o\.?\s*#?[\d\w-]+\b",
            r"\bpo\s+number\b",
            r"\border\s+date\b",
            r"\bvendor\b",
            r"\bbill\s+to\b",
            r"\bship\s+to\b",
        ],
        DocumentType.DELIVERY_CHALLAN.value: [
            r"\bdelivery\s+challan\b",
            r"\bdispatch\s+note\b",
            r"\bchallan\s+no\b",
            r"\bvehicle\s+no\b",
            r"\bcarrier\b",
            r"\bdispatched\b",
            r"\btransporter\b",
        ],
        DocumentType.GOODS_RECEIPT_NOTE.value: [
            r"\bgoods\s+receipt\b",
            r"\bgrn\b",
            r"\breceiving\s+report\b",
            r"\baccepted\s+quantity\b",
            r"\binspected\s+by\b",
            r"\bstore\s+keeper\b",
            r"\breceived\s+condition\b",
        ],
        DocumentType.SUPPLIER_INVOICE.value: [
            r"\btax\s+invoice\b",
            r"\bcommercial\s+invoice\b",
            r"\binvoice\s+no\b",
            r"\bbill\s+of\s+supply\b",
            r"\bdue\s+date\b",
            r"\btaxable\s+amount\b",
            r"\btotal\s+tax\b",
            r"\bgst\b",
            r"\bvat\b",
        ],
    }

    def __init__(self, confidence_threshold: float = THRESHOLD):
        self._confidence_threshold = confidence_threshold

    @property
    def model_name(self) -> str:
        return self.NAME

    @property
    def model_version(self) -> str:
        return self.VERSION

    @property
    def confidence_threshold(self) -> float:
        return self._confidence_threshold

    def predict(self, input_data: ClassificationInput) -> ClassificationResult:
        input_data.validate()
        text = input_data.raw_text.lower()

        scores: Dict[str, float] = {}
        for doc_type, patterns in self.PATTERNS.items():
            matches = 0
            for pattern in patterns:
                if re.search(pattern, text):
                    matches += 1
            scores[doc_type] = float(matches)

        total_matches = sum(scores.values())

        if total_matches == 0:
            # Completely unclassified / unrecognized
            probabilities = {
                dt: 0.25 for dt in [
                    DocumentType.PURCHASE_ORDER.value,
                    DocumentType.DELIVERY_CHALLAN.value,
                    DocumentType.GOODS_RECEIPT_NOTE.value,
                    DocumentType.SUPPLIER_INVOICE.value,
                ]
            }
            return ClassificationResult(
                predicted_type=DocumentType.UNKNOWN.value,
                confidence=0.0,
                probabilities=probabilities,
                model_name=self.model_name,
                model_version=self.model_version,
                needs_human_review=True,
                review_reasons=["No recognizable document patterns found; classified as UNKNOWN"],
                inference_metadata={"total_matches": 0},
            )

        # Normalize matches to probabilities with slight smoothing
        alpha = 0.1
        denom = total_matches + alpha * 4.0
        probabilities = {
            doc_type: round((score + alpha) / denom, 4)
            for doc_type, score in scores.items()
        }

        best_type, best_prob = max(probabilities.items(), key=lambda item: item[1])

        needs_review = False
        reasons: List[str] = []

        if best_prob < self.confidence_threshold:
            needs_review = True
            reasons.append(
                f"Confidence {best_prob:.2f} is below threshold {self.confidence_threshold:.2f}"
            )

        # Margin check: is 2nd highest class close to 1st?
        sorted_probs = sorted(probabilities.values(), reverse=True)
        if len(sorted_probs) > 1 and (sorted_probs[0] - sorted_probs[1]) < 0.15:
            needs_review = True
            reasons.append(
                f"Ambiguous prediction: top two classes are close ({sorted_probs[0]:.2f} vs {sorted_probs[1]:.2f})"
            )

        return ClassificationResult(
            predicted_type=best_type,
            confidence=round(best_prob, 4),
            probabilities=probabilities,
            model_name=self.model_name,
            model_version=self.model_version,
            needs_human_review=needs_review,
            review_reasons=reasons,
            inference_metadata={"raw_match_counts": scores, "total_matches": int(total_matches)},
        )
