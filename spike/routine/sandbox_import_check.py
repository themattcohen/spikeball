"""Pre-bundle guard: import the entire nightly extract path with openpyxl BLOCKED, the way
the cloud sandbox sees it (no openpyxl, no workbook). Run this before publish_bundle.py.

The 2026-08-28 03:06 nightly failed NIGHTLY_FAIL because extract_v2_bs.py imported openpyxl
at module level and the sandbox has no openpyxl. The local test env has openpyxl, so a local
`python extract.py` did not catch it. This check makes the sandbox's missing-dep condition
reproducible locally.

Run:
  python spike/routine/sandbox_import_check.py
Exit 0 = the nightly path imports clean without openpyxl. Non-zero = a module-level import of
a local-only dependency would crash the routine; fix it (make the import lazy) before bundling.
"""
import builtins
import sys
from pathlib import Path

# Any dependency that is NOT in the cloud sandbox and must never be imported at module level
# on the nightly path. openpyxl is local-only (reads the workbook fixture). Add others here if
# the sandbox ever rejects one.
SANDBOX_MISSING = {"openpyxl"}

_orig_import = builtins.__import__


def _blocked(name, *args, **kwargs):
    root = name.split(".")[0]
    if root in SANDBOX_MISSING:
        raise ModuleNotFoundError(f"No module named '{root}' (blocked by sandbox_import_check)")
    return _orig_import(name, *args, **kwargs)


def main():
    builtins.__import__ = _blocked
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # spike/
    failed = None
    for mod in ("extract_v2", "extract_v2_bs", "extract_v2_bs_snapshot", "checks_v2", "extract", "demand_plan"):
        try:
            __import__(mod)
        except ModuleNotFoundError as e:  # a local-only dep imported at module level
            failed = (mod, str(e))
            break
        except Exception as e:  # noqa: BLE001 -- other import-time errors are also disqualifying
            failed = (mod, f"{type(e).__name__}: {e}")
            break
    builtins.__import__ = _orig_import
    if failed:
        print(f"SANDBOX_IMPORT_FAIL importing {failed[0]}: {failed[1]}")
        print("A local-only dependency is imported at module level on the nightly path. Make it "
              "lazy (import inside the function that uses it) before bundling.")
        sys.exit(1)
    print("SANDBOX_IMPORT_OK: the nightly path imports clean without", ", ".join(sorted(SANDBOX_MISSING)))
    sys.exit(0)


if __name__ == "__main__":
    main()
