"""Server-side preload cache for typeahead fields.

Populated at startup via warm() as a background task.
Read by collect_node when rendering Activity.input for typeahead.preload=true fields.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

log = logging.getLogger(__name__)

# Module-level cache: key → list of {id, label} items. Lives for the process lifetime.
_cache: dict[str, list] = {}


async def get_designations(karmayogi: Any) -> list:
    if "designations" in _cache:
        return _cache["designations"]

    PAGE_SIZE = 20
    _alpha_pairs = [c + v for c in "abcdefghijklmnopqrstuvwxyz" for v in "aeiou"]
    _consonant_clusters = [
        "bl", "br", "ch", "cl", "cr", "dr", "fl", "fr", "gl", "gr",
        "nd", "ng", "nt", "ph", "pl", "pr", "sc", "sh", "sk", "sl",
        "sm", "sn", "sp", "sq", "st", "sw", "th", "tr", "tw", "wh",
    ]
    seen_terms: set = set()
    SEARCH_TERMS = [
        t for t in _alpha_pairs + _consonant_clusters
        if not (seen_terms.__contains__(t) or seen_terms.add(t))  # type: ignore[func-returns-value]
    ]

    async def _fetch_page(term: str, page_num: int) -> list:
        try:
            r = await karmayogi.execute_request(
                method="POST",
                url="/apis/public/v8/designation/search",
                body={
                    "pageNumber": page_num,
                    "pageSize": PAGE_SIZE,
                    "filterCriteriaMap": {"status": "Active"},
                    "requestedFields": ["id", "designation"],
                    "searchString": term,
                },
            )
            return r.get("result", {}).get("data", [])
        except Exception as exc:
            log.warning("[preload] designation term=%r page=%d failed: %s", term, page_num, exc)
            return []

    async def _fetch_all_for_term(term: str) -> list:
        try:
            first = await karmayogi.execute_request(
                method="POST",
                url="/apis/public/v8/designation/search",
                body={
                    "pageNumber": 1,
                    "pageSize": PAGE_SIZE,
                    "filterCriteriaMap": {"status": "Active"},
                    "requestedFields": ["id", "designation"],
                    "searchString": term,
                },
            )
        except Exception as exc:
            log.warning("[preload] designation term=%r page=1 failed: %s", term, exc)
            return []

        data: list = first.get("result", {}).get("data", [])
        total_count = int(first.get("result", {}).get("totalCount", 0) or 0)
        if not data:
            return []
        total_pages = max(1, math.ceil(total_count / PAGE_SIZE))

        if total_pages > 1:
            BATCH = 20
            for batch_start in range(2, total_pages + 1, BATCH):
                batch = list(range(batch_start, min(batch_start + BATCH, total_pages + 1)))
                pages = await asyncio.gather(*[_fetch_page(term, p) for p in batch])
                for page_data in pages:
                    data.extend(page_data)
        return data

    log.info("[preload] designations: fetching for %d search terms", len(SEARCH_TERMS))
    term_results: list = []
    TERM_BATCH = 8
    for term_start in range(0, len(SEARCH_TERMS), TERM_BATCH):
        batch = SEARCH_TERMS[term_start:term_start + TERM_BATCH]
        batch_results = await asyncio.gather(*[_fetch_all_for_term(t) for t in batch])
        term_results.extend(batch_results)

    seen_ids: set = set()
    items: list = []
    for term_data in term_results:
        for d in term_data:
            if not isinstance(d, dict) or not d.get("id") or not d.get("designation"):
                continue
            item_id = str(d["id"])
            if item_id not in seen_ids:
                seen_ids.add(item_id)
                items.append({"id": item_id, "label": str(d["designation"])})

    log.info("[preload] designations: loaded %d unique items", len(items))
    _cache["designations"] = items
    return items


async def get_services(karmayogi: Any) -> list:
    if "services" in _cache:
        return _cache["services"]

    try:
        data = await karmayogi.execute_request(
            method="GET",
            url="/api/data/v2/system/settings/get/cadreConfig",
        )
        from app.engine.nodes.api_call_node import _flatten_cadre_services
        flat = _flatten_cadre_services(data.get("response", {}).get("value", {}))
        items = [{"id": s["name"], "label": s["name"]} for s in flat]
        _cache["services"] = items
        log.info("[preload] services: loaded %d items", len(items))
        return items
    except Exception as exc:
        log.warning("[preload] services: failed to load cadreConfig: %s", exc)
        return []


async def warm(services: Any) -> None:
    """Pre-warm all preload caches. Called as a background task at startup."""
    karmayogi = services.get("karmayogi") if hasattr(services, "get") else None
    if karmayogi is None:
        log.warning("[preload] warm: karmayogi service not available, skipping")
        return
    try:
        await get_designations(karmayogi)
    except Exception as exc:
        log.warning("[preload] warm: designations failed: %s", exc)
    try:
        await get_services(karmayogi)
    except Exception as exc:
        log.warning("[preload] warm: services failed: %s", exc)
