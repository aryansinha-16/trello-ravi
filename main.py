"""
Trello Ravi Digest — Railway Service
Two cron jobs:
  Task 1 (8:00 AM IST): Check if "Ravi (Trello)" meeting is in 2 days → email Ravi
  Task 2 (6:00 AM IST): Check if "Ravi (Trello)" meeting is today → email Sonal 2h before
"""

import json
import os
import logging
import requests
import anthropic
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from google.oauth2 import service_account
from googleapiclient.discovery import build
from apscheduler.schedulers.blocking import BlockingScheduler

EMAIL_MCP_URL = "https://valuecart-email-mcp-production.up.railway.app/mcp/valuecart2026"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# ── Env ──────────────────────────────────────────────────────────────────────

TRELLO_API_KEY   = os.environ["TRELLO_API_KEY"]
TRELLO_TOKEN     = os.environ["TRELLO_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# Board / list IDs
BOARD_ID = "667516b9619b636121467c4e"
LISTS = {
    "667516b9619b636121467c4f": "Recruiting",
    "667516b9619b636121467c51": "PMS",
    "667516b9619b636121467c53": "Activities",
    "667516b9619b636121467c50": "Open Tasks",
    "667516b9619b636121467c54": "Open Tasks and Recurring",
    "667516b9619b636121467c52": "Salary and Compliances",
}

LABEL_PRIORITY = {
    "High Preiority and High Intervention": 1,
    "Low Preiority and High Intervention":  2,
    "High Priority and Low Inetrvention":   3,
    "Low Preiority and Low Intervention":   4,
}

RAVI_EMAIL  = "aryan@valuecart.in"
SONAL_EMAIL = "aryan@valuecart.in"

# ── Google helpers ────────────────────────────────────────────────────────────

def _gcal_service():
    info = {
        "type": "service_account",
        "project_id":     os.environ["GCP_PROJECT_ID"],
        "private_key_id": os.environ["GCP_PRIVATE_KEY_ID"],
        "private_key":    os.environ["GCP_PRIVATE_KEY"].replace("\\n", "\n"),
        "client_email":   os.environ["GCP_CLIENT_EMAIL"],
        "client_id":      os.environ["GCP_CLIENT_ID"],
        "auth_uri":       "https://accounts.google.com/o/oauth2/auth",
        "token_uri":      "https://oauth2.googleapis.com/token",
    }
    creds = service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/calendar.readonly"],
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def get_ravi_meeting(target_date: datetime) -> dict | None:
    """Return the first 'Ravi (Trello)' event on target_date (IST), or None."""
    svc = _gcal_service()
    day_start = target_date.replace(hour=0,  minute=0,  second=0,  microsecond=0)
    day_end   = target_date.replace(hour=23, minute=59, second=59, microsecond=0)
    result = svc.events().list(
        calendarId=os.environ["GCAL_CALENDAR_ID"],
        timeMin=day_start.isoformat(),
        timeMax=day_end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    for event in result.get("items", []):
        if "ravi (trello)" in event.get("summary", "").lower():
            return event
    return None


# ── Email send via MCP ────────────────────────────────────────────────────────

def send_email(to: str, subject: str, html_body: str):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "send_email",
            "arguments": {
                "to": to,
                "subject": subject,
                "body_html": html_body,
                "body_text": "Please view this email in an HTML-capable client.",
            },
        },
    }
    res = requests.post(
        EMAIL_MCP_URL,
        json=payload,
        headers={"Accept": "application/json, text/event-stream"},
        timeout=15,
    )
    res.raise_for_status()
    data_line = next((l for l in res.text.splitlines() if l.startswith("data:")), None)
    if not data_line:
        raise Exception("No data in MCP email response")
    parsed = json.loads(data_line[5:].strip())
    if "error" in parsed:
        raise Exception(f"Email MCP error: {parsed['error']['message']}")
    log.info("Email sent to %s: %s", to, subject)


# ── Trello helpers ────────────────────────────────────────────────────────────

def trello_get(path: str, **params) -> dict | list:
    url = f"https://api.trello.com/1/{path}"
    params.update({"key": TRELLO_API_KEY, "token": TRELLO_TOKEN})
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def fetch_all_open_cards() -> list[dict]:
    """Fetch all open cards from every list, enrich with list name."""
    cards = []
    for list_id, list_name in LISTS.items():
        try:
            raw = trello_get(
                f"lists/{list_id}/cards",
                fields="name,due,dueComplete,badges,labels,idList",
                filter="open",
            )
            for c in raw:
                c["_list_name"] = list_name
            cards.extend(raw)
        except Exception as e:
            log.warning("Failed to fetch list %s (%s): %s", list_name, list_id, e)
    return cards


def priority_of(card: dict) -> int:
    for lbl in card.get("labels", []):
        p = LABEL_PRIORITY.get(lbl.get("name", ""))
        if p:
            return p
    return 3  # default: orange


def due_status(due_str: str | None) -> str:
    if not due_str:
        return "no-due"
    now = datetime.now(IST)
    due = datetime.fromisoformat(due_str.replace("Z", "+00:00")).astimezone(IST)
    delta = (due.date() - now.date()).days
    if delta < 0:
        return "overdue"
    if delta == 0:
        return "today"
    if delta <= 2:
        return "due-soon"
    return "normal"


def checklist_summary(card: dict) -> dict:
    total   = card["badges"].get("checkItems", 0)
    checked = card["badges"].get("checkItemsChecked", 0)
    pending = total - checked
    return {"total": total, "checked": checked, "pending": pending}


def sort_key(card: dict):
    status_order = {"overdue": 0, "today": 1, "due-soon": 2, "normal": 3, "no-due": 4}
    return (priority_of(card), status_order[due_status(card.get("due"))])


# ── Claude digest generation ─────────────────────────────────────────────────

def _claude_client():
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def build_card_data_text(cards: list[dict]) -> str:
    """Serialize card data into a structured text for Claude."""
    lines = []
    for c in sorted(cards, key=sort_key):
        cl = checklist_summary(c)
        due = c.get("due", "None")
        ds  = due_status(due)
        pri = priority_of(c)
        section_map = {1: "RED", 2: "AMBER", 3: "ORANGE", 4: "GREEN"}
        lines.append(
            f"CARD: {c['name']}\n"
            f"  List: {c['_list_name']}\n"
            f"  Section: {pri} ({section_map[pri]})\n"
            f"  Due: {due} | Status: {ds}\n"
            f"  Checklist: {cl['checked']}/{cl['total']} (pending: {cl['pending']})\n"
        )
    return "\n".join(lines)


RAVI_PROMPT_TEMPLATE = """
You are an HTML email generator. Output a COMPLETE, self-contained HTML email for Ravi's pre-meeting digest.
Output ONLY raw HTML — no markdown, no code fences, no explanation.
CRITICAL: Use ONLY inline styles with hardcoded hex colors — NO CSS classes, NO <style> blocks, NO CSS variables. Every element must have style= attributes directly.

MEETING DATE: {meeting_date}
TODAY: {today}

CARD DATA:
{card_data}

COLOR REFERENCE (use these exact hex values inline):
- Navy bg: #1C2A3A  Navy mid: #2C3E50  Page bg: #F0F2F5
- Red: #B03A2E  Red light: #FBEAE9  Red bar: #C0392B
- Amber: #9A7D0A  Amber light: #FEFDE7  Amber bar: #D4AC0D
- Orange: #A04000  Orange light: #FEF5E7  Orange bar: #CA6F1E
- Green: #1A5C35  Green light: #EAFAF1  Green bar: #1E8449
- Grid: #D5D8DC  Text: #1C1C1C  Meta: #566573

STRUCTURE:

<html><body style="background:#F0F2F5;font-family:Arial,sans-serif;color:#1C1C1C;padding:32px 16px;">
<div style="background:#FFFFFF;max-width:860px;margin:0 auto;padding:28px 30px;">

  <!-- Navy header bar -->
  <div style="background:#1C2A3A;color:white;padding:8px 14px;margin-bottom:4px;font-size:11px;">
    <b>Ravi Shankar K</b> &nbsp;·&nbsp; <span style="opacity:0.7;">Sent 2 days before meeting · Subject: Ravi (Trello) — Pre-Meeting Digest | {meeting_date}</span>
  </div>

  <!-- Masthead -->
  <div style="border-left:5px solid #1C2A3A;padding:2px 0 2px 14px;margin-bottom:6px;">
    <div style="font-size:15px;font-weight:700;color:#1C2A3A;">Ravi Board — Pre-Meeting Prep</div>
    <div style="font-size:10.5px;color:#566573;margin-top:3px;">Meeting: Ravi (Trello) &nbsp;·&nbsp; {today} &nbsp;·&nbsp; Please review your board and update all cards before the meeting</div>
  </div>
  <hr style="border:none;border-top:1px solid #D5D8DC;margin:10px 0 14px;">

  <!-- Intro -->
  <div style="font-size:11.5px;color:#566573;line-height:1.6;margin-bottom:14px;padding:10px 14px;background:#F8F9FA;border-left:3px solid #2C3E50;">
    Hi <strong style="color:#1C2A3A;">Ravi</strong>,<br><br>
    There's a <strong style="color:#1C2A3A;">Ravi (Trello)</strong> meeting on <strong style="color:#1C2A3A;">{meeting_date}</strong>. Please go through each item below, update checklist progress, and be ready with a status update. Items marked <strong style="color:#C0392B;">OVERDUE</strong> need immediate attention.
  </div>

  For each of the 4 sections, output a div block like this:
  <!-- SECTION HEADER (use correct bg color per section) -->
  <div style="margin-bottom:16px;">
    <div style="padding:7px 12px;background:SECTION_COLOR;color:white;font-size:11px;font-weight:700;">SECTION_TITLE</div>
    <div style="font-size:10px;color:#566573;padding:4px 12px 5px;background:#F8F9FA;border-bottom:1px solid #D5D8DC;font-style:italic;">SECTION_DESC</div>
    <table style="width:100%;border-collapse:collapse;font-size:11.5px;">
      <thead><tr style="background:#2C3E50;">
        <th style="color:white;font-size:10px;font-weight:600;text-transform:uppercase;padding:6px 8px;text-align:left;border:1px solid #1C2A3A;width:36%;">Card</th>
        <th style="color:white;font-size:10px;font-weight:600;text-transform:uppercase;padding:6px 8px;text-align:left;border:1px solid #1C2A3A;width:16%;">List</th>
        <th style="color:white;font-size:10px;font-weight:600;text-transform:uppercase;padding:6px 8px;text-align:left;border:1px solid #1C2A3A;width:18%;">Due Date</th>
        <th style="color:white;font-size:10px;font-weight:600;text-transform:uppercase;padding:6px 8px;text-align:left;border:1px solid #1C2A3A;width:30%;">Checklist</th>
      </tr></thead>
      <tbody>
        <!-- For each card, alternate row bg: odd=white, even=SECTION_LIGHT_COLOR -->
        <tr style="background:ROW_BG;border-bottom:1px solid #D5D8DC;">
          <td style="padding:7px 8px;vertical-align:top;border-right:1px solid #D5D8DC;"><span style="font-weight:600;font-size:11.5px;color:#1C1C1C;">CARD NAME</span></td>
          <td style="padding:7px 8px;vertical-align:top;border-right:1px solid #D5D8DC;font-size:10px;color:#566573;">LIST NAME</td>
          <td style="padding:7px 8px;vertical-align:top;border-right:1px solid #D5D8DC;">
            <!-- if overdue: -->
            <span style="font-size:10.5px;color:#C0392B;font-weight:700;">DD Mon YYYY <span style="display:inline-block;font-size:8.5px;background:#C0392B;color:white;padding:1px 5px;border-radius:2px;margin-left:4px;font-weight:600;">OVERDUE</span></span>
            <!-- if normal: -->
            <span style="font-size:10.5px;color:#1C1C1C;">DD Mon YYYY</span>
            <!-- if no due: -->
            <span style="font-size:10px;color:#AAB2BD;font-style:italic;">No due date</span>
          </td>
          <td style="padding:7px 8px;vertical-align:top;">
            <!-- if has checklist: -->
            <div style="display:flex;flex-direction:column;gap:3px;">
              <div style="height:5px;border-radius:3px;background:#E5E8E8;overflow:hidden;width:100%;margin-bottom:2px;">
                <div style="height:100%;border-radius:3px;background:BAR_COLOR;width:PCT%;"></div>
              </div>
              <span style="font-size:9.5px;color:#566573;">DONE/TOTAL done (PCT%)</span>
              <!-- if pending>0: <span style="font-size:9px;color:#C0392B;font-weight:600;">N items pending</span> -->
              <!-- if 100%: <span style="font-size:9px;color:#1E8449;font-weight:600;">✓ Complete</span> -->
              <!-- if 0%: <span style="font-size:9px;color:#C0392B;font-weight:700;">⚠ Not started</span> -->
            </div>
            <!-- if no checklist: <span style="font-size:10px;color:#AAB2BD;font-style:italic;">No checklist</span> -->
          </td>
        </tr>
      </tbody>
    </table>
  </div>

  Section colors:
  - Section 1 (priority=1): header bg #B03A2E, even row bg #FBEAE9, bar colors: 0%=#C0392B, >0%=#D4AC0D, >=40%=#CA6F1E, >=75%=#1E8449, 100%=#1E8449
  - Section 2 (priority=2): header bg #9A7D0A, even row bg #FEFDE7
  - Section 3 (priority=3): header bg #A04000, even row bg #FEF5E7
  - Section 4 (priority=4): header bg #1A5C35, even row bg #EAFAF1

  Section titles:
  - 1: "🔴 IMPORTANT · URGENT — Act on these before the meeting" / desc "High priority, high intervention — Sonal will ask about these first"
  - 2: "🟡 IMPORTANT · NOT URGENT — Needs your hands-on involvement" / desc "Low priority, high intervention — make sure these are moving"
  - 3: "🟠 URGENT · NOT IMPORTANT — Monitor and keep moving" / desc "High priority, low intervention — update status before the meeting"
  - 4: "🟢 NOT URGENT · NOT IMPORTANT — Running in background" / desc "Low priority, low intervention — note any blockers"

  After all 4 sections, add the attention box:
  <div style="border:1.5px solid #D4AC0D;background:#FEFDE7;padding:12px 16px;margin-top:4px;display:flex;gap:16px;">
    <div style="font-size:11px;font-weight:600;color:#9A7D0A;white-space:nowrap;min-width:130px;">📋 Please update<br>before meeting</div>
    <div style="font-size:11px;line-height:1.65;color:#1C1C1C;">
      <ul style="list-style:none;padding:0;">
        <!-- bullet per overdue/0%-progress/due-soon card: -->
        <li style="padding-left:14px;position:relative;margin-bottom:3px;"><span style="position:absolute;left:0;color:#D4AC0D;font-weight:700;">•</span><strong style="color:#1C2A3A;">CARD NAME:</strong> short action item</li>
      </ul>
    </div>
  </div>

  <div style="margin-top:18px;text-align:center;font-size:9.5px;color:#AAB2BD;border-top:1px solid #D5D8DC;padding-top:10px;">Valuecart Automation &nbsp;·&nbsp; ravi.digest@valuecart.in &nbsp;·&nbsp; Generated: {today} &nbsp;·&nbsp; Sent 2 days before meeting</div>
</div>
</body></html>
"""

SONAL_PROMPT_TEMPLATE = """
You are an HTML email generator. Output a COMPLETE, self-contained HTML email for Sonal's key-discussion digest.
Output ONLY raw HTML — no markdown, no code fences, no explanation.
CRITICAL: Use ONLY inline styles with hardcoded hex colors — NO CSS classes, NO <style> blocks, NO CSS variables. Every element must have style= attributes directly. This will be rendered in Gmail which strips all <style> blocks.

MEETING TIME (IST): {meeting_time}
TODAY: {today}

CARD DATA (each card has Section 1-4, use exactly as given):
{card_data}

---
SECTION RULES:
- Section 1 (priority=1): "🔴 IMPORTANT · URGENT" — header bg #B03A2E, even-row bg #FBEAE9, badge "N cards — Act now"
- Section 2 (priority=2): "🟡 IMPORTANT · NOT URGENT" — header bg #9A7D0A, even-row bg #FEFDE7, badge "N cards — Needs involvement"
- Section 3 (priority=3): "🟠 URGENT · NOT IMPORTANT" — header bg #A04000, even-row bg #FEF5E7, badge "N overdue — Quick updates needed" — INCLUDE ONLY cards that are overdue OR have 0% checklist progress
- Section 4 (priority=4): "🟢 NOT URGENT · NOT IMPORTANT" — header bg #1A5C35 — NO table, just a single text line listing card names with checklist % separated by ·

CHECKLIST PROGRESS BAR COLORS:
- 0%: bar bg #C0392B
- 1–39%: bar bg #D4AC0D
- 40–74%: bar bg #CA6F1E
- 75–99%: bar bg #1E8449
- 100%: bar bg #1E8449

MUST-DISCUSS BOX: include all Section 1 cards + any Section 2/3 cards that are overdue with 0% progress. One bullet per card: card name in bold + one sharp sentence on status and what decision/update is needed from Ravi.

---
OUTPUT the following HTML exactly, filling in real data. Do NOT change any inline styles. Do NOT add <style> blocks.

<!DOCTYPE html>
<html><body style="background:#F0F2F5;font-family:Arial,sans-serif;color:#1C1C1C;padding:32px 16px;margin:0;">
<div style="background:#FFFFFF;max-width:860px;margin:0 auto;padding:28px 30px;box-shadow:0 4px 40px rgba(0,0,0,0.13);">

  <div style="border-left:5px solid #C0392B;padding:2px 0 2px 14px;margin:10px 0 6px;">
    <div style="font-size:15px;font-weight:700;color:#1C2A3A;font-family:Arial,sans-serif;">Ravi Meeting — Key Discussion Areas</div>
    <div style="font-size:10.5px;color:#566573;margin-top:3px;font-weight:300;">Ravi (Trello) meeting today &nbsp;·&nbsp; {today} &nbsp;·&nbsp; For Sonal's reference only</div>
  </div>
  <hr style="border:none;border-top:1px solid #D5D8DC;margin:10px 0 14px;">

  <div style="font-size:11.5px;color:#566573;line-height:1.6;margin-bottom:16px;padding:10px 14px;background:#F8F9FA;border-left:3px solid #C0392B;">
    Hi <strong style="color:#1C2A3A;">Sonal</strong>,<br><br>
    Your Ravi (Trello) meeting is in <strong style="color:#1C2A3A;">~2 hours</strong> at <strong style="color:#1C2A3A;">{meeting_time} IST</strong>. Here are the key areas to discuss, organised by urgency.
  </div>

  <!-- Stats strip — 5 colour boxes -->
  <table style="width:100%;border-collapse:separate;border-spacing:6px 0;margin-bottom:16px;"><tr>
    <td style="background:#1C2A3A;color:white;padding:10px 12px;text-align:center;">
      <div style="font-size:22px;font-weight:700;line-height:1;font-family:Arial,sans-serif;">TOTAL_COUNT</div>
      <div style="font-size:9.5px;font-weight:600;text-transform:uppercase;margin-top:4px;opacity:0.85;">Total Open</div>
    </td>
    <td style="background:#FBEAE9;color:#B03A2E;padding:10px 12px;text-align:center;">
      <div style="font-size:22px;font-weight:700;line-height:1;font-family:Arial,sans-serif;">S1_COUNT</div>
      <div style="font-size:9.5px;font-weight:600;text-transform:uppercase;margin-top:4px;">Immediate Action</div>
    </td>
    <td style="background:#FEFDE7;color:#9A7D0A;padding:10px 12px;text-align:center;">
      <div style="font-size:22px;font-weight:700;line-height:1;font-family:Arial,sans-serif;">S2_COUNT</div>
      <div style="font-size:9.5px;font-weight:600;text-transform:uppercase;margin-top:4px;">Needs Involvement</div>
    </td>
    <td style="background:#FEF5E7;color:#A04000;padding:10px 12px;text-align:center;">
      <div style="font-size:22px;font-weight:700;line-height:1;font-family:Arial,sans-serif;">OVERDUE_COUNT</div>
      <div style="font-size:9.5px;font-weight:600;text-transform:uppercase;margin-top:4px;">Overdue Items</div>
    </td>
    <td style="background:#EAFAF1;color:#1A5C35;padding:10px 12px;text-align:center;">
      <div style="font-size:22px;font-weight:700;line-height:1;font-family:Arial,sans-serif;">ON_TRACK_COUNT</div>
      <div style="font-size:9.5px;font-weight:600;text-transform:uppercase;margin-top:4px;">On Track</div>
    </td>
  </tr></table>

  <!-- Must-Discuss box -->
  <div style="border:1.5px solid #C0392B;background:#FBEAE9;padding:14px 18px;margin-bottom:14px;">
    <div style="font-size:11.5px;font-weight:700;color:#B03A2E;margin-bottom:8px;text-transform:uppercase;letter-spacing:0.5px;font-family:Arial,sans-serif;">⚡ Must-Discuss Today</div>
    <ul style="list-style:none;padding:0;margin:0;">
      [INSERT one <li> per must-discuss card using this exact markup:]
      <li style="font-size:11px;line-height:1.6;padding-left:14px;position:relative;margin-bottom:4px;color:#1C1C1C;"><span style="position:absolute;left:0;color:#C0392B;font-weight:700;">▸</span><strong style="color:#1C2A3A;">CARD NAME:</strong> one sharp sentence on current status + what decision or update is needed from Ravi.</li>
    </ul>
  </div>

  [INSERT Section 1 block if cards exist — use this exact structure:]
  <div style="margin-bottom:14px;">
    <div style="padding:7px 12px;background:#B03A2E;color:white;font-size:11px;font-weight:700;font-family:Arial,sans-serif;">🔴 &nbsp;IMPORTANT · URGENT <span style="font-size:9px;background:rgba(255,255,255,0.25);padding:2px 7px;border-radius:10px;margin-left:8px;">N cards — Act now</span></div>
    <table style="width:100%;border-collapse:collapse;font-size:11px;">
      <thead><tr style="background:#2C3E50;">
        <th style="color:white;font-size:9.5px;font-weight:700;text-transform:uppercase;padding:5px 10px;text-align:left;border-right:1px solid #1C2A3A;width:36%;">Card</th>
        <th style="color:white;font-size:9.5px;font-weight:700;text-transform:uppercase;padding:5px 10px;text-align:left;border-right:1px solid #1C2A3A;width:18%;">Due</th>
        <th style="color:white;font-size:9.5px;font-weight:700;text-transform:uppercase;padding:5px 10px;text-align:left;width:46%;">Discussion Prompt</th>
      </tr></thead>
      <tbody>
        [INSERT one <tr> per card — odd rows bg #FFFFFF, even rows bg #FBEAE9:]
        <tr style="background:#FBEAE9;border-bottom:1px solid #D5D8DC;">
          <td style="padding:8px 10px;border-right:1px solid #D5D8DC;vertical-align:top;"><span style="font-weight:600;font-size:11.5px;color:#1C1C1C;display:block;">CARD NAME</span><span style="font-size:9px;color:#566573;font-family:Arial,sans-serif;">LIST NAME</span></td>
          <td style="padding:8px 10px;border-right:1px solid #D5D8DC;vertical-align:top;">
            [if overdue:] <span style="font-size:10.5px;color:#C0392B;font-weight:700;">DD Mon YYYY <span style="display:inline-block;font-size:8px;background:#C0392B;color:white;padding:1px 4px;border-radius:2px;margin-left:3px;font-weight:600;">OVERDUE</span></span>
            [if not overdue:] <span style="font-size:10.5px;color:#1C1C1C;">DD Mon YYYY</span>
            [if has checklist:] <div style="height:4px;border-radius:2px;background:#E5E8E8;overflow:hidden;width:80%;margin-top:3px;"><div style="height:100%;border-radius:2px;background:BAR_COLOR;width:PCT%;"></div></div><span style="font-size:9px;color:#566573;display:block;margin-top:2px;">DONE/TOTAL · N pending</span>
          </td>
          <td style="padding:8px 10px;vertical-align:top;line-height:1.5;">
            <span style="font-size:8.5px;font-weight:700;text-transform:uppercase;letter-spacing:0.4px;color:#566573;display:block;margin-bottom:3px;">Ask Ravi</span>
            <em style="color:#1C2A3A;font-size:10.5px;">"Verbatim question Sonal can read out directly."</em>
          </td>
        </tr>
      </tbody>
    </table>
  </div>

  [INSERT Section 2 block if cards exist — same table structure, header bg #9A7D0A, even-row bg #FEFDE7, badge "N cards — Needs involvement"]

  [INSERT Section 3 block if qualifying cards exist — same table structure, header bg #A04000, even-row bg #FEF5E7, badge "N overdue — Quick updates needed". ONLY include cards that are overdue OR have 0 checklist items done.]

  [INSERT Section 4 block if cards exist — header only + single text line, NO table:]
  <div style="margin-bottom:14px;">
    <div style="padding:7px 12px;background:#1A5C35;color:white;font-size:11px;font-weight:700;font-family:Arial,sans-serif;">🟢 &nbsp;NOT URGENT · NOT IMPORTANT <span style="font-size:9px;background:rgba(255,255,255,0.25);padding:2px 7px;border-radius:10px;margin-left:8px;">N cards — On track, no action needed</span></div>
    <div style="border:1px solid #D5D8DC;border-top:none;padding:10px 12px;font-size:11px;color:#566573;line-height:1.7;">
      The following are progressing normally — raise only if Ravi flags a blocker:<br>
      <span style="color:#1C1C1C;">CARD NAME (PCT%) &nbsp;·&nbsp; CARD NAME (PCT%) &nbsp;·&nbsp; ...</span>
    </div>
  </div>

  <div style="margin-top:18px;text-align:center;font-size:9.5px;color:#AAB2BD;border-top:1px solid #D5D8DC;padding-top:10px;font-family:Arial,sans-serif;">Valuecart Automation &nbsp;·&nbsp; ravi.digest@valuecart.in &nbsp;·&nbsp; Sent 2 hours before meeting &nbsp;·&nbsp; For Sonal's eyes only</div>
</div>
</body></html>
"""


def generate_html_with_claude(prompt: str) -> str:
    client = _claude_client()
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=24000,
        messages=[{"role": "user", "content": prompt}],
    )
    log.info("Claude stop_reason=%s tokens_used=%s", msg.stop_reason, msg.usage.output_tokens)
    html = msg.content[0].text.strip()
    # Strip markdown code fences if Claude wrapped the output
    if html.startswith("```"):
        html = html.split("\n", 1)[1] if "\n" in html else html
        if html.endswith("```"):
            html = html[:-3].rstrip()
    return html


# ── Job 1: Ravi's digest ──────────────────────────────────────────────────────

def job_ravi_digest():
    log.info("=== Job 1: Ravi digest check ===")
    now = datetime.now(IST)
    run_now = os.environ.get("RUN_NOW") == "true"
    target = now if run_now else now + timedelta(days=2)

    event = get_ravi_meeting(target)
    if not event:
        log.info("No Ravi (Trello) meeting %s. Skipping.", "today (RUN_NOW)" if run_now else "in 2 days")
        return

    meeting_date = target.strftime("%A, %d %B %Y") + (" [TEST]" if run_now else "")
    log.info("Meeting found on %s. Fetching cards...", meeting_date)

    cards = fetch_all_open_cards()
    if not cards:
        send_email(RAVI_EMAIL, f"📋 Ravi (Trello) — Pre-Meeting Prep | {meeting_date}",
                   "<p>✅ All clear — no open cards on the board.</p>")
        return

    card_data = build_card_data_text(cards)
    prompt = RAVI_PROMPT_TEMPLATE.format(
        meeting_date=meeting_date,
        today=now.strftime("%A, %d %B %Y"),
        card_data=card_data,
    )
    html_body = generate_html_with_claude(prompt)
    send_email(
        RAVI_EMAIL,
        f"📋 Ravi (Trello) — Pre-Meeting Prep | {meeting_date}",
        html_body,
    )
    log.info("Ravi digest sent.")


# ── Job 2: Sonal's digest ─────────────────────────────────────────────────────

def job_sonal_digest():
    log.info("=== Job 2: Sonal digest check ===")
    now = datetime.now(IST)

    event = get_ravi_meeting(now)
    if not event:
        log.info("No Ravi (Trello) meeting today. Skipping.")
        return

    # Parse meeting start time
    start = event["start"]
    if "dateTime" in start:
        meeting_dt = datetime.fromisoformat(start["dateTime"]).astimezone(IST)
        meeting_time_str = meeting_dt.strftime("%I:%M %p")
    else:
        meeting_time_str = "time unspecified"

    log.info("Meeting found today at %s. Fetching cards...", meeting_time_str)

    cards = fetch_all_open_cards()
    if not cards:
        send_email(SONAL_EMAIL, f"📋 Ravi Meeting Today — Key Discussion Areas | {meeting_time_str} IST",
                   "<p>✅ All clear — no open cards on the Ravi board.</p>")
        return

    card_data = build_card_data_text(cards)
    prompt = SONAL_PROMPT_TEMPLATE.format(
        meeting_time=meeting_time_str,
        today=now.strftime("%A, %d %B %Y"),
        card_data=card_data,
    )
    html_body = generate_html_with_claude(prompt)
    send_email(
        SONAL_EMAIL,
        f"📋 Ravi Meeting Today — Key Discussion Areas | {meeting_time_str} IST",
        html_body,
    )
    log.info("Sonal digest sent.")


# ── Scheduler ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if os.environ.get("RUN_NOW") == "true":
        log.info("RUN_NOW=true — firing both jobs immediately.")
        job_ravi_digest()
        job_sonal_digest()

    scheduler = BlockingScheduler(timezone=IST)

    # 9:00 AM IST daily — check both conditions
    scheduler.add_job(job_ravi_digest,  "cron", hour=9, minute=0, id="ravi_digest")
    scheduler.add_job(job_sonal_digest, "cron", hour=9, minute=0, id="sonal_digest")

    log.info("Scheduler started. Both jobs @ 09:00 IST daily.")
    try:
        scheduler.start()
    except KeyboardInterrupt:
        log.info("Scheduler stopped.")
