"""Read-only WhatsApp MCP over streamable-HTTP (for VPS hermes-agent).

Wraps the existing FastMCP `main.mcp` server:
  - strips all write/send tools (read-only guarantee),
  - enforces a static bearer token via a pure-ASGI middleware (no response
    buffering, so streamable-HTTP streaming is preserved),
  - serves on 127.0.0.1:8085 at path /mcp (Caddy proxies the public /mcp).

Config is read from ./.env (KEY=VALUE, first '=' wins):
  WHATSAPP_MCP_TOKEN=<bearer clients must send>
  WHATSAPP_ACCOUNTS={"main":{"db":"...","api":"..."},"second":{...}}

WHATSAPP_ACCOUNTS must be set BEFORE importing main (whatsapp.py reads it at
import time), so we load .env first.
"""
import os

_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_ENV):
    with open(_ENV) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)

TOKEN = os.environ.get("WHATSAPP_MCP_TOKEN", "")
if not TOKEN:
    raise SystemExit("FATAL: WHATSAPP_MCP_TOKEN not set")

HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_PORT", "8085"))

import uvicorn
import main  # registers all tools on main.mcp; sees WHATSAPP_ACCOUNTS from env

# --- read-only: remove every write/send tool ---
WRITE_TOOLS = ["send_message", "send_file", "send_audio_message", "download_media"]
for name in WRITE_TOOLS:
    main.mcp._tool_manager._tools.pop(name, None)
print("read-only MCP tools:", sorted(main.mcp._tool_manager._tools.keys()), flush=True)

# --- compat shim: the upstream tools return dataclasses (Chat/Message/Contact),
# but mcp>=1.10 validates tool output against a dict-shaped schema and rejects
# dataclass instances. Wrap each tool fn to convert dataclasses -> plain dicts. ---
import dataclasses
import inspect
import functools


def _to_plain(obj):
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, (list, tuple)):
        return [_to_plain(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    return obj


for _tool in main.mcp._tool_manager._tools.values():
    _orig = _tool.fn
    if inspect.iscoroutinefunction(_orig):
        @functools.wraps(_orig)
        async def _wrapper(*a, __orig=_orig, **k):
            return _to_plain(await __orig(*a, **k))
    else:
        @functools.wraps(_orig)
        def _wrapper(*a, __orig=_orig, **k):
            return _to_plain(__orig(*a, **k))
    object.__setattr__(_tool, "fn", _wrapper)


class BearerAuth:
    """Pure-ASGI middleware: 401 unless Authorization: Bearer <TOKEN>. Does not
    buffer responses, so it is safe in front of streamable-HTTP."""

    def __init__(self, app, token):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"").decode("latin-1")
            provided = auth[7:] if auth.startswith("Bearer ") else ""
            if provided != self.token:
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json"),
                                (b"x-robots-tag", b"noindex, nofollow")],
                })
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


app = BearerAuth(main.mcp.streamable_http_app(), TOKEN)

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
