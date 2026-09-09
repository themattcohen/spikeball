"""amazon_orders.py -- FD1b: Amazon SP-API Orders API incremental pull.

READ-ONLY, GET-only (`GET /orders/v0/orders`, `GET /orders/v0/orders/{id}/orderItems`,
`GET /sellers/v1/marketplaceParticipations`). No `createReport`/report-document path (that
path is dead -- 403/400, see `research/01e-spapi-report-403.md`). No write of any kind to
Amazon. Plain LWA bearer in `x-amz-access-token`, no AWS SigV4 (removed as a requirement for
unrestricted SP-API operations in 2023; `getOrders`/`getOrderItems` proven live with a plain
LWA token on 2026-08-26, see `research/01e-spapi-report-403.md` H2 and this module's own
build-time probe).

Order-level data (SKU, revenue, ship city/state/country -- reduced, no street/name, per
`BuyerInfo: {}` on every call proven live) persists under `spike/data/amazon/`, which is a
NEW directory this module introduces. That directory is NOT covered by the repo's existing
`.gitignore` (`spike/data/*.json` does not match nested paths, confirmed live with
`git check-ignore` during this build) -- so this module writes its own nested
`spike/data/amazon/.gitignore` (`*`, keeping only itself) the first time it runs, closing
that gap without touching the shared root `.gitignore`. See `spike/README.md` "Gitignore
gap" for the full note to the team lead.

Rate limits (Amazon's published restore rates for these two operations; both implemented as
a token bucket that also reads the live `x-amzn-RateLimit-Limit` response header and adopts
it when present, which is usually looser than the conservative default below --
`getOrders` measured 0.04512 req/s live on 2026-08-26, well above the 0.0167 (1/60s)
documented floor):
  - `GET /orders/v0/orders`: burst 20, then 1 request / 60 seconds (rate 0.0167 req/s).
  - `GET /orders/v0/orders/{id}/orderItems`: burst 30, then 0.5 req/s.
A 429 response backs off honoring `Retry-After` if present, else exponential backoff capped
at 60s. A 500/503 gets the same capped exponential backoff. Every other non-2xx raises.

Incremental design: `LastUpdatedAfter` per region, persisted in `spike/data/amazon/state.json`
(`{"NA": {"last_updated_after": ..., "orders_seen": int, "next_token": str|null}, "EU": {...}}`).
First run per region defaults `LastUpdatedAfter` to `mtd_start` at 00:00 MT (ISO with explicit
offset). Two resume mechanisms, team-lead fix 2026-08-26 after a live-run proof that the
original design (checkpoint only advances on a full drain) would never finish a first MTD
backfill for a real-volume marketplace:
  1. `next_token`: when a region's pagination is cut short by the time budget, the pending
     `NextToken` (the one about to be used for the next page) is persisted and reused FIRST
     on the next run -- resumes exactly where it left off rather than re-walking pages already
     seen (Amazon NextTokens stay valid for a while). Cleared to `null` once a pull fully
     drains.
  2. `last_updated_after`: computed every run as `max(LastUpdateDate)` across every order
     currently known for the region (this run's pulls plus every prior run's, from the
     on-disk jsonl, not just new rows) minus a 5-minute safety buffer -- applied
     UNCONDITIONALLY, whether or not the pagination fully drained this run, so a region cut
     short still advances instead of restarting the whole MTD window every night. Falls back
     to the prior watermark unchanged only when no order has a parseable `LastUpdateDate` yet.
Both together mean a cut-short run resumes primarily via the cheaper `next_token` path next
time (if it's still valid), with `last_updated_after` as the fallback/ground-truth watermark
if the token has expired. `OrderStatuses` is never sent as a request filter (`OrderStatuses`
"all except Canceled handled by filtering the response" per brief) -- every status is pulled
and persisted; `Canceled` is excluded only at aggregation time, so a status flip (e.g. Shipped
-> Canceled) is still visible in the raw file across runs. Every order row is persisted by
upsert-on-`AmazonOrderId` regardless of which resume path is used, so re-touching an
already-seen order (e.g. if a token turns out stale and a run falls back to
`last_updated_after`) is always safe, never a duplicate.

`getOrderItems` is called only for orders whose `MarketplaceId` is NOT one of the three that
already reach NetSuite (US/CA/UK, `IN_NETSUITE_MARKETPLACE_IDS` below) -- those three already
have SKU-level revenue in NetSuite's consolidated DAILY-FBA invoices (`00-findings.md` Q1).
Known simplification: an order with genuinely zero line items (not observed in this build's
live testing -- see the README's recorded raw shape) would be re-attempted every run rather
than cached as "done with zero items", since the backlog is computed as "orders needing items
minus orders that already have at least one items row on disk". Not expected to matter in
practice; documented here rather than adding a second tracking file for an edge case with no
live evidence it occurs.

Run via (never standalone with secrets outside doppler):
  doppler run --project $DOPPLER_PROJECT --config $DOPPLER_CONFIG -- python -c \
    "from spike.amazon_orders import run_amazon_orders; ..."
In practice this module is only ever called from `extract.py`, which owns `D` (the run's
date-window dict) and `meta.rollups` (parsed `config/rollups.json`).
"""
from __future__ import annotations

import datetime
import json
import os
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

MT = ZoneInfo("America/Denver")

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
ORDERS_API_BASE = "/orders/v0"

# Per team-lead brief: the three marketplaces whose sales already reach NetSuite as
# consolidated DAILY-FBA invoices with SKU-level Income lines (post 2026-08-20 cutover).
IN_NETSUITE_MARKETPLACE_IDS = {
    "ATVPDKIKX0DER": "US",
    "A2EUQ1WTGCTBG2": "CA",
    "A1F83G8C2ARO7P": "UK",
}

ORDERS_RATE_DEFAULT = 1.0 / 60.0  # 1 request per 60s, Amazon's documented floor
ORDERS_BURST = 20
ITEMS_RATE_DEFAULT = 0.5
ITEMS_BURST = 30

MAX_HTTP_ATTEMPTS = 6


class TokenBucket:
    """Token-bucket rate limiter. `rate` tokens/sec refill, `capacity` = burst size.
    `observe_header()` lets a caller feed Amazon's own `x-amzn-RateLimit-Limit` value back
    in; since that is usually a looser (faster) rate than our conservative documented
    default, later waits shrink automatically once we have seen it."""

    def __init__(self, rate: float, capacity: float):
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.last = time.monotonic()

    def observe_header(self, value) -> None:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        if v > 0:
            self.rate = v

    def wait_for_token(self, deadline: float | None = None) -> bool:
        """Blocks until one token is available, sleeping in <=5s increments so a deadline
        check stays responsive. Returns False (consuming nothing) if satisfying the wait
        would cross `deadline` (a `time.monotonic()` timestamp); True once a token is spent."""
        while True:
            now = time.monotonic()
            elapsed = now - self.last
            self.last = now
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            if self.tokens >= 1:
                self.tokens -= 1
                return True
            needed = (1 - self.tokens) / self.rate if self.rate > 0 else 3600.0
            if deadline is not None and time.monotonic() + needed > deadline:
                return False
            time.sleep(min(needed, 5.0))


# ---------------------------------------------------------------------------
# HTTP / auth
# ---------------------------------------------------------------------------

def get_access_token(refresh_token: str, client_id: str, client_secret: str) -> str:
    resp = requests.post(
        LWA_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"LWA token exchange failed: HTTP {resp.status_code}: {resp.text[:500]}")
    return resp.json()["access_token"]


def get_marketplace_participations(endpoint: str, access_token: str) -> list[dict]:
    url = f"{endpoint}/sellers/v1/marketplaceParticipations"
    headers = {"x-amz-access-token": access_token, "Content-Type": "application/json"}
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"marketplaceParticipations failed: HTTP {resp.status_code}: {resp.text[:500]}")
    out = []
    for p in resp.json().get("payload", []):
        mp = p.get("marketplace", {})
        participation = p.get("participation", {})
        name = mp.get("name") or ""
        if not participation.get("isParticipating"):
            continue
        if name.startswith("Non-Amazon"):
            continue
        mid = mp.get("id")
        out.append({
            "marketplace_id": mid,
            "country": mp.get("countryCode"),
            "name": name,
            "in_netsuite": mid in IN_NETSUITE_MARKETPLACE_IDS,
        })
    return out


def amazon_get(endpoint, path, access_token, bucket, params=None, deadline=None):
    """Paced GET: consumes a token from `bucket` first (returns None, no request made, if
    that would cross `deadline`). Retries 429/500/503 with backoff (honoring `Retry-After`
    on 429); feeds every response's rate-limit header back into `bucket`. Raises on any
    other non-2xx after exhausting attempts; returns the `requests.Response` on success."""
    if not bucket.wait_for_token(deadline):
        return None
    url = f"{endpoint}{path}"
    headers = {"x-amz-access-token": access_token, "Content-Type": "application/json"}
    resp = None
    last_exc = None
    for attempt in range(MAX_HTTP_ATTEMPTS):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
        except requests.exceptions.RequestException as e:
            last_exc = e
            if attempt == MAX_HTTP_ATTEMPTS - 1:
                raise
            time.sleep(2 ** attempt)
            continue
        bucket.observe_header(resp.headers.get("x-amzn-RateLimit-Limit"))
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else min(60.0, (2 ** attempt) * 2)
            if deadline is not None and time.monotonic() + wait > deadline:
                return None
            time.sleep(wait)
            continue
        if resp.status_code in (500, 503):
            wait = min(60.0, (2 ** attempt) * 2)
            if deadline is not None and time.monotonic() + wait > deadline:
                return None
            time.sleep(wait)
            continue
        return resp
    if resp is not None:
        return resp
    raise last_exc


# ---------------------------------------------------------------------------
# Local storage: spike/data/amazon/ (new dir; self-gitignored, see module docstring)
# ---------------------------------------------------------------------------

def data_dir() -> Path:
    d = Path(__file__).parent / "data" / "amazon"
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text("# real Amazon order/SKU data -- never enters git history\n*\n!.gitignore\n", encoding="utf-8")
    return d


def load_jsonl_by_key(path: Path, key_field: str) -> dict:
    out = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                k = row.get(key_field)
                if k is not None:
                    out[k] = row
    return out


def write_jsonl(path: Path, rows_by_key: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for k in sorted(rows_by_key.keys()):
            f.write(json.dumps(rows_by_key[k], default=str))
            f.write("\n")
    tmp.replace(path)


def load_state(d: Path) -> dict:
    p = d / "state.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(d: Path, state: dict) -> None:
    p = d / "state.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)


# ---------------------------------------------------------------------------
# Pull loops
# ---------------------------------------------------------------------------

def pull_region_orders(endpoint, access_token, marketplace_ids, last_updated_after, start_next_token, deadline, bucket, orders_by_id):
    """Paginates GET /orders/v0/orders, upserting into `orders_by_id` (mutated in place,
    keyed by AmazonOrderId). If `start_next_token` is given (a NextToken persisted from a
    prior run cut short mid-pagination), resumes from it directly instead of starting a new
    LastUpdatedAfter-based pull -- Amazon NextTokens stay valid for a while, so this avoids
    re-fetching pages already seen. Returns (completed: bool, pages: int, seen_this_run: int,
    pending_next_token: str|None) -- pending_next_token is the token to persist and resume
    from next time when the budget cut this pull short (None when it fully drained)."""
    base_params = {
        "MarketplaceIds": ",".join(marketplace_ids),
        "LastUpdatedAfter": normalize_amazon_timestamp(last_updated_after) or last_updated_after,
        "MaxResultsPerPage": 100,
    }
    pages = 0
    seen = 0
    next_token = start_next_token
    timestamp_retry_used = False
    while True:
        call_params = {"NextToken": next_token} if next_token else dict(base_params)
        resp = amazon_get(endpoint, f"{ORDERS_API_BASE}/orders", access_token, bucket, params=call_params, deadline=deadline)
        if resp is None:
            return False, pages, seen, next_token
        if (resp.status_code == 400 and not next_token and not timestamp_retry_used
                and "ISO8601" in resp.text):
            # Defense in depth: LastUpdatedAfter is already normalized above, so this should
            # not fire in practice, but if some other path ever hands in an unnormalized
            # value, reformat once and retry the same call rather than failing the region.
            timestamp_retry_used = True
            fixed = normalize_amazon_timestamp(base_params["LastUpdatedAfter"])
            if fixed and fixed != base_params["LastUpdatedAfter"]:
                base_params["LastUpdatedAfter"] = fixed
                continue
        if resp.status_code != 200:
            raise RuntimeError(f"getOrders failed: HTTP {resp.status_code}: {resp.text[:500]}")
        payload = resp.json().get("payload", {})
        orders = payload.get("Orders", [])
        pages += 1
        for o in orders:
            oid = o.get("AmazonOrderId")
            if not oid:
                continue
            orders_by_id[oid] = {
                "AmazonOrderId": oid,
                "PurchaseDate": o.get("PurchaseDate"),
                "LastUpdateDate": o.get("LastUpdateDate"),
                "OrderStatus": o.get("OrderStatus"),
                "MarketplaceId": o.get("MarketplaceId"),
                "OrderTotal": o.get("OrderTotal"),
                "NumberOfItemsShipped": o.get("NumberOfItemsShipped"),
                "NumberOfItemsUnshipped": o.get("NumberOfItemsUnshipped"),
                "FulfillmentChannel": o.get("FulfillmentChannel"),
                "IsBusinessOrder": o.get("IsBusinessOrder"),
            }
            seen += 1
        next_token = payload.get("NextToken")
        if not next_token:
            return True, pages, seen, None


def pull_order_items(endpoint, access_token, order_ids, mp_by_order, deadline, bucket, items_by_key):
    """Calls getOrderItems for each id in `order_ids`, upserting into `items_by_key` (mutated
    in place, keyed by OrderItemId, falling back to a composite key if Amazon omits it).
    Returns (completed: bool, n_pulled_this_run: int). A non-200 on one order is skipped
    (not fatal to the whole backlog) and logged into the returned notes list."""
    n = 0
    skipped = []
    for oid in order_ids:
        resp = amazon_get(endpoint, f"{ORDERS_API_BASE}/orders/{oid}/orderItems", access_token, bucket, deadline=deadline)
        if resp is None:
            return False, n, skipped
        if resp.status_code != 200:
            skipped.append({"order_id": oid, "http_status": resp.status_code})
            n += 1
            continue
        payload = resp.json().get("payload", {})
        mp_id = mp_by_order.get(oid)
        for item in payload.get("OrderItems", []):
            key = item.get("OrderItemId") or f"{oid}:{item.get('SellerSKU')}:{item.get('ASIN')}"
            items_by_key[key] = {
                "OrderItemId": item.get("OrderItemId"),
                "AmazonOrderId": oid,
                "MarketplaceId": mp_id,
                "SellerSKU": item.get("SellerSKU"),
                "ASIN": item.get("ASIN"),
                "QuantityOrdered": item.get("QuantityOrdered"),
                "ItemPrice": item.get("ItemPrice"),
            }
        n += 1
    return True, n, skipped


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _parse_iso_utc(iso_str: str | None):
    """Parses an Amazon timestamp (PurchaseDate/LastUpdateDate, always UTC 'Z'-suffixed) into
    a UTC-aware datetime. Returns None on missing/unparseable input."""
    if not iso_str:
        return None
    s = iso_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _parse_purchase_date_mt(iso_utc: str | None):
    dt = _parse_iso_utc(iso_utc)
    return dt.astimezone(MT) if dt is not None else None


def normalize_amazon_timestamp(value) -> str | None:
    """Normalizes any timestamp -- a raw ISO string (any offset, with or without
    microseconds) or a `datetime` -- to the exact format Amazon's Orders API requires for
    `LastUpdatedAfter`/`CreatedAfter`: UTC, whole seconds, literal 'Z' suffix
    ("YYYY-MM-DDTHH:MM:SSZ"), matching the format Amazon's own responses use. Team-lead fix,
    2026-08-26: a value with microseconds and a non-Z offset (produced by an EARLIER version
    of this module's checkpoint logic, e.g. "2026-08-26T21:20:42.630453-06:00") reached
    `getOrders` unnormalized and was rejected with `HTTP 400 InvalidInput "timestamp must
    follow ISO8601"`. Every timestamp this module sends now passes through here first --
    both when writing state (`compute_watermark`, `_default_last_updated_after`) and when
    reading a possibly-older/malformed state value back in (`run_amazon_orders`) -- so a
    pre-existing bad value self-heals on the next run rather than failing forever. Returns
    `None` for an unparseable value (caller falls back to a safe default, never sends
    garbage to the API)."""
    if value is None:
        return None
    dt = value if isinstance(value, datetime.datetime) else _parse_iso_utc(str(value))
    if dt is None:
        return None
    return dt.astimezone(datetime.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


WATERMARK_SAFETY_BUFFER = datetime.timedelta(minutes=5)


def compute_watermark(orders_by_id: dict) -> str | None:
    """The next LastUpdatedAfter to persist: max LastUpdateDate seen across every order
    currently known for the region (this run's pulls plus every prior run's -- not just
    this run's new rows, since a resumed pull may have added nothing new this time), minus a
    5-minute safety buffer (clock-skew/ordering margin -- team-lead fix, 2026-08-26), always
    returned in Amazon's required "YYYY-MM-DDTHH:MM:SSZ" format (see
    normalize_amazon_timestamp). Applied unconditionally, whether or not the region's
    pagination fully drained this run, so a region cut short still advances instead of
    restarting the whole MTD window every night. Returns None when no order has a parseable
    LastUpdateDate (caller keeps the prior watermark unchanged in that case)."""
    max_dt = None
    for o in orders_by_id.values():
        dt = _parse_iso_utc(o.get("LastUpdateDate"))
        if dt is not None and (max_dt is None or dt > max_dt):
            max_dt = dt
    if max_dt is None:
        return None
    return normalize_amazon_timestamp(max_dt - WATERMARK_SAFETY_BUFFER)


def _amount(obj) -> float:
    if not obj:
        return 0.0
    try:
        return float(obj.get("Amount") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def aggregate_by_marketplace_mtd(all_orders: dict, marketplaces: list[dict], mtd_start_date, asof_date) -> list[dict]:
    mp_meta = {m["marketplace_id"]: m for m in marketplaces}
    agg: dict = {}
    for o in all_orders.values():
        if (o.get("OrderStatus") or "") == "Canceled":
            continue
        dt = _parse_purchase_date_mt(o.get("PurchaseDate"))
        if dt is None:
            continue
        d = dt.date()
        if not (mtd_start_date <= d <= asof_date):
            continue
        mid = o.get("MarketplaceId")
        e = agg.setdefault(mid, {"orders": 0, "units": 0.0, "sales_native": 0.0, "currency": None})
        e["orders"] += 1
        e["units"] += float(o.get("NumberOfItemsShipped") or 0) + float(o.get("NumberOfItemsUnshipped") or 0)
        total = o.get("OrderTotal") or {}
        e["sales_native"] += _amount(total)
        if e["currency"] is None and total.get("CurrencyCode"):
            e["currency"] = total["CurrencyCode"]

    out = []
    for mid, e in agg.items():
        meta = mp_meta.get(mid, {})
        aov = round(e["sales_native"] / e["orders"], 2) if e["orders"] else None
        out.append({
            "marketplace_id": mid,
            "country": meta.get("country"),
            "currency": e["currency"],
            "orders": e["orders"],
            "units": round(e["units"], 2),
            "sales_native": round(e["sales_native"], 2),
            "aov_native": aov,
        })
    out.sort(key=lambda r: (r["country"] or "", r["marketplace_id"] or ""))
    return out


def aggregate_sku_by_marketplace_mtd(all_orders: dict, all_items: dict, marketplaces: list[dict],
                                      mtd_start_date, asof_date, extra_marketplace_ids: set) -> list[dict]:
    mp_meta = {m["marketplace_id"]: m for m in marketplaces}
    eligible_mids = {m["marketplace_id"] for m in marketplaces if not m.get("in_netsuite")} | set(extra_marketplace_ids)

    agg: dict = {}
    for item in all_items.values():
        oid = item.get("AmazonOrderId")
        order = all_orders.get(oid)
        if not order or (order.get("OrderStatus") or "") == "Canceled":
            continue
        dt = _parse_purchase_date_mt(order.get("PurchaseDate"))
        if dt is None:
            continue
        d = dt.date()
        if not (mtd_start_date <= d <= asof_date):
            continue
        mid = item.get("MarketplaceId") or order.get("MarketplaceId")
        if mid not in eligible_mids:
            continue
        sku = item.get("SellerSKU")
        if not sku:
            continue
        price = item.get("ItemPrice") or {}
        key = (mid, sku)
        e = agg.setdefault(key, {"units": 0.0, "sales_native": 0.0, "currency": price.get("CurrencyCode")})
        try:
            qty = float(item.get("QuantityOrdered") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        e["units"] += qty
        e["sales_native"] += _amount(price)

    out = []
    for (mid, sku), e in agg.items():
        meta = mp_meta.get(mid, {})
        out.append({
            "marketplace_id": mid, "country": meta.get("country"), "sku": sku,
            "units": round(e["units"], 2), "sales_native": round(e["sales_native"], 2),
            "currency": e["currency"],
        })
    out.sort(key=lambda r: (r["country"] or "", r["sku"] or "", -r["sales_native"]))
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _default_last_updated_after(D: dict) -> str:
    return normalize_amazon_timestamp(D["mtd_start_dt"])


def _region_login(label, endpoint, refresh_token, client_id, client_secret, errors_out):
    """LWA token exchange + marketplaceParticipations for one region (fast, unpaced calls).
    On failure, records the reason into `errors_out[label]` (mutated) and returns
    (None, []). Returns (access_token, marketplaces_list) on success."""
    try:
        token = get_access_token(refresh_token, client_id, client_secret)
    except Exception as e:
        errors_out[label] = f"LWA exchange failed: {e}"
        return None, []
    try:
        mps = get_marketplace_participations(endpoint, token)
    except Exception as e:
        errors_out[label] = f"marketplaceParticipations failed: {e}"
        return None, []
    return token, mps


def run_amazon_orders(D: dict, rollups: dict, max_minutes: int = 40) -> dict:
    """FD1b entry point, called from extract.py. `D` is extract.py's date-window dict
    (needs mtd_start_dt, mtd_start (str), asof_date). `rollups` is the parsed
    config/rollups.json (used for features.amazon_sku_items_for, default none)."""
    t_start = time.monotonic()
    deadline = t_start + max_minutes * 60
    half_deadline = t_start + (max_minutes * 60) / 2.0

    client_id = os.environ.get("SP_API_LWA_CLIENT_ID")
    client_secret = os.environ.get("SP_API_LWA_CLIENT_SECRET")
    na_endpoint = os.environ.get("SP_API_NA_ENDPOINT", "https://sellingpartnerapi-na.amazon.com")
    eu_endpoint = os.environ.get("SP_API_EU_ENDPOINT", "https://sellingpartnerapi-eu.amazon.com")
    na_refresh = os.environ.get("SP_API_REFRESH_TOKEN_NA")
    eu_refresh = os.environ.get("SP_API_REFRESH_TOKEN_EU")

    empty = {
        "status": "error", "pulled_through_utc": None, "marketplaces": [],
        "by_marketplace_mtd": [], "sku_by_marketplace_mtd": [],
        "incremental_state": {}, "notes": {},
    }
    if not client_id or not client_secret or not na_refresh or not eu_refresh:
        empty["notes"] = {"error": "SP-API credentials not fully set in env "
                                    "(SP_API_LWA_CLIENT_ID/SECRET, SP_API_REFRESH_TOKEN_NA/EU)"}
        return empty

    d = data_dir()
    state = load_state(d)
    errors: dict = {}

    na_token, na_mps = _region_login("NA", na_endpoint, na_refresh, client_id, client_secret, errors)
    eu_token, eu_mps = _region_login("EU", eu_endpoint, eu_refresh, client_id, client_secret, errors)

    if na_token is None and eu_token is None:
        empty["status"] = "error"
        empty["notes"] = errors
        return empty

    all_marketplaces = na_mps + eu_mps
    notes: dict = {}
    if errors:
        notes.update({k: {"error": v} for k, v in errors.items()})

    na_orders_path = d / "orders_NA.jsonl"
    eu_orders_path = d / "orders_EU.jsonl"
    na_orders_by_id = load_jsonl_by_key(na_orders_path, "AmazonOrderId")
    eu_orders_by_id = load_jsonl_by_key(eu_orders_path, "AmazonOrderId")

    # normalize_amazon_timestamp() sanitizes a pre-existing state value too (not just ones
    # this module writes going forward) -- a malformed value self-heals to Amazon's required
    # format on read rather than being sent straight through and rejected.
    na_lua = normalize_amazon_timestamp(state.get("NA", {}).get("last_updated_after")) or _default_last_updated_after(D)
    eu_lua = normalize_amazon_timestamp(state.get("EU", {}).get("last_updated_after")) or _default_last_updated_after(D)
    na_start_token = state.get("NA", {}).get("next_token")
    eu_start_token = state.get("EU", {}).get("next_token")

    na_completed = eu_completed = False
    na_pages = na_seen = eu_pages = eu_seen = 0
    na_pending_token = eu_pending_token = None

    if na_token is not None and na_mps:
        na_bucket = TokenBucket(ORDERS_RATE_DEFAULT, ORDERS_BURST)
        try:
            na_completed, na_pages, na_seen, na_pending_token = pull_region_orders(
                na_endpoint, na_token, [m["marketplace_id"] for m in na_mps], na_lua, na_start_token,
                half_deadline, na_bucket, na_orders_by_id
            )
        except Exception as e:
            notes["NA"] = {"error": f"getOrders failed: {e}"}
        write_jsonl(na_orders_path, na_orders_by_id)
    elif na_token is not None and not na_mps:
        notes["NA"] = {"error": "no participating (non-'Non-Amazon') marketplaces found"}

    if eu_token is not None and eu_mps:
        eu_bucket = TokenBucket(ORDERS_RATE_DEFAULT, ORDERS_BURST)
        try:
            eu_completed, eu_pages, eu_seen, eu_pending_token = pull_region_orders(
                eu_endpoint, eu_token, [m["marketplace_id"] for m in eu_mps], eu_lua, eu_start_token,
                deadline, eu_bucket, eu_orders_by_id
            )
        except Exception as e:
            notes["EU"] = {"error": f"getOrders failed: {e}"}
        write_jsonl(eu_orders_path, eu_orders_by_id)
    elif eu_token is not None and not eu_mps:
        notes["EU"] = {"error": "no participating (non-'Non-Amazon') marketplaces found"}

    # Watermark: max LastUpdateDate across every order known for the region (this run's
    # pulls plus every prior run's), minus a 5-minute safety buffer -- applied whether or not
    # the region's pagination fully drained, so a cut-short region still advances instead of
    # restarting the whole MTD window every night (team-lead fix, 2026-08-26). Falls back to
    # the prior watermark when no order has a parseable LastUpdateDate yet (e.g. a region
    # that errored before any page landed).
    na_watermark = compute_watermark(na_orders_by_id)
    eu_watermark = compute_watermark(eu_orders_by_id)
    state["NA"] = {
        "last_updated_after": na_watermark if na_watermark else na_lua,
        "orders_seen": len(na_orders_by_id),
        "next_token": na_pending_token,
    }
    state["EU"] = {
        "last_updated_after": eu_watermark if eu_watermark else eu_lua,
        "orders_seen": len(eu_orders_by_id),
        "next_token": eu_pending_token,
    }

    # --- getOrderItems backlog: non-NetSuite marketplaces only, whatever budget remains ---
    na_items_path = d / "order_items_NA.jsonl"
    eu_items_path = d / "order_items_EU.jsonl"
    na_items_by_key = _load_items(na_items_path)
    eu_items_by_key = _load_items(eu_items_path)

    mp_lookup = {m["marketplace_id"]: m for m in all_marketplaces}

    def needs_items(orders_by_id, items_by_key):
        covered = {row["AmazonOrderId"] for row in items_by_key.values()}
        return [
            oid for oid, o in orders_by_id.items()
            if not mp_lookup.get(o.get("MarketplaceId"), {}).get("in_netsuite", True) and oid not in covered
        ]

    na_backlog = needs_items(na_orders_by_id, na_items_by_key) if na_token is not None else []
    eu_backlog = needs_items(eu_orders_by_id, eu_items_by_key) if eu_token is not None else []

    items_na_completed = not na_backlog
    items_eu_completed = not eu_backlog
    na_items_skipped = eu_items_skipped = []

    if na_backlog:
        na_items_bucket = TokenBucket(ITEMS_RATE_DEFAULT, ITEMS_BURST)
        mp_by_order = {oid: na_orders_by_id[oid]["MarketplaceId"] for oid in na_backlog}
        try:
            items_na_completed, n_pulled, na_items_skipped = pull_order_items(
                na_endpoint, na_token, na_backlog, mp_by_order, deadline, na_items_bucket, na_items_by_key
            )
        except Exception as e:
            notes.setdefault("NA", {})["items_error"] = f"getOrderItems failed: {e}"
            items_na_completed = False
        write_jsonl(na_items_path, na_items_by_key)

    if eu_backlog:
        eu_items_bucket = TokenBucket(ITEMS_RATE_DEFAULT, ITEMS_BURST)
        mp_by_order = {oid: eu_orders_by_id[oid]["MarketplaceId"] for oid in eu_backlog}
        try:
            items_eu_completed, n_pulled, eu_items_skipped = pull_order_items(
                eu_endpoint, eu_token, eu_backlog, mp_by_order, deadline, eu_items_bucket, eu_items_by_key
            )
        except Exception as e:
            notes.setdefault("EU", {})["items_error"] = f"getOrderItems failed: {e}"
            items_eu_completed = False
        write_jsonl(eu_items_path, eu_items_by_key)

    save_state(d, state)

    # --- aggregation, over everything on disk (this run's data plus every prior run's) ---
    all_orders = {**na_orders_by_id, **eu_orders_by_id}
    all_items = {**na_items_by_key, **eu_items_by_key}
    mtd_start_date = datetime.date.fromisoformat(D["mtd_start"])
    asof_date = D["asof_date"] if isinstance(D["asof_date"], datetime.date) else datetime.date.fromisoformat(D["asof"])

    extra_sku_mids = set((rollups or {}).get("features", {}).get("amazon_sku_items_for", []) or [])
    by_marketplace_mtd = aggregate_by_marketplace_mtd(all_orders, all_marketplaces, mtd_start_date, asof_date)
    sku_by_marketplace_mtd = aggregate_sku_by_marketplace_mtd(
        all_orders, all_items, all_marketplaces, mtd_start_date, asof_date, extra_sku_mids
    )

    both_orders_ok = na_completed and eu_completed
    both_items_ok = items_na_completed and items_eu_completed
    if na_token is None and eu_token is None:
        overall = "error"  # unreachable in practice (early-returned above); kept as a safe default
    elif na_token is None or eu_token is None:
        overall = "partial"  # one region never got past LWA/participations
    elif both_orders_ok and both_items_ok:
        overall = "ok"
    else:
        overall = "partial"

    def region_lua(label):
        v = state.get(label, {}).get("last_updated_after")
        if not v:
            return None
        try:
            dt = datetime.datetime.fromisoformat(v)
            return dt.astimezone(datetime.timezone.utc)
        except ValueError:
            return None

    watermarks = [w for w in (region_lua("NA"), region_lua("EU")) if w is not None]
    pulled_through_utc = min(watermarks).isoformat() if watermarks else None

    notes["NA"] = {
        **notes.get("NA", {}),
        "pages_this_run": na_pages, "orders_seen_this_run": na_seen,
        "orders_completed_this_run": na_completed,
        "items_backlog_started": len(na_backlog), "items_completed_this_run": items_na_completed,
        "items_skipped": na_items_skipped,
    }
    notes["EU"] = {
        **notes.get("EU", {}),
        "pages_this_run": eu_pages, "orders_seen_this_run": eu_seen,
        "orders_completed_this_run": eu_completed,
        "items_backlog_started": len(eu_backlog), "items_completed_this_run": items_eu_completed,
        "items_skipped": eu_items_skipped,
    }
    notes["runtime_seconds"] = round(time.monotonic() - t_start, 2)
    notes["budget_minutes"] = max_minutes

    return {
        "status": overall,
        "pulled_through_utc": pulled_through_utc,
        "marketplaces": all_marketplaces,
        "by_marketplace_mtd": by_marketplace_mtd,
        "sku_by_marketplace_mtd": sku_by_marketplace_mtd,
        "incremental_state": {"NA": state.get("NA", {}), "EU": state.get("EU", {})},
        "notes": notes,
    }


def _load_items(path: Path) -> dict:
    out = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = row.get("OrderItemId") or f"{row.get('AmazonOrderId')}:{row.get('SellerSKU')}:{row.get('ASIN')}"
                out[key] = row
    return out
