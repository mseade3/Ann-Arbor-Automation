"""
OpenAI-based outreach: U–M CS student persona with injected template variables.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from openai import OpenAI

from aade.config import (
    OPENAI_API_KEY,
    OUTREACH_MODEL,
    get_design_brief_models,
    get_outreach_models,
)

SYSTEM_PERSONA = """You are drafting outreach on behalf of a real University of Michigan
undergraduate studying Computer Science. You are grounded, specific, and community-minded—
not salesy, not robotic. You want to build genuine local relationships and use your skills
to help Ann Arbor small businesses, starting with a simple win (a clean one-page web presence
or a clearer story online). The tone is warm, competent, and humble. You never fabricate
having met the person or having visited the business. Keep the message under 200 words."""


USER_TEMPLATE = """Write one outreach message (email body or DM) using these facts:

- Business name: {Business_Name}
- Street / area line for personalization: {Street_Name}
- Niche: {Niche}

Include the business's context naturally. You may use Markdown with light formatting (e.g. one short list).
Do not include a subject line unless asked; default is body-only."""

DESIGN_SYSTEM = """You are a web style strategist for local business landing pages.
Return only valid JSON with keys:
- color_theme: short theme token (examples: emerald-earth, slate-sharp, rose-warm, blue-clean)
- font_family: short token (examples: serif, sans, mono)
- hero_image_prompt: short Unsplash-friendly phrase for the niche and city.
No markdown, no extra keys, no commentary."""


@dataclass(frozen=True, slots=True)
class DesignBrief:
    color_theme: str
    font_family: str
    hero_image_prompt: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "color_theme": self.color_theme,
                "font_family": self.font_family,
                "hero_image_prompt": self.hero_image_prompt,
            },
            ensure_ascii=True,
        )


def generate_outreach(
    business_name: str,
    street_name: str,
    niche: str,
    *,
    model: str | None = None,
) -> str:
    """
    Call configured OpenAI chat models to produce a personalized message.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set. Add it to your .env file.")

    client = OpenAI(api_key=OPENAI_API_KEY)
    user = USER_TEMPLATE.format(
        Business_Name=business_name,
        Street_Name=street_name,
        Niche=niche,
    )
    candidates = [model] if model else list(get_outreach_models() or (OUTREACH_MODEL,))
    last_error: Exception | None = None
    for model_name in candidates:
        try:
            resp = client.chat.completions.create(
                model=model_name,
                temperature=0.75,
                messages=[
                    {"role": "system", "content": SYSTEM_PERSONA},
                    {"role": "user", "content": user},
                ],
            )
            text = (resp.choices[0].message.content or "").strip()
            if text:
                return text
            last_error = RuntimeError(f"Empty response from model {model_name}.")
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(f"All outreach models failed. Last error: {last_error}")


def generate_design_brief(
    business_name: str,
    niche: str,
    *,
    business_description: str | None = None,
    mission_statement: str | None = None,
    model: str | None = None,
) -> DesignBrief:
    """
    Ask configured OpenAI chat models for JSON style controls.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set. Add it to your .env file.")
    client = OpenAI(api_key=OPENAI_API_KEY)
    user = (
        "Business context:\n"
        f"- Name: {business_name}\n"
        f"- Niche: {niche}\n"
        f"- Description: {(business_description or 'not provided')}\n"
        f"- Mission statement: {(mission_statement or 'not provided')}\n"
        "- City: Ann Arbor, Michigan\n\n"
        "Choose visual style based on the specific business context, not generic niche stereotypes."
        " Produce JSON now."
    )
    candidates = [model] if model else list(get_design_brief_models())
    last_error: Exception | None = None
    for model_name in candidates:
        try:
            resp = client.chat.completions.create(
                model=model_name,
                temperature=0.2,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": DESIGN_SYSTEM},
                    {"role": "user", "content": user},
                ],
            )
            raw = (resp.choices[0].message.content or "").strip()
            if not raw:
                last_error = RuntimeError(f"Empty response from model {model_name}.")
                continue
            data = json.loads(raw)
            return DesignBrief(
                color_theme=str(data.get("color_theme") or "emerald-earth"),
                font_family=str(data.get("font_family") or "sans"),
                hero_image_prompt=str(
                    data.get("hero_image_prompt") or f"{niche} Ann Arbor storefront"
                ),
            )
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(f"All design-brief models failed. Last error: {last_error}")


def apply_variables(
    text: str,
    *,
    business_name: str,
    street_name: str,
    niche: str,
) -> str:
    """
    Post-process: replace any literal placeholders if the model echoed them.
    """
    return (
        text.replace("{Business_Name}", business_name)
        .replace("{Street_Name}", street_name)
        .replace("{Niche}", niche)
    )
