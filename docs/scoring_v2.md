# Company assessment v2

The Company Research page and `GET /api/analytics/factors/{ticker}?as_of=YYYY-MM-DD` use
one published warehouse epoch. The assessment uses the configured equity research
universe. Where a dated membership snapshot exists it restricts the configured seed
to members in the most recent snapshot at or before the cutoff. Earlier assessments
carry a survivorship-bias flag. The legacy five-factor rankings remain available as a
separate baseline.

## Calculation and coverage

Each evidence slot selects one metric from its documented, ordered options. Each
metric is compared with the same metric among eligible companies of the same
financial/nonfinancial business-model class. At least eight finite observations are
required. A missing primary metric can use the specified alternative within its slot.
This does not transfer any weight to another group. Required slots, available and
missing inputs, sample sizes and coverage are exposed. A percentile is
100 times (count below + 0.5 times count tied) / sample size. A lower-is-better
metric uses 100 minus that percentile. Ranks bound the influence of outlier
magnitudes without clipping raw financial data.

| Dimension | Equal-weight groups and their required evidence |
| --- | --- |
| Fundamentals | Valuation: positive annual earnings yield. Business quality: ROA or net margin. Growth: annual revenue growth. Cash flow quality: annual FCF margin, for nonfinancial issuers. |
| Technical | Trend: close/SMA200 minus 1. Relative momentum: 252-session price return less SPY's matched price return. |
| Resilience | Market risk: lower 252-return realized volatility and smaller 253-close maximum drawdown. Financial risk: lower verified debt/assets, lower separately labeled reported debt categories/assets, or higher interest coverage, plus current ratio or cash/debt. For financial issuers use the book equity/assets proxy, which is distinct from regulatory capital. |

A group averages its fixed slots. A dimension averages its fixed applicable
groups. With defaults, the opportunity score is
`0.60 × fundamental + 0.25 × technical + 0.15 × resilience`.
Set `FINLAB_ASSESSMENT_WEIGHTS='{"fundamental":0.6,"technical":0.25,"risk":0.15}'`
to explicitly configure the weights. The weights are unvalidated. An eligible
security needs all positively weighted dimensions and a contiguous 253-close
history on the supported Yahoo price basis. Relative momentum and beta require
matching SPY dates. Higher resilience consistently means comparatively favorable
risk. Beta, downside deviation, positive-loss VaR/CVaR and relative volume are
displayed as market diagnostics. Net debt/EBITDA, debt/equity and earnings
variability are financial diagnostics. Correlated indicators do not get extra
weight when they are displayed as diagnostics. The API reports pairwise
Spearman correlations at absolute correlation at least 0.75, for pairs with
eight or more comparable observations.

The API exposes each scored slot's direction, raw value, cohort median, percentile,
and weight. A contribution is `(metric score - 50) × final effective weight`.
When the opportunity score is available the contributions plus 50 reconstruct it.
The three largest positive and negative effects use these actual contributions.

## Financial conventions

- The historical API key `trailing_pe` reports market capitalization divided by
  positive net income of the latest published fiscal year. The page labels it
  as a fiscal-year multiple. Annual earnings yield uses the same denominator
  basis. Explicit TTM P/E and yield use four contiguous, individually reported
  quarters with a consistent income tag and a roughly year-long total period.
  Missing quarters remain unavailable.
- Annual flows must have the same start and end as the corresponding revenue
  fiscal year. The latest fiscal-year period is limited to 550 days old. YoY
  growth requires consecutive fiscal years within the documented alignment
  window and a positive prior denominator. ROA and ROE use average beginning
  and ending assets or parent equity respectively. ROIC uses operating income
  times one minus a supported reported effective tax rate, divided by average
  verified debt plus parent equity plus noncontrolling interest less cash.
- Annual FCF is operating cash flow less positively signed reported capex. Price/FCF
  requires positive FCF. FCF yield and margin retain negative FCF. EBITDA is
  operating income plus the combined reported depreciation and amortization tag.
- EV equals report-based market cap plus verified debt, preferred equity and
  noncontrolling interest less specifically tagged unrestricted cash and cash
  equivalents. All inputs are required. An EV multiple requires positive EV and
  a positive denominator. Net debt/EBITDA and cash/debt use the same strict cash
  definition and verified total debt. No unreported claim is treated as zero.
- The separate reported debt categories/assets metric requires all three
  nonoverlapping debt categories (current term debt, noncurrent term debt and
  commercial paper) in one filing. The metric is explicitly narrower than total
  debt. Current ratio uses reported current assets/current liabilities. Interest
  coverage uses operating income/interest expense. Industrial leverage, EV and
  cash flow ratios are excluded for financial issuers identified by available
  sector or SIC metadata. Those classifications are current metadata.
- Reported-share market cap pairs the last covered Yahoo close with public
  reported common shares issued for a measurement date at most 180 days old.
  Both inputs are aligned to the same split basis and the close must be within
  seven calendar days. Multiple listed tickers under the same filer invalidate
  the simple per-share valuation approach. Historical price and publication
  dates are never replaced with current prices or shares. Companyfacts filing
  dates determine filing availability at end-of-day resolution.
- Historical comparison uses 20 cutoffs spaced 91 calendar days apart, strictly
  before the requested date. At least eight compatible observations are needed.
  The UI includes the dated values, median and raw percentile. Historical sector
  classifications are absent from warehouse snapshots. No current-sector median
  or percentile is displayed as historical evidence.

## Forward validation

Run from the repository root against the local published warehouse.

```sh
env PYTHONPATH=src .venv/bin/python -m finlab.analytics.cli validate-scores 2020-01-02 2022-12-30
```

Use `--epoch EPOCH` to replay against the same published data. The report stores
source and runtime fingerprints, dimension and baseline weights, and universe IDs.
Each run is registered in the shared local research ledger before simulation.

The runner compares the fixed legacy five-factor, opportunity, fundamental-only
and technical-only ranks on the intersection of scoreable securities known at the
signal date. It records next-open to exit-close adjusted returns for 21, 63, 126 and
252 sessions, Spearman IC, quintiles, gross top-minus-bottom spreads, matched SPY
returns, fees and turnover. It evaluates successive nonoverlapping windows for
each horizon without fitting weights. It retains unresolved holdings in the
coverage figures and suppresses portfolio performance if their outcomes cannot
be observed. Daily marked values produce risk statistics only across contiguous,
fully valued folds. The long-short spread excludes borrow and financing and is
reported as a descriptive spread. These experiments use retrospective data
revisions and an incompletely captured universe. A forward-period evaluation of
fixed rules does not establish prospective superiority.

## Historical checks on the local warehouse

The offline API test uses the publication
`20261122T143857959526Z-848451fc2e9a4d5caa6106a81a244a44`.

| Security and date | Fundamentals | Technical | Resilience | Opportunity |
| --- | ---: | ---: | ---: | ---: |
| AAPL, 2020-12-31 | 65.63 | 88.61 | 60.31 | 70.57 |
| TSLA, 2020-07-31 | Unavailable | Scoring evidence varies by dimension | 14.00 | Unavailable |

For AAPL, the 2020-09-26 annual period was filed on 2020-10-30. The filing
supports current assets of $143.713 billion, current liabilities of $105.392
billion, and interest expense of $2.873 billion. The resulting 1.3636× current
ratio and 23.0727× interest coverage fill the financial risk slots. AAPL's
reported current and noncurrent term debt and commercial paper are available
as a narrower 34.71% of assets metric. The verified combined debt input and
several related leverage metrics remain unavailable. The reported-debt cohort
was below the eight-observation threshold for scoring, so the fixed coverage
rule selected interest coverage. The AAPL report is stored in
`data/reports/research/assessment-v2-AAPL-2020-12-31.json`. The TSLA assessment
withheld fiscal-year P/E because published earnings were negative.

To reproduce the assessment with the same current publication and no
external connection, run from the repository root:

```sh
env PYTHONPATH=src .venv/bin/python -c 'from datetime import date; from finlab.analytics.company import company_factors; print(company_factors("AAPL", date(2020, 12, 31))["assessment"])'
```

The completed sparse trial used one signal every 252 sessions over 2019-01-02
through 2023-12-29, 10 bps per trade side, and the pinned epoch above. It is
stored at `data/reports/research/score-validation-v2-2019-2023-sparse.json`.

| Horizon | Windows | Legacy rank IC | Opportunity rank IC | Fundamentals rank IC | Technical rank IC |
| --- | ---: | ---: | ---: | ---: | ---: |
| 21 sessions | 5 | -0.073 | -0.142 | 0.038 | -0.283 |
| 63 sessions | 5 | -0.044 | -0.008 | 0.112 | -0.141 |
| 126 sessions | 5 | -0.012 | 0.044 | 0.097 | -0.060 |
| 252 sessions | 4 | 0.009 | 0.182 | 0.101 | 0.097 |

The four contiguous 252-session windows had 52–61 common scoreable names per
signal. Net top-quintile Sharpe was 0.67 for legacy, 0.78 for opportunity,
0.84 for fundamental-only and 0.79 for technical-only, versus 0.66 for
the cost-matched SPY comparison. Opportunity top-quintile daily drawdown
was -34.45% versus -33.72% for SPY. The shorter sparse windows are separated by
gaps, so the report leaves their compounded portfolio metrics unavailable.
These results show mixed performance over a small, survivorship-biased sample.

The denser 2020-01-02 through 2022-12-30 trial uses the same epoch and costs,
with decision dates every 21 sessions. Results are stored at
`data/reports/research/score-validation-v2-2020-2022-monthly.json`.

| Horizon | Windows | Legacy rank IC | Opportunity rank IC | Fundamentals rank IC | Technical rank IC |
| --- | ---: | ---: | ---: | ---: | ---: |
| 21 sessions | 35 | -0.014 | 0.024 | 0.032 | 0.003 |
| 63 sessions | 11 | -0.061 | -0.013 | 0.021 | -0.046 |
| 126 sessions | 5 | -0.043 | 0.051 | 0.050 | 0.019 |
| 252 sessions | 2 | -0.054 | 0.223 | 0.060 | 0.261 |

For the 21-session rebalance, net top-quintile Sharpe was 0.63 for both
legacy and opportunity and 0.71 for fundamental-only. Opportunity drawdown
was -28.52% versus -30.43% for legacy. These runs share historical dates and
cannot be treated as independent evidence. The results do not establish
incremental predictive value for the composite.

## Files changed

- Analytics: `src/finlab/analytics/assessment.py`, `company.py`, `factors.py`,
  `fundamentals.py`, `cli.py`.
- Warehouse configuration: `src/finlab/data/concept_map.yaml`, `src/finlab/config.py`.
- API: `src/finlab/api/app.py`.
- Research: `src/finlab/research/score_validation.py`, `backtest.py`, `trials.py`.
- React: `frontend/src/CompanyAssessment.tsx`, `frontend/src/App.tsx`.
- Tests: `tests/test_assessment_v2.py`, `tests/test_concepts.py`,
  `tests/test_fundamental_factors.py`, `tests/test_historical_recommendations.py`.
- Methodology: `docs/scoring_v2.md`, `docs/factor_methodology.md`.
