"""Persist the nightly run state on Spikeball's Google Drive.

The cloud routine's sandbox starts empty every night and `spike/data/` is deliberately not part of
the code bundle, so the Amazon Orders API watermark and order files (`spike/data/amazon/`) and the
prior-run state (`spike/data/state_prev.json`) would not carry over. This module zips those files,
uploads them to a single Drive file (id in Doppler as SPIKEBALL_DASH_STATE_FILE_ID) after a passing
run, and downloads them at the start of the next run. Same Google refresh token as the publishers.

CLI (from the project root, secrets loaded):
  python spike/routine/state_sync.py init      # create the Drive file from the local spike/data
  python spike/routine/state_sync.py download  # restore spike/data from Drive
  python spike/routine/state_sync.py upload    # push the local state to Drive
No secrets printed. No input().
"""
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import google_auth  # noqa: E402
import doppler_env  # noqa: E402  (sibling module, spike/routine/)

SPIKE = Path(__file__).resolve().parents[1]
DATA = SPIKE / "data"
SECRET_NAME = "SPIKEBALL_DASH_STATE_FILE_ID"
STATE_NAME = "spikeball-finance-dashboard-state.zip"
STATE_PATHS = ["amazon/state.json", "amazon/orders_NA.jsonl", "amazon/orders_EU.jsonl",
               "amazon/order_items_NA.jsonl", "amazon/order_items_EU.jsonl", "state_prev.json"]


def _headers():
    return {"Authorization": f"Bearer {google_auth.get_access_token()}"}


def build_zip() -> bytes:
    buf = io.BytesIO()
    n = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in STATE_PATHS:
            p = DATA / rel
            if p.is_file():
                z.write(p, rel)
                n += 1
    print(f"[state_sync] bundled {n} state files, {buf.tell():,} bytes")
    return buf.getvalue()


def upload(file_id: str | None = None) -> str:
    import requests
    file_id = file_id or os.environ.get(SECRET_NAME) or None
    data = build_zip()
    if file_id:
        r = requests.patch(f"https://www.googleapis.com/upload/drive/v3/files/{file_id}?uploadType=media",
                           headers={**_headers(), "Content-Type": "application/zip"}, data=data, timeout=180)
        if r.status_code != 404:
            r.raise_for_status()
            print(f"[state_sync] state uploaded (updated in place)")
            return file_id
    meta = json.dumps({"name": STATE_NAME, "mimeType": "application/zip"})
    body = (b"--b0undary\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n" + meta.encode() +
            b"\r\n--b0undary\r\nContent-Type: application/zip\r\n\r\n" + data + b"\r\n--b0undary--")
    import requests as rq
    r = rq.post("https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id",
                headers={**_headers(), "Content-Type": "multipart/related; boundary=b0undary"}, data=body, timeout=180)
    r.raise_for_status()
    fid = r.json()["id"]
    project = doppler_env.doppler_project()
    config = doppler_env.doppler_config()
    if project and config:
        res = subprocess.run(["doppler", "secrets", "set", f"{SECRET_NAME}={fid}", "--project", project,
                              "--config", config, "--silent"], capture_output=True, text=True)
        print(f"[state_sync] state file created; id stored in Doppler (rc={res.returncode})")
    else:
        print(f"DOPPLER_WRITEBACK_SKIPPED set DOPPLER_PROJECT and DOPPLER_CONFIG to persist {SECRET_NAME}")
    return fid


def download() -> bool:
    import requests
    file_id = os.environ.get(SECRET_NAME)
    (DATA / "amazon").mkdir(parents=True, exist_ok=True)
    if not file_id:
        print("[state_sync] no SPIKEBALL_DASH_STATE_FILE_ID in environment; starting without prior state")
        return False
    r = requests.get(f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media", headers=_headers(), timeout=180)
    if r.status_code == 404:
        print("[state_sync] state file not found on Drive; starting without prior state")
        return False
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    z.extractall(DATA)
    print(f"[state_sync] restored {len(z.namelist())} state files from Drive ({len(r.content):,} bytes)")
    return True


def main() -> int:
    op = sys.argv[1] if len(sys.argv) > 1 else ""
    if op == "init":
        print(upload(None if not os.environ.get(SECRET_NAME) else os.environ[SECRET_NAME]))
    elif op == "download":
        download()
    elif op == "upload":
        upload()
    else:
        print("usage: state_sync.py init|download|upload")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
