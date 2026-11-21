# FinLab React frontend

The React 19/TypeScript/Vite interface consumes the read-only FinLab Analytics FastAPI.

## Development

```bash
make app
```

Or run independently:

```bash
make analytics
make web
```

The frontend proxies `/api/*` to `http://127.0.0.1:8000` in development.

## Validation

```bash
npm run build
npm run lint
npm run test --if-present
```
