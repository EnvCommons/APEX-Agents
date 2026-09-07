"""Tests for the frozen market-data fixture and the fmp_market tool.

No network access and no sandbox: every assertion reads the committed fixture.

The reconciliation tests at the end rebuild the DCF from
``4. Analysis/DCF/Project Band-Aid - DCF Model - vF.xlsx`` (World 246) using the
fixture's own rates, and check that the result equals the figure the task's rubric
asserts. They are the evidence that the rates carried here are the rates the tasks
were written against.
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

import market_data as md
from market_data import MarketDataError, MarketDataToolset, MarketInput

IB_WORLD_246 = "world_5970ed13783a463181bdf38337f0cad1"
IB_WORLD_244 = "world_43a921f91f0f4d2c85d8bd2774f9e681"
LAW_WORLD = "world_9797d81fa71c4dbfb192e89a0f2ac811"

# Every Treasury point the two Investment Banking worlds' prompts name, as
# (date, maturity key, expected percent, task).
REQUIRED_TREASURY_POINTS = [
    ("2026-01-02", "year10", 4.19, "World246_RL_01"),
    ("2026-01-02", "year30", 4.86, "World246_RL_01 / World246_RL_06"),
    ("2026-01-05", "year5", 3.71, "World_246_IL_01"),
    ("2026-01-05", "year10", 4.17, "World_246_IL_01"),
    ("2025-12-15", "year5", 3.73, "World246_ML_01 / World246_SM_01"),
    ("2025-12-22", "year10", 4.17, "World246_RL_09"),
    ("2025-12-22", "year20", 4.78, "World246_RL_09"),
    ("2025-12-22", "year1", 3.53, "World244_RL_01"),
    ("2025-12-22", "year5", 3.71, "World244_RL_01"),
    ("2025-12-12", "year10", 4.19, "World246_AY01"),
    ("2025-12-12", "year5", 3.75, "World244_RL_05"),
    ("2025-12-12", "year7", 3.95, "World244_RL_05"),
    ("2025-11-28", "year7", 3.78, "World244_JP_01"),
    ("2025-11-28", "year10", 4.02, "World244_JP_01"),
    ("2025-10-20", "year20", 4.56, "World244_AS_Task03"),
]

# Every CPI-U point the prompts name.
REQUIRED_CPI_POINTS = [
    ("2025-01-01", 317.671, "World246_RL_07"),
    ("2025-11-01", 324.122, "World246_RL_07"),
]


# ---------------------------------------------------------------------------
# Fixture integrity
# ---------------------------------------------------------------------------


def test_fixtures_parse_and_carry_provenance():
    for fixture in (md.treasury_fixture(), md.cpi_fixture()):
        assert fixture["description"]
        assert fixture["units"]
        assert fixture["source"]["publisher"]
        assert fixture["source"]["url"].startswith("https://")
        assert "public domain" in fixture["source"]["rights"].lower()
        assert fixture["coverage"]["start"] <= fixture["coverage"]["end"]


def test_treasury_rows_are_well_formed():
    fixture = md.treasury_fixture()
    maturities = set(fixture["coverage"]["maturities"])
    dates = list(fixture["rows"])
    assert dates == sorted(dates), "rows must be in date order"
    assert len(dates) == fixture["coverage"]["observations"]
    for stamp, row in fixture["rows"].items():
        assert set(row) <= maturities
        assert all(isinstance(v, float) for v in row.values())
        # Every maturity the tasks read must be present on every covered day.
        assert {"year1", "year5", "year7", "year10", "year20", "year30"} <= set(row), stamp


def test_cpi_observations_and_gaps_do_not_overlap():
    fixture = md.cpi_fixture()
    assert not set(fixture["observations"]) & set(fixture["unavailable"])
    assert all(stamp.endswith("-01") for stamp in fixture["observations"])


# ---------------------------------------------------------------------------
# Every data point in the spec resolves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stamp,maturity,expected,task", REQUIRED_TREASURY_POINTS)
def test_required_treasury_point_resolves(stamp, maturity, expected, task):
    result = md.treasury_rates(stamp)
    assert result["count"] == 1
    row = result["rates"][0]
    assert row["date"] == stamp
    assert row[maturity] == expected, f"{task} reads {maturity} on {stamp}"


@pytest.mark.parametrize("stamp,expected,task", REQUIRED_CPI_POINTS)
def test_required_cpi_point_resolves(stamp, expected, task):
    result = md.economic_indicator("CPI", stamp)
    assert result["observations"] == [{"date": stamp, "value": expected}], task


def test_ten_year_series_over_the_os_04_window():
    """World244_OS_04 averages the 10-year yield over 2025-12-02..2025-12-19."""
    result = md.treasury_rates(start="2025-12-02", end="2025-12-19")
    yields = [row["year10"] for row in result["rates"]]
    assert result["count"] == 14
    assert round(sum(yields) / len(yields), 5) == 4.14143


def test_indicator_lookup_is_case_insensitive():
    assert md.economic_indicator("cpi", "2025-11-01")["indicator"] == "CPI"
    assert md.economic_indicator("CPI", "2025-11-01")["indicator"] == "CPI"


# ---------------------------------------------------------------------------
# Misses are clean: no interpolation, no neighbouring value
# ---------------------------------------------------------------------------


def _maturity_values(payload):
    """Every numeric maturity reading anywhere in a payload."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key.startswith(("year", "month")) and isinstance(value, (int, float)):
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found


@pytest.mark.parametrize(
    "stamp,why",
    [
        ("2026-01-03", "Saturday"),
        ("2026-01-04", "Sunday"),
        ("2025-12-25", "Christmas Day, a federal holiday"),
        ("2025-11-27", "Thanksgiving Day, a federal holiday"),
    ],
)
def test_non_publication_date_reports_a_miss(stamp, why):
    with pytest.raises(MarketDataError) as excinfo:
        md.treasury_rates(stamp)
    payload = excinfo.value.payload
    assert payload["reason"] == "not_a_publication_date", why
    assert not _maturity_values(payload), "a miss must not carry a rate"
    # The neighbours are named for navigation but their values are not returned.
    assert payload["nearest_preceding_observation"] < stamp
    assert payload["nearest_following_observation"] > stamp


@pytest.mark.parametrize("stamp", ["2019-06-03", "2022-12-30", "2026-02-02", "2030-01-02"])
def test_treasury_outside_coverage_reports_a_miss(stamp):
    with pytest.raises(MarketDataError) as excinfo:
        md.treasury_rates(stamp)
    payload = excinfo.value.payload
    assert payload["reason"] == "outside_coverage"
    assert not _maturity_values(payload)
    assert payload["coverage"]["available_from"] and payload["coverage"]["available_to"]


def test_october_2025_cpi_is_reported_unpublished_not_substituted():
    """BLS released no October 2025 CPI; the tool must say so, not answer with a neighbour."""
    with pytest.raises(MarketDataError) as excinfo:
        md.economic_indicator("CPI", "2025-10-01")
    payload = excinfo.value.payload
    assert payload["reason"] == "not_published"
    assert "lapse in appropriations" in payload["unavailable"]["2025-10-01"]
    september = md.cpi_fixture()["observations"]["2025-09-01"]
    november = md.cpi_fixture()["observations"]["2025-11-01"]
    text = json.dumps(payload)
    assert str(september) not in text and str(november) not in text


def test_range_spanning_the_cpi_gap_flags_it():
    result = md.economic_indicator("CPI", start="2025-09-01", end="2025-11-01")
    assert [o["date"] for o in result["observations"]] == ["2025-09-01", "2025-11-01"]
    assert "2025-10-01" in result["unavailable"]


def test_cpi_outside_coverage_reports_a_miss():
    with pytest.raises(MarketDataError) as excinfo:
        md.economic_indicator("CPI", "2026-06-01")
    assert excinfo.value.payload["reason"] == "outside_coverage"


def test_mid_month_cpi_date_reports_a_miss():
    with pytest.raises(MarketDataError) as excinfo:
        md.economic_indicator("CPI", "2025-11-15")
    payload = excinfo.value.payload
    assert payload["reason"] == "not_an_observation_date"
    assert "first day of the reference month" in payload["detail"]


def test_unsupported_indicator_names_what_is_supported():
    with pytest.raises(MarketDataError) as excinfo:
        md.economic_indicator("GDP", "2025-11-01")
    payload = excinfo.value.payload
    assert payload["reason"] == "unsupported_indicator"
    assert payload["supported_indicators"] == ["CPI"]


def test_bad_date_format_is_rejected():
    for bad in ("12/15/2025", "Dec 15 2025", "2025-13-01"):
        with pytest.raises(MarketDataError) as excinfo:
            md.treasury_rates(bad)
        assert excinfo.value.payload["expected_format"] == "YYYY-MM-DD"


def test_inverted_range_is_rejected():
    with pytest.raises(MarketDataError) as excinfo:
        md.treasury_rates(start="2025-12-19", end="2025-12-02")
    assert "later than" in excinfo.value.payload["error"]


def test_oversized_range_is_refused_rather_than_truncated():
    with pytest.raises(MarketDataError) as excinfo:
        md.treasury_rates(start="2023-01-01", end="2026-01-31")
    assert excinfo.value.payload["reason"] == "range_too_large"


# ---------------------------------------------------------------------------
# Discoverability
# ---------------------------------------------------------------------------


def test_help_declares_coverage_and_limits():
    payload = md.help_payload()
    assert payload["tool_name"] == "fmp_market"
    assert set(payload["actions"]) == {"help", "treasury_rates", "economic_indicators"}
    treasury = payload["actions"]["treasury_rates"]["coverage"]
    assert treasury["available_from"] == md.treasury_fixture()["coverage"]["start"]
    assert treasury["available_to"] == md.treasury_fixture()["coverage"]["end"]
    assert "year10" in payload["actions"]["treasury_rates"]["coverage"]["maturities"]
    cpi = payload["actions"]["economic_indicators"]
    assert cpi["supported_indicators"] == ["CPI"]
    assert "2025-10-01" in cpi["coverage"]["unavailable"]
    assert "Share prices" in payload["not_available"]


def test_no_date_argument_returns_the_latest_observation():
    latest = md.treasury_fixture()["coverage"]["end"]
    assert md.treasury_rates()["rates"][0]["date"] == latest


# ---------------------------------------------------------------------------
# Tool registration and world gating
# ---------------------------------------------------------------------------


def test_tool_is_registered_on_the_environment_class():
    from apexagents import ApexAgents

    listed = {spec.name: spec for spec in ApexAgents.list_tools().tools}
    assert "fmp_market" in listed
    schema = listed["fmp_market"].input_schema
    assert set(schema["properties"]["action"]["enum"]) == {
        "help",
        "treasury_rates",
        "economic_indicators",
    }
    assert "treasury" in listed["fmp_market"].description.lower()


def _toolset_for(world_id):
    env = types.SimpleNamespace(
        sandbox=object(),
        validated=types.SimpleNamespace(world_id=world_id),
    )
    return MarketDataToolset(env)


def _call(world_id, **kwargs):
    toolset = _toolset_for(world_id)
    output = asyncio.run(toolset.fmp_market(MarketInput(**kwargs)))
    return output, json.loads(output.blocks[0].text)


@pytest.mark.parametrize("world_id", [IB_WORLD_246, IB_WORLD_244])
def test_investment_banking_worlds_are_served(world_id):
    output, payload = _call(world_id, action="treasury_rates", date="2026-01-02")
    assert payload["rates"][0]["year30"] == 4.86
    assert output.finished is False and output.reward == 0.0


def test_other_worlds_are_refused_with_a_reason():
    _, payload = _call(LAW_WORLD, action="treasury_rates", date="2026-01-02")
    assert payload["reason"] == "world_not_provisioned"
    assert not _maturity_values(payload)


def test_tool_returns_a_miss_as_a_normal_result_not_an_exception():
    output, payload = _call(IB_WORLD_246, action="treasury_rates", date="2026-01-03")
    assert output.finished is False
    assert payload["reason"] == "not_a_publication_date"


def test_economic_indicators_requires_an_indicator():
    _, payload = _call(IB_WORLD_246, action="economic_indicators", date="2025-11-01")
    assert payload["error"] == "Missing required parameter: indicator"


# ---------------------------------------------------------------------------
# Gold reconciliation: the rates in the fixture reproduce the rubric's figures
# ---------------------------------------------------------------------------

# Constants read from World 246's "Project Band-Aid - DCF Model - vF.xlsx".
KVUE_SALES = {
    2025: 15715.8474,
    2026: 15854.06918,
    2027: 16057.57932,
    2028: 16231.25423,
    2029: 16423.20683,
}
KVUE_OPERATING_INCOME = {
    2025: 2413.95416,
    2026: 2300.42544,
    2027: 2236.8208,
    2028: 2369.76312,
    2029: 2356.73018,
}
KVUE_CAPEX = {
    2025: 426.0,
    2026: 443.0,
    2027: 434.33333,
    2028: 434.44444,
    2029: 437.25926,
}
KVUE_UFCF = [2097.80242, 2026.5786, 1992.15606, 2104.81365, 2099.28245]
KVUE_DA_RATIO = 649.17497 / KVUE_SALES[2025]
KVUE_OCA_RATIO = 4751.30282 / KVUE_SALES[2025]
KVUE_OCL_RATIO = 4450.90648 / KVUE_SALES[2025]
KVUE_NWC_2024 = 4455 - 4187
KVUE_TAX = 0.21
KVUE_ERP = 0.0583
KVUE_SHARE_PRICE = 16.92
KVUE_SHARES = 1911.24072
KVUE_DEBT = 8607.0
KVUE_EQUITY_WEIGHT = (KVUE_SHARE_PRICE * KVUE_SHARES) / (
    KVUE_SHARE_PRICE * KVUE_SHARES + KVUE_DEBT
)
# WACC Build sheet: risk-free rate, beta and the weighted-average coupon the
# model uses as its cost of debt.
KVUE_BASE_RISK_FREE = 0.0406
KVUE_BASE_BETA = 0.67775
KVUE_BASE_COST_OF_DEBT = 0.05111


def _wacc(risk_free, beta, cost_of_debt):
    cost_of_equity = risk_free + beta * KVUE_ERP
    return KVUE_EQUITY_WEIGHT * cost_of_equity + (1 - KVUE_EQUITY_WEIGHT) * cost_of_debt * (
        1 - KVUE_TAX
    )


KVUE_BASE_WACC = _wacc(KVUE_BASE_RISK_FREE, KVUE_BASE_BETA, KVUE_BASE_COST_OF_DEBT)


def _unlevered_fcf(sales):
    previous_nwc, flows = KVUE_NWC_2024, []
    for year in (2025, 2026, 2027, 2028, 2029):
        revenue = sales[year]
        margin = KVUE_OPERATING_INCOME[year] / KVUE_SALES[year]
        nwc = revenue * (KVUE_OCA_RATIO - KVUE_OCL_RATIO)
        flows.append(
            revenue * margin * (1 - KVUE_TAX)
            + revenue * KVUE_DA_RATIO
            - (nwc - previous_nwc)
            - KVUE_CAPEX[year]
        )
        previous_nwc = nwc
    return flows


def _enterprise_value(flows, wacc, terminal_growth):
    terminal = flows[-1] * (1 + terminal_growth) / (wacc - terminal_growth)
    discounted = sum(f / (1 + wacc) ** (n + 1) for n, f in enumerate(flows))
    return discounted + terminal / (1 + wacc) ** len(flows)


def test_dcf_reconstruction_matches_the_shipped_model():
    """The reconstruction is faithful before any rate is substituted into it."""
    assert _unlevered_fcf(KVUE_SALES) == pytest.approx(KVUE_UFCF, abs=1e-4)
    assert round(KVUE_BASE_WACC, 5) == 0.07176


@pytest.mark.parametrize(
    "maturity,beta,expected_ev",
    [
        ("year10", 0.75, 37399),
        ("year10", 1.00, 30587),
        ("year30", 0.75, 33274),
        ("year30", 1.00, 27778),
    ],
)
def test_world246_rl_01_enterprise_values(maturity, beta, expected_ev):
    """World246_RL_01: risk-free rate becomes the 10y/30y CMT on 2026-01-02.

    Cost of debt is that rate plus 100bp; the rubric asserts each enterprise value
    to the nearest million.
    """
    rate = md.treasury_rates("2026-01-02")["rates"][0][maturity] / 100
    wacc = _wacc(rate, beta, rate + 0.01)
    value = _enterprise_value(_unlevered_fcf(KVUE_SALES), wacc, 0.025)
    assert round(value) == expected_ev


def test_world246_rl_06_terminal_value():
    """World246_RL_06: terminal growth becomes the 30y CMT on 2026-01-02 less 100bp.

    2029E net sales growth is reset to the 2023A actual. The rubric asserts a
    terminal value of $67,213 million.
    """
    rate = md.treasury_rates("2026-01-02")["rates"][0]["year30"] / 100
    terminal_growth = rate - 0.01
    growth_2023a = 15444 / 14950 - 1
    sales = dict(KVUE_SALES)
    sales[2029] = sales[2028] * (1 + growth_2023a)
    flows = _unlevered_fcf(sales)
    terminal = flows[-1] * (1 + terminal_growth) / (KVUE_BASE_WACC - terminal_growth)
    assert round(terminal) == 67213


def test_world246_rl_07_cpi_growth_rate():
    """World246_RL_07 sets terminal growth to the CPI increase Jan-2025 -> Nov-2025."""
    series = md.economic_indicator("CPI", start="2025-01-01", end="2025-11-01")
    levels = {o["date"]: o["value"] for o in series["observations"]}
    growth = levels["2025-11-01"] / levels["2025-01-01"] - 1
    assert round(growth * 100, 4) == 2.0307
