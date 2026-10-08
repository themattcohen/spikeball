#!/usr/bin/env python3
"""Sends the nightly-routine failure alert via the Gmail API, as mcohen@spikeball.com
(the SPIKEBALL_GCP_OAUTH_REFRESH_TOKEN owner). The recipient is Doppler
SPIKEBALL_ALERT_TO (owner ruling R21, PRD Section 12: "alert should go to me, cut over
to casandra later") -- never hardcoded here; --to overrides it for manual testing only.

Usage:
    doppler run --project $DOPPLER_PROJECT --config $DOPPLER_CONFIG -- python spike/routine/alert.py \\
        --subject "Spikeball Finance nightly: test alert" --body "test body"
    python spike/routine/alert.py --to someone@example.com --subject S --body B --dry-run

Importable: run_nightly.py calls send_alert(subject, body) directly (same process, same
already-loaded env) rather than shelling out.

De-duplication (owner ruling 2026-10-08): the hourly gate re-runs the full pipeline every
hour once the last success is more than 20 hours old, so one persistent failure used to
produce one identical email per hour (five on 2026-10-07). The rule is now ONE alert per
distinct failing reason per window (SPIKEBALL_ALERT_DEDUPE_HOURS, default 24). The
record of what was sent or suppressed lives on the finance Sheet's `alert_log` tab
(header: sent_at_utc, sent_at_mt, verdict, signature, action), created on first use.
`alert_signature()` turns a failure into a stable key (numbers and timestamps vary
between identical failures, so the key is built from the failing check names and, for
the closed-months check, the month keys -- never from the raw numbers); `should_send()`
reads the tab; `log_alert()` appends to it. Both Sheet helpers fail OPEN: any error
reading or writing the tab means the alert is sent as before and the problem is printed.
send_alert() itself is unchanged.
"""
import argparse
import base64
import json
import math
import os
import re
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import google_auth  # noqa: E402
try:
    import doppler_env  # noqa: E402
    doppler_env.ensure_loaded()  # standalone invocations (routine step 4) need the Doppler secrets too
except Exception as _e:  # noqa: BLE001
    print(f"[alert] doppler_env not loaded: {_e}", file=sys.stderr)

GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
FROM_ADDR = "mcohen@spikeball.com"

SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
ALERT_LOG_TAB = "alert_log"
ALERT_LOG_HEADER = ["sent_at_utc", "sent_at_mt", "verdict", "signature", "action"]
DEDUPE_HOURS_DEFAULT = 24
DEDUPE_HOURS_ENV = "SPIKEBALL_ALERT_DEDUPE_HOURS"

# The closed-months check names each month it found moved as "YYYY-MM: ..." in its
# detail; those month keys are the only part of that detail that identifies the reason.
_MONTH_KEY_RE = re.compile(r"(\d{4}-\d{2}):")
_SIGNATURE_REASON_MAX = 80


# ---------------------------------------------------------------------------
# De-duplication: signature, window, Sheet log
# ---------------------------------------------------------------------------

def alert_signature(code, reason, verdict="NIGHTLY_FAIL", checks=None):
    """A stable key for "the same failure". With `checks` (the extract's meta.checks
    dict, name -> {"pass": bool, "detail": str}): `<verdict>:checks:` plus the sorted
    failing check names, where g_closed_months_stable carries the sorted month keys its
    detail names, e.g. `NIGHTLY_FAIL:checks:g_closed_months_stable[2026-09]`. A check
    failing on a different month, or a different set of failing checks, is a different
    signature. Without `checks` (or when none of them is failing): `<verdict>:<code>:`
    plus the first line of `reason` with every digit removed, whitespace collapsed,
    lowercased and cut to 80 characters, so the varying numbers and timestamps inside an
    otherwise identical reason do not defeat the match."""
    if isinstance(checks, dict):
        failing = []
        for name in sorted(checks):
            entry = checks[name]
            if name == "all_pass" or not isinstance(entry, dict) or entry.get("pass") is not False:
                continue
            label = str(name)
            if name == "g_closed_months_stable":
                months = sorted(set(_MONTH_KEY_RE.findall(str(entry.get("detail") or ""))))
                if months:
                    label = f"{label}[{','.join(months)}]"
            failing.append(label)
        if failing:
            return f"{verdict}:checks:" + ",".join(failing)
    first_line = str(reason or "").splitlines()[0] if str(reason or "").strip() else ""
    normalized = re.sub(r"\d+", "", first_line)
    normalized = re.sub(r"\s+", " ", normalized).strip().lower()
    return f"{verdict}:{code}:" + normalized[:_SIGNATURE_REASON_MAX]


def dedupe_window_hours():
    """The suppression window in hours from env SPIKEBALL_ALERT_DEDUPE_HOURS, default
    24. Non-numeric or negative values fall back to 24; 0 disables suppression (every
    failure emails, and is still logged). Read at call time so tests and the routine's
    environment can change it without a code change; never raises."""
    raw = os.environ.get(DEDUPE_HOURS_ENV)
    if raw is None or not raw.strip():
        return DEDUPE_HOURS_DEFAULT
    try:
        value = float(raw.strip())
    except ValueError:
        return DEDUPE_HOURS_DEFAULT
    if value < 0 or not math.isfinite(value):
        return DEDUPE_HOURS_DEFAULT
    return int(value) if value.is_integer() else value


def to_mt_iso(dt_utc):
    """An aware UTC datetime rendered on the Mountain clock in the same shape
    publish_sheet.now_mt_iso() writes to run_log (e.g. 2026-10-07T03:28:11-06:00)."""
    from zoneinfo import ZoneInfo
    return dt_utc.astimezone(ZoneInfo("America/Denver")).isoformat(timespec="seconds")


def _to_utc_z(dt_utc):
    return dt_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc_z(s):
    """'YYYY-MM-DDTHH:MM:SSZ' (the sent_at_utc column) to an aware UTC datetime, or
    None when it does not parse."""
    if not isinstance(s, str) or not s.strip():
        return None
    s = s.strip()
    iso = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _alert_log_range(a1):
    return urllib.parse.quote(f"'{ALERT_LOG_TAB}'!{a1}", safe="")


def should_send(signature, sheet_id, now_utc, window_hours):
    """Returns (send: bool, last_sent_utc: datetime|None). Reads the Sheet's alert_log
    tab and finds the latest row with this signature whose action is "sent"; when that
    row is inside the window the alert is a duplicate and send is False. A window of 0
    (or less) means always send, without reading the Sheet. Any transport error, a
    non-200 response (including a missing tab, which the API reports as 400 "Unable to
    parse range"), or an unreadable sheet body yields (True, None) and prints why: a
    broken de-duplication must never cost an alert."""
    if not window_hours or window_hours <= 0:
        return True, None
    url = f"{SHEETS_BASE}/{sheet_id}/values/{_alert_log_range('A:E')}"
    try:
        resp = google_auth.authed_request("GET", url)
    except Exception as e:  # noqa: BLE001  fail open
        print(f"[alert] dedupe read failed ({e}); sending the alert")
        return True, None
    if resp.status_code != 200:
        text = (resp.text or "")[:300]
        if resp.status_code == 400 and "Unable to parse range" in text:
            print(f"[alert] {ALERT_LOG_TAB} tab not present yet; sending the alert")
        else:
            print(f"[alert] dedupe read HTTP {resp.status_code}: {text}; sending the alert")
        return True, None
    try:
        values = resp.json().get("values") or []
    except Exception as e:  # noqa: BLE001  fail open
        print(f"[alert] dedupe read returned an unreadable body ({e}); sending the alert")
        return True, None
    last_sent = None
    unparsable = 0
    for row in values[1:]:  # row 1 is the header
        if not isinstance(row, list) or len(row) < 5:
            continue
        if str(row[3]) != signature or str(row[4]).strip() != "sent":
            continue
        dt = _parse_utc_z(row[0])
        if dt is None:
            unparsable += 1
            continue
        if last_sent is None or dt > last_sent:
            last_sent = dt
    if unparsable:
        print(f"[alert] dedupe: ignored {unparsable} {ALERT_LOG_TAB} row(s) with an unreadable sent_at_utc")
    if last_sent is not None and now_utc - last_sent < timedelta(hours=window_hours):
        return False, last_sent
    return True, last_sent


def _append_alert_log_row(sheet_id, row):
    return google_auth.authed_request(
        "POST", f"{SHEETS_BASE}/{sheet_id}/values/{_alert_log_range('A:E')}:append",
        params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
        json={"values": [row]},
    )


def _create_alert_log_tab(sheet_id):
    """Adds the alert_log tab and writes its header row. Raises on a non-200."""
    r = google_auth.authed_request(
        "POST", f"{SHEETS_BASE}/{sheet_id}:batchUpdate",
        json={"requests": [{"addSheet": {"properties": {"title": ALERT_LOG_TAB}}}]},
    )
    if r.status_code != 200:
        raise RuntimeError(f"addSheet {ALERT_LOG_TAB} HTTP {r.status_code}: {(r.text or '')[:300]}")
    r = google_auth.authed_request(
        "PUT", f"{SHEETS_BASE}/{sheet_id}/values/{_alert_log_range('A1:E1')}",
        params={"valueInputOption": "RAW"},
        json={"values": [ALERT_LOG_HEADER]},
    )
    if r.status_code != 200:
        raise RuntimeError(f"{ALERT_LOG_TAB} header PUT HTTP {r.status_code}: {(r.text or '')[:300]}")


def log_alert(sheet_id, now_utc, verdict, signature, action):
    """Appends one row [sent_at_utc, sent_at_mt, verdict, signature, action] to the
    Sheet's alert_log tab, creating the tab (with its header) when the append reports
    it missing. Returns True when the row landed. Never raises: every failure is printed
    and swallowed, because the alert itself has already been handled by the caller."""
    row = [_to_utc_z(now_utc), to_mt_iso(now_utc), str(verdict), str(signature), str(action)]
    try:
        resp = _append_alert_log_row(sheet_id, row)
        if resp.status_code == 400 and "Unable to parse range" in (resp.text or ""):
            print(f"[alert] creating the {ALERT_LOG_TAB} tab")
            _create_alert_log_tab(sheet_id)
            resp = _append_alert_log_row(sheet_id, row)
        if resp.status_code != 200:
            print(f"[alert] {ALERT_LOG_TAB} append HTTP {resp.status_code}: {(resp.text or '')[:300]}")
            return False
        return True
    except Exception as e:  # noqa: BLE001  never fail the run over the log
        print(f"[alert] {ALERT_LOG_TAB} append failed: {e}")
        return False


class AlertError(RuntimeError):
    pass


def build_raw_message(to_addr, subject, body):
    msg = MIMEText(body)
    msg["To"] = to_addr
    msg["From"] = FROM_ADDR
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    return raw


def send_alert(subject, body, to=None, dry_run=False):
    """Returns (ok: bool, detail: str). detail is the Gmail message id on success, or
    an error/blocker description on failure. Never raises -- run_nightly.py's failure
    path should not itself fail on a broken alert."""
    to_addr = to or os.environ.get("SPIKEBALL_ALERT_TO")
    if not to_addr:
        return False, ("SPIKEBALL_ALERT_TO not set in Doppler prd_spikeball and no --to given; "
                        "cannot send alert (owner ruling R21: alert recipient must come from "
                        "Doppler, never hardcoded)")
    if dry_run:
        print(f"[dry-run] would send Gmail from {FROM_ADDR} to {to_addr}: subject={subject!r}")
        return True, "dry_run"
    try:
        project_id = google_auth.resolve_project_id()
        already, newly = google_auth.ensure_apis_enabled(project_id, ["gmail.googleapis.com"])
        if newly:
            print(f"[alert] enabled {newly} on project {project_id}")
    except google_auth.GoogleAuthError as e:
        return False, f"could not ensure gmail.googleapis.com enabled: {e}"
    raw = build_raw_message(to_addr, subject, body)
    try:
        resp = google_auth.authed_request("POST", GMAIL_SEND_URL, json={"raw": raw})
    except google_auth.GoogleAuthError as e:
        return False, f"auth error sending alert: {e}"
    if resp.status_code != 200:
        return False, f"Gmail send HTTP {resp.status_code}: {resp.text[:500]}"
    msg_id = resp.json().get("id", "")
    return True, msg_id


def main():
    ap = argparse.ArgumentParser(description="Send the Spikeball dashboard nightly alert via Gmail.")
    ap.add_argument("--to", default=None, help="Override recipient (default: Doppler SPIKEBALL_ALERT_TO).")
    ap.add_argument("--subject", required=True)
    ap.add_argument("--body", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ok, detail = send_alert(args.subject, args.body, to=args.to, dry_run=args.dry_run)
    if ok:
        print(f"ALERT_SENT id={detail} to={args.to or os.environ.get('SPIKEBALL_ALERT_TO')}")
        return 0
    print(f"ALERT_FAILED {detail}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
