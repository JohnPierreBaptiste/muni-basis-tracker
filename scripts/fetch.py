#!/usr/bin/env python3
"""Daily pull of the tax-exempt vs. taxable short-rate basis.

Series:
  SOFR  - NY Fed Markets API (prints T+1, business days)
  UST   - Treasury daily par yield curve 2s/5s/10s/30s (FRED DGS* fallback)
  SIFMA - SIFMA Municipal Swap Index, weekly Wednesday reset, read from
          SIFMA's historical-data workbook (overwritten in place each week)

Appends one row per run to data/history.csv and rewrites data/latest.json.
If no series has a fresh as-of date versus the last stored row, exits 0
without touching either file so the Action can skip the commit.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import sys
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
HISTORY = ROOT / "data" / "history.csv"
LATEST = ROOT / "data" / "latest.json"

TIMEOUT = 30
UA = {
    "User-Agent": "muni-basis-tracker/1.0 (+https://github.com/JohnPierreBaptiste/muni-basis-tracker)"
}

SOFR_URL = "https://markets.newyorkfed.org/api/rates/secured/sofr/last/1.json"
TREASURY_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
    "&field_tdr_date_value={year}&page&_format=csv"
)
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
# Static-looking path, but SIFMA overwrites this workbook after each Wednesday reset.
SIFMA_URL = "https://www.sifma.org/wp-content/uploads/2024/01/Muni-Swap-Historical-Data.xlsx"

TENOR_COLUMNS = {"ust_2y": "2 Yr", "ust_5y": "5 Yr", "ust_10y": "10 Yr", "ust_30y": "30 Yr"}
FRED_IDS = {"ust_2y": "DGS2", "ust_5y": "DGS5", "ust_10y": "DGS10", "ust_30y": "DGS30"}

FIELDS = [
    "run_date",
    "sofr", "sofr_as_of",
    "sifma", "sifma_as_of",
    "ust_2y", "ust_5y", "ust_10y", "ust_30y", "ust_as_of",
    "sifma_sofr_ratio", "sifma_vs_70pct_sofr", "ust_2s10s",
]
AS_OF_KEYS = ("sofr_as_of", "sifma_as_of", "ust_as_of")

XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
EXCEL_EPOCH = date(1899, 12, 30)

log = logging.getLogger("fetch")


def fetch_sofr() -> tuple[float, str]:
    resp = requests.get(SOFR_URL, headers=UA, timeout=TIMEOUT)
    resp.raise_for_status()
    ref = resp.json()["refRates"][0]
    return float(ref["percentRate"]), ref["effectiveDate"]


def _treasury_direct() -> dict:
    year = datetime.now(timezone.utc).year
    resp = requests.get(TREASURY_URL.format(year=year), headers=UA, timeout=TIMEOUT)
    resp.raise_for_status()
    # Rows are newest-first; take the newest row with all four tenors populated
    # (early-year and half-day rows can be partial).
    for row in csv.DictReader(io.StringIO(resp.text)):
        try:
            vals = {key: float(row[col]) for key, col in TENOR_COLUMNS.items()}
            month, day, yr = row["Date"].split("/")
        except (KeyError, TypeError, ValueError):
            continue
        vals["ust_as_of"] = f"{yr}-{month.zfill(2)}-{day.zfill(2)}"
        return vals
    raise ValueError("no complete curve row in Treasury CSV")


def _treasury_fred() -> dict:
    series: dict[str, dict[str, str]] = {}
    for key, sid in FRED_IDS.items():
        resp = requests.get(FRED_URL.format(series=sid), headers=UA, timeout=TIMEOUT)
        resp.raise_for_status()
        rows = list(csv.reader(io.StringIO(resp.text)))
        series[key] = {r[0]: r[1] for r in rows[1:] if len(r) == 2 and r[1] not in (".", "")}
    # A 2s10s off mixed curve dates is meaningless; use the latest date where
    # every tenor printed.
    common = set.intersection(*(set(s) for s in series.values()))
    if not common:
        raise ValueError("no common observation date across FRED series")
    as_of = max(common)
    vals = {key: float(s[as_of]) for key, s in series.items()}
    vals["ust_as_of"] = as_of
    return vals


def fetch_treasury() -> dict:
    try:
        return _treasury_direct()
    except Exception as exc:
        log.warning("treasury.gov fetch failed (%s); falling back to FRED", exc)
        return _treasury_fred()


def fetch_sifma() -> tuple[float, str]:
    resp = requests.get(SIFMA_URL, headers=UA, timeout=TIMEOUT)
    resp.raise_for_status()
    book = zipfile.ZipFile(io.BytesIO(resp.content))
    try:
        strings = [
            "".join(t.text or "" for t in si.iter(f"{XLSX_NS}t"))
            for si in ET.fromstring(book.read("xl/sharedStrings.xml"))
        ]
    except KeyError:
        strings = []
    sheet = ET.fromstring(book.read("xl/worksheets/sheet1.xml"))
    best: tuple[date, float] | None = None
    for row in sheet.iter(f"{XLSX_NS}row"):
        cells = []
        for c in row.iter(f"{XLSX_NS}c"):
            v = c.find(f"{XLSX_NS}v")
            raw = v.text if v is not None else ""
            if c.get("t") == "s" and raw:
                raw = strings[int(raw)]
            cells.append(raw)
        if len(cells) < 2:
            continue
        try:
            as_of = EXCEL_EPOCH + timedelta(days=int(float(cells[0])))
            value = float(cells[1])
        except ValueError:
            continue  # header row
        if best is None or as_of > best[0]:
            best = (as_of, value)
    if best is None:
        raise ValueError("no data rows parsed from SIFMA workbook")
    return best[1], best[0].isoformat()


def read_last_row() -> dict | None:
    if not HISTORY.exists():
        return None
    with HISTORY.open(newline="") as fh:
        last = None
        for last in csv.DictReader(fh):
            pass
    return last


def is_fresh(row: dict, last: dict | None) -> bool:
    if last is None:
        return any(row[k] for k in AS_OF_KEYS)
    return any(row[k] and row[k] > (last.get(k) or "") for k in AS_OF_KEYS)


def _num(value) -> float | None:
    return None if value in ("", None) else float(value)


def append_row(row: dict) -> None:
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    new_file = not HISTORY.exists()
    with HISTORY.open("a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def write_latest(row: dict) -> None:
    payload = {
        "run_date": row["run_date"],
        "sofr": {"value": _num(row["sofr"]), "as_of": row["sofr_as_of"] or None},
        "sifma": {"value": _num(row["sifma"]), "as_of": row["sifma_as_of"] or None},
        "treasury": {
            "as_of": row["ust_as_of"] or None,
            "2y": _num(row["ust_2y"]),
            "5y": _num(row["ust_5y"]),
            "10y": _num(row["ust_10y"]),
            "30y": _num(row["ust_30y"]),
        },
        "computed": {
            "sifma_sofr_ratio": _num(row["sifma_sofr_ratio"]),
            "sifma_vs_70pct_sofr": _num(row["sifma_vs_70pct_sofr"]),
            "ust_2s10s": _num(row["ust_2s10s"]),
        },
    }
    LATEST.parent.mkdir(parents=True, exist_ok=True)
    LATEST.write_text(json.dumps(payload, indent=2) + "\n")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    row: dict = {k: "" for k in FIELDS}
    row["run_date"] = datetime.now(timezone.utc).date().isoformat()
    last = read_last_row()

    try:
        row["sofr"], row["sofr_as_of"] = fetch_sofr()
    except Exception as exc:
        log.warning("SOFR fetch failed: %s", exc)

    try:
        row.update(fetch_treasury())
    except Exception as exc:
        log.warning("Treasury fetch failed on both sources: %s", exc)

    try:
        row["sifma"], row["sifma_as_of"] = fetch_sifma()
    except Exception as exc:
        log.warning("SIFMA fetch failed (%s); carrying last known value forward", exc)
        if last and last.get("sifma"):
            row["sifma"], row["sifma_as_of"] = last["sifma"], last["sifma_as_of"]

    sofr, sifma = _num(row["sofr"]), _num(row["sifma"])
    if sofr and sifma is not None:
        row["sifma_sofr_ratio"] = round(sifma / sofr, 4)
        row["sifma_vs_70pct_sofr"] = round(sifma - 0.70 * sofr, 4)
    two_yr, ten_yr = _num(row["ust_2y"]), _num(row["ust_10y"])
    if two_yr is not None and ten_yr is not None:
        row["ust_2s10s"] = round(ten_yr - two_yr, 4)

    if not is_fresh(row, last):
        log.info("no series has a new as-of date; nothing to write")
        return 0

    append_row(row)
    write_latest(row)
    log.info(
        "appended %s: SOFR %s (%s), SIFMA %s (%s), ratio %s",
        row["run_date"], row["sofr"], row["sofr_as_of"],
        row["sifma"], row["sifma_as_of"], row["sifma_sofr_ratio"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
