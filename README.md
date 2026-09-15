# Ann Arbor Digital Growth Engine (AADE)

Local SMB lead engine for Ann Arbor, MI: scrape Maps/Places → SQLite → optional OpenAI outreach + mockup generation → Streamlit ops dashboard — plus a **measured lead-priority ML layer** (logistic regression + random forest).

**Public repo:** [github.com/mseade3/Ann-Arbor-Automation](https://github.com/mseade3/Ann-Arbor-Automation)

---

## Architecture

```text
┌─────────────────────┐     ┌──────────────┐     ┌──────────────────────────┐
│  Maps discovery     │     │   SQLite     │     │  Streamlit dashboard     │
│  Playwright  and/or │────▶│  data/*.db   │────▶│  map · intel · outreach  │
│  Google Places API  │     │  (local only)│     └──────────────────────────┘
└─────────────────────┘     └──────┬───────┘
                                   │
                    ┌──────────────┼──────────────┐
                    ▼              ▼              ▼
             OpenAI outreach   Design briefs   ml/ lead scorer
             (copy + cooldown) (mockup hooks)  (LR + RF, proxy labels)
```

| Stage | Module | Role |
|-------|--------|------|
| Scrape | `aade/scraper_playwright.py`, `aade/scraper_places.py` | Discover local businesses; Places path stores `rating` + `review_count` |
| Persist | `aade/database.py` | Upsert leads; 90-day outreach cooldown |
| Generate | `aade/outreach.py`, `aade/visual_hook.py` | Personalized copy + landing mockups (API keys required) |
| Operate | `dashboard.py` | Streamlit control plane |
| Score | `ml/train_lead_scorer.py` | Train/evaluate priority models |

---

## Lead-priority ML (proxy-labeled)

There are **no human CRM conversion labels** in this repo yet. Training uses a documented **proxy** sales heuristic (`ml/proxy_labels.py`) on **synthetic** Maps-like features (no PII), with label noise so holdout metrics are non-trivial.

**Positive class:** `worth_outreach = 1` (prioritize for outbound digital pitch).

### Held-out metrics

*(Filled by `python -m ml.train_lead_scorer` — see `ml/artifacts/metrics.json`.)*

| Model | Accuracy | Precision | Recall | F1 |
|-------|---------:|----------:|-------:|---:|
| Logistic regression | 0.840 | 0.845 | 0.875 | 0.860 |
| Random forest | 0.924 | 0.917 | 0.950 | 0.933 |

Stratified 80/20 holdout on *n*=2500 synthetic leads (`random_state=42`, *n*_test=500). Positive class = proxy `worth_outreach`. Confusion matrices: `ml/artifacts/confusion_*.png`.

Reproduce:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m ml.train_lead_scorer --n 2500
# optional: score anonymized features from a local DB (not committed)
python -m ml.train_lead_scorer --db data/aade.db
```

Artifacts: `ml/artifacts/metrics.json`, confusion PNGs, `feature_importance.csv`.  
Notebook: `notebooks/lead_scoring.ipynb`.

---

## Setup

```bash
git clone https://github.com/mseade3/Ann-Arbor-Automation.git
cd Ann-Arbor-Automation
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium   # only if using the Playwright Maps scraper
cp .env.example .env          # add keys locally — never commit .env
python -m aade init-db
```

### Environment

See `.env.example`. Secrets can also live in OS keyring via `python -m aade.secrets`.

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | Outreach + design brief generation |
| `GOOGLE_PLACES_API_KEY` | Stable lead fetch (+ ratings) |
| `AADE_MAPS_SOURCE` | `playwright` (default) or `places` |
| `AADE_DB_PATH` | Default `./data/aade.db` (gitignored) |

### CLI

```bash
python -m aade scrape --query "landscapers in Ann Arbor MI" --max 25
python -m aade outreach --limit 5 --no-screenshots
streamlit run dashboard.py
```

---

## Honest limits

- **Maps Playwright scraping is fragile** (DOM changes, bot checks). Prefer Places API when you have a key.
- **Outreach quality depends on OpenAI**; without a key the scrape → SQLite path still works.
- **Lead ML labels are proxies**, not measured reply/book rates. Treat metrics as pipeline proof, not revenue lift.
- **Local DB, `projects/`, and mockup PNGs are gitignored** — they can contain business contact info and large binaries.
- Niche tagging in `aade/llm_niche.py` is **heuristic regex**, not a trained classifier (the lead scorer is the trained piece).

---

## Repo layout

```text
aade/           # scrape, SQLite, outreach, mockups, CLI
ml/             # proxy labels, synthetic data, train + artifacts
notebooks/      # lead_scoring.ipynb
dashboard.py    # Streamlit ops UI
requirements.txt
.env.example
```

## License

MIT — see [LICENSE](LICENSE).
