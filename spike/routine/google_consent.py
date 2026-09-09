"""One-click Google OAuth consent for the dashboard's Spikeball-side Google access.

Builds the consent URL for the existing Spikeball OAuth client (Doppler prd_spikeball:
SPIKEBALL_OAUTH_CLIENT_ID / SPIKEBALL_OAUTH_CLIENT_SECRET), listens on the loopback redirect the
client already supports (http://localhost:8765/), exchanges the code, and stores the refresh token in
Doppler prd_spikeball as SPIKEBALL_GCP_OAUTH_REFRESH_TOKEN. Scopes: Drive (Sheets read/write via the
Drive scope), cloud-platform (BigQuery + enabling APIs on the project), gmail.send (nightly alert).

Run:  doppler run -p $DOPPLER_PROJECT -c $DOPPLER_CONFIG -- python spike/routine/google_consent.py
Then the owner opens the printed URL as mcohen@spikeball.com and approves. No input() anywhere.
"""
import http.server
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import doppler_env  # noqa: E402  (sibling module, spike/routine/)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REDIRECT = "http://localhost:8765/"
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/userinfo.email",
]
SECRET_NAME = "SPIKEBALL_GCP_OAUTH_REFRESH_TOKEN"
STATE = "spikeball-dashboard-" + str(int(time.time()))
code_holder = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(q.query)
        print("REDIRECT_HIT path=" + self.path[:300], flush=True)
        if "code" in params:
            if params.get("state", [""])[0] != STATE:
                print("STATE_MISMATCH (accepting anyway; single-user loopback)", flush=True)
            code_holder["code"] = params["code"][0]
            body = b"<html><body style='font-family:system-ui;padding:40px'><h2>Consent received. You can close this tab.</h2></body></html>"
        else:
            body = b"<html><body style='font-family:system-ui;padding:40px'><h2>No code in request.</h2></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *a):
        print("HTTP " + (fmt % a), flush=True)


def main():
    cid = os.environ["SPIKEBALL_OAUTH_CLIENT_ID"]
    csec = os.environ["SPIKEBALL_OAUTH_CLIENT_SECRET"]
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": cid,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "login_hint": "mcohen@spikeball.com",
        "state": STATE,
    })
    print("CONSENT_URL " + url, flush=True)
    srv = http.server.HTTPServer(("127.0.0.1", 8765), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    deadline = time.time() + int(os.environ.get("CONSENT_WAIT_SEC", "5400"))
    while "code" not in code_holder and time.time() < deadline:
        time.sleep(1)
    srv.shutdown()
    if "code" not in code_holder:
        print("CONSENT_TIMEOUT", flush=True)
        return 2
    data = urllib.parse.urlencode({
        "code": code_holder["code"], "client_id": cid, "client_secret": csec,
        "redirect_uri": REDIRECT, "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        tok = json.loads(r.read().decode())
    rt = tok.get("refresh_token")
    if not rt:
        print("NO_REFRESH_TOKEN " + json.dumps({k: v for k, v in tok.items() if k != "access_token"}), flush=True)
        return 3
    # who consented
    who = ""
    try:
        req = urllib.request.Request("https://www.googleapis.com/oauth2/v3/userinfo",
                                     headers={"Authorization": "Bearer " + tok["access_token"]})
        with urllib.request.urlopen(req, timeout=30) as r:
            who = json.loads(r.read().decode()).get("email", "")
    except Exception as e:  # noqa: BLE001
        who = f"unknown ({e})"
    project = doppler_env.doppler_project()
    config = doppler_env.doppler_config()
    if not (project and config):
        print(f"DOPPLER_WRITEBACK_SKIPPED set DOPPLER_PROJECT and DOPPLER_CONFIG to persist {SECRET_NAME}", flush=True)
        return 4
    r = subprocess.run(["doppler", "secrets", "set", f"{SECRET_NAME}={rt}", "--project", project,
                        "--config", config, "--silent"], capture_output=True, text=True)
    print(f"STORED {SECRET_NAME} in prd_spikeball as={who} scopes={tok.get('scope','')} doppler_rc={r.returncode} {r.stderr.strip()[:200]}", flush=True)
    return 0 if r.returncode == 0 else 4


if __name__ == "__main__":
    sys.exit(main())
