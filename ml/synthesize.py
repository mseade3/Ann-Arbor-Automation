"""Synthetic Ann Arbor–like lead table for measurable proxy-label ML."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from ml import NICHES
from ml.proxy_labels import PROXY_VERSION, proxy_label

DEFAULT_N = 2500
DEFAULT_SEED = 42


def synthesize_leads(n: int = DEFAULT_N, seed: int = DEFAULT_SEED) -> pd.DataFrame:
    """
    Generate synthetic Maps-style lead features (no real business PII).

    Distributions are tuned to resemble local SMB listings: mostly mid ratings,
    heavy right-tail on review counts, ~15–25% missing websites.
    """
    rng = np.random.default_rng(seed)
    niches = np.array(NICHES)

    # Over-sample service niches that matter for outreach.
    niche_p = np.array([0.18, 0.12, 0.12, 0.12, 0.08, 0.06, 0.05, 0.04, 0.23])
    niche_p = niche_p / niche_p.sum()
    niche = rng.choice(niches, size=n, p=niche_p)

    # Rating ~ truncated normal around 4.2
    rating = np.clip(rng.normal(4.15, 0.55, size=n), 1.0, 5.0)
    # Reviews: lognormal, then occasional large franchise-like tails
    review_count = np.clip(rng.lognormal(mean=3.2, sigma=1.1, size=n), 0, 5000).astype(int)
    big = rng.random(n) < 0.06
    review_count[big] = rng.integers(600, 3500, size=int(big.sum()))

    website_missing = (rng.random(n) < 0.22).astype(int)
    # Service niches slightly more likely to lack sites
    for i, nic in enumerate(niche):
        if nic in {"automotive", "home services", "beauty & personal care"} and rng.random() < 0.12:
            website_missing[i] = 1

    has_phone = (rng.random(n) < 0.88).astype(int)
    has_description = (rng.random(n) < 0.35).astype(int)
    name_token_count = rng.integers(1, 7, size=n)
    address_completeness = np.clip(rng.beta(5, 1.5, size=n), 0.0, 1.0)

    rows = []
    labels = []
    scores = []
    for i in range(n):
        row = {
            "niche": str(niche[i]),
            "rating": float(round(rating[i], 2)),
            "review_count": int(review_count[i]),
            "website_missing": int(website_missing[i]),
            "has_phone": int(has_phone[i]),
            "has_description": int(has_description[i]),
            "business_name": f"Synth Biz {i}",
            "address": "123 Main St, Ann Arbor, MI 48104" if address_completeness[i] > 0.5 else "Ann Arbor",
            "name_token_count": int(name_token_count[i]),
            "address_completeness": float(address_completeness[i]),
        }
        from ml.proxy_labels import proxy_score

        scores.append(proxy_score(row))
        labels.append(proxy_label(row, rng=rng))
        rows.append(row)

    df = pd.DataFrame(rows)
    df["proxy_score"] = scores
    df["worth_outreach"] = labels
    df["label_source"] = f"proxy:{PROXY_VERSION}"
    # Drop synthetic display names before write — keep only modeling fields.
    return df.drop(columns=["business_name", "address"])


def write_synthetic_csv(path: Path, n: int = DEFAULT_N, seed: int = DEFAULT_SEED) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    df = synthesize_leads(n=n, seed=seed)
    df.to_csv(path, index=False)
    return path


if __name__ == "__main__":
    out = Path(__file__).resolve().parent / "data" / "synthetic_leads.csv"
    write_synthetic_csv(out)
    print(f"Wrote {out}")
