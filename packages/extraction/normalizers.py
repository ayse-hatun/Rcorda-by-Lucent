"""Normalization and parsing utilities for OCR text, dates, numbers, and currencies."""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Optional, Tuple


def normalize_whitespace(text: Optional[str]) -> str:
    """Clean up OCR whitespace noise, non-breaking spaces, and tabs."""
    if not text:
        return ""
    # Replace non-breaking spaces and line separators
    cleaned = text.replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")
    # Replace sequences of spaces/tabs with a single space
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in cleaned.split("\n")]
    # Strip empty lines from outer edges while preserving paragraph structure
    return "\n".join(lines).strip()


def parse_decimal_value(raw: Optional[str]) -> Tuple[Optional[Decimal], Optional[str]]:
    """Parse a numeric string into a Decimal with exact precision.

    Returns:
        (decimal_value, error_message)
    """
    if not raw:
        return None, None

    # Strip currency signs, whitespace, and clean formatting
    s = raw.strip()
    s = re.sub(r"^[^\d\-+]+", "", s)  # Remove leading currency/symbols
    s = re.sub(r"[^\d.]+$", "", s)   # Remove trailing characters
    s = s.replace(",", "").strip()

    if not s:
        return None, None

    try:
        val = Decimal(s)
        return val, None
    except InvalidOperation:
        return None, f"Could not parse numeric decimal from '{raw}'"


# Supported date formats
DATE_PATTERNS = [
    ("%Y-%m-%d", r"\b\d{4}-\d{2}-\d{2}\b"),
    ("%Y/%m/%d", r"\b\d{4}/\d{2}/\d{2}\b"),
    ("%d-%m-%Y", r"\b\d{2}-\d{2}-\d{4}\b"),
    ("%d/%m/%Y", r"\b\d{2}/\d{2}/\d{4}\b"),
    ("%m/%d/%Y", r"\b\d{2}/\d{2}/\d{4}\b"),
    ("%d-%b-%Y", r"\b\d{1,2}-[A-Za-z]{3}-\d{4}\b"),
    ("%d %b %Y", r"\b\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\b"),
    ("%B %d, %Y", r"\b[A-Za-z]+\s+\d{1,2},\s*\d{4}\b"),
    ("%b %d, %Y", r"\b[A-Za-z]{3}\s+\d{1,2},\s*\d{4}\b"),
]


def parse_date_value(raw: Optional[str]) -> Tuple[Optional[date], Optional[str]]:
    """Parse date strings in various common procurement formats.

    Returns:
        (date_object, warning_or_error)
    """
    if not raw:
        return None, None

    s = raw.strip()
    # Strip common leading prefix words like 'Date:', 'Dated:'
    s = re.sub(r"^(?:date|dated|on)[:\s]*", "", s, flags=re.IGNORECASE).strip()

    for fmt, pat in DATE_PATTERNS:
        match = re.search(pat, s, flags=re.IGNORECASE)
        if match:
            candidate_str = match.group(0)
            try:
                # Normalize case for month abbreviations
                parsed = datetime.strptime(candidate_str, fmt).date()
                return parsed, None
            except ValueError:
                continue

    # Fallback to general dateutil parser if available
    try:
        from dateutil import parser
        parsed = parser.parse(s, fuzzy=True).date()
        return parsed, None
    except Exception:
        pass

    return None, f"Invalid or unrecognized date format: '{raw}'"


def normalize_currency_code(raw: Optional[str]) -> Tuple[str, Optional[str]]:
    """Normalize currency code for Recorda's USD-only MVP scope."""
    if not raw:
        return "USD", None

    s = raw.strip().upper()
    if s in ("$", "US$", "USD", "DOLLARS", "DOLLAR"):
        return "USD", None

    # Flag non-USD currencies as unsupported in MVP
    return s, f"Unsupported currency '{s}' detected. Recorda MVP supports USD only."
