#!/usr/bin/env python3
"""
moodle_ntfy.py — Poll Moodle iCal feed, push deadline reminders to ntfy.sh

CONFIG (via environment variables):
  ICAL_URL     — Full iCal URL from Moodle (with authtoken parameter)
  NTFY_TOPIC   — ntfy.sh topic name (choose a long unguessable string)
  NTFY_SERVER  — Default: https://ntfy.sh
  STATE_FILE   — Default: ./state.json (path to dedupe state)
  LOOKAHEAD_H  — Default: 240 (hours to look ahead = 10 days)
  OFFSETS_H    — Default: "168,72,24,1" (hours before event: H-7, H-3, H-1, 1hr)
  POLL_INTERVAL_H — Default: 1 (your cron interval; only fire reminders whose
                  notify_at moment is within this window of "now" — prevents
                  sending stale missed reminders on first run)
  TZ_OFFSET    — Default: 7 (hours from UTC for display, 7 = WIB)
  DRY_RUN      — Default: 0 (set to 1 to skip actually sending)

USAGE:
  ICAL_URL="https://..." NTFY_TOPIC="mytopic" python3 moodle_ntfy.py
  ICAL_URL="..." NTFY_TOPIC="..." DRY_RUN=1 python3 moodle_ntfy.py  # test, no send
"""
import os
import sys
import json
import hashlib
from pathlib import Path
from datetime import datetime, timedelta, timezone, time as dtime

import requests
from icalendar import Calendar


def get_config():
    if not os.environ.get("ICAL_URL"):
        sys.exit("ERROR: ICAL_URL env var not set. Set it to your Moodle iCal URL.")
    if not os.environ.get("NTFY_TOPIC"):
        sys.exit("ERROR: NTFY_TOPIC env var not set. Pick a long unguessable topic name.")
    offsets_raw = os.environ.get("OFFSETS_H", "168,72,24,1")
    return {
        "ical_url": os.environ["ICAL_URL"],
        "ntfy_topic": os.environ["NTFY_TOPIC"],
        "ntfy_server": os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
        "state_file": Path(os.environ.get("STATE_FILE", "state.json")),
        "lookahead_h": int(os.environ.get("LOOKAHEAD_H", "240")),
        "offsets_h": [int(x) for x in offsets_raw.split(",") if x.strip()],
        "poll_interval_h": float(os.environ.get("POLL_INTERVAL_H", "1")),
        "tz_offset": int(os.environ.get("TZ_OFFSET", "7")),
        "dry_run": os.environ.get("DRY_RUN", "0") == "1",
    }


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def load_state(path):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            log(f"WARN: state file {path} corrupted, starting fresh")
    return {"sent": {}}


def save_state(path, state):
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def fetch_ical(url):
    r = requests.get(url, timeout=30, allow_redirects=True)
    r.raise_for_status()
    # Moodle sometimes returns HTML login page if token is invalid
    ct = r.headers.get("Content-Type", "")
    if "text/calendar" not in ct and "ics" not in ct.lower() and not r.text.strip().startswith("BEGIN:VCALENDAR"):
        raise ValueError(f"Expected iCal feed, got Content-Type '{ct}'. URL/token invalid?")
    if not r.text.strip().startswith("BEGIN:VCALENDAR"):
        raise ValueError("Response does not start with BEGIN:VCALENDAR. Probably an error page.")
    return r.text


def parse_events(ical_text):
    cal = Calendar.from_ical(ical_text)
    events = []
    for ev in cal.walk("VEVENT"):
        summary = str(ev.get("SUMMARY", "(tanpa judul)"))
        uid = str(ev.get("UID", ""))
        dtstart = ev.get("DTSTART")
        if dtstart is None:
            continue
        start = dtstart.dt
        # Normalize to UTC-aware datetime
        if isinstance(start, datetime):
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            start_utc = start.astimezone(timezone.utc)
        else:
            # date-only event (datetime.date)
            start_utc = datetime.combine(start, dtime.min, tzinfo=timezone.utc)
        events.append({
            "uid": uid,
            "summary": summary,
            "start_utc": start_utc,
        })
    return events


def fmt_local(dt_utc, tz_offset_h):
    tz = timezone(timedelta(hours=tz_offset_h))
    local = dt_utc.astimezone(tz)
    return local.strftime("%a %d %b %Y, %H:%M")


def offset_label(offset_h):
    if offset_h >= 24:
        days = offset_h // 24
        return f"H-{days}"
    if offset_h == 1:
        return "H-1jam"
    return f"H-{offset_h}jam"


def send_ntfy(cfg, title, message, tags="alarm_clock,tada"):
    url = f"{cfg['ntfy_server']}/{cfg['ntfy_topic']}"
    headers = {
        "Title": title,
        "Tags": tags,
        "Priority": "high",
    }
    if cfg["dry_run"]:
        log(f"[DRY-RUN] Would POST to {url}")
        log(f"  Title: {title}")
        log(f"  Body : {message}")
        return
    r = requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=15)
    r.raise_for_status()
    log(f"Sent: {title}")


def dedupe_key(uid, offset_h):
    raw = f"{uid}|{offset_h}"
    return hashlib.sha1(raw.encode()).hexdigest()


def main():
    cfg = get_config()
    now = datetime.now(timezone.utc)
    lookahead_end = now + timedelta(hours=cfg["lookahead_h"])

    log(f"Config: topic={cfg['ntfy_topic']}, lookahead={cfg['lookahead_h']}h, "
        f"offsets={cfg['offsets_h']}h, tz=UTC+{cfg['tz_offset']}, dry_run={cfg['dry_run']}")

    try:
        ical_text = fetch_ical(cfg["ical_url"])
    except Exception as e:
        log(f"FATAL: failed to fetch iCal: {e}")
        sys.exit(2)

    events = parse_events(ical_text)
    log(f"Parsed {len(events)} events from feed")

    state = load_state(cfg["state_file"])
    sent = state.setdefault("sent", {})
    # Cleanup: remove entries older than 30 days
    cutoff_iso = (now - timedelta(days=30)).isoformat(timespec="seconds")
    sent = {k: v for k, v in sent.items() if v.get("sent_at", "") >= cutoff_iso}
    state["sent"] = sent

    to_send = []

    for ev in events:
        start = ev["start_utc"]
        # Skip past events (more than 1 hour ago)
        if start < now - timedelta(hours=1):
            continue
        # Skip events too far ahead
        if start > lookahead_end:
            continue

        for offset_h in cfg["offsets_h"]:
            notify_at = start - timedelta(hours=offset_h)
            # Only fire if notify_at is within the last poll interval window
            # (i.e. we just crossed the reminder moment). This prevents stale
            # missed reminders all firing on the first run after setup.
            time_since_notify = now - notify_at
            window = timedelta(hours=cfg["poll_interval_h"])
            if not (timedelta(0) <= time_since_notify <= window):
                continue
            if start < now:
                continue
            key = dedupe_key(ev["uid"], offset_h)
            if key in sent:
                continue
            label = offset_label(offset_h)
            time_str = fmt_local(start, cfg["tz_offset"])
            title = f"[{label}] {ev['summary']}"
            msg = (f"Tugas: {ev['summary']}\n"
                   f"Deadline: {time_str} WIB\n"
                   f"(reminder {label})")
            to_send.append((key, title, msg))

    if not to_send:
        log("No new notifications due.")
        save_state(cfg["state_file"], state)
        return

    log(f"Sending {len(to_send)} notifications...")
    sent_count = 0
    for key, title, msg in to_send:
        try:
            send_ntfy(cfg, title, msg)
            sent[key] = {
                "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "title": title,
            }
            sent_count += 1
        except Exception as e:
            log(f"Failed to send '{title}': {e}")

    save_state(cfg["state_file"], state)
    log(f"Done. Sent {sent_count}/{len(to_send)}.")


if __name__ == "__main__":
    main()
