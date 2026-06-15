"""LLM adapter — Vertex AI Gemini via google-genai SDK.

Three use cases, one client:
  generate_ticket_summary()   — transfer_llm node (Mode B): paraphrase → Zoho ticket draft
  generate_choice()           — llm_choose node (Mode C): classify free-text → one of N candidates
  generate_recommendations()  — open_llm_subgraph node (Mode D): semantic course search

Provider switching (LLM_PROVIDER=vertex|vllm) is decided at construction.
Kill-switch: LLM_KILL_SWITCH=true disables all LLM calls; callers fall back gracefully.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.config import settings

# ── Prompts ────────────────────────────────────────────────────────────────────

TICKET_SUMMARY_PROMPT = """You are an iGOT Karmayogi Bharat L1 support assistant.

Read the support conversation transcript and produce a ticket summary for L2.

Return ONLY a valid JSON object with this exact schema (no preamble, no markdown):
{
  "subject": "short ticket subject (max 80 chars)",
  "description": "3-5 sentence summary of the user's issue + what the bot tried",
  "category": "<one of CERTIFICATE_ISSUES | PROFILE_ISSUES | COURSE_ACCESS | MASTER_DATA_REQUEST | TECHNICAL_SUPPORT | GENERAL_INQUIRY>",
  "sub_category": "<specific sub-issue, e.g. NOT_RECEIVED>",
  "classification": "<Query | Service Request | Incident>",
  "priority": "<P1 | P2 | P3 | P4>",
  "severity": "<Sev 1 | Sev 2 | Sev 3 | Sev 4>",
  "portal": "<Learner Portal | MDO Portal>"
}

Priority guide: P1=system down, P2=major feature broken, P3=minor issue, P4=query.
"""

CHOICE_PROMPT = """\
You are an intent classifier for the iGOT Karmayogi Bharat support chatbot.

User message: "{user_text}"

Choose the SINGLE best matching category from this list:
{candidates_json}

Reply with ONLY a JSON object (no preamble, no markdown fences):
{{"choice": "<one of the id values above>", "confidence": <float 0.0-1.0>}}

If nothing fits well, still return the closest match but set confidence below 0.5.\
"""

RECOMMENDATION_PROMPT = """\
You are a course recommendation assistant for iGOT Karmayogi Bharat, India's government learning platform.

User query: "{query}"

{context_section}

Recommend exactly {max_results} relevant government training courses.

Return ONLY a JSON array (no preamble, no markdown fences):
[
  {{
    "title": "Full course title",
    "provider": "Department or institution name",
    "duration": "e.g. 4 hours",
    "karma_points": <integer between 10 and 200>,
    "relevance_explanation": "One sentence: why this matches the query"
  }}
]

Focus on government administration, public policy, digital literacy, leadership, RTI, service delivery.\
"""


class LLMAdapter:
    """Vertex AI Gemini client. Lazily initialised on first use."""

    def __init__(self) -> None:
        self.provider = settings.llm_provider
        self.model_name = settings.genai_model_name
        self.project_id = settings.google_project_id
        self.location = settings.google_location
        self._client = None  # lazy init

    # ── Initialisation ────────────────────────────────────────────────────────

    def _init_vertex(self) -> None:
        """Lazy-init the Vertex AI SDK client."""
        if self._client is not None:
            return
        if not self.project_id or not settings.google_application_credentials:
            return  # creds not configured — caller will raise NotImplementedError

        # google-auth reads GOOGLE_APPLICATION_CREDENTIALS from os.environ, not from
        # pydantic settings. Ensure the env var is set before constructing the client.
        import os
        os.environ.setdefault(
            "GOOGLE_APPLICATION_CREDENTIALS", settings.google_application_credentials
        )

        from google import genai
        self._client = genai.Client(
            vertexai=True,
            project=self.project_id,
            location=self.location,
        )

    def _init_vllm(self) -> None:
        if self._client is not None:
            return
        if not settings.local_llm_api_base:
            return
        from openai import AsyncOpenAI
        self._client = AsyncOpenAI(
            base_url=settings.local_llm_api_base,
            api_key="not-needed",
        )

    def _ensure_client(self) -> None:
        if self.provider == "vertex":
            self._init_vertex()
        else:
            self._init_vllm()
        if self._client is None:
            raise NotImplementedError(
                "LLM adapter not initialised — set GOOGLE_PROJECT_ID + "
                "GOOGLE_APPLICATION_CREDENTIALS (Vertex) or LOCAL_LLM_API_BASE (vLLM)."
            )

    async def _call(self, prompt: str) -> str:
        """Single content-generation call. Returns raw text."""
        response = await asyncio.to_thread(
            self._client.models.generate_content,
            model=self.model_name,
            contents=prompt,
        )
        return response.text.strip()

    @staticmethod
    def _parse_json(raw: str) -> Any:
        """Strip markdown fences and parse JSON."""
        cleaned = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(cleaned)

    # ── Public API ─────────────────────────────────────────────────────────────

    async def generate_ticket_summary(
        self,
        transcript: str,
        collected: dict[str, Any],
        flow_meta: dict[str, Any],
        directives: dict[str, Any],
    ) -> dict[str, Any]:
        """Mode B — paraphrase conversation → structured Zoho ticket draft."""
        from app.services import tracing
        self._ensure_client()
        request_text = (
            f"CONVERSATION TRANSCRIPT:\n{transcript}\n\n"
            f"COLLECTED DATA:\n{json.dumps(collected, indent=2, default=str)}\n\n"
            f"FLOW: {flow_meta.get('flow_id', 'unknown')}\n\n"
            f"DIRECTIVES:\n{directives.get('objective', '')}\n\n"
            "Extract the support ticket fields as JSON."
        )
        full_prompt = f"{TICKET_SUMMARY_PROMPT}\n\n{request_text}"
        with tracing.generation_span(
            model=self.model_name,
            operation="ticket_summary",
            prompt_len=len(full_prompt),
        ):
            raw = await self._call(full_prompt)
            tracing.update_current_generation(output=raw[:800])
        return self._parse_json(raw)

    async def generate_choice(
        self,
        input_text: str,
        candidates: list[dict[str, Any]],
        threshold: float = 0.8,
    ) -> tuple[str, float]:
        """Mode C — classify free-text into one of N candidate node IDs.

        Returns (chosen_candidate_id, confidence).
        If the LLM returns an id not in the candidate list, confidence is set to 0.
        """
        from app.services import tracing
        self._ensure_client()
        candidates_json = json.dumps(
            [{"id": c["id"], "criteria": c.get("criteria", c["id"])} for c in candidates],
            indent=2,
        )
        prompt = CHOICE_PROMPT.format(
            user_text=input_text,
            candidates_json=candidates_json,
        )
        with tracing.generation_span(
            model=self.model_name,
            operation="choice_classification",
            prompt_len=len(prompt),
        ):
            raw = await self._call(prompt)
            tracing.update_current_generation(output=raw[:200])
        result = self._parse_json(raw)

        choice: str = result.get("choice", "")
        confidence: float = float(result.get("confidence", 0.0))

        valid_ids = {c["id"] for c in candidates}
        if choice not in valid_ids:
            confidence = 0.0  # invalid → will trigger on_low_confidence routing

        return choice, confidence

    async def generate_recommendations(
        self,
        query: str,
        context_courses: list[dict[str, Any]] | None = None,
        max_results: int = 5,
    ) -> list[dict[str, Any]]:
        """Mode D — semantic course recommendation from free-text query.

        context_courses: user's existing enrolments (avoid re-recommending).
        Returns list of {title, provider, duration, karma_points, relevance_explanation}.
        """
        from app.services import tracing
        self._ensure_client()
        if context_courses:
            enrolled_titles = [
                c.get("courseName") or c.get("title", "")
                for c in context_courses[:10]
                if c.get("courseName") or c.get("title")
            ]
            context_section = (
                "User's existing enrolments (do NOT recommend these again):\n"
                + "\n".join(f"- {t}" for t in enrolled_titles)
            )
        else:
            context_section = ""

        prompt = RECOMMENDATION_PROMPT.format(
            query=query,
            context_section=context_section,
            max_results=max_results,
        )
        with tracing.generation_span(
            model=self.model_name,
            operation="recommendations",
            prompt_len=len(prompt),
        ):
            raw = await self._call(prompt)
            tracing.update_current_generation(output=raw[:800])
        result = self._parse_json(raw)
        if not isinstance(result, list):
            raise ValueError(f"Expected JSON array, got: {type(result)}")
        return result
