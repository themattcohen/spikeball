#!/usr/bin/env python3
"""Monthly gross-margin reconciliation (close checklist item): reads NetSuite (SELECT
only), the Amazon orders cache on Drive and the settlement records, builds one workbook
for the closing month, uploads it to Drive, emails the controller a short summary and
logs one row on the Sheet's `gm_recon_log` tab. Never writes to NetSuite.

Run from the repository root:
    python3 spike/gm_recon/run_recon.py [--month YYYY-MM] [--asof YYYY-MM-DD] [--out DIR]
        [--no-upload] [--no-email] [--no-log] [--dry-run] [--skip-amazon-cache] [--diagnose]

The last line of stdout is the machine contract, exactly one of:
    RECON_OK <drive link or local path>        exit 0
    RECON_PARTIAL_OK <reason>                  exit 3  (workbook built, delivery partly failed)
    RECON_FAIL <reason>                        exit 4 before a workbook exists, 3 after
`--diagnose` prints ENV_OK or ENV_BLOCKED: <hosts> and exits 0.

Credentials come from environment variables (doppler_env.ensure_loaded() first, the
same way the nightly does). No secrets are printed. No input().
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import traceback
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent          # .../spike/gm_recon
SPIKE = HERE.parent                              # .../spike
ROUTINE = SPIKE / "routine"
FD_ROOT = SPIKE.parent                           # repository root

sys.path.insert(0, str(SPIKE))
sys.path.insert(0, str(ROUTINE))
sys.path.insert(0, str(HERE))
import doppler_env  # noqa: E402  (spike/routine/doppler_env.py)
import rules  # noqa: E402

CONFIG_PATH = SPIKE / "config" / "gm_recon.json"
DEFAULT_OUT = SPIKE / "data" / "gm_recon"
DIAGNOSE_HTTPS_HOSTS = [
    "oauth2.googleapis.com",
    "www.googleapis.com",
    "sheets.googleapis.com",
    "gmail.googleapis.com",
    "4201313.suitetalk.api.netsuite.com",
]


def _check_https(host, timeout=8):
    try:
        req = urllib.request.Request(f"https://{host}/", method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return True, f"HTTP {e.code}"  # any HTTP response means the host is reachable
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:150]


def diagnose() -> int:
    hosts = list(DIAGNOSE_HTTPS_HOSTS)
    acct = os.environ.get("NETSUITE_ACCOUNT_ID")
    if acct:
        hosts[-1] = f"{acct.lower()}.suitetalk.api.netsuite.com"
    results = {h: dict(zip(("ok", "detail"), _check_https(h))) for h in hosts}
    blocked = [h for h, v in results.items() if not v["ok"]]
    print(json.dumps(results, indent=2))
    print("ENV_OK" if not blocked else f"ENV_BLOCKED: {', '.join(blocked)}")
    return 0


def code_rev() -> str:
    try:
        p = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(FD_ROOT), capture_output=True,
                           text=True, timeout=15)
        return p.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Monthly gross-margin reconciliation (read-only against NetSuite).")
    ap.add_argument("--month", help="Closing month YYYY-MM (default: previous calendar month, Mountain Time).")
    ap.add_argument("--asof", help="As-of date YYYY-MM-DD (default: today, Mountain Time).")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="Output folder (default spike/data/gm_recon/).")
    ap.add_argument("--no-upload", action="store_true", help="Skip the Drive upload and folder sharing.")
    ap.add_argument("--no-email", action="store_true", help="Skip the email.")
    ap.add_argument("--no-log", action="store_true", help="Skip the gm_recon_log Sheet row.")
    ap.add_argument("--dry-run", action="store_true", help="Same as --no-upload --no-email --no-log.")
    ap.add_argument("--skip-amazon-cache", action="store_true",
                    help="Do not download the orders cache; sales month falls back to the settlement row date.")
    ap.add_argument("--diagnose", action="store_true", help="Check host reachability and exit.")
    a = ap.parse_args(argv)
    if a.dry_run:
        a.no_upload = a.no_email = a.no_log = True
    return a


def _verdict(line: str, code: int) -> int:
    print(line.replace("\n", " ")[:900])
    return code


def run(args) -> int:
    run_at = rules.now_mt()
    try:
        month = args.month or rules.default_month(run_at)
        rules.parse_month(month)
        asof = date.fromisoformat(args.asof) if args.asof else rules.default_asof(run_at)
    except ValueError as e:
        return _verdict(f"RECON_FAIL bad arguments: {e}", 4)
    S, _E = rules.month_bounds(month)
    if asof < S:
        return _verdict(f"RECON_FAIL as-of {asof} is before the month starts", 4)
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = asof.strftime("%Y%m%d")
    xlsx_path = out_dir / f"Spikeball_GM_Recon_{month}_asof_{stamp}.xlsx"
    json_path = out_dir / f"summary_{month}_asof_{stamp}.json"
    print(f"[gm_recon] month {month}, as of {asof}, run at {run_at.isoformat(timespec='seconds')} MT")

    # Every recon module is imported here, together, before the three-minute NetSuite
    # read. A lazy import later in the run could pick up a file edited mid-run and mix
    # two code versions in one workbook (seen 2026-10-07: Python figures from one
    # version, workbook formulas from the next). openpyxl itself stays lazy inside
    # workbook.write_workbook().
    import amazon_cache  # noqa: E402
    import compute  # noqa: E402
    import deliver  # noqa: E402
    import workbook  # noqa: E402
    try:
        from _lib import load_env
        env = load_env()
    except SystemExit as e:
        return _verdict(f"RECON_FAIL env not loaded: {e}", 4)
    try:
        raw = compute.fetch_raw(env, month, asof, cfg)
    except Exception as e:  # noqa: BLE001
        return _verdict(f"RECON_FAIL NetSuite read failed: {type(e).__name__}: {str(e)[:300]}", 4)

    cache_orders, cache_info, cache_note = None, None, None
    if args.skip_amazon_cache:
        cache_note = "Orders cache skipped (--skip-amazon-cache): no estimate for fees not yet settled; sales month falls back to the settlement row date where no invoice exists."
    else:
        try:
            cache_orders, cache_info = amazon_cache.load(asof, out_dir / "_amazon_state", cfg["marketplace_ids"],
                                                         cfg["completeness"]["uk_vat_rate"])
            print(f"[gm_recon] orders cache: {cache_info['orders']} orders through {cache_info['cutoff_utc']}")
        except Exception as e:  # noqa: BLE001
            cache_note = f"Orders cache unavailable ({str(e)[:160]}); no estimate for fees not yet settled."
            print(f"[gm_recon] {cache_note}")

    try:
        model, details = compute.build(month, asof, raw, cfg, cache_orders, cache_info,
                                       {"run_at_mt": run_at.isoformat(timespec="seconds"), "code_rev": code_rev()})
        if cache_note:
            model["limitations"].insert(0, cache_note)
        workbook.write_workbook(model, details, xlsx_path, cfg)
        json_path.write_text(json.dumps(model, indent=2, default=str), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc()[-1200:]
        print(tb, file=sys.stderr)
        return _verdict(f"RECON_FAIL build failed: {type(e).__name__}: {str(e)[:300]}", 3)
    print(f"[gm_recon] workbook {xlsx_path}")
    print(f"[gm_recon] summary {json_path}")
    for line in model["answer"]:
        print(f"[gm_recon] {line}")


    delivery = model["delivery"]
    failures = []
    link = ""
    if not args.no_upload:
        try:
            folder = deliver.ensure_folder(cfg)
            fid, link = deliver.upload_xlsx(xlsx_path, folder)
            delivery.update(drive_file_id=fid, drive_link=link)
            print(f"[gm_recon] uploaded to Drive: {link}")
            try:
                added = deliver.ensure_readers(folder, deliver.recipients(cfg))
                if added:
                    print(f"[gm_recon] folder shared with {len(added)} recipient(s)")
            except Exception as e:  # noqa: BLE001
                failures.append(f"sharing: {str(e)[:150]}")
        except Exception as e:  # noqa: BLE001
            delivery["drive_link"] = ""
            failures.append(f"upload: {str(e)[:200]}")
    if not args.no_email:
        ok, detail = deliver.send_email(deliver.email_subject(model),
                                        deliver.email_body(model, link, compute.email_tables(model)),
                                        deliver.recipients(cfg))
        delivery["email"] = "sent" if ok else f"failed: {detail[:200]}"
        if not ok:
            failures.append(f"email: {detail[:150]}")
    verdict_word = "RECON_PARTIAL_OK" if failures else "RECON_OK"
    if not args.no_log:
        sheet_id = os.environ.get(cfg["env"]["sheet_id"])
        if not sheet_id:
            delivery["log_row"] = "failed: SPIKEBALL_FINANCE_SHEET_ID not set"
            failures.append("log: sheet id not set")
        else:
            m = model["margin"]
            row = {"run_at_mt": model["run_at_mt"], "month": month, "asof": asof.isoformat(),
                   "as_booked_gm": m["as_booked"], "corrected_gm": m["corrected"], "matched_gm": m["matched"],
                   "n_errors": len(model["errors"]), "n_entries": len(model["timing"]),
                   "drive_link": link, "verdict": verdict_word, "code_rev": model["code_rev"]}
            try:
                delivery["log_row"] = deliver.append_log_row(sheet_id, cfg["log_tab"], cfg["log_header"], row)
            except Exception as e:  # noqa: BLE001
                delivery["log_row"] = f"failed: {str(e)[:200]}"
                failures.append(f"log: {str(e)[:150]}")
    json_path.write_text(json.dumps(model, indent=2, default=str), encoding="utf-8")
    if failures:
        return _verdict(f"RECON_PARTIAL_OK {'; '.join(failures)}", 3)
    return _verdict(f"RECON_OK {link or xlsx_path.as_posix()}", 0)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.diagnose:
        return diagnose()
    if not doppler_env.ensure_loaded():
        return _verdict("RECON_FAIL env not loaded", 4)
    try:
        return run(args)
    except Exception as e:  # noqa: BLE001
        print(traceback.format_exc()[-1500:], file=sys.stderr)
        return _verdict(f"RECON_FAIL unexpected: {type(e).__name__}: {str(e)[:300]}", 3)


if __name__ == "__main__":
    sys.exit(main())
