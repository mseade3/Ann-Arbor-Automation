"""
Google Places API (Text Search + Place Details).

More stable than UI scraping. Requires `GOOGLE_PLACES_API_KEY` in the environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import time

import requests

from aade.llm_niche import infer_niche, street_from_address

TEXT_SEARCH = "https://maps.googleapis.com/maps/api/place/textsearch/json"
PLACE_DETAILS = "https://maps.googleapis.com/maps/api/place/details/json"


@dataclass(frozen=True, slots=True)
class RawLead:
    place_id: str
    business_name: str
    address: str | None
    phone: str | None
    website: str | None
    maps_url: str | None
    niche: str
    street_name: str
    business_description: str | None = None
    mission_statement: str | None = None
    rating: float | None = None
    review_count: int | None = None


def fetch_ann_arbor_leads(
    api_key: str,
    *,
    query: str = "small businesses in Ann Arbor MI",
    max_results: int = 20,
) -> list[RawLead]:
    """
    Text Search, then Place Details for website/phone. Pages while results remain.
    """
    if not api_key:
        return []

    out: list[RawLead] = []
    next_token: str | None = None
    while len(out) < max_results:
        params: dict[str, Any] = {
            "query": query,
            "key": api_key,
        }
        if next_token:
            params["pagetoken"] = next_token
        r = requests.get(TEXT_SEARCH, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        status = data.get("status")
        if status not in ("OK", "ZERO_RESULTS"):
            raise RuntimeError(
                f"Places text search: {status} {data.get('error_message', '')}"
            )
        for res in data.get("results", []):
            if len(out) >= max_results:
                return out
            pid = res.get("place_id")
            if not pid:
                continue
            name = (res.get("name") or "").strip()
            address = (res.get("formatted_address") or res.get("vicinity") or "").strip() or None
            detail = _place_details(api_key, pid)
            website = (detail or {}).get("website")
            phone = (detail or {}).get("formatted_phone_number")
            naddr = (detail or {}).get("formatted_address") or address
            maps_url = (detail or {}).get("url")
            editorial = ((detail or {}).get("editorial_summary") or {}).get("overview")
            # Prefer Details fields; fall back to Text Search result.
            rating_raw = (detail or {}).get("rating", res.get("rating"))
            reviews_raw = (detail or {}).get(
                "user_ratings_total",
                res.get("user_ratings_total"),
            )
            rating = float(rating_raw) if rating_raw is not None else None
            review_count = int(reviews_raw) if reviews_raw is not None else None
            niche = infer_niche(name)
            out.append(
                RawLead(
                    place_id=pid,
                    business_name=name,
                    address=naddr,
                    phone=phone,
                    website=website,
                    maps_url=maps_url,
                    niche=niche,
                    street_name=street_from_address(naddr),
                    business_description=(editorial or "").strip() or None,
                    mission_statement=None,
                    rating=rating,
                    review_count=review_count,
                )
            )
        next_token = data.get("next_page_token")
        if not next_token:
            break
        time.sleep(2.0)

    return out


def _place_details(api_key: str, place_id: str) -> dict[str, Any] | None:
    r = requests.get(
        PLACE_DETAILS,
        params={
            "place_id": place_id,
            "fields": "name,formatted_address,formatted_phone_number,website,url,editorial_summary,rating,user_ratings_total",
            "key": api_key,
        },
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "OK":
        return None
    return data.get("result") or {}
