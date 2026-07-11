# whatsapp-readapi

Read-only HTTP query API over the whatsmeow bridge's `messages.db`. Lets cloud
skills / Claude Code routines query WhatsApp over HTTPS with a bearer token.
**No send/write path** — the bridge's `/api/send` stays on localhost:8080 and is
never exposed.

## Deployment (VPS `hermes-agent`, 139.59.158.24)

- Source: `/root/whatsapp/readapi/server.mjs`
- Service: `whatsapp-readapi.service` (systemd), listens on `127.0.0.1:45410`
- Session/data: fed by `whatsapp-bridge.service` writing `/root/whatsapp/bridge/store/messages.db`
- Public route: Caddy proxies `https://ingest.example.com/whatsapp/*` → `127.0.0.1:45410`
- Auth: `Authorization: Bearer <WHATSAPP_READ_TOKEN>` on every request (except `/health`)

## Endpoints (all GET)

| Path | Query params | Returns |
|------|--------------|---------|
| `/health` | — | `{ok:true}` (no auth) |
| `/chats` | `limit` (≤200), `offset`, `search` | recent chats |
| `/messages` | `chat_jid`, `limit` (≤500), `offset`, `search`, `after`, `before`, `order=asc\|desc` | messages |
| `/contacts` | `query` (required), `limit` | chats matching name/jid |
| `/context` | `chat_jid`, `message_id` (both required), `before`, `after` | messages around a target |

`after`/`before` compare against the stored timestamp string
(`2026-07-11 22:18:22+03:00`).

## Example

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://ingest.example.com/whatsapp/chats?search=onur&limit=5"
```
