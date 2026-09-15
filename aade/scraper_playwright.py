"""
Playwright-based Google Maps discovery (fragile: Maps DOM changes often).

Searches a Maps query, collects `place` hrefs, opens each to read fields.
Use `scraper_places` when you have a Google API key.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from playwright.sync_api import Page, sync_playwright

from aade.database import stable_place_id
from aade.llm_niche import infer_niche, street_from_address
from aade.scraper_places import RawLead


# Default Maps search; user may override
DEFAULT_MAPS_QUERY = "businesses in Ann Arbor Michigan"


@dataclass(frozen=True, slots=True)
class PlaywrightScraperConfig:
    headless: bool = True
    timeout_ms: float = 45_000
    max_places: int = 15
    slow_mo_ms: float = 0


def scrape_maps_playwright(
    query: str = DEFAULT_MAPS_QUERY,
    config: PlaywrightScraperConfig | None = None,
) -> list[RawLead]:
    """
    Return discovered leads. Hrefs that lack a Google `place_id` get a hash id.

    **Note:** Google may block or alter automation; the Places API is preferred.
    """
    cfg = config or PlaywrightScraperConfig()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=cfg.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            context = browser.new_context(
                locale="en-US",
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            page.set_default_timeout(cfg.timeout_ms)
            url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(3_000)
            hrefs = _collect_place_hrefs(page)[: max(cfg.max_places * 2, 20)]
            seen: set[str] = set()
            out: list[RawLead] = []
            for href in hrefs:
                if len(out) >= cfg.max_places:
                    break
                if href in seen:
                    continue
                seen.add(href)
                lead = _scrape_place_page(context, href, page)
                if lead:
                    out.append(lead)
            return out
        finally:
            browser.close()


def _collect_place_hrefs(page: Page) -> list[str]:
    # Multiple selector strategies; Maps updates classes frequently.
    links = page.locator('a[href*="/maps/place/"]')
    n = min(links.count(), 80)
    hrefs: list[str] = []
    for i in range(n):
        h = links.nth(i).get_attribute("href")
        if h and "/maps/place/" in h:
            hrefs.append(_normalize_href(h))
    return hrefs


def _normalize_href(h: str) -> str:
    if h.startswith("/"):
        return f"https://www.google.com{h}"
    return h


def _extract_place_id_from_url(url: str) -> str | None:
    # Standard place id in new URLs: 1s0x...:0x... or ChIJ... in path/query
    m = re.search(r"1s(0x[0-9a-fA-F]+(:|%3A)0x[0-9a-fA-F]+)", url)
    if m:
        return unquote(m.group(1).replace("%3A", ":"))
    m2 = re.search(r"(ChIJ[\w-]{20,})", url)
    if m2:
        return m2.group(1)
    return None


def _scrape_place_page(
    context: Any,
    place_url: str,
    _list_page: Page,
) -> RawLead | None:
    page2 = context.new_page()
    try:
        page2.set_default_timeout(30_000)
        page2.goto(place_url, wait_until="domcontentloaded")
        page2.wait_for_timeout(2_000)
        name = _first_text(
            page2,
            [
                "h1",
                "h1.DUwDvf",
                'div[role="main"] h1',
            ],
        )
        if not name:
            name = "Unknown business"
        address = _read_data_item(page2, "address")
        phone = _read_phone_hrefish(page2)
        if not phone:
            phone = _read_data_item(page2, "phone:tel:1")
        if not phone:
            phone = _read_phone_alt(page2)
        website = _read_website(page2)
        business_description = _read_business_description(page2)
        pid = _extract_place_id_from_url(page2.url) or stable_place_id(
            name=name,
            address=address,
            maps_url=place_url,
        )
        maps_url = page2.url
        niche = infer_niche(name)
        return RawLead(
            place_id=pid,
            business_name=name.strip(),
            address=address,
            phone=phone,
            website=website,
            maps_url=maps_url,
            niche=niche,
            street_name=street_from_address(address),
            business_description=business_description,
            mission_statement=None,
        )
    except Exception:
        return None
    finally:
        page2.close()


def _read_data_item(page: Page, item_id: str) -> str | None:
    btn = page.locator(f'button[data-item-id="{item_id}"]')
    if btn.count() == 0:
        return None
    t = btn.first.inner_text()
    return t.strip() if t else None


def _read_phone_hrefish(page: Page) -> str | None:
    btn = page.locator('button[data-item-id^="phone:"]')
    if btn.count() == 0:
        return None
    t = btn.first.inner_text()
    return t.strip() if t else None


def _read_phone_alt(page: Page) -> str | None:
    a = page.locator('a[href^="tel:"]')
    if a.count() == 0:
        return None
    href = a.first.get_attribute("href") or ""
    if href.startswith("tel:"):
        return href.replace("tel:", "").strip()
    return None


def _read_website(page: Page) -> str | None:
    a = page.locator('a[data-item-id="authority" i]')  # sometimes case varies
    if a.count() == 0:
        a = page.locator('[data-item-id="authority"]')
    if a.count() == 0:
        return None
    href = a.first.get_attribute("href")
    if not href:
        return None
    if "google.com" in (href or ""):
        return None
    return href.strip() or None


def _read_business_description(page: Page) -> str | None:
    candidates = [
        'meta[name="description"]',
        'div[role="main"] [jslog*="metadata"]',
        'div[role="main"] span',
    ]
    for sel in candidates:
        loc = page.locator(sel)
        if loc.count() == 0:
            continue
        if sel.startswith("meta"):
            content = loc.first.get_attribute("content")
            if content and len(content.strip()) > 20:
                return content.strip()
            continue
        text = (loc.first.inner_text() or "").strip()
        if len(text) > 20:
            return text
    return None


def _first_text(page: Page, selectors: list[str]) -> str | None:
    for s in selectors:
        loc = page.locator(s)
        if loc.count() > 0:
            t = loc.first.inner_text()
            if t and t.strip():
                return t.strip()
    return None
