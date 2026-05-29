# Public API

Base URL: `http://<host>/api/v1`

## Authentication

All endpoints require an API key sent in one of these headers:

```
Authorization: Bearer <api_key>
```
or
```
X-API-Key: <api_key>
```

Each user has a unique API key visible (and regenerable) on the dashboard.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | [`/api/v1/business-managers`](api/business-managers.md) | Add a Business Manager (WABA) to your account |
