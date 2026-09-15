"""Load configuration from the environment and sensible defaults."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"


def _path(env_key: str, default: Path) -> Path:
    p = os.getenv(env_key)
    return Path(p).expanduser() if p else default


def _secret(key: str) -> str | None:
    """
    Read secrets from environment first, then optional OS keyring.

    Keyring lookup uses:
      - service: AADE_KEYRING_SERVICE (default: ann-arbor-automated)
      - username/account: secret key name (e.g. OPENAI_API_KEY)
    """
    value = os.getenv(key)
    if value:
        return value
    try:
        import keyring  # type: ignore
    except Exception:
        return None
    service = os.getenv("AADE_KEYRING_SERVICE") or "ann-arbor-automated"
    try:
        secret = keyring.get_password(service, key)
    except Exception:
        return None
    return secret or None


def _csv_models(env_key: str, default_csv: str) -> tuple[str, ...]:
    raw = (os.getenv(env_key) or default_csv).strip()
    parts = [p.strip() for p in raw.split(",")]
    values = tuple(p for p in parts if p)
    return values or tuple(x.strip() for x in default_csv.split(",") if x.strip())


def parse_model_csv(raw: str, default_csv: str) -> tuple[str, ...]:
    parts = [p.strip() for p in (raw or "").split(",")]
    values = tuple(p for p in parts if p)
    return values or tuple(x.strip() for x in default_csv.split(",") if x.strip())


# SQLite database file
DB_PATH: Path = _path("AADE_DB_PATH", DATA_DIR / "aade.db")

# Screenshots from visual hook
SCREENSHOT_DIR: Path = _path("AADE_SCREENSHOT_DIR", OUTPUT_DIR / "screenshots")

# API keys
OPENAI_API_KEY: str | None = _secret("OPENAI_API_KEY")
GOOGLE_PLACES_API_KEY: str | None = _secret("GOOGLE_PLACES_API_KEY")
GOOGLE_EMBED_API_KEY: str | None = _secret("GOOGLE_EMBED_API_KEY")
MAPBOX_API_KEY: str | None = _secret("MAPBOX_API_KEY")

# Outbound automation webhooks (optional)
CLAY_ENRICH_WEBHOOK: str | None = os.getenv("AADE_CLAY_ENRICH_WEBHOOK")
ELEVENX_CALL_WEBHOOK: str | None = os.getenv("AADE_ELEVENX_CALL_WEBHOOK")
AGENT_FRANK_EMAIL_WEBHOOK: str | None = os.getenv("AADE_AGENT_FRANK_EMAIL_WEBHOOK")
WATERFALL_WEBHOOK: str | None = os.getenv("AADE_WATERFALL_WEBHOOK")

# "playwright" (default) or "places" when GOOGLE_PLACES_API_KEY is set
MAPS_SOURCE: str = (os.getenv("AADE_MAPS_SOURCE") or "playwright").lower()

# LLM
OUTREACH_MODEL: str = os.getenv("AADE_OUTREACH_MODEL") or "gpt-4o"
NICHE_STYLE_MODELS: tuple[str, ...] = _csv_models(
    "AADE_NICHE_STYLE_MODELS",
    "gpt-5.5,gpt-4o",
)
OUTREACH_MODELS: tuple[str, ...] = _csv_models(
    "AADE_OUTREACH_MODELS",
    "gpt-5.5,gpt-4o",
)
DESIGN_BRIEF_MODELS: tuple[str, ...] = _csv_models(
    "AADE_DESIGN_BRIEF_MODELS",
    "gpt-5.5,gpt-4o",
)
IMAGE_MODEL: str = os.getenv("AADE_IMAGE_MODEL") or "gpt-image-2"


def get_niche_style_models() -> tuple[str, ...]:
    return parse_model_csv(
        os.getenv("AADE_NICHE_STYLE_MODELS") or ",".join(NICHE_STYLE_MODELS),
        ",".join(NICHE_STYLE_MODELS),
    )


def get_outreach_models() -> tuple[str, ...]:
    return parse_model_csv(
        os.getenv("AADE_OUTREACH_MODELS") or ",".join(OUTREACH_MODELS),
        ",".join(OUTREACH_MODELS),
    )


def get_design_brief_models() -> tuple[str, ...]:
    return parse_model_csv(
        os.getenv("AADE_DESIGN_BRIEF_MODELS") or ",".join(DESIGN_BRIEF_MODELS),
        ",".join(DESIGN_BRIEF_MODELS),
    )


def get_image_model() -> str:
    return (os.getenv("AADE_IMAGE_MODEL") or IMAGE_MODEL).strip() or IMAGE_MODEL

# Deduplication window (days) between outreach touches
CONTACT_COOLDOWN_DAYS: int = int(os.getenv("AADE_CONTACT_COOLDOWN_DAYS") or "90")
