# whatsapp-mcp (read-only, remote HTTP) — VPS deployment

Runs the existing FastMCP WhatsApp server (`whatsapp-mcp-server/main.py`) over
**streamable-HTTP** on the `hermes-agent` VPS so Claude Code can use it as a
remote MCP connector. Read-only: all send/download tools are stripped at startup.

## What `serve_http.py` does
- loads `.env` (token + `WHATSAPP_ACCOUNTS`) before importing `main`,
- removes `send_message`, `send_file`, `send_audio_message`, `download_media`,
- converts upstream dataclass results (Chat/Message/Contact) → dicts so mcp≥1.10
  output-schema validation passes,
- wraps `main.mcp.streamable_http_app()` in a pure-ASGI bearer-token middleware
  (no buffering → streaming preserved),
- serves `127.0.0.1:8085` at path `/mcp`.

## Deployment (VPS)
> **Host moved 2026-08-11.** This no longer runs on the DigitalOcean `hermes-agent`
> box (139.59.158.24) — every WhatsApp unit there is stopped and disabled. It now
> runs on the AWS EC2 instance **`ingest-box`** (`i-0ef0a3161202cf418`,
> eu-central-1, 3.79.227.197), which is where `ingest.globalmentorluk.com`
> resolves. SSM is not connected on that instance; reach it over plain SSH:
> `ssh -i ~/.ssh/dora_aws root@3.79.227.197`.

- Source: `/root/whatsapp/mcp/` (venv with `mcp[cli]` 1.28), `.env` (chmod 600)
- Service: `whatsapp-mcp.service` (systemd), boot-enabled
- Caddy: `https://ingest.example.com/mcp` → `127.0.0.1:8085`, with
  `header_up Host {upstream_hostport}` (defeats the MCP DNS-rebinding Host check)
- Tools (read-only): list_chats, list_messages, search_contacts, get_chat,
  get_contact_chats, get_direct_chat_by_contact, get_last_interaction,
  get_message_context, list_whatsapp_accounts. All accept an `account` arg
  (`main` / `second`).

## Add to Claude Code
```bash
claude mcp add --transport http whatsapp-readonly \
  https://ingest.example.com/mcp \
  --header "Authorization: Bearer <WHATSAPP_MCP_TOKEN>" --scope user
```

## Note on claude.ai / Cowork
claude.ai custom connectors expect OAuth, not a static bearer header, so this
endpoint plugs cleanly into **Claude Code** (CLI/desktop) but not the claude.ai
connector UI without adding an OAuth layer.

## Allowlisted sending (`mentee_send.py`)

The server stays read-only for every upstream write tool. `mentee_send.py` adds
back one narrow capability: text-only sends to group JIDs listed verbatim in
`allowlist.json`.

Why not simply stop stripping `send_message`: that tool takes
`recipient: str` documented as "phone number OR JID", so exposing it grants
every chat and every phone number on the account. `send_to_mentee_group`
cannot express a recipient outside the allowlist.

Guarantees:
- group JIDs only — direct chats (students', parents' personal numbers) are
  unreachable by construction
- allowlist re-read on every call, so adding a mentee needs no restart
- fails closed — missing/unreadable/malformed allowlist denies every send
- no media: `send_file`, `send_audio_message`, `download_media` stay stripped
- every attempt (allowed and denied) appended to `send-audit.log` as JSONL
- `dry_run: true` runs all validation and reports without sending

### Tools added
- `list_mentee_groups()` → permitted `{jid, name, account}` list
- `send_to_mentee_group(chat_jid, message, dry_run=False)`

### Deploy
1. Copy `mentee_send.py` and `allowlist.json` to `/root/whatsapp/mcp/`
2. `chown root:root` + `chmod 600` on `allowlist.json` — if any MCP tool can
   write this file the guard is worthless
3. In `serve_http.py`, immediately after the `WRITE_TOOLS` pop loop, add:
   ```python
   import mentee_send  # registers send_to_mentee_group + list_mentee_groups
   ```
4. `systemctl restart whatsapp-mcp` and confirm the startup log prints
   `[mentee_send] registered; allowlist=... (N groups)`
5. Verify from the client: `list_mentee_groups()`, then a `dry_run: true` send,
   then one real send to a test group before touching a mentee group.

### Env overrides
`MENTEE_ALLOWLIST_PATH`, `MENTEE_AUDIT_PATH`, `MENTEE_MAX_LEN` (default 2000).

### Note on the bearer token
Enabling sends changes the blast radius of `WHATSAPP_MCP_TOKEN` from "can read
all chats" to "can also message clients". The allowlist caps that to the groups
above; rotate the token when enabling.
