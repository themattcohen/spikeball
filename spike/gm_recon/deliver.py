"""Delivery for the monthly gross-margin reconciliation: Drive upload into a shared
folder, reader access for the recipients, a plain-text email with the Drive link, and
one row on the `gm_recon_log` Sheet tab.

Every step is independent and returns (ok, detail); run_recon.py records each result
and a failure in any of them yields RECON_PARTIAL_OK. Google calls go through
google_auth.authed_request (same refresh token as the nightly). Nothing here writes to
`run_log`, `refresh_requests`, `meta` or any protected tab, and nothing takes the
refresh gate's lock.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SPIKE = _HERE.parent
for _p in (_SPIKE, _SPIKE / "routine", _HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import google_auth  # noqa: E402
import rules  # noqa: E402

DRIVE = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3"
SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FOLDER_MIME = "application/vnd.google-apps.folder"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
NEVER_WRITE_TABS = {"run_log", "refresh_requests", "meta"}


def _q_literal(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def recipients(cfg: dict) -> list[str]:
    raw = os.environ.get(cfg["env"]["recipients"]) or os.environ.get(cfg["env"]["recipients_fallback"]) or ""
    return [a.strip() for a in raw.split(",") if a.strip()]


def ensure_folder(cfg: dict, log=print) -> str:
    """The Drive folder id: env SPIKEBALL_RECON_FOLDER_ID when set, else the folder with
    the configured name in the identity's Drive root (created when missing)."""
    fid = os.environ.get(cfg["env"]["folder_id"])
    if fid:
        return fid
    name = cfg["drive_folder_name"]
    q = (f"name='{_q_literal(name)}' and mimeType='{FOLDER_MIME}' and trashed=false and 'root' in parents")
    r = google_auth.authed_request("GET", f"{DRIVE}/files", params={"q": q, "fields": "files(id,name)", "spaces": "drive"})
    if r.status_code != 200:
        raise RuntimeError(f"Drive folder search HTTP {r.status_code}: {r.text[:300]}")
    files = r.json().get("files", [])
    if files:
        fid = files[0]["id"]
    else:
        r = google_auth.authed_request("POST", f"{DRIVE}/files", params={"fields": "id"},
                                       json={"name": name, "mimeType": FOLDER_MIME, "parents": ["root"]})
        if r.status_code != 200:
            raise RuntimeError(f"Drive folder create HTTP {r.status_code}: {r.text[:300]}")
        fid = r.json()["id"]
        log(f"[gm_recon] created Drive folder '{name}'")
    log(f"RECON_FOLDER_ID {fid} (set {cfg['env']['folder_id']} to this id to pin the folder)")
    return fid


def upload_xlsx(path: Path, folder_id: str) -> tuple[str, str]:
    """Uploads the workbook into the folder as a native Google Sheet (Drive converts the
    xlsx, and Sheets recalculates every formula, so the preview shows figures rather
    than blanks). The Sheet is named after the file without `.xlsx`. When a Sheet with
    that name is already in the folder, its media is replaced with the new xlsx content
    (Drive converts on update too). The local .xlsx is left exactly as written.
    Returns (file_id, webViewLink)."""
    path = Path(path)
    data = path.read_bytes()
    name = path.stem
    q = (f"name='{_q_literal(name)}' and '{_q_literal(folder_id)}' in parents and "
         f"mimeType='{SHEET_MIME}' and trashed=false")
    r = google_auth.authed_request("GET", f"{DRIVE}/files", params={"q": q, "fields": "files(id)", "spaces": "drive"})
    if r.status_code != 200:
        raise RuntimeError(f"Drive file search HTTP {r.status_code}: {r.text[:300]}")
    files = r.json().get("files", [])
    if files:
        fid = files[0]["id"]
        r = google_auth.authed_request("PATCH", f"{DRIVE_UPLOAD}/files/{fid}",
                                       params={"uploadType": "media", "fields": "id,webViewLink"},
                                       headers={"Content-Type": XLSX_MIME}, data=data, timeout=180)
    else:
        meta = json.dumps({"name": name, "mimeType": SHEET_MIME, "parents": [folder_id]})
        body = (b"--b0undary\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n" + meta.encode() +
                b"\r\n--b0undary\r\nContent-Type: " + XLSX_MIME.encode() + b"\r\n\r\n" + data + b"\r\n--b0undary--")
        r = google_auth.authed_request("POST", f"{DRIVE_UPLOAD}/files",
                                       params={"uploadType": "multipart", "fields": "id,webViewLink"},
                                       headers={"Content-Type": "multipart/related; boundary=b0undary"}, data=body,
                                       timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"Drive upload HTTP {r.status_code}: {r.text[:300]}")
    j = r.json()
    return j["id"], j.get("webViewLink") or f"https://docs.google.com/spreadsheets/d/{j['id']}/edit"


def ensure_readers(folder_id: str, emails: list[str]) -> list[str]:
    """Idempotent reader permission on the folder for each address, no notification
    email. Returns the addresses newly granted."""
    r = google_auth.authed_request("GET", f"{DRIVE}/files/{folder_id}/permissions",
                                   params={"fields": "permissions(id,emailAddress,role,type)"})
    if r.status_code != 200:
        raise RuntimeError(f"Drive permissions list HTTP {r.status_code}: {r.text[:300]}")
    have = {(p.get("emailAddress") or "").lower() for p in r.json().get("permissions", [])}
    added = []
    for e in emails:
        if e.lower() in have:
            continue
        r = google_auth.authed_request("POST", f"{DRIVE}/files/{folder_id}/permissions",
                                       params={"sendNotificationEmail": "false", "fields": "id"},
                                       json={"type": "user", "role": "reader", "emailAddress": e})
        if r.status_code != 200:
            raise RuntimeError(f"Drive permission create HTTP {r.status_code}: {r.text[:300]}")
        added.append(e)
    return added


def email_subject(model: dict) -> str:
    asof = rules.parse_ns_date(model["asof"])
    return (f"Spikeball GM reconciliation {model['month']}: {len(model['errors'])} errors, "
            f"{len(model['timing'])} entries, as of {rules.fmt_md(asof)}")


def email_body(model: dict, link: str, tables_text: str) -> str:
    parts = [" ".join(model["answer"]), "", tables_text, "", f"Workbook: {link or 'not uploaded'}", "",
             "Nothing was changed in NetSuite."]
    return "\n".join(parts)


def send_email(subject: str, body: str, to: list[str]) -> tuple[bool, str]:
    if not to:
        return False, "no recipients (set SPIKEBALL_RECON_TO or SPIKEBALL_ALERT_TO)"
    import alert  # lazy: importing it loads secrets; only needed when an email goes out
    fails = []
    for addr in to:
        ok, detail = alert.send_alert(subject, body, to=addr)
        if not ok:
            fails.append(f"{addr}: {detail[:150]}")
    if fails:
        return False, "; ".join(fails)
    return True, f"sent to {len(to)}"


def append_log_row(sheet_id: str, tab: str, header: list[str], row: dict) -> str:
    """Appends one row to `tab` (created when missing, header written when empty).
    Refuses the nightly's own tabs."""
    if tab in NEVER_WRITE_TABS:
        raise ValueError(f"refusing to write the nightly's tab {tab!r}")
    import publish_sheet  # lazy: only the log step needs it
    existing = publish_sheet.get_existing_tabs(sheet_id)
    if tab not in existing:
        new_id = (max(existing.values()) + 1) if existing else 1
        r = google_auth.authed_request("POST", f"{SHEETS_BASE}/{sheet_id}:batchUpdate",
                                       json={"requests": [{"addSheet": {"properties": {"sheetId": new_id, "title": tab}}}]})
        if r.status_code != 200:
            raise RuntimeError(f"add tab HTTP {r.status_code}: {r.text[:300]}")
    rng = urllib.parse.quote(f"'{tab}'!1:1", safe="")
    r = google_auth.authed_request("GET", f"{SHEETS_BASE}/{sheet_id}/values/{rng}")
    first = (r.json().get("values") or [[]])[0] if r.status_code == 200 else []
    if not first:
        a1 = urllib.parse.quote(f"'{tab}'!A1", safe="")
        r = google_auth.authed_request("PUT", f"{SHEETS_BASE}/{sheet_id}/values/{a1}",
                                       params={"valueInputOption": "RAW"}, json={"values": [header]})
        if r.status_code != 200:
            raise RuntimeError(f"write header HTTP {r.status_code}: {r.text[:300]}")
    whole = urllib.parse.quote(f"'{tab}'", safe="")
    values = [["" if row.get(k) is None else row.get(k) for k in header]]
    r = google_auth.authed_request("POST", f"{SHEETS_BASE}/{sheet_id}/values/{whole}:append",
                                   params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                                   json={"values": values})
    if r.status_code != 200:
        raise RuntimeError(f"append HTTP {r.status_code}: {r.text[:300]}")
    return "written"
