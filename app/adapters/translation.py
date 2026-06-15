"""Translation adapter — composite chain with automatic failover.

Provider chain (configurable via settings.translation_primary):
  1. GeminiTranslator        — primary; reuses existing Vertex AI credentials, no extra billing
  2. GoogleTranslateAdapter  — fallback; Cloud Translation API v3 via Application Default Credentials
  3. BhashiniAdapter         — last resort; free, India-optimised, but unreliable uptime

All providers share a single async interface: translate(text, src, tgt) -> str.

Fail-open policy: if ALL providers fail, the original text is returned and a warning
is logged — a translation failure must never crash or block a conversation.

Language boundary rule (architecture):
  Translation happens ONLY in the channel adapter layer, NOT inside flow YAML or
  engine nodes. The engine always receives English input and produces English output.
  This keeps all flow YAMLs language-agnostic.

Language codes: BCP-47  e.g. "hi", "ta", "kn", "mr", "en"

Dev/test mode:
  Set TRANSLATION_ENABLED=false in .env to skip all translation (returns original text).
  Useful for local development without GCP credentials.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from app.config import settings

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

# BCP-47 → full name (used in Gemini prompt for better accuracy)
SUPPORTED_LANGUAGES: dict[str, str] = {
    "en": "English",
    "hi": "Hindi",
    "ta": "Tamil",
    "te": "Telugu",
    "kn": "Kannada",
    "ml": "Malayalam",
    "mr": "Marathi",
    "gu": "Gujarati",
    "bn": "Bengali",
    "pa": "Punjabi",
    "or": "Odia",
    "as": "Assamese",
    "ur": "Urdu",
}


# =============================================================================
# Base
# =============================================================================

class BaseTranslator(ABC):
    name: str = "base"
    timeout_s: float = 3.0

    @abstractmethod
    async def translate(self, text: str, src: str, tgt: str) -> str:
        """Translate text from src language to tgt language.

        Raises an exception on any failure — the caller (TranslationService)
        handles fallover and logging.
        """
        ...


# =============================================================================
# Provider: Gemini (primary)
# =============================================================================

class GeminiTranslator(BaseTranslator):
    """Uses Gemini via Vertex AI (google-genai SDK) for translation.

    Reuses the project's existing GCP credentials — no extra setup.
    Slightly higher latency than Cloud Translation API but more accurate for
    code-mixed and low-resource Indian languages.
    """

    name = "gemini"

    def __init__(self) -> None:
        self.timeout_s = settings.translation_gemini_timeout_s
        self._client = None  # lazy init

    def _get_client(self):
        if self._client is not None:
            return self._client
        if not settings.google_project_id or not settings.google_application_credentials:
            raise RuntimeError("Vertex AI credentials not configured for Gemini translator")
        from google import genai
        self._client = genai.Client(
            vertexai=True,
            project=settings.google_project_id,
            location=settings.google_location,
        )
        return self._client

    async def translate(self, text: str, src: str, tgt: str) -> str:
        tgt_name = SUPPORTED_LANGUAGES.get(tgt, tgt)
        src_name = SUPPORTED_LANGUAGES.get(src, src)
        prompt = (
            f"Translate the following {src_name} text to {tgt_name}. "
            f"Return ONLY the translated text — no explanations, no quotes.\n\n{text}"
        )
        client = self._get_client()
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=settings.genai_model_name,
            contents=prompt,
        )
        return response.text.strip()


# =============================================================================
# Provider: Google Cloud Translation API v3 (fallback)
# =============================================================================

class GoogleTranslateAdapter(BaseTranslator):
    """Cloud Translation API v3.

    Uses Application Default Credentials by default (same GCP project — no
    additional key needed). Set GOOGLE_TRANSLATE_API_KEY in .env only if you
    want to use a separate service account.
    """

    name = "google_translate"

    def __init__(self) -> None:
        self.timeout_s = settings.translation_google_translate_timeout_s

    async def translate(self, text: str, src: str, tgt: str) -> str:
        from google.cloud import translate_v3 as tr  # lazy import

        loop = asyncio.get_event_loop()
        client = tr.TranslationServiceClient()
        parent = f"projects/{settings.google_project_id}/locations/global"

        response = await loop.run_in_executor(
            None,
            lambda: client.translate_text(
                request={
                    "parent": parent,
                    "contents": [text],
                    "source_language_code": src,
                    "target_language_code": tgt,
                    "mime_type": "text/plain",
                }
            ),
        )
        return response.translations[0].translated_text


# =============================================================================
# Provider: Bhashini (last resort)
# =============================================================================

class BhashiniAdapter(BaseTranslator):
    """Bhashini ULCA / Udyat API — free, government-run, India-optimised.

    Best quality for Indian language pairs, but uptime is unreliable.
    Used as last resort after Gemini and Google Translate have failed.
    Requires BHASHINI_UDYAT_KEY and BHASHINI_API_ENDPOINT in .env.
    """

    name = "bhashini"

    def __init__(self) -> None:
        self.timeout_s = settings.translation_bhashini_timeout_s

    async def translate(self, text: str, src: str, tgt: str) -> str:
        import httpx  # lazy import

        if not settings.bhashini_udyat_key or not settings.bhashini_api_endpoint:
            raise RuntimeError("Bhashini credentials not configured")

        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.post(
                f"{settings.bhashini_api_endpoint}/pipeline/compute",
                headers={
                    "Authorization": settings.bhashini_udyat_key,
                    "userID": settings.bhashini_user_id,
                },
                json={
                    "pipelineTasks": [
                        {
                            "taskType": "translation",
                            "config": {
                                "language": {
                                    "sourceLanguage": src,
                                    "targetLanguage": tgt,
                                }
                            },
                        }
                    ],
                    "inputData": {"input": [{"source": text}]},
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["pipelineResponse"][0]["output"][0]["target"]


# =============================================================================
# Composite TranslationService
# =============================================================================

class TranslationService:
    """Composite translation service with automatic failover.

    Provider order: primary (from settings) → remaining providers in fixed order.
    On failure, the next provider is tried. If all fail, the original text is
    returned (fail-open) and an error is logged.

    Usage:
        svc = TranslationService.from_settings()

        # Translate user input to English before engine
        english_text = await svc.to_english("नमस्ते", src="hi")

        # Translate engine output back to user's language
        hindi_reply = await svc.from_english("Hello! How can I help?", tgt="hi")
    """

    def __init__(self, providers: list[BaseTranslator]) -> None:
        self._providers = providers

    @classmethod
    def from_settings(cls) -> "TranslationService":
        """Build the provider chain from application settings."""
        all_providers: dict[str, BaseTranslator] = {
            "gemini": GeminiTranslator(),
            "google_translate": GoogleTranslateAdapter(),
            "bhashini": BhashiniAdapter(),
        }
        primary = settings.translation_primary
        # Primary first, then others in a sensible fallback order
        order = [primary] + [k for k in ["gemini", "google_translate", "bhashini"] if k != primary]
        return cls(providers=[all_providers[k] for k in order])

    async def translate(self, text: str, src: str, tgt: str) -> str:
        """Translate text. Returns original on all-provider failure (fail-open)."""
        if not text.strip() or src == tgt or not settings.translation_enabled:
            return text

        for provider in self._providers:
            try:
                result = await asyncio.wait_for(
                    provider.translate(text, src, tgt),
                    timeout=provider.timeout_s,
                )
                log.debug("Translation via %s: %s→%s (%d chars)", provider.name, src, tgt, len(text))
                return result
            except Exception as exc:  # noqa: BLE001
                log.warning("Translation provider '%s' failed (%s→%s): %s", provider.name, src, tgt, exc)
                continue

        log.error(
            "All translation providers failed for %s→%s. Returning original text.", src, tgt
        )
        return text  # fail-open

    async def to_english(self, text: str, src: str) -> str:
        """Translate user input → English (inbound boundary)."""
        return await self.translate(text, src=src, tgt="en")

    async def from_english(self, text: str, tgt: str) -> str:
        """Translate engine output → user's preferred language (outbound boundary)."""
        return await self.translate(text, src="en", tgt=tgt)
