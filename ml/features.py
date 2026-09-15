"""Feature builders for lead-priority models (synthetic + SQLite leads)."""

from __future__ import annotations

import math
import re
import sqlite3
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from ml import FEATURE_COLUMNS
from ml.proxy_labels import SERVICE_NICHES


def _niche_flags(niche: str) -> dict[str, int]:
    n = (niche or "").strip().lower()
    return {
        "niche_service_score": int(n in SERVICE_NICHES),
        "niche_is_auto": int(n == "automotive"),
        "niche_is_beauty": int("beauty" in n),
        "niche_is_food": int("food" in n or "beverage" in n),
        "niche_is_home": int("home" in n),
        "niche_is_health": int("health" in n),
    }


def _name_token_count(name: str | None) -> int:
    return len(re.findall(r"[A-Za-z0-9]+", name or ""))


def _address_completeness(address: str | None) -> float:
    if not address:
        return 0.0
    parts = [p.strip() for p in str(address).split(",") if p.strip()]
    # street, city, state/zip-ish
    return min(1.0, len(parts) / 3.0)


def row_to_features(row: Mapping[str, Any]) -> dict[str, float]:
    """Map a lead-like dict to model features (no PII retained)."""
    rating = row.get("rating")
    review_count = row.get("review_count")
    rating_f = float(rating) if rating is not None and str(rating) != "" else 0.0
    reviews_i = int(review_count) if review_count is not None and str(review_count) != "" else 0

    website_missing = row.get("website_missing")
    if website_missing is None:
        website_missing = row.get("no_website_flag")
    if website_missing is None:
        website = (row.get("website") or "").strip()
        website_missing = 0 if website else 1

    has_phone = row.get("has_phone")
    if has_phone is None:
        phone = (row.get("phone") or "").strip()
        has_phone = 1 if phone else 0

    has_description = row.get("has_description")
    if has_description is None:
        desc = (row.get("business_description") or "").strip()
        has_description = 1 if desc else 0

    niche = str(row.get("niche") or "local service business")
    feats = {
        "rating": float(rating_f),
        "log_review_count": float(math.log1p(max(reviews_i, 0))),
        "website_missing": float(int(bool(website_missing))),
        "has_phone": float(int(bool(has_phone))),
        "has_description": float(int(bool(has_description))),
        "name_token_count": float(_name_token_count(row.get("business_name"))),
        "address_completeness": float(_address_completeness(row.get("address"))),
    }
    feats.update({k: float(v) for k, v in _niche_flags(niche).items()})
    return feats


def features_frame(rows: list[Mapping[str, Any]]) -> pd.DataFrame:
    records = [row_to_features(r) for r in rows]
    return pd.DataFrame.from_records(records)[list(FEATURE_COLUMNS)]


def load_leads_from_sqlite(db_path: Path) -> pd.DataFrame:
    """
    Load leads for scoring. Drops PII columns from the returned frame used for
    training exports — keeps only features + niche metadata.
    """
    if not db_path.exists():
        return pd.DataFrame()
    # Ensure optional rating columns exist (migrates older local DBs).
    try:
        from aade import database as db

        with db.connect(db_path):
            pass
    except Exception:
        pass

    conn = sqlite3.connect(str(db_path))
    try:
        cols = {
            str(r[1]) for r in conn.execute("PRAGMA table_info(leads)").fetchall()
        }
        select_cols = [
            "niche",
            "has_website",
            "no_website_flag",
            "phone",
            "website",
            "business_name",
            "address",
        ]
        if "business_description" in cols:
            select_cols.append("business_description")
        if "rating" in cols:
            select_cols.append("rating")
        if "review_count" in cols:
            select_cols.append("review_count")
        df = pd.read_sql_query(
            f"SELECT {', '.join(select_cols)} FROM leads",
            conn,
        )
    finally:
        conn.close()
    if df.empty:
        return df
    if "rating" not in df.columns:
        df["rating"] = np.nan
    if "review_count" not in df.columns:
        df["review_count"] = 0
    if "business_description" not in df.columns:
        df["business_description"] = ""
    df["website_missing"] = df["no_website_flag"].fillna(0).astype(int)
    df["has_phone"] = df["phone"].fillna("").astype(str).str.strip().ne("").astype(int)
    df["has_description"] = (
        df["business_description"].fillna("").astype(str).str.strip().ne("").astype(int)
    )
    return df
