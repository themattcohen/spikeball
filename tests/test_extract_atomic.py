"""Atomic latest.json write: an interrupted or crashed write must never leave a
truncated file that the next run's json.load() cannot parse (the 2026-09-14
NIGHTLY_FAIL root cause). Exercises extract._atomic_write_json directly."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "spike"))

import extract  # noqa: E402


def test_atomic_write_roundtrips_and_leaves_no_tmp(tmp_path):
    target = tmp_path / "latest.json"
    obj = {"meta": {"asof_date": "2026-09-13"}, "rows": list(range(1000))}
    extract._atomic_write_json(target, obj)
    assert json.loads(target.read_text(encoding="utf-8")) == obj
    # no leftover temp file next to the target
    assert not (tmp_path / "latest.json.tmp").exists()
    assert [p.name for p in tmp_path.iterdir()] == ["latest.json"]


def test_atomic_write_overwrite_keeps_old_file_until_replace(tmp_path):
    target = tmp_path / "latest.json"
    extract._atomic_write_json(target, {"v": 1})
    extract._atomic_write_json(target, {"v": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"v": 2}
    assert not (tmp_path / "latest.json.tmp").exists()


def test_atomic_write_creates_parent_dir(tmp_path):
    target = tmp_path / "nested" / "dir" / "latest.json"
    extract._atomic_write_json(target, {"ok": True})
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}


def test_interrupted_write_does_not_corrupt_existing_target(tmp_path, monkeypatch):
    """If the dump is interrupted, os.replace never runs, so the pre-existing
    target keeps its previous complete content -- never a half-written file."""
    target = tmp_path / "latest.json"
    extract._atomic_write_json(target, {"good": 1})

    real_dump = json.dump

    def boom(obj, fp, **kw):
        real_dump(obj, fp, **kw)
        raise KeyboardInterrupt("simulated [Request interrupted by user]")

    monkeypatch.setattr(extract.json, "dump", boom)
    try:
        extract._atomic_write_json(target, {"good": 2, "big": list(range(5000))})
    except KeyboardInterrupt:
        pass
    # the target still parses and is the OLD complete content, not corrupt
    assert json.loads(target.read_text(encoding="utf-8")) == {"good": 1}
