# Scoring engine audit

## Scope and baseline

This audit inspects the checked-out FinLab implementation in this workspace. The historical baseline is recorded in `docs/technical_audit.md`; it is not a claim that all requirements in the broader product plan are already implemented. The current Python suite has 264 tests passing at this revision, and Ruff checks pass.

## Current implementation

Analytics reads a validated, pinned DuckDB/Parquet publication. `fundamentals_asof` excludes facts not filed by the requested date; `common_shares_pit` provides public share counts. `analytics/fundamentals.py` calculates selected annual margins, cash-flow yields, reported-share valuation ratios, ROE/ROIC, growth, and debt/assets when inputs align. Missing or unsupported inputs remain unavailable. `analytics/factors.py` calculates returns, 12-minus-1 momentum, volatility, drawdown, SMA50/200, Cutler RSI, MACD line, Sharpe/Sortino, VaR/CVaR and beta. `analytics/scoring.py` winsorizes, robust-normalizes, optionally sector-normalizes, and requires all nonzero weighted features.

`analytics/rankings.py` currently produces one five-family descriptive score from annual earnings yield, net margin, annual revenue growth, 12-month price momentum and inverse volatility. It uses the configured seed universe and flags historical membership limitations. `analytics/company.py` exposes a company detail API with annual factors and market factors. Recommendations archive source hashes and warehouse epoch. Backtesting and walk-forward tools consume published local data and record trials.

## Gap to requested methodology

The one score does not yet expose separate fundamental attractiveness, technical timing, risk resilience, and overall opportunity scores. Historical and sector valuation distributions, TTM aggregation, forward estimates, earnings/EV multiples, multi-year CAGRs, balance-sheet liquidity ratios, broader technical indicators (EMA set, slopes/crossovers, ADX, Bollinger, ATR, volume/liquidity) and risk windows are incomplete. Fundamental features are annual and data-coverage dependent. Sector mapping is often unavailable, so company classifications may be `Unclassified`.

Options snapshots, historical options chains, IV surfaces and GEX are not present in the local warehouse. No derivatives score can be reported or validated; no dealer inventory can be inferred. Historical score deciles, IC, bootstrap uncertainty, purged validation, ablation and incremental comparison against the baseline are also incomplete. The frontend does not yet render a full explainability panel for every metric or separate hierarchical scores.

## Data and correctness risks

Market capitalization requires reported shares aligned through split events and is currently supported only in the company detail path for covered USD common shares. TTM and forward estimates cannot be safely inferred from annual facts. Historical sector medians and peer groups require PIT industry classification. SEC facts can contain restatements; filings are filtered by availability but filing acceptance timestamps are explicitly unverified unless backed by source evidence. Yahoo adjusted prices are vendor-revised history, not point-in-time vendor vintages. Seed-universe history before snapshots is survivorship biased.

## Implementation priorities

1. Add pure, fixture-tested TTM/valuation, liquidity, profitability trend and technical feature calculations with explicit applicability and null semantics.
2. Produce distinct fundamental, technical and resilience scores with feature coverage, correlation/redundancy diagnostics and configurable horizon weights.
3. Enrich company and recommendation APIs with score dimensions, signal agreement/contradiction, formulas and data lineage.
4. Add historical cross-sectional evaluation with aligned horizons, captured-universe constraints, costs, embargoes and uncertainty; do not claim predictive validity before results.
5. Add UI inspection and historical comparison. Options remain explicitly unavailable until prospective snapshots exist.
