# Spikeball Finance dashboard

Runtime code for the Spikeball Finance CFO/CEO dashboard: a read-only nightly pipeline
that pulls sales, margin, and inventory data and publishes it to a Google Sheet, a
BigQuery dataset, and a shared dashboard page.

## What this repository is

This repository holds only the code the scheduled routine needs to run each pull. It
is attached to the routine as its git source, so the routine checks it out directly
instead of downloading a code bundle.

- `spike/` -- the extract, checks, and publisher scripts, plus the nightly
  orchestration and refresh gate under `spike/routine/`.
- `design/mockup/` -- the dashboard page builder and its HTML template.
- `scripts/refresh_request_webapp/` -- the Apps Script pair behind the dashboard's
  "Request data refresh" control.
- `tests/` -- the automated test suite covering the refresh gate, the dashboard's
  month-range control, and the code export that populates this repository.
- `requirements.txt` -- the Python packages the routine installs before it runs.
- `.claude/settings.json` -- the allow-list of commands the routine's session may run
  without a prompt (pip install, the nightly script, alert.py, pytest, and a handful of
  read-only shell utilities the prompt uses for polling and diagnostics). No command
  outside this list runs unattended.

## How the routine uses it

The routine's cloud environment installs dependencies from `requirements.txt` in its
Setup script, then on each scheduled run checks out this repository fresh and runs
`spike/routine/run_nightly.py --gate`. That script reads the last run's status and any
queued refresh requests from the Google Sheet, decides whether this slot needs a run at
all, and -- when it does -- pulls fresh data, publishes it, and rebuilds the dashboard
page. Every credential the routine needs is a plain environment variable on its cloud
environment; nothing here reads a secrets manager.

This repository is updated from the maintainer's working copy by an export step that
copies only these runtime files across, after checking every one of them for text that
should never leave the maintainer's side.

## Running the tests

```
pip install -r requirements.txt
pip install pytest
python -m pytest tests -q
```

`tests/test_dashboard_range.py` additionally requires the `patchright` package and a
headless Chromium browser; without them that file fails to collect. Run the other test
files individually (for example `python -m pytest tests/test_refresh_gate.py -q`) on a
machine without a browser available.
