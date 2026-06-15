"""Presidio PII redaction adapter.

Phase 1 stub — uses Microsoft Presidio with India-specific recognizers
(Aadhaar, PAN, UPI, India phone, PIN, IFSC).

Runs over any text before it reaches the LLM adapter — see transfer_llm_node.
Also runs over the transcript attached to Zoho tickets.
"""

from __future__ import annotations

import re

from app.config import settings


# Phase 1 stub: regex-based fallback for the most common India PII patterns.
# Phase 1 production wiring: Microsoft Presidio AnalyzerEngine + AnonymizerEngine
# with custom recognizers registered for in_aadhaar, in_pan, in_phone, etc.
_AADHAAR_RE = re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")
_PAN_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
_INDIA_PHONE_RE = re.compile(r"(?:\+91[\s-]?)?[6-9]\d{9}\b")
_EMAIL_RE = re.compile(r"\b[\w._%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")


class PresidioRedactor:
    """Redacts PII from text. Stub implementation in Phase 1 scaffold."""

    def __init__(self) -> None:
        self.enabled = settings.presidio_enabled
        # Phase 1 production:
        #
        # from presidio_analyzer import AnalyzerEngine, PatternRecognizer, Pattern
        # from presidio_anonymizer import AnonymizerEngine
        # self.analyzer = AnalyzerEngine()
        # # Register India recognizers
        # self.analyzer.registry.add_recognizer(_make_aadhaar_recognizer())
        # ...
        # self.anonymizer = AnonymizerEngine()

    def redact(self, text: str) -> str:
        """Replace PII with placeholders. Returns redacted string."""
        if not self.enabled or not text:
            return text

        text = _AADHAAR_RE.sub("<AADHAAR>", text)
        text = _PAN_RE.sub("<PAN>", text)
        text = _INDIA_PHONE_RE.sub("<PHONE>", text)
        text = _EMAIL_RE.sub("<EMAIL>", text)
        return text
