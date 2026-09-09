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
"""
import argparse
import base64
import json
import os
import sys
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
