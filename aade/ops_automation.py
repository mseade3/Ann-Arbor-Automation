"""Operations-tray automation: verified -> active multi-tool workflow."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import requests

from aade import database as db
from aade.config import (
    AGENT_FRANK_EMAIL_WEBHOOK,
    CLAY_ENRICH_WEBHOOK,
    ELEVENX_CALL_WEBHOOK,
)
from aade.visual_hook import generate_premium_mockup, premium_vibe_for_niche, screenshot_landing_page


@dataclass(frozen=True, slots=True)
class FlowResult:
    enrichment_ok: bool
    owner_name: str | None
    owner_linkedin: str | None
    direct_email: str | None
    mockup_asset: str | None
    call_tag: str | None
    email_fired: bool
    notes: list[str]


@dataclass(frozen=True, slots=True)
class StepResult:
    step: str
    ok: bool
    message: str
    meta: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class SequenceProcessResult:
    scanned: int
    sent_count: int
    messages: list[str]


def _post_json(url: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    resp = requests.post(url, json=dict(payload), timeout=25)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        return data
    return {}


def _normalize_path_or_url(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    if not v:
        return None
    if v.startswith("http://") or v.startswith("https://"):
        return v
    return str(Path(v))


def _render_mockup(lead: Mapping[str, Any]) -> str:
    business = str(lead.get("business_name") or "Local Business")
    niche = str(lead.get("niche") or "local business")
    rendered: str | None = None
    try:
        rendered = generate_premium_mockup(
            business,
            niche,
            premium_vibe_for_niche(niche, str(lead.get("design_color_theme") or "")),
        )
    except Exception:
        rendered = None
    if rendered:
        return rendered
    out = screenshot_landing_page(business, niche=niche)
    return str(out)


def _email_one_subject(business_name: str) -> str:
    return f"A quick gift for {business_name} (from a U-M student)"


def _email_one_body(
    *,
    owner_name: str | None,
    business_name: str,
    street_name: str,
) -> str:
    salutation = f"Hi {owner_name}," if owner_name else f"Hi {business_name} team,"
    return (
        f"{salutation}\n\n"
        f"I was flying over Ann Arbor on our digital map and noticed your shop on {street_name}.\n\n"
        "I took the liberty of using our AI engine to build a high-conversion landing page for you. "
        "No strings attached-just wanted to show you what is possible.\n\n"
        "If it is useful, I can walk you through it and have a live version up quickly.\n\n"
        "Best,\n"
        "Miles\n"
        "University of Michigan CS"
    )


def _state_from_lead(lead: Mapping[str, Any]) -> dict[str, Any]:
    raw = str(lead.get("automation_state_json") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().replace(microsecond=0).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _write_state(conn, place_id: str, lead: Mapping[str, Any], **updates: Any) -> None:
    state = dict(_state_from_lead(lead))
    state.update(updates)
    db.save_lead_automation_fields(
        conn,
        place_id,
        automation_state_json=json.dumps(state, ensure_ascii=True),
    )


def _schedule_followup_sequence(
    conn,
    place_id: str,
    lead: Mapping[str, Any],
    *,
    email_1_sent_at: str | None = None,
) -> None:
    now_iso = email_1_sent_at or _utc_now_iso()
    email1_dt = _parse_iso(now_iso) or _utc_now()
    email2_due = (email1_dt + timedelta(days=2)).replace(microsecond=0).isoformat()
    email3_due = (email1_dt + timedelta(days=5)).replace(microsecond=0).isoformat()
    _write_state(
        conn,
        place_id,
        lead,
        email_1_sent_at=now_iso,
        email_2_due_at=email2_due,
        email_3_due_at=email3_due,
        email_2_sent_at=None,
        email_3_sent_at=None,
        sequence_active=True,
    )


def _email_two_subject(competitor_name: str) -> str:
    return f"Why {competitor_name} is winning search in Ann Arbor"


def _email_two_body(
    *,
    owner_name: str | None,
    business_name: str,
    competitor_name: str,
) -> str:
    salutation = f"Hi {owner_name}," if owner_name else f"Hi {business_name} team,"
    return (
        f"{salutation}\n\n"
        f"Quick follow-up: there is a visible SEO gap between {business_name} and {competitor_name} "
        "because they have an indexable site and stronger local signals.\n\n"
        "That means they are capturing more high-intent Ann Arbor searches while your demand is still mostly "
        "word-of-mouth.\n\n"
        "If useful, I can send a 10-minute breakdown with 2-3 quick wins specific to your niche.\n\n"
        "Best,\n"
        "Miles\n"
        "University of Michigan CS"
    )


def _email_three_subject() -> str:
    return "Should I delete this mockup?"


def _email_three_body(
    *,
    owner_name: str | None,
    business_name: str,
) -> str:
    salutation = f"Hi {owner_name}," if owner_name else f"Hi {business_name} team,"
    return (
        f"{salutation}\n\n"
        "I wanted to close the loop.\n\n"
        "If you want, I can make the site live before football season / move-in week traffic picks up.\n"
        "If timing is off, no pressure - I can clear this project from my dashboard.\n\n"
        "Just reply with 'launch' or 'delete' and I will handle it.\n\n"
        "Best,\n"
        "Miles\n"
        "University of Michigan CS"
    )


def run_verified_to_active_flow(
    conn,
    *,
    place_id: str,
    lead: Mapping[str, Any],
) -> FlowResult:
    business_name = str(lead.get("business_name") or "Local Business")
    street_name = str(lead.get("street_name") or "your street in Ann Arbor")
    phone = str(lead.get("phone") or "")

    notes: list[str] = []
    owner_name = None
    owner_linkedin = None
    direct_email = None
    enrichment_ok = False

    # Action 1: Clay enrichment
    if CLAY_ENRICH_WEBHOOK:
        try:
            clay = _post_json(
                CLAY_ENRICH_WEBHOOK,
                {"business_name": business_name, "place_id": place_id},
            )
            owner_name = (clay.get("owner_name") or "").strip() or None
            owner_linkedin = (clay.get("owner_linkedin") or "").strip() or None
            direct_email = (clay.get("direct_email") or "").strip() or None
            enrichment_ok = bool(owner_linkedin or direct_email)
            notes.append("Clay enrichment webhook completed.")
        except Exception as exc:
            notes.append(f"Clay enrichment webhook failed: {exc}")
    else:
        notes.append("Clay webhook not configured; enrichment skipped.")

    db.save_lead_automation_fields(
        conn,
        place_id,
        owner_name=owner_name,
        owner_linkedin=owner_linkedin,
        direct_email=direct_email,
    )

    # Action 2: Visual hook generation
    mockup_asset = _render_mockup(lead)
    db.save_lead_automation_fields(conn, place_id, mockup_asset=mockup_asset)
    notes.append("Visual mockup generated.")

    # Action 3: 11x voice call
    call_tag = "No Answer"
    if ELEVENX_CALL_WEBHOOK:
        try:
            call = _post_json(
                ELEVENX_CALL_WEBHOOK,
                {
                    "business_name": business_name,
                    "place_id": place_id,
                    "phone": phone,
                    "owner_name": owner_name,
                    "owner_linkedin": owner_linkedin,
                    "direct_email": direct_email,
                    "mockup_asset": mockup_asset,
                },
            )
            call_tag = str(call.get("tag") or call.get("call_tag") or call_tag)
            notes.append(f"11x call completed with tag '{call_tag}'.")
        except Exception as exc:
            notes.append(f"11x call webhook failed: {exc}")
    else:
        notes.append("11x webhook not configured; call skipped.")
    db.save_lead_automation_fields(conn, place_id, call_log_tag=call_tag)

    # Action 4: Agent Frank email #1 if interested
    email_fired = False
    if call_tag.strip().lower() == "interested":
        subject = _email_one_subject(business_name)
        body = _email_one_body(
            owner_name=owner_name,
            business_name=business_name,
            street_name=street_name,
        )
        payload = {
            "place_id": place_id,
            "business_name": business_name,
            "owner_name": owner_name,
            "to_email": direct_email,
            "subject": subject,
            "body": body,
            "attachment_path_or_url": _normalize_path_or_url(mockup_asset),
            "sequence_step": 1,
        }
        if AGENT_FRANK_EMAIL_WEBHOOK:
            try:
                _post_json(AGENT_FRANK_EMAIL_WEBHOOK, payload)
                email_fired = True
                notes.append("Agent Frank fired Email #1 with mockup.")
            except Exception as exc:
                notes.append(f"Agent Frank webhook failed: {exc}")
        else:
            notes.append("Agent Frank webhook not configured; email not sent.")
    else:
        notes.append("Call tag is not 'Interested'; Email #1 not fired.")

    state = {
        "place_id": place_id,
        "business_name": business_name,
        "owner_name": owner_name,
        "owner_linkedin": owner_linkedin,
        "direct_email": direct_email,
        "mockup_asset": mockup_asset,
        "call_tag": call_tag,
        "email_fired": email_fired,
        "notes": notes,
    }
    if email_fired:
        email1 = _utc_now()
        state.update(
            {
                "email_1_sent_at": email1.replace(microsecond=0).isoformat(),
                "email_2_due_at": (email1 + timedelta(days=2)).replace(microsecond=0).isoformat(),
                "email_3_due_at": (email1 + timedelta(days=5)).replace(microsecond=0).isoformat(),
                "email_2_sent_at": None,
                "email_3_sent_at": None,
                "sequence_active": True,
            }
        )
    db.save_lead_automation_fields(
        conn,
        place_id,
        automation_state_json=json.dumps(state, ensure_ascii=True),
    )

    return FlowResult(
        enrichment_ok=enrichment_ok,
        owner_name=owner_name,
        owner_linkedin=owner_linkedin,
        direct_email=direct_email,
        mockup_asset=mockup_asset,
        call_tag=call_tag,
        email_fired=email_fired,
        notes=notes,
    )


def retry_enrichment_step(
    conn,
    *,
    place_id: str,
    lead: Mapping[str, Any],
) -> StepResult:
    business_name = str(lead.get("business_name") or "Local Business")
    if not CLAY_ENRICH_WEBHOOK:
        return StepResult("enrichment", False, "Clay webhook is not configured.")
    try:
        clay = _post_json(
            CLAY_ENRICH_WEBHOOK,
            {"business_name": business_name, "place_id": place_id},
        )
        owner_name = (clay.get("owner_name") or "").strip() or None
        owner_linkedin = (clay.get("owner_linkedin") or "").strip() or None
        direct_email = (clay.get("direct_email") or "").strip() or None
        db.save_lead_automation_fields(
            conn,
            place_id,
            owner_name=owner_name,
            owner_linkedin=owner_linkedin,
            direct_email=direct_email,
        )
        _write_state(
            conn,
            place_id,
            lead,
            owner_name=owner_name,
            owner_linkedin=owner_linkedin,
            direct_email=direct_email,
            enrichment_ok=bool(owner_linkedin or direct_email),
        )
        return StepResult("enrichment", True, "Enrichment retry completed.")
    except Exception as exc:
        return StepResult("enrichment", False, f"Enrichment retry failed: {exc}")


def retry_mockup_step(
    conn,
    *,
    place_id: str,
    lead: Mapping[str, Any],
) -> StepResult:
    try:
        mockup_asset = _render_mockup(lead)
        db.save_lead_automation_fields(conn, place_id, mockup_asset=mockup_asset)
        _write_state(conn, place_id, lead, mockup_asset=mockup_asset)
        return StepResult("mockup", True, "Mockup regenerated.")
    except Exception as exc:
        return StepResult("mockup", False, f"Mockup retry failed: {exc}")


def retry_call_step(
    conn,
    *,
    place_id: str,
    lead: Mapping[str, Any],
) -> StepResult:
    if not ELEVENX_CALL_WEBHOOK:
        return StepResult("call", False, "11x webhook is not configured.")
    business_name = str(lead.get("business_name") or "Local Business")
    phone = str(lead.get("phone") or "")
    owner_name = (str(lead.get("owner_name") or "").strip() or None)
    owner_linkedin = (str(lead.get("owner_linkedin") or "").strip() or None)
    direct_email = (str(lead.get("direct_email") or "").strip() or None)
    mockup_asset = (str(lead.get("mockup_asset") or "").strip() or None)
    if not mockup_asset:
        mockup_asset = _render_mockup(lead)
        db.save_lead_automation_fields(conn, place_id, mockup_asset=mockup_asset)
    try:
        call = _post_json(
            ELEVENX_CALL_WEBHOOK,
            {
                "business_name": business_name,
                "place_id": place_id,
                "phone": phone,
                "owner_name": owner_name,
                "owner_linkedin": owner_linkedin,
                "direct_email": direct_email,
                "mockup_asset": mockup_asset,
            },
        )
        call_tag = str(call.get("tag") or call.get("call_tag") or "No Answer")
        db.save_lead_automation_fields(conn, place_id, call_log_tag=call_tag)
        _write_state(conn, place_id, lead, call_tag=call_tag, mockup_asset=mockup_asset)
        return StepResult("call", True, f"Call retry completed with tag '{call_tag}'.")
    except Exception as exc:
        return StepResult("call", False, f"Call retry failed: {exc}")


def retry_email_step1(
    conn,
    *,
    place_id: str,
    lead: Mapping[str, Any],
) -> StepResult:
    if not AGENT_FRANK_EMAIL_WEBHOOK:
        return StepResult("email_1", False, "Agent Frank webhook is not configured.")
    business_name = str(lead.get("business_name") or "Local Business")
    street_name = str(lead.get("street_name") or "your street in Ann Arbor")
    owner_name = (str(lead.get("owner_name") or "").strip() or None)
    direct_email = (str(lead.get("direct_email") or "").strip() or None)
    mockup_asset = (str(lead.get("mockup_asset") or "").strip() or None)
    if not mockup_asset:
        mockup_asset = _render_mockup(lead)
        db.save_lead_automation_fields(conn, place_id, mockup_asset=mockup_asset)
    try:
        _post_json(
            AGENT_FRANK_EMAIL_WEBHOOK,
            {
                "place_id": place_id,
                "business_name": business_name,
                "owner_name": owner_name,
                "to_email": direct_email,
                "subject": _email_one_subject(business_name),
                "body": _email_one_body(
                    owner_name=owner_name,
                    business_name=business_name,
                    street_name=street_name,
                ),
                "attachment_path_or_url": _normalize_path_or_url(mockup_asset),
                "sequence_step": 1,
                "trigger_reason": "retry_button",
            },
        )
        _write_state(conn, place_id, lead, email_fired=True, mockup_asset=mockup_asset)
        _schedule_followup_sequence(conn, place_id, lead, email_1_sent_at=_utc_now_iso())
        return StepResult("email_1", True, "Email #1 retried successfully.")
    except Exception as exc:
        _write_state(conn, place_id, lead, email_fired=False, mockup_asset=mockup_asset)
        return StepResult("email_1", False, f"Email #1 retry failed: {exc}")


def run_interested_email_only(
    conn,
    *,
    place_id: str,
    lead: Mapping[str, Any],
) -> FlowResult:
    """
    Secondary trigger path:
    when a lead is manually set to `interested`, fire Email #1 immediately.
    """
    business_name = str(lead.get("business_name") or "Local Business")
    street_name = str(lead.get("street_name") or "your street in Ann Arbor")
    owner_name = (str(lead.get("owner_name") or "").strip() or None)
    owner_linkedin = (str(lead.get("owner_linkedin") or "").strip() or None)
    direct_email = (str(lead.get("direct_email") or "").strip() or None)
    notes: list[str] = []

    mockup_asset = (str(lead.get("mockup_asset") or "").strip() or None)
    if not mockup_asset:
        mockup_asset = _render_mockup(lead)
        db.save_lead_automation_fields(conn, place_id, mockup_asset=mockup_asset)
        notes.append("No existing mockup found; generated one.")
    else:
        notes.append("Reused existing mockup asset.")

    subject = _email_one_subject(business_name)
    body = _email_one_body(
        owner_name=owner_name,
        business_name=business_name,
        street_name=street_name,
    )
    payload = {
        "place_id": place_id,
        "business_name": business_name,
        "owner_name": owner_name,
        "to_email": direct_email,
        "subject": subject,
        "body": body,
        "attachment_path_or_url": _normalize_path_or_url(mockup_asset),
        "sequence_step": 1,
        "trigger_reason": "manual_interested_status",
    }

    email_fired = False
    if AGENT_FRANK_EMAIL_WEBHOOK:
        try:
            _post_json(AGENT_FRANK_EMAIL_WEBHOOK, payload)
            email_fired = True
            notes.append("Agent Frank fired Email #1 from interested status trigger.")
        except Exception as exc:
            notes.append(f"Agent Frank webhook failed: {exc}")
    else:
        notes.append("Agent Frank webhook not configured; email not sent.")

    db.save_lead_automation_fields(
        conn,
        place_id,
        call_log_tag="Interested",
        automation_state_json=json.dumps(
            {
                "place_id": place_id,
                "business_name": business_name,
                "owner_name": owner_name,
                "owner_linkedin": owner_linkedin,
                "direct_email": direct_email,
                "mockup_asset": mockup_asset,
                "call_tag": "Interested",
                "email_fired": email_fired,
                "notes": notes,
                "flow": "interested_email_only",
                "sequence_active": bool(email_fired),
                "email_1_sent_at": _utc_now_iso() if email_fired else None,
                "email_2_due_at": (
                    (_utc_now() + timedelta(days=2)).replace(microsecond=0).isoformat()
                    if email_fired
                    else None
                ),
                "email_3_due_at": (
                    (_utc_now() + timedelta(days=5)).replace(microsecond=0).isoformat()
                    if email_fired
                    else None
                ),
                "email_2_sent_at": None,
                "email_3_sent_at": None,
            },
            ensure_ascii=True,
        ),
    )

    return FlowResult(
        enrichment_ok=bool(owner_linkedin or direct_email),
        owner_name=owner_name,
        owner_linkedin=owner_linkedin,
        direct_email=direct_email,
        mockup_asset=mockup_asset,
        call_tag="Interested",
        email_fired=email_fired,
        notes=notes,
    )


def process_due_sequence_emails(
    conn,
    *,
    limit: int = 120,
) -> SequenceProcessResult:
    """
    Send due follow-up emails (#2 and #3) based on stored schedule state.
    Safe to call on dashboard refresh.
    """
    rows = conn.execute(
        """
        SELECT place_id, business_name, owner_name, direct_email, mockup_asset, automation_state_json
        FROM leads
        WHERE automation_state_json IS NOT NULL
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    scanned = len(rows)
    sent_count = 0
    messages: list[str] = []
    now = _utc_now()

    if not AGENT_FRANK_EMAIL_WEBHOOK:
        return SequenceProcessResult(
            scanned=scanned,
            sent_count=0,
            messages=["Agent Frank webhook not configured; scheduler skipped."],
        )

    for row in rows:
        lead = dict(row)
        place_id = str(lead.get("place_id") or "")
        if not place_id:
            continue
        state = _state_from_lead(lead)
        if not bool(state.get("sequence_active")):
            continue

        business_name = str(lead.get("business_name") or "Local Business")
        owner_name = (str(lead.get("owner_name") or "").strip() or None)
        direct_email = (str(lead.get("direct_email") or "").strip() or None)
        mockup_asset = (str(lead.get("mockup_asset") or "").strip() or None)
        competitor_name = str(state.get("competitor_name") or "a nearby competitor")

        email2_due = _parse_iso(state.get("email_2_due_at"))
        email2_sent = _parse_iso(state.get("email_2_sent_at"))
        email3_due = _parse_iso(state.get("email_3_due_at"))
        email3_sent = _parse_iso(state.get("email_3_sent_at"))

        if email2_due and email2_sent is None and email2_due <= now:
            try:
                _post_json(
                    AGENT_FRANK_EMAIL_WEBHOOK,
                    {
                        "place_id": place_id,
                        "business_name": business_name,
                        "owner_name": owner_name,
                        "to_email": direct_email,
                        "subject": _email_two_subject(competitor_name),
                        "body": _email_two_body(
                            owner_name=owner_name,
                            business_name=business_name,
                            competitor_name=competitor_name,
                        ),
                        "attachment_path_or_url": _normalize_path_or_url(mockup_asset),
                        "sequence_step": 2,
                        "trigger_reason": "scheduled_followup",
                    },
                )
                _write_state(
                    conn,
                    place_id,
                    lead,
                    email_2_sent_at=_utc_now_iso(),
                    last_sequence_event="email_2_sent",
                )
                sent_count += 1
                messages.append(f"Sent Email #2 for {business_name}.")
            except Exception as exc:
                messages.append(f"Email #2 failed for {business_name}: {exc}")

        if email3_due and email3_sent is None and email3_due <= now:
            try:
                _post_json(
                    AGENT_FRANK_EMAIL_WEBHOOK,
                    {
                        "place_id": place_id,
                        "business_name": business_name,
                        "owner_name": owner_name,
                        "to_email": direct_email,
                        "subject": _email_three_subject(),
                        "body": _email_three_body(
                            owner_name=owner_name,
                            business_name=business_name,
                        ),
                        "attachment_path_or_url": _normalize_path_or_url(mockup_asset),
                        "sequence_step": 3,
                        "trigger_reason": "scheduled_followup",
                    },
                )
                _write_state(
                    conn,
                    place_id,
                    lead,
                    email_3_sent_at=_utc_now_iso(),
                    sequence_active=False,
                    last_sequence_event="email_3_sent",
                )
                sent_count += 1
                messages.append(f"Sent Email #3 for {business_name}.")
            except Exception as exc:
                messages.append(f"Email #3 failed for {business_name}: {exc}")

    return SequenceProcessResult(scanned=scanned, sent_count=sent_count, messages=messages)


def snooze_sequence(
    conn,
    *,
    place_id: str,
    lead: Mapping[str, Any],
    days: int,
) -> StepResult:
    """
    Push unsent follow-up due dates forward by `days`.
    """
    shift_days = int(days)
    if shift_days <= 0:
        return StepResult("snooze", False, "Snooze days must be greater than 0.")
    state = _state_from_lead(lead)
    if not state:
        return StepResult("snooze", False, "No automation state found for this lead.")

    updates: dict[str, Any] = {}
    touched = 0

    email2_sent = _parse_iso(state.get("email_2_sent_at"))
    email2_due = _parse_iso(state.get("email_2_due_at"))
    if email2_sent is None:
        base2 = email2_due or _utc_now()
        updates["email_2_due_at"] = (base2 + timedelta(days=shift_days)).replace(
            microsecond=0
        ).isoformat()
        touched += 1

    email3_sent = _parse_iso(state.get("email_3_sent_at"))
    email3_due = _parse_iso(state.get("email_3_due_at"))
    if email3_sent is None:
        base3 = email3_due or (_utc_now() + timedelta(days=3))
        updates["email_3_due_at"] = (base3 + timedelta(days=shift_days)).replace(
            microsecond=0
        ).isoformat()
        touched += 1

    if touched == 0:
        return StepResult("snooze", False, "Nothing to snooze; follow-ups are already sent.")

    updates["last_sequence_event"] = f"snoozed_{shift_days}d"
    _write_state(conn, place_id, lead, **updates)
    return StepResult(
        "snooze",
        True,
        f"Snoozed follow-ups by {shift_days} day(s).",
        meta={
            "email_2_due_at": updates.get("email_2_due_at"),
            "email_3_due_at": updates.get("email_3_due_at"),
        },
    )
