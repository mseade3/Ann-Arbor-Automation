"""
SQLite persistence: leads, outreach history, and dashboard inputs.

A unique `place_id` (from Google or a deterministic hash) prevents duplicate
processing; `last_contacted_at` enforces a minimum gap before re-contact
(default 90 days).
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator, Iterable, Mapping

from aade.config import CONTACT_COOLDOWN_DAYS, DB_PATH


SCHEMA_VERSION = 1

DDL = f"""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
  version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  place_id TEXT NOT NULL UNIQUE,
  business_name TEXT NOT NULL,
  address TEXT,
  phone TEXT,
  website TEXT,
  has_website INTEGER NOT NULL DEFAULT 0,
  niche TEXT,
  business_description TEXT,
  mission_statement TEXT,
  design_color_theme TEXT,
  design_font_family TEXT,
  design_hero_image_prompt TEXT,
  design_brief_json TEXT,
  owner_name TEXT,
  owner_linkedin TEXT,
  direct_email TEXT,
  mockup_asset TEXT,
  call_log_tag TEXT,
  automation_last_run_at TEXT,
  automation_state_json TEXT,
  street_name TEXT,
  maps_url TEXT,
  no_website_flag INTEGER NOT NULL DEFAULT 0,
  rating REAL,
  review_count INTEGER,
  status TEXT NOT NULL DEFAULT 'new',
  first_seen_at TEXT NOT NULL,
  last_contacted_at TEXT,
  last_outreach_text TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leads_status ON leads (status);
CREATE INDEX IF NOT EXISTS idx_leads_no_website ON leads (no_website_flag);
CREATE INDEX IF NOT EXISTS idx_leads_updated_at ON leads (updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_leads_status_updated ON leads (status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_leads_contacted_updated ON leads (last_contacted_at, updated_at DESC);

CREATE TABLE IF NOT EXISTS dashboard_inputs (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class Lead:
    """Normalized lead for application logic (mirrors `leads` row)."""

    place_id: str
    business_name: str
    address: str | None
    phone: str | None
    website: str | None
    has_website: bool
    niche: str | None
    business_description: str | None
    mission_statement: str | None
    design_color_theme: str | None
    design_font_family: str | None
    design_hero_image_prompt: str | None
    design_brief_json: str | None
    owner_name: str | None
    owner_linkedin: str | None
    direct_email: str | None
    mockup_asset: str | None
    call_log_tag: str | None
    automation_last_run_at: str | None
    automation_state_json: str | None
    street_name: str | None
    maps_url: str | None
    no_website_flag: bool
    rating: float | None
    review_count: int | None
    status: str
    first_seen_at: str
    last_contacted_at: str | None


def stable_place_id(*, name: str, address: str | None, maps_url: str | None) -> str:
    """
    When Google does not return a `place_id`, build a stable surrogate.

    Not a real Google `place_id`, but unique per business location for this DB.
    """
    key = f"{(name or '').strip().lower()}|{(address or '').strip().lower()}"
    if maps_url:
        key += f"|{maps_url}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


@contextmanager
def connect(db_path: Path | None = None) -> Generator[sqlite3.Connection, None, None]:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(DDL)
        _ensure_version(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_version(conn: sqlite3.Connection) -> None:
    cur = conn.execute("SELECT 1 FROM schema_meta LIMIT 1")
    if cur.fetchone() is None:
        conn.execute("INSERT INTO schema_meta (version) VALUES (?)", (SCHEMA_VERSION,))
    _ensure_column(conn, "leads", "business_description", "TEXT")
    _ensure_column(conn, "leads", "mission_statement", "TEXT")
    _ensure_column(conn, "leads", "design_color_theme", "TEXT")
    _ensure_column(conn, "leads", "design_font_family", "TEXT")
    _ensure_column(conn, "leads", "design_hero_image_prompt", "TEXT")
    _ensure_column(conn, "leads", "design_brief_json", "TEXT")
    _ensure_column(conn, "leads", "owner_name", "TEXT")
    _ensure_column(conn, "leads", "owner_linkedin", "TEXT")
    _ensure_column(conn, "leads", "direct_email", "TEXT")
    _ensure_column(conn, "leads", "mockup_asset", "TEXT")
    _ensure_column(conn, "leads", "call_log_tag", "TEXT")
    _ensure_column(conn, "leads", "automation_last_run_at", "TEXT")
    _ensure_column(conn, "leads", "automation_state_json", "TEXT")
    _ensure_column(conn, "leads", "rating", "REAL")
    _ensure_column(conn, "leads", "review_count", "INTEGER")


def _ensure_column(conn: sqlite3.Connection, table: str, col: str, col_type: str) -> None:
    cols = {str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")


def can_contact_again(
    conn: sqlite3.Connection,
    place_id: str,
    cooldown_days: int = CONTACT_COOLDOWN_DAYS,
) -> bool:
    """
    Return True if we may run outreach: never contacted, or last contact older
    than `cooldown_days`.
    """
    row = conn.execute(
        "SELECT last_contacted_at FROM leads WHERE place_id = ?",
        (place_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return True
    last = datetime.fromisoformat(str(row[0]))
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last >= timedelta(days=cooldown_days)


def upsert_lead(
    conn: sqlite3.Connection,
    *,
    place_id: str,
    business_name: str,
    address: str | None = None,
    phone: str | None = None,
    website: str | None = None,
    niche: str | None = None,
    business_description: str | None = None,
    mission_statement: str | None = None,
    street_name: str | None = None,
    maps_url: str | None = None,
    rating: float | None = None,
    review_count: int | None = None,
) -> int:
    """
    Insert or update a lead. Sets `no_website_flag` and `has_website` from `website`.
    """
    w = (website or "").strip()
    has_website = 1 if w else 0
    no_flag = 0 if has_website else 1
    now = _utc_now_iso()
    cur = conn.execute(
        """
        INSERT INTO leads (
          place_id, business_name, address, phone, website, has_website, niche,
          business_description, mission_statement, street_name, maps_url, no_website_flag,
          rating, review_count,
          first_seen_at, created_at, updated_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')
        ON CONFLICT(place_id) DO UPDATE SET
          business_name = excluded.business_name,
          address = COALESCE(excluded.address, leads.address),
          phone = COALESCE(excluded.phone, leads.phone),
          website = COALESCE(excluded.website, leads.website),
          has_website = excluded.has_website,
          niche = COALESCE(excluded.niche, leads.niche),
          business_description = COALESCE(excluded.business_description, leads.business_description),
          mission_statement = COALESCE(excluded.mission_statement, leads.mission_statement),
          street_name = COALESCE(excluded.street_name, leads.street_name),
          maps_url = COALESCE(excluded.maps_url, leads.maps_url),
          no_website_flag = excluded.no_website_flag,
          rating = COALESCE(excluded.rating, leads.rating),
          review_count = COALESCE(excluded.review_count, leads.review_count),
          updated_at = excluded.updated_at
        """,
        (
            place_id,
            business_name,
            address,
            phone,
            w or None,
            has_website,
            niche,
            business_description,
            mission_statement,
            street_name,
            maps_url,
            no_flag,
            rating,
            review_count,
            now,
            now,
            now,
        ),
    )
    return int(cur.lastrowid)


def mark_contacted(
    conn: sqlite3.Connection,
    place_id: str,
    outreach_text: str,
) -> None:
    now = _utc_now_iso()
    conn.execute(
        """
        UPDATE leads
        SET last_contacted_at = ?, last_outreach_text = ?, status = 'contacted', updated_at = ?
        WHERE place_id = ?
        """,
        (now, outreach_text, now, place_id),
    )


def save_design_brief(
    conn: sqlite3.Connection,
    place_id: str,
    *,
    color_theme: str,
    font_family: str,
    hero_image_prompt: str,
    brief_json: str,
) -> None:
    now = _utc_now_iso()
    conn.execute(
        """
        UPDATE leads
        SET design_color_theme = ?, design_font_family = ?, design_hero_image_prompt = ?,
            design_brief_json = ?, updated_at = ?
        WHERE place_id = ?
        """,
        (color_theme, font_family, hero_image_prompt, brief_json, now, place_id),
    )


def set_lead_status(conn: sqlite3.Connection, place_id: str, status: str) -> str | None:
    now = _utc_now_iso()
    row = conn.execute(
        "SELECT status FROM leads WHERE place_id = ?",
        (place_id,),
    ).fetchone()
    prev = str(row["status"]) if row and row["status"] is not None else None
    conn.execute(
        "UPDATE leads SET status = ?, updated_at = ? WHERE place_id = ?",
        (status, now, place_id),
    )
    return prev


def save_lead_automation_fields(
    conn: sqlite3.Connection,
    place_id: str,
    *,
    owner_name: str | None = None,
    owner_linkedin: str | None = None,
    direct_email: str | None = None,
    mockup_asset: str | None = None,
    call_log_tag: str | None = None,
    automation_state_json: str | None = None,
) -> None:
    now = _utc_now_iso()
    conn.execute(
        """
        UPDATE leads
        SET owner_name = COALESCE(?, owner_name),
            owner_linkedin = COALESCE(?, owner_linkedin),
            direct_email = COALESCE(?, direct_email),
            mockup_asset = COALESCE(?, mockup_asset),
            call_log_tag = COALESCE(?, call_log_tag),
            automation_state_json = COALESCE(?, automation_state_json),
            automation_last_run_at = ?,
            updated_at = ?
        WHERE place_id = ?
        """,
        (
            owner_name,
            owner_linkedin,
            direct_email,
            mockup_asset,
            call_log_tag,
            automation_state_json,
            now,
            now,
            place_id,
        ),
    )


def get_lead_by_place_id(conn: sqlite3.Connection, place_id: str) -> Lead | None:
    r = conn.execute("SELECT * FROM leads WHERE place_id = ?", (place_id,)).fetchone()
    if r is None:
        return None
    return _row_to_lead(r)


def _row_to_lead(r: sqlite3.Row) -> Lead:
    d = dict(r)
    return Lead(
        place_id=str(d["place_id"]),
        business_name=str(d["business_name"]),
        address=d.get("address"),
        phone=d.get("phone"),
        website=d.get("website"),
        has_website=bool(d.get("has_website")),
        niche=d.get("niche"),
        business_description=d.get("business_description"),
        mission_statement=d.get("mission_statement"),
        design_color_theme=d.get("design_color_theme"),
        design_font_family=d.get("design_font_family"),
        design_hero_image_prompt=d.get("design_hero_image_prompt"),
        design_brief_json=d.get("design_brief_json"),
        owner_name=d.get("owner_name"),
        owner_linkedin=d.get("owner_linkedin"),
        direct_email=d.get("direct_email"),
        mockup_asset=d.get("mockup_asset"),
        call_log_tag=d.get("call_log_tag"),
        automation_last_run_at=d.get("automation_last_run_at"),
        automation_state_json=d.get("automation_state_json"),
        street_name=d.get("street_name"),
        maps_url=d.get("maps_url"),
        no_website_flag=bool(d.get("no_website_flag")),
        status=str(d.get("status") or "new"),
        first_seen_at=str(d["first_seen_at"]),
        last_contacted_at=d.get("last_contacted_at"),
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# --- dashboard key/value ---


def get_dashboard_value(conn: sqlite3.Connection, key: str, default: str) -> str:
    row = conn.execute(
        "SELECT value FROM dashboard_inputs WHERE key = ?",
        (key,),
    ).fetchone()
    return str(row[0]) if row else default


def set_dashboard_value(conn: sqlite3.Connection, key: str, value: str) -> None:
    now = _utc_now_iso()
    conn.execute(
        """
        INSERT INTO dashboard_inputs (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, now),
    )


def get_aggregate_stats(conn: sqlite3.Connection) -> Mapping[str, int]:
    total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    contacted = conn.execute(
        "SELECT COUNT(*) FROM leads WHERE last_contacted_at IS NOT NULL"
    ).fetchone()[0]
    no_site = conn.execute(
        "SELECT COUNT(*) FROM leads WHERE no_website_flag = 1"
    ).fetchone()[0]
    return {
        "total_leads": int(total),
        "contacted": int(contacted),
        "no_website": int(no_site),
    }


def list_recent_leads(conn: sqlite3.Connection, limit: int = 200) -> Iterable[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM leads ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
