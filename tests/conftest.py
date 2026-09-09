"""Shared pytest configuration for the Spikeball Finance dashboard test suite
(PRD-month-refresh.md Section 7 / RC M6). Puts the project root and every module
directory the test files import from directly (without package qualification) on
sys.path, and provides fixtures that skip a test when the local data pull
(spike/data/latest.json, gitignored, not committed per Section 2) is absent.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPIKE = ROOT / "spike"
ROUTINE = SPIKE / "routine"
MOCKUP = ROOT / "design" / "mockup"
LATEST_JSON = SPIKE / "data" / "latest.json"

for _p in (ROOT, SPIKE, ROUTINE, MOCKUP):
    _sp = str(_p)
    if _sp not in sys.path:
        sys.path.insert(0, _sp)


@pytest.fixture
def latest_data_path():
    """The path to spike/data/latest.json. Skips the test (with a message) when the
    file is absent -- PRD-month-refresh.md Section 7: "Tests read the local
    spike/data/latest.json (gitignored; skip with a message when absent)"."""
    if not LATEST_JSON.is_file():
        pytest.skip(f"{LATEST_JSON} not present -- run spike/extract.py locally first")
    return LATEST_JSON


@pytest.fixture
def latest_data(latest_data_path):
    """The parsed contents of spike/data/latest.json. Depends on latest_data_path so
    the skip happens the same way."""
    return json.loads(latest_data_path.read_text(encoding="utf-8"))
