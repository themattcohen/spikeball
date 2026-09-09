#!/usr/bin/env python3
"""Nightly orchestration executed by the Claude Code cloud routine (PRD FD7). Runs
under `doppler run --project $DOPPLER_PROJECT --config $DOPPLER_CONFIG -- python
spikeball/financial-dashboard/spike/routine/run_nightly.py` (see PROMPT.md); every
subprocess it spawns inherits that same env, so nothing here re-wraps a call in
`doppler run` itself.

Order: (1) fetch the prior run's state (BigQuery run_state, falling back to a local
file) so extract.py's E2(f)/(g) checks have something to diff against; (2) run
extract.py; (3) if checks pass, publish_sheet.py, publish_bq.py, and the artifact
rebuild each run INDEPENDENTLY (one failing does not skip or abort the others -- see
"Verdicts" below and README.md's "Sheets write-quota" section for why this matters);
(4) print one of three verdicts and, on the two failure-shaped ones, send the operator
alert.

Verdicts (see README.md "Exit codes and verdicts" for the full table):
- `NIGHTLY_OK` (exit 0) -- all three (Sheet, BigQuery, artifact) succeeded. The
  routine's own Claude session reads the printed summary to know to republish the
  artifact (Python cannot call the Artifact tool -- see PROMPT.md).
- `NIGHTLY_PARTIAL_OK <reason>` (exit 3) -- the artifact built AND at least one store
  published, but not all three. Still republish-worthy; PROMPT.md treats this like
  NIGHTLY_OK for that purpose. Alerted.
- `NIGHTLY_FAIL <reason>` (exit 2 checks / 3 publish / 4 extract) -- checks failed,
  extract crashed, the artifact failed to build, or neither store published. Alerted.

With `--gate` (PRD-month-refresh.md Section 5 M5, the routine prompt (ROUTINE-PROMPT.md)): a fourth verdict,
`NIGHTLY_SKIP <reason>` (exit 0), prints and returns BEFORE any of the above runs --
`refresh_gate.decide()` reads `run_log` and `refresh_requests` first and only starts
the pipeline when its decision table says to. Without `--gate` nothing here changes:
`run()` calls the same pipeline (`run_pipeline()`) with `trigger="nightly"` every time,
exactly as before this flag existed.

Never commits data files. The one exception -- spike/data/state_prev.json, the local
fallback for the next run's prior-state fetch when BigQuery is unreachable -- is
refreshed here but committed only by the routine session (PROMPT.md), never by this
script (no git calls live in this file).
"""
import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent          # .../spike/routine
SPIKE = HERE.parent                              # .../spike
FD_ROOT = SPIKE.parent                           # repo root: dedicated routine repo, or .../financial-dashboard in the monorepo
DESIGN_MOCKUP = FD_ROOT / "design" / "mockup"

sys.path.insert(0, str(SPIKE))
sys.path.insert(0, str(HERE))
import doppler_env
import state_sync  # noqa: E402  (spike/routine/doppler_env.py)
import refresh_gate  # noqa: E402  (spike/routine/refresh_gate.py, PRD-month-refresh.md Section 5 M5)
from publish_sheet import get_all_pass, now_mt_iso, build_tables  # noqa: E402
import alert  # noqa: E402  (spike/routine/alert.py)

# Every host the nightly pipeline touches, end to end. --diagnose checks exactly this
# list so the routine can fail fast with a clear message when the sandbox's network
# access is not set to Full (the cloud routine's default "Trusted" mode blocks the
# Doppler, NetSuite, and Amazon entries below; *.googleapis.com and github.com are
# reachable in both modes).
DIAGNOSE_HTTPS_HOSTS = [
    "sheets.googleapis.com",
    "bigquery.googleapis.com",
    "oauth2.googleapis.com",
    "www.googleapis.com",
    "gmail.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "serviceusage.googleapis.com",
    "api.doppler.com",
    "cli.doppler.com",
    "4201313.suitetalk.api.netsuite.com",
    "sellingpartnerapi-na.amazon.com",
    "sellingpartnerapi-eu.amazon.com",
    "api.amazon.com",
]
DIAGNOSE_TCP_PORTS = []  # no SSH: the code arrives as a Drive bundle, not a git clone


def _check_https(host, timeout=8):
    try:
        req = urllib.request.Request(f"https://{host}/", method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return True, f"HTTP {e.code}"  # any HTTP response at all means the host is reachable
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:150]


def _check_tcp(host, port, timeout=8):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "connected"
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:150]


def diagnose():
    """Pure network reachability check, no credentials needed. Prints one machine-
    readable verdict line (ENV_OK / ENV_BLOCKED: host1, host2, ...) followed by the
    per-host detail as JSON. Always exits 0 -- callers (PROMPT.md, a human) act on the
    verdict text, not the process exit code."""
    results = {}
    for host in DIAGNOSE_HTTPS_HOSTS:
        ok, detail = _check_https(host)
        results[host] = {"ok": ok, "detail": detail}
    for host, port in DIAGNOSE_TCP_PORTS:
        ok, detail = _check_tcp(host, port)
        results[f"{host}:{port}"] = {"ok": ok, "detail": detail}
    blocked = [k for k, v in results.items() if not v["ok"]]
    verdict = "ENV_OK" if not blocked else f"ENV_BLOCKED: {', '.join(blocked)}"
    print(verdict)
    print(json.dumps(results, indent=2))
    return 0


def run_step(cmd, label, cwd=None):
    printable = " ".join(str(c) for c in cmd)
    print(f"[run_nightly] [{label}] running: {printable}")
    proc = subprocess.run([str(c) for c in cmd], cwd=str(cwd or FD_ROOT), capture_output=True, text=True)
    if proc.stdout:
        print(proc.stdout)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    print(f"[run_nightly] [{label}] exit code {proc.returncode}")
    return proc


def strip_artifact_shell(html_text):
    """Fallback only -- used when design/mockup/build.py doesn't (yet) support
    --artifact. Reproduces exactly what that flag is documented to do (mockup/README.md
    "Publication status"): drop the DOCTYPE/html/head-open/charset-meta preamble, blank
    the viewport meta (the Artifact wrapper supplies its own), and drop the
    head-close/body-open and the trailing body/html close. Once build.py grows
    --artifact this function is dead code kept only as a safety net."""
    text = html_text
    text = re.sub(
        r"^\s*<!DOCTYPE html>\s*\n<html[^>]*>\s*\n<head>\s*\n<meta charset=\"UTF-8\">\s*\n",
        "", text, count=1, flags=re.IGNORECASE)
    text = re.sub(r'<meta name="viewport"[^>]*>', "", text, count=1)
    text = re.sub(r"</head>\s*\n<body[^>]*>\s*\n", "", text, count=1, flags=re.IGNORECASE)
    text = re.sub(r"\n?</body>\s*\n?</html>\s*\n?$", "\n", text, flags=re.IGNORECASE)
    return text


def fetch_prev_state(args):
    fetched_path = SPIKE / "data" / "state_prev_fetched.json"
    cmd = [sys.executable, str(SPIKE / "publish_bq.py"), "fetch-state",
           "--dataset", args.dataset, "--out", str(fetched_path)]
    if args.project:
        cmd += ["--project", args.project]
    proc = run_step(cmd, "fetch-state")
    if proc.returncode == 0 and fetched_path.is_file():
        print(f"[run_nightly] prior state fetched from BigQuery run_state -> {fetched_path}")
        return str(fetched_path)
    fallback = SPIKE / "data" / "state_prev.json"
    if fallback.is_file():
        print(f"[run_nightly] BigQuery fetch-state unavailable (rc={proc.returncode}); "
              f"using local fallback {fallback}")
        return str(fallback)
    print("[run_nightly] no prior state available (BigQuery empty/unreachable, no local "
          "fallback present) -- E2(f)/(g) will pass with 'no prior state' this run")
    return None


def fetch_demand_plan_step(args):
    """Reads the CFO-owned "Demand Plan" tab via spike/demand_plan.py, READ-ONLY, right
    after fetch_prev_state() and before run_extract() (research/09 Section 3). Returns
    (demand_plan_path: str|None, fatal_reason: str|None). fatal_reason is set ONLY for a
    genuine Google API error on the read (demand_plan.py's DEMAND_PLAN_FETCH_ERROR, a
    nonzero exit) -- that is NIGHTLY_FAIL, exactly like an extract.py crash. An absent
    tab or a validation failure is NOT fatal: demand_plan.py itself falls back to the
    last-good local snapshot (spike/data/demand_plan_prev.json, refreshed here only when
    this run's read was fresh and valid) and labels the result stale, same pattern as
    fetch_prev_state()'s state_prev.json fallback -- the demand section shows the
    previous snapshot, never blank."""
    sheet_id = args.sheet or os.environ.get("SPIKEBALL_FINANCE_SHEET_ID")
    if not sheet_id:
        print("[run_nightly] no Sheet id available (SPIKEBALL_FINANCE_SHEET_ID unset); "
              "skipping the Demand Plan read this run")
        return None, None

    out_path = SPIKE / "data" / "demand_plan_fetched.json"
    prev_path = SPIKE / "data" / "demand_plan_prev.json"
    cmd = [sys.executable, str(SPIKE / "demand_plan.py"), "fetch", "--sheet", sheet_id, "--out", str(out_path)]
    if prev_path.is_file():
        cmd += ["--prev", str(prev_path)]
    proc = run_step(cmd, "demand_plan")
    if proc.returncode != 0:
        tail = (proc.stdout + proc.stderr)[-1000:]
        return None, f"demand_plan.py fetch failed (rc={proc.returncode}): {tail}"

    if not out_path.is_file():
        return None, "demand_plan.py exited 0 but wrote no output file"

    try:
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        if payload.get("valid") and not payload.get("stale"):
            shutil.copyfile(out_path, prev_path)
            print(f"[run_nightly] refreshed local Demand Plan snapshot: {prev_path}")
    except (OSError, json.JSONDecodeError) as e:
        print(f"[run_nightly] WARNING could not refresh Demand Plan snapshot: {e}")
    return str(out_path), None


def run_extract(args, prev_state_path, demand_plan_path=None):
    out_path = SPIKE / "data" / "latest.json"
    state_new_path = SPIKE / "data" / "state_new.json"
    cmd = [sys.executable, str(SPIKE / "extract.py"), "--out", str(out_path)]
    if prev_state_path:
        cmd += ["--prev-state", str(prev_state_path)]
    if demand_plan_path:
        cmd += ["--demand-plan", str(demand_plan_path)]
    cmd += ["--write-state", str(state_new_path), "--amazon-max-minutes", "40"]
    if args.skip_amazon:
        cmd += ["--skip-amazon"]
    proc = run_step(cmd, "extract")
    return proc, out_path, state_new_path


def fail(code, reason, args, verdict="NIGHTLY_FAIL"):
    print(f"{verdict} {reason}")
    if args.no_alert:
        print("[run_nightly] --no-alert set, skipping alert send")
        return code
    subject = ("Spikeball Finance nightly: FAILED" if verdict == "NIGHTLY_FAIL"
               else "Spikeball Finance nightly: PARTIAL failure")
    body = f"Nightly run reported {verdict} (exit code {code}) at {now_mt_iso()} MT.\n\n{reason}"
    ok, detail = alert.send_alert(subject, body, dry_run=args.dry_run)
    if ok:
        print(f"[run_nightly] alert sent: {detail}")
    else:
        print(f"[run_nightly] ALERT SEND FAILED (not fatal to this run's exit code): {detail}")
    return code


def run_pipeline(args, trigger="nightly", request_row=""):
    """The nightly pipeline itself (extract, checks, Sheet/BigQuery/artifact publish),
    unchanged from before PRD-month-refresh.md's gate (M5) except for two additions:
    `trigger`/`request_row` are passed straight through to `publish_sheet.py`'s
    `--trigger`/`--request-row` flags (which land in the `run_log` row -- Section 4/5),
    and every return is now `(exit_code, ok_for_republish, pulled_at_mt)` instead of a
    bare exit code, so `run()`'s gate wrapper below knows whether to call
    `refresh_gate.mark_honored()` after this returns. `ok_for_republish` is True for
    exactly NIGHTLY_OK and NIGHTLY_PARTIAL_OK (the two verdicts PROMPT.md/the routine prompt (ROUTINE-PROMPT.md)
    treat as republish-worthy); `pulled_at_mt` is this run's `meta.pulled_at_mt` once
    the extract has produced `data`, else None. Every existing print/summary line is
    unchanged from the pre-gate behavior.
    """
    # The cloud sandbox starts empty: create the data dirs and restore last night's state
    # (Amazon watermark + order files, prior-run state) from Spikeball's Drive before anything runs.
    (SPIKE / "data" / "amazon").mkdir(parents=True, exist_ok=True)
    try:
        state_sync.download()
    except Exception as e:  # noqa: BLE001
        print(f"[run_nightly] WARNING state restore from Drive failed: {e}; continuing without prior state")
    prev_state_path = fetch_prev_state(args)

    demand_plan_path, demand_plan_fatal = fetch_demand_plan_step(args)
    if demand_plan_fatal:
        return fail(4, f"Demand Plan read failed with a genuine API error (not a "
                        f"validation failure): {demand_plan_fatal}", args), False, None

    extract_proc, out_path, state_new_path = run_extract(args, prev_state_path, demand_plan_path)

    def extract_crash_note():
        note = ""
        if "unrecognized arguments" in (extract_proc.stderr or ""):
            note = ("\n\nNOTE: extract.py does not yet accept --prev-state/--write-state/"
                     "--amazon-max-minutes -- this is the known FD1a/FD1b build gap (PRD "
                     "Section 9), not a NetSuite/Amazon failure. Re-run once FD1a/FD1b land.")
        return note

    if not out_path.is_file():
        tail = (extract_proc.stderr or extract_proc.stdout or "")[-1500:]
        return fail(4, f"extract.py crashed (rc={extract_proc.returncode}), no output written: "
                        f"{tail}{extract_crash_note()}", args), False, None

    try:
        with open(out_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        tail = (extract_proc.stderr or extract_proc.stdout or "")[-1500:]
        return fail(4, f"extract.py crashed (rc={extract_proc.returncode}), {out_path} unreadable "
                        f"({e}): {tail}{extract_crash_note()}", args), False, None

    all_pass, detail = get_all_pass(data)

    # extract.py's own checks.py wiring exits nonzero specifically WHEN
    # meta.checks.all_pass is false, after already writing a complete, valid
    # latest.json -- that is a checks failure (exit 2), not a crash (exit 4). Only
    # treat a nonzero exit as a crash when it does NOT line up with a checks failure
    # (no output, unreadable output, or -- unexpectedly -- checks say pass anyway,
    # which means something else went wrong after checks ran).
    if extract_proc.returncode != 0:
        if not all_pass:
            print(f"[run_nightly] extract.py exited rc={extract_proc.returncode}, consistent with "
                  f"a checks failure (not a crash) -- {detail}")
        else:
            tail = (extract_proc.stderr or extract_proc.stdout or "")[-1500:]
            return fail(4, f"extract.py exited rc={extract_proc.returncode} despite "
                            f"meta.checks.all_pass=true -- unexplained nonzero exit, treating as a "
                            f"crash: {tail}{extract_crash_note()}", args), False, None

    if state_new_path.is_file():
        try:
            shutil.copyfile(state_new_path, SPIKE / "data" / "state_prev.json")
            print(f"[run_nightly] refreshed local state fallback: {SPIKE / 'data' / 'state_prev.json'}")
        except OSError as e:
            print(f"[run_nightly] WARNING could not refresh local state fallback: {e}")

    if not all_pass:
        return fail(2, f"checks failed: {detail}", args), False, None
    print(f"[run_nightly] checks passed: {detail}")
    try:
        state_sync.upload()
    except Exception as e:  # noqa: BLE001
        print(f"[run_nightly] WARNING state upload to Drive failed: {e}; next run will re-pull Amazon from the window start")

    # Every step below runs independently of the others' outcome, and every outcome is
    # collected rather than short-circuiting on the first failure. This matters
    # specifically because a Sheets API write burst can 429 publish_sheet without
    # publish_bq or the artifact build being at fault at all -- proven live: extract
    # passed, publish_sheet hit the Sheets write quota, and the old short-circuit
    # design meant publish_bq and the artifact never even got a chance to run.
    results = {}  # name -> (ok: bool, detail: str|None)

    sheet_id = args.sheet or os.environ.get("SPIKEBALL_FINANCE_SHEET_ID")
    if not sheet_id:
        results["publish_sheet"] = (False, "SPIKEBALL_FINANCE_SHEET_ID not set in Doppler "
                                             "prd_spikeball and no --sheet given; run "
                                             "publish_sheet.py --create once manually first")
    else:
        sheet_cmd = [sys.executable, str(SPIKE / "publish_sheet.py"), "--data", str(out_path), "--sheet", sheet_id,
                     "--trigger", trigger, "--request-row", request_row]
        if args.dry_run:
            sheet_cmd.append("--dry-run")
        if args.force:
            sheet_cmd.append("--force")
        proc = run_step(sheet_cmd, "publish_sheet")
        if proc.returncode == 0:
            results["publish_sheet"] = (True, None)
        else:
            results["publish_sheet"] = (False, f"publish_sheet.py failed (rc={proc.returncode}): "
                                                 f"{(proc.stdout + proc.stderr)[-1000:]}")

    bq_cmd = [sys.executable, str(SPIKE / "publish_bq.py"), "load", "--data", str(out_path),
              "--dataset", args.dataset]
    if args.project:
        bq_cmd += ["--project", args.project]
    if state_new_path.is_file():
        bq_cmd += ["--state", str(state_new_path)]
    if args.dry_run:
        bq_cmd.append("--dry-run")
    if args.force:
        bq_cmd.append("--force")
    proc = run_step(bq_cmd, "publish_bq")
    if proc.returncode == 0:
        results["publish_bq"] = (True, None)
    else:
        results["publish_bq"] = (False, f"publish_bq.py failed (rc={proc.returncode}): "
                                         f"{(proc.stdout + proc.stderr)[-1000:]}")

    # Artifact build ALWAYS runs when checks passed, regardless of the two above --
    # it only depends on out_path (already written), not on either store succeeding.
    html_path = DESIGN_MOCKUP / "dashboard.html"
    artifact_path = DESIGN_MOCKUP / "dashboard.artifact.html"
    build_cmd = [sys.executable, str(DESIGN_MOCKUP / "build.py"), "--data", str(out_path),
                 "--out", str(html_path), "--artifact", str(artifact_path)]
    proc = run_step(build_cmd, "mockup_build")
    if proc.returncode != 0 and "--artifact" in (proc.stderr or "") and "unrecognized" in (proc.stderr or "").lower():
        print("[run_nightly] build.py does not support --artifact yet; falling back to a manual shell strip")
        build_cmd2 = [sys.executable, str(DESIGN_MOCKUP / "build.py"), "--data", str(out_path), "--out", str(html_path)]
        proc2 = run_step(build_cmd2, "mockup_build_fallback")
        if proc2.returncode != 0:
            results["artifact_build"] = (False, f"design/mockup/build.py failed (rc={proc2.returncode}): "
                                                  f"{(proc2.stdout + proc2.stderr)[-1000:]}")
        else:
            try:
                artifact_text = strip_artifact_shell(html_path.read_text(encoding="utf-8"))
                artifact_path.write_text(artifact_text, encoding="utf-8", newline="\n")
                print(f"[run_nightly] wrote {artifact_path} via fallback shell strip ({len(artifact_text)} bytes)")
                results["artifact_build"] = (True, None)
            except OSError as e:
                results["artifact_build"] = (False, f"fallback artifact shell-strip failed: {e}")
    elif proc.returncode != 0:
        results["artifact_build"] = (False, f"design/mockup/build.py failed (rc={proc.returncode}): "
                                             f"{(proc.stdout + proc.stderr)[-1000:]}")
    else:
        results["artifact_build"] = (True, None)

    try:
        tab_count = len(build_tables(data))
    except Exception:  # noqa: BLE001
        tab_count = "?"

    meta = data.get("meta") or {}
    failed = {k: v[1] for k, v in results.items() if not v[0]}
    sheet_ok, bq_ok, artifact_ok = (results.get(k, (False, None))[0]
                                     for k in ("publish_sheet", "publish_bq", "artifact_build"))

    if not failed:
        summary = (
            f"Spikeball Finance nightly OK for asof_date={meta.get('asof_date')} "
            f"(pulled_at_mt={meta.get('pulled_at_mt')}). Checks: {detail}. "
            f"Published {tab_count} tabs/tables to Sheet {sheet_id} and BigQuery dataset "
            f"{args.project or '(auto-resolved project)'}.{args.dataset}. "
            f"Artifact page ready at {artifact_path} -- republish its contents now to the "
            f"existing artifact URL with the Artifact tool (see PROMPT.md)."
        )
        print("NIGHTLY_OK")
        print(summary)
        return 0, True, meta.get("pulled_at_mt")

    if artifact_ok and (sheet_ok or bq_ok):
        succeeded = [k for k, v in results.items() if v[0]]
        reason = (
            f"asof_date={meta.get('asof_date')} pulled_at_mt={meta.get('pulled_at_mt')}. "
            f"Checks: {detail}. Succeeded: {succeeded}. Failed: {failed}. "
            f"Artifact page IS ready at {artifact_path} -- republish its contents now to the "
            f"existing artifact URL with the Artifact tool (see PROMPT.md); the failed "
            f"store(s) hold stale data until the next successful run."
        )
        return fail(3, reason, args, verdict="NIGHTLY_PARTIAL_OK"), True, meta.get("pulled_at_mt")

    reason = f"asof_date={meta.get('asof_date')}. Checks: {detail}. Failed: {failed}."
    return fail(3, reason, args), False, None


def _now_utc():
    """Thin wrapper around datetime.now(timezone.utc) so tests can monkeypatch a fixed
    clock (T10) without needing to patch the datetime module itself."""
    return datetime.now(timezone.utc)


def run(args):
    """PRD-month-refresh.md Section 5 M5: entry point `main()` calls. Without
    `--gate`, behaves exactly as `run_pipeline()` did before this gate existed
    (trigger=nightly, request_row=""). With `--gate`, computes the gate decision
    BEFORE `state_sync.download()`, `fetch_prev_state()`, `fetch_demand_plan_step()`,
    or any write -- on skip, prints `NIGHTLY_SKIP <reason>` and returns 0 having
    touched nothing on disk; on run, writes the attempt marker, acquires the lock, runs
    `run_pipeline()` unchanged inside try/finally (lock released in finally), and after
    a republish-worthy result (NIGHTLY_OK or NIGHTLY_PARTIAL_OK) calls
    `refresh_gate.mark_honored()` for the rows the decision named. Either way (skip or
    run), any now-stale `queued` refresh_requests row (`refresh_gate.stale_rows()`) is
    marked 'superseded <now_mt_iso>' via `refresh_gate.mark_status()` -- a Sheets cell
    PUT, never a local write, and never allowed to change the verdict already
    computed."""
    if not args.gate:
        code, _ok, _pulled_at_mt = run_pipeline(args, trigger="nightly", request_row="")
        return code

    sheet_id = args.sheet or os.environ.get("SPIKEBALL_FINANCE_SHEET_ID")
    now_utc = _now_utc()
    lock_age_min = refresh_gate.lock_age_minutes()
    last_attempt_utc = refresh_gate.read_last_attempt_utc()

    requests_ = None
    last_success_utc = None
    if not sheet_id:
        # PRD Section 5 M5: "if unset treat as sheets_error -> run" -- with no sheet id
        # there is nothing to read requests or last-success from, so fail open exactly
        # like a Sheets transport error would.
        verdict, reason, honored_rows = "run", "sheets_error", []
    else:
        try:
            requests_ = refresh_gate.read_requests(sheet_id)
        except Exception as e:  # noqa: BLE001
            print(f"[run_nightly] refresh_gate.read_requests failed: {e}")
            requests_ = None
        last_success_utc = refresh_gate.read_run_log_last_success(sheet_id)
        verdict, reason, honored_rows = refresh_gate.decide(
            now_utc, requests_, last_success_utc, last_attempt_utc, lock_age_min)

    # Stale-queued-request cleanup runs on BOTH the skip and run paths, before the
    # skip-vs-run branch below, and never on its own can change `verdict`/`reason`
    # (already computed above): a Sheets write failure here is logged and swallowed,
    # not raised. Only possible when a sheet id was available and read_requests()
    # actually returned data (requests_ is not None) -- with no sheet id, or a Sheets
    # transport error, there is nothing to evaluate staleness against.
    if sheet_id and requests_ is not None:
        stale = refresh_gate.stale_rows(requests_, last_success_utc)
        if stale:
            try:
                refresh_gate.mark_status(sheet_id, stale, f"superseded {now_mt_iso()}")
                print(f"[run_nightly] marked {len(stale)} stale queued refresh_requests "
                      f"row(s) superseded: {stale}")
            except Exception as e:  # noqa: BLE001
                print(f"[run_nightly] WARNING mark_status(superseded) failed (rows stay "
                      f"'queued' for the next gate run to retry): {e}")

    if verdict == "skip":
        print(f"NIGHTLY_SKIP {reason}")
        return 0

    trigger = "request" if reason.startswith("request") else "nightly"
    request_row = ",".join(str(r) for r in honored_rows)

    refresh_gate.touch_attempt()
    refresh_gate.acquire_lock()
    try:
        code, ok_for_republish, pulled_at_mt = run_pipeline(args, trigger=trigger, request_row=request_row)
    finally:
        refresh_gate.release_lock()

    if ok_for_republish and honored_rows and sheet_id and pulled_at_mt:
        try:
            refresh_gate.mark_honored(sheet_id, honored_rows, pulled_at_mt)
        except Exception as e:  # noqa: BLE001
            print(f"[run_nightly] WARNING mark_honored failed (requests stay 'queued' for the "
                  f"next gate run to retry): {e}")

    return code


def parse_args():
    ap = argparse.ArgumentParser(description="Nightly orchestration for the Spikeball Finance dashboard.")
    ap.add_argument("--dataset", default="spikeball_finance", help="BigQuery dataset name.")
    ap.add_argument("--project", default=None, help="GCP project id override (default: auto-resolve).")
    ap.add_argument("--sheet", default=None, help="Sheet id override (default: Doppler SPIKEBALL_FINANCE_SHEET_ID).")
    ap.add_argument("--skip-amazon", action="store_true", help="Passthrough to extract.py.")
    ap.add_argument("--dry-run", action="store_true", help="Passthrough to the publishers/mockup build; no writes.")
    ap.add_argument("--force", action="store_true", help="Passthrough --force to the publishers.")
    ap.add_argument("--no-alert", action="store_true", help="Skip sending the failure alert (local testing).")
    ap.add_argument("--diagnose", action="store_true",
                     help="Check reachability of every host the pipeline needs and exit; no "
                          "credentials required, no pipeline steps run.")
    ap.add_argument("--gate", action="store_true",
                     help="Gate the run behind refresh_gate.decide() (PRD-month-refresh.md Section 5 "
                          "M5): reads run_log and refresh_requests first and either skips (prints "
                          "NIGHTLY_SKIP <reason>, exit 0, no filesystem writes) or runs the pipeline "
                          "exactly as without this flag, plus trigger/request_row in the run_log row "
                          "and mark_honored() for any request rows this run honors.")
    return ap.parse_args()


def main():
    args = parse_args()
    if args.diagnose:
        return diagnose()
    if not doppler_env.ensure_loaded():
        # No secrets means no alert either (alert.py needs the same Google OAuth
        # secrets) -- report and stop rather than calling fail(), which would itself
        # fail trying to send an alert with nothing to authenticate it.
        print("NIGHTLY_FAIL could not load Doppler secrets: not already present in the "
              "environment, the Doppler CLI is unavailable, and the Doppler REST API "
              "call failed (DOPPLER_TOKEN unset, or api.doppler.com unreachable -- run "
              "--diagnose first).")
        return 4
    try:
        return run(args)
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc()[-1500:]
        return fail(3, f"run_nightly.py crashed unexpectedly: {e}\n{tb}", args)


if __name__ == "__main__":
    sys.exit(main())
