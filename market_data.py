"""Frozen market and macroeconomic reference data for the Investment Banking worlds.

Two worlds -- Investment Banking World 246 and Investment Banking World 244 --
pose valuation tasks whose answers hinge on published rates and index levels that
no static world filesystem can hold: a Treasury constant-maturity yield on a named
date, or the CPI-U level in a named month. This module serves those figures from
a fixture committed alongside the code, so a run is reproducible and never depends
on a live data provider.

Coverage is deliberately narrow. The fixture holds the daily Treasury par yield
curve and the CPI-U index, and nothing else; a query it cannot answer says so.

The tool surface mirrors the ``fmp_market`` meta-tool of the FMP MCP server --
same tool name, same ``action`` dispatch, same response field names -- so the
shape is familiar to a model that has seen that API.

Provenance for each series, including its publisher and rights, travels inside
the fixture files under ``market_data_fixtures/`` and is echoed by
``action="help"``.
"""

from __future__ import annotations

import functools
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from openreward.environments import TextBlock, Toolset, ToolOutput, tool
from pydantic import BaseModel, Field

FIXTURE_DIR = Path(__file__).parent / "market_data_fixtures"

# The worlds whose task set actually calls for this data. Every other world in
# the benchmark declares no market-data application, and a call from one is
# refused rather than silently answered.
MARKET_DATA_WORLDS = {
    "world_5970ed13783a463181bdf38337f0cad1",  # Investment Banking World 246
    "world_43a921f91f0f4d2c85d8bd2774f9e681",  # Investment Banking World 244
}

TREASURY_ACTION = "treasury_rates"
INDICATOR_ACTION = "economic_indicators"
SUPPORTED_ACTIONS = ("help", TREASURY_ACTION, INDICATOR_ACTION)

# Indicator names accepted by economic_indicators, keyed by their lookup form.
SUPPORTED_INDICATORS = {"cpi": "CPI"}

MAX_ROWS = 400


class MarketDataError(ValueError):
    """A query the fixture cannot answer, carrying an agent-readable payload."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(payload.get("error", "market data unavailable"))
        self.payload = payload


@functools.lru_cache(maxsize=None)
def load_fixture(name: str) -> dict[str, Any]:
    """Read one fixture file. Cached, so the JSON is parsed once per process."""
    with open(FIXTURE_DIR / name) as handle:
        return json.load(handle)


def treasury_fixture() -> dict[str, Any]:
    return load_fixture("treasury_par_yield_curve.json")


def cpi_fixture() -> dict[str, Any]:
    return load_fixture("cpi_u.json")


def _parse_date(value: str, field: str) -> date:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except (AttributeError, ValueError):
        raise MarketDataError(
            {
                "error": f"Could not read {field!r} as a date.",
                "received": value,
                "expected_format": "YYYY-MM-DD",
            }
        )


def _coverage(fixture: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """The coverage block echoed on every response, so limits are always visible."""
    block = {
        "series": fixture.get("series"),
        "units": fixture.get("units"),
        "available_from": fixture["coverage"]["start"],
        "available_to": fixture["coverage"]["end"],
        "frequency": fixture["coverage"]["frequency"],
        "source": fixture["source"]["publisher"],
        "source_url": fixture["source"]["url"],
    }
    if extra:
        block.update(extra)
    return block


def _window(
    dates: list[str],
    on: str | None,
    start: str | None,
    end: str | None,
) -> tuple[str, str]:
    """Resolve the requested date arguments into an inclusive [start, end] pair."""
    if on:
        stamp = _parse_date(on, "date").isoformat()
        return stamp, stamp
    if start or end:
        low = _parse_date(start, "from_date").isoformat() if start else dates[0]
        high = _parse_date(end, "to_date").isoformat() if end else dates[-1]
        if low > high:
            raise MarketDataError(
                {
                    "error": "from_date is later than to_date.",
                    "from_date": low,
                    "to_date": high,
                }
            )
        return low, high
    # No date arguments: the most recent observation the fixture holds.
    return dates[-1], dates[-1]


def treasury_rates(
    on: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Daily Treasury par yield curve rates over a date or an inclusive range.

    Each row carries the maturities ``month1`` through ``year30`` in percent per
    annum. Dates the curve was not published on -- weekends, federal holidays,
    anything outside the fixture window -- resolve to an explicit miss.
    """
    fixture = treasury_fixture()
    rows: dict[str, dict[str, float]] = fixture["rows"]
    dates = list(rows)
    low, high = _window(dates, on, start, end)

    if high < dates[0] or low > dates[-1]:
        raise MarketDataError(
            {
                "error": "No Treasury yield curve data for the requested dates.",
                "requested": {"from_date": low, "to_date": high},
                "reason": "outside_coverage",
                "coverage": _coverage(fixture),
            }
        )

    hits = [d for d in dates if low <= d <= high]
    if not hits:
        # Inside the covered window but on a day the curve was not published.
        earlier = [d for d in dates if d < low]
        later = [d for d in dates if d > high]
        raise MarketDataError(
            {
                "error": "No Treasury yield curve observation on the requested date(s).",
                "requested": {"from_date": low, "to_date": high},
                "reason": "not_a_publication_date",
                "detail": (
                    "The Treasury publishes the par yield curve on business days only. "
                    "No value is carried forward or interpolated for other dates."
                ),
                "nearest_preceding_observation": earlier[-1] if earlier else None,
                "nearest_following_observation": later[0] if later else None,
                "coverage": _coverage(fixture),
            }
        )

    if len(hits) > MAX_ROWS:
        raise MarketDataError(
            {
                "error": f"Requested range covers {len(hits)} observations, above the {MAX_ROWS} row limit.",
                "requested": {"from_date": low, "to_date": high},
                "reason": "range_too_large",
                "detail": "Narrow from_date/to_date and query again.",
                "coverage": _coverage(fixture),
            }
        )

    return {
        "action": TREASURY_ACTION,
        "requested": {"from_date": low, "to_date": high},
        "count": len(hits),
        "rates": [dict(date=d, **rows[d]) for d in hits],
        "coverage": _coverage(
            fixture, {"maturities": fixture["coverage"]["maturities"]}
        ),
    }


def economic_indicator(
    indicator: str,
    on: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Levels of one macroeconomic series over a date or an inclusive range.

    Only CPI is carried. Observations are monthly and dated to the first day of
    the reference month. A month the publisher did not release is reported as
    unavailable, with the publisher's own note, and is never filled from a
    neighbouring month.
    """
    key = (indicator or "").strip().lower()
    if key not in SUPPORTED_INDICATORS:
        raise MarketDataError(
            {
                "error": f"Indicator {indicator!r} is not available in this environment.",
                "reason": "unsupported_indicator",
                "supported_indicators": sorted(SUPPORTED_INDICATORS.values()),
            }
        )

    fixture = cpi_fixture()
    observations: dict[str, float] = fixture["observations"]
    unavailable: dict[str, str] = fixture["unavailable"]
    dates = sorted(set(observations) | set(unavailable))
    low, high = _window(dates, on, start, end)

    if high < dates[0] or low > dates[-1]:
        raise MarketDataError(
            {
                "error": f"No {SUPPORTED_INDICATORS[key]} data for the requested dates.",
                "requested": {"from_date": low, "to_date": high},
                "reason": "outside_coverage",
                "coverage": _coverage(fixture),
            }
        )

    hits = [d for d in sorted(observations) if low <= d <= high]
    gaps = {d: note for d, note in sorted(unavailable.items()) if low <= d <= high}

    if not hits:
        if gaps:
            raise MarketDataError(
                {
                    "error": f"{SUPPORTED_INDICATORS[key]} was not published for the requested month(s).",
                    "requested": {"from_date": low, "to_date": high},
                    "reason": "not_published",
                    "unavailable": gaps,
                    "detail": (
                        "The publisher released no value for these months. No adjacent "
                        "month is substituted and no value is interpolated."
                    ),
                    "coverage": _coverage(fixture),
                }
            )
        raise MarketDataError(
            {
                "error": f"No {SUPPORTED_INDICATORS[key]} observation for the requested date(s).",
                "requested": {"from_date": low, "to_date": high},
                "reason": "not_an_observation_date",
                "detail": (
                    "Observations are monthly and dated to the first day of the "
                    "reference month, e.g. 2025-11-01 for November 2025."
                ),
                "coverage": _coverage(fixture),
            }
        )

    response = {
        "action": INDICATOR_ACTION,
        "indicator": SUPPORTED_INDICATORS[key],
        "series_id": fixture["series_id"],
        "requested": {"from_date": low, "to_date": high},
        "count": len(hits),
        "observations": [{"date": d, "value": observations[d]} for d in hits],
        "coverage": _coverage(fixture, {"description": fixture["description"]}),
    }
    if gaps:
        response["unavailable"] = gaps
    return response


def help_payload() -> dict[str, Any]:
    """Describe the actions this tool supports and exactly what each one covers."""
    treasury, cpi = treasury_fixture(), cpi_fixture()
    return {
        "tool_name": "fmp_market",
        "description": (
            "Frozen market and macroeconomic reference data. Values are served from a "
            "committed fixture, not a live feed, so repeated calls return identical results."
        ),
        "actions": {
            "help": {
                "description": "This message.",
                "required_params": [],
                "optional_params": [],
            },
            TREASURY_ACTION: {
                "description": treasury["description"],
                "required_params": [],
                "optional_params": ["date", "from_date", "to_date"],
                "returns": (
                    "Array with: date, "
                    + ", ".join(treasury["coverage"]["maturities"])
                ),
                "coverage": _coverage(
                    treasury,
                    {
                        "maturities": treasury["coverage"]["maturities"],
                        "observations": treasury["coverage"]["observations"],
                        "note": treasury["coverage"]["note"],
                    },
                ),
            },
            INDICATOR_ACTION: {
                "description": cpi["description"],
                "required_params": ["indicator"],
                "optional_params": ["date", "from_date", "to_date"],
                "returns": "Array with: date, value",
                "supported_indicators": sorted(SUPPORTED_INDICATORS.values()),
                "coverage": _coverage(
                    cpi,
                    {
                        "observations": cpi["coverage"]["observations"],
                        "date_convention": cpi["coverage"]["date_convention"],
                        "unavailable": cpi["unavailable"],
                    },
                ),
            },
        },
        "not_available": (
            "Share prices, company financial statements, filings and analyst data are not "
            "served by this tool. Company documents for this engagement are on the world "
            "filesystem."
        ),
        "rights": {
            treasury["series"]: treasury["source"]["rights"],
            cpi["series"]: cpi["source"]["rights"],
        },
    }


class MarketInput(BaseModel):
    """Input for the fmp_market tool."""

    action: Literal["help", "treasury_rates", "economic_indicators"] = Field(
        ...,
        description=(
            "Action to perform. 'treasury_rates' for the daily Treasury par yield curve, "
            "'economic_indicators' for a macroeconomic series, 'help' to list coverage."
        ),
    )
    indicator: str | None = Field(
        None,
        description="Indicator name. REQUIRED for economic_indicators. Currently: 'CPI'.",
    )
    date: str | None = Field(
        None,
        description=(
            "A single date (YYYY-MM-DD). For economic_indicators use the first day of the "
            "reference month, e.g. '2025-11-01' for November 2025."
        ),
    )
    from_date: str | None = Field(
        None, description="Start of an inclusive date range (YYYY-MM-DD)."
    )
    to_date: str | None = Field(
        None, description="End of an inclusive date range (YYYY-MM-DD)."
    )


def dispatch(params: MarketInput) -> dict[str, Any]:
    """Route one fmp_market call to its handler. Raises MarketDataError on a miss."""
    if params.action == "help":
        return help_payload()
    if params.action == TREASURY_ACTION:
        return treasury_rates(params.date, params.from_date, params.to_date)
    if params.action == INDICATOR_ACTION:
        if not params.indicator:
            raise MarketDataError(
                {
                    "error": "Missing required parameter: indicator",
                    "supported_indicators": sorted(SUPPORTED_INDICATORS.values()),
                }
            )
        return economic_indicator(
            params.indicator, params.date, params.from_date, params.to_date
        )
    raise MarketDataError(
        {
            "error": f"Unknown action {params.action!r}.",
            "supported_actions": list(SUPPORTED_ACTIONS),
        }
    )


def world_refusal(world_id: str) -> dict[str, Any]:
    """Payload returned when a world outside the two IB engagements calls the tool."""
    return {
        "error": "Market data is not part of this world's toolset.",
        "reason": "world_not_provisioned",
        "world_id": world_id,
        "detail": (
            "Only the Investment Banking engagements that require published rate and "
            "index data have this application. Work from the files on the world filesystem."
        ),
    }


class MarketDataToolset(Toolset):
    """Serves the frozen market-data fixture to the Investment Banking worlds."""

    @tool
    async def fmp_market(self, params: MarketInput) -> ToolOutput:
        """
        Market and macroeconomic reference data: US Treasury par yield curve rates
        (action='treasury_rates') and macroeconomic index levels such as CPI
        (action='economic_indicators', indicator='CPI').

        Values are historical and fixed; identical arguments always return identical
        results. Call with action='help' for the exact date coverage, the maturities
        carried, the indicators supported and the source of each series.

        Only dates the publisher actually released are returned. A weekend, a federal
        holiday, a month the publisher skipped or a date outside the covered window
        reports the miss; nothing is interpolated or carried forward.
        """
        world_id = getattr(getattr(self.env, "validated", None), "world_id", "")
        if world_id not in MARKET_DATA_WORLDS:
            payload = world_refusal(world_id)
            return ToolOutput(
                blocks=[TextBlock(text=json.dumps(payload, indent=2))],
                metadata=payload,
                reward=0.0,
                finished=False,
            )

        try:
            payload = dispatch(params)
        except MarketDataError as miss:
            return ToolOutput(
                blocks=[TextBlock(text=json.dumps(miss.payload, indent=2))],
                metadata=miss.payload,
                reward=0.0,
                finished=False,
            )

        return ToolOutput(
            blocks=[TextBlock(text=json.dumps(payload, indent=2))],
            metadata={"action": params.action, "count": payload.get("count")},
            reward=0.0,
            finished=False,
        )
