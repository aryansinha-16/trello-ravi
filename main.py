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
You are an email formatter. Generate a polished HTML email body for Ravi's pre-meeting digest.

MEETING DATE: {meeting_date}
TODAY: {today}

CARD DATA:
{card_data}

RULES:
1. HTML only — no markdown, no code fences.
2. Sections in order: RED (1), AMBER (2), ORANGE (3), GREEN (4).
3. Each section has a coloured header and a table: Card | List | Due Date | Checklist Progress.
4. Due date styling: OVERDUE = bold red + "[OVERDUE]", TODAY = bold red + "[TODAY]", DUE SOON = orange, normal = plain.
5. Checklist cell: show done/total (pct%). If pending>0: show "N pending" in red. If 0% and total>0: "⚠ Not started" red. If 100%: "✓ Complete" green. If no checklist: "No checklist" grey italic.
6. Add an action box (amber border, light yellow bg) at the bottom listing: cards with 0% due within 7 days, overdue cards, cards due within 2 days.
7. Start with: "Hi Ravi,<br><br>There's a Ravi (Trello) meeting on {meeting_date}. Please update checklist progress and add comments on overdue cards before the meeting."
8. Footer: "Valuecart Automation · ravi.digest@valuecart.in · Sent 2 days before meeting"
9. Output ONLY the HTML body (starting from <div or <table — no <html><head><body> wrapper).
"""

SONAL_PROMPT_TEMPLATE = """
You are an email formatter. Generate a compact HTML email body for Sonal's key-discussion digest.

MEETING TIME (IST): {meeting_time}
TODAY: {today}

CARD DATA:
{card_data}

RULES:
1. HTML only — no markdown, no code fences.
2. Keep the total HTML under 6000 characters. Be concise — short labels, no verbose descriptions.
3. Start with: "Hi Sonal,<br><br>Your Ravi (Trello) meeting is at {meeting_time} — approximately 2 hours away. Here are the key areas to discuss."
4. Stats strip: 4 small inline boxes — Total Open, Immediate Action (Section 1 count), Overdue, On Track.
5. Must-Discuss box (red border): bullet list only — card name + one short question (max 12 words each). No extra prose.
6. Section 1 (RED "Must-Discuss"): compact table — Card Name | Due/Checklist | Ask Ravi (≤10 words).
7. Section 2 (AMBER): same compact table.
8. Section 3 (ORANGE): only overdue or 0%-progress cards, same compact table.
9. Section 4 (GREEN): one line listing card names + checklist % separated by " · ". No table.
10. Footer: one line — "Valuecart Automation · Sent 2 hours before meeting"
11. Output ONLY the HTML body (no <html><head><body> wrapper).
"""


def generate_html_with_claude(prompt: str) -> str:
    client = _claude_client()
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
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
    target = now + timedelta(days=2)

    event = get_ravi_meeting(target)
    if not event:
        log.info("No Ravi (Trello) meeting in 2 days. Skipping.")
        return

    meeting_date = target.strftime("%A, %d %B %Y")
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
    scheduler = BlockingScheduler(timezone=IST)

    # 9:00 AM IST daily — check both conditions
    scheduler.add_job(job_ravi_digest,  "cron", hour=9, minute=0, id="ravi_digest")
    scheduler.add_job(job_sonal_digest, "cron", hour=9, minute=0, id="sonal_digest")

    log.info("Scheduler started. Both jobs @ 09:00 IST daily.")
    try:
        scheduler.start()
    except KeyboardInterrupt:
        log.info("Scheduler stopped.")
