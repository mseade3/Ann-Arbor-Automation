"""Per-niche hero compositions — structure, not just accent colour."""

from __future__ import annotations

_NICHE_HERO_LAYOUT: dict[str, str] = {
    "landscaping": "split",
    "auto": "industrial",
    "barber": "split",
    "medical": "panel",
    "restaurant": "bottom",
    "default": "center",
}


def layout_for(niche_key: str) -> str:
    return _NICHE_HERO_LAYOUT.get(niche_key, "center")


def _copy_block(
    *,
    eyebrow: str,
    title: str,
    headline: str,
    subheadline: str,
    cta_primary: str,
    cta_secondary: str,
    primary: str,
    soft_text: str,
    muted_text: str,
    secondary_btn: str,
    a_up1: str,
    a_up2: str,
    a_up3: str,
    a_up4: str,
    a_up5: str,
    align: str = "center",
    max_w: str = "max-w-xl",
) -> str:
    text_align = "text-left" if align == "left" else "text-center"
    justify = "justify-start" if align == "left" else "justify-center"
    mx = "" if align == "left" else "mx-auto"
    return f"""
        <div class="{text_align}">
          <p {a_up1} class="relative z-10 text-xs font-semibold uppercase tracking-[0.25em]" style="color:{primary};">{eyebrow}</p>
          <h1 {a_up2} class="font-brand text-balance relative z-10 mt-3 text-4xl font-bold leading-[1.05] md:text-6xl">{title}</h1>
          <h2 {a_up3} class="text-balance relative z-10 mt-4 text-lg font-medium {soft_text} md:text-2xl">{headline}</h2>
          <p {a_up4} class="text-balance relative z-10 mt-3 {max_w} text-base leading-relaxed {muted_text} {mx}">{subheadline}</p>
          <div {a_up5} class="relative z-10 mt-8 flex flex-wrap items-center gap-3 {justify}">
            <a class="shimmer-btn inline-flex items-center justify-center rounded-xl px-7 py-3.5 text-sm shadow-lg transition-all duration-200" href="#contact">{cta_primary}</a>
            <a class="inline-flex items-center justify-center rounded-xl px-6 py-3.5 text-sm font-medium transition {secondary_btn}" href="#services">{cta_secondary}</a>
          </div>
        </div>
"""


def _scroll(a_up7: str, scroll_text: str) -> str:
    if not (scroll_text or "").strip():
        return ""
    return f"""
      <div {a_up7} class="absolute bottom-5 left-1/2 z-20 -translate-x-1/2">
        <div class="flex flex-col items-center gap-1.5 {scroll_text} opacity-80">
          <span class="text-[12px] font-medium uppercase tracking-widest">Scroll</span>
          <svg class="h-4 w-4 animate-bounce" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" d="M19 9l-7 7-7-7"/>
          </svg>
        </div>
      </div>"""


def _bg(hero_url: str, hero_alt: str, hero_opacity: str, hero_veil: str) -> str:
    return f"""
      <div class="absolute inset-0 -z-10 hero-fallback">
        <img src="{hero_url}" alt="{hero_alt}" class="h-full w-full object-cover" style="opacity: {hero_opacity};" loading="eager" />
        <div class="absolute inset-0 bg-gradient-to-b {hero_veil}"></div>
      </div>"""


def render_hero(
    *,
    layout: str,
    hero_url: str,
    hero_alt: str,
    hero_opacity: str,
    hero_veil: str,
    a_hero: str,
    a_up7: str,
    scroll_text: str,
    primary: str,
    eyebrow: str,
    title: str,
    headline: str,
    subheadline: str,
    cta_primary: str,
    cta_secondary: str,
    soft_text: str,
    muted_text: str,
    secondary_btn: str,
    a_up1: str,
    a_up2: str,
    a_up3: str,
    a_up4: str,
    a_up5: str,
) -> str:
    kw = dict(
        eyebrow=eyebrow,
        title=title,
        headline=headline,
        subheadline=subheadline,
        cta_primary=cta_primary,
        cta_secondary=cta_secondary,
        primary=primary,
        soft_text=soft_text,
        muted_text=muted_text,
        secondary_btn=secondary_btn,
        a_up1=a_up1,
        a_up2=a_up2,
        a_up3=a_up3,
        a_up4=a_up4,
        a_up5=a_up5,
    )
    scroll = _scroll(a_up7, scroll_text)
    bg = _bg(hero_url, hero_alt, hero_opacity, hero_veil)

    if layout == "split":
        body = _copy_block(align="left", max_w="max-w-md", **kw)
        return f"""
    <section id="top" class="relative min-h-screen overflow-hidden pt-16">
      <div class="mx-auto grid min-h-[calc(100vh-4rem)] max-w-6xl md:grid-cols-2">
        <div {a_hero} class="flex flex-col justify-center px-6 py-16 md:px-10 lg:px-14">{body}</div>
        <div class="relative min-h-[52vh] md:min-h-full">
          <img src="{hero_url}" alt="{hero_alt}" class="absolute inset-0 h-full w-full object-cover" loading="eager" />
          <div class="absolute inset-0 bg-gradient-to-r from-[var(--page-bg)] via-[var(--page-bg)]/40 to-transparent"></div>
        </div>
      </div>
      {scroll}
    </section>"""

    if layout == "bleed":
        body = _copy_block(align="left", max_w="max-w-lg", **kw)
        return f"""
    <section id="top" class="relative flex min-h-screen items-end overflow-hidden pt-16 md:items-center">
      {bg}
      <div class="absolute inset-0 bg-gradient-to-r from-[var(--page-bg)] via-[var(--page-bg)]/80 to-transparent"></div>
      <div {a_hero} class="relative z-10 mx-auto w-full max-w-6xl px-6 pb-24 pt-24 md:px-10 md:pb-28">
        <div class="max-w-2xl border-l-4 pl-6 md:pl-8" style="border-color:{primary};">{body}</div>
      </div>
      {scroll}
    </section>"""

    if layout == "bottom":
        body = _copy_block(align="left", max_w="max-w-xl", **kw)
        return f"""
    <section id="top" class="relative flex min-h-screen items-end overflow-hidden pt-16">
      {bg}
      <div class="absolute inset-0 bg-gradient-to-t from-[var(--page-bg)] via-[var(--page-bg)]/60 to-transparent"></div>
      <div {a_hero} class="relative z-10 mx-auto w-full max-w-6xl px-6 pb-24 md:px-10">
        <div class="max-w-2xl">{body}</div>
      </div>
      {scroll}
    </section>"""

    if layout == "panel":
        body = _copy_block(align="left", max_w="max-w-md", **kw)
        return f"""
    <section id="top" class="relative min-h-screen overflow-hidden pt-16">
      <div class="mx-auto grid min-h-[calc(100vh-4rem)] max-w-6xl items-stretch md:grid-cols-5">
        <div {a_hero} class="glass relative z-10 flex flex-col justify-center px-6 py-16 md:col-span-2 md:px-10 lg:rounded-r-3xl">{body}</div>
        <div class="relative min-h-[44vh] md:col-span-3 md:min-h-full">
          <img src="{hero_url}" alt="{hero_alt}" class="absolute inset-0 h-full w-full object-cover" loading="eager" />
        </div>
      </div>
      {scroll}
    </section>"""

    if layout == "industrial":
        body = _copy_block(align="left", max_w="max-w-lg", **kw)
        return f"""
    <section id="top" class="relative flex min-h-screen items-center overflow-hidden pt-16">
      {bg}
      <div class="absolute inset-0 bg-gradient-to-r from-[var(--page-bg)] via-[var(--page-bg)]/85 to-transparent"></div>
      <div {a_hero} class="relative z-10 mx-auto grid w-full max-w-6xl gap-10 px-6 py-24 md:grid-cols-[1.15fr_0.85fr] md:px-10">
        <div>
          <div class="mb-5 h-1.5 w-16 rounded-full" style="background:{primary};"></div>
          {body}
        </div>
        <div class="hidden md:flex md:items-end md:justify-end">
          <div class="glass max-w-sm rounded-2xl p-5 text-left text-sm {muted_text}">
            <p class="text-xs font-semibold uppercase tracking-[0.2em]" style="color:{primary};">Same-week slots</p>
            <p class="mt-2 leading-relaxed">Drop off any morning. Written estimate before we touch the car. Most repairs done the same day.</p>
          </div>
        </div>
      </div>
      {scroll}
    </section>"""

    body = _copy_block(align="center", **kw)
    return f"""
    <section id="top" class="relative flex min-h-screen flex-col items-center justify-center overflow-hidden pt-16">
      {bg}
      <div {a_hero} class="glass relative mx-4 max-w-3xl rounded-3xl px-8 py-12 text-center shadow-2xl md:px-14">
        <div class="hero-glow"></div>
        {body}
      </div>
      {scroll}
    </section>"""
