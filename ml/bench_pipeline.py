#!/usr/bin/env python3
"""Synthetic pipeline timing bench — no network, no PII DB required."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=5000)
    p.add_argument("--out", type=Path, default=Path("ml/artifacts/bench_pipeline.json"))
    args = p.parse_args()

    rng = np.random.default_rng(42)
    n = args.n
    # Fake Maps-like features
    t0 = time.perf_counter()
    X = np.column_stack(
        [
            rng.uniform(1, 5, n),  # rating
            rng.integers(0, 2000, n),  # reviews
            rng.integers(0, 2, n),  # has_website
            rng.integers(0, 2, n),  # has_phone
            rng.integers(0, 8, n),  # niche id
        ]
    ).astype(float)
    t_features = time.perf_counter() - t0

    # Tiny stand-in model: linear score
    t1 = time.perf_counter()
    w = np.array([0.4, 0.001, 0.3, 0.2, 0.05])
    scores = X @ w
    _ = (scores > np.median(scores)).astype(int)
    t_score = time.perf_counter() - t1

    out = {
        "n": n,
        "feature_build_sec": round(t_features, 4),
        "score_sec": round(t_score, 4),
        "total_sec": round(t_features + t_score, 4),
        "rows_per_sec": int(n / max(t_features + t_score, 1e-9)),
        "note": "Synthetic features only — not live scrape latency",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
