"""Service registry — provides services to engine nodes via dependency injection.

Nodes look up services by name (e.g. `services['karmayogi']`, `services['zoho']`,
`services['llm']`). The registry is built once at app startup.
"""

from __future__ import annotations

from typing import Any

from app.adapters.llm import LLMAdapter
from app.adapters.presidio import PresidioRedactor
from app.adapters.translation import TranslationService
from app.adapters.zoho import ZohoDeskAdapter
from app.services.karmayogi import KarmayogiService
from app.services.yp_lookup import YPLookupService


class ServiceRegistry(dict):
    """Dict-like container for services. Engine nodes read from it.

    Keys available to node handlers via ctx.services[...]:
      'karmayogi'    — KarmayogiService  (iGOT portal API gateway)
      'zoho_desk_api'— ZohoDeskAdapter   (Zoho Desk OAuth gateway)
      'llm'          — LLMAdapter        (Vertex AI / vLLM)
      'presidio'     — PresidioRedactor  (in-process PII redaction)
      'translation'  — TranslationService (composite translation chain)
      'yp_lookup'    — YPLookupService   (in-memory YP allocation from Excel)
    """

    @classmethod
    def from_env(cls) -> "ServiceRegistry":
        reg = cls()
        reg["karmayogi"]     = KarmayogiService()
        reg["zoho_desk_api"] = ZohoDeskAdapter()
        reg["llm"]           = LLMAdapter()
        reg["presidio"]      = PresidioRedactor()
        reg["translation"]   = TranslationService.from_settings()
        reg["yp_lookup"]     = YPLookupService()
        return reg

    async def aclose(self) -> None:
        for svc in self.values():
            close = getattr(svc, "aclose", None)
            if callable(close):
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    pass
