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

## Why the caller uploads, instead of the VPS pulling from Drive

`gog` on the box is authenticated as a **different Google account**
(`kaandn51@gmail.com`) holding the **`drive.file`** scope — it only sees files
it uploaded itself. The mentee reports are filed by the Cowork Drive connector
under the user's own account, so the VPS genuinely cannot read them.

Making it able to would mean re-authorising with something like
`drive.readonly`: server-side read access to the entire Drive, granted for one
narrow feature. Having the caller post the bytes needs no new Google scope and
works identically from Cowork, a laptop, or a cloud routine.

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

## Known rough edges

- The bridge's MIME table has no `pdf` case, so PDFs go out as
  `application/octet-stream` (`whatsapp-bridge/main.go`, the `fileExt` switch).
  Delivery works and the `.pdf` title is right, but adding a `pdf` case would
  let clients label it properly. Needs a Go rebuild and a bridge restart.
- The bridge only records an outgoing message in `messages.db` when the caption
  is non-empty (`if messageStore != nil && message != ""`). A document sent with
  no caption is delivered but invisible to `list_messages` afterwards. Passing a
  caption avoids this.
