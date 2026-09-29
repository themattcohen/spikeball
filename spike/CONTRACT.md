# latest.json contract, v2 (build 2026-08-26 evening)

Authoritative for every producer and consumer in this subproject. v1 keys from PRD Section 4 stay as
they are unless listed under "Changed". Money = JSON numbers rounded to 2 decimals, USD base currency,
revenue positive, cogs positive. Dates `YYYY-MM-DD` America/Denver. `pulled_at_mt` ISO-8601 with offset.

## Changed

- `meta.asof_date` is YESTERDAY in America/Denver by default (ruling R10, "through yesterday's close");
  `mtd_start`/`ytd_start`/prior-year windows derive from it. `--asof` overrides for tests only.
- `meta.period_rule` = `"through_yesterday_close"`.
- `meta.features` = copy of `config/rollups.json` `features` (e.g. `{"amazon_aov": false}`).
- `meta.rollups` = the full parsed `config/rollups.json`, so consumers never re-read the file.
- `meta.checks` = `{ "a_channel_foot": {"pass": bool, "detail": str}, "b_inventory_tie": {...},
  "c_rows_present": {...}, "d_fresh": {...}, "e_asof": {...}, "f_picklist_stable": {...},
  "g_closed_months_stable": {...}, "t5_bom_rule": {...}, "all_pass": bool }`. Producer: `checks.py`.
  `f` and `g` compare against `--prev-state` (see below); when no prior state exists they pass with
  detail "no prior state".
- `pnl_by_channel_month[*]` gains `revenue_py`, `cogs_py` (same calendar month, prior year).
- `inventory.onhand_by_item_location[*]` gains `item_class` (NetSuite item class name or null),
  `location_country`, `location_state`, `location_city` (from the location record; null when blank).
- `amazon_spapi` is REMOVED. Replaced by `amazon_orders` (below).

## Added

- `rollup_by_period`: `[{"key", "label", "channel_ids": [..], "mtd": {revenue, cogs, gp, margin_pct|null},
  "ytd": {...}, "mtd_prior_year": {...}, "ytd_prior_year": {...}, "yoy_mtd_pct": num|null,
  "yoy_ytd_pct": num|null, "show_margin": bool, "margin_note": str|null}]` for every group in
  `meta.rollups.groups`, then the Unassigned group, then `{"key": "total", ...}`. Sums come from
  `pnl_by_channel_period` by id; the total row equals the TOTAL row of `pnl_by_channel_period`.
- `rollup_by_month`: `[{"ym", "key", "label", "revenue", "cogs", "gp", "margin_pct", "revenue_py"}]`
  for 13 months x (groups + unassigned).
- `dtc_by_region.rollup` is `{"US": {"mtd","ytd","trailing13"}, "Other": {...}}` using
  `meta.rollups.regions.us_region_ids`; region ids in `data_quality_region_ids` with non-zero revenue
  go to `dtc_by_region.data_quality` and are excluded from Other.
- `orders_by_channel`: `[{"channel_id", "channel", "mtd_orders", "ytd_orders", "mtd_aov", "ytd_aov",
  "note"}]`. Orders = distinct posting `CustInvc` + `CashSale` transactions with Income lines in the
  channel and window; AOV = revenue / orders. For channel 1 (Amazon) `mtd_orders`/`ytd_orders` are
  null with note "consolidated invoices since 2026-08-20, no order grain in NetSuite; see amazon_orders".
- `returns_by_channel[*]` gains `mtd_return_rate_pct`, `ytd_return_rate_pct` = -credits / revenue x 100
  (credits are negative numbers in v1; rate is positive), null when revenue is 0.
- `inventory.days_on_hand`: `[{"sku", "item_id", "itemtype", "onhand_total", "units_90d",
  "avg_daily_units", "days_on_hand"}]` for the top 25 YTD sellers across channels; `units_90d` from the
  same Income-line SKU rule over the trailing 90 days ending `asof_date`; Kit rows carry
  `onhand_total: null, days_on_hand: null`.
- `picklist_snapshot`: `{"channels": {"<id>": "name"}, "regions": {"<id>": "name"}}`.
- `amazon_orders`: `{"status": "ok"|"partial"|"skipped"|"error", "pulled_through_utc": str|null,
  "marketplaces": [{"marketplace_id", "country", "name", "in_netsuite": bool}],
  "by_marketplace_mtd": [{"marketplace_id", "country", "currency", "orders", "units", "sales_native",
  "aov_native"}], "sku_by_marketplace_mtd": [{"marketplace_id", "country", "sku", "units",
  "sales_native", "currency"}] (item-level only for marketplaces with `in_netsuite: false`, plus any
  marketplace listed in `features.amazon_sku_items_for`), "incremental_state": {"NA": {"last_updated_after":
  str|null, "orders_seen": int}, "EU": {...}}, "notes": {..}}`. Source: SP-API Orders API `getOrders`
  (all 13 live marketplaces, paced 1 request/min after a burst of 20, 100 orders per page,
  `LastUpdatedAfter` incremental; statuses exclude Canceled) and `getOrderItems` (0.5 req/s) only for
  non-NetSuite marketplaces. Order-level rows persist in BigQuery `amazon_orders` /
  `amazon_order_items` when publishing is available; the JSON carries aggregates only.

## Prior-run state (for checks f and g)

`checks.py` accepts `--prev-state PATH`, a small JSON: `{"pulled_at_mt", "picklist_snapshot",
"closed_months": {"YYYY-MM": {"revenue", "cogs", "ntxn"}}}`. `extract.py --write-state PATH` writes
the same shape from the current run. `publish_bq.py` stores/fetches it as the `run_state` table so the
routine (fresh clone each night) can compare with the previous night; local file fallback.

## Publishers

- `publish_sheet.py --data latest.json --sheet <id>`: one tab per top-level list/dict key, flattened
  to a rectangular table (nested `mtd`/`ytd` objects become `mtd_revenue`, `mtd_cogs`, ...), full
  overwrite; a `meta` tab (key/value) with `pulled_at_mt`, `asof_date`, check results; a `run_log` tab
  appended one row per run. Tabs are the Looker Studio sources.
- `publish_bq.py --data latest.json --project <id> --dataset spikeball_finance`: same tables,
  WRITE_TRUNCATE loads via the REST API (newline-delimited JSON), `run_log` and `run_state` appended.
- Both refuse to write when `meta.checks.all_pass` is false unless `--force`.

## Surface

- `template.html` reads `meta.rollups` for groups, colors, labels; never hardcodes ids.
- `meta.features.amazon_aov` false: the Amazon AOV tile and the `by_marketplace_mtd` AOV column are
  rendered hidden (present in DOM with `hidden` attribute) so flipping the flag needs no code change.
- Every period label says "through <asof_date>" and "provisional" for the open month.
- Stale state: when `pulled_at_mt` is older than 26 h at render time, a banner "Data as of <ts>, refresh
  overdue" shows above the KPI row.

## Gross revenue and revenue plan (added 2026-09-29)

Net `revenue` everywhere is unchanged. Gross is additive.

- Gross definition: sum of posting lines (`ai.posting='T'`) on accounts with `accttype='Income'` AND
  `acctnumber IN ('40100000','40104000','40105000')` (gross sales, tournaments, shipping; the keys of
  `config/pnl_map.json` `gross_component_labels`), sign-flipped like `revenue` (`-amount`), rounded 2dp.
  It equals `pnl_channel_gross_net[*].gross_revenue` for the same channel and month.
- `gross_revenue` is `null` (never a fabricated 0) when its query failed after 3 attempts; the net
  sections are unaffected and check q reports it.
- `rollup_by_month[*]` gains `gross_revenue`, `gross_revenue_py`.
- `pnl_by_channel_month[*]` gains `gross_revenue`, `gross_revenue_py` (same semantics per channel,
  including the Unassigned / `channel_id: null` row).
- `rollup_by_period[*]`: each of `mtd`, `ytd`, `mtd_prior_year`, `ytd_prior_year` gains `gross_revenue`;
  the entry gains `yoy_mtd_gross_pct`, `yoy_ytd_gross_pct` (same formula as `yoy_mtd_pct` on gross;
  `null` when the prior-year gross is 0 or unavailable).
- `pnl_by_channel_period[*]` (including the `TOTAL` row): the same four period dicts gain `gross_revenue`.
- `extract.py --revenue-plan PATH`: optional JSON written by run_nightly's fetch step (same pattern as
  `--demand-plan`). After `rollup_by_month` is built, `revenue_plan.build_outputs(plan_json,
  rollup_by_month, asof_date_str, rollups_cfg)` produces the three keys below; without the flag it is
  called with `plan_json=None` and returns `valid: false`, empty rows and a note. The call is guarded:
  on any exception `extract.py` prints one line `REVENUE_PLAN_BUILD_ERROR <msg>`, emits
  `revenue_plan_meta` with `valid: false` and `error` set, and empty lists. It never fails the nightly.
- Revenue Plan tab rows (`Channel | Series | YYYY-MM...`): Channel is a rollup label or key from
  `config/rollups.json`, or `Total` (case-insensitive, trimmed): the explicit total row for that series,
  key `total`, label `Total`, allowed for any series. Series `Plan` is the plan of record; series
  `Forecast` is the CFO's forecast of record (the "La Plata Forecast", total grain only: one `Total |
  Forecast` row seeded by `tools/seed_revenue_plan.py --series forecast` from the CFO workbook tab
  "2026 Forecast" row "Gross Revenue"). Any other series is parsed into `revenue_plan_month` and drawn
  nowhere. Unknown channels, blank Series, unparsable amounts and a duplicate (channel, series) are
  dropped and listed in `dropped_rows`.
- Series total rule (plan and forecast alike): a series' total for a month is its explicit `Total` row
  when present, else the sum of its channel rows, else `null`. A channel row's value is that series'
  channel amount or `null`. The plan has no `Total` row today, so plan output is unchanged by the rule.
- `revenue_plan_meta`: `{"valid": bool, "stale": bool, "fetched_at_mt": str|null, "source": "Revenue Plan",
  "year": int|null, "series": ["forecast", "plan"] (sorted, only the series present), "month_columns":
  [ym..], "row_count": int, "dropped_rows": [..], "note": str, "error": str|null, "forecast_available":
  bool, "forecast_grain": "total"|"channel"|"mixed"|null}`. `forecast_available` = at least one
  `Forecast` row parsed. `forecast_grain`: `total` = only a `Total` forecast row, `channel` = only channel
  forecast rows, `mixed` = both, `null` = no forecast row. Sheet tabs `revenue_plan_meta_summary` (the
  scalars, including the two forecast fields) and `revenue_plan_meta_series` come from this dict through
  the generic publisher split; no publisher code lists these columns.
- `revenue_plan_month`: `[{"ym", "key", "label", "series", "plan_gross": num}]`, one row per (series, key,
  month) with an amount. `plan_gross` is the row's amount for ITS series: on a `series: "forecast"` row it
  is the forecast amount. The column keeps that name because the Sheet tab and BigQuery table already
  carry it. Keys are `amazon`, `dtc`, `wholesale`, `other_b2b` (only when the tab has such a row) and
  `total` (the explicit Total row of a series, when present).
- `plan_vs_actual_month`: `[{"ym", "key", "label", "plan_gross": num|null, "actual_gross": num|null,
  "variance": num|null, "variance_pct": num|null, "basis": "actual"|"open"|"future"|"no_plan",
  "forecast_gross": num|null, "variance_vs_forecast": num|null, "variance_vs_forecast_pct": num|null}]`
  for every rollup key in rollup order plus `key: "total"` (label `Total`). Total plan and total forecast
  follow the series total rule above; total actual = sum of `gross_revenue` over ALL rollup keys including
  `other_b2b` and unassigned, which equals the hero total. `variance` = actual - plan and `variance_pct`
  = variance / plan * 100 (1dp, `null` when plan is 0). `variance_vs_forecast` = actual - forecast and
  `variance_vs_forecast_pct` = that / forecast * 100 (1dp, `null` when forecast is 0 or null). Both
  variances are `null` whenever `actual_gross` is `null` (future months, months outside the actuals
  window). `forecast_gross` is populated for future months like `plan_gross`. `basis` depends on the
  plan side only: `actual` = month before the as-of month; `open` = the as-of month (provisional MTD);
  `future` = after as-of (`actual_gross` null); `no_plan` = actuals but no plan row. A forecast without a
  plan does not change `basis`. Every row carries all eleven fields; with no forecast row the three
  forecast fields are `null` on every row and the plan fields are byte-for-byte what they were before
  the forecast series existed (tests/test_revenue_plan.py pins this).
- Stale fallback (`resolve_snapshot`): the last known-good snapshot is reused whole, so forecast and
  `Total` rows survive a failed read exactly like plan rows.
- `meta.plan_year` (int|null, from `revenue_plan_meta.year`), `meta.chart_months` (sorted ym list: the
  union of `trailing_months` and the plan year's 12 months when the plan is valid, else
  `trailing_months`), `meta.default_range` `{"start", "end"}` as ym strings: plan year Jan..Dec when the
  plan is valid, else `trailing_months[0]..trailing_months[-1]`.
- `meta.checks_v2` gains `q_gross_tie` / `gross_tie_ok` and `r_revenue_plan_ok` / `revenue_plan_ok`.
  Check q: for every (channel_id, ym) in both `pnl_by_channel_month` and `pnl_channel_gross_net`,
  `gross_revenue` matches to the cent, and per month the channel sum equals the other build's total
  gross; up to 5 examples are reported. Check r: `revenue_plan_meta.valid` and a non-empty
  `plan_vs_actual_month`. Both are INFORMATIONAL: like check p they never feed `v2_pass` or
  `meta.checks.all_pass`, so a plan or gross problem cannot block publishing.
