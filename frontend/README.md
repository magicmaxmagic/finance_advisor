# FinLab Analytics frontend

React, TypeScript and Vite serve the local research workspace. Research, personal
tools, and Data Engine operations share a shell and have distinct navigation groups.
The visual system uses charcoal surfaces, restrained gold emphasis, and labeled
semantic colors for evidence and concerns.

## Run locally

From the repository root, with Python dependencies available and local financial
data already published, run Analytics and Vite in separate terminals.

```sh
env PYTHONPATH=src .venv/bin/python -m finlab.analytics.cli serve --host 127.0.0.1 --port 8000
npm --prefix frontend ci
npm --prefix frontend run dev -- --host 127.0.0.1 --port 5173
```

Open `http://127.0.0.1:5173`. Configure the Vite proxy with
`FINLAB_VITE_API_URL` when Analytics uses a different port. Run the Data Engine
separately for new observations or queued refreshes. A published warehouse
supports offline Analytics.

## Source and view navigation

| File | Responsibility |
| --- | --- |
| `src/main.tsx`, `index.html` | React mount and document metadata. |
| `src/index.css`, `src/App.css` | Theme tokens, accessibility defaults, shell, tables, responsive breakpoints, and chart layout. |
| `src/App.tsx` | Hash navigation, browser history, global company search, historical cutoff, watchlist persistence, request states, company hero and Data Center publication summary. |
| `src/MarketViews.tsx` | Overview snapshot and opportunity cards, compact multidimensional screener, filters, custom columns, sorting and paging. |
| `src/CompanyAssessment.tsx` | Expandable score evidence, grouped indicators, Simple/Quant modes, and filing detail. |
| `src/FinancialChart.tsx` | Canvas chart with session ranges, moving averages, volume, optional benchmark, cursor and keyboard-operable session slider. |
| `src/ModelValidation.tsx` | Archived V2/V3 evidence, horizons, model and factor results, uncertainty, quintiles and limitations. |
| `src/ResearchTools.tsx` | Strategy and benchmark views, portfolio CSV and scenario workflow, ingestion status and queued refresh. |
| `src/format.ts` | Shared financial values, labels and research classification formatting. |
| `vite.config.ts`, `scripts/browser-check.mjs` | Local proxy and headless browser interaction checks. |

```mermaid
flowchart TD
    SHELL["App shell and historical date"] --> OVER["Overview and Discover"]
    SHELL --> COMPANY["Company research"]
    SHELL --> STUDY["Model Validation"]
    SHELL --> STRAT["Strategies and Portfolio"]
    SHELL --> OPS["Data Center"]
    OVER --> DISC["Discovery API"]
    COMPANY --> DETAILS["Company factors and price history"]
    STUDY --> ARCHIVE["Archived research API"]
    STRAT --> SIM["Local simulation APIs"]
    OPS --> CONTROL["Publication and job APIs"]
```

## Verify

```sh
npm --prefix frontend run build
npm --prefix frontend run lint
node --experimental-websocket frontend/scripts/browser-check.mjs \
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
  http://127.0.0.1:5173
```

The browser script uses a disposable Chrome profile and writes screenshots under
`data/reports/ui/`. See [UX audit](../docs/ui_ux_audit.md) for baseline findings
and [V3 validation](../docs/validation_v3.md) for the research protocol.
