"""Shared Google auth + REST helpers for the dashboard publishers (FD2/FD7).

Exchanges the Spikeball-side OAuth refresh token (Doppler prd_spikeball
SPIKEBALL_GCP_OAUTH_REFRESH_TOKEN, minted by routine/google_consent.py as
mcohen@spikeball.com, scopes drive + cloud-platform + gmail.send + userinfo.email)
for a short-lived access token, caches it in memory for the life of the process, and
refreshes on demand or on a 401. All Google API calls in this subproject (Sheets,
Drive, BigQuery, Cloud Resource Manager, Service Usage, Gmail) go through
`authed_request()` here -- plain `requests`, no google-api-python-client.

Never prints or logs a token value. Credentials from env only (doppler run --project
$DOPPLER_PROJECT --config $DOPPLER_CONFIG -- python <script>.py).
"""
import json
import os
import sys
import time

import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
CRM_V1_URL = "https://cloudresourcemanager.googleapis.com/v1/projects"
SERVICE_USAGE_URL = "https://serviceusage.googleapis.com/v1"

NEEDED = ("SPIKEBALL_OAUTH_CLIENT_ID", "SPIKEBALL_OAUTH_CLIENT_SECRET", "SPIKEBALL_GCP_OAUTH_REFRESH_TOKEN")

REQUIRED_APIS = (
    "bigquery.googleapis.com",
    "sheets.googleapis.com",
    "drive.googleapis.com",
    "gmail.googleapis.com",
)

_token_cache = {"access_token": None, "expires_at": 0.0}
_project_cache = {"id": None}


class GoogleAuthError(RuntimeError):
    pass


def load_env():
    missing = [k for k in NEEDED if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            f"Missing env vars: {missing}. Run via "
            f"doppler run --project $DOPPLER_PROJECT --config $DOPPLER_CONFIG -- python <script>.py "
            f"(SPIKEBALL_GCP_OAUTH_REFRESH_TOKEN is written by spike/routine/google_consent.py "
            f"once the owner completes consent)."
        )
    return {k: os.environ[k] for k in NEEDED}


def get_access_token(force_refresh=False):
    """Returns a bearer access token, cached in memory, refreshed on demand or when
    within 60s of expiry. Raises GoogleAuthError (message never contains the refresh
    token or client secret) on failure."""
    now = time.time()
    if not force_refresh and _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]
    env = load_env()
    data = {
        "client_id": env["SPIKEBALL_OAUTH_CLIENT_ID"],
        "client_secret": env["SPIKEBALL_OAUTH_CLIENT_SECRET"],
        "refresh_token": env["SPIKEBALL_GCP_OAUTH_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }
    try:
        r = requests.post(TOKEN_URL, data=data, timeout=30)
    except requests.exceptions.RequestException as e:
        raise GoogleAuthError(f"token refresh request failed: {e}") from e
    if r.status_code != 200:
        body = r.text[:500]
        raise GoogleAuthError(f"token refresh HTTP {r.status_code}: {body}")
    tok = r.json()
    access_token = tok.get("access_token")
    if not access_token:
        raise GoogleAuthError("token refresh response had no access_token")
    _token_cache["access_token"] = access_token
    _token_cache["expires_at"] = now + int(tok.get("expires_in", 3600))
    return access_token


def _parse_retry_after(resp):
    """Returns the Retry-After header as an int number of seconds, or None if absent
    or unparseable (e.g. an HTTP-date rather than a seconds count -- rare from Google
    APIs but not assumed)."""
    val = resp.headers.get("Retry-After")
    if not val:
        return None
    try:
        return max(1, int(float(val)))
    except (TypeError, ValueError):
        return None


def authed_request(method, url, retry_on_401=True, max_retries=5, max_429_retries=3, **kwargs):
    """requests.request() with a bearer Authorization header attached, one retry on a
    fresh token after a 401, exponential backoff on 500/502/503/504, and a SEPARATE,
    more patient retry budget for 429 (rate limiting): honors Retry-After when the API
    sends one, else sleeps 65s (Sheets API's write quota is per-minute), up to
    max_429_retries times independent of the general max_retries budget -- so a write
    burst against a per-minute quota never kills a run the way an 8s-capped exponential
    backoff did (proven live: Sheets API 429 on 'Write requests per minute per user').
    Does not raise_for_status -- callers inspect the Response and decide (matches the
    repo's existing SuiteQL helper style in spike/_lib.py)."""
    headers = dict(kwargs.pop("headers", None) or {})
    timeout = kwargs.pop("timeout", 120)
    tried_refresh = False
    other_attempts = 0
    retries_429 = 0
    while True:
        headers["Authorization"] = f"Bearer {get_access_token()}"
        try:
            resp = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
        except requests.exceptions.RequestException as e:
            other_attempts += 1
            if other_attempts >= max_retries:
                raise GoogleAuthError(f"{method} {url} failed after {max_retries} attempts: {e}") from e
            time.sleep(min(2 ** other_attempts, 30))
            continue
        if resp.status_code == 401 and retry_on_401 and not tried_refresh:
            tried_refresh = True
            get_access_token(force_refresh=True)
            continue
        if resp.status_code == 429:
            if retries_429 >= max_429_retries:
                return resp
            wait = _parse_retry_after(resp) or 65
            retries_429 += 1
            print(f"[google_auth] HTTP 429 on {method} {url}, waiting {wait}s "
                  f"(429 retry {retries_429}/{max_429_retries})", file=sys.stderr)
            time.sleep(wait)
            continue
        if resp.status_code in (500, 502, 503, 504):
            other_attempts += 1
            if other_attempts >= max_retries:
                return resp
            wait = min(2 ** other_attempts, 30)
            print(f"[google_auth] HTTP {resp.status_code} on {method} {url}, backoff {wait}s "
                  f"(attempt {other_attempts})", file=sys.stderr)
            time.sleep(wait)
            continue
        return resp


def whoami():
    """Returns the userinfo dict for the token owner (email, sub, etc.) -- structural
    info only, safe to print."""
    resp = authed_request("GET", USERINFO_URL)
    if resp.status_code != 200:
        raise GoogleAuthError(f"userinfo HTTP {resp.status_code}: {resp.text[:500]}")
    return resp.json()


def resolve_project_id(override=None):
    """Resolves the GCP project id that owns the Spikeball OAuth client, per PRD
    ruling R2/R21: the project number is the numeric prefix of
    SPIKEBALL_OAUTH_CLIENT_ID (e.g. "1066762651676-xxxx.apps.googleusercontent.com").
    Tries Cloud Resource Manager's filtered list first, falls back to an unfiltered
    list scanned client-side if the filter call is denied. Cached in memory."""
    if override:
        return override
    if _project_cache["id"]:
        return _project_cache["id"]
    env = load_env()
    client_id = env["SPIKEBALL_OAUTH_CLIENT_ID"]
    project_number = client_id.split("-")[0].strip()
    if not project_number.isdigit():
        raise GoogleAuthError(
            f"could not parse a project number from SPIKEBALL_OAUTH_CLIENT_ID prefix "
            f"({client_id[:20]}...); pass --project explicitly"
        )
    resp = authed_request("GET", CRM_V1_URL, params={"filter": f"projectNumber:{project_number}"})
    if resp.status_code == 200:
        projects = resp.json().get("projects", [])
        if projects:
            pid = projects[0]["projectId"]
            _project_cache["id"] = pid
            return pid
    # Bootstrapping gap: Cloud Resource Manager itself may not be enabled yet on a
    # brand-new project. Service Usage accepts a raw project NUMBER (no CRM needed),
    # so enable CRM that way and retry once before falling back to a full list scan.
    if resp is not None and resp.status_code == 403 and "has not been used" in resp.text:
        print(f"[google_auth] Cloud Resource Manager API not yet enabled on project "
              f"{project_number}; enabling it via Service Usage and retrying", file=sys.stderr)
        enable_url = f"{SERVICE_USAGE_URL}/projects/{project_number}/services/cloudresourcemanager.googleapis.com:enable"
        enable_resp = authed_request("POST", enable_url, json={})
        if enable_resp.status_code == 200:
            for attempt in range(6):
                time.sleep(5)
                resp2 = authed_request("GET", CRM_V1_URL, params={"filter": f"projectNumber:{project_number}"})
                if resp2.status_code == 200:
                    projects = resp2.json().get("projects", [])
                    if projects:
                        pid = projects[0]["projectId"]
                        _project_cache["id"] = pid
                        return pid
                    break
                if resp2.status_code != 403:
                    break
        else:
            print(f"[google_auth] could not enable Cloud Resource Manager API: HTTP "
                  f"{enable_resp.status_code}: {enable_resp.text[:300]}", file=sys.stderr)
    print("[google_auth] filtered project list unavailable, falling back to full list scan", file=sys.stderr)
    resp = authed_request("GET", CRM_V1_URL)
    if resp.status_code != 200:
        raise GoogleAuthError(
            f"could not resolve GCP project id: filtered lookup and full list both failed "
            f"(full list HTTP {resp.status_code}: {resp.text[:500]})"
        )
    for p in resp.json().get("projects", []):
        if str(p.get("projectNumber")) == project_number:
            _project_cache["id"] = p["projectId"]
            return p["projectId"]
    raise GoogleAuthError(
        f"no project with number {project_number} visible to this token; pass --project explicitly"
    )


def list_enabled_services(project_id):
    """Returns the set of enabled service names (e.g. 'bigquery.googleapis.com') on
    the given project."""
    enabled = set()
    url = f"{SERVICE_USAGE_URL}/projects/{project_id}/services"
    params = {"filter": "state:ENABLED", "pageSize": 200}
    while True:
        resp = authed_request("GET", url, params=params)
        if resp.status_code != 200:
            raise GoogleAuthError(f"list services HTTP {resp.status_code}: {resp.text[:500]}")
        body = resp.json()
        for svc in body.get("services", []):
            name = svc.get("config", {}).get("name") or svc.get("name", "").split("/")[-1]
            if name:
                enabled.add(name)
        token = body.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token
    return enabled


def ensure_apis_enabled(project_id, apis, dry_run=False):
    """Enables any of `apis` not already enabled on `project_id`. Returns
    (already_enabled: list, newly_enabled: list). Idempotent -- safe to call every
    run."""
    enabled = list_enabled_services(project_id)
    already = [a for a in apis if a in enabled]
    to_enable = [a for a in apis if a not in enabled]
    newly = []
    for api in to_enable:
        if dry_run:
            print(f"[google_auth] [dry-run] would enable {api} on {project_id}")
            continue
        url = f"{SERVICE_USAGE_URL}/projects/{project_id}/services/{api}:enable"
        resp = authed_request("POST", url, json={})
        if resp.status_code not in (200,):
            raise GoogleAuthError(f"enable {api} on {project_id} HTTP {resp.status_code}: {resp.text[:500]}")
        # Enabling is an Operation; the REST response for services:enable is
        # synchronous-shaped (returns the Operation with done true in practice for
        # this API) but we don't block on operation polling here -- a second
        # ensure_apis_enabled() call next run will show it as already-enabled once
        # propagation completes, which is the only thing that matters operationally.
        newly.append(api)
    return already, newly


def main():
    """CLI self-check: python google_auth.py --check
    Confirms the refresh token works, prints who consented, the resolved GCP project,
    and which of the required APIs are enabled. Never prints token values."""
    import argparse
    ap = argparse.ArgumentParser(description="Google auth self-check for the Spikeball dashboard.")
    ap.add_argument("--check", action="store_true", help="Run the full self-check (default if no flag given).")
    ap.add_argument("--project", default=None, help="Override the resolved GCP project id.")
    args = ap.parse_args()

    try:
        load_env()
    except SystemExit as e:
        print(f"AUTH_CHECK_BLOCKED {e}")
        return 2

    try:
        get_access_token()
        who = whoami()
        project_id = resolve_project_id(override=args.project)
        enabled = list_enabled_services(project_id)
        missing = [a for a in REQUIRED_APIS if a not in enabled]
        print(json.dumps({
            "auth": "ok",
            "email": who.get("email"),
            "project_id": project_id,
            "required_apis": list(REQUIRED_APIS),
            "enabled": sorted(a for a in REQUIRED_APIS if a in enabled),
            "missing": missing,
        }, indent=2))
        return 0
    except GoogleAuthError as e:
        print(f"AUTH_CHECK_FAILED {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
