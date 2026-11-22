# FinLab UX audit

The baseline is the existing local React frontend and its six destinations.
The public Finary site was reviewed for navigation grouping, concise labels, and
progressive disclosure. FinLab uses its own palette, layout, writing, and assets.
[Public design reference](https://finary.com/fr)

| Screen | Baseline problem | Correction |
| --- | --- | --- |
| Shell | Opens on a dense ranking, uses unexplained icon glyphs, and keeps a long ticker list in the main navigation. | Default to Overview, group destinations by purpose, support global search and browser history, and collapse navigation on small screens. |
| Overview | The first useful result is a bare rank list. Benchmark, data age, and evidence require another view. | Surface benchmark context, publication status, and a small set of research candidates together. |
| Ranking | Rows are mouse-click targets and the distinction between the legacy score and the multidimensional company score is unclear. | Use keyboard-operable company buttons, explicit model labels, filters, sortable columns, and pagination. |
| Company | Legacy score and dense metric panels precede the explanation. The chart has no timeframe selection, usable axis, or interaction. | Lead with identity, price, score and evidence. Move formulas and filings into expandable sections and add chart controls. |
| Strategy | A single curve and raw metric keys provide little decision context. | Label the run, costs, benchmark and the limits of the evaluation. |
| Portfolio | CSV instructions and raw result JSON dominate the screen. | Keep import and scenario controls and add human-readable results. |
| Data Center | Research and operational status share the same hierarchy. Jobs need clearer feedback. | Separate operations in navigation, show publication details and keep refresh routed through the existing queue. |
| Across screens | Mixed color conventions, inconsistent form labels, limited loading feedback and a desktop-oriented layout. | Use dark tokens, status colors, responsive layouts, labeled forms, visible focus and reduced-motion support. |

Source inspection covered all six routes before edits. Screenshot and browser
interaction coverage is recorded separately in the verification notes, so
source findings are distinguishable from browser observations.

## Implementation and verification

The desktop baseline screenshot showed the first row of filters exceeding the
viewport at 1440 pixels and the entire company list occupying the sidebar.
The revised workspace puts market context and research candidates on the home
screen. The company screen gives conclusions before chart and filing details.
The visual review also surfaced raw engine status wording and navigation
focus that scrolled the page under the sticky header. The implementation now
uses plain status labels and moves focus without scrolling the heading away.

Headless Chrome exercised the actual React preview with the local FastAPI
warehouse at 1440, 1280, 768, and 390 pixels. It checked overflow, route
navigation, screener density and pagination, chart ranges and overlays,
watchlist persistence, published status, the refresh-confirmation cancel path,
the archived validation page, a completed strategy simulation and an AAPL
portfolio valuation. The historical AAPL view returned 70.6 on the page and
exposed Quant evidence. The script checks for browser runtime exceptions.
No refresh job was created during visual QA.

| Capture | Local file under `data/reports/ui/` |
| --- | --- |
| Baseline desktop | `before-desktop.png` |
| Overview desktop and mobile | `after-overview-1440.png`, `after-overview-390.png` |
| Discovery | `after-discover-1280.png` |
| Company at laptop, tablet and mobile widths | `after-company-1280.png`, `after-company-768.png`, `after-company-390.png` |
| Historical company and chart | `after-company-historical-1440.png`, `after-chart-1440.png` |
| Model validation, strategy and portfolio | `after-validation-1440.png`, `after-strategy-1440.png`, `after-portfolio-1440.png` |
| Operations mobile | `after-data-center-390.png` |

The automated check covers practical accessibility features such as named
controls, focusable tables and usable narrow layouts. It does not certify
WCAG conformance. The workspace uses a dark theme, one browser-local watchlist
and delayed published data. The Data Center can queue a standard sync. Full
backfill remains an engine CLI operation. Charts, prior scores, and financial
ratios show availability from the live local publication.
