#!/usr/bin/env python3
"""
Train logistic regression + random forest lead-priority classifiers.

Labels are PROXY sales heuristics (see ml/proxy_labels.py), not CRM conversions.
Default training data is synthetic Maps-like features (no PII). Optionally score
anonymized feature rows exported from a local SQLite DB.

Usage:
  python -m ml.train_lead_scorer
  python -m ml.train_lead_scorer --n 3000 --db data/aade.db
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
)
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from ml import FEATURE_COLUMNS
from ml.features import features_frame, load_leads_from_sqlite, row_to_features
from ml.proxy_labels import PROXY_VERSION, proxy_label
from ml.synthesize import synthesize_leads

ROOT = Path(__file__).resolve().parent.parent
ART = Path(__file__).resolve().parent / "artifacts"
DATA = Path(__file__).resolve().parent / "data"


def _metrics(y_true, y_pred) -> dict[str, float]:
    return {
        "accuracy": float(accuracy_score(y_true, y_pred)),
        "precision": float(precision_score(y_true, y_pred, zero_division=0)),
        "recall": float(recall_score(y_true, y_pred, zero_division=0)),
        "f1": float(f1_score(y_true, y_pred, zero_division=0)),
    }


def _plot_confusion(cm: np.ndarray, title: str, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(4.2, 3.6))
    im = ax.imshow(cm, cmap="Blues")
    ax.set_title(title)
    ax.set_xlabel("Predicted")
    ax.set_ylabel("Actual")
    ax.set_xticks([0, 1], ["skip", "outreach"])
    ax.set_yticks([0, 1], ["skip", "outreach"])
    for (i, j), v in np.ndenumerate(cm):
        ax.text(j, i, str(v), ha="center", va="center", color="black")
    fig.colorbar(im, ax=ax, fraction=0.046)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


def build_xy(df: pd.DataFrame) -> tuple[pd.DataFrame, np.ndarray]:
    rows = df.to_dict(orient="records")
    X = features_frame(rows)
    if "worth_outreach" in df.columns:
        y = df["worth_outreach"].astype(int).to_numpy()
    else:
        rng = np.random.default_rng(0)
        y = np.array([proxy_label(r, rng=rng) for r in rows], dtype=int)
    return X, y


def train(
    *,
    n: int = 2500,
    seed: int = 42,
    test_size: float = 0.2,
    db_path: Path | None = None,
) -> dict:
    ART.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)

    synth = synthesize_leads(n=n, seed=seed)
    csv_path = DATA / "synthetic_leads.csv"
    synth.to_csv(csv_path, index=False)

    X, y = build_xy(synth)
    X_train, X_test, y_train, y_test = train_test_split(
        X,
        y,
        test_size=test_size,
        random_state=seed,
        stratify=y,
    )

    numeric = list(FEATURE_COLUMNS)
    pre = ColumnTransformer(
        [("num", StandardScaler(), numeric)],
        remainder="drop",
    )

    logit = Pipeline(
        steps=[
            ("pre", pre),
            (
                "clf",
                LogisticRegression(
                    max_iter=2000,
                    class_weight="balanced",
                    random_state=seed,
                ),
            ),
        ]
    )
    rf = RandomForestClassifier(
        n_estimators=300,
        max_depth=8,
        min_samples_leaf=4,
        class_weight="balanced_subsample",
        random_state=seed,
        n_jobs=-1,
    )

    logit.fit(X_train, y_train)
    rf.fit(X_train, y_train)

    results: dict = {
        "label_type": "proxy",
        "proxy_version": PROXY_VERSION,
        "n_samples": int(len(synth)),
        "n_train": int(len(X_train)),
        "n_test": int(len(X_test)),
        "positive_rate_train": float(y_train.mean()),
        "positive_rate_test": float(y_test.mean()),
        "features": list(FEATURE_COLUMNS),
        "models": {},
    }

    for name, model, Xtr, Xte in (
        ("logistic_regression", logit, X_train, X_test),
        ("random_forest", rf, X_train, X_test),
    ):
        pred = model.predict(Xte)
        m = _metrics(y_test, pred)
        cm = confusion_matrix(y_test, pred, labels=[0, 1])
        results["models"][name] = {
            **m,
            "confusion_matrix": {
                "labels": ["skip(0)", "outreach(1)"],
                "matrix": cm.tolist(),
            },
            "classification_report": classification_report(
                y_test,
                pred,
                target_names=["skip", "outreach"],
                zero_division=0,
            ),
        }
        _plot_confusion(
            cm,
            f"{name} (holdout)",
            ART / f"confusion_{name}.png",
        )
        print(f"\n=== {name} ===")
        print(
            f"Acc={m['accuracy']:.4f}  Prec={m['precision']:.4f}  "
            f"Rec={m['recall']:.4f}  F1={m['f1']:.4f}"
        )
        print(cm)

    # Feature importance from RF (on raw features)
    importances = pd.DataFrame(
        {
            "feature": list(FEATURE_COLUMNS),
            "importance": rf.feature_importances_,
        }
    ).sort_values("importance", ascending=False)
    importances.to_csv(ART / "feature_importance.csv", index=False)

    # Optional: score local DB leads (anonymized feature dump only)
    if db_path and db_path.exists():
        live = load_leads_from_sqlite(db_path)
        if not live.empty:
            live_rows = live.to_dict(orient="records")
            X_live = features_frame(live_rows)
            proba = rf.predict_proba(X_live)[:, 1]
            scored = X_live.copy()
            scored["rf_outreach_proba"] = proba
            scored["proxy_label"] = [proxy_label(r) for r in live_rows]
            # No business names / phones / emails
            scored.to_csv(ART / "live_leads_scored_anonymized.csv", index=False)
            results["live_db"] = {
                "path": str(db_path),
                "n_leads_scored": int(len(scored)),
                "mean_rf_proba": float(proba.mean()),
                "proxy_positive_rate": float(scored["proxy_label"].mean()),
            }
            print(
                f"\nScored {len(scored)} live DB leads → "
                f"{ART / 'live_leads_scored_anonymized.csv'}"
            )

    metrics_path = ART / "metrics.json"
    metrics_path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {metrics_path}")
    print(f"Synthetic CSV: {csv_path}")
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="Train AADE lead-priority models")
    ap.add_argument("--n", type=int, default=2500, help="Synthetic sample size")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--test-size", type=float, default=0.2)
    ap.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Optional local SQLite path to score anonymized features",
    )
    args = ap.parse_args()
    train(n=args.n, seed=args.seed, test_size=args.test_size, db_path=args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
