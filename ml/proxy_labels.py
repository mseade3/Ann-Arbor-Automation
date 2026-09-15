"""
Documented PROXY label for "worth prioritizing for outbound digital outreach."

This is NOT a human-labeled conversion outcome. It encodes an explicit sales
heuristic so we can train/evaluate a ranking model before CRM outcomes exist.

Proxy positive (`worth_outreach = 1`) when the lead looks like a reachable local
service business with weak digital presence and enough Maps signal to justify
personalization — roughly the ops filter a human SDR would apply.

Scoring (threshold on continuous score + Bernoulli label noise):
  +3.0  website missing
  +1.2  has phone
  +1.0  service niche (auto / beauty / food / home / health / pets / fitness)
  +0.8  rating in [3.2, 4.7] (established, not brand-dominant)
  +0.6  review_count in [8, 250] (real local demand, not franchise-scale)
  -1.5  review_count > 800 (likely already well-served digitally)
  -1.0  rating >= 4.85 and review_count > 400 (strong organic presence)
  -0.8  niche is incubator / market / coworking style ("local service business"
       with high review volume and website present)

Label = 1{score >= THRESHOLD} XOR flipped with probability LABEL_NOISE.
"""

from __future__ import annotations

from typing import Mapping

import numpy as np

SERVICE_NICHES = frozenset(
    {
        "automotive",
        "beauty & personal care",
        "food & beverage",
        "home services",
        "health care",
        "fitness",
        "pets",
        "legal services",
    }
)

PROXY_THRESHOLD = 3.2
LABEL_NOISE = 0.08
PROXY_VERSION = "v1-sales-heuristic-2026-09"


def proxy_score(row: Mapping[str, object]) -> float:
    """Continuous proxy priority score (higher = more worth outreach)."""
    website_missing = int(bool(row.get("website_missing") or row.get("no_website_flag")))
    has_phone = int(bool(row.get("has_phone")))
    niche = str(row.get("niche") or "local service business").strip().lower()
    rating = row.get("rating")
    review_count = row.get("review_count")

    rating_f = float(rating) if rating is not None else float("nan")
    reviews_i = int(review_count) if review_count is not None else 0

    score = 0.0
    score += 3.0 * website_missing
    score += 1.2 * has_phone
    if niche in SERVICE_NICHES:
        score += 1.0
    if not np.isnan(rating_f) and 3.2 <= rating_f <= 4.7:
        score += 0.8
    if 8 <= reviews_i <= 250:
        score += 0.6
    if reviews_i > 800:
        score -= 1.5
    if (not np.isnan(rating_f)) and rating_f >= 4.85 and reviews_i > 400:
        score -= 1.0
    if niche == "local service business" and not website_missing and reviews_i > 200:
        score -= 0.8
    return score


def proxy_label(
    row: Mapping[str, object],
    *,
    rng: np.random.Generator | None = None,
    noise: float = LABEL_NOISE,
    threshold: float = PROXY_THRESHOLD,
) -> int:
    """Binary proxy label with optional label noise for non-trivial holdout metrics."""
    positive = 1 if proxy_score(row) >= threshold else 0
    if rng is not None and noise > 0 and rng.random() < noise:
        return 1 - positive
    return positive
