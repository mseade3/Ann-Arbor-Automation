"""Prompt-generation helpers for dashboard intelligence workflows."""

from __future__ import annotations

from typing import Any, Mapping


_DESIGN_SYSTEM_BY_NICHE: dict[str, dict[str, str]] = {
    "auto": {
        "name": "Rugged Industrial",
        "palette": "slate-900, zinc-700, metallic silver accents, warning amber CTA highlights",
        "visual_language": "bold typography, high-contrast cards, mechanical iconography",
    },
    "landscaping": {
        "name": "Premium Organic",
        "palette": "emerald-700, stone-900, moss accents, warm neutral backgrounds",
        "visual_language": "earthy texture cues, confident serif headers, clean whitespace",
    },
    "barber": {
        "name": "Sharp Modern Classic",
        "palette": "neutral-950, amber-500, graphite accents, off-white body surfaces",
        "visual_language": "editorial hero, crisp dividers, premium studio tone",
    },
    "medical": {
        "name": "Clinical Trust",
        "palette": "sky-700, slate-900, cool gray, sterile white content zones",
        "visual_language": "clear hierarchy, reassuring spacing, compliance-friendly tone",
    },
    "restaurant": {
        "name": "Sensory Luxe",
        "palette": "rose-700, neutral-950, warm gold accents, soft ivory text surfaces",
        "visual_language": "cinematic imagery blocks, elegant headlines, appetite-focused CTA",
    },
}

_DEFAULT_DESIGN_SYSTEM = {
    "name": "Modern Local Authority",
    "palette": "navy-900, slate-800, subtle accent gradients, bright action color",
    "visual_language": "clear conversion hierarchy, practical premium styling",
}


def _niche_key(raw_niche: str) -> str:
    value = (raw_niche or "").strip().lower()
    aliases = {
        "landscape": "landscaping",
        "landscaper": "landscaping",
        "car": "auto",
        "automotive": "auto",
        "auto repair": "auto",
        "barbershop": "barber",
        "food": "restaurant",
        "cafe": "restaurant",
        "doctor": "medical",
        "clinic": "medical",
        "dental": "medical",
        "dentist": "medical",
    }
    return aliases.get(value, value) or "default"


def _rating_value(selected: Mapping[str, Any]) -> str:
    rating = (
        selected.get("google_rating")
        or selected.get("rating")
        or selected.get("stars")
        or "4.8"
    )
    count = (
        selected.get("google_review_count")
        or selected.get("review_count")
        or selected.get("ratings_total")
        or "87"
    )
    return f"{rating} stars from {count} Google reviews"


def generate_lovable_prompt(selected: Mapping[str, Any]) -> str:
    """Return a structured XML-style prompt for Lovable/V0."""
    business = str(selected.get("business_name") or "Local Business")
    niche = str(selected.get("niche") or "Local Service Business")
    address = str(selected.get("address") or "Ann Arbor, MI")
    phone = str(selected.get("phone") or selected.get("telephone") or "N/A")
    rating_text = _rating_value(selected)

    design = _DESIGN_SYSTEM_BY_NICHE.get(_niche_key(niche), _DEFAULT_DESIGN_SYSTEM)

    return f"""<lovable_prompt version="2.0">
  <project>
    <city>Ann Arbor, MI</city>
    <business_name>{business}</business_name>
    <niche>{niche}</niche>
    <address>{address}</address>
    <phone>{phone}</phone>
  </project>

  <objective>
    Build a premium, conversion-first marketing site that feels trustworthy,
    modern, and mobile-native for local customers.
  </objective>

  <design_system>
    <aesthetic>{design["name"]}</aesthetic>
    <color_palette>{design["palette"]}</color_palette>
    <visual_language>{design["visual_language"]}</visual_language>
    <typography>Inter for body, bold display treatment for hero headlines</typography>
    <ui_principles>
      glassmorphism cards, strong contrast, 8px spacing rhythm, fast scanning hierarchy
    </ui_principles>
  </design_system>

  <conversion_architecture>
    <primary_cta>Get a Quote</primary_cta>
    <cta_rules>
      Put "Get a Quote" above the fold, in sticky nav, and at end of every major section.
    </cta_rules>
    <trust_section>
      Feature real social proof prominently:
      "{rating_text}" for {business}.
      Include review stars, rating count, and customer quote card styling.
    </trust_section>
    <lead_capture>
      Keep form friction low: Name, Phone, Email, Service Needed, Preferred Time.
    </lead_capture>
  </conversion_architecture>

  <required_sections order="strict">
    Hero, Services, Trust Section, Why Choose Us, Quote Form, Service Area Map, FAQ, Final CTA
  </required_sections>

  <implementation_rules>
    Tailwind CSS CDN, responsive-first, no Lorem Ipsum, production-ready index.html output.
  </implementation_rules>
</lovable_prompt>"""

