"""
Scrape → SQLite upsert → optional outreach + visual hook, respecting 90-day cooldown.
"""

from __future__ import annotations

from dataclasses import dataclass
from aade import database as db
from aade.outreach import (
    DesignBrief,
    apply_variables,
    generate_design_brief,
    generate_outreach,
)
from aade.scraper import fetch_leads
from aade.scraper_places import RawLead
from aade.visual_hook import (
    DesignOverrides,
    generate_premium_mockup,
    premium_vibe_for_niche,
    screenshot_landing_page,
)


@dataclass(frozen=True, slots=True)
class ScrapeResult:
    """Summary of a scrape run (batch statistics)."""

    new_rows: int
    batch_missing_website: int
    batch_size: int


def run_scrape(
    *,
    query: str = "small businesses in Ann Arbor MI",
    max_results: int = 20,
) -> ScrapeResult:
    """
    Pull leads, upsert into SQLite, count those without a website.
    """
    raw = fetch_leads(query=query, max_results=max_results)
    new_rows = 0
    no_web = 0
    n_raw = len(raw)
    with db.connect() as conn:
        for r in raw:
            before = conn.execute(
                "SELECT 1 FROM leads WHERE place_id = ?",
                (r.place_id,),
            ).fetchone()
            db.upsert_lead(
                conn,
                place_id=r.place_id,
                business_name=r.business_name,
                address=r.address,
                phone=r.phone,
                website=r.website,
                niche=r.niche,
                business_description=r.business_description,
                mission_statement=r.mission_statement,
                street_name=r.street_name,
                maps_url=r.maps_url,
                rating=getattr(r, "rating", None),
                review_count=getattr(r, "review_count", None),
            )
            if r.website in (None, ""):
                no_web += 1
            if before is None:
                new_rows += 1
    return ScrapeResult(
        new_rows=new_rows,
        batch_missing_website=no_web,
        batch_size=n_raw,
    )


@dataclass(frozen=True, slots=True)
class OutreachBatchResult:
    processed: int
    skipped_cooldown: int
    skipped_has_website: int
    paths_screenshot: list[str]


def run_outreach_batch(
    *,
    limit: int = 5,
    with_screenshots: bool = True,
    use_premium_mockup: bool = True,
) -> OutreachBatchResult:
    """
    For leads without a site, if cooldown allows, generate text and mark contacted.
    """
    paths: list[str] = []
    processed = 0
    skipped_cool = 0
    skipped_web = 0
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM leads
            WHERE no_website_flag = 1
            ORDER BY updated_at ASC
            LIMIT 500
            """,
        ).fetchall()
        for row in rows:
            if processed >= limit:
                break
            d = dict(row)
            if d.get("has_website"):
                skipped_web += 1
                continue
            place_id = str(d["place_id"])
            if not db.can_contact_again(conn, place_id):
                skipped_cool += 1
                continue
            name = str(d["business_name"])
            street = d.get("street_name") or ""
            niche = d.get("niche") or "local business"
            text = generate_outreach(
                name,
                str(street),
                str(niche),
            )
            text = apply_variables(
                text,
                business_name=name,
                street_name=str(street),
                niche=str(niche),
            )
            if with_screenshots:
                try:
                    brief: DesignBrief = generate_design_brief(
                        name,
                        str(niche),
                        business_description=d.get("business_description"),
                        mission_statement=d.get("mission_statement"),
                    )
                except Exception:
                    # Keep pipeline resilient: fall back to niche-driven defaults.
                    brief = DesignBrief(
                        color_theme="emerald-earth" if str(niche).lower().startswith("land") else "slate-sharp",
                        font_family="serif" if "land" in str(niche).lower() else "sans",
                        hero_image_prompt=f"{niche} business in Ann Arbor",
                    )
                db.save_design_brief(
                    conn,
                    place_id,
                    color_theme=brief.color_theme,
                    font_family=brief.font_family,
                    hero_image_prompt=brief.hero_image_prompt,
                    brief_json=brief.to_json(),
                )
                rendered: str | None = None
                if use_premium_mockup:
                    try:
                        rendered = generate_premium_mockup(
                            name,
                            str(niche),
                            premium_vibe_for_niche(str(niche), brief.color_theme),
                        )
                    except Exception:
                        rendered = None
                if rendered is None:
                    p = screenshot_landing_page(
                        name,
                        niche=str(niche),
                        design_overrides=DesignOverrides(
                            color_theme=brief.color_theme,
                            font_family=brief.font_family,
                            hero_image_prompt=brief.hero_image_prompt,
                        ),
                    )
                    rendered = str(p)
                paths.append(rendered)
            db.mark_contacted(conn, place_id, text)
            processed += 1
    return OutreachBatchResult(
        processed=processed,
        skipped_cooldown=skipped_cool,
        skipped_has_website=skipped_web,
        paths_screenshot=paths,
    )
