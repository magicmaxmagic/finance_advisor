# Finlab React frontend

React 19, TypeScript and Vite frontend for the Finlab market workstation.

## Development

From the repository root:

```bash
make app
```

This launches:

- FastAPI at `http://127.0.0.1:8000`
- React/Vite at `http://127.0.0.1:5173`
- Swagger/OpenAPI at `http://127.0.0.1:8000/docs`

The two servers can also be started independently:

```bash
make api
make web
```

Install frontend dependencies directly when needed:

```bash
cd frontend
npm install
```

## API integration

Vite proxies `/api/*` to `http://127.0.0.1:8000`. The dashboard calls:

- `GET /api/health`
- `GET /api/universe`
- `GET /api/market/live`

The market endpoint accepts `tickers`, `demo`, and `demo_price` query parameters.
It is read-only and reuses the same deterministic live-research engine as the existing Streamlit application.

## Commands

```bash
npm run dev
npm run build
npm run lint
npm run preview
```

The demo is available without Alpaca credentials. Live mode requires `FINLAB_ALPACA_KEY_ID` and `FINLAB_ALPACA_SECRET_KEY`, while SEC fundamentals require `FINLAB_SEC_USER_AGENT` to contain a real contact address.
