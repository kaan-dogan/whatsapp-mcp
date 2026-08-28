# whatsapp-outbox — staging files for an allowlisted WhatsApp send

Lets a report PDF reach a mentee **as a file in the chat** rather than as a
Drive link. The point is avoiding per-person Drive permissions: the document
just sits in the conversation where the student already is.

## Why a separate service

Two boundaries are kept apart on purpose:

- **`whatsapp-readapi` stays GET-only.** Its "read-only, databases opened
  read-only" promise is load-bearing — bolting an upload route onto it would
  quietly retire the promise. This service accepts writes and cannot read a
  chat.
- **The outbox cannot send.** It takes bytes, parks them on disk, returns a
  handle. Who may *receive* a file is still decided by the allowlist in
  `mentee_send.py`. So a leaked outbox token buys an attacker disk space, not a
  message to a student.

## Two ways in, and why

The box's `gog` credential holds the **`drive.file`** scope, which grants access
to exactly the files that credential uploaded — not to the Drive as a whole.
Measured, not assumed:

| File | `gog drive download` |
|---|---|
| `Pinar_Turan_Aday_Brifingi` (VPS pipeline output) | 92 565 bytes |
| Görüşme özeti PDF (VPS pipeline output) | 131 024 bytes |
| A PDF added to Drive by hand | `404 notFound` |

So the pipeline's own output — reports, meeting summaries, aday brifingi — is
already reachable server-side. `stage_from_drive(file_id)` uses that: one MCP
call, no upload, no outbox token, and the bytes never travel through the client.

Everything else needs the caller to post the bytes. Widening the scope to
`drive.readonly` would fix that too, but it buys server-side read access to the
entire Drive for one narrow feature — not a trade worth making when uploading
needs no Google permission at all.

## Endpoints

`https://ingest.example.com/outbox/*` → `127.0.0.1:45411` (Caddy `handle_path`).
All routes except `/health` need `Authorization: Bearer $WHATSAPP_OUTBOX_TOKEN`.

| Route | Purpose |
|---|---|
| `POST /?filename=<name>.pdf` | Upload one PDF as the raw request body. Returns `{handle, filename, bytes, sha256}`. |
| `GET /` | List what is currently staged. |
| `DELETE /<handle>` | Drop a staged file early. |
| `GET /health` | Unauthenticated liveness. |

Rejected: anything whose first bytes are not `%PDF-` (the extension is not
trusted), empty bodies, and anything over `OUTBOX_MAX_BYTES` (20 MiB default —
returned as a clean `413` when the client sends `Content-Length`). Staged files
are deleted after `OUTBOX_TTL_HOURS` (24).

**The filename becomes what the student sees.** The bridge sets the document
title from the staged file's name, so upload under the name they should read —
`Ada — Danışmanlık Raporu.pdf`, not `output.pdf`. Directory parts, control
characters and leading dots are stripped; the name is otherwise preserved,
Turkish characters included.

## Staging from Drive

```
stage_from_drive(file_id, filename="") -> {success, handle, filename, bytes}
```

Reads the Drive metadata first (rejecting a non-PDF mimeType or an oversized
file before downloading), stages under the file's own Drive name unless
`filename` overrides it, then re-checks the downloaded bytes for the `%PDF-`
signature — Drive's mimeType is a label the uploader chose, and this file is
about to go to a student. A file outside the credential's reach comes back as
*"file not visible to this server"*, which is the signal to upload instead.

Needs `GOG_ACCOUNT` and `GOG_KEYRING_PASSWORD` in the MCP service's `.env`
(the file keyring cannot prompt in a service).

## Sending

```
send_file_to_mentee_group(chat_jid, handle, caption="", dry_run=False)
```

Same allowlist, same audit log as `send_to_mentee_group`. The tool never accepts
a filesystem path — only a UUID handle — so a path outside the staging root
cannot be expressed. Before sending it re-checks, rather than trusting the
upload step: handle shape, that the staging dir holds exactly one file, that
`realpath` stays inside the root (a planted symlink cannot redirect the send),
non-empty, under the size cap, and still a real PDF.

`caption` rides along as the document caption, so file and explanation arrive as
one message.

## End-to-end

```bash
TOKEN=$(ssh root@<host> 'sed -n "s/^WHATSAPP_OUTBOX_TOKEN=//p" /root/whatsapp/outbox-svc/.env')
curl -s -X POST "https://ingest.example.com/outbox/?filename=Ada%20-%20Rapor.pdf" \
  -H "Authorization: Bearer $TOKEN" --data-binary @rapor.pdf
# -> {"handle":"a914e6f0-...", ...}
```

Then call `send_file_to_mentee_group` with that handle — `dry_run=True` first if
you want to see the resolved group and filename before anything is sent.

## Deployment

- Source: `/root/whatsapp/outbox-svc/server.mjs`, `.env` (chmod 600)
- Staging: `/root/whatsapp/outbox/` (chmod 700)
- Service: `whatsapp-outbox.service`, boot-enabled, `Restart=always`
- Caddy: `handle_path /outbox/*` → `127.0.0.1:45411`, placed before the catch-all

## Known rough edge

- The bridge only records an outgoing message in `messages.db` when the caption
  is non-empty (`if messageStore != nil && message != ""`). A document sent with
  no caption is delivered but invisible to `list_messages` afterwards. Passing a
  caption avoids this.
