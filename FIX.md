# Fix: nightly NIGHTLY_FAIL exit 4 on 2026-09-14

## What happened, and what did not

On 2026-09-14 a run reported `NIGHTLY_FAIL` exit 4:
`extract.py crashed (rc=1), latest.json unreadable (Expecting ':' delimiter: line 16135 ...)`.

That run fired at 03:20 MT, which is not a scheduled hour, so it was a manual run. It hit a
transient Amazon error and was then interrupted (`[Request interrupted by user]`).

The scheduled nightly runs that same morning both succeeded: 03:04 MT and 04:06 MT, each
`all_pass=TRUE`, each with `amazon_orders: ok`. The dashboard stayed fresh (pulled 04:06 MT,
asof 2026-09-13) and nothing that was published broke. So the routine was never actually
down; a single interrupted manual run produced a scary-looking message.

## Root cause (confirmed from source)

`spike/extract.py` wrote `latest.json` (and `state_new.json`) with a direct
`open(path, "w")` followed by `json.dump` streaming straight into the final file. That
write is not atomic: it truncates the real file first, then fills it in. An interrupt or a
crash partway through leaves a half-written file at the canonical path.

`spike/routine/run_nightly.py` then reads that file back to decide whether to publish. It
hit the truncated JSON, raised a parse error, and returned `fail(4, ... unreadable ...)`.
Exit 4 is the "extract output missing or unreadable" branch.

The Amazon error was separate and non-gating: `spike/checks.py` excludes `amazon_orders`
from the pass gate by name, so an Amazon hiccup never fails the nightly on its own.

## The fix (already in this repo)

`spike/extract.py` now writes JSON atomically through a small helper: it writes a complete
temp file in the same directory, flushes and fsyncs it, then `os.replace()`s it onto the
target. `os.replace` is atomic on one filesystem, so a reader always sees either the old
complete file or the new complete file, never a half-written one. This is the same pattern
`spike/amazon_orders.py` already used for its own state files.

Covered by `tests/test_extract_atomic.py` (four tests, including one that simulates an
interrupt mid-write and asserts the previous good file survives intact).

## How to verify after merging

1. `pip install -r requirements-dev.txt` then `python3 -m pytest tests -q` -- all pass
   (the gate tests pass at `SPIKEBALL_NIGHTLY_SLOT_UTC` = 9, 10, or unset).
2. Let the next scheduled nightly run. It should write a fresh `run_log` row with
   `all_pass=TRUE`, and the dashboard's "As of" time should advance. A run outside the
   nightly slot with nothing queued still ends in `NIGHTLY_SKIP` -- that is normal.

## Amazon SP-API: no code change needed

`amazon_orders` is non-gating by design, and the scheduled runs pull Amazon fine (the
incremental watermark advances every morning). The 03:20 error was a transient auth or
throttle response, most likely because three Amazon pulls landed within twenty minutes and
one got refused.

- For a manual test run, pass `--skip-amazon` so you do not collide with a scheduled
  pull's quota. A skipped section is recorded as `skipped`, not `error`, and does not
  affect `all_pass`.
- Rotate `SP_API_REFRESH_TOKEN_NA` / `SP_API_REFRESH_TOKEN_EU` only if the scheduled runs
  begin showing `amazon_orders: error` on consecutive days. As of 2026-09-14 they are not.

## Optional hardening (owner's call, not required)

The JSON writer uses the default `allow_nan=True`. No run produces a `NaN` today (the ratio
fields are guarded, and average order value is left blank when order count is zero), but if
one ever did, it would embed a bare `NaN` token that the dashboard's browser JSON parser
and a BigQuery load both reject. A later change could pass `allow_nan=False` and scrub any
non-finite float to null before writing, so such a value fails loudly at write time rather
than silently reaching the Sheet or the page. Not part of this fix.
