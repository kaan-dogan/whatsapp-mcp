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
