"""Regenerate the frozen market-data fixtures from their public sources.

Run manually when the fixture needs to change; the environment never executes
this script and never reaches the network at task time.

    python3 market_data_fixtures/build_fixture.py

Sources
-------
Treasury par yield curve (CMT), U.S. Department of the Treasury:
    https://home.treasury.gov/resource-center/data-chart-center/interest-rates/
    TextView?type=daily_treasury_yield_curve
CPI-U (series CUUR0000SA0), U.S. Bureau of Labor Statistics public API v1:
    https://api.bls.gov/publicAPI/v1/timeseries/data/

Both are works of the U.S. federal government and are in the public domain.
"""

from __future__ import annotations

import csv
import io
import json
import urllib.request
from datetime import date
from pathlib import Path

OUT_DIR = Path(__file__).parent

# The fixture stops just past the latest date any task in the two Investment
# Banking worlds refers to, so a lookup beyond the worlds' horizon reports a
# coverage miss instead of returning data the scenario could not have had.
CMT_START = date(2023, 1, 1)
CMT_END = date(2026, 1, 31)
CPI_START = "2019-01-01"
CPI_END = "2025-12-01"

TREASURY_CSV = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all"
    "?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv"
)
BLS_API = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
BLS_SERIES = "CUUR0000SA0"

# Treasury's CSV column -> the maturity key used by the fmp_market tool. The key
# names and their order mirror the upstream FMP treasury_rates response so an
# agent that knows that shape can read this one. Treasury also publishes 1.5-month
# and 4-month CMTs; they have no counterpart key and are not carried.
MATURITIES = [
    ("1 Mo", "month1"),
    ("2 Mo", "month2"),
    ("3 Mo", "month3"),
    ("6 Mo", "month6"),
    ("1 Yr", "year1"),
    ("2 Yr", "year2"),
    ("3 Yr", "year3"),
    ("5 Yr", "year5"),
    ("7 Yr", "year7"),
    ("10 Yr", "year10"),
    ("20 Yr", "year20"),
    ("30 Yr", "year30"),
]

USER_AGENT = "APEX-Agents market-data fixture builder"


def _fetch(url: str, data: bytes | None = None) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def build_treasury() -> dict:
    rows: dict[str, dict[str, float]] = {}
    for year in range(CMT_START.year, CMT_END.year + 1):
        text = _fetch(TREASURY_CSV.format(year=year)).decode("utf-8-sig")
        for rec in csv.DictReader(io.StringIO(text)):
            month, day, yr = rec["Date"].split("/")
            when = date(int(yr), int(month), int(day))
            if not CMT_START <= when <= CMT_END:
                continue
            row = {}
            for column, key in MATURITIES:
                raw = (rec.get(column) or "").strip()
                if raw and raw != "N/A":
                    row[key] = float(raw)
            rows[when.isoformat()] = row
    ordered = dict(sorted(rows.items()))
    dates = list(ordered)
    return {
        "series": "daily_treasury_par_yield_curve_rates",
        "description": (
            "Daily Treasury Par Yield Curve Rates (constant maturity Treasury, CMT), "
            "percent per annum, quoted on a bond-equivalent basis."
        ),
        "units": "percent_per_annum",
        "source": {
            "publisher": "U.S. Department of the Treasury",
            "dataset": "Daily Treasury Par Yield Curve Rates",
            "url": (
                "https://home.treasury.gov/resource-center/data-chart-center/"
                "interest-rates/TextView?type=daily_treasury_yield_curve"
            ),
            "rights": "Work of the U.S. federal government; public domain.",
            "retrieved": date.today().isoformat(),
        },
        "coverage": {
            "start": dates[0],
            "end": dates[-1],
            "observations": len(dates),
            "frequency": "business_daily",
            "maturities": [key for _, key in MATURITIES],
            "note": (
                "Business days only. Federal holidays and weekends carry no observation "
                "and are reported as unavailable rather than filled from a neighbouring day."
            ),
        },
        "rows": ordered,
    }


def build_cpi() -> dict:
    payload = json.dumps(
        {
            "seriesid": [BLS_SERIES],
            "startyear": CPI_START[:4],
            "endyear": CPI_END[:4],
        }
    ).encode()
    body = json.loads(_fetch(BLS_API, payload))
    if body.get("status") != "REQUEST_SUCCEEDED":
        raise RuntimeError(f"BLS request failed: {body.get('status')} {body.get('message')}")

    observations: dict[str, float] = {}
    unavailable: dict[str, str] = {}
    for rec in body["Results"]["series"][0]["data"]:
        if not rec["period"].startswith("M") or rec["period"] == "M13":
            continue
        when = f"{rec['year']}-{rec['period'][1:]}-01"
        if not CPI_START <= when <= CPI_END:
            continue
        if rec["value"] in ("-", ""):
            notes = [f["text"] for f in rec.get("footnotes", []) if f.get("text")]
            unavailable[when] = notes[0] if notes else "Not published by BLS."
            continue
        observations[when] = float(rec["value"])

    ordered = dict(sorted(observations.items()))
    dates = sorted(list(ordered) + list(unavailable))
    return {
        "series": "CPI",
        "series_id": BLS_SERIES,
        "description": (
            "Consumer Price Index for All Urban Consumers (CPI-U), U.S. city average, "
            "all items, not seasonally adjusted."
        ),
        "units": "index_1982_1984_equals_100",
        "source": {
            "publisher": "U.S. Bureau of Labor Statistics",
            "dataset": "Consumer Price Index - All Urban Consumers (CPI-U)",
            "url": "https://data.bls.gov/timeseries/CUUR0000SA0",
            "api": BLS_API,
            "rights": "Work of the U.S. federal government; public domain.",
            "retrieved": date.today().isoformat(),
        },
        "coverage": {
            "start": dates[0],
            "end": dates[-1],
            "observations": len(ordered),
            "frequency": "monthly",
            "date_convention": "first day of the reference month",
        },
        "observations": ordered,
        "unavailable": dict(sorted(unavailable.items())),
    }


def main() -> None:
    for name, payload in (
        ("treasury_par_yield_curve.json", build_treasury()),
        ("cpi_u.json", build_cpi()),
    ):
        path = OUT_DIR / name
        path.write_text(json.dumps(payload, indent=1, sort_keys=False) + "\n")
        print(f"wrote {path} ({path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
