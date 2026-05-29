# POST /api/v1/business-managers

Add a Business Manager (WABA) to the authenticated user's account. If the `waba_id` already exists, its token is updated (idempotent upsert). After saving, the endpoint automatically subscribes the WABA to Meta webhook events.

## Authentication

Send your API key in one of these headers:

```
Authorization: Bearer <api_key>
```
or
```
X-API-Key: <api_key>
```

Your API key is shown on the dashboard under **Chave de API**.

## Request

**Content-Type:** `application/json`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `waba_id` | string | yes | WhatsApp Business Account ID |
| `token` | string | yes | Meta access token for this WABA |
| `adspower_profile_id` | string | no | AdsPower profile ID to link with this BM; when set, an "Open in AdsPower" button appears on the dashboard |

```json
{
  "waba_id": "123456789",
  "token": "EAAxxxxxx..."
}
```

## Response

### 201 Created

```json
{
  "ok": true,
  "waba_id": "123456789",
  "adspower_profile_id": "user_12345",
  "webhook_subscribed": true,
  "webhook_error": null
}
```

`adspower_profile_id` is `null` when the field was not sent in the request.

- `webhook_subscribed` — `true` if the webhook subscription call to Meta succeeded.
- `webhook_error` — `null` on success, or a string describing the Meta API error. The BM is saved regardless of webhook subscription outcome.

### 400 Bad Request

Missing or empty `waba_id` or `token`, or invalid JSON body.

```json
{
  "ok": false,
  "error": "waba_id is required."
}
```

### 401 Unauthorized

Missing or invalid API key.

```json
{
  "ok": false,
  "error": "Invalid API key."
}
```

### 403 Forbidden

The account associated with the API key is banned.

```json
{
  "ok": false,
  "error": "Account banned."
}
```

## curl Example

```bash
curl -X POST http://localhost:5000/api/v1/business-managers \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"waba_id": "123456789", "token": "EAAxxxxxx..."}'
```

## Notes

- The token is stored as-is and is never returned in API responses.
- Re-posting with the same `waba_id` updates the stored token (safe to call repeatedly).
- Webhook subscription failure does **not** prevent the BM from being saved — check `webhook_subscribed` in the response if you need to confirm it.
