"""Read-only access to the Amazon orders cache the nightly refresh keeps on Drive.

`load()` calls `state_sync.download()` to fetch the nightly's state zip (the same file
the dashboard refresh restores from), extracting it into a scratch directory under the
run's output folder rather than `spike/data/`, so this module never writes to
`spike/data/amazon/` and never touches the nightly's watermark. It NEVER calls
`state_sync.upload()`.

Returned map: order_id -> {purchase_mt_date, purchase_utc_date, marketplace, status,
order_total, currency, item_total, n_items}. Marketplace codes are US / CA / UK.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SPIKE = _HERE.parent
for _p in (_SPIKE, _SPIKE / "routine", _HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import rules  # noqa: E402

ORDER_FILES = ("orders_NA.jsonl", "orders_EU.jsonl")
ITEM_FILES = ("order_items_NA.jsonl", "order_items_EU.jsonl")


class CacheUnavailable(RuntimeError):
    pass


def download_state(scratch_dir: Path) -> Path:
    """Restores the nightly's Drive state zip into `scratch_dir` and returns the amazon
    folder inside it. Raises CacheUnavailable when the state file is missing or the
    download fails."""
    import state_sync  # lazy: needs Google credentials only when the cache is used
    scratch_dir = Path(scratch_dir)
    scratch_dir.mkdir(parents=True, exist_ok=True)
    original = state_sync.DATA
    state_sync.DATA = scratch_dir
    try:
        ok = state_sync.download()
    except Exception as e:  # noqa: BLE001 -- any download failure means no cache
        raise CacheUnavailable(f"state download failed: {type(e).__name__}: {str(e)[:200]}") from e
    finally:
        state_sync.DATA = original
    if not ok:
        raise CacheUnavailable("state file not available on Drive (SPIKEBALL_DASH_STATE_FILE_ID unset or not found)")
    return scratch_dir / "amazon"


def _amount(money) -> float:
    if not money:
        return 0.0
    try:
        return float(money.get("Amount") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def read_cache(amazon_dir: Path, marketplace_ids: dict, uk_vat_rate: float) -> tuple[dict, dict]:
    """Parses the cache files in `amazon_dir`. Returns (orders, info). info carries the
    cache cutoff (max PurchaseDate, UTC ISO), counts, and whether item files exist."""
    import amazon_orders  # sibling module in spike/; pure file readers used here
    amazon_dir = Path(amazon_dir)
    raw_orders: dict = {}
    for fn in ORDER_FILES:
        raw_orders.update(amazon_orders.load_jsonl_by_key(amazon_dir / fn, "AmazonOrderId"))
    items_present = any((amazon_dir / fn).is_file() for fn in ITEM_FILES)
    item_sum: dict[str, float] = {}
    item_n: dict[str, int] = {}
    for fn in ITEM_FILES:
        rows = amazon_orders.load_jsonl_by_key(amazon_dir / fn, "OrderItemId")
        for row in rows.values():
            oid = row.get("AmazonOrderId")
            if not oid:
                continue
            item_sum[oid] = item_sum.get(oid, 0.0) + _amount(row.get("ItemPrice"))
            item_n[oid] = item_n.get(oid, 0) + 1
    orders: dict = {}
    max_pd = ""
    for oid, o in raw_orders.items():
        mk = marketplace_ids.get(o.get("MarketplaceId"))
        pd = o.get("PurchaseDate") or ""
        if pd > max_pd:
            max_pd = pd
        total = o.get("OrderTotal") or {}
        it = item_sum.get(oid)
        if it is not None and mk == "UK":
            it = it / (1.0 + uk_vat_rate)
        orders[oid] = {
            "purchase_mt_date": rules.utc_iso_to_mt_date(pd),
            "purchase_utc_date": rules.utc_iso_to_utc_date(pd),
            "marketplace": mk,
            "status": o.get("OrderStatus") or "",
            "order_total": _amount(total),
            "currency": total.get("CurrencyCode") if isinstance(total, dict) else None,
            "item_total": it,
            "n_items": item_n.get(oid, 0),
        }
    info = {"cutoff_utc": max_pd or None, "orders": len(orders), "items_present": items_present,
            "orders_with_items": sum(1 for o in orders.values() if o["item_total"] is not None)}
    return orders, info


def load(asof: date, scratch_dir: Path, marketplace_ids: dict, uk_vat_rate: float) -> tuple[dict, dict]:
    """Downloads the Drive state into `scratch_dir` (a folder this run owns), parses it,
    and removes the folder again so no copy of the order data is left behind. Orders
    purchased after `asof` (America/Denver) are dropped so the run is reproducible as of
    that date."""
    import shutil
    scratch_dir = Path(scratch_dir)
    try:
        amazon_dir = download_state(scratch_dir)
        if not any((amazon_dir / fn).is_file() for fn in ORDER_FILES):
            raise CacheUnavailable("state zip held no order files")
        orders, info = read_cache(amazon_dir, marketplace_ids, uk_vat_rate)
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)
    kept = {k: v for k, v in orders.items() if v["purchase_mt_date"] is None or v["purchase_mt_date"] <= asof}
    info["dropped_after_asof"] = len(orders) - len(kept)
    return kept, info
