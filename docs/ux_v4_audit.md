# FinLab V4 UX audit

Baseline inspected at commit `a862093` in a clean checkout. The published
warehouse was used for both the baseline browser pass and financial examples.

| Area | Verified baseline | V4 change |
| --- | --- | --- |
| Research path | Overview, Discover and Company work, but comparisons have no selection flow. | Carry a persistent comparison list between Discover, Company and Compare. |
| Company evidence | The 2020 AAPL screen shows a correct assessment but requires nested disclosures for metric definitions, historical values and filing evidence. | Use category navigation, concise metric cards and one keyboard-accessible evidence drawer. |
| Charts | Canvas code manually draws axes, traces and the hover cursor. A range control provides keyboard access, but scale, gap and series behavior are owned by the app. | Use a single financial chart library and retain session inspection and an accessible textual reading. |
| Screener | Advanced numeric filters require users to choose their own thresholds. Company rows have no concise evidence context. | Add documented presets, signal summaries and direct comparison actions. |
| Portfolio | The first workflow is CSV editing and internal field names. | Add a guided transaction ledger with a separate local store and retain CSV as an advanced path. |
| Validation | Archived values load correctly. The headline is hardcoded, and the API strips the windows required for charting outcomes. | Derive the headline from quantified evidence and expose selected archived windows. |
| Data Center | Counts and latest publication exist, while sync health and raw attempt details are visually similar. | Separate system health, current coverage and operational detail. |

Baseline checks passed: **279 Python tests**, TypeScript, Vite build, frontend
lint and the existing Chrome workflow at 1440, 1280, 768 and 390 pixels.
Baseline captures are in `data/reports/ui-v4/baseline/`.

The design keeps FinLab’s own charcoal and gold identity and uses progressive
disclosure as the organizing principle. [Finary public reference](https://finary.com/fr)

## Verification after implementation

Playwright passed all six local workflows. The browser used real published
prices, filings and archived validation with outbound page requests blocked.
The suite exercised company search, historical AAPL (70.6), keyboard Evidence
drawer dismissal, Quant formula and filing details, period changes, SMA and
SPY overlays, AAPL/MSFT comparison, a discovery preset, and adding and editing
a portfolio buy without CSV. It rejected a sale above available shares,
accepted a valid sale, and confirmed removal. It checked the changing
validation conclusion and canceled the synchronization confirmation without
queuing a job.

Visual checks covered 1440, 1280, 768, and 390 pixels. The first mobile pass
showed cramped cutoff controls and a clipped second comparison column.
The final version uses a dedicated cutoff row and leaves both selected
columns visible on narrow screens. Quant formulas moved ahead of the
valuation history plot so they're readable on entry to the drawer.

- Baseline screenshots: `data/reports/ui-v4/baseline/`
- Final screenshots: `data/reports/ui-v4/final/`
- Test traces on failure: `data/reports/ui-v4/playwright/`

Python: **288 tests passed**. Ruff lint and format, TypeScript, frontend lint,
production build and `git diff --check` passed. Browser checks do not constitute
a formal WCAG audit. [V4 implementation and limitations](product_v4.md)
