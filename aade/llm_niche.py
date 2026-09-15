"""Lightweight niche inference for outreach (no extra API calls by default)."""

from __future__ import annotations

import re


def infer_niche(business_name: str, default: str = "local service business") -> str:
    """
    Heuristic: tag the lead with a short niche from the name.

    Production systems might use a classifier or a Place `type` from Google.
    """
    n = (business_name or "").lower()
    pairs = [
        (r"restaurant|cafe|coffee|diner|grill|kitchen|bistro|taco|pizza|bar|brew", "food & beverage"),
        (r"salon|spa|barber|nails|hair|tattoo|massage", "beauty & personal care"),
        (r"auto|tire|mechanic|body shop|garage|car", "automotive"),
        (r"dog|veterinary|pet|grooming", "pets"),
        (r"fitness|gym|yoga|crossfit|pilates", "fitness"),
        (r"dental|dentist|medical|clinic|chiropract|physical therapy", "health care"),
        (r"law|attorney|legal", "legal services"),
        (r"plumb|hvac|electric|roof|contract|handyman", "home services"),
    ]
    for pattern, label in pairs:
        if re.search(pattern, n):
            return label
    return default


def street_from_address(address: str | None) -> str:
    """
    Return the first line of an address, used as {Street_Name} in templates.

    Example: "123 Main St, Ann Arbor, MI" -> "123 Main St" (best-effort).
    """
    if not address:
        return "your street"
    first = str(address).split(",")[0].strip()
    return first or "your street"
