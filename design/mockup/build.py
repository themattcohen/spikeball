#!/usr/bin/env python3
"""Build the Spikeball Finance dashboard page from template.html + a data JSON.

Usage:
    python build.py --data ../../spike/data/latest.json --out dashboard.html
    python build.py --data ../../spike/data/latest.json --out dashboard.html --artifact dashboard.artifact.html

Reads template.html (which contains one placeholder line:
    <script type="application/json" id="dash-data">__DATA__</script>
), minifies the given JSON with json.dumps(separators=(',', ':')), escapes
"</" as "<\\/" so a literal "</script>" inside a string value cannot close the
tag early, and writes the substituted page to --out (the full, standalone
document -- doctype/html/head/body -- for local `file://` viewing).

If the data file predates the v2 contract (no meta.rollups / meta.features /
meta.period_rule), those three keys are copied in from --rollups (default
spike/config/rollups.json) so the page always has a roll-up config to render
against. This is a pure compatibility shim: once extract.py itself writes
meta.rollups (v2), the copied-in value is never used because the key is
already present in the data.

--artifact PATH additionally writes a shell-stripped variant for the Artifact
publisher, which supplies its own <!DOCTYPE>/<html>/<head>/<body> wrapper and
forbids a page from declaring its own: the <head> is unwrapped and any <meta>
tag inside it is dropped, while <title>, <link> and <style> are kept; the
<body> is unwrapped to its inner content. Order is preserved. No dependencies
beyond the Python standard library.
"""
import argparse
import json
import os
import re
import sys


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def inject_v1_compat(data, rollups_path):
    """Copy meta.rollups / meta.features / meta.period_rule from the config
    file when the data JSON predates the v2 contract and lacks them."""
    if not os.path.isfile(rollups_path):
        return data
    meta = data.setdefault("meta", {})
    needs_rollups = "rollups" not in meta
    needs_features = "features" not in meta
    needs_period_rule = "period_rule" not in meta
    if not (needs_rollups or needs_features or needs_period_rule):
        return data
    with open(rollups_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if needs_rollups:
        meta["rollups"] = cfg
    if needs_features:
        meta["features"] = cfg.get("features", {})
    if needs_period_rule and "period_rule" in cfg:
        meta["period_rule"] = cfg["period_rule"]
    return data


def apply_feature_flags(data, features_csv, refresh_url):
    """PRD-month-refresh.md Section 3 "Feature flags" / Section 5 M3 deliverable 1:
    sets meta.features.<name> = True unconditionally for each name in features_csv (a
    comma list, default source SPIKEBALL_DASH_FEATURES), and meta.refresh_request_url
    unconditionally to refresh_url or "" (default source SPIKEBALL_REFRESH_REQUEST_URL)
    so the key is always present after build. Called after load_json and after
    inject_v1_compat. With features_csv empty and refresh_url empty/None, meta.features
    is left as whatever inject_v1_compat produced (no names added) and
    meta.refresh_request_url is added as "" and the two schedule keys
    (meta.nightly_slot_utc, meta.check_hours_utc) are added -- the only differences
    from a pre-flag build, none visible in the rendered text (T5)."""
    meta = data.setdefault("meta", {})
    features = meta.setdefault("features", {})
    for name in (n.strip() for n in (features_csv or "").split(",")):
        if name:
            features[name] = True
    meta["refresh_request_url"] = refresh_url or ""
    # Schedule facts for the page's refresh wording (nextCheckLabel / cadence copy in
    # template.html) so the MT labels follow the routine's cron and nightly slot
    # (SPIKEBALL_NIGHTLY_SLOT_UTC, default 9) instead of being written into the page.
    raw_slot = os.environ.get("SPIKEBALL_NIGHTLY_SLOT_UTC", "").strip()
    try:
        slot_hour = int(raw_slot) if raw_slot else 9
    except ValueError:
        slot_hour = 9
    if not 0 <= slot_hour <= 23:
        slot_hour = 9
    meta["nightly_slot_utc"] = slot_hour
    meta["check_hours_utc"] = sorted({0, slot_hour, *range(13, 24)})
    return data


def build_full_html(template, data):
    minified = json.dumps(data, separators=(",", ":"))
    minified = minified.replace("</", "<\\/")

    marker = '<script type="application/json" id="dash-data">__DATA__</script>'
    if marker not in template:
        print("[build] placeholder marker not found in template.html", file=sys.stderr)
        sys.exit(1)

    return template.replace(
        marker,
        '<script type="application/json" id="dash-data">' + minified + "</script>",
        1,
    )


def strip_shell(html):
    """Produce the Artifact-ready variant: drop <!DOCTYPE>, <html>, <head>
    wrapper and <meta> tags, and the <body> wrapper; keep <title>, <link>,
    <style>, <script> and the body content, in document order."""
    head_match = re.search(r"<head>(.*?)</head>", html, re.DOTALL)
    body_match = re.search(r"<body>(.*?)</body>", html, re.DOTALL)
    if not head_match or not body_match:
        print("[build] could not locate <head>...</head> and <body>...</body> for --artifact stripping", file=sys.stderr)
        sys.exit(1)
    head_inner = head_match.group(1)
    head_inner = re.sub(r"<meta\b[^>]*>\s*", "", head_inner)
    body_inner = body_match.group(1)
    return head_inner.strip() + "\n" + body_inner.strip() + "\n"


def main():
    ap = argparse.ArgumentParser(description="Build dashboard.html (and optionally an Artifact-ready variant) from template.html + a data JSON.")
    ap.add_argument("--data", required=True, help="Path to the data JSON (test1.json, latest.json, or a v2 pull).")
    ap.add_argument("--out", default="dashboard.html", help="Output HTML path, full standalone document (default dashboard.html).")
    ap.add_argument("--artifact", default=None, help="Also write a shell-stripped variant at this path, ready for the Artifact publisher.")
    ap.add_argument("--template", default=None, help="Path to template.html (default: alongside this script).")
    ap.add_argument("--rollups", default=None, help="Path to config/rollups.json, used to backfill meta.rollups/meta.features/meta.period_rule when the data file lacks them (default: ../../spike/config/rollups.json relative to this script).")
    ap.add_argument("--features", default=None, help="Comma-separated feature names to set true in meta.features (e.g. range_selector,refresh_control). Default: env SPIKEBALL_DASH_FEATURES, or unset/empty for none.")
    ap.add_argument("--refresh-url", default=None, help="URL written to meta.refresh_request_url (always present in the output, empty string when unset). Default: env SPIKEBALL_REFRESH_REQUEST_URL.")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    template_path = args.template or os.path.join(here, "template.html")
    rollups_path = args.rollups or os.path.join(here, "..", "..", "spike", "config", "rollups.json")

    if not os.path.isfile(template_path):
        print(f"[build] template not found: {template_path}", file=sys.stderr)
        sys.exit(1)
    if not os.path.isfile(args.data):
        print(f"[build] data file not found: {args.data}", file=sys.stderr)
        sys.exit(1)

    data = load_json(args.data)
    data = inject_v1_compat(data, rollups_path)
    features_csv = args.features if args.features is not None else os.environ.get("SPIKEBALL_DASH_FEATURES", "")
    refresh_url = args.refresh_url if args.refresh_url is not None else os.environ.get("SPIKEBALL_REFRESH_REQUEST_URL", "")
    data = apply_feature_flags(data, features_csv, refresh_url)

    with open(template_path, "r", encoding="utf-8") as f:
        template = f.read()

    full_html = build_full_html(template, data)

    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(full_html)
    size_kb = os.path.getsize(args.out) / 1024
    print(f"[build] wrote {args.out} ({size_kb:.1f} KB), full document, from {args.data}")

    if args.artifact:
        artifact_html = strip_shell(full_html)
        with open(args.artifact, "w", encoding="utf-8", newline="\n") as f:
            f.write(artifact_html)
        artifact_kb = os.path.getsize(args.artifact) / 1024
        print(f"[build] wrote {args.artifact} ({artifact_kb:.1f} KB), shell-stripped for Artifact publish")


if __name__ == "__main__":
    main()
