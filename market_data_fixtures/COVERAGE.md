# Market data: what this environment provides

Two of the environment's 33 worlds — **Investment Banking World 246**
(`world_5970ed13783a463181bdf38337f0cad1`, 19 tasks) and **Investment Banking
World 244** (`world_43a921f91f0f4d2c85d8bd2774f9e681`, 9 tasks) — set valuation
tasks whose rubrics assert an exact figure derived from published market or
macroeconomic data on a named date. Those figures cannot live in a world
filesystem, so the environment serves the ones it can from a frozen fixture and
declines the ones it cannot.

This page is the boundary: what is served, what is not, and why the line sits
where it does.

## Covered

| Series | Publisher | Rights |
|---|---|---|
| Daily Treasury Par Yield Curve Rates (constant maturity, `month1`–`year30`) | U.S. Department of the Treasury | Work of the U.S. federal government; public domain |
| CPI-U, U.S. city average, all items, NSA (`CUUR0000SA0`) | U.S. Bureau of Labor Statistics | Work of the U.S. federal government; public domain |

Both are published by U.S. federal agencies and are in the public domain, which
is what makes them redistributable as a committed fixture. They are read through
the `fmp_market` tool; `action="help"` reports the exact date windows, the
maturities carried, the indicators supported and the source of each series.
Provenance also travels inside `treasury_par_yield_curve.json` and `cpi_u.json`
themselves.

These two series satisfy the rate and index requirements of **13 of the 28 tasks**
in the two worlds. **10 of those need nothing else external:**

| Task | Reads |
|---|---|
| `World246_RL_01` | 10Y and 30Y CMT, 2026-01-02 |
| `World246_RL_06` | 30Y CMT, 2026-01-02 |
| `World246_SM_01` | 5Y CMT, 2025-12-15 |
| `World246_RL_09` | 10Y and 20Y CMT, 2025-12-22 |
| `World246_AY01` | 10Y CMT, 2025-12-12 |
| `World246_RL_07` | CPI-U, 2025-01 and 2025-11 |
| `World244_RL_01` | 1Y and 5Y CMT, 2025-12-22 |
| `World244_RL_05` | 5Y and 7Y CMT, 2025-12-12 |
| `World244_JP_01` | 7Y and 10Y CMT, 2025-11-28 |
| `World244_OS_04` | 10Y CMT daily, 2025-12-02 – 2025-12-19 |

A further **5 tasks** need no external data at all, being fully served by the
documents on their world filesystem: `WORLD246_HL_01`, `World246_RL_08`,
`World246_RL_04`, `World244_OS_Task06`, `World244_SK_Task04`.

### Gaps inside the covered window

Coverage is by observation, not by calendar. The Treasury publishes the curve on
business days only, and a weekend, a federal holiday or a date beyond the fixture
window returns an explicit miss naming the coverage bounds. Nothing is carried
forward or interpolated.

The Bureau of Labor Statistics published **no October 2025 CPI**; its own footnote
reads *"Data unavailable due to the 2025 lapse in appropriations"*. That month is
recorded as unavailable with the footnote attached. A query for it reports the
gap, and a range spanning it returns the months that exist while flagging the one
that does not. September and November are never substituted for it.

## Not covered

The remaining tasks turn on data this environment does not carry: daily equity
prices, non-U.S. and post-FY2023 company fundamentals, and vendor-computed market
aggregates. The upstream reference implementation serves all of these from
Financial Modeling Prep, a commercial data vendor requiring a licensed API key.
No such key ships here, and no substitute source is used: an approximation drawn
from elsewhere would reproduce some figures and quietly miss others, which is
worse than a stated gap, because a wrong number fails a rubric criterion without
ever looking like a failure.

### Requiring daily equity close and volume

| Task | Needs |
|---|---|
| `World246_JP_01` | KVUE daily close **and volume**, 2024-12-09 – 2025-12-08 (52-week high/low, 30- and 90-trading-day VWAP) |
| `WORLD246_HL_02` | KMB close on 2025-10-31 and 2025-12-16; KVUE close on 2025-12-16 |
| `WORLD246_ES_02` | KVUE daily closes, 2025-01-01 – 2025-06-30 |
| `World246_RL_02` | KVUE closes, 2025-12-15 – 2025-12-19 |
| `World246_AS_01` | PBH close on 2025-12-17 |
| `World246_ML_01` | KVUE close on 2025-12-15 (its 5Y CMT input **is** covered) |
| `World_246_IL_01` | KVUE close on 2026-01-05 (its 5Y and 10Y CMT inputs **are** covered) |

### Requiring company fundamentals absent from the world filesystem

The worlds ship an extensive filing corpus — SEC 10-K and 10-Q documents for the
U.S. peers in World 246 and for the World 244 comparables through FY2023. These
tasks reach outside it:

| Task | Needs | Why the world cannot serve it |
|---|---|---|
| `World 246_MM_04` | Haleon (HLN) total debt and market capitalisation at FY2024 year end | Haleon is a foreign private issuer; no Haleon documents in the world |
| `World246_RL_10` | KVUE FY2019 net sales and operating income | Pre-IPO carve-out period; the world's KVUE filings begin at FY2021 |
| `World244_SK_Task08` | DUOL, COUR and LOPE FY2023 and FY2024 revenue | The comparables corpus stops at FY2023, and Duolingo appears in no folder |
| `World244_AS_Task03` | Stride (LRN) FY2024 gross margin and SG&A as % of revenue (its 20Y CMT input **is** covered) | The Stride corpus stops at FY2023 |
| `World 246_MM_03` | EV and FCF at 2025-12-20 for ten comparables | See below |

### `World 246_MM_03` — out of scope

The task computes a cleaned average EV/FCF across the ten comparables in the
discussion deck. Four of them — Unilever, GSK, Reckitt Benckiser and Beiersdorf —
are foreign filers with no documents anywhere in the world and no SEC EDGAR
coverage of the statements required, and all ten need an enterprise value struck
on a specific date. Satisfying this one task means sourcing a full cross-border
comparables set from a licensed vendor. It is not supported.

### `World244_OS_Task03` — a task-data defect

This task is not merely uncovered; it cannot be graded deterministically even
with a vendor feed, and should be treated as defective task data.

The prompt asks how much KSchool's 2023 earnings must change for its P/E — taken
against the DCF's implied share price of $20.891 — to equal "the sector average
for communication services as of 1/1/2026". The rubric asserts an increase of
$147 million. Working backwards from that figure, the sector P/E the task was
written against depends on which earnings line is used as the denominator, and
the prompt does not say:

- against **net loss available to common stockholders** (−$31,137K): sector P/E ≈ **38.7x**
- against **total net loss** (−$39,072K): sector P/E ≈ **41.6x**

Both readings are defensible from the income statement in `DCF_vF.xlsx`, and they
imply different sector multiples. The expected answer therefore rests on an
unstated choice, so no data source — licensed or otherwise — makes this task
reliably gradable. Fixing it requires amending the task, not adding data.

## `World246_RL_07` — a note on the residual

The CPI series is verified: `World246_RL_07` sets the DCF's long-term growth rate
to the CPI-U increase from 2025-01 to 2025-11, which is 317.671 → 324.122, or
**+2.0307%**, and the fixture reproduces both index levels exactly.

An independent reconstruction of the rest of that task's model nonetheless lands
at an implied share price of $13.14 against the rubric's $12.75, which implies a
long-term growth rate nearer 1.86%. The difference lies in how the task's other
instructions — a 60 basis point increase to WACC, and the treatment of operating
margin across the forecast period — are applied to the model, not in the CPI
figures. The data here is sound; the ambiguity is elsewhere in the task.

## Determinism

Values are served from the committed fixture. No network call is made while a
task runs, and identical arguments always return identical results.
`build_fixture.py` regenerates the two data files from Treasury.gov and the BLS
public API; it is a maintenance script and is never executed by the environment.
