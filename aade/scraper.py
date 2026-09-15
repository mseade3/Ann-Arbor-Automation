"""
Lead discovery: choose Google Places API or Playwright based on `aade.config`.
"""

from __future__ import annotations

from aade.config import GOOGLE_PLACES_API_KEY, MAPS_SOURCE
from aade.scraper_places import RawLead, fetch_ann_arbor_leads as _places
from aade.scraper_playwright import (
    PlaywrightScraperConfig,
    scrape_maps_playwright,
)


def _use_places_api() -> bool:
    if not GOOGLE_PLACES_API_KEY:
        return False
    return MAPS_SOURCE in ("places", "google", "api")


def fetch_leads(
    *,
    query: str = "small businesses in Ann Arbor MI",
    max_results: int = 20,
    playwright: PlaywrightScraperConfig | None = None,
) -> list[RawLead]:
    """
    Return raw leads from Google Places (when key + `AADE_MAPS_SOURCE=places`) or Playwright.
    """
    if _use_places_api():
        return _places(
            GOOGLE_PLACES_API_KEY,
            query=query,
            max_results=max_results,
        )
    return scrape_maps_playwright(
        query=query,
        config=playwright
        or PlaywrightScraperConfig(
            headless=True,
            max_places=max_results,
        ),
    )
