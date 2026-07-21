# muni-basis-tracker

Daily automated tracker of the tax-exempt vs. taxable short-rate basis — the
SIFMA Municipal Swap Index against SOFR — with Treasury par-curve context. A
GitHub Action runs each weekday morning, appends one row to
[`data/history.csv`](data/history.csv), and commits only when something
actually printed.

## What the SIFMA/SOFR ratio measures

SIFMA resets weekly off high-grade tax-exempt VRDO remarketings; SOFR is the
overnight secured taxable rate. The ratio between them is the cleanest public
read on where tax-exempt money clears relative to taxable — effectively, what
the market is paying for the exemption at the very front of the curve. For a
top-bracket buyer the theoretical fair ratio sits in the low 60s; the street
convention for floater economics and percentage-of-SOFR hedges is 70%, which
is why the sheet also carries `sifma_vs_70pct_sofr` (SIFMA − 0.70 × SOFR) as a
spread in rate terms. Persistent prints above 70% mean tax-exempt floating
funding is rich to its taxable hedge — painful basis for TOB programs and
issuers hedging VRDO exposure with %-of-SOFR swaps; sub-70 prints are the
mirror image. The series is violently seasonal: expect the ratio to gap around
April tax outflows, January/July coupon reinvestment, and quarter-end
balance-sheet dates, so a single print means little without its calendar
context.

## Sources and publication schedules

| Series | Source | Schedule |
|---|---|---|
| SOFR | [NY Fed Markets API](https://markets.newyorkfed.org/api/rates/secured/sofr/last/1.json) | T+1, ~8:00am ET each business day |
| UST par yields (2/5/10/30) | [Treasury daily yield curve CSV](https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve) | Same evening, ~6:00pm ET |
| UST fallback | FRED `DGS2/DGS5/DGS10/DGS30` ([fredgraph.csv](https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10), no key) | Mirrors H.15 with an extra lag |
| SIFMA Municipal Swap Index | [SIFMA historical-data workbook](https://www.sifma.org/resources/guides-playbooks/about-the-municipal-swap-index) | Weekly, resets Wednesday ~4:00pm ET |

Because the three sources publish on different lags, every series carries its
own `*_as_of` date in the CSV alongside the `run_date`. The 9am ET cron means
each row normally pairs yesterday's SOFR with yesterday's Treasury close and
the most recent Wednesday's SIFMA.

## Data notes

- **SIFMA is weekly.** On non-Wednesday runs the last reset is carried at its
  original `sifma_as_of`. If SIFMA's site is ever unreachable, the previous
  value is carried forward and the column is nullable — a SIFMA outage never
  fails the run.
- **The SIFMA workbook URL lies about its age.** It lives at a static
  `wp-content/uploads/2024/01/` path, but SIFMA overwrites the file in place
  after each Wednesday reset (verified via `Last-Modified`). There is no free
  JSON/CSV feed — Bloomberg (`MUNIPSA:IND`) is the official publisher — so
  this workbook is the only free machine-readable source. It is a bare
  two-column xlsx (Excel serial dates), parsed here with the stdlib rather
  than a spreadsheet dependency.
- **Ratio uses mixed as-ofs by construction.** `sifma_sofr_ratio` pairs the
  freshest weekly SIFMA with the freshest daily SOFR, which is how desks quote
  it — but the as-ofs can differ by up to a week, so be careful interpreting
  the ratio in weeks when the front end is moving (FOMC weeks in particular).
- **2s10s never mixes curve dates.** The Treasury parser takes the newest row
  with all four tenors populated (early-January and half-day rows can be
  partial); the FRED fallback uses the latest date on which all four series
  printed.
- **Holidays are no-ops.** No SOFR print, no new Treasury close, mid-week
  SIFMA unchanged → the script exits without writing and the Action skips the
  commit. Same-day reruns are idempotent for the same reason.

## Layout

```
data/history.csv    one row per run; per-series values and as-of dates
data/latest.json    latest snapshot of every series plus computed fields
scripts/fetch.py    the whole pipeline (Python 3.12, requests + stdlib)
```

Computed columns: `sifma_sofr_ratio`, `sifma_vs_70pct_sofr`,
`ust_2s10s` (10Y − 2Y).

## Running locally

```
pip install requests
python scripts/fetch.py
```

## License

MIT
