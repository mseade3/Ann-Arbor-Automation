# SYSTEMS — Ann Arbor Digital Growth Engine

## Architecture tradeoffs

| Choice | Why | Cost |
|--------|-----|------|
| SQLite locally / optional Postgres later | Simple ops for a solo scraper + dashboard | Concurrent writers and remote multi-user are weak |
| Playwright / Places scrape → upsert | Fresh local SMB inventory | Rate limits, ToS, and brittle selectors; must back off |
| OpenAI for outreach/mockups | Fast personalization | Cost, nondeterminism, not a ranking model |
| Streamlit ops UI | Fast internal tooling | Not a multi-tenant product UI |
| Sklearn lead scorer (`ml/`) | Measurable priority signal | **Proxy labels** — recovers the heuristic, not CRM truth |

## Failure modes (ops)

1. **API / scrape rate limits** — Places and Maps-like scrapes will 429 or empty-out under burst. Mitigate: sleep/jitter between requests, cache place IDs, cap batch size, resume from last cursor.
2. **Partial lead rows** — missing phone/website/rating is common. Scorer and outreach must tolerate nulls; do not pretend complete enrichment.
3. **LLM timeouts / empty completions** — outreach path should fail soft (skip or queue retry), never write blank “personalized” copy as success.
4. **Secret leakage** — `.env` and `data/*.db` stay gitignored; never commit outreach text tied to real local businesses in public artifacts.

## What the proxy ML does **not** prove

- Not real conversion / revenue lift
- Not that RF Acc ~0.92 would hold on human-labeled “worth outreach”
- Not production ranking quality — only that models fit a documented sales heuristic on synthetic Maps-like features

Interview line: “I built the pipeline and an offline priority model with explicit proxy labels; production would need human labels or outcome data.”

## Latency / pipeline timing

See `ml/bench_pipeline.py` (synthetic feature rows, no live scrape). Report wall-clock for feature build + LR/RF predict on N rows in README after running locally.
