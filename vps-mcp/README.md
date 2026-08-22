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
>
> **Port 22 is allow-listed per source IP** on security group `ingest-box`
> (`sg-0e11a456779314049`, eu-central-1). A home-ISP address change makes SSH
> time out with no other symptom -- the box is fine, you are simply not on the
> list. Check the current rules, then re-add yourself:
>
> ```bash
> aws ec2 describe-security-groups --group-ids sg-0e11a456779314049 \
>   --region eu-central-1 --query 'SecurityGroups[].IpPermissions' --output json
> aws ec2 authorize-security-group-ingress --region eu-central-1 \
>   --group-id sg-0e11a456779314049 --ip-permissions \
>   "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=$(curl -s https://checkip.amazonaws.com | tr -d '\n')/32,Description=kaan-home}]"
> ```

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

## Search integrity (`selfcheck.py`)

The June 2026 `@lid` migration produced a failure nobody noticed for weeks: the
bridges were up and storing messages, listings still showed contact names, but
every name-based query returned nothing — the display name was resolved in
Python after the SQL ran, while the SQL still filtered on the raw `chats.name`
column, which for LID rows holds an opaque number. Anything that starts from a
name ("what did I write to X?") came back empty all the way down.

Two things now prevent a silent repeat:

- **One name, one code path.** `display_name()` is the only source of a chat's
  name, and it is registered on every connection as the SQL function
  `wa_name(jid, name)`. Filters and sorts call it, so a chat can never be
  visible in a listing yet invisible to a search for the name that listing
  showed. `wa_match()` does the comparison with Unicode folding, so `Betül`,
  `betul` and `BETUL` all match (SQLite's own `LOWER()`/`LIKE` fold ASCII only,
  which quietly broke every Turkish name).
- **An assertion that fails loudly.** `selfcheck.py` takes the chats a listing
  returns and demands each one be findable by its own name, per account. Run it
  by hand after any deploy:

  ```bash
  cd /root/whatsapp/mcp && WHATSAPP_ACCOUNTS="$(sed -n 's/^WHATSAPP_ACCOUNTS=//p' .env)" python3 selfcheck.py
  ```

  `vps-watchdog/watchdog.sh` also runs it hourly and alerts to Telegram on
  failure, so the next migration surfaces as a message rather than as months of
  empty results. Set `SELFCHECK_DIR` / `SELFCHECK_PY` in the watchdog `.env` if
  the layout moves; the check reports "skipped", never a false pass.

Deploying the fix means copying `whatsapp.py` **and** `selfcheck.py`, then
restarting `whatsapp-mcp.service`.
