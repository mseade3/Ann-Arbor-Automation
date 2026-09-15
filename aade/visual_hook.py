"""
Render niche-aware, AI-styled Tailwind landing pages and take screenshots.

Pipeline:
  1. call_niche_agent()  → OpenAI model returns NicheStyle JSON (primary_color, font, etc.)
  2. build_landing_html() → injects style into a full-bleed hero + glassmorphism template
  3. screenshot_landing_page() → headless Chromium renders & saves PNG
"""

from __future__ import annotations

import base64
import json
import math
import re
import secrets
import string
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

from playwright.sync_api import sync_playwright

from aade.hero_layouts import layout_for, render_hero

PROJECTS_DIR = Path(__file__).resolve().parent.parent / "projects"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_fragment(name: str) -> str:
    t = re.sub(r"[^\w\s\-'.&,]", "", name or "Business", flags=re.ASCII)
    return t.strip() or "Local business"


def _normalize_niche(niche: str | None) -> str:
    value = (niche or "").strip().lower()
    if not value:
        return "local business"
    aliases = {
        "landscape": "landscaping",
        "landscaper": "landscaping",
        "car": "auto",
        "automotive": "auto",
        "barbershop": "barber",
        "food": "restaurant",
        "cafe": "restaurant",
        "doctor": "medical",
        "clinic": "medical",
        "dental": "medical",
        "dentist": "medical",
        "medical office": "medical",
    }
    return aliases.get(value, value)


def _slug(value: str) -> str:
    return re.sub(r"[^\w\-]+", "-", (value or "business").strip().lower()).strip("-") or "business"


def project_folder_for_business(business_name: str) -> Path:
    folder = PROJECTS_DIR / _slug(_safe_fragment(business_name))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


# ---------------------------------------------------------------------------
# Design overrides (from outreach.py pipeline)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class DesignOverrides:
    color_theme: str
    font_family: str
    hero_image_prompt: str


# ---------------------------------------------------------------------------
# Niche Agent — OpenAI style config
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class NicheStyle:
    primary_color: str       # hex e.g. "#10b981"
    font_family: str         # Google Fonts name
    unsplash_keyword: str    # 2-3 word Unsplash search phrase
    animation_style: str     # AOS data-aos value
    tone: str                # Rugged | Luxury | Friendly | Clinical | Sharp


_NICHE_STYLE_CACHE: dict[str, NicheStyle] = {}

_NICHE_AGENT_SYSTEM = """You are a web design strategist for local business landing pages.
Given a business name and niche, return ONLY valid JSON (no markdown) with exactly these keys:
- primary_color: hex color code matching the niche personality
- font_family: a Google Fonts name (Inter, Playfair Display, DM Sans, Space Grotesk, Syne)
- unsplash_keyword: 2-3 word Unsplash search phrase (no city names, no quotes)
- animation_style: exactly one of: fade-up, zoom-in, fade-right, flip-up
- tone: exactly one of: Rugged, Luxury, Friendly, Clinical, Sharp

Color guide:
- Landscaping  → Emerald #10b981 or Stone #78716c
- Auto Repair  → Steel Blue #3b82f6 or Slate #475569
- Barber       → Amber #f59e0b or Warm Stone #a8a29e
- Medical      → Sky #0ea5e9 or Indigo #6366f1
- Restaurant   → Rose #f43f5e or Amber #f59e0b
- Default      → Violet #8b5cf6 or Cyan #06b6d4"""

_NICHE_FALLBACKS: dict[str, NicheStyle] = {
    "landscaping": NicheStyle("#10b981", "Playfair Display", "garden landscape estate luxury",  "fade-up",    "Luxury"),
    "auto":        NicheStyle("#3b82f6", "Space Grotesk",    "auto repair workshop industrial", "fade-right", "Rugged"),
    "barber":      NicheStyle("#f59e0b", "Syne",             "barbershop grooming style",       "zoom-in",    "Sharp"),
    "medical":     NicheStyle("#0ea5e9", "DM Sans",          "modern clinic medical clean",     "fade-up",    "Clinical"),
    "restaurant":  NicheStyle("#f43f5e", "Playfair Display", "restaurant food dining luxury",   "fade-up",    "Luxury"),
}
_NICHE_DEFAULT = NicheStyle("#8b5cf6", "Inter", "modern local business professional", "fade-up", "Friendly")

# Curated, verified Unsplash photo IDs. The keyword-search endpoint this used to
# build (`/photo-1?keywords=...`) is not a real asset path and always 404'd, so
# every hero rendered empty. Pinned IDs are stable and give each niche its own
# photography instead of an accent-colour swap.
_NICHE_HERO: dict[str, str] = {
    "landscaping": "photo-1558904541-efa843a96f01",
    "auto":        "photo-1486262715619-67b85e0b08d3",
    "barber":      "photo-1503951914875-452162b0f3f1",
    # Not photo-1519494026892: that corridor carries legible Spanish hospital
    # signage (BANCO DE SANGRE, TERAPIA NEONATAL) — wrong for a US practice.
    "medical":     "photo-1576091160399-112ba8d25d1d",
    "restaurant":  "photo-1517248135467-4c7edcad34c4",
    "default":     "photo-1487754180451-c456f719a1fc",
}

# Hero image opacity per niche — bright daylight shots need knocking back
# further than dim interiors to keep the headline legible.
_NICHE_HERO_OPACITY: dict[str, str] = {
    "landscaping": "0.52",
    "auto":        "0.46",
    "barber":      "0.50",
    "medical":     "0.55",
    "restaurant":  "0.46",
    "default":     "0.46",
}


# Page surface: medical needs a light clinical register; most trades stay dark.
_NICHE_SURFACE: dict[str, str] = {
    "medical": "light",
    "restaurant": "warm",
    "landscaping": "dark",
    "auto": "dark",
    "barber": "dark",
    "default": "dark",
}


def _hero_image_url(niche_key: str) -> str:
    photo_id = _NICHE_HERO.get(niche_key, _NICHE_HERO["default"])
    return f"https://images.unsplash.com/{photo_id}?auto=format&fit=crop&q=80&w=1920"


def _static_map_html(lat: float, lon: float, *, zoom: int = 15, cols: int = 3, rows: int = 2, tile_svc: str = "World_Dark_Gray_Base", css_filter: str = "brightness(1.18) contrast(1.05)") -> str:
    """
    Build a static map from raw dark-theme map tiles as plain <img> elements.

    An `openstreetmap.org/export/embed.html` iframe loads without error but
    paints nothing when the host page has no real origin, which is exactly the
    case for screenshots and email previews. Tiles have no such constraint.

    Esri World Dark Gray tiles are used: Carto's public dark_all endpoint now
    returns watermarked "API KEY REQUIRED" tiles even on HTTP 200.

    The pin is offset by the business's true position within its centre tile,
    so it marks the actual address rather than the middle of the crop.
    """
    n = 2 ** zoom
    fx = (lon + 180.0) / 360.0 * n
    fy = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    xt, yt = int(fx), int(fy)
    x0 = xt - (cols - 1) // 2
    y0 = yt - (rows - 1) // 2

    # Fractional position of the point across the whole composed grid.
    pin_left = min(94.0, max(6.0, ((fx - x0) / cols) * 100.0))
    pin_top = min(96.0, max(14.0, ((fy - y0) / rows) * 100.0))

    # Esri ArcGIS World Dark Gray — no API key, no watermark.
    # Path order is z/y/x (not z/x/y like OSM/Carto).
    tiles = "".join(
        f'<img src="https://server.arcgisonline.com/ArcGIS/rest/services/'
        f'Canvas/{tile_svc}/MapServer/tile/{zoom}/{y0 + dy}/{x0 + dx}" '
        f'alt="" loading="eager" class="block h-auto w-full select-none" '
        f'style="filter: {css_filter};" />'
        for dy in range(rows)
        for dx in range(cols)
    )
    return (
        '<div class="relative overflow-hidden rounded-xl border border-white/15">'
        f'<div class="grid" style="grid-template-columns: repeat({cols}, 1fr);">{tiles}</div>'
        '<div class="pointer-events-none absolute inset-0 rounded-xl ring-1 ring-inset ring-white/10"></div>'
        '<div class="pointer-events-none absolute -translate-x-1/2 -translate-y-full" '
        f'style="left:{pin_left:.2f}%; top:{pin_top:.2f}%;">'
        '<svg width="30" height="42" viewBox="0 0 24 34" fill="none" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M12 0C5.4 0 0 5.4 0 12c0 9 12 22 12 22s12-13 12-22c0-6.6-5.4-12-12-12z" '
        'fill="var(--primary)" stroke="rgba(255,255,255,.9)" stroke-width="1.5"/>'
        '<circle cx="12" cy="12" r="4.5" fill="#020817"/></svg></div></div>'
    )


def _aos(animate: bool, name: str, delay: int = 0, duration: int = 0) -> str:
    """
    Render AOS attributes, or nothing in static mode.

    AOS holds `[data-aos]` elements at opacity 0 until they scroll into view.
    A full-page screenshot never scrolls, so in static mode the attributes are
    omitted entirely — otherwise every section below the fold renders blank in
    the PNG that actually gets sent to a prospect.
    """
    if not animate:
        return ""
    out = f'data-aos="{name}"'
    if delay:
        out += f' data-aos-delay="{delay}"'
    if duration:
        out += f' data-aos-duration="{duration}"'
    return out


def generate_premium_mockup(
    business_name: str,
    niche: str,
    vibe: str,
    *,
    size: str = "3840x2160",
    quality: str = "high",
    out_dir: Path | None = None,
) -> str:
    """
    Generate a premium landing-page mockup with OpenAI Images 2.0 (gpt-image-2).

    Returns:
      - URL when the API returns a hosted image URL
      - local file path when the API returns base64 image bytes
    """
    from aade.config import OPENAI_API_KEY, get_image_model  # avoid circular at module load
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured.")

    from openai import OpenAI

    title = _safe_fragment(business_name)
    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.images.generate(
        model=get_image_model(),
        prompt=(
            "Reason about a high-converting, modern landing page for "
            f"'{title}', a {niche} business in Ann Arbor.\n\n"
            f"LAYOUT: Use a {vibe} aesthetic with clean section hierarchy.\n"
            f"CONTENT: Render the business name '{title}' clearly in the hero section.\n"
            "The call-to-action button must say 'Book in Ann Arbor'.\n"
            "STYLE: 4K resolution, cinematic lighting, premium UI polish, and a "
            "glassmorphism overlay.\n"
            "TEXT QUALITY: Keep all text sharp, legible, and correctly spelled."
        ),
        size=size,
        quality=quality,
        n=1,
    )

    image = response.data[0]
    image_url = getattr(image, "url", None)
    if image_url:
        return image_url

    b64_data = getattr(image, "b64_json", None)
    if b64_data:
        target_dir = out_dir or (project_folder_for_business(title) / "assets")
        target_dir.mkdir(parents=True, exist_ok=True)
        suffix = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6))
        safe = re.sub(r"[^\w\-]+", "-", title[:40]).strip("-") or "mockup"
        out_path = target_dir / f"mockup_{safe}_{suffix}.png"
        out_path.write_bytes(base64.b64decode(b64_data))
        return str(out_path)

    raise RuntimeError("Images API returned no URL or image bytes.")


def premium_vibe_for_niche(niche: str, color_theme: str = "") -> str:
    n = _normalize_niche(niche)
    theme = (color_theme or "").strip().lower()
    if "medical" in n or "clinic" in n or "dental" in n or "clean" in theme:
        return "clean, clinical, minimalist"
    if "barber" in n or "style" in theme:
        return "bold, editorial, high-contrast"
    if "auto" in n or "industrial" in theme or "slate" in theme:
        return "industrial, sharp, modern"
    if "land" in n or "earth" in theme or "emerald" in theme:
        return "organic luxury, earthy, premium"
    if "restaurant" in n or "food" in n:
        return "warm, cinematic, upscale hospitality"
    return "modern premium, local-trust, conversion-focused"


def call_niche_agent(
    business_name: str,
    niche: str,
    description: str | None = None,
) -> NicheStyle:
    """
    Ask OpenAI models for a per-business visual style config.
    Falls back to niche presets without API key.
    Results are cached in-process to avoid redundant calls.
    """
    niche_key = _normalize_niche(niche)
    cache_key = f"{business_name}|{niche_key}"
    if cache_key in _NICHE_STYLE_CACHE:
        return _NICHE_STYLE_CACHE[cache_key]

    fallback = _NICHE_FALLBACKS.get(niche_key, _NICHE_DEFAULT)

    try:
        from aade.config import OPENAI_API_KEY, get_niche_style_models  # avoid circular at module load
        if not OPENAI_API_KEY:
            return fallback
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        user_payload = (
            f"Business: {business_name}\n"
            f"Niche: {niche}\n"
            f"Description: {description or 'not provided'}\n\n"
            "Reason about the visual identity of this business and return a JSON config."
        )
        # Try preferred models in order, e.g. gpt-5.5 first, then gpt-4o.
        for model_name in get_niche_style_models():
            try:
                resp = client.chat.completions.create(
                    model=model_name,
                    temperature=0.2,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": _NICHE_AGENT_SYSTEM},
                        {"role": "user", "content": user_payload},
                    ],
                )
                raw = (resp.choices[0].message.content or "").strip()
                data = json.loads(raw)
                style = NicheStyle(
                    primary_color=str(data.get("primary_color") or fallback.primary_color),
                    font_family=str(data.get("font_family") or fallback.font_family),
                    unsplash_keyword=str(data.get("unsplash_keyword") or fallback.unsplash_keyword),
                    animation_style=str(data.get("animation_style") or fallback.animation_style),
                    tone=str(data.get("tone") or fallback.tone),
                )
                _NICHE_STYLE_CACHE[cache_key] = style
                return style
            except Exception:
                continue
        return fallback
    except Exception:
        return fallback


# ---------------------------------------------------------------------------
# Niche copy boilerplates
# ---------------------------------------------------------------------------

# Copy speaks to the business's OWN customers, not to the business owner.
# Earlier drafts described the website's features ("social proof above the
# fold"), which reads as a pitch deck wearing the prospect's name.
_COPY: dict[str, dict[str, object]] = {
    "landscaping": {
        "headline": "Design, Build, and Keep It Looking Right",
        "subheadline": "Outdoor design and year-round care for Ann Arbor homes — from first sketch to weekly upkeep.",
        "section_h2": "What we do for your yard",
        "find_us": "Serving Ann Arbor, Saline, and Dexter. On-site estimates are free, and we'll walk the property with you.",
                "gallery_kicker": "Recent work",
        "gallery_h2": "Yards we've built",
        "steps_h2": "From first walk-through to weekly care",
        "eyebrow": "Ann Arbor · Outdoor Design & Care",
        "cta_primary": "Get a free estimate →",
        "cta_secondary": "See our yard work",
        "nav_services": "Services",
        "nav_work": "Yards",
"benefits": [
            "Design consultations with a written plan and plant list before anything is planted.",
            "Weekly mowing, pruning, mulching, and spring and fall cleanups.",
            "Patios, retaining walls, and irrigation installed by our own crew.",
        ],
        "testimonials": [
            ('"They redid the front beds and the whole house looks newer."', "Karen M. · Burns Park"),
            ('"Same crew every week, and they never leave a mess."', "Dave R. · Ann Arbor Hills"),
        ],
    },
    "auto": {
        "headline": "Honest Diagnostics, Same-Week Appointments",
        "subheadline": "Your car looked at by ASE-certified techs, with a written estimate before any work starts.",
        "section_h2": "Everything your car needs",
        "find_us": "Free customer parking and a clean waiting room. We'll shuttle you within three miles while you wait.",
                "gallery_kicker": "In the shop",
        "gallery_h2": "What we work on",
        "steps_h2": "From symptom to back on the road",
        "eyebrow": "Ann Arbor · ASE Auto Repair",
        "cta_primary": "Book a diagnosis →",
        "cta_secondary": "See what we fix",
        "nav_services": "Services",
        "nav_work": "Shop",
"benefits": [
            "Computer diagnostics with a written estimate before we touch anything.",
            "Brakes, tires, batteries, and oil changes, usually same week.",
            "ASE-certified technicians and a 12-month warranty on repairs.",
        ],
        "testimonials": [
            ('"They showed me the worn pad instead of just charging me."', "Marcus T. · Ypsilanti"),
            ('"Quoted me half what the dealer wanted, done in a day."', "Priya S. · Ann Arbor"),
        ],
    },
    "barber": {
        "headline": "Sharp Cuts, No Waiting Around",
        "subheadline": "Classic barbering on State Street — book your barber online or walk in.",
        "section_h2": "What happens in the chair",
        "find_us": "Walk-ins welcome most weekdays. Weekends book up fast, so reserve your chair online.",
                "gallery_kicker": "The shop",
        "gallery_h2": "Work from our chairs",
        "steps_h2": "From booking to out the door",
        "eyebrow": "Ann Arbor · Classic Barbering",
        "cta_primary": "Book a chair →",
        "cta_secondary": "See the cuts",
        "nav_services": "Services",
        "nav_work": "Cuts",
"benefits": [
            "Precision cuts, fades, and beard shaping.",
            "Hot-towel straight-razor shaves.",
            "Book the same barber every time, online in about thirty seconds.",
        ],
        "testimonials": [
            ('"First shop in years that got the fade right the first time."', "Andre W. · Kerrytown"),
            ('"In and out in twenty minutes on my lunch break."', "Chris L. · Downtown"),
        ],
    },
    "medical": {
        "headline": "Care for Your Whole Family, Close to Home",
        "subheadline": "Primary care in Ann Arbor with same-day sick visits and early-morning appointments.",
        "section_h2": "How we care for patients",
        "find_us": "Accepting new patients. Early-morning and Saturday appointments available, with parking at the door.",
                "gallery_kicker": "Our practice",
        "gallery_h2": "Inside the clinic",
        "steps_h2": "From first call to ongoing care",
        "eyebrow": "Ann Arbor · Family Primary Care",
        "cta_primary": "Request an appointment →",
        "cta_secondary": "See how we care",
        "nav_services": "Care",
        "nav_work": "Clinic",
"benefits": [
            "Same-day sick visits and annual physicals.",
            "Pediatric through geriatric primary care under one roof.",
            "Most major insurance accepted, with billing explained up front.",
        ],
        "testimonials": [
            ('"Got my daughter in the same morning she spiked a fever."', "Rachel K. · Ann Arbor"),
            ('"They actually explain things instead of rushing out."', "Tom B. · Pittsfield"),
        ],
    },
    "restaurant": {
        "headline": "What's on the Table Tonight",
        "subheadline": "Seasonal cooking from Michigan growers, two blocks off Main Street.",
        "section_h2": "What's on the table tonight",
        "find_us": "Two blocks from Main Street with street parking and a patio in summer. Reservations recommended on weekends.",
                "gallery_kicker": "The room",
        "gallery_h2": "What we're serving",
        "steps_h2": "From reservation to last round",
        "eyebrow": "Ann Arbor · Seasonal Dining",
        "cta_primary": "Reserve a table →",
        "cta_secondary": "See the menu",
        "nav_services": "Menu",
        "nav_work": "Plates",
"benefits": [
            "Seasonal menus built from Michigan farms and growers.",
            "Reservations, takeout, and private events for up to forty.",
            "Full bar with local drafts and a short, well-chosen wine list.",
        ],
        "testimonials": [
            ('"The fall menu is the best meal we\'ve had in Ann Arbor."', "Elena V. · Old West Side"),
            ('"Booked the back room for twenty and they nailed it."', "Greg H. · Ann Arbor"),
        ],
    },
    "default": {
        "headline": "Local, Reliable, and Easy to Reach",
        "subheadline": "Straight answers, fair pricing, and work that holds up — right here in Ann Arbor.",
        "section_h2": "What we do best, every day",
        "find_us": "Based in Ann Arbor and serving the surrounding townships. Call or send a message and we'll get right back to you.",
                "gallery_kicker": "Our work",
        "gallery_h2": "A look at what we do",
        "steps_h2": "From first call to finished job",
        "eyebrow": "Ann Arbor · Local & Trusted",
        "cta_primary": "Get in touch →",
        "cta_secondary": "See our work",
        "nav_services": "Services",
        "nav_work": "Work",
"benefits": [
            "Clear pricing quoted before the work begins.",
            "Scheduling that fits around your week, not ours.",
            "Local crew that stands behind what it delivers.",
        ],
        "testimonials": [
            ('"Showed up when they said they would and did it right."', "Janet P. · Ann Arbor"),
            ('"Straightforward pricing, no surprises on the invoice."', "Sam O. · Dexter"),
        ],
    },
}


# Three-step process band. Kept to things that are true of any shop in the
# trade — no invented prices, which would be a bad thing to put on someone
# else's page in a cold-outreach preview.
_STEPS: dict[str, list[tuple[str, str]]] = {
    "landscaping": [
        ("Walk the property", "We meet on site, listen to what you want, and measure up. No charge."),
        ("Get a written plan", "A drawn plan with plant list, timeline, and a fixed price before we start."),
        ("We build and maintain", "Our own crew installs it, then keeps it looking right season after season."),
    ],
    "auto": [
        ("Tell us the symptom", "Call or book online and describe what the car is doing. Drop it any morning."),
        ("We diagnose and quote", "You get the actual cause and a written estimate before anything is touched."),
        ("Approve, then we fix", "Nothing happens without your go-ahead. Most repairs are done the same day."),
    ],
    "barber": [
        ("Pick your barber", "Browse the team and book the one you like, or take the first chair open."),
        ("Book online or walk in", "Thirty seconds to reserve. Weekday walk-ins are usually no wait."),
        ("Sit down, look sharp", "Cut, fade, beard, or a hot-towel shave. Out the door on schedule."),
    ],
    "medical": [
        ("Call or request online", "New patients welcome. Tell us what is going on and how soon you need to be seen."),
        ("Get seen quickly", "Same-day slots for sick visits, plus early-morning and Saturday appointments."),
        ("Ongoing care plan", "One practice for the whole family, with your records and billing in one place."),
    ],
    "restaurant": [
        ("Reserve your table", "Book online in a few taps, or call us for larger parties and private events."),
        ("Eat what's in season", "The menu changes with what Michigan growers have. Ask about the night's specials."),
        ("Stay for a drink", "Local drafts and a short wine list at the bar, open later than the kitchen."),
    ],
    "default": [
        ("Tell us what you need", "Call or send a message and describe the job. We will ask the right questions."),
        ("Get a clear quote", "A fixed price in writing before any work begins, with no surprises later."),
        ("We do the work", "Scheduled around your week, done properly, and stood behind afterwards."),
    ],
}


# Below-fold photography. Six bands of text-on-dark read as a wireframe no
# matter how good the type is; a restaurant page needs food on it.
# Captions are written against what is actually IN each frame — an earlier
# pass captioned these blind and shipped "Patios and hardscape" over a flower
# path and "Cuts and fades" over an empty room.
_NICHE_GALLERY: dict[str, list[tuple[str, str]]] = {
    "landscaping": [
        ("photo-1585320806297-9794b3e4eeae", "Garden paths and borders"),
        ("photo-1416879595882-3373a0480b5b", "Planting and soil prep"),
        ("photo-1523348837708-15d4a09cfac2", "Seasonal planting"),
    ],
    "auto": [
        ("photo-1492144534655-ae79c964c9d7", "In the service bay"),
        ("photo-1487754180451-c456f719a1fc", "Oil and fluid service"),
        ("photo-1503376780353-7e6692767b70", "Back on the road"),
    ],
    "barber": [
        ("photo-1622286342621-4bd786c2447c", "Cuts and fades"),
        ("photo-1585747860715-2ba37e788b70", "The shop floor"),
        # Replaces a bright white salon interior that was both off-subject and
        # far brighter than the rest of the row.
        ("photo-1596728325488-58c87691e9af", "Beard trims and shaves"),
    ],
    "medical": [
        ("photo-1631217868264-e5b90bb7e133", "Primary care visits"),
        ("photo-1666214280557-f1b5022eb634", "On-site diagnostics"),
        ("photo-1629909613654-28e377c37b09", "Care for every age"),
    ],
    "restaurant": [
        ("photo-1414235077428-338989a2e8c0", "The dining room"),
        ("photo-1466978913421-dad2ebd01d17", "Seasonal plates"),
        ("photo-1504674900247-0877df9cc836", "From the kitchen"),
    ],
    "default": [
        ("photo-1497366754035-f200968a6e72", "Our workspace"),
        ("photo-1497366811353-6870744d04b2", "The team"),
        ("photo-1521737604893-d14cc237f11d", "On the job"),
    ],
}


def _gallery_html(niche_key: str, anim: str, *, animate: bool) -> str:
    shots = _NICHE_GALLERY.get(niche_key, _NICHE_GALLERY["default"])
    cards = "".join(
        f'<figure {_aos(animate, anim, i * 110)} '
        f'class="group relative overflow-hidden rounded-2xl border border-white/10">'
        f'<img src="https://images.unsplash.com/{pid}?auto=format&fit=crop&q=80&w=900&h=760" '
        f'alt="{caption}" loading="eager" '
        # Shared exposure so the row reads as one art-directed set rather than
        # three unrelated pictures at three different brightnesses.
        f'style="filter: brightness(0.88) contrast(1.06) saturate(0.94);" '
        f'class="h-72 w-full object-cover transition duration-500 group-hover:scale-105" />'
        f'<figcaption class="absolute inset-x-0 bottom-0 bg-gradient-to-t from-[#020817] '
        f'via-[#020817]/85 to-transparent px-5 pb-4 pt-14 text-[15px] font-semibold text-white">'
        f'{caption}</figcaption></figure>'
        for i, (pid, caption) in enumerate(shots)
    )
    return cards


def _step_card(index: int, title: str, body: str, anim: str, primary: str, *, animate: bool) -> str:
    return (
        f'<div {_aos(animate, anim, index * 110)} class="relative pl-14">'
        f'<span class="absolute left-0 top-0 grid h-10 w-10 place-items-center rounded-xl '
        f'text-base font-black text-[#020817]" style="background:{primary};">{index + 1}</span>'
        f'<h4 class="text-base font-bold text-slate-50">{title}</h4>'
        f'<p class="mt-1.5 text-sm leading-relaxed text-slate-300">{body}</p>'
        f'</div>'
    )


# ---------------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------------

def build_landing_html(
    business_name: str,
    niche: str = "local business",
    *,
    design_overrides: DesignOverrides | None = None,
    niche_style: NicheStyle | None = None,
    animate: bool = True,
    address: str = "",
    phone: str = "",
    hours: str = "",
    lat: float = 42.2808,
    lon: float = -83.7430,
) -> str:
    """
    Build a Lovable-quality Tailwind landing page:
    - Full-bleed Unsplash hero with glassmorphism card
    - AOS on every element (skipped when ``animate`` is False)
    - Grid-pattern dark background
    - Shimmer CTA buttons
    - "Prepared by" U-M ribbon

    Pass ``animate=False`` for anything that is screenshotted rather than
    scrolled; see :func:`_aos`.
    """
    title = _safe_fragment(business_name)
    niche_key = _normalize_niche(niche)
    copy_key = niche_key if niche_key in _COPY else "default"
    copy = _COPY[copy_key]

    # Resolve style from niche agent or fallback
    style = niche_style or _NICHE_FALLBACKS.get(niche_key, _NICHE_DEFAULT)

    hero_url = _hero_image_url(niche_key)
    hero_opacity = _NICHE_HERO_OPACITY.get(niche_key, _NICHE_HERO_OPACITY["default"])
    hero_alt = f"{niche_key.replace('-', ' ')} work in Ann Arbor"
    surface = _NICHE_SURFACE.get(niche_key, "dark")
    eyebrow = str(copy.get("eyebrow") or f"Ann Arbor · {style.tone}")
    cta_primary = str(copy.get("cta_primary") or "Get in touch →")
    cta_secondary = str(copy.get("cta_secondary") or "See our services")
    nav_services = str(copy.get("nav_services") or "Services")
    nav_work = str(copy.get("nav_work") or "Work")
    if surface == "light":
        page_bg, glass_bg, glass_border, grid_line = "#f4f7fb", "rgba(255,255,255,0.88)", "rgba(15,23,42,0.12)", "rgba(15,23,42,0.05)"
        hero_veil = "from-white/90 via-white/60 to-[#f4f7fb]"
        body_class = "theme-light min-h-screen text-slate-900 antialiased"
        nav_muted, nav_hover = "text-slate-700", "hover:text-slate-900"
        secondary_btn = "border border-slate-300/80 bg-white/80 text-slate-800 hover:bg-white"
        soft_text, muted_text = "text-slate-800", "text-slate-600"
        scroll_text = "text-slate-500"
        map_filter, map_tile_svc = "brightness(1.0)", "World_Light_Gray_Base"
        shimmer_fg = "#ffffff"
        mark_fg = "#ffffff"
    elif surface == "warm":
        page_bg, glass_bg, glass_border, grid_line = "#140e0c", "rgba(32,22,18,0.62)", "rgba(255,220,180,0.14)", "rgba(255,220,180,0.04)"
        hero_veil = "from-[#140e0c]/80 via-[#140e0c]/50 to-[#140e0c]"
        body_class = "theme-warm min-h-screen grid-bg text-stone-100 antialiased"
        nav_muted, nav_hover = "text-stone-300", "hover:text-white"
        secondary_btn = "border border-white/20 bg-white/5 text-stone-100 hover:bg-white/10"
        soft_text, muted_text = "text-stone-200", "text-stone-400"
        scroll_text = "text-stone-300"
        map_filter, map_tile_svc = "brightness(1.12) sepia(0.22) saturate(0.9)", "World_Dark_Gray_Base"
        shimmer_fg = "#140e0c"
        mark_fg = "#140e0c"
    else:
        page_bg, glass_bg, glass_border, grid_line = "#020817", "rgba(2, 8, 23, 0.55)", "rgba(255,255,255,0.13)", "rgba(255,255,255,0.03)"
        hero_veil = "from-[#020817]/75 via-[#020817]/45 to-[#020817]"
        body_class = "theme-dark min-h-screen grid-bg text-slate-100 antialiased"
        nav_muted, nav_hover = "text-slate-300", "hover:text-white"
        secondary_btn = "border border-white/25 bg-white/5 text-slate-200 hover:bg-white/10"
        soft_text, muted_text = "text-slate-200", "text-slate-400"
        scroll_text = "text-slate-300"
        map_filter, map_tile_svc = "brightness(1.18) contrast(1.05)", "World_Dark_Gray_Base"
        shimmer_fg = "#020817"
        mark_fg = "#020817"

    # AOS attribute bundles, resolved once so the template stays readable.
    a_hero = _aos(animate, style.animation_style, duration=900)
    a_up1 = _aos(animate, "fade-up", 100)
    a_up2 = _aos(animate, "fade-up", 200)
    a_up3 = _aos(animate, "fade-up", 300)
    a_up4 = _aos(animate, "fade-up", 400)
    a_up5 = _aos(animate, "fade-up", 500)
    a_up7 = _aos(animate, "fade-up", 700)
    a_up = _aos(animate, "fade-up")
    aos_head = (
        '<link rel="stylesheet" href="https://unpkg.com/aos@2.3.1/dist/aos.css" />'
        if animate
        else ""
    )
    aos_script = (
        '<script src="https://unpkg.com/aos@2.3.1/dist/aos.js"></script>\n'
        '    <script>AOS.init({ duration: 750, once: true, '
        'easing: "ease-out-quart", offset: 60 });</script>'
        if animate
        else ""
    )

    # Google-friendly font name → CSS @import name
    font_name = style.font_family
    font_css_name = font_name.replace(" ", "+")
    anim = style.animation_style
    primary = style.primary_color

    # Name / address / phone / hours. A local-business page without these is
    # missing the thing owners look for first, so they always render.
    nap_address = address.strip() or "Downtown Ann Arbor, MI 48104"
    nap_phone = phone.strip() or "(734) 555-0138"
    nap_hours = hours.strip() or "Mon–Fri 8am–6pm · Sat 9am–2pm"
    tel_href = "tel:" + re.sub(r"[^\d+]", "", nap_phone)
    initials = "".join(w[0] for w in re.findall(r"[A-Za-z]+", title)[:2]).upper() or "AA"

    static_map = _static_map_html(lat, lon, tile_svc=map_tile_svc, css_filter=map_filter)

    # Shared "SCROLL" chrome was reading as the same template on every niche.
    scroll_text = ""  # shared scroll chrome read as one template

    hero_html = render_hero(
        layout=layout_for(niche_key),
        hero_url=hero_url,
        hero_alt=hero_alt,
        hero_opacity=hero_opacity,
        hero_veil=hero_veil,
        a_hero=a_hero,
        a_up7=a_up7,
        scroll_text=scroll_text,
        primary=primary,
        eyebrow=eyebrow,
        title=title,
        headline=str(copy["headline"]),
        subheadline=str(copy["subheadline"]),
        cta_primary=cta_primary,
        cta_secondary=cta_secondary,
        soft_text=soft_text,
        muted_text=muted_text,
        secondary_btn=secondary_btn,
        a_up1=a_up1,
        a_up2=a_up2,
        a_up3=a_up3,
        a_up4=a_up4,
        a_up5=a_up5,
    )


    directions_link = (
        "https://www.google.com/maps/search/?api=1&query="
        + quote_plus(f"{title}, {nap_address}")
    )

    benefits: list[str] = list(copy.get("benefits", []))  # type: ignore[arg-type]
    testimonials: list[tuple[str, str]] = list(copy.get("testimonials", []))  # type: ignore[arg-type]

    def b(i: int) -> str:
        return benefits[i] if i < len(benefits) else ""

    def t_quote(i: int) -> str:
        return testimonials[i][0] if i < len(testimonials) else ""

    def t_attr(i: int) -> str:
        return testimonials[i][1] if i < len(testimonials) else ""

    steps_html = "".join(
        _step_card(i, s_title, s_body, anim, primary, animate=animate)
        for i, (s_title, s_body) in enumerate(_STEPS.get(niche_key, _STEPS["default"]))
    )
    gallery_html = _gallery_html(niche_key, anim, animate=animate)

    steps_grid = {
        "medical": "mt-9 flex flex-col gap-6",
        "restaurant": "mt-9 grid gap-8 md:grid-cols-3 md:divide-x md:divide-white/10",
        "barber": "mt-9 grid gap-8 md:grid-cols-3",
        "landscaping": "mt-9 grid gap-8 md:grid-cols-3",
        "auto": "mt-9 grid gap-6 md:grid-cols-3",
    }.get(niche_key, "mt-9 grid gap-8 md:grid-cols-3")
    gallery_grid = {
        "restaurant": "mt-9 grid gap-5 md:grid-cols-2",
        "medical": "mt-9 grid gap-5 sm:grid-cols-3",
        "landscaping": "mt-9 grid gap-4 md:grid-cols-3",
        "barber": "mt-9 columns-1 gap-5 md:columns-2 md:gap-6 [&>figure]:mb-5",
        "auto": "mt-9 grid gap-5 sm:grid-cols-2 md:grid-cols-3",
    }.get(niche_key, "mt-9 grid gap-5 sm:grid-cols-2 md:grid-cols-3")
    benefits_grid = {
        "medical": "mt-10 grid gap-4",
        "restaurant": "mt-10 grid gap-6 md:grid-cols-3",
        "landscaping": "mt-10 grid gap-6 md:grid-cols-3",
        "barber": "mt-10 flex flex-col gap-4 md:flex-row",
        "auto": "mt-10 grid gap-6 md:grid-cols-3",
    }.get(niche_key, "mt-10 grid gap-6 md:grid-cols-3")

    sec_benefits = f'''    <!-- ===== BENEFITS ===== -->
    <section id="services" class="mx-auto max-w-5xl px-6 pb-20 pt-14">
      <p {a_up} class="text-xs font-semibold uppercase tracking-[0.2em]" style="color:{primary};">What you get</p>
      <h3 {a_up1} class="font-brand mt-3 text-3xl font-bold md:text-4xl">
        {copy["section_h2"]}
      </h3>
      <div class="{benefits_grid}">
        {_benefit_card(b(0), anim, primary, delay=0, animate=animate)}
        {_benefit_card(b(1), anim, primary, delay=100, animate=animate)}
        {_benefit_card(b(2), anim, primary, delay=200, animate=animate)}
      </div>
    </section>

'''
    sec_gallery = f'''    <!-- ===== GALLERY ===== -->
    <section id="work" class="mx-auto max-w-5xl px-6 pb-20">
      <p {a_up} class="text-xs font-semibold uppercase tracking-[0.2em]" style="color:{primary};">{copy["gallery_kicker"]}</p>
      <h3 {a_up1} class="font-brand mt-3 text-3xl font-bold md:text-4xl">{copy["gallery_h2"]}</h3>
      <div class="{gallery_grid}">
        {gallery_html}
      </div>
    </section>

'''
    sec_how_it_works = f'''    <!-- ===== HOW IT WORKS ===== -->
    <section class="mx-auto max-w-5xl px-6 pb-20">
      <div class="glass rounded-2xl px-6 py-10 md:px-10">
        <p {a_up} class="text-xs font-semibold uppercase tracking-[0.2em]" style="color:{primary};">How it works</p>
        <h3 {a_up1} class="font-brand mt-3 text-3xl font-bold md:text-4xl">{copy["steps_h2"]}</h3>
        <div class="{steps_grid}">
          {steps_html}
        </div>
      </div>
    </section>

'''
    sec_testimonials = f'''    <!-- ===== TESTIMONIALS ===== -->
    <section id="reviews" {a_up} class="mx-auto max-w-5xl px-6 pb-20">
      <!-- These quotes are placeholders. The "replace with your reviews" note
           lives in the outreach email, not baked into the client's layout. -->
      <p class="mb-5 text-xs font-semibold uppercase tracking-[0.2em]" style="color:{primary};">What customers say</p>
      <div class="grid gap-6 md:grid-cols-2">
        {_testimonial_card(t_quote(0), t_attr(0), anim, delay=0, primary=primary, animate=animate)}
        {_testimonial_card(t_quote(1), t_attr(1), anim, delay=150, primary=primary, animate=animate)}
      </div>
    </section>

'''
    sec_map_location = f'''    <!-- ===== MAP + LOCATION ===== -->
    <section id="story" {a_up} class="glass mx-auto max-w-5xl rounded-2xl px-6 py-10 md:px-10 md:grid md:grid-cols-2 md:gap-8 mb-16 mx-6 lg:mx-auto">
      <div>
        <p class="text-xs font-semibold uppercase tracking-[0.2em]" style="color:{primary};">Visit us</p>
        <h3 class="font-brand mt-2 text-2xl font-bold">Find us in Ann Arbor</h3>
        <p class="mt-3 text-sm text-slate-300">{copy["find_us"]}</p>
        <dl class="mt-6 space-y-4 text-sm">
          <div class="flex items-start gap-3">
            <span class="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full" style="background:{primary};"></span>
            <div>
              <dt class="text-[11px] font-semibold uppercase tracking-wider text-slate-300">Address</dt>
              <dd class="mt-0.5 text-slate-100">{nap_address}</dd>
            </div>
          </div>
          <div class="flex items-start gap-3">
            <span class="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full" style="background:{primary};"></span>
            <div>
              <dt class="text-[11px] font-semibold uppercase tracking-wider text-slate-300">Phone</dt>
              <dd class="mt-0.5">
                <a href="{tel_href}" class="font-semibold text-slate-100 underline decoration-white/25 underline-offset-4 hover:decoration-white">{nap_phone}</a>
              </dd>
            </div>
          </div>
          <div class="flex items-start gap-3">
            <span class="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full" style="background:{primary};"></span>
            <div>
              <dt class="text-[11px] font-semibold uppercase tracking-wider text-slate-300">Hours</dt>
              <dd class="mt-0.5 text-slate-100">{nap_hours}</dd>
            </div>
          </div>
        </dl>
      </div>
      <div class="mt-6 md:mt-0">
        {static_map}
        <div class="mt-2.5 flex items-center justify-between gap-3">
          <a href="{directions_link}" class="text-sm font-semibold text-slate-100 underline decoration-white/30 underline-offset-4 hover:decoration-white">
            Get directions →
          </a>
          <span class="text-[11px] text-slate-400">© Esri · OpenStreetMap</span>
        </div>
      </div>
    </section>

'''
    sec_contact = f'''    <!-- ===== CONTACT ===== -->
    <section id="contact" {a_up} class="glass mx-6 mb-24 max-w-5xl rounded-2xl px-6 py-10 md:px-10 lg:mx-auto">
      <p class="text-xs font-semibold uppercase tracking-[0.2em]" style="color:{primary};">Get started</p>
      <h3 class="font-brand mt-2 text-2xl font-bold">Send us a message</h3>
      <p class="mt-2 text-sm text-slate-300">We reply within one business day — or call {nap_phone}.</p>
      <form action="https://formspree.io/f/mqabdqyv" method="POST" class="mt-6 grid gap-4 md:grid-cols-2">
        <input name="business" type="hidden" value="{title}" />
        <input name="niche"    type="hidden" value="{niche_key}" />
        <label class="md:col-span-1">
          <span class="mb-1 block text-xs font-semibold uppercase tracking-wide text-slate-300">Name</span>
          <input required name="name" type="text"
            class="w-full rounded-xl border border-white/25 bg-white/[0.12] px-4 py-2.5 text-slate-50 outline-none transition placeholder:text-slate-400 focus:border-white/60 focus:bg-white/20" />
        </label>
        <label class="md:col-span-1">
          <span class="mb-1 block text-xs font-semibold uppercase tracking-wide text-slate-300">Email</span>
          <input required name="email" type="email"
            class="w-full rounded-xl border border-white/25 bg-white/[0.12] px-4 py-2.5 text-slate-50 outline-none transition placeholder:text-slate-400 focus:border-white/60 focus:bg-white/20" />
        </label>
        <label class="md:col-span-2">
          <span class="mb-1 block text-xs font-semibold uppercase tracking-wide text-slate-300">Message</span>
          <textarea required name="message" rows="4"
            class="w-full rounded-xl border border-white/25 bg-white/[0.12] px-4 py-2.5 text-slate-50 outline-none transition placeholder:text-slate-400 focus:border-white/60 focus:bg-white/20"></textarea>
        </label>
        <button type="submit"
          class="shimmer-btn md:col-span-2 inline-flex items-center justify-center rounded-xl px-8 py-3 text-sm shadow-lg transition-all duration-200">
          Send Message →
        </button>
      </form>
    </section>

'''
    _order = {
        "restaurant": [sec_gallery, sec_benefits, sec_how_it_works, sec_testimonials, sec_map_location, sec_contact],
        "medical": [sec_how_it_works, sec_benefits, sec_testimonials, sec_gallery, sec_map_location, sec_contact],
        "barber": [sec_benefits, sec_gallery, sec_how_it_works, sec_testimonials, sec_map_location, sec_contact],
        "landscaping": [sec_gallery, sec_benefits, sec_how_it_works, sec_testimonials, sec_map_location, sec_contact],
        "auto": [sec_benefits, sec_how_it_works, sec_gallery, sec_testimonials, sec_map_location, sec_contact],
        "default": [sec_benefits, sec_gallery, sec_how_it_works, sec_testimonials, sec_map_location, sec_contact],
    }
    body_sections = "".join(_order.get(niche_key, _order["default"]))

    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link href="https://fonts.googleapis.com/css2?family={font_css_name}:wght@400;500;600;700;800&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet" />
    <script src="https://cdn.tailwindcss.com"></script>
    {aos_head}
    <title>{title} — Ann Arbor</title>
    <style>
      :root {{ --primary: {primary}; --page-bg: {page_bg}; --glass-bg: {glass_bg}; --glass-border: {glass_border}; }}
      body {{ font-family: 'Inter', sans-serif; color: inherit; }}
      html {{ scroll-behavior: smooth; scroll-padding-top: 84px; }}
      section[id] {{ scroll-margin-top: 84px; }}
      .font-brand {{ font-family: '{font_name}', serif; }}

      /* Grid pattern background */
      .grid-bg {{
        background-color: {page_bg};
        background-image:
          linear-gradient({grid_line} 1px, transparent 1px),
          linear-gradient(90deg, {grid_line} 1px, transparent 1px);
        background-size: 32px 32px;
      }}

      /* Glassmorphism */
      .glass {{
        background: {glass_bg};
        border: 1px solid {glass_border};
        backdrop-filter: blur(18px);
        -webkit-backdrop-filter: blur(18px);
      }}

      /* Hero glow behind title */
      .hero-glow {{
        position: absolute;
        inset: -40px;
        background: radial-gradient(ellipse at center,
          color-mix(in srgb, {primary} 38%, transparent),
          transparent 70%);
        filter: blur(32px);
        pointer-events: none;
        z-index: 0;
      }}

      /* Shimmer button */
      @keyframes shimmer {{
        0%   {{ background-position: -300% 0; }}
        100% {{ background-position:  300% 0; }}
      }}
      .shimmer-btn {{
        background: linear-gradient(
          110deg,
          {primary} 0%,
          color-mix(in srgb, {primary} 60%, white) 40%,
          {primary} 60%
        );
        background-size: 300% 100%;
        animation: shimmer 3s linear infinite;
        color: {shimmer_fg};
        font-weight: 700;
      }}
      .shimmer-btn:hover {{ filter: brightness(1.1); transform: scale(1.02); }}

      /* Hero gradient backdrop — visible if the photo fails to load. */
      .hero-fallback {{
        background:
          radial-gradient(ellipse at 25% 15%,
            color-mix(in srgb, {primary} 30%, transparent), transparent 55%),
          radial-gradient(ellipse at 75% 85%,
            color-mix(in srgb, {primary} 18%, transparent), transparent 60%),
          linear-gradient(160deg, color-mix(in srgb, {page_bg} 70%, {primary}) 0%, {page_bg} 60%);
      }}

      /* Balanced headline wraps — stops orphaned words in long business names. */
      .text-balance {{ text-wrap: balance; }}

      /* Light clinical surface — remaps dark-template utility classes. */
      body.theme-light .text-slate-50,
      body.theme-light .text-slate-100,
      body.theme-light .text-slate-200 {{ color: #0f172a !important; }}
      body.theme-light .text-slate-300 {{ color: #334155 !important; }}
      body.theme-light .text-slate-400,
      body.theme-light .text-slate-500 {{ color: #475569 !important; }}
      body.theme-light [class*='border-white'],
                  body.theme-light input,
      body.theme-light textarea {{
        background: #ffffff !important;
        border-color: #cbd5e1 !important;
        color: #0f172a !important;
      }}
      body.theme-light input:focus,
      body.theme-light textarea:focus {{
        border-color: color-mix(in srgb, var(--primary) 55%, #94a3b8) !important;
        background: #fff !important;
      }}
      body.theme-light footer {{
        background: #e8eef6 !important;
        border-color: rgba(15,23,42,0.1) !important;
        color: #0f172a;
      }}
      body.theme-light .glass {{
        background: rgba(255,255,255,0.92) !important;
        border-color: rgba(15,23,42,0.1) !important;
        box-shadow: 0 10px 30px rgba(15,23,42,0.06);
      }}

    </style>
  </head>
  <body class="{body_class}">

    <!-- ===== NAV ===== -->
    <header class="fixed inset-x-0 top-0 z-40 glass border-x-0 border-t-0">
      <nav class="mx-auto flex max-w-6xl items-center justify-between gap-4 px-6 py-3.5">
        <a href="#top" class="flex items-center gap-2.5">
          <span class="grid h-8 w-8 place-items-center rounded-lg text-sm font-black"
                style="background:{primary}; color:{mark_fg};">{initials}</span>
          <span class="font-brand text-base font-bold tracking-tight">{title}</span>
        </a>
        <div class="hidden items-center gap-7 text-sm font-medium {nav_muted} md:flex">
          <a href="#services" class="transition {nav_hover}">{nav_services}</a>
          <a href="#work" class="transition {nav_hover}">{nav_work}</a>
          <a href="#reviews" class="transition {nav_hover}">Reviews</a>
          <a href="#story" class="transition {nav_hover}">Visit</a>
          <a href="#contact" class="transition {nav_hover}">Contact</a>
        </div>
        <a href="{tel_href}"
           class="inline-flex items-center gap-2 rounded-xl px-4 py-2 text-sm font-bold shadow-lg transition hover:brightness-110"
           style="background:{primary}; color:{mark_fg};">
          <svg class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" d="M2.25 6.75c0 8.284 6.716 15 15 15h2.25a2.25 2.25 0 002.25-2.25v-1.372c0-.516-.351-.966-.852-1.091l-4.423-1.106c-.44-.11-.902.055-1.173.417l-.97 1.293c-.282.376-.769.542-1.21.38a12.035 12.035 0 01-7.143-7.143c-.162-.441.004-.928.38-1.21l1.293-.97c.363-.271.527-.734.417-1.173L6.963 3.102a1.125 1.125 0 00-1.091-.852H4.5A2.25 2.25 0 002.25 4.5v2.25z"/>
          </svg>
          <span class="hidden sm:inline">{nap_phone}</span>
          <span class="sm:hidden">Call</span>
        </a>
      </nav>
    </header>

    {hero_html}

    {body_sections}

    <!-- ===== FOOTER ===== -->
    <footer class="border-t border-white/10 bg-[#020817]/60">
      <div class="mx-auto grid max-w-5xl gap-10 px-6 py-14 md:grid-cols-4">
        <div class="md:col-span-2">
          <div class="flex items-center gap-2.5">
            <span class="grid h-9 w-9 place-items-center rounded-lg text-sm font-black text-[#020817]"
                  style="background:{primary};">{initials}</span>
            <span class="font-brand text-lg font-bold">{title}</span>
          </div>
          <p class="mt-4 max-w-sm text-sm leading-relaxed text-slate-400">{copy["subheadline"]}</p>
          <a href="{tel_href}" class="mt-5 inline-flex items-center gap-2 rounded-xl px-5 py-2.5 text-sm font-bold text-[#020817] transition hover:brightness-110" style="background:{primary};">
            Call {nap_phone}
          </a>
        </div>
        <div>
          <p class="text-xs font-semibold uppercase tracking-[0.16em]" style="color:{primary};">Services</p>
          <ul class="mt-4 space-y-2.5 text-sm text-slate-300">
            <li>{b(0)}</li>
            <li>{b(1)}</li>
            <li>{b(2)}</li>
          </ul>
        </div>
        <div>
          <p class="text-xs font-semibold uppercase tracking-[0.16em]" style="color:{primary};">Visit</p>
          <address class="mt-4 space-y-2.5 text-sm not-italic text-slate-300">
            <div>{nap_address}</div>
            <div>{nap_hours}</div>
            <div><a href="{tel_href}" class="underline decoration-white/25 underline-offset-4 hover:decoration-white">{nap_phone}</a></div>
          </address>
        </div>
      </div>
      <div class="border-t border-white/10 px-6 py-6">
        <div class="mx-auto flex max-w-5xl flex-wrap items-center justify-between gap-3 text-sm {muted_text}">
          <span>© {title} · Ann Arbor, Michigan</span>
          <a href="{tel_href}" class="font-semibold underline decoration-white/20 underline-offset-4 hover:opacity-80">{nap_phone}</a>
        </div>
      </div>
    </footer>

    {aos_script}
  </body>
</html>"""


def _benefit_card(text: str, anim: str, primary: str, *, delay: int, animate: bool = True) -> str:
    if not text:
        return ""
    return (
        f'<div {_aos(animate, anim, delay)} '
        f'class="glass rounded-2xl p-6">'
        f'<div class="mb-3 h-1 w-8 rounded-full" style="background:{primary};"></div>'
        f'<p class="text-sm text-slate-200 leading-relaxed">{text}</p>'
        f'</div>'
    )


def _testimonial_card(
    quote: str, attribution: str, anim: str, *, delay: int, primary: str = "#8b5cf6", animate: bool = True
) -> str:
    if not quote:
        return ""
    # Stars only — no platform badge. These quotes are placeholders, and
    # stamping a Google or Yelp mark on invented copy would be a lie.
    stars = "".join(
        f'<svg class="h-4 w-4" viewBox="0 0 20 20" fill="{primary}">'
        f'<path d="M10 1.5l2.6 5.3 5.9.9-4.2 4.1 1 5.8-5.3-2.8-5.3 2.8 1-5.8L1.5 7.7l5.9-.9z"/></svg>'
        for _ in range(5)
    )
    return (
        f'<blockquote {_aos(animate, anim, delay)} '
        f'class="glass rounded-2xl p-6">'
        f'<div class="mb-3 flex items-center gap-1">{stars}</div>'
        f'<p class="text-[15px] italic leading-relaxed text-slate-100">{quote}</p>'
        f'<footer class="mt-4 text-[13px] font-medium text-slate-400">{attribution}</footer>'
        f'</blockquote>'
    )


# ---------------------------------------------------------------------------
# Screenshot runner
# ---------------------------------------------------------------------------

def screenshot_landing_page(
    business_name: str,
    *,
    niche: str = "local business",
    design_overrides: DesignOverrides | None = None,
    use_niche_agent: bool = True,
    out_dir: Path | None = None,
    full_page: bool = True,
) -> Path:
    """
    1. Optionally call GPT-4o niche agent for style config.
    2. Render HTML in headless Chromium.
    3. Save PNG and index.html to the business project folder.
    """
    project_dir = project_folder_for_business(business_name)
    target = out_dir or (project_dir / "assets")
    target.mkdir(parents=True, exist_ok=True)
    suffix = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6))
    safe = re.sub(r"[^\w\-]+", "-", (business_name or "page")[:40]).strip("-") or "page"
    out_path = target / f"landing_{safe}_{suffix}.png"

    niche_style: NicheStyle | None = None
    if use_niche_agent:
        try:
            niche_style = call_niche_agent(business_name, niche)
        except Exception:
            niche_style = None

    common = {
        "niche": niche,
        "design_overrides": design_overrides,
        "niche_style": niche_style,
    }
    # The saved page keeps its scroll animations; the screenshot does not, or
    # every section below the fold would capture at opacity 0.
    (project_dir / "index.html").write_text(
        build_landing_html(business_name, animate=True, **common), encoding="utf-8"
    )
    static_html = build_landing_html(business_name, animate=False, **common)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.set_content(static_html, wait_until="load")
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(1200)
            page.screenshot(path=str(out_path), full_page=full_page)
        finally:
            browser.close()
    return out_path
