# FinLab React frontend

React 19, TypeScript and Vite frontend for the FinLab Analytics application.

## Development

```bash
make start
make stop
```

The frontend runs at `http://127.0.0.1:5173` and proxies `/api/*` to Analytics FastAPI at `http://127.0.0.1:8000`.

## Views

- Overview
- Quantitative ranking
- Company analysis
- Data Center

The interface uses only real warehouse endpoints and clearly labels data availability. It does not issue guaranteed investment recommendations.

## Validation

```bash
npm ci
npm run build
npm run lint
npm run test --if-present
```
