"""Lead intelligence — Ann Arbor 3D Growth Engine."""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

import pydeck as pdk
import requests
import streamlit as st

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aade import database as db
from aade.config import (
    AGENT_FRANK_EMAIL_WEBHOOK,
    CLAY_ENRICH_WEBHOOK,
    DB_PATH,
    ELEVENX_CALL_WEBHOOK,
    GOOGLE_EMBED_API_KEY,
    GOOGLE_PLACES_API_KEY,
    MAPBOX_API_KEY,
    WATERFALL_WEBHOOK,
    get_design_brief_models,
    get_image_model,
    get_niche_style_models,
    get_outreach_models,
)
from aade.intelligence import generate_lovable_prompt
from aade.ops_automation import (
    process_due_sequence_emails,
    retry_call_step,
    retry_email_step1,
    retry_enrichment_step,
    retry_mockup_step,
    run_interested_email_only,
    run_verified_to_active_flow,
    snooze_sequence,
)
from aade.visual_hook import (
    DesignOverrides,
    generate_premium_mockup,
    premium_vibe_for_niche,
    screenshot_landing_page,
)

D_MRR = "projected_mrr"
DB_KEY_CONVERSION = "conversion_rate_pct"
DB_KEY_COST_PER_CONTACT = "cost_per_contact"
ANN_ARBOR_LAT = 42.2808
ANN_ARBOR_LON = -83.7430
DIAG_LAT = 42.27618
DIAG_LON = -83.73822
PROJECTS_DIR = _ROOT / "projects"
GOOGLE_STREET_VIEW_KEY = GOOGLE_EMBED_API_KEY or GOOGLE_PLACES_API_KEY
UMICH_BLUE = "#00274C"
UMICH_MAIZE = "#FFCB05"
MAX_SCRAPE_RADIUS_MILES = 28.0
MAX_MAP_ROWS = 90
STATUS_CHOICES = ["new", "verified", "active", "interested", "contacted"]
MIN_CAM_PITCH = 8.0
MAX_CAM_PITCH = 85.0
STREET_VIEW_THRESHOLD = 18.5
SPIRAL_ZOOM_MIN = 13.0
SPIRAL_ZOOM_MAX = 19.5
RECORDING_SNAP_SECONDS = 5.0
# Fixed cadence keeps tween chaining predictable; motion itself is time-based.
EXPLORE_TICK_SEC = 0.20
EXPLORE_TWEEN_OVERLAP = 1.15
# Scale 4 sent columns 1.6km up and out of frame; scale 1 flattened them into
# uniform stubs. Scale 2 over this range keeps the tallest near the upper third
# while leaving real variance between leads.
COLUMN_ELEVATION_MIN = 35
COLUMN_ELEVATION_MAX = 70
COLUMN_ELEVATION_SCALE = 1.6
MANUAL_NUDGE_MS = 350
RESET_GLIDE_MS = 700
FOCUS_FLY_MS = 900
EXPLORE_PRESETS: dict[str, dict[str, float]] = {
    "Drone Fast": {
        "speed_sec": 7.0,
        "pitch_amp": 9.0,
        "zoom_amp": 0.11,
    },
    "LinkedIn Smooth": {
        "speed_sec": 18.0,
        "pitch_amp": 7.5,
        "zoom_amp": 0.08,
    },
    "LinkedIn Cinematic": {
        "speed_sec": 14.0,
        "pitch_amp": 0.0,
        "zoom_amp": 0.0,
    },
    "Survey Slow": {
        "speed_sec": 42.0,
        "pitch_amp": 5.5,
        "zoom_amp": 0.05,
    },
}

_CAM_DEFAULTS: dict[str, object] = {
    "lat": DIAG_LAT,
    "lon": DIAG_LON,
    "pitch": 46.0,
    "bearing": 0.0,
    "zoom": 14.0,
    "map_style": "satellite",
}

_NICHE_COLORS: dict[str, str] = {
    "landscaping": "#10b981",
    "auto":        "#3b82f6",
    "barber":      "#f59e0b",
    "medical":     "#0ea5e9",
    "restaurant":  "#f43f5e",
    "other":       "#8b5cf6",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slug(value: str) -> str:
    return "-".join((value or "business").strip().lower().split()) or "business"


def _project_folder(business_name: str) -> Path:
    return PROJECTS_DIR / _slug(business_name)


def _niche_key(niche: str) -> str:
    v = (niche or "").strip().lower()
    aliases = {
        "landscape": "landscaping", "landscaper": "landscaping",
        "car": "auto", "automotive": "auto",
        "barbershop": "barber",
        "food": "restaurant", "cafe": "restaurant",
        "doctor": "medical", "clinic": "medical",
        "dental": "medical", "dentist": "medical",
    }
    return aliases.get(v, v) or "other"


def _clean_address_display(value: str | None) -> str:
    raw = (value or "").strip()
    # Strip leading icon-like glyphs that can render as empty boxes.
    return raw.lstrip("📍•·◦▪▫◆◇▸▹▶▷➤➜→»| ").strip()


def _automation_state(selected: dict) -> dict:
    raw = str(selected.get("automation_state_json") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _env_file_path() -> Path:
    return _ROOT / ".env"


def _upsert_env_key(key: str, value: str) -> None:
    env_path = _env_file_path()
    line = f"{key}={value}"
    if not env_path.exists():
        env_path.write_text(f"{line}\n", encoding="utf-8")
        return
    lines = env_path.read_text(encoding="utf-8").splitlines()
    replaced = False
    for i, raw in enumerate(lines):
        if raw.strip().startswith(f"{key}="):
            lines[i] = line
            replaced = True
            break
    if not replaced:
        lines.append(line)
    env_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _niche_counts(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        k = _niche_key(str(r.get("niche") or "other"))
        counts[k] = counts.get(k, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


def _niche_benchmarks(niche: str) -> tuple[int, int]:
    key = _niche_key(niche)
    table: dict[str, tuple[int, int]] = {
        "landscaping": (420, 700),
        "auto": (560, 480),
        "barber": (640, 55),
        "medical": (740, 220),
        "restaurant": (980, 42),
        "other": (360, 180),
    }
    return table.get(key, table["other"])


def _revenue_leak(niche: str) -> tuple[int, float]:
    search_vol, avg_ticket = _niche_benchmarks(niche)
    lost_traffic = search_vol * 0.15
    lost_revenue = lost_traffic * avg_ticket * 0.05
    return int(round(lost_traffic)), float(lost_revenue)


def _retainer_proposal(selected: dict, lost_revenue: float) -> str:
    business = str(selected.get("business_name") or "This business")
    niche = str(selected.get("niche") or "local service")
    return (
        f"{business} appears to be leaving roughly ${lost_revenue:,.0f}/mo on the table due to weak web visibility.\n\n"
        "Recommended launch structure:\n"
        "- Setup: $1,500 one-time (AI site build, local SEO foundation, conversion wiring)\n"
        "- Ongoing: $250/mo maintenance (hosting, updates, analytics, monthly iteration)\n\n"
        f"For {niche} in Ann Arbor, this keeps payback realistic while compounding lead quality."
    )


def _sync_to_outreach_waterfall(selected: dict) -> tuple[bool, str]:
    business = str(selected.get("business_name") or "Local Business")
    place_id = str(selected.get("place_id") or "")
    phone = str(selected.get("phone") or "")
    niche = str(selected.get("niche") or "local business")
    mockup = str(selected.get("mockup_asset") or "").strip()

    if not mockup:
        try:
            mockup = generate_premium_mockup(
                business,
                niche,
                premium_vibe_for_niche(niche, str(selected.get("design_color_theme") or "")),
            )
        except Exception:
            mockup = ""

    payload = {
        "place_id": place_id,
        "business_name": business,
        "phone": phone,
        "niche": niche,
        "owner_name": selected.get("owner_name"),
        "direct_email": selected.get("direct_email"),
        "owner_linkedin": selected.get("owner_linkedin"),
        "mockup_asset": mockup,
        "workflow": "clay_gpt_image_2_11x",
    }
    if WATERFALL_WEBHOOK:
        webhook_url = str(WATERFALL_WEBHOOK).strip().strip("<>").strip("'").strip('"')
        if not webhook_url.startswith(("http://", "https://")):
            return False, "AADE_WATERFALL_WEBHOOK must start with http:// or https://"
        try:
            r = requests.post(webhook_url, json=payload, timeout=25)
            r.raise_for_status()
            return True, "Waterfall sent to n8n/Zapier."
        except Exception as exc:
            return False, f"Waterfall webhook failed: {exc}"
    return False, "AADE_WATERFALL_WEBHOOK is not configured."


def _load_stats() -> tuple:
    with db.connect() as conn:
        s = db.get_aggregate_stats(conn)
        mrr = float(db.get_dashboard_value(conn, D_MRR, "500"))
        conv = float(db.get_dashboard_value(conn, DB_KEY_CONVERSION, "0"))
        cpc = float(db.get_dashboard_value(conn, DB_KEY_COST_PER_CONTACT, "0.69"))
        recent = [dict(x) for x in db.list_recent_leads(conn, 200)]
    return s, mrr, conv, cpc, recent


@st.cache_data(show_spinner=False)
def _geocode(address: str, allow_network: bool = True) -> tuple[float, float]:
    if not address:
        return (ANN_ARBOR_LAT, ANN_ARBOR_LON)
    if allow_network:
        try:
            r = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": address, "format": "json", "limit": 1},
                timeout=1.2,
                headers={"User-Agent": "ann-arbor-growth-engine/0.1"},
            )
            r.raise_for_status()
            data = r.json()
            if data:
                return (float(data[0]["lat"]), float(data[0]["lon"]))
        except Exception:
            pass
    h = int(hashlib.sha256(address.encode()).hexdigest()[:8], 16)
    lat = ANN_ARBOR_LAT + (((h % 1000) / 1000.0) - 0.5) * 0.08
    lon = ANN_ARBOR_LON + ((((h // 1000) % 1000) / 1000.0) - 0.5) * 0.08
    return (lat, lon)


def _attach_coords(rows: list[dict], *, network_lookups: int = 0) -> list[dict]:
    out: list[dict] = []
    max_network_lookups = max(0, int(network_lookups))
    for i, r in enumerate(rows):
        lat, lon = _geocode(
            str(r.get("address") or "Ann Arbor, MI"),
            allow_network=i < max_network_lookups,
        )
        x = dict(r)
        x["latitude"] = lat
        x["longitude"] = lon
        out.append(x)
    return out


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r_miles = 3958.8
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lon / 2) ** 2
    )
    return 2 * r_miles * math.asin(math.sqrt(a))


def _is_near_ann_arbor(address: str | None) -> bool:
    if not address:
        return False
    lat, lon = _geocode(address, allow_network=True)
    return _haversine_miles(ANN_ARBOR_LAT, ANN_ARBOR_LON, lat, lon) <= MAX_SCRAPE_RADIUS_MILES


def _maps_embed_url(
    key: str,
    *,
    lat: float,
    lon: float,
    place_id: str | None = None,
) -> str:
    # Some stored place_id values come from non-Places sources and can break
    # the /place embed with: "Invalid 'q' parameter". Street View is robust.
    return (
        "https://www.google.com/maps/embed/v1/streetview"
        f"?key={key}&location={lat},{lon}&heading=210&pitch=10&fov=90"
    )


def _selected_from_event(rows: list[dict], event: object | None) -> dict | None:
    if not rows or event is None:
        return None
    try:
        sel = getattr(event, "selection", None) or (
            event.get("selection") if isinstance(event, dict) else None
        )
        if not sel:
            return None
        objs = getattr(sel, "objects", None) or (
            sel.get("objects") if isinstance(sel, dict) else None
        )
        if isinstance(objs, dict):
            for _, items in objs.items():
                if isinstance(items, list) and items:
                    first = items[0]
                    if isinstance(first, dict):
                        pid = str(first.get("place_id") or "")
                        if pid:
                            for r in rows:
                                if str(r.get("place_id") or "") == pid:
                                    return r
        idxs = getattr(sel, "indices", None) or (
            sel.get("indices") if isinstance(sel, dict) else None
        )
        if isinstance(idxs, dict):
            for _, idx_list in idxs.items():
                if isinstance(idx_list, list) and idx_list:
                    idx = int(idx_list[0])
                    if 0 <= idx < len(rows):
                        return rows[idx]
    except Exception:
        return None
    return None


def _selected_from_state(rows: list[dict]) -> dict | None:
    pid = str(st.session_state.get("selected_place_id") or "")
    if not pid:
        return None
    for r in rows:
        if str(r.get("place_id") or "") == pid:
            return r
    return None


def _render_street_view_fullscreen(
    *,
    slot: object | None,
    key: str,
    business_name: str,
    lat: float,
    lon: float,
) -> None:
    overlay_name = html.escape(business_name or "Unknown Business")
    src = _maps_embed_url(
        key,
        lat=lat,
        lon=lon,
    )
    html_block = f"""
<div style="position:fixed;inset:0;z-index:0;">
  <iframe
    src="{src}"
    title="Street View"
    loading="eager"
    style="position:absolute;inset:0;width:100%;height:100%;border:0;"
    referrerpolicy="no-referrer-when-downgrade"
    allowfullscreen
  ></iframe>
  <div style="
    position:absolute;top:14px;left:50%;transform:translateX(-50%);
    padding:8px 14px;border-radius:999px;
    background:rgba(10,10,10,0.78);border:1px solid rgba(255,203,5,0.48);
    color:#FFCB05;font-family:'JetBrains Mono',monospace;font-size:10px;
    font-weight:700;letter-spacing:.08em;text-transform:uppercase;
    box-shadow:0 0 16px rgba(255,203,5,0.24);
  ">
    Surface Level Reached: {overlay_name}
  </div>
</div>
    """
    host = slot if slot is not None else st
    host.markdown(html_block, unsafe_allow_html=True)


def _pick_high_value_target(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    hv = [r for r in rows if int(r.get("no_website_flag") or 0) == 1]
    targets = hv or rows
    idx = int(st.session_state.get("_recording_target_idx", 0)) % len(targets)
    target = targets[idx]
    st.session_state._recording_target_idx = (idx + 1) % len(targets)
    return target


def _clear_motion_state() -> None:
    st.session_state._next_explore_tick = 0.0
    st.session_state._explore_last_motion_ts = 0.0
    st.session_state._explore_phase = 0.0
    st.session_state._cam_fly_until = 0.0
    st.session_state._focus_orbit_until = 0.0
    st.session_state._focus_orbit_pid = ""
    st.session_state._recording_last_snap_ts = 0.0
    st.session_state.selected_place_id = ""
    st.session_state.selected_business_name = ""


def _render_recording_overlay(selected: dict | None) -> None:
    if not selected:
        return
    business = str(selected.get("business_name") or "Unknown Business")
    with st.container(key="recording_overlay"):
        st.markdown('<div class="record-label">LinkedIn Hero View · Before vs After</div>', unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Before (Invisible)**")
            st.caption(str(selected.get("address") or "No address"))
            st.caption("Low discoverability in local search")
        with c2:
            st.markdown("**After (Mockup)**")
            mockup_ref = str(selected.get("mockup_asset") or "").strip()
            if mockup_ref:
                st.image(mockup_ref, use_container_width=True)
            else:
                st.caption("No mockup yet")
        st.caption(f"Target: {business}")


def _render_recording_health(
    *,
    selected: dict | None,
    explore_on: bool,
    recording_on: bool,
    current_zoom: float,
    next_snap_eta: float,
    street_key_available: bool,
) -> None:
    target_name = str(
        (selected or {}).get("business_name")
        or st.session_state.get("selected_business_name")
        or "none"
    )
    target_pid = str(st.session_state.get("selected_place_id") or "none")
    if current_zoom >= STREET_VIEW_THRESHOLD and street_key_available:
        gate_status = "OPEN"
    elif current_zoom >= STREET_VIEW_THRESHOLD and not street_key_available:
        gate_status = "BLOCKED (no key)"
    else:
        gate_status = "3D"
    mode = "RECORDING" if recording_on else ("EXPLORE" if explore_on else "IDLE")
    with st.container(key="recording_health"):
        st.markdown(
            f"""
<div class="record-label">Recording Health</div>
<div class="record-health-grid">
  <div class="record-health-row"><b>Mode</b>: {html.escape(mode)}</div>
  <div class="record-health-row"><b>Target</b>: {html.escape(target_name)}</div>
  <div class="record-health-row"><b>Place ID</b>: {html.escape(target_pid)}</div>
  <div class="record-health-row"><b>Zoom</b>: {current_zoom:.2f}</div>
  <div class="record-health-row"><b>Street Gate</b>: {gate_status} (>= {STREET_VIEW_THRESHOLD:.1f})</div>
  <div class="record-health-row"><b>Next Snap ETA</b>: {max(0.0, next_snap_eta):.1f}s</div>
</div>
            """,
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------------------
# Content generators
# ---------------------------------------------------------------------------


def _email_draft(selected: dict) -> str:
    business = str(selected.get("business_name") or "your business")
    niche = str(selected.get("niche") or "local").title()
    return (
        f"Hi {business} team,\n\n"
        f"I came across your {niche} business while mapping Ann Arbor's local "
        f"service landscape — and noticed your online presence has room to grow "
        f"given the quality of what you offer.\n\n"
        f"I'm a University of Michigan CS student building modern, "
        f"conversion-focused websites for local businesses at a fraction of "
        f"agency prices. I've put together a design preview specifically for "
        f"{business}, built with the same aesthetic language as Stripe and Linear.\n\n"
        f"Would you be open to a 10-minute call this week? I'll walk you "
        f"through the mockup — no strings attached.\n\n"
        f"Best,\nMiles Seade\nUniversity of Michigan · CS\nAnn Arbor Growth Engine"
    )


def _render_copy_button(text: str, key_suffix: str) -> None:
    payload = json.dumps(text)
    st.markdown(
        f"""
<button
  id="copy-btn-{key_suffix}"
  class="copy-btn"
  type="button"
  onclick="navigator.clipboard.writeText({payload}).then(() => {{
    const b = document.getElementById('copy-btn-{key_suffix}');
    if (!b) return;
    const old = b.innerText;
    b.innerText = 'Copied';
    setTimeout(() => b.innerText = old, 1200);
  }})"
>
  Copy to Clipboard
</button>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Camera state
# ---------------------------------------------------------------------------

def _init_camera() -> None:
    for k, v in _CAM_DEFAULTS.items():
        if f"cam_{k}" not in st.session_state:
            st.session_state[f"cam_{k}"] = v
    if "ops_expanded" not in st.session_state:
        st.session_state.ops_expanded = False
    if "high_precision_map" not in st.session_state:
        st.session_state.high_precision_map = False
    if "coord_warmup_step" not in st.session_state:
        st.session_state.coord_warmup_step = 0
    if "coord_warmup_done" not in st.session_state:
        st.session_state.coord_warmup_done = False
    if "highlight_active_targets" not in st.session_state:
        st.session_state.highlight_active_targets = False
    if "explore_mode" not in st.session_state:
        st.session_state.explore_mode = False
    if "explore_speed_sec" not in st.session_state:
        st.session_state.explore_speed_sec = 48
    if "snooze_preview" not in st.session_state:
        st.session_state.snooze_preview = None
    if "ops_hidden" not in st.session_state:
        st.session_state.ops_hidden = False
    if "ops_advanced_mode" not in st.session_state:
        st.session_state.ops_advanced_mode = False
    if "_last_orbit_ts" not in st.session_state:
        st.session_state._last_orbit_ts = time.perf_counter()
    if "_focus_orbit_until" not in st.session_state:
        st.session_state._focus_orbit_until = 0.0
    if "_focus_orbit_pid" not in st.session_state:
        st.session_state._focus_orbit_pid = ""
    if "_explore_phase" not in st.session_state:
        st.session_state._explore_phase = 0.0
    if "_next_explore_tick" not in st.session_state:
        st.session_state._next_explore_tick = 0.0
    if "_explore_base_zoom" not in st.session_state:
        st.session_state._explore_base_zoom = float(st.session_state.cam_zoom)
    if "_cam_transition_ms" not in st.session_state:
        st.session_state._cam_transition_ms = 0
    if "explore_preset" not in st.session_state:
        st.session_state.explore_preset = "LinkedIn Smooth"
    if "explore_pitch_amp" not in st.session_state:
        st.session_state.explore_pitch_amp = EXPLORE_PRESETS["LinkedIn Smooth"]["pitch_amp"]
    if "explore_zoom_amp" not in st.session_state:
        st.session_state.explore_zoom_amp = EXPLORE_PRESETS["LinkedIn Smooth"]["zoom_amp"]
    if "current_bearing" not in st.session_state:
        st.session_state.current_bearing = float(st.session_state.cam_bearing)
    if "current_zoom" not in st.session_state:
        st.session_state.current_zoom = float(st.session_state.cam_zoom)
    if "selected_place_id" not in st.session_state:
        st.session_state.selected_place_id = ""
    if "selected_business_name" not in st.session_state:
        st.session_state.selected_business_name = ""
    if "selected_lat" not in st.session_state:
        st.session_state.selected_lat = float(st.session_state.cam_lat)
    if "selected_lon" not in st.session_state:
        st.session_state.selected_lon = float(st.session_state.cam_lon)
    if "_explore_loop_count" not in st.session_state:
        st.session_state._explore_loop_count = 0
    if "_explore_last_tick_ts" not in st.session_state:
        st.session_state._explore_last_tick_ts = 0.0
    if "_explore_last_motion_ts" not in st.session_state:
        st.session_state._explore_last_motion_ts = 0.0
    if "_cam_fly_until" not in st.session_state:
        st.session_state._cam_fly_until = 0.0
    if "sunset_mode" not in st.session_state:
        st.session_state.sunset_mode = True
    if "recording_mode" not in st.session_state:
        st.session_state.recording_mode = False
    if "_recording_last_snap_ts" not in st.session_state:
        st.session_state._recording_last_snap_ts = 0.0
    if "_recording_target_idx" not in st.session_state:
        st.session_state._recording_target_idx = 0


def _bearing_label(b: float) -> str:
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return dirs[round((b % 360) / 45) % 8]


def _clamp_camera_state() -> None:
    pitch = float(st.session_state.cam_pitch)
    if pitch < MIN_CAM_PITCH or pitch > MAX_CAM_PITCH:
        # Recovery mode: snap to stable default if drift occurs.
        st.session_state.cam_pitch = 62.0
    else:
        st.session_state.cam_pitch = max(
            MIN_CAM_PITCH,
            min(MAX_CAM_PITCH, pitch),
        )


# ---------------------------------------------------------------------------
# Map
# ---------------------------------------------------------------------------

def _render_map(rows: list[dict], *, slot: object | None = None) -> object | None:
    if not rows:
        st.info("No lead data yet.")
        return None

    cam_lat = float(st.session_state.cam_lat)
    cam_lon = float(st.session_state.cam_lon)
    pulse = 0.62 + 0.38 * (0.5 + 0.5 * math.sin(time.perf_counter() * 2.6))
    sunset_mode = bool(st.session_state.get("sunset_mode", True))
    highlight_targets = bool(st.session_state.get("highlight_active_targets", False))
    layer_data = []
    for i, r in enumerate(rows):
        dist_miles = _haversine_miles(cam_lat, cam_lon, float(r["latitude"]), float(r["longitude"]))
        # Closer bars become more transparent so street detail stays visible.
        alpha = int(max(110, min(235, 55 + (min(dist_miles, 8.0) / 8.0) * 180)))
        is_active_target = int(r.get("no_website_flag") or 0) == 1
        if highlight_targets and not is_active_target:
            continue
        if highlight_targets and is_active_target:
            fill_color = [0, 156, 222, alpha]  # standout blue when target mode enabled
            glow_color = [0, 255, 240, int(120 * pulse)]
            stroke_color = [120, 255, 245, 220]
        elif is_active_target:
            # Value ramp: high-value targets burn maize, everything else drops
            # to muted steel. Uniform colour left the field with no focal point.
            fill_color = [255, 203, 5, alpha]
            glow_color = [255, 203, 5, int(110 * pulse)]
            stroke_color = [255, 243, 170, 210]
        else:
            # Lifted off dark-v11: the earlier muted steel sat at nearly the
            # same value as the basemap, hiding most of the dataset.
            fill_color = [104, 140, 184, max(120, alpha - 45)]
            glow_color = [104, 140, 184, int(48 * pulse)]
            stroke_color = [150, 184, 222, 150]
        layer_data.append(
            {
                "place_id": str(r.get("place_id") or ""),
                "position": [r["longitude"], r["latitude"]],
                "elevation": (
                    COLUMN_ELEVATION_MAX if is_active_target else COLUMN_ELEVATION_MIN
                ) + (i % 7) * 8,
                "business_name": r.get("business_name", "Unknown"),
                "address": r.get("address", ""),
                "niche": (r.get("niche") or "").title(),
                "fill_color": fill_color,
                "glow_color": glow_color,
                "stroke_color": stroke_color,
            }
        )

    view_state = pdk.ViewState(
        latitude=float(st.session_state.cam_lat),
        longitude=float(st.session_state.cam_lon),
        zoom=float(st.session_state.cam_zoom),
        pitch=float(st.session_state.cam_pitch),
        bearing=float(st.session_state.cam_bearing),
        min_zoom=SPIRAL_ZOOM_MIN - 3.0,
        max_zoom=SPIRAL_ZOOM_MAX,
        min_pitch=MIN_CAM_PITCH,
        max_pitch=MAX_CAM_PITCH,
        transition_duration=max(0, int(st.session_state.get("_cam_transition_ms", 0) or 0)),
    )
    map_view = pdk.View(
        type="MapView",
        controller={
            "minPitch": MIN_CAM_PITCH,
            "maxPitch": MAX_CAM_PITCH,
            "dragPan": True,
            "dragRotate": True,
            "touchRotate": True,
            "touchZoom": True,
            "doubleClickZoom": True,
            "keyboard": True,
            "inertia": 450,
            "scrollZoom": {"smooth": True, "speed": 0.006},
        },
    )

    token: str = MAPBOX_API_KEY or os.getenv("MAPBOX_API_KEY", "")
    if token:
        os.environ["MAPBOX_API_KEY"] = token

    style_map = {
        "satellite": "mapbox://styles/mapbox/satellite-streets-v12",
        "dark":      "mapbox://styles/mapbox/dark-v11",
        "street":    "https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json",
    }
    map_style = (
        style_map.get(str(st.session_state.cam_map_style), style_map["satellite"])
        if token
        else "https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json"
    )
    if sunset_mode and token:
        # navigation-night ships coloured interstate shields and bright labels
        # that fight the amber HUD; dark-v11 is desaturated and stays back.
        map_style = "mapbox://styles/mapbox/dark-v10"

    chart_host = slot if slot is not None else st
    return chart_host.pydeck_chart(
        pdk.Deck(
            map_style=map_style,
            api_keys={"mapbox": token} if token else {},
            initial_view_state=view_state,
            views=[map_view],
            layers=[
                pdk.Layer(
                    "ScatterplotLayer",
                    id="lead-glow",
                    data=layer_data,
                    get_position="position",
                    get_fill_color="glow_color",
                    get_radius=48,
                    opacity=0.38,
                    stroked=False,
                    pickable=False,
                ),
                pdk.Layer(
                    "ColumnLayer",
                    id="leads",
                    data=layer_data,
                    get_position="position",
                    get_elevation="elevation",
                    elevation_scale=COLUMN_ELEVATION_SCALE,
                    radius=62,
                    extruded=True,
                    pickable=True,
                    get_fill_color="fill_color",
                    get_line_color="stroke_color",
                    stroked=True,
                    wireframe=False,
                    opacity=0.92,
                    auto_highlight=True,
                    highlight_color=[255, 238, 161, 255],
                )
            ],
            tooltip={
                "html": (
                    "<div style='font-family:\"JetBrains Mono\",monospace;padding:8px 10px;"
                    "background:rgba(0,39,76,0.92);border:1px solid #ffcb0566;"
                    "border-radius:10px;font-size:11px;'>"
                    "<b style='color:#FFCB05;font-size:13px'>{business_name}</b><br/>"
                    "<span style='color:#cbd5e1'>{address}</span><br/>"
                    "<span style='color:#ffcb0588;font-size:10px;text-transform:uppercase;"
                    "letter-spacing:.06em'>{niche}</span></div>"
                )
            },
        ),
        use_container_width=True,
        height=980,
        on_select="rerun",
        selection_mode="single-object",
        key="lead_map",
    )


# ---------------------------------------------------------------------------
# CSS injection
# ---------------------------------------------------------------------------

def _inject_css() -> None:
    st.markdown(
        """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">

<style>
/* ══════════════════════════════════════════════════════════════════
   NUCLEAR STREAMLIT RESET
══════════════════════════════════════════════════════════════════ */
#MainMenu, footer, header,
[data-testid="stToolbar"],
[data-testid="stDecoration"],
[data-testid="stStatusWidget"],
[data-testid="stSidebar"],
[data-testid="stCaptionContainer"] { display: none !important; }

html, body, [data-testid="stAppViewContainer"],
[data-testid="stAppViewContainer"] > .main {
  background: #0a0a0a !important;
  padding: 0 !important;
  margin: 0 !important;
  overflow: hidden !important;
}
[data-testid="stMainBlockContainer"],
[data-testid="block-container"],
[data-testid="stVerticalBlock"] { padding: 0 !important; margin: 0 !important; max-width: 100% !important; }

body, [data-testid="stAppViewContainer"] {
  font-family: 'Inter', -apple-system, sans-serif !important;
}
*, *::before, *::after { box-sizing: border-box; }

/* ══════════════════════════════════════════════════════════════════
   FULL-SCREEN MAP
══════════════════════════════════════════════════════════════════ */
div[data-testid="stDeckGlJsonChart"] {
  position: fixed !important;
  inset: 0 !important;
  z-index: 0 !important;
  width: 100vw !important;
  height: 100vh !important;
}
body.explore-on div[data-testid="stDeckGlJsonChart"] {
  transform: scale(1.03);
  transition: transform 300ms ease-out;
}
body:not(.explore-on) div[data-testid="stDeckGlJsonChart"] {
  transform: scale(1.00);
  transition: transform 500ms ease-out;
}

/* ══════════════════════════════════════════════════════════════════
   SHARED GLASS VARIABLES
══════════════════════════════════════════════════════════════════ */
:root {
  --em: #FFCB05;
  --em-dim: rgba(255,203,5,0.35);
  --em-glow: rgba(255,203,5,0.16);
  --glass-bg: rgba(10,10,10,0.70);
  --glass-blur: blur(16px);
  --glass-border: 1px solid rgba(255,203,5,0.32);
  --text-shadow: 0 1px 4px rgba(0,0,0,0.9), 0 0 12px rgba(0,0,0,0.6);
  --mono: 'JetBrains Mono', 'Fira Code', monospace;
}

/* ══════════════════════════════════════════════════════════════════
   PILL BAR — top center
══════════════════════════════════════════════════════════════════ */
.st-key-pill_bar {
  position: fixed !important;
  /* Clear of the top frame edge so the kicker pills are not sliced. */
  top: 22px !important;
  left: 50% !important;
  transform: translateX(-50%) !important;
  z-index: 30 !important;
  background: var(--glass-bg) !important;
  backdrop-filter: var(--glass-blur) saturate(180%) !important;
  -webkit-backdrop-filter: var(--glass-blur) saturate(180%) !important;
  border: var(--glass-border) !important;
  border-radius: 999px !important;
  padding: 0 !important;
  box-shadow:
    0 10px 34px rgba(0,0,0,0.45),
    0 0 0 1px rgba(255,255,255,0.08),
    inset 0 1px 0 rgba(255,255,255,0.05) !important;
  white-space: nowrap !important;
}
.st-key-pill_bar > div { padding: 0 !important; }
.st-key-pill_bar [data-testid="stVerticalBlock"] { gap: 0 !important; }
.st-key-pill_bar [data-testid="stMarkdownContainer"] { padding: 0 !important; }
.st-key-pill_bar button {
  margin-top: 6px !important;
  height: 34px !important;
  border-radius: 999px !important;
  border: 1px solid rgba(255,203,5,0.45) !important;
  background: rgba(0,39,76,0.85) !important;
  color: #FFCB05 !important;
  font-family: var(--mono) !important;
  font-size: 10px !important;
  font-weight: 700 !important;
  letter-spacing: .06em !important;
  text-transform: uppercase !important;
}
.st-key-pill_bar button:hover {
  background: rgba(255,203,5,0.16) !important;
}
.pill-inner {
  display: flex;
  align-items: center;
  gap: 0;
  /* Vertical room for the 30px numerals; at 2px the value ran into the
     bar's bottom edge and the kicker pills clipped off the top. */
  padding: 14px 10px 10px;
  background:
    radial-gradient(circle at 10% 10%, rgba(255,203,5,0.16), transparent 42%),
    linear-gradient(180deg, rgba(255,203,5,0.10) 0%, rgba(255,203,5,0.04) 100%);
}
.pill-stat {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 20px;
  position: relative;
}
.pill-kicker {
  position: absolute;
  top: -7px;
  left: 50%;
  transform: translateX(-50%);
  font-family: var(--mono) !important;
  font-size: 8px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: rgba(255,203,5,0.95);
  background: rgba(0,39,76,0.9);
  border: 1px solid rgba(255,203,5,0.35);
  border-radius: 999px;
  padding: 1px 6px;
  text-shadow: var(--text-shadow);
}
.pill-stat + .pill-stat::before {
  content: '';
  position: absolute;
  left: 0; top: 20%; bottom: 20%;
  width: 1px;
  background: rgba(255,203,5,0.22);
}
.pill-label {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: rgba(191,219,254,0.92);
  text-shadow: var(--text-shadow);
}
/* Display-scale numerals: these are read off a screen recording, often on a
   phone, so the value has to outrank its own label by a wide margin. */
.pill-value {
  font-family: var(--mono) !important;
  font-size: 30px;
  line-height: 1.05;
  font-weight: 700;
  color: var(--em);
  text-shadow: 0 0 16px rgba(255,203,5,0.5), var(--text-shadow);
}
.profit-stat .pill-value {
  animation: profit-pulse 2.2s ease-in-out infinite;
  font-size: 24px;
}
.pill-sub {
  font-family: var(--mono) !important;
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: rgba(191,219,254,0.6);
  margin-left: 6px;
}
.profit-stat .pill-kicker {
  border-color: rgba(52,211,153,0.35);
  color: rgba(52,211,153,0.95);
}
@keyframes profit-pulse {
  0%, 100% { transform: scale(1); text-shadow: 0 0 14px rgba(52,211,153,0.25), var(--text-shadow); }
  50% { transform: scale(1.04); text-shadow: 0 0 22px rgba(52,211,153,0.5), var(--text-shadow); }
}
.pill-dot {
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--em);
  box-shadow: 0 0 8px var(--em);
  animation: dot-pulse 1.8s ease-in-out infinite;
  flex-shrink: 0;
}
.pill-live {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  margin-left: 6px;
  font-family: var(--mono) !important;
  font-size: 8.5px;
  font-weight: 700;
  letter-spacing: 0.09em;
  text-transform: uppercase;
  color: rgba(255,203,5,0.9);
  padding: 2px 6px;
  border-radius: 999px;
  border: 1px solid rgba(255,203,5,0.3);
  background: rgba(255,203,5,0.08);
}
.pill-live-dot {
  width: 5px;
  height: 5px;
  border-radius: 999px;
  background: #FFCB05;
  box-shadow: 0 0 8px rgba(255,203,5,0.9);
  animation: dot-pulse 1.7s ease-in-out infinite;
}
@keyframes dot-pulse {
  0%,100% { opacity:1; transform:scale(1); }
  50% { opacity:.4; transform:scale(.7); }
}

/* ══════════════════════════════════════════════════════════════════
   INTELLIGENCE PANEL — right side
══════════════════════════════════════════════════════════════════ */
.st-key-intel_panel {
  position: fixed !important;
  top: 0 !important;
  right: 0 !important;
  width: min(420px, 98vw) !important;
  height: 100vh !important;
  z-index: 20 !important;
  background: var(--glass-bg) !important;
  backdrop-filter: blur(14px) saturate(160%) !important;
  -webkit-backdrop-filter: blur(14px) saturate(160%) !important;
  border-left: var(--glass-border) !important;
  overflow-y: auto !important;
  overflow-x: hidden !important;
  scrollbar-width: thin !important;
  scrollbar-color: var(--em-dim) transparent !important;
  animation: intel-slide-in 0.35s cubic-bezier(.16,1,.3,1) both !important;
}
@keyframes intel-slide-in {
  from { transform: translateX(30px); opacity: 0; }
  to   { transform: translateX(0);    opacity: 1; }
}
.st-key-intel_panel::-webkit-scrollbar { width: 3px; }
.st-key-intel_panel::-webkit-scrollbar-thumb { background: var(--em-dim); border-radius: 99px; }
.st-key-intel_panel > div { padding: 0 !important; }
.st-key-intel_panel [data-testid="stVerticalBlock"] { gap: 0 !important; }

/* Intel panel — expanders */
.st-key-intel_panel details {
  background: rgba(52,211,153,0.03) !important;
  border: 1px solid rgba(52,211,153,0.2) !important;
  border-radius: 11px !important;
  margin: 0 14px 8px !important;
  overflow: hidden !important;
  transition: border-color 0.2s, box-shadow 0.2s !important;
}
.st-key-intel_panel details:hover {
  border-color: rgba(52,211,153,0.45) !important;
  box-shadow: 0 0 18px rgba(52,211,153,0.1) !important;
}
.st-key-intel_panel details summary {
  list-style: none !important;
  padding: 11px 14px !important;
  font-size: 11px !important;
  font-weight: 700 !important;
  letter-spacing: 0.09em !important;
  text-transform: uppercase !important;
  color: var(--em) !important;
  cursor: pointer !important;
  display: flex !important;
  align-items: center !important;
  gap: 8px !important;
  text-shadow: var(--text-shadow) !important;
}
.st-key-intel_panel details summary::before {
  content: '›';
  font-size: 16px;
  font-weight: 400;
  color: var(--em-dim);
  transition: transform 0.2s;
  flex-shrink: 0;
}
.st-key-intel_panel details[open] summary::before { transform: rotate(90deg); }
.st-key-intel_panel [data-testid="stExpanderToggleIcon"] { display: none !important; }
.st-key-intel_panel details summary::-webkit-details-marker { display: none !important; }
.st-key-intel_panel details summary::marker { display: none !important; }
.st-key-intel_panel details[open] summary {
  border-bottom: 1px solid rgba(52,211,153,0.14) !important;
}
.st-key-intel_panel [data-testid="stExpanderDetails"] { padding: 12px 14px !important; }

/* Intel panel — file uploader */
.st-key-intel_panel [data-testid="stFileUploader"] > div {
  background: rgba(52,211,153,0.04) !important;
  border: 1.5px dashed rgba(52,211,153,0.35) !important;
  border-radius: 10px !important;
  transition: all 0.2s !important;
}
.st-key-intel_panel [data-testid="stFileUploader"] > div:hover {
  border-color: var(--em) !important;
  background: rgba(52,211,153,0.08) !important;
  box-shadow: 0 0 20px rgba(52,211,153,0.12) !important;
}

/* Intel panel — forms & inputs */
.st-key-intel_panel input, .st-key-intel_panel textarea {
  background: rgba(255,255,255,0.05) !important;
  border: 1px solid rgba(255,255,255,0.1) !important;
  border-radius: 8px !important;
  color: #f3f4f6 !important;
  font-family: var(--mono) !important;
  font-size: 11.5px !important;
}
.st-key-intel_panel input:focus, .st-key-intel_panel textarea:focus {
  border-color: var(--em) !important;
  box-shadow: 0 0 0 2px var(--em-glow) !important;
}
.st-key-intel_panel label {
  color: rgba(156,163,175,0.75) !important;
  font-size: 9.5px !important;
  font-weight: 700 !important;
  letter-spacing: 0.08em !important;
  text-transform: uppercase !important;
}
.st-key-intel_panel button {
  background: var(--em-glow) !important;
  border: 1px solid var(--em-dim) !important;
  border-radius: 9px !important;
  color: var(--em) !important;
  font-size: 11px !important;
  font-weight: 700 !important;
  letter-spacing: 0.05em !important;
  transition: all 0.15s !important;
}
.copy-btn {
  margin-top: 8px;
  background: rgba(255,203,5,0.14);
  color: #FFCB05;
  border: 1px solid rgba(255,203,5,0.45);
  border-radius: 8px;
  padding: 8px 12px;
  font-family: var(--mono) !important;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: .06em;
  text-transform: uppercase;
  cursor: pointer;
}
.copy-btn:hover {
  background: rgba(255,203,5,0.24);
  box-shadow: 0 0 14px rgba(255,203,5,0.3);
}
.st-key-intel_panel button:hover {
  background: rgba(52,211,153,0.2) !important;
  box-shadow: 0 0 16px rgba(52,211,153,0.3) !important;
}

/* ══════════════════════════════════════════════════════════════════
   OPS TRAY — bottom left
══════════════════════════════════════════════════════════════════ */

/* Hide-panel control should be quiet chrome, not the loudest CTA. */
.st-key-hide_ops_tray_btn button {
  background: transparent !important;
  border: none !important;
  color: rgba(191,219,254,0.55) !important;
  font-size: 14px !important;
  font-weight: 500 !important;
  box-shadow: none !important;
  min-height: 28px !important;
  padding: 2px 8px !important;
}
.st-key-hide_ops_tray_btn button:hover {
  color: rgba(255,255,255,0.9) !important;
  background: rgba(255,255,255,0.06) !important;
}

.st-key-ops_tray {
  position: fixed !important;
  /* Above the Mapbox wordmark in the bottom-left corner. */
  bottom: 42px !important;
  left: 20px !important;
  width: min(340px, 96vw) !important;
  z-index: 20 !important;
  background: var(--glass-bg) !important;
  backdrop-filter: var(--glass-blur) !important;
  -webkit-backdrop-filter: var(--glass-blur) !important;
  border: var(--glass-border) !important;
  border-radius: 16px !important;
  /* Cap to the safe area and scroll internally; a fixed panel taller than the
     viewport was severing the Camera Controls row at the bottom edge. */
  max-height: calc(100vh - 210px) !important;
  overflow-y: auto !important;
  overscroll-behavior: contain !important;
  box-shadow: 0 16px 40px rgba(0,0,0,0.55), 0 0 0 1px rgba(255,255,255,0.08) !important;
}
.st-key-ops_tray > div { padding: 0 !important; }
.st-key-ops_tray [data-testid="stVerticalBlock"] { gap: 0 !important; }
body.ops-hidden .st-key-ops_tray { display: none !important; }

.st-key-ops_show_btn {
  position: fixed !important;
  bottom: 20px !important;
  left: 20px !important;
  z-index: 21 !important;
  width: auto !important;
}
.st-key-ops_show_btn button {
  background: rgba(10,10,10,0.72) !important;
  border: 1px solid rgba(255,203,5,0.45) !important;
  border-radius: 10px !important;
  color: #FFCB05 !important;
  font-size: 11px !important;
  font-weight: 700 !important;
  letter-spacing: .05em !important;
}
.st-key-recording_overlay {
  position: fixed !important;
  right: 18px !important;
  bottom: 20px !important;
  width: min(520px, 48vw) !important;
  z-index: 24 !important;
  background: rgba(10,10,10,0.78) !important;
  border: 1px solid rgba(255,203,5,0.38) !important;
  border-radius: 12px !important;
  backdrop-filter: blur(10px) !important;
  -webkit-backdrop-filter: blur(10px) !important;
}
.record-label {
  font-family: var(--mono) !important;
  font-size: 9px;
  letter-spacing: .08em;
  text-transform: uppercase;
  color: rgba(255,203,5,.88);
}
.st-key-recording_health {
  position: fixed !important;
  left: 18px !important;
  bottom: 108px !important;
  z-index: 24 !important;
  width: min(460px, 46vw) !important;
  background: rgba(10,10,10,0.80) !important;
  border: 1px solid rgba(52,211,153,0.35) !important;
  border-radius: 12px !important;
  backdrop-filter: blur(10px) !important;
  -webkit-backdrop-filter: blur(10px) !important;
}
.record-health-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px 12px;
}
.record-health-row {
  font-family: var(--mono) !important;
  font-size: 9px;
  line-height: 1.45;
  letter-spacing: .04em;
  color: rgba(191,219,254,.9);
}
.record-health-row b {
  color: rgba(52,211,153,.95);
  font-weight: 700;
}

/* Ops tray — expanders */
.st-key-ops_tray details {
  background: transparent !important;
  border: none !important;
  border-top: 1px solid rgba(52,211,153,0.14) !important;
  border-radius: 0 !important;
  margin: 0 !important;
}
.st-key-ops_tray details:first-of-type { border-top: none !important; }
.st-key-ops_tray details summary {
  list-style: none !important;
  padding: 10px 14px !important;
  font-size: 10.5px !important;
  font-weight: 700 !important;
  letter-spacing: 0.09em !important;
  text-transform: uppercase !important;
  color: rgba(52,211,153,0.8) !important;
  cursor: pointer !important;
  text-shadow: var(--text-shadow) !important;
}
.st-key-ops_tray [data-testid="stExpanderToggleIcon"] { display: none !important; }
.st-key-ops_tray details summary::-webkit-details-marker { display: none !important; }
.st-key-ops_tray details summary::marker { display: none !important; }
.st-key-ops_tray details[open] summary { color: var(--em) !important; }
.st-key-ops_tray [data-testid="stExpanderDetails"] { padding: 4px 14px 14px !important; }

/* Ops — inputs & buttons */
.st-key-ops_tray input, .st-key-ops_tray textarea {
  background: rgba(255,255,255,0.05) !important;
  border: 1px solid rgba(255,255,255,0.1) !important;
  border-radius: 8px !important;
  color: #f3f4f6 !important;
  font-family: var(--mono) !important;
  font-size: 11.5px !important;
}
.st-key-ops_tray input:focus {
  border-color: var(--em) !important;
  box-shadow: 0 0 0 2px var(--em-glow) !important;
}
.st-key-ops_tray label {
  font-size: 12px !important;
  color: rgba(156,163,175,0.7) !important;
  font-size: 9.5px !important;
  font-weight: 700 !important;
  letter-spacing: 0.07em !important;
  text-transform: uppercase !important;
}
.st-key-ops_tray button {
  background: var(--em-glow) !important;
  border: 1px solid var(--em-dim) !important;
  border-radius: 8px !important;
  color: var(--em) !important;
  font-size: 11px !important;
  font-weight: 700 !important;
  transition: all 0.15s !important;
}
.st-key-ops_tray button:hover {
  background: rgba(52,211,153,0.2) !important;
  box-shadow: 0 0 14px rgba(52,211,153,0.28) !important;
}

/* Camera buttons inside ops tray */
.cam-btn-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 5px;
  margin: 6px 0;
}
.cam-btn-grid-2 { grid-template-columns: repeat(2, 1fr); }

/* ══════════════════════════════════════════════════════════════════
   REUSABLE PRIMITIVES
══════════════════════════════════════════════════════════════════ */
.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 18px;
  border-bottom: 1px solid rgba(52,211,153,0.18);
}
.panel-title {
  font-size: 10.5px;
  font-weight: 800;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--em);
  text-shadow: var(--text-shadow);
  display: flex;
  align-items: center;
  gap: 8px;
}
.info-grid { padding: 0 2px; }
.info-row {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 5px 0;
  border-bottom: 1px solid rgba(255,255,255,0.04);
}
.info-row:last-child { border-bottom: none; }
.info-key {
  font-family: var(--mono) !important;
  font-size: 9.5px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.07em;
  color: rgba(156,163,175,0.6);
  min-width: 52px;
  flex-shrink: 0;
}
.info-val {
  font-family: var(--mono) !important;
  font-size: 12px;
  font-weight: 500;
  color: #e5e7eb;
  line-height: 1.4;
  word-break: break-word;
  text-shadow: var(--text-shadow);
}
.niche-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 11px;
  border-radius: 999px;
  font-size: 9.5px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  border: 1px solid rgba(52,211,153,0.3);
  background: rgba(52,211,153,0.1);
  color: var(--em);
}
.street-frame {
  width: 100%;
  height: 210px;
  border: 0;
  border-radius: 10px;
  border: 1px solid rgba(52,211,153,0.15);
  display: block;
  margin-top: 10px;
}
.code-box {
  background: rgba(0,0,0,0.45);
  border: 1px solid rgba(52,211,153,0.2);
  border-radius: 10px;
  padding: 12px 14px;
  font-family: var(--mono) !important;
  font-size: 10.5px;
  line-height: 1.7;
  color: #d1fae5;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 280px;
  overflow-y: auto;
  scrollbar-width: thin;
  scrollbar-color: var(--em-dim) transparent;
}
.typewriter-box {
  background: rgba(0,0,0,0.42);
  border: 1px solid rgba(52,211,153,0.28);
  border-radius: 10px;
  padding: 12px 14px;
  font-family: var(--mono) !important;
  font-size: 10.5px;
  line-height: 1.7;
  color: #d1fae5;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 280px;
  overflow-y: auto;
  box-shadow: inset 0 0 14px rgba(52,211,153,0.08), 0 0 16px rgba(52,211,153,0.08);
}
.email-pre {
  background: rgba(0,0,0,0.32);
  border: 1px solid rgba(255,255,255,0.07);
  border-radius: 10px;
  padding: 12px 14px;
  font-family: var(--mono) !important;
  font-size: 10.5px;
  line-height: 1.75;
  color: #d1d5db;
  white-space: pre-wrap;
  max-height: 180px;
  overflow-y: auto;
  scrollbar-width: thin;
  scrollbar-color: rgba(255,255,255,0.1) transparent;
}
.send-btn {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  margin-top: 12px;
  padding: 10px 20px;
  background: linear-gradient(135deg, #34d399, #059669);
  color: #020817 !important;
  font-weight: 800;
  font-size: 11px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  text-decoration: none !important;
  border-radius: 10px;
  box-shadow: 0 4px 18px rgba(52,211,153,0.4);
  transition: all 0.2s;
}
.send-btn:hover {
  filter: brightness(1.08);
  box-shadow: 0 6px 26px rgba(52,211,153,0.55);
  transform: translateY(-1px);
}
.cursor-link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 13px;
  margin-top: 10px;
  background: rgba(52,211,153,0.08);
  border: 1px solid var(--em-dim);
  border-radius: 8px;
  color: var(--em) !important;
  font-size: 10.5px;
  font-weight: 700;
  text-decoration: none !important;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  transition: all 0.15s;
}
.cursor-link:hover {
  background: rgba(52,211,153,0.18);
  box-shadow: 0 0 16px rgba(52,211,153,0.25);
}
.empty-state {
  padding: 32px 20px;
  text-align: center;
  color: rgba(156,163,175,0.5);
  font-size: 12.5px;
  line-height: 1.7;
}
.empty-icon { font-size: 38px; display: block; margin-bottom: 12px; }
.review-card {
  background: rgba(0,39,76,0.40);
  border: 1px solid rgba(255,203,5,0.18);
  border-radius: 12px;
  padding: 14px;
  margin-top: 10px;
}
.section-label {
  font-size: 9.5px;
  font-weight: 700;
  letter-spacing: 0.09em;
  text-transform: uppercase;
  color: rgba(52,211,153,0.7);
  margin: 0 0 6px;
}
.cam-label {
  font-family: var(--mono) !important;
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: rgba(52,211,153,0.82);
  line-height: 1.4;
  margin: 0 0 6px;
}
.niche-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 0;
  border-bottom: 1px solid rgba(255,255,255,0.04);
}
.niche-row:last-child { border-bottom: none; }
.niche-label {
  font-size: 11.5px;
  font-weight: 600;
  color: #e5e7eb;
  text-shadow: var(--text-shadow);
}
.niche-count {
  font-family: var(--mono) !important;
  font-size: 11px;
  font-weight: 700;
  color: var(--em);
}
.ops-tray-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 11px 14px;
}
.ops-tray-title {
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: rgba(255,203,5,0.9);
  display: flex;
  align-items: center;
  gap: 7px;
  text-shadow: var(--text-shadow);
}

/* ══════════════════════════════════════════════════════════════════
   U-M WATERMARK
══════════════════════════════════════════════════════════════════ */
/* One chrome system across every floating panel: the KPI bar, lockup,
   legend, ops tray and watermark previously had four different border
   weights and blur radii, which read as assembled rather than designed. */
.gev-lockup, .gev-legend, .um-watermark {
  border-color: rgba(255,203,5,0.26) !important;
  backdrop-filter: blur(14px) saturate(150%) !important;
  -webkit-backdrop-filter: blur(14px) saturate(150%) !important;
  background: rgba(0,39,76,0.74) !important;
  box-shadow: 0 10px 30px rgba(0,0,0,0.45) !important;
}

/* Product identity — the console had no title anywhere on screen. */
.gev-lockup {
  position: fixed;
  top: 92px;
  left: 22px;
  z-index: 24;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 10px 15px;
  background: rgba(0,39,76,0.72);
  border: 1px solid rgba(255,255,255,0.10);
  border-left: 2px solid rgba(255,203,5,0.35);
  border-radius: 4px 12px 12px 4px;
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  pointer-events: none;
}
.gev-lockup-name {
  font-size: 15px;
  font-weight: 500;
  letter-spacing: 0.02em;
  color: rgba(226,240,255,0.95);
  text-shadow: var(--text-shadow);
}
.gev-lockup-name b { color: var(--em); font-weight: 700; }
.gev-lockup-sub {
  font-family: var(--mono) !important;
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: rgba(191,219,254,0.62);
}
body.hud-hidden .gev-lockup { opacity: 0; visibility: hidden; }

/* Colour now carries the primary meaning in the view, so it needs a key.
   Sits under the orphaned TARGETS pill on the right. */
.gev-legend {
  position: fixed;
  top: 92px;
  right: 22px;
  z-index: 24;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 11px 14px;
  background: rgba(0,39,76,0.72);
  border: 1px solid rgba(255,255,255,0.10);
  border-radius: 12px;
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  pointer-events: none;
}
.gev-legend-title {
  font-family: var(--mono) !important;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: rgba(255,203,5,0.85);
}
.gev-legend-row {
  display: flex;
  align-items: center;
  gap: 9px;
  font-size: 13px;
  font-weight: 600;
  color: rgba(226,240,255,0.9);
  text-shadow: var(--text-shadow);
}
.gev-legend-swatch {
  width: 13px;
  height: 13px;
  border-radius: 3px;
  flex-shrink: 0;
}
.gev-legend-swatch.swatch-target {
  background: rgb(255,203,5);
  box-shadow: 0 0 9px rgba(255,203,5,0.65);
}
.gev-legend-swatch.swatch-base {
  background: rgb(148,186,226);
}
body.hud-hidden .gev-legend { opacity: 0; visibility: hidden; }

.um-watermark {
  position: fixed;
  bottom: 42px;
  right: 16px;
  z-index: 25;
  opacity: 0; visibility: hidden;
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 7px 13px;
  background: rgba(0,39,76,0.82);
  border: 1px solid rgba(255,203,5,0.35);
  border-radius: 999px;
  backdrop-filter: blur(12px);
  pointer-events: none;
}
.um-dot {
  width: 6px; height: 6px;
  border-radius: 50%;
  background: #FFCB05;
  box-shadow: 0 0 8px #FFCB05;
  flex-shrink: 0;
}
.um-text {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.07em;
  text-transform: uppercase;
  color: #FFCB05;
  text-shadow: 0 0 12px rgba(255,203,5,0.5);
}
body.hud-hidden .st-key-pill_bar,
body.hud-hidden .st-key-intel_panel,
body.hud-hidden .st-key-ops_tray,
body.hud-hidden .um-watermark {
  display: none !important;
}
body.recording-mode .st-key-pill_bar,
body.recording-mode .st-key-intel_panel,
body.recording-mode .st-key-ops_tray {
  opacity: 0.18 !important;
  transform: scale(0.92) !important;
  pointer-events: none !important;
}
.hud-hidden-indicator {
  position: fixed;
  top: 18px;
  right: 18px;
  z-index: 9998;
  display: none;
  align-items: center;
  gap: 8px;
  padding: 7px 11px;
  border-radius: 999px;
  border: 1px solid rgba(255,203,5,0.5);
  background: rgba(0,39,76,0.88);
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  color: #FFCB05;
  font-family: var(--mono) !important;
  font-size: 9px;
  font-weight: 700;
  letter-spacing: .08em;
  text-transform: uppercase;
  text-shadow: var(--text-shadow);
}
.hud-hidden-indicator-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #FFCB05;
  box-shadow: 0 0 8px rgba(255,203,5,0.9);
}
body.hud-hidden .hud-hidden-indicator {
  display: inline-flex;
}
</style>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Keyboard navigation + click-to-focus JS
# ---------------------------------------------------------------------------

def _inject_nav_js(
    selected: dict | None,
    explore_enabled: bool,
    explore_speed_sec: int | float,
    recording_enabled: bool,
) -> None:
    st.markdown(
        f"""
<script>
(function() {{
  // ── Keyboard → pydeck canvas forwarding ─────────────────────────
  const NAV_KEYS = new Set([
    'ArrowUp','ArrowDown','ArrowLeft','ArrowRight',
    '+','-','=','PageUp','PageDown','Home','End'
  ]);
  const HUD_TOGGLE_CODE = 'Backquote';
  const EXPLORE_ENABLED = {str(explore_enabled).lower()};
  const RECORDING_ENABLED = {str(recording_enabled).lower()};
  const OPS_HIDDEN = {str(bool(getattr(st.session_state, "ops_hidden", False))).lower()};
  const EXPLORE_DURATION_SEC = {float(explore_speed_sec):.1f};
  // Safety reset: never boot with HUD stuck hidden.
  document.body.classList.remove('hud-hidden');
  document.body.classList.toggle('explore-on', EXPLORE_ENABLED);
  document.body.classList.toggle('recording-mode', RECORDING_ENABLED);
  document.body.classList.toggle('ops-hidden', OPS_HIDDEN);
  document.documentElement.style.setProperty(
    '--explore-duration',
    `${{Math.max(12, EXPLORE_DURATION_SEC)}}s`
  );

  function syncExploreMotion() {{
    const root = document.querySelector('div[data-testid="stDeckGlJsonChart"]');
    if (!root) return;
    const iframe = root.querySelector('iframe');
    const canvas = root.querySelector('canvas');
    if (iframe) iframe.classList.add('explore-motion');
    if (canvas) canvas.classList.add('explore-motion');
  }}
  syncExploreMotion();
  setTimeout(syncExploreMotion, 300);
  setTimeout(syncExploreMotion, 1200);

  function getDeckCanvas() {{
    const frames = document.querySelectorAll('iframe');
    for (const fr of frames) {{
      try {{
        const canvas = fr.contentDocument && fr.contentDocument.querySelector('canvas');
        if (canvas) return {{ canvas, doc: fr.contentDocument }};
      }} catch(e) {{}}
    }}
    return null;
  }}

  let deckReady = false;

  document.addEventListener('keydown', function(e) {{
    if (e.code === HUD_TOGGLE_CODE) {{
      if (['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)) return;
      e.preventDefault();
      e.stopPropagation();
      document.body.classList.toggle('hud-hidden');
      return;
    }}
    if (!NAV_KEYS.has(e.key)) return;
    // Don't intercept if user is typing in an input
    if (['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)) return;
    e.preventDefault();
    e.stopPropagation();

    const target = getDeckCanvas();
    if (!target) return;

    if (!deckReady) {{
      target.canvas.focus();
      target.canvas.click();
      deckReady = true;
    }}

    const opts = {{
      key: e.key, code: e.code, keyCode: e.keyCode,
      which: e.which, bubbles: true, cancelable: true,
      shiftKey: e.shiftKey, ctrlKey: e.ctrlKey, altKey: e.altKey
    }};
    target.canvas.dispatchEvent(new KeyboardEvent('keydown', opts));
    target.doc.dispatchEvent(new KeyboardEvent('keydown', opts));
  }}, true);

  document.addEventListener('keyup', function(e) {{
    if (!NAV_KEYS.has(e.key)) return;
    const target = getDeckCanvas();
    if (!target) return;
    const opts = {{ key: e.key, code: e.code, keyCode: e.keyCode, bubbles: true }};
    target.canvas.dispatchEvent(new KeyboardEvent('keyup', opts));
  }}, true);

  // Reset focus tracking on direct map click
  document.addEventListener('click', function(e) {{
    if (e.target && e.target.tagName === 'CANVAS') deckReady = true;
    else deckReady = false;
  }});

  // ── U-M Watermark (ensure it stays on top) ──────────────────────
  // Nothing needed; it's injected as HTML

  console.log('[TCC] Nav JS loaded. Arrow keys forwarded to pydeck canvas.');
}})();
</script>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Pill bar
# ---------------------------------------------------------------------------

def _render_pill(total: int, flagged: int, mrr: float) -> None:
    c1, c2 = st.columns([6, 1], vertical_alignment="center")
    with c2:
        # The bare "Targets" label read as inert chrome; name the current
        # filter state so the control explains what it is doing.
        active_label = (
            "◉ Targets only" if st.session_state.highlight_active_targets else "○ Showing all leads"
        )
        if st.button(active_label, key="pill_toggle_targets", use_container_width=True):
            st.session_state.highlight_active_targets = not st.session_state.highlight_active_targets
            st.rerun()
    with c1:
        st.markdown(
            f"""
<div class="pill-inner">
  <div class="pill-stat">
    <span class="pill-kicker">Radar</span>
    <span class="pill-dot"></span>
    <span class="pill-label">Total Leads</span>
    <span class="pill-value">{total}</span>
    <span class="pill-live"><span class="pill-live-dot"></span>Live</span>
  </div>
  <div class="pill-stat">
    <span class="pill-kicker">Pipeline</span>
    <span class="pill-label">Active Targets</span>
    <span class="pill-value">{flagged}</span>
  </div>
  <div class="pill-stat profit-stat">
    <span class="pill-kicker">Revenue</span>
    <span class="pill-label">Pipeline</span>
    <span class="pill-value">${flagged * mrr:,.0f}</span>
    <span class="pill-sub">{flagged} × ${mrr:,.0f}/mo</span>
  </div>
</div>
            """,
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------------------
# Intelligence panel
# ---------------------------------------------------------------------------

def _render_intel(selected: dict | None, recent: list[dict]) -> None:
    if selected:
        _render_intel_lead(selected)
    else:
        _render_intel_overview(recent)


def _render_intel_overview(recent: list[dict]) -> None:
    counts = _niche_counts(recent)
    st.markdown(
        """
<div class="panel-header">
  <div class="panel-title">
    <span class="pill-dot"></span>
    Intelligence Panel
  </div>
  <div style="font-size:9px;color:rgba(156,163,175,0.45);letter-spacing:.07em;font-weight:600;">
    ANN ARBOR · MI
  </div>
</div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="empty-state">'
        '<span class="empty-icon">&#9676;</span>'
        '<strong style="color:#34d399">Click any 3D bar</strong> on the satellite map<br/>'
        'to open Lead Intelligence.<br/><br/>'
        '<span style="font-size:11px;color:rgba(156,163,175,.4);">'
        'Arrow keys pan &nbsp;·&nbsp; Scroll zooms &nbsp;·&nbsp; Right-drag tilts</span>'
        "</div>",
        unsafe_allow_html=True,
    )
    if counts:
        st.markdown(
            '<div style="padding:0 18px 6px;"><div class="section-label">Lead Categories</div></div>',
            unsafe_allow_html=True,
        )
        rows_html = "".join(
            f'<div class="niche-row">'
            f'<span class="niche-label">{niche.title()}</span>'
            f'<span class="niche-count">{count}</span>'
            f"</div>"
            for niche, count in counts.items()
        )
        st.markdown(
            f'<div style="padding:0 18px 16px;">{rows_html}</div>',
            unsafe_allow_html=True,
        )


def _render_intel_lead(selected: dict) -> None:
    business = str(selected.get("business_name") or "Unknown")
    niche = str(selected.get("niche") or "local business")
    address = _clean_address_display(str(selected.get("address") or "Ann Arbor, MI"))
    phone = str(selected.get("phone") or selected.get("telephone") or "—")
    status = str(selected.get("status") or "new")
    owner_name = str(selected.get("owner_name") or "—")
    direct_email = str(selected.get("direct_email") or "—")
    lat = float(selected.get("latitude", ANN_ARBOR_LAT))
    lon = float(selected.get("longitude", ANN_ARBOR_LON))
    precise_lat, precise_lon = _geocode(address, allow_network=True)
    place_id = str(selected.get("place_id") or "") or None
    nk = _niche_key(niche)
    color = _NICHE_COLORS.get(nk, _NICHE_COLORS["other"])
    folder = _project_folder(business)
    cursor_url = f"cursor://file/{quote(str(folder))}"
    action_key = place_id or _slug(business)

    st.markdown(
        f"""
<div class="panel-header">
  <div class="panel-title">
    <span class="pill-dot"></span>
    Lead Intelligence
  </div>
  <span class="niche-badge" style="border-color:{color}55;background:{color}14;color:{color};">
    {niche.title()}
  </span>
</div>
<div style="padding:12px 18px 14px;">
  <div style="font-size:18px;font-weight:800;color:#f9fafb;
    text-shadow:0 2px 8px rgba(0,0,0,.8);margin-bottom:4px;">{html.escape(business)}</div>
  <div style="font-family:var(--mono);font-size:11px;color:rgba(156,163,175,.7);
    text-shadow:var(--text-shadow);">{html.escape(address)}</div>
</div>
        """,
        unsafe_allow_html=True,
    )

    # ── Quick Actions ──────────────────────────────────────────────────
    q1, q2, q3 = st.columns(3)
    if q1.button("Generate Mockup", key=f"qa_gen_{action_key}", use_container_width=True):
        try:
            overrides = None
            if any([
                selected.get("design_color_theme"),
                selected.get("design_font_family"),
                selected.get("design_hero_image_prompt"),
            ]):
                overrides = DesignOverrides(
                    color_theme=str(selected.get("design_color_theme") or "emerald-earth"),
                    font_family=str(selected.get("design_font_family") or "sans"),
                    hero_image_prompt=str(
                        selected.get("design_hero_image_prompt")
                        or f"{niche} business Ann Arbor"
                    ),
                )
            rendered: str | None = None
            try:
                rendered = generate_premium_mockup(
                    business,
                    niche,
                    premium_vibe_for_niche(niche, overrides.color_theme if overrides else ""),
                )
            except Exception:
                rendered = None
            if rendered is None:
                rendered = str(
                    screenshot_landing_page(
                        business,
                        niche=niche,
                        design_overrides=overrides,
                    )
                )
            st.session_state[f"qa_mockup_{action_key}"] = rendered
            if place_id:
                with db.connect() as conn:
                    db.save_lead_automation_fields(conn, place_id, mockup_asset=rendered)
            st.success("Mockup generated.")
        except Exception as exc:
            st.error(f"Mockup generation failed: {exc}")

    if q2.button("Sync to Outreach", key=f"qa_sync_{action_key}", use_container_width=True):
        ok, msg = _sync_to_outreach_waterfall(selected)
        if ok:
            st.success(msg)
        else:
            st.error(msg)

    if q3.button("Propose Retainer", key=f"qa_retainer_{action_key}", use_container_width=True):
        _, lost_revenue = _revenue_leak(niche)
        st.session_state[f"qa_retainer_text_{action_key}"] = _retainer_proposal(selected, lost_revenue)

    mockup_preview = st.session_state.get(f"qa_mockup_{action_key}")
    if isinstance(mockup_preview, str) and mockup_preview.strip():
        st.image(mockup_preview, use_container_width=True)
    retainer_text = st.session_state.get(f"qa_retainer_text_{action_key}")
    if isinstance(retainer_text, str) and retainer_text.strip():
        st.code(retainer_text, language=None)

    # ── Lead Specs ────────────────────────────────────────────────────
    with st.expander("Lead Specs — Address, Phone, Street View"):
        st.markdown(
            f"""
<div class="info-grid">
  <div class="info-row">
    <span class="info-key">Status</span>
    <span class="info-val">{html.escape(status)}</span>
  </div>
  <div class="info-row">
    <span class="info-key">Name</span>
    <span class="info-val">{html.escape(business)}</span>
  </div>
  <div class="info-row">
    <span class="info-key">Niche</span>
    <span class="info-val">{html.escape(niche.title())}</span>
  </div>
  <div class="info-row">
    <span class="info-key">Address</span>
    <span class="info-val">{html.escape(address)}</span>
  </div>
  <div class="info-row">
    <span class="info-key">Phone</span>
    <span class="info-val">{html.escape(phone)}</span>
  </div>
  <div class="info-row">
    <span class="info-key">Owner</span>
    <span class="info-val">{html.escape(owner_name)}</span>
  </div>
  <div class="info-row">
    <span class="info-key">Email</span>
    <span class="info-val">{html.escape(direct_email)}</span>
  </div>
  <div class="info-row">
    <span class="info-key">Coords</span>
    <span class="info-val">{precise_lat:.5f}, {precise_lon:.5f}</span>
  </div>
</div>
            """,
            unsafe_allow_html=True,
        )
        if GOOGLE_STREET_VIEW_KEY:
            embed_url = _maps_embed_url(
                GOOGLE_STREET_VIEW_KEY,
                lat=precise_lat,
                lon=precise_lon,
                place_id=place_id,
            )
            st.markdown(
                f'<iframe class="street-frame" src="{embed_url}" '
                f'loading="lazy" allowfullscreen allow="fullscreen"></iframe>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                '<div style="margin-top:10px;padding:12px;background:rgba(52,211,153,.04);'
                'border:1.5px dashed rgba(52,211,153,.22);border-radius:10px;'
                'font-family:var(--mono);font-size:10.5px;color:rgba(156,163,175,.55);">'
                "Set GOOGLE_EMBED_API_KEY to enable 360 Street View.</div>",
                unsafe_allow_html=True,
            )
        st.markdown(
            f'<a class="cursor-link" href="{cursor_url}">⎋ Open in Cursor</a>',
            unsafe_allow_html=True,
        )
        st.caption("Use Quick Actions above for mockup generation and outreach sync.")

    # ── Lovable Prompt ────────────────────────────────────────────────
    with st.expander("Lovable Prompt — One-Click Copy"):
        prompt = generate_lovable_prompt(selected)
        prompt_payload = json.dumps(prompt)
        tw_id = f"typewriter-{_slug(business)}"
        st.markdown(
            '<div class="section-label">Engineered for Lovable.ai</div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            f"""
<div id="{tw_id}" class="typewriter-box"></div>
<script>
(function() {{
  const el = document.getElementById({json.dumps(tw_id)});
  if (!el) return;
  const text = {prompt_payload};
  if (el.dataset.typed === "1") {{
    el.textContent = text;
    return;
  }}
  el.dataset.typed = "1";
  el.textContent = "";
  const step = 2;
  let idx = 0;
  function tick() {{
    idx = Math.min(text.length, idx + step);
    el.textContent = text.slice(0, idx);
    if (idx < text.length) {{
      window.requestAnimationFrame(tick);
    }}
  }}
  tick();
}})();
</script>
            """,
            unsafe_allow_html=True,
        )
        _render_copy_button(prompt, key_suffix=f"lovable-{_slug(business)}")
        st.markdown(
            '<div style="font-family:var(--mono);font-size:10px;'
            'color:rgba(156,163,175,.45);margin-top:6px;line-height:1.6;">'
            "Click the copy icon above. Paste into Lovable.ai.<br/>"
            "Screenshot the result and upload in Outreach Flow.</div>",
            unsafe_allow_html=True,
        )

    # ── Digital Twin Preview + Waterfall ──────────────────────────────
    if selected is not None:
        with st.expander("Digital Twin Preview + Sync"):
            mockup_ref = str(selected.get("mockup_asset") or "").strip()
            if mockup_ref:
                st.image(mockup_ref, use_container_width=True)
            else:
                st.info("No high-fidelity mockup saved yet. Generate one in Lead Specs.")
            if st.button("Sync to Outreach", key=f"sync_outreach_{place_id or _slug(business)}", use_container_width=True):
                ok, msg = _sync_to_outreach_waterfall(selected)
                if ok:
                    st.success(msg)
                else:
                    st.error(msg)

    # ── Sequence Orchestrator ──────────────────────────────────────────
    if selected is not None:
        st.markdown("**Sequence Orchestrator — Timeline + Retries**")
        state = _automation_state(selected)
        call_tag = str(selected.get("call_log_tag") or state.get("call_tag") or "pending")
        enrichment_ok = bool(selected.get("owner_linkedin") or selected.get("direct_email"))
        mockup_ok = bool(str(selected.get("mockup_asset") or "").strip())
        call_done = call_tag.strip().lower() not in {"", "pending"}
        email_fired = bool(state.get("email_fired"))
        email_2_sent = bool(state.get("email_2_sent_at"))
        email_3_sent = bool(state.get("email_3_sent_at"))
        email_2_due = str(state.get("email_2_due_at") or "not scheduled")
        email_3_due = str(state.get("email_3_due_at") or "not scheduled")

        def _step_row(label: str, ok: bool, detail: str) -> str:
            dot = "🟢" if ok else "🟡"
            return f"{dot} **{label}** — {detail}"

        st.markdown(
            "\n".join(
                [
                    _step_row(
                        "1) Enrichment",
                        enrichment_ok,
                        (
                            f"Owner: {selected.get('owner_name') or 'unknown'} · "
                            f"Email: {selected.get('direct_email') or 'missing'}"
                        ),
                    ),
                    _step_row(
                        "2) Visual Hook",
                        mockup_ok,
                        "Mockup ready" if mockup_ok else "Mockup missing",
                    ),
                    _step_row(
                        "3) Voice (11x)",
                        call_done,
                        f"Call tag: {call_tag}",
                    ),
                    _step_row(
                        "4) Email #1",
                        email_fired,
                        "Sent" if email_fired else "Not sent",
                    ),
                    _step_row(
                        "5) Email #2",
                        email_2_sent,
                        (
                            f"Sent at {state.get('email_2_sent_at')}"
                            if email_2_sent
                            else f"Due at {email_2_due}"
                        ),
                    ),
                    _step_row(
                        "6) Email #3",
                        email_3_sent,
                        (
                            f"Sent at {state.get('email_3_sent_at')}"
                            if email_3_sent
                            else f"Due at {email_3_due}"
                        ),
                    ),
                ]
            )
        )

        step_key = place_id or _slug(business)
        has_place_id = bool(place_id)
        if not has_place_id:
            st.warning("This lead has no place_id; retry actions are disabled.")

        if st.button(
            "Run Follow-up Scheduler Now",
            key=f"run_followup_scheduler_{step_key}",
            use_container_width=True,
        ):
            with db.connect() as conn:
                sched = process_due_sequence_emails(conn, limit=250)
            if sched.sent_count:
                st.success(f"Scheduler sent {sched.sent_count} follow-up email(s).")
            else:
                st.info("Scheduler checked leads; nothing due right now.")
            for msg in sched.messages[-4:]:
                st.caption(f"- {msg}")
            st.rerun()

        snooze_days = st.number_input(
            "Snooze follow-up sequence (days)",
            min_value=1,
            max_value=30,
            value=2,
            step=1,
            key=f"snooze_days_{step_key}",
            help="Push unsent Email #2/#3 due dates forward by N days.",
        )
        if st.button(
            "Snooze Sequence",
            key=f"snooze_sequence_{step_key}",
            use_container_width=True,
            disabled=not has_place_id,
        ):
            with db.connect() as conn:
                result = snooze_sequence(
                    conn,
                    place_id=str(place_id or ""),
                    lead=selected,
                    days=int(snooze_days),
                )
            if result.ok:
                st.success(result.message)
                st.session_state.snooze_preview = {
                    "place_id": str(place_id or ""),
                    **(result.meta or {}),
                }
            else:
                st.error(result.message)

        preview = st.session_state.get("snooze_preview")
        if isinstance(preview, dict) and preview and str(preview.get("place_id") or "") == str(place_id or ""):
            e2 = str(preview.get("email_2_due_at") or "n/a")
            e3 = str(preview.get("email_3_due_at") or "n/a")
            st.caption(f"Updated due dates -> Email #2: {e2} · Email #3: {e3}")

        with st.expander("Advanced Recovery Actions"):
            a1, a2 = st.columns(2)
            if a1.button(
                "Retry Enrichment",
                key=f"retry_enrich_{step_key}",
                use_container_width=True,
                disabled=not has_place_id,
            ):
                with db.connect() as conn:
                    result = retry_enrichment_step(conn, place_id=str(place_id or ""), lead=selected)
                st.success(result.message) if result.ok else st.error(result.message)
                st.rerun()
            if a2.button(
                "Retry Mockup",
                key=f"retry_mockup_{step_key}",
                use_container_width=True,
                disabled=not has_place_id,
            ):
                with db.connect() as conn:
                    result = retry_mockup_step(conn, place_id=str(place_id or ""), lead=selected)
                st.success(result.message) if result.ok else st.error(result.message)
                st.rerun()
            a3, a4 = st.columns(2)
            if a3.button(
                "Retry Call",
                key=f"retry_call_{step_key}",
                use_container_width=True,
                disabled=not has_place_id,
            ):
                with db.connect() as conn:
                    result = retry_call_step(conn, place_id=str(place_id or ""), lead=selected)
                st.success(result.message) if result.ok else st.error(result.message)
                st.rerun()
            if a4.button(
                "Retry Email #1",
                key=f"retry_email_{step_key}",
                use_container_width=True,
                disabled=not has_place_id,
            ):
                with db.connect() as conn:
                    result = retry_email_step1(conn, place_id=str(place_id or ""), lead=selected)
                st.success(result.message) if result.ok else st.error(result.message)
                st.rerun()

        notes = state.get("notes")
        if isinstance(notes, list) and notes:
            st.caption("Latest automation notes:")
            for note in notes[-4:]:
                st.caption(f"- {str(note)}")

    # ── Outreach Flow ─────────────────────────────────────────────────
    with st.expander("Outreach Flow — Screenshot + Gmail"):
        uploader_key = f"ss_{place_id or _slug(business)}"
        uploaded = st.file_uploader(
            "Lovable.ai screenshot",
            type=["png", "jpg", "jpeg", "webp"],
            key=uploader_key,
            label_visibility="visible",
        )
        draft = _email_draft(selected)
        if uploaded:
            st.markdown('<div class="review-card">', unsafe_allow_html=True)
            st.markdown('<div class="section-label">Final Review</div>', unsafe_allow_html=True)
            st.image(uploaded, use_container_width=True)
            st.markdown(
                '<div class="section-label" style="margin-top:12px;">AI Email Draft</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                f'<div class="email-pre">{html.escape(draft)}</div>',
                unsafe_allow_html=True,
            )
            biz_email = str(selected.get("email") or "")
            subject = f"Quick question about {business}'s online presence"
            mailto = f"mailto:{biz_email}?subject={quote(subject)}&body={quote(draft)}"
            st.markdown(
                f'<a class="send-btn" href="{mailto}" target="_blank">&#9993; Send via Gmail</a>',
                unsafe_allow_html=True,
            )
            st.markdown("</div>", unsafe_allow_html=True)
        else:
            st.markdown(
                '<div style="font-family:var(--mono);font-size:10.5px;color:rgba(156,163,175,.5);'
                'padding:8px 0 10px;line-height:1.7;">'
                "Upload the Lovable screenshot above to preview<br/>the final outreach card + Gmail send.</div>",
                unsafe_allow_html=True,
            )
            st.markdown(
                '<div class="section-label" style="margin-top:4px;">Draft Preview</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                f'<div class="email-pre">{html.escape(draft)}</div>',
                unsafe_allow_html=True,
            )

    st.markdown('<div style="height:24px;"></div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Ops tray
# ---------------------------------------------------------------------------

def _render_ops_tray(
    conversion_rate: float,
    cost_per_contact: float,
    projected_mrr: float,
    selected: dict | None,
    perf_metrics: dict[str, float] | None = None,
) -> None:
    st.markdown(
        '<div class="ops-tray-header">'
        '<div class="ops-tray-title">'
        '<span class="pill-dot" style="width:5px;height:5px;"></span>'
        "Operations"
        "</div>"
        '</div>',
        unsafe_allow_html=True,
    )
    # Quiet dismiss control — sits as micro chrome, not a primary CTA.
    hide_cols = st.columns([5, 1])
    with hide_cols[1]:
        if st.button("✕", key="hide_ops_tray_btn", help="Hide operations panel"):
            st.session_state.ops_hidden = True
            st.rerun()
    st.toggle(
        "Advanced Controls",
        key="ops_advanced_mode",
        help="Show lower-frequency tools (scraper, model tuning, performance diagnostics, manual camera nudges).",
    )

    # ── Scraper Controls ──────────────────────────────────────────────
    if st.session_state.ops_advanced_mode:
        st.markdown("**Scraper Controls**")
        scrape_query = st.text_input(
            "Query",
            value="small businesses in Ann Arbor MI",
            key="scrape_query",
            label_visibility="visible",
        )
        scrape_max = st.number_input(
            "Max Leads",
            min_value=5,
            max_value=200,
            value=20,
            step=5,
            key="scrape_max",
        )
        if st.button("Run Scraper", use_container_width=True, key="run_scraper"):
            try:
                from aade.scraper import fetch_leads
                from aade.database import upsert_lead

                before_total = 0
                with db.connect() as conn:
                    before_total = int(db.get_aggregate_stats(conn)["total_leads"])
                with st.spinner("Scraping…"):
                    raw_leads = fetch_leads(
                        query=scrape_query,
                        max_results=int(scrape_max),
                    )
                kept = 0
                skipped_far = 0
                with db.connect() as conn:
                    for lead in raw_leads:
                        if not _is_near_ann_arbor(getattr(lead, "address", None)):
                            skipped_far += 1
                            continue
                        upsert_lead(
                            conn,
                            place_id=str(lead.place_id),
                            business_name=str(lead.business_name),
                            address=(str(lead.address) if lead.address else None),
                            phone=(str(lead.phone) if lead.phone else None),
                            website=(str(lead.website) if lead.website else None),
                            niche=(str(lead.niche) if lead.niche else None),
                            business_description=(
                                str(lead.business_description)
                                if getattr(lead, "business_description", None)
                                else None
                            ),
                            mission_statement=(
                                str(lead.mission_statement)
                                if getattr(lead, "mission_statement", None)
                                else None
                            ),
                            street_name=(str(lead.street_name) if lead.street_name else None),
                            maps_url=(str(lead.maps_url) if lead.maps_url else None),
                        )
                        kept += 1
                    after_total = int(db.get_aggregate_stats(conn)["total_leads"])
                delta = max(0, after_total - before_total)
                st.success(
                    f"Scraped {len(raw_leads)} · kept {kept} near Ann Arbor · "
                    f"new in DB {delta} · skipped far {skipped_far}"
                )
                st.rerun()
            except Exception as exc:
                st.error(f"Scraper error: {exc}")

    # ── Financial Assumptions ─────────────────────────────────────────
    if st.session_state.ops_advanced_mode:
        st.markdown("**Financial Assumptions**")
        with st.form("ops_finance_form", clear_on_submit=False):
            c1, c2, c3 = st.columns(3)
            val_conv = c1.number_input(
                "Conv %", min_value=0.0, max_value=100.0,
                value=float(conversion_rate), step=0.1, format="%.1f",
            )
            val_cost = c2.number_input(
                "$/Contact", min_value=0.0,
                value=float(cost_per_contact), step=0.01, format="%.2f",
            )
            val_mrr = c3.number_input(
                "MRR $", min_value=0.0,
                value=float(projected_mrr), step=10.0, format="%.0f",
            )
            if st.form_submit_button("Save", use_container_width=True):
                with db.connect() as conn:
                    db.set_dashboard_value(conn, DB_KEY_CONVERSION, f"{val_conv:.1f}")
                    db.set_dashboard_value(conn, DB_KEY_COST_PER_CONTACT, f"{val_cost:.2f}")
                    db.set_dashboard_value(conn, D_MRR, f"{val_mrr:.0f}")
                st.success("Saved.")
                st.rerun()

    # ── Model Settings ────────────────────────────────────────────────
    if st.session_state.ops_advanced_mode:
        st.markdown("**Model Settings**")
        current_niche_models = os.getenv("AADE_NICHE_STYLE_MODELS") or ",".join(get_niche_style_models())
        current_outreach_models = os.getenv("AADE_OUTREACH_MODELS") or ",".join(get_outreach_models())
        current_design_models = os.getenv("AADE_DESIGN_BRIEF_MODELS") or ",".join(
            get_design_brief_models()
        )
        current_image_model = os.getenv("AADE_IMAGE_MODEL") or get_image_model()
        with st.form("ops_model_settings_form", clear_on_submit=False):
            niche_models = st.text_input(
                "Niche Style Models (comma-separated)",
                value=current_niche_models,
            )
            outreach_models = st.text_input(
                "Outreach Models (comma-separated)",
                value=current_outreach_models,
            )
            design_models = st.text_input(
                "Design Brief Models (comma-separated)",
                value=current_design_models,
            )
            image_model = st.text_input(
                "Image Model",
                value=current_image_model,
            )
            if st.form_submit_button("Save Model Settings", use_container_width=True):
                os.environ["AADE_NICHE_STYLE_MODELS"] = niche_models.strip()
                os.environ["AADE_OUTREACH_MODELS"] = outreach_models.strip()
                os.environ["AADE_DESIGN_BRIEF_MODELS"] = design_models.strip()
                os.environ["AADE_IMAGE_MODEL"] = image_model.strip()
                _upsert_env_key("AADE_NICHE_STYLE_MODELS", niche_models.strip())
                _upsert_env_key("AADE_OUTREACH_MODELS", outreach_models.strip())
                _upsert_env_key("AADE_DESIGN_BRIEF_MODELS", design_models.strip())
                _upsert_env_key("AADE_IMAGE_MODEL", image_model.strip())
                st.success("Saved model settings. New calls use these models immediately.")
                st.rerun()

    if selected is None:
        st.caption("Select a lead on the map to unlock Money + Automation actions.")
    # ── Money Tab ─────────────────────────────────────────────────────
    if selected is not None:
        with st.expander("Money Tab — Revenue Leak"):
            niche = str(selected.get("niche") or "other")
            search_vol, avg_ticket = _niche_benchmarks(niche)
            lost_traffic, lost_revenue = _revenue_leak(niche)
            st.markdown(
                (
                    f"**Lost Traffic:** `{lost_traffic}`/mo  \n"
                    f"**Lost Revenue:** `${lost_revenue:,.0f}`/mo  \n"
                    f"Business is losing an estimated **${lost_revenue:,.0f}/mo** by being invisible."
                )
            )
            st.caption(
                f"Inputs -> Search Vol: {search_vol}/mo · Avg Ticket: ${avg_ticket} · CTR: 15% · Conv: 5%"
            )
            if st.button(
                "Propose Retainer",
                key=f"propose_retainer_{_slug(str(selected.get('business_name') or 'biz'))}",
                use_container_width=True,
            ):
                proposal = _retainer_proposal(selected, lost_revenue)
                st.code(proposal, language=None)

    # ── Performance ───────────────────────────────────────────────────
    if st.session_state.ops_advanced_mode:
        st.markdown("**Performance**")
        metrics = perf_metrics or {}
        load_ms = float(metrics.get("load_stats_ms", 0.0))
        scheduler_ms = float(metrics.get("scheduler_ms", 0.0))
        map_prep_ms = float(metrics.get("map_prep_ms", 0.0))
        frame_ms = float(metrics.get("frame_ms", 0.0))
        st.markdown(
            (
                f"**Stats Load:** `{load_ms:.1f}ms`  \n"
                f"**Scheduler Tick:** `{scheduler_ms:.1f}ms`  \n"
                f"**Map Prep:** `{map_prep_ms:.1f}ms`  \n"
                f"**Frame Total:** `{frame_ms:.1f}ms`"
            )
        )
        if frame_ms > 0:
            quality = "Fast"
            if frame_ms > 220:
                quality = "Heavy"
            elif frame_ms > 120:
                quality = "Moderate"
            st.caption(f"Runtime profile: {quality}")

    # ── Automation Flow ────────────────────────────────────────────────
    if selected is not None:
        with st.expander("Automation Flow — Verified -> Active"):
            place_id = str(selected.get("place_id") or "")
            business = str(selected.get("business_name") or "Unknown")
            current_status = str(selected.get("status") or "new")
            default_idx = (
                STATUS_CHOICES.index(current_status)
                if current_status in STATUS_CHOICES
                else 0
            )
            target_status = st.selectbox(
                "Set lead status",
                STATUS_CHOICES,
                index=default_idx,
                key=f"ops_status_{place_id}",
            )
            st.markdown(
                (
                    f"**Lead:** `{business}`  \n"
                    f"**Current Status:** `{current_status}`  \n"
                    f"**Trigger Rule:** `verified -> active` only"
                )
            )
            webhook_flags = [
                ("Clay", bool(CLAY_ENRICH_WEBHOOK)),
                ("11x", bool(ELEVENX_CALL_WEBHOOK)),
                ("Agent Frank", bool(AGENT_FRANK_EMAIL_WEBHOOK)),
            ]
            status_line = " · ".join(
                f"{name}:{'OK' if ok else 'missing'}" for name, ok in webhook_flags
            )
            st.caption(f"Webhook config: {status_line}")

            if st.button("Apply Status / Run Flow", use_container_width=True, key=f"ops_run_{place_id}"):
                with db.connect() as conn:
                    prev = db.set_lead_status(conn, place_id, target_status)
                    if (prev or "").lower() == "verified" and target_status.lower() == "active":
                        with st.spinner("Running: Clay -> Visual Hook -> 11x -> Agent Frank"):
                            result = run_verified_to_active_flow(
                                conn,
                                place_id=place_id,
                                lead=selected,
                            )
                        st.success("Automation flow completed.")
                        st.write(
                            {
                                "enrichment_ok": result.enrichment_ok,
                                "owner_name": result.owner_name,
                                "owner_linkedin": result.owner_linkedin,
                                "direct_email": result.direct_email,
                                "mockup_asset": result.mockup_asset,
                                "call_tag": result.call_tag,
                                "email_fired": result.email_fired,
                            }
                        )
                        for note in result.notes:
                            st.caption(f"- {note}")
                    elif (prev or "").lower() != "interested" and target_status.lower() == "interested":
                        with st.spinner("Running interested trigger: Agent Frank Email #1"):
                            result = run_interested_email_only(
                                conn,
                                place_id=place_id,
                                lead=selected,
                            )
                        st.success("Interested trigger completed.")
                        st.write(
                            {
                                "owner_name": result.owner_name,
                                "owner_linkedin": result.owner_linkedin,
                                "direct_email": result.direct_email,
                                "mockup_asset": result.mockup_asset,
                                "call_tag": result.call_tag,
                                "email_fired": result.email_fired,
                            }
                        )
                        for note in result.notes:
                            st.caption(f"- {note}")
                    else:
                        st.info(
                            f"Status updated ({prev or 'unknown'} -> {target_status}). "
                            "Flow did not run because triggers are verified -> active or any -> interested."
                        )
                st.rerun()

    # ── Camera Controls ───────────────────────────────────────────────
    with st.expander("Camera Controls"):
        rec_label = "Recording: ON" if st.session_state.recording_mode else "Recording Mode"
        if st.button(rec_label, key="toggle_recording_mode", use_container_width=True):
            st.session_state.recording_mode = not st.session_state.recording_mode
            if st.session_state.recording_mode:
                st.session_state.explore_mode = True
                st.session_state.explore_preset = "LinkedIn Cinematic"
                preset = EXPLORE_PRESETS["LinkedIn Cinematic"]
                st.session_state.explore_speed_sec = int(preset["speed_sec"])
                st.session_state.explore_pitch_amp = float(preset["pitch_amp"])
                st.session_state.explore_zoom_amp = float(preset["zoom_amp"])
                st.session_state._recording_last_snap_ts = 0.0
            st.rerun()
        explore_label = "Explore: ON" if st.session_state.explore_mode else "Explore"
        if st.button(explore_label, key="toggle_explore_mode", use_container_width=True):
            next_mode = not st.session_state.explore_mode
            st.session_state.explore_mode = next_mode
            if next_mode:
                now = time.perf_counter()
                st.session_state._explore_phase = 0.0
                st.session_state._next_explore_tick = now
                st.session_state._explore_last_motion_ts = 0.0
                # Start the orbit below the Street View gate so the opening
                # move is a glide rather than an instant cut to the iframe.
                st.session_state._explore_base_zoom = min(
                    float(st.session_state.cam_zoom), STREET_VIEW_THRESHOLD - 1.0
                )
                st.session_state.cam_zoom = float(st.session_state._explore_base_zoom)
                st.session_state.current_bearing = float(st.session_state.cam_bearing)
                st.session_state.current_zoom = float(st.session_state.cam_zoom)
                st.session_state._cam_transition_ms = 600
            else:
                _clear_motion_state()
                st.session_state.recording_mode = False
                st.session_state.cam_lat = float(_CAM_DEFAULTS["lat"])
                st.session_state.cam_lon = float(_CAM_DEFAULTS["lon"])
                st.session_state.cam_bearing = 0.0
                st.session_state.cam_pitch = float(_CAM_DEFAULTS["pitch"])
                st.session_state.cam_zoom = float(_CAM_DEFAULTS["zoom"])
                st.session_state.current_bearing = float(st.session_state.cam_bearing)
                st.session_state.current_zoom = float(st.session_state.cam_zoom)
                st.session_state._cam_transition_ms = 0
            st.rerun()
        preset_names = list(EXPLORE_PRESETS.keys())
        current_preset = str(st.session_state.get("explore_preset", "LinkedIn Smooth"))
        if current_preset not in preset_names:
            current_preset = "LinkedIn Smooth"
        chosen_preset = st.selectbox(
            "Cinematic Preset",
            preset_names,
            index=preset_names.index(current_preset),
            key="explore_preset_select",
        )
        if chosen_preset != st.session_state.get("explore_preset"):
            st.session_state.explore_preset = chosen_preset
            preset = EXPLORE_PRESETS[chosen_preset]
            st.session_state.explore_speed_sec = int(preset["speed_sec"])
            st.session_state.explore_pitch_amp = float(preset["pitch_amp"])
            st.session_state.explore_zoom_amp = float(preset["zoom_amp"])
            st.rerun()
        speed = st.slider(
            "Explore Orbit Speed (seconds per full revolution)",
            min_value=5,
            max_value=60,
            value=int(st.session_state.explore_speed_sec),
            step=1,
            key="explore_speed_sec",
            help="Lower is faster spin. Higher is slower, smoother cinematic orbit.",
        )
        st.caption(f"Current orbit: 1 full rotation every {int(speed)}s")
        st.toggle(
            "Sunset Mode",
            key="sunset_mode",
            help="Enable cinematic night styling with warm neon contrast.",
        )
        st.toggle(
            "High-Precision Map",
            key="high_precision_map",
            help=(
                "Off = fastest startup. On = more accurate geocoding, but slower initialization."
            ),
        )
        pitch = float(st.session_state.cam_pitch)
        bearing = float(st.session_state.cam_bearing)
        zoom = float(st.session_state.cam_zoom)
        compass = _bearing_label(bearing)

        st.markdown(
            f'<div style="font-family:var(--mono);font-size:10px;text-align:center;'
            f'color:rgba(52,211,153,.7);letter-spacing:.06em;margin-bottom:8px;">'
            f'{compass} &nbsp;·&nbsp; {pitch:.0f}° tilt &nbsp;·&nbsp; z{zoom:.1f}'
            f"</div>",
            unsafe_allow_html=True,
        )
        if st.session_state.ops_advanced_mode:
            st.markdown('<div class="cam-label">Tilt</div>', unsafe_allow_html=True)
            c1, c2 = st.columns(2)
            if c1.button("▲ Up", key="c_tilt_u", use_container_width=True):
                st.session_state.cam_pitch = min(MAX_CAM_PITCH, pitch + 8.0)
                st.session_state._cam_transition_ms = MANUAL_NUDGE_MS
            if c2.button("▼ Down", key="c_tilt_d", use_container_width=True):
                st.session_state.cam_pitch = max(MIN_CAM_PITCH, pitch - 8.0)
                st.session_state._cam_transition_ms = MANUAL_NUDGE_MS

            st.markdown('<div class="cam-label" style="margin-top:6px;">Rotate</div>', unsafe_allow_html=True)
            c3, c4 = st.columns(2)
            if c3.button("↺ Left", key="c_rot_l", use_container_width=True):
                st.session_state.cam_bearing = bearing - 22.5
                st.session_state.current_bearing = float(st.session_state.cam_bearing)
                st.session_state._cam_transition_ms = MANUAL_NUDGE_MS
            if c4.button("↻ Right", key="c_rot_r", use_container_width=True):
                st.session_state.cam_bearing = bearing + 22.5
                st.session_state.current_bearing = float(st.session_state.cam_bearing)
                st.session_state._cam_transition_ms = MANUAL_NUDGE_MS

            st.markdown('<div class="cam-label" style="margin-top:6px;">Zoom</div>', unsafe_allow_html=True)
            c5, c6 = st.columns(2)
            if c5.button("+ In", key="c_zoom_i", use_container_width=True):
                st.session_state.cam_zoom = min(20.0, zoom + 0.5)
                st.session_state.current_zoom = float(st.session_state.cam_zoom)
                st.session_state._cam_transition_ms = MANUAL_NUDGE_MS
            if c6.button("- Out", key="c_zoom_o", use_container_width=True):
                st.session_state.cam_zoom = max(8.0, zoom - 0.5)
                st.session_state.current_zoom = float(st.session_state.cam_zoom)
                st.session_state._cam_transition_ms = MANUAL_NUDGE_MS

            st.markdown('<div class="cam-label" style="margin-top:6px;">Map Style</div>', unsafe_allow_html=True)
            c7, c8, c9 = st.columns(3)
            if c7.button("Sat", key="c_sat", use_container_width=True):
                st.session_state.cam_map_style = "satellite"
            if c8.button("Dark", key="c_dark", use_container_width=True):
                st.session_state.cam_map_style = "dark"
            if c9.button("Street", key="c_st", use_container_width=True):
                st.session_state.cam_map_style = "street"

        st.markdown('<div style="height:4px;"></div>', unsafe_allow_html=True)
        if st.button("Reset View", key="c_reset", use_container_width=True):
            _clear_motion_state()
            st.session_state.explore_mode = False
            st.session_state.recording_mode = False
            for k, v in _CAM_DEFAULTS.items():
                st.session_state[f"cam_{k}"] = v
            st.session_state.current_bearing = float(st.session_state.cam_bearing)
            st.session_state.current_zoom = float(st.session_state.cam_zoom)
            st.session_state._cam_transition_ms = RESET_GLIDE_MS
        if st.button("Manual Override", key="manual_override_btn", use_container_width=True):
            _clear_motion_state()
            st.session_state.explore_mode = False
            st.session_state.recording_mode = False
            st.session_state.cam_map_style = "satellite"
            st.session_state.cam_lat = float(_CAM_DEFAULTS["lat"])
            st.session_state.cam_lon = float(_CAM_DEFAULTS["lon"])
            st.session_state.cam_bearing = 0.0
            st.session_state.cam_pitch = float(_CAM_DEFAULTS["pitch"])
            st.session_state.cam_zoom = float(_CAM_DEFAULTS["zoom"])
            st.session_state.current_bearing = float(st.session_state.cam_bearing)
            st.session_state.current_zoom = float(st.session_state.cam_zoom)
            st.session_state._cam_transition_ms = RESET_GLIDE_MS
            st.rerun()


# ---------------------------------------------------------------------------
# U-M watermark
# ---------------------------------------------------------------------------

def _inject_watermark() -> None:
    st.markdown(
        """
<div class="gev-lockup">
  <span class="gev-lockup-mark"></span>
  <span class="gev-lockup-name">Ann Arbor <b>Growth Engine</b></span>
  <span class="gev-lockup-sub">Lead intelligence</span>
</div>
<div class="gev-legend">
  <span class="gev-legend-title">Legend</span>
  <span class="gev-legend-row">
    <span class="gev-legend-swatch swatch-target"></span>No website · high value
  </span>
  <span class="gev-legend-row">
    <span class="gev-legend-swatch swatch-base"></span>Has website · tracked
  </span>
</div>
<div class="um-watermark">
  <span class="um-dot"></span>
  <span class="um-text">U-M CS Student &nbsp;·&nbsp; Ann Arbor</span>
</div>
<div class="hud-hidden-indicator">
  <span class="hud-hidden-indicator-dot"></span>
  HUD Hidden · Press ` to restore
</div>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Startup intro
# ---------------------------------------------------------------------------

def _inject_startup_intro(name: str = "Miles") -> None:
    safe_name = html.escape(name)
    st.markdown(
        f"""
<div id="startup-intro" class="startup-intro">
  <div class="startup-center">
    <div class="startup-logo">
      <span class="slackish p1"></span>
      <span class="slackish p2"></span>
      <span class="slackish p3"></span>
      <span class="slackish p4"></span>
    </div>
    <div class="startup-title">Welcome back, {safe_name}</div>
    <div class="startup-sub">Lead intelligence is initializing</div>
    <div class="startup-progress-wrap">
      <div class="startup-progress-track"><div class="startup-progress-fill"></div></div>
      <div id="startup-progress-text" class="startup-progress-text">Loading map tiles...</div>
    </div>
  </div>
</div>
<style>
.startup-intro {{
  position: fixed;
  inset: 0;
  z-index: 9999;
  display: grid;
  place-items: center;
  background:
    radial-gradient(circle at 30% 25%, rgba(255,203,5,0.16), transparent 38%),
    radial-gradient(circle at 70% 75%, rgba(255,203,5,0.10), transparent 45%),
    rgba(0,39,76,0.84);
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  opacity: 1;
  visibility: visible;
  transition: opacity .45s ease, visibility .45s ease;
}}
.startup-intro.ready-to-close {{
  opacity: 0;
  visibility: hidden;
  pointer-events: none;
}}
.startup-center {{
  text-align: center;
  color: #FFCB05;
  text-shadow: 0 2px 10px rgba(0,0,0,0.8);
  opacity: 1;
  animation: intro-rise .75s cubic-bezier(.16,1,.3,1) both;
}}
.startup-logo {{
  position: relative;
  width: 82px;
  height: 82px;
  margin: 0 auto 18px;
  animation: slackish-orbit 1.8s cubic-bezier(.2,.9,.25,1) infinite;
}}
.startup-logo .slackish {{
  position: absolute;
  width: 22px;
  height: 46px;
  border-radius: 999px;
  top: 18px;
  left: 30px;
  transform-origin: 50% 50%;
  box-shadow: 0 0 18px rgba(255,203,5,0.40);
  opacity: .95;
}}
.startup-logo .p1 {{ background: #FFCB05; transform: rotate(0deg) translateY(-20px); animation: slackish-build-1 1.2s cubic-bezier(.2,.9,.25,1) .00s both, slackish-pulse 1.4s ease-in-out 1.2s infinite; }}
.startup-logo .p2 {{ background: #FFD84D; transform: rotate(90deg) translateY(-20px); animation: slackish-build-2 1.2s cubic-bezier(.2,.9,.25,1) .09s both, slackish-pulse 1.4s ease-in-out 1.29s infinite; }}
.startup-logo .p3 {{ background: #F4B400; transform: rotate(180deg) translateY(-20px); animation: slackish-build-3 1.2s cubic-bezier(.2,.9,.25,1) .18s both, slackish-pulse 1.4s ease-in-out 1.38s infinite; }}
.startup-logo .p4 {{ background: #E3A700; transform: rotate(270deg) translateY(-20px); animation: slackish-build-4 1.2s cubic-bezier(.2,.9,.25,1) .27s both, slackish-pulse 1.4s ease-in-out 1.47s infinite; }}
.startup-title {{
  font-size: 30px;
  font-weight: 800;
  letter-spacing: .01em;
  margin-bottom: 6px;
  opacity: 1;
  animation: intro-text-in .55s cubic-bezier(.2,.9,.25,1) .35s forwards;
}}
.startup-sub {{
  font-family: var(--mono) !important;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: .14em;
  color: rgba(255,203,5,0.8);
  opacity: 1;
  animation: intro-text-in .55s cubic-bezier(.2,.9,.25,1) .48s forwards;
}}
.startup-progress-wrap {{
  margin-top: 14px;
  width: min(360px, 72vw);
  opacity: 0;
  animation: intro-text-in .55s cubic-bezier(.2,.9,.25,1) .62s forwards;
}}
.startup-progress-track {{
  width: 100%;
  height: 4px;
  border-radius: 999px;
  background: rgba(255,203,5,0.18);
  border: 1px solid rgba(255,203,5,0.25);
  overflow: hidden;
}}
.startup-progress-fill {{
  width: 38%;
  height: 100%;
  border-radius: 999px;
  background: linear-gradient(90deg, rgba(255,203,5,0.25), #FFCB05, rgba(255,203,5,0.25));
  box-shadow: 0 0 16px rgba(255,203,5,0.65);
  animation: startup-progress-sweep 1.2s ease-in-out infinite;
}}
.startup-progress-text {{
  margin-top: 7px;
  font-family: var(--mono) !important;
  font-size: 9px;
  letter-spacing: .1em;
  text-transform: uppercase;
  color: rgba(255,203,5,0.82);
}}
@keyframes intro-rise {{
  from {{ opacity: 0; transform: translateY(9px) scale(.985); }}
  to {{ opacity: 1; transform: translateY(0) scale(1); }}
}}
@keyframes intro-text-in {{
  from {{ opacity: 0; transform: translateY(6px); }}
  to {{ opacity: 1; transform: translateY(0); }}
}}
@keyframes slackish-orbit {{
  0% {{ transform: rotate(0deg) scale(0.92); }}
  35% {{ transform: rotate(88deg) scale(1.02); }}
  100% {{ transform: rotate(360deg) scale(0.96); }}
}}
@keyframes slackish-build-1 {{
  0% {{ opacity: 0; transform: rotate(0deg) translateY(-4px) scale(.6); }}
  100% {{ opacity: 1; transform: rotate(0deg) translateY(-20px) scale(1); }}
}}
@keyframes slackish-build-2 {{
  0% {{ opacity: 0; transform: rotate(90deg) translateY(-4px) scale(.6); }}
  100% {{ opacity: 1; transform: rotate(90deg) translateY(-20px) scale(1); }}
}}
@keyframes slackish-build-3 {{
  0% {{ opacity: 0; transform: rotate(180deg) translateY(-4px) scale(.6); }}
  100% {{ opacity: 1; transform: rotate(180deg) translateY(-20px) scale(1); }}
}}
@keyframes slackish-build-4 {{
  0% {{ opacity: 0; transform: rotate(270deg) translateY(-4px) scale(.6); }}
  100% {{ opacity: 1; transform: rotate(270deg) translateY(-20px) scale(1); }}
}}
@keyframes slackish-pulse {{
  0%, 100% {{ opacity: .7; }}
  50% {{ opacity: 1; }}
}}
@keyframes startup-progress-sweep {{
  0% {{ transform: translateX(-45%); opacity: .82; }}
  50% {{ transform: translateX(82%); opacity: 1; }}
  100% {{ transform: translateX(170%); opacity: .82; }}
}}
@media (prefers-reduced-motion: reduce) {{
  .startup-intro {{
    transition: none !important;
  }}
  .startup-logo, .startup-logo .slackish, .startup-center, .startup-title, .startup-sub {{
    animation: none !important;
  }}
}}
</style>
<script>
(function() {{
  const introSeen = window.sessionStorage && window.sessionStorage.getItem("tcc_intro_seen") === "1";
  if (introSeen) {{
    const existing = document.getElementById("startup-intro");
    if (existing) existing.classList.add("ready-to-close");
    return;
  }}
  if (window.sessionStorage) {{
    window.sessionStorage.setItem("tcc_intro_seen", "1");
  }}
  if (!window.__tccIntroStartMs) {{
    window.__tccIntroStartMs = Date.now();
  }}
  const phases = [
    "Loading map tiles...",
    "Syncing lead intelligence...",
    "Preparing tactical HUD...",
    "Calibrating camera controls..."
  ];
  let phaseIdx = 0;
  const phaseNode = document.getElementById("startup-progress-text");
  const phaseTimer = setInterval(function() {{
    if (!phaseNode) return;
    phaseIdx = (phaseIdx + 1) % phases.length;
    phaseNode.textContent = phases[phaseIdx];
  }}, 780);

  // Failsafe in case ready signal never arrives.
  setTimeout(function() {{
    const el = document.getElementById("startup-intro");
    if (!el) return;
    el.classList.add("ready-to-close");
    clearInterval(phaseTimer);
  }}, 2500);
}})();
</script>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Startup intro dismiss
# ---------------------------------------------------------------------------

def _dismiss_startup_intro() -> None:
    st.markdown(
        """
<script>
(function() {
  const el = document.getElementById("startup-intro");
  if (!el) return;
  const minMs = 300;
  const started = window.__tccIntroStartMs || Date.now();
  const elapsed = Date.now() - started;
  const wait = Math.max(0, minMs - elapsed);

  setTimeout(() => {
    const phaseNode = document.getElementById("startup-progress-text");
    if (phaseNode) phaseNode.textContent = "Finalizing render...";
    // Defer one frame so all HUD/map nodes are painted first.
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        el.classList.add("ready-to-close");
        setTimeout(() => el.remove(), 700);
      });
    });
  }, wait);
})();
</script>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    frame_t0 = time.perf_counter()
    st.set_page_config(
        page_title="Lead intelligence · Ann Arbor",
        page_icon="〽️",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    _inject_css()
    _init_camera()
    _clamp_camera_state()
    if "intro_done" not in st.session_state:
        st.session_state.intro_done = False
    if not st.session_state.intro_done:
        _inject_startup_intro("Miles")

    perf_metrics: dict[str, float] = {}
    t0 = time.perf_counter()
    s, projected_mrr, conversion_rate, cost_per_contact, recent = _load_stats()
    perf_metrics["load_stats_ms"] = (time.perf_counter() - t0) * 1000.0
    if "last_scheduler_tick" not in st.session_state:
        st.session_state.last_scheduler_tick = 0.0
    now_ts = float(time.time())
    perf_metrics["scheduler_ms"] = 0.0
    if st.session_state.intro_done and now_ts - float(st.session_state.last_scheduler_tick) >= 90.0:
        t_sched = time.perf_counter()
        with db.connect() as conn:
            sched = process_due_sequence_emails(conn, limit=120)
        st.session_state.last_scheduler_tick = now_ts
        perf_metrics["scheduler_ms"] = (time.perf_counter() - t_sched) * 1000.0
        if sched.sent_count > 0:
            st.toast(f"Sequence scheduler sent {sched.sent_count} follow-up email(s).", icon="📨")

    t_map = time.perf_counter()
    high_precision = bool(st.session_state.high_precision_map)
    warmup_step = int(st.session_state.coord_warmup_step)
    explore_on = bool(st.session_state.explore_mode)
    selected_for_street: dict | None = None
    explore_tick_sec = 0.20
    if explore_on:
        now_orbit = time.perf_counter()
        speed_sec = max(5.0, float(st.session_state.explore_speed_sec))
        explore_tick_sec = EXPLORE_TICK_SEC
        next_tick = float(st.session_state.get("_next_explore_tick", now_orbit))
        if now_orbit >= next_tick:
            # Advance by real elapsed time, not by tick count: Streamlit rerun
            # pacing drifts, and a fixed step turns that drift into speed jumps.
            last_motion = float(st.session_state.get("_explore_last_motion_ts", 0.0))
            dt = explore_tick_sec if last_motion <= 0.0 else (now_orbit - last_motion)
            dt = max(0.05, min(0.6, dt))
            st.session_state._explore_last_motion_ts = now_orbit

            phase = float(st.session_state.get("_explore_phase", 0.0)) + dt
            st.session_state._explore_phase = phase

            pitch_amp = max(0.0, float(st.session_state.get("explore_pitch_amp", 7.5)))
            zoom_amp = max(0.0, float(st.session_state.get("explore_zoom_amp", 0.08)))
            base_pitch = float(_CAM_DEFAULTS["pitch"])
            base_zoom = float(st.session_state.get("_explore_base_zoom", st.session_state.cam_zoom))

            # Bearing must accumulate unwrapped. A modulo here makes the linear
            # tween take the long way round once per revolution (a 359 deg whip).
            target_bearing = float(st.session_state.current_bearing) + (360.0 / speed_sec) * dt

            # Pitch and zoom share the orbit phase clock so all three channels
            # stay in sync even when frame pacing is uneven.
            sway = 2.0 * math.pi * phase
            target_pitch = base_pitch + pitch_amp * math.sin(sway / max(6.0, speed_sec * 1.5))
            target_zoom = base_zoom + zoom_amp * math.sin(sway / max(8.0, speed_sec * 2.0))

            # Never let the orbit drift across the Street View gate; crossing it
            # swaps the deck canvas for an iframe and cuts the shot mid-flight.
            zoom_ceiling = min(SPIRAL_ZOOM_MAX, STREET_VIEW_THRESHOLD - 0.4)

            st.session_state.current_bearing = float(target_bearing)
            st.session_state.cam_bearing = float(target_bearing)
            st.session_state.cam_pitch = max(MIN_CAM_PITCH, min(MAX_CAM_PITCH, target_pitch))
            st.session_state.cam_zoom = max(SPIRAL_ZOOM_MIN, min(zoom_ceiling, target_zoom))
            st.session_state.current_zoom = float(st.session_state.cam_zoom)

            # Tween slightly longer than the step it covers so consecutive
            # tweens overlap instead of stopping dead between frames.
            tween_ms = int(max(140, min(700, dt * 1000.0 * EXPLORE_TWEEN_OVERLAP)))
            # A long hop to another lead outranks the orbit tick, otherwise the
            # short tween yanks the camera across town.
            fly_until = float(st.session_state.get("_cam_fly_until", 0.0))
            if now_orbit < fly_until:
                tween_ms = max(tween_ms, int((fly_until - now_orbit) * 1000.0))
            st.session_state._cam_transition_ms = tween_ms
            st.session_state._next_explore_tick = now_orbit + explore_tick_sec
    else:
        st.session_state._cam_transition_ms = 0
        st.session_state._last_orbit_ts = time.perf_counter()
        st.session_state._explore_loop_count = 0
        st.session_state._explore_last_motion_ts = 0.0
    if high_precision:
        network_lookups = 10
    else:
        # Staged refinement: instant first paint, then 1 quick warm-up pass.
        network_lookups = [0, 6][min(warmup_step, 1)]
    map_rows = _attach_coords(recent[:MAX_MAP_ROWS], network_lookups=network_lookups)

    if st.session_state.recording_mode and map_rows:
        now_rec = time.perf_counter()
        last_rec = float(st.session_state.get("_recording_last_snap_ts", 0.0))
        if now_rec - last_rec >= RECORDING_SNAP_SECONDS:
            hv = [r for r in map_rows if int(r.get("no_website_flag") or 0) == 1]
            targets = hv or map_rows
            idx = int(st.session_state.get("_recording_target_idx", 0)) % len(targets)
            target = targets[idx]
            st.session_state._recording_target_idx = (idx + 1) % len(targets)
            st.session_state._recording_last_snap_ts = now_rec
            st.session_state.selected_place_id = str(target.get("place_id") or "")
            st.session_state.selected_business_name = str(target.get("business_name") or "")
            st.session_state.selected_lat = float(target.get("latitude", st.session_state.cam_lat))
            st.session_state.selected_lon = float(target.get("longitude", st.session_state.cam_lon))
            st.session_state.cam_lat = st.session_state.selected_lat
            st.session_state.cam_lon = st.session_state.selected_lon
            st.session_state.cam_zoom = 16.0
            st.session_state.current_zoom = float(st.session_state.cam_zoom)
            st.session_state._explore_base_zoom = float(st.session_state.cam_zoom)
            st.session_state._cam_transition_ms = FOCUS_FLY_MS
            st.session_state._cam_fly_until = now_rec + (FOCUS_FLY_MS / 1000.0)

    # ── Click-to-focus: zoom to selected business ─────────────────────
    map_slot = st.empty()
    selected = None
    map_event = None
    current_zoom = float(st.session_state.get("current_zoom", st.session_state.cam_zoom))
    if (
        explore_on
        and current_zoom >= STREET_VIEW_THRESHOLD
        and GOOGLE_PLACES_API_KEY
    ):
        selected_for_street = _selected_from_state(map_rows)
        if selected_for_street:
            _render_street_view_fullscreen(
                slot=map_slot,
                key=GOOGLE_PLACES_API_KEY,
                business_name=str(selected_for_street.get("business_name") or "Unknown Business"),
                lat=float(selected_for_street.get("latitude", st.session_state.cam_lat)),
                lon=float(selected_for_street.get("longitude", st.session_state.cam_lon)),
            )
            selected = selected_for_street
        else:
            map_event = _render_map(map_rows, slot=map_slot)
            selected = _selected_from_event(map_rows, map_event) or _selected_from_state(map_rows)
    else:
        map_event = _render_map(map_rows, slot=map_slot)
        selected = _selected_from_event(map_rows, map_event) or _selected_from_state(map_rows)
    perf_metrics["map_prep_ms"] = (time.perf_counter() - t_map) * 1000.0

    current_pid = str(selected.get("place_id") or "") if selected else ""
    prev_pid = str(st.session_state.get("_prev_pid", ""))
    if selected and current_pid != prev_pid:
        st.session_state.cam_lat = float(selected["latitude"])
        st.session_state.cam_lon = float(selected["longitude"])
        st.session_state.cam_zoom = 15.5
        st.session_state.cam_pitch = max(MIN_CAM_PITCH, min(MAX_CAM_PITCH, 62.0))
        st.session_state._explore_base_zoom = float(st.session_state.cam_zoom)
        st.session_state.current_zoom = float(st.session_state.cam_zoom)
        if not explore_on:
            # Explore drives its own tween each tick; only glide on manual clicks.
            st.session_state._cam_transition_ms = FOCUS_FLY_MS
        st.session_state.selected_place_id = current_pid
        st.session_state.selected_business_name = str(selected.get("business_name") or "")
        st.session_state.selected_lat = float(selected.get("latitude", st.session_state.cam_lat))
        st.session_state.selected_lon = float(selected.get("longitude", st.session_state.cam_lon))
        # Cinematic focus mode: temporarily slow orbit around newly selected lead.
        st.session_state._focus_orbit_until = time.perf_counter() + 6.0
        st.session_state._focus_orbit_pid = current_pid
    st.session_state["_prev_pid"] = current_pid

    # ── Pill bar (top center) ─────────────────────────────────────────
    with st.container(key="pill_bar"):
        _render_pill(
            total=int(s["total_leads"]),
            flagged=int(s["no_website"]),
            mrr=float(projected_mrr),
        )

    # ── Intelligence panel (right) ────────────────────────────────────
    if selected is not None:
        with st.container(key="intel_panel"):
            _render_intel(selected, recent)

    # ── Operations tray (bottom left) ────────────────────────────────
    perf_metrics["frame_ms"] = (time.perf_counter() - frame_t0) * 1000.0
    if bool(st.session_state.get("ops_hidden")):
        with st.container(key="ops_show_btn"):
            if st.button("Show Operations", key="show_ops_tray_btn", use_container_width=True):
                st.session_state.ops_hidden = False
                st.rerun()
    else:
        with st.container(key="ops_tray"):
            _render_ops_tray(
                conversion_rate,
                cost_per_contact,
                projected_mrr,
                selected,
                perf_metrics=perf_metrics,
            )

    # ── Static HTML overlays ──────────────────────────────────────────
    _inject_watermark()
    if bool(st.session_state.recording_mode):
        _render_recording_overlay(selected)
        next_eta = RECORDING_SNAP_SECONDS - (
            time.perf_counter() - float(st.session_state.get("_recording_last_snap_ts", 0.0))
        )
        _render_recording_health(
            selected=selected,
            explore_on=bool(st.session_state.explore_mode),
            recording_on=bool(st.session_state.recording_mode),
            current_zoom=float(st.session_state.get("current_zoom", st.session_state.cam_zoom)),
            next_snap_eta=next_eta,
            street_key_available=bool(GOOGLE_PLACES_API_KEY),
        )
    _inject_nav_js(
        selected,
        bool(st.session_state.explore_mode),
        float(st.session_state.explore_speed_sec),
        bool(st.session_state.recording_mode),
    )

    intro_ready = bool(map_rows)
    if not st.session_state.intro_done and intro_ready:
        _dismiss_startup_intro()
        st.session_state.intro_done = True

    # Background-like warmup via bounded auto-reruns.
    if not high_precision and warmup_step < 1:
        st.session_state.coord_warmup_step = warmup_step + 1
        st.rerun()
    elif not high_precision and not st.session_state.coord_warmup_done:
        st.session_state.coord_warmup_done = True
        st.toast("Map precision warm-up complete.", icon="✅")

    if not MAPBOX_API_KEY:
        st.toast("Set MAPBOX_API_KEY for satellite imagery.", icon="⚠️")
    if not GOOGLE_STREET_VIEW_KEY:
        st.toast("Set GOOGLE_EMBED_API_KEY for Street View.", icon="⚠️")
    if explore_on and not GOOGLE_PLACES_API_KEY:
        st.toast("Set GOOGLE_PLACES_API_KEY for cinematic Street View gateway.", icon="⚠️")

    # Persistent explore loop with throttled rerun; state carries motion across frames.
    if bool(st.session_state.get("explore_mode", False)):
        now_guard = time.perf_counter()
        last_tick = float(st.session_state.get("_explore_last_tick_ts", 0.0))
        loop_count = int(st.session_state.get("_explore_loop_count", 0))
        if now_guard - last_tick > 1.8:
            loop_count = 0
        loop_count += 1
        st.session_state._explore_loop_count = loop_count
        st.session_state._explore_last_tick_ts = now_guard
        # Safety guard: break out if a rerun-stall causes excessive looping.
        if loop_count > 240:
            st.session_state.explore_mode = False
            st.session_state._explore_loop_count = 0
            st.session_state._cam_transition_ms = 0
            st.toast("Explore auto-recovered from a render stall.", icon="⚠️")
        else:
            now_loop = time.perf_counter()
            wait = max(
                0.0,
                min(
                    0.45,
                    float(st.session_state.get("_next_explore_tick", now_loop + explore_tick_sec)) - now_loop,
                ),
            )
            if wait > 0:
                time.sleep(wait)
            st.rerun()


if __name__ == "__main__":
    main()
