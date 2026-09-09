"""Loads prd_spikeball's Doppler secrets into os.environ WITHOUT requiring the Doppler
CLI or a `doppler run` wrapper. Exists because the cloud routine's sandbox has neither
the CLI installed nor (under the sandbox's default "Trusted" network mode) reachability
to cli.doppler.com -- api.doppler.com IS reachable once the owner sets the routine's
network access to Full (see run_nightly.py --diagnose), which is the one thing this
module actually depends on.

run_nightly.py imports this and calls ensure_loaded() first thing, so plain
`python spike/routine/run_nightly.py` (no `doppler run` prefix) works standalone in the
cloud routine. Nothing changes for local/manual runs that already use
`doppler run --project $DOPPLER_PROJECT --config $DOPPLER_CONFIG -- python ...`:
ensure_loaded() detects the environment is already populated and does nothing.

Env-first: when the two sentinel variables below are already present in os.environ
(the cloud routine's own "Environment variables" block, or an outer `doppler run`),
ensure_loaded() returns immediately -- no Doppler CLI call, no Doppler API call, no
DOPPLER_TOKEN required. Doppler is a fallback only, for the case where the caller wants
this module to fetch the secrets itself.

Precedence: (1) already-wrapped environment (env-first -- doppler run already ran, or
the caller's environment variables already carry every secret), (2) the Doppler CLI if
it happens to be present, (3) the Doppler REST API using DOPPLER_TOKEN. Never prints a
secret value or the token.
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

DOPPLER_API_URL = "https://api.doppler.com/v3/configs/config/secrets/download"

# Presence of these two (from very different secret families -- NetSuite creds and the
# Google OAuth client) is a reliable signal the whole prd_spikeball config is already
# loaded, without hardcoding every secret name here.
_SENTINELS = ("NETSUITE_ACCOUNT_ID", "SPIKEBALL_OAUTH_CLIENT_ID")


def doppler_project():
    """DOPPLER_PROJECT env value, or None when unset -- no literal fallback. A Doppler
    service token (DOPPLER_TOKEN) is scoped to exactly one project/config, so both the
    Doppler CLI and the REST download endpoint accept a call with no project/config at
    all when the token itself already pins one; _load_via_cli()/_load_via_api() omit
    the flags/query params entirely in that case rather than guessing a value. Read at
    call time (never cached at import) so a caller -- or a test -- can set/change the
    env var any time before this is read."""
    return os.environ.get("DOPPLER_PROJECT") or None


def doppler_config():
    """DOPPLER_CONFIG env value, or None when unset. See doppler_project()."""
    return os.environ.get("DOPPLER_CONFIG") or None


def _already_loaded():
    return all(os.environ.get(k) for k in _SENTINELS)


def _load_via_cli():
    cmd = ["doppler", "secrets", "download"]
    project = doppler_project()
    config = doppler_config()
    if project:
        cmd += ["--project", project]
    if config:
        cmd += ["--config", config]
    cmd += ["--format", "json", "--no-file"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if r.returncode != 0:
        return False
    try:
        secrets = json.loads(r.stdout)
    except json.JSONDecodeError:
        return False
    for k, v in secrets.items():
        if isinstance(v, str):
            os.environ.setdefault(k, v)
    return True


def _load_via_api():
    token = os.environ.get("DOPPLER_TOKEN")
    if not token:
        print("[doppler_env] DOPPLER_TOKEN not set; cannot fetch secrets via the Doppler API",
              file=sys.stderr)
        return False
    params = {}
    project = doppler_project()
    config = doppler_config()
    if project:
        params["project"] = project
    if config:
        params["config"] = config
    params["format"] = "json"
    url = f"{DOPPLER_API_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            secrets = json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"[doppler_env] could not reach api.doppler.com: {e} -- network access is likely "
              f"not set to Full for this routine (see run_nightly.py --diagnose)", file=sys.stderr)
        return False
    except json.JSONDecodeError as e:
        print(f"[doppler_env] Doppler API returned unparseable JSON: {e}", file=sys.stderr)
        return False
    for k, v in secrets.items():
        if isinstance(v, str):
            os.environ.setdefault(k, v)
    return True


def ensure_loaded():
    """Idempotent. Returns True once prd_spikeball's secrets are confirmed present in
    os.environ (by any of the three means above), False if none of them worked --
    callers must fail loudly on False, never proceed with partial credentials.

    Env-first: when the sentinels are already set (the caller's own "Environment
    variables", or an outer `doppler run`), returns immediately -- no Doppler CLI call,
    no Doppler API call, no DOPPLER_TOKEN required."""
    if _already_loaded():
        print("[doppler_env] secrets already present in the environment; Doppler not contacted", flush=True)
        return True
    if _load_via_cli() and _already_loaded():
        return True
    if _load_via_api() and _already_loaded():
        return True
    return False


def main():
    ok = ensure_loaded()
    print("DOPPLER_ENV_OK" if ok else "DOPPLER_ENV_FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
