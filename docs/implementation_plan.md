# FinLab implementation plan

Continue the existing application; each increment must leave existing tests,
Ruff checks and frontend build green. Use deterministic fixtures for core tests.
Do not expose strategy results before correctness gates pass.

## Current track — V7 data reliability

[V7 Stage 1](data_reliability_v7.md) documents the completed software changes,
the pending SEC source gap and the same-epoch coverage results. Share-class
evidence from Alphabet's filing is staged. Exact market capitalization still
requires supported quotes for all economic classes. New research model
experiments and promotion remain separate from the data reliability work.

## V6 data repair and Company Research

The [V6 repair report](financial_repair_v6.md) records the Alphabet trace,
publication identifiers, coverage before and after, and commands.

| Milestone | State |
|---|---|
| A. Alphabet correctness | Fixed nonoperating interest coverage and SMA200 history. Risk scores are independently visible. Class-based capitalization still needs validated data. |
| B. Generalized coverage | CLI measures the configured universe by year and business model. Expanded issuer-specific and dimensional extraction remains open. |
| C. Context and explanation | Added structured formula and score-slot failures and explicit supported group evidence. Dated peer coverage and macro-release vintages remain open. |
| D. Company Research | Moved the price chart ahead of metric details. Added chart navigation, glossary, persistent evidence mode, and print-specific layout. |
| E. Historical validation | Added a separately archived, fixed-cutoff holdout with frozen selection and a Model Validation study selector. |
| F. Decision research | Entry and thesis-review conditions have separate forward outcome diagnostics. Position-aware exit simulation and portfolio turnover for those hypotheses remain open. |
| G. Sentiment and onboarding | Company Research includes a skippable first-visit guide and metric glossary. Calibrated sentiment and application-wide onboarding remain open. |

## V5 quantitative evidence and financial context

V5 extends the V4 research experience and preserves the production scoring
version and configurable 60/25/15 defaults. V3's local research archive remains
the baseline. [V5 context and verification](financial_context_v5.md) describes
the first increment. [V3 methodology](validation_v3.md) documents the existing
point-in-time research protocol. V5 extensions require separately versioned
trials and artifacts.

| Phase | Work and acceptance gate | State |
|---|---|---|
| 1 | Reuse scoring formulas and V3 research. Record comparable baselines, coverage, dataset identifiers, and known survivorship and revision risks. | Baseline recorded in [V2 scoring](scoring_v2.md) and [V3 results](validation_v3.md) |
| 2 | Add prior-only distributions and peer context. Enforce comparable accounting, minimum distinct-issuer counts and dated classifications for historical peers. | Sampled fundamental history and current peers shipped. Dated classifications and broader historical coverage remain open. |
| 3 | Extend the prespecified factor registry across valuation, quality, growth, technical and risk at 21/63/126/252 sessions. Report matched coverage, bucket returns, temporal stability, regime and valid sector results, effective sample size and dependence-aware intervals. | V3's tested factor subset available. Expanded registry and new research runs remain open. |
| 4 | Add value-, quality- and growth-only ablations to the existing dimension pairs, full score, legacy and baselines. Diagnose redundancy on matched samples. Evaluate entry filters and risk constraints as distinct experiments. | Existing V3 dimension and paired comparisons available. New ablations remain open. |
| 5 | Freeze model selection and a chronological final test boundary. Verify label maturation, purging, embargoes, and next-open execution with independent fixtures. Record all tried weights, paired cost-adjusted outcomes, multiple-testing corrections, and uncertainty. | V3 walk-forward protocol available. A new prespecified final holdout and validation run remain open. |
| 6 | Extend Data Engine coverage with public macro releases and known vintage identifiers. Publish event and sentiment timestamps and source evidence. Exercise missing-data states against the local warehouse. | SPY regime and warehouse SEC/corporate-action timeline shipped. Macro vintages and timestamped sentiment remain open. |
| 7 | Surface Model Evidence and company context in Simple and Quant views. Teach the distinction between descriptive scores and predictive evidence in onboarding. | Company context and V4 Model Validation available. Expanded evidence and onboarding remain open. |
| 8 | Evaluate entry conditions and thesis-review/exit signals separately, with outcome definitions, missed opportunities, downside, turnover and costs. Consider portfolio concentration only when holdings data supports it. | Open |

Data Completeness owns coverage diagnostics and historical availability in
warehouse publications. Sentiment Intelligence must supply timestamped evidence
through the Data Engine before Analytics can use it. Investment Decision
Research consumes versioned studies and keeps descriptive business quality,
entry conditions, and exit review separate. Onboarding explains where the data
comes from and how to interpret each form of evidence.

## Original implementation tracker

This table records the original warehouse rollout. Consult
[upgrade status](upgrade_status.md) and the phase-specific guides for shipped
work and remaining gaps.

| Order | Increment | Acceptance gate | State |
|---|---|---|---|
| 0 | Source audit and baseline | Document actual modules, risks and baseline | Complete |
| 1a | Immutable publication | Readers pinned across updates; reject tampering | Complete |
| 1b | Incremental recovery | Per-security persisted observation watermarks; correction overlap; cold-start history; partial downloads retry | In progress |
| 1c | Price contract and quality | Explicit adjustment convention, adjusted close, UTC ingestion/provider provenance; reject invalid/duplicate/conflicting bars; migration parity | Pending |
| 1d | Complete publication cycle | Export actions/identity; publish only after complete successful cycle; interrupted-cycle fixture preserves prior epoch | Pending |
| 1e | Daily operation | After-close New York schedule, exchange holiday/session checks, startup catch-up, launchd and systemd, freshness API | Pending |
| 1f | SEC availability | Acceptance timestamps, amendment/restatement fixtures, statement coverage and update handler | Pending |
| 2a | Factors | Correct fixed horizons, benchmark alignment, total returns, risk and technical formulas documented and tested | Pending |
| 2b | Fundamentals and ranking | Canonical PIT queries, units/currency, sector normalization, explicit insufficient status, model version/history; API uses shared modules | Pending |
| 3 | Backtesting | Next-session execution, actions, membership, costs, trial ledger; benchmark and walk-forward separation with hand-calculated fixtures | Pending |
| 4 | Portfolio | Separate transaction store, FIFO accounting, USD/CAD FX history, allocation/risk/scenarios with reconciled fixtures | Pending |
| 5 | Research experience | Existing React views extended, warehouse-only weekly report, archived score changes and outcome evaluation; API/build checks | Pending |
| 6 | Optional local explanations | Ollama consumes structured results only; offline deterministic behavior unchanged | Pending |

### Original increment 1b

Use the most recent stored observation for each security as the recovery anchor,
not a fixed ten-day window from today. Request a configurable overlap before
that observation to catch recent corrections. Securities without history use a
configurable historical start. Group securities sharing a request window.
A missing symbol must raise a retryable job failure rather than report success;
retained observations make retries idempotent. This does not detect internal
historical gaps, prove session completeness or repair corrections older than
the overlap; those require the session/quality increments above.

## Validation and release discipline

For every increment: targeted correctness fixtures, full pytest, Ruff lint and
format, frontend TypeScript/build/lint, API compatibility where affected, and
updated architecture/status documentation. No automatic real-money orders,
mandatory paid API, cloud infrastructure or mandatory LLM. Do not claim live
provider or scheduling validation from fixture tests. Keep milestone 1 open until
all its acceptance gates pass. Re-evaluate the plan after discovering source or
provider constraints.
