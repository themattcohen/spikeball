"""Shared SuiteQL helper -- COPIED VERBATIM from
spikeball/financial-dashboard/research/probes/_lib.py (agent 1a's proven, retrying,
paginated SuiteQL client) so extract.py has no import-path dependency on the research
folder. Do not diverge from the source; if a fix is needed there, port it here too.
READ-ONLY. SELECT statements only. Credentials from env vars only (doppler run -p $DOPPLER_PROJECT
-c $DOPPLER_CONFIG -- python <script>.py).
"""
import json
import os
import sys
import time

import requests
from requests_oauthlib import OAuth1

NEEDED = (
    "NETSUITE_ACCOUNT_ID",
    "NETSUITE_CONSUMER_KEY",
    "NETSUITE_CONSUMER_SECRET",
    "NETSUITE_TOKEN_ID",
    "NETSUITE_TOKEN_SECRET",
)


def load_env():
    missing = [k for k in NEEDED if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            f"Missing env vars: {missing}. Run via "
            f"doppler run --project $DOPPLER_PROJECT --config $DOPPLER_CONFIG -- python <script>.py"
        )
    return {k: os.environ[k] for k in NEEDED}


def make_auth(env):
    return OAuth1(
        client_key=env["NETSUITE_CONSUMER_KEY"],
        client_secret=env["NETSUITE_CONSUMER_SECRET"],
        resource_owner_key=env["NETSUITE_TOKEN_ID"],
        resource_owner_secret=env["NETSUITE_TOKEN_SECRET"],
        signature_method="HMAC-SHA256",
        signature_type="auth_header",
        realm=env["NETSUITE_ACCOUNT_ID"],
    )


class SuiteQLError(RuntimeError):
    pass


def suiteql(env, sql, page_size=1000, max_rows=None, verbose=False, throttle_sec=0.3):
    """Paginated SuiteQL SELECT. Returns list[dict]. Raises SuiteQLError with the NS
    error body on failure (never swallows an error into an empty list)."""
    url_base = f"https://{env['NETSUITE_ACCOUNT_ID'].lower()}.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql"
    headers = {"Content-Type": "application/json", "Prefer": "transient"}
    auth = make_auth(env)
    rows = []
    offset = 0
    page = 0
    warned_no_order_by = False
    while True:
        url = f"{url_base}?limit={page_size}&offset={offset}"
        time.sleep(throttle_sec)
        r = None
        last_exc = None
        for attempt in range(5):
            try:
                r = requests.post(url, json={"q": sql}, auth=auth, headers=headers, timeout=600)
                if r.status_code in (500, 503, 429):
                    wait = (2 ** attempt) * 2
                    if verbose:
                        print(f"  HTTP {r.status_code}, backoff {wait}s (attempt {attempt+1})", file=sys.stderr)
                    time.sleep(wait)
                    continue
                break
            except requests.exceptions.RequestException as e:
                last_exc = e
                if attempt == 4:
                    raise
                time.sleep(2 ** attempt)
        if r is None:
            raise last_exc
        if r.status_code >= 400:
            raise SuiteQLError(f"HTTP {r.status_code}: {r.text[:2000]}\nQuery: {sql[:800]}")
        body = r.json()
        if "o:errorDetails" in body:
            raise SuiteQLError(f"SuiteQL 200+errorDetails: {json.dumps(body['o:errorDetails'])[:2000]}\nQuery: {sql[:800]}")
        items = body.get("items", [])
        rows.extend(items)
        page += 1
        if verbose:
            print(f"  page {page}: +{len(items)} rows (total={len(rows)}, hasMore={body.get('hasMore')})", file=sys.stderr)
        if max_rows is not None and len(rows) >= max_rows:
            return rows[:max_rows]
        if not body.get("hasMore"):
            break
        if not warned_no_order_by and "order by" not in sql.lower():
            print(
                "WARNING: paginating with no ORDER BY -- page-boundary duplication/drop risk. "
                "Query (truncated): " + " ".join(sql.split())[:200],
                file=sys.stderr,
            )
            warned_no_order_by = True
        offset += page_size
    return rows


def try_suiteql(env, sql, **kw):
    """Like suiteql() but catches SuiteQLError and returns (rows_or_None, error_str_or_None)."""
    try:
        return suiteql(env, sql, **kw), None
    except SuiteQLError as e:
        return None, str(e)


def fnum(v):
    try:
        return float(v)
    except Exception:
        return 0.0
