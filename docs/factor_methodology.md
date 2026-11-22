# Deterministic factor methodology

Market factors v2 consume caller-supplied observations dated on or before the
cutoff. Closes must be finite, positive and unique by security/date. Input basis
is the supplied close series: these outputs are price returns, not guaranteed
total returns. Do not mix adjustment conventions or currencies. Corporate-action
aware return reconstruction is still required before rigorous backtests.

- 1/3/6/12 month return: P[t]/P[t-k]-1 for k=21/63/126/252 observations.
  Insufficient history returns null; no shortened horizon is substituted.
- 12-minus-1 momentum: P[t-21]/P[t-252]-1.
- Relative 12-month momentum: asset return minus benchmark return, only when
  both endpoint dates match and each series has 253 observations.
- Volatility: sample standard deviation of consecutive simple returns times sqrt(252).
- Maximum drawdown: min(P[t]/running_max(P)-1) over supplied history.
- MA50/MA200: arithmetic mean of the last 50/200 closes; null before full history.
- RSI14: simple trailing average gains/losses (Cutler convention); flat series
  returns 50. This implementation is not Wilder-smoothed RSI.
- MACD: recursive EMA12 minus EMA26, adjust=False; minimum 26 observations.
  The field is the MACD line, not the signal-line histogram.

Scoring v2 caps tails at empirical order-statistic 5%/95% cutoffs, centers with
the median and scales by 1.4826 times median absolute deviation. Zero MAD yields
neutral scores. Component score is clip(50+15*z,0,100); overall is the weighted
sum only if every positive-weight input exists. Missing factors produce null
overall_score and insufficient_data. Duplicate factor vintages are rejected.
An as_of column, when supplied, must match the requested scoring date.

The ranking API currently weights annual earnings yield 40%, 12-month price
momentum 40%, negative volatility 20%. Earnings yield uses latest public annual
diluted EPS divided by latest close. It is not TTM and does not yet reconcile
historical EPS/share price split bases; use it as preliminary research only.
Refresh concept mappings with `make concepts`, then republish, to enable the
new eps_diluted mapping. Missing mappings result in unavailable scores.

These are descriptive ranks, not calibrated profit probabilities. Sector
neutralization, complete five-family factors, total-return series, stale-data
gating and model history remain outstanding. Historical active-security selection
in the API is not yet a survivorship-free universe. No historical robustness or
profitability claim is established by passing arithmetic tests.
