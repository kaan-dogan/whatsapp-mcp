"""Read-only WhatsApp MCP over streamable-HTTP, with a self-contained OAuth 2.1
authorization server (for claude.ai web custom connectors + routines).

Why OAuth: claude.ai persists whatever credential you give a connector. A static
header or a URL secret would put our long-lived API key in claude.ai's database.
OAuth avoids that — claude.ai only ever stores a short-lived, revocable *delegated*
token; our real credential never leaves this box.

Low-maintenance by design: STATELESS. No database, no token store, no cron. Every
token (authorization code, access, refresh) is an HMAC-signed blob carrying its own
expiry, verified in-process with one signing key. Kill switch = rotate
OAUTH_SIGNING_KEY (invalidates every issued token at once).

The MCP endpoint (/mcp) accepts EITHER:
  - a valid OAuth access token   (claude.ai web / routines), OR
  - Authorization: Bearer <WHATSAPP_MCP_TOKEN>   (Claude Code / header clients),
and returns 401 + WWW-Authenticate (RFC 9728 pointer) otherwise, which kicks off
the OAuth discovery flow for clients that support it.

Only the account holder can complete /authorize: it is gated by OAUTH_CONSENT_PASSWORD,
entered in the browser during the one-time connect. That password never reaches claude.ai.

Config from ./.env (KEY=VALUE, first '=' wins):
  WHATSAPP_MCP_TOKEN=<static bearer for Claude Code>
  OAUTH_SIGNING_KEY=<hex; signs all tokens; rotate to revoke everything>
  OAUTH_CONSENT_PASSWORD=<what you type on the consent screen>
  WHATSAPP_ACCOUNTS={"main":{...},"second":{...}}
  PUBLIC_BASE=https://ingest.globalmentorluk.com   (optional; default below)
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
SIGNING_KEY = os.environ.get("OAUTH_SIGNING_KEY", "").encode()
if not SIGNING_KEY:
    raise SystemExit("FATAL: OAUTH_SIGNING_KEY not set")
CONSENT_PASSWORD = os.environ.get("OAUTH_CONSENT_PASSWORD", "")
if not CONSENT_PASSWORD:
    raise SystemExit("FATAL: OAUTH_CONSENT_PASSWORD not set")

BASE = os.environ.get("PUBLIC_BASE", "https://ingest.globalmentorluk.com").rstrip("/")
RESOURCE = BASE + "/mcp"
SCOPES = ["whatsapp:read", "offline_access"]
ACCESS_TTL = 3600            # 1h
REFRESH_TTL = 30 * 24 * 3600  # 30d
CODE_TTL = 300               # 5m
REDIRECT_ALLOWLIST = {
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    # ChatGPT's legacy connector callback (still used by published apps).
    "https://chatgpt.com/connector_platform_oauth_redirect",
}
# ChatGPT mints a FRESH callback path per connector
# (https://chatgpt.com/connector/oauth/<callback_id>), so it can never match an
# exact-string allowlist. Allow a narrow path prefix on the ChatGPT host instead;
# the consent password still gates every authorization regardless of redirect.
_REDIRECT_HOSTS = {"chatgpt.com"}
_REDIRECT_PATH_PREFIXES = ("/connector/oauth/", "/aip/")
# Desktop/native clients get a loopback callback on an ephemeral port (RFC 8252).
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_PORT", "8085"))

import base64
import hashlib
import hmac
import json
import secrets
import time
import html
from urllib.parse import parse_qs, quote, urlparse

import uvicorn
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

import main  # registers all tools on main.mcp; sees WHATSAPP_ACCOUNTS from env

# --- read-only: remove every write/send tool ---
WRITE_TOOLS = ["send_message", "send_file", "send_audio_message", "download_media"]
for name in WRITE_TOOLS:
    main.mcp._tool_manager._tools.pop(name, None)
print("read-only MCP tools:", sorted(main.mcp._tool_manager._tools.keys()), flush=True)

# --- allowlisted send: one narrow write tool, group JIDs from allowlist.json ---
import mentee_send  # registers send_to_mentee_group + list_mentee_groups

# --- compat shim: upstream tools return dataclasses; mcp>=1.10 wants dicts ---
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

# --- drop structured-output schema on every read tool ---
# The upstream text tools (list_messages, get_message_context, get_*) return a
# PREFORMATTED STRING but are annotated `-> List[Dict[str, Any]]`, so mcp>=1.10
# derives a list-shaped output schema and then rejects the string with
# "Input should be a valid list [type=list_type]". Clear the output schema so the
# result is returned as plain text content (what these tools actually produce).
# Applied to all read tools — bulletproof against the upstream annotation mismatch.
for _tool in main.mcp._tool_manager._tools.values():
    object.__setattr__(_tool, "output_schema", None)
    _fm = getattr(_tool, "fn_metadata", None)
    if _fm is not None:
        if hasattr(_fm, "output_schema"):
            object.__setattr__(_fm, "output_schema", None)
        if hasattr(_fm, "output_model"):
            object.__setattr__(_fm, "output_model", None)


# ---------------------------------------------------------------------------
# Stateless signed tokens (HMAC-SHA256).  <b64url(payload)>.<b64url(sig)>
# ---------------------------------------------------------------------------
def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(payload: dict) -> str:
    body = _b64u(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64u(hmac.new(SIGNING_KEY, body.encode(), hashlib.sha256).digest())
    return body + "." + sig


def _verify(tok: str, typ: str):
    try:
        body, sig = tok.split(".", 1)
    except ValueError:
        return None
    expect = _b64u(hmac.new(SIGNING_KEY, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        payload = json.loads(_b64u_dec(body))
    except Exception:
        return None
    if payload.get("typ") != typ or payload.get("exp", 0) < time.time():
        return None
    return payload


async def _form(request: Request) -> dict:
    """Parse an application/x-www-form-urlencoded body without depending on
    python-multipart (keeps the runtime dependency-light)."""
    body = (await request.body()).decode("utf-8")
    return {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}


def _pkce_ok(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return hmac.compare_digest(_b64u(digest), challenge)


def _redirect_ok(uri: str) -> bool:
    """Exact allowlist, plus a narrow per-connector prefix for ChatGPT."""
    if uri in REDIRECT_ALLOWLIST:
        return True
    try:
        u = urlparse(uri)
    except Exception:
        return False
    if u.fragment or "@" in u.netloc:
        return False
    host = (u.hostname or "").lower()
    # RFC 8252 s7.3: native/desktop clients (ChatGPT app, Claude Code, mcp-remote)
    # listen on a loopback socket with an EPHEMERAL port and a random path, e.g.
    # http://127.0.0.1:63959/callback/dRmFLUYR9mxS. The port cannot be known in
    # advance, so it must not be pinned; http is correct for loopback. Safe here:
    # the code is still PKCE-bound and /authorize is still password-gated.
    if u.scheme == "http" and host in _LOOPBACK_HOSTS:
        return True
    return (
        u.scheme == "https"
        and u.port is None
        and host in _REDIRECT_HOSTS
        and any(u.path.startswith(p) for p in _REDIRECT_PATH_PREFIXES)
    )


def _issue_tokens(scope: str, aud: str = ""):
    now = int(time.time())
    base = {"scope": scope, "iat": now}
    if aud:
        base["aud"] = aud
    access = _sign({"typ": "access", "exp": now + ACCESS_TTL, **base})
    refresh = _sign({"typ": "refresh", "exp": now + REFRESH_TTL, **base})
    return JSONResponse(
        {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_TTL,
            "refresh_token": refresh,
            "scope": scope,
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


# ---------------------------------------------------------------------------
# OAuth / discovery handlers
# ---------------------------------------------------------------------------
async def meta_as(request: Request):
    return JSONResponse({
        "issuer": BASE,
        "authorization_endpoint": BASE + "/authorize",
        "token_endpoint": BASE + "/token",
        "registration_endpoint": BASE + "/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": SCOPES,
    })


async def meta_prm(request: Request):
    return JSONResponse({
        "resource": RESOURCE,
        "authorization_servers": [BASE],
        "scopes_supported": SCOPES,
        "bearer_methods_supported": ["header"],
    })


async def register(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    now = int(time.time())
    return JSONResponse({
        "client_id": "mcp-" + secrets.token_hex(8),
        "client_id_issued_at": now,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "redirect_uris": data.get("redirect_uris", []),
    }, status_code=201)


_FORM = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>WhatsApp (read-only) — authorize</title>
<style>body{{font-family:system-ui,sans-serif;background:#111;color:#eee;display:flex;
min-height:100vh;align-items:center;justify-content:center;margin:0}}
form{{background:#1c1c1c;padding:32px;border-radius:12px;width:320px;box-shadow:0 8px 30px #0008}}
h1{{font-size:17px;margin:0 0 4px}}p{{font-size:13px;color:#999;margin:0 0 20px}}
input[type=password]{{width:100%;padding:10px;border-radius:8px;border:1px solid #333;
background:#111;color:#eee;box-sizing:border-box;font-size:14px}}
button{{width:100%;padding:11px;margin-top:16px;border:0;border-radius:8px;
background:#d97757;color:#fff;font-size:14px;font-weight:600;cursor:pointer}}
.err{{color:#e5675a;font-size:13px;margin-top:12px}}</style></head><body>
<form method=post action="{base}/authorize">
<h1>WhatsApp (read-only)</h1>
<p>Bu bağlantıyı vermek için parolanı gir.</p>
<input type=password name=password placeholder=Parola autofocus>
{hidden}{err}
<button type=submit>İzin ver</button></form></body></html>"""


def _consent_page(params: dict, err: bool = False):
    fields = ["response_type", "client_id", "redirect_uri", "state",
              "code_challenge", "code_challenge_method", "scope", "resource"]
    hidden = "".join(
        '<input type=hidden name="{}" value="{}">'.format(
            f, html.escape(params.get(f, "") or "", quote=True))
        for f in fields
    )
    errhtml = '<div class=err>Parola yanlış.</div>' if err else ""
    return HTMLResponse(_FORM.format(base=BASE, hidden=hidden, err=errhtml),
                        status_code=401 if err else 200)


async def authorize(request: Request):
    if request.method == "GET":
        p = dict(request.query_params)
        if p.get("response_type") != "code" or not p.get("code_challenge") \
                or p.get("code_challenge_method") != "S256":
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        if not _redirect_ok(p.get("redirect_uri", "")):
            print("rejected redirect_uri:", p.get("redirect_uri", ""), flush=True)
            return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
        return _consent_page(p)

    form = await _form(request)
    if not hmac.compare_digest(form.get("password", ""), CONSENT_PASSWORD):
        return _consent_page(form, err=True)
    redirect_uri = form.get("redirect_uri", "")
    if not _redirect_ok(redirect_uri):
        print("rejected redirect_uri:", redirect_uri, flush=True)
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
    now = int(time.time())
    code = _sign({
        "typ": "code", "iat": now, "exp": now + CODE_TTL,
        "cc": form.get("code_challenge", ""),
        "ru": redirect_uri,
        "scope": form.get("scope") or "whatsapp:read",
        # ChatGPT sends RFC 8707 `resource` on /authorize and /token; carry it
        # through so it can be echoed back as the token audience.
        "aud": form.get("resource") or "",
    })
    sep = "&" if "?" in redirect_uri else "?"
    dest = "{}{}code={}".format(redirect_uri, sep, code)
    if form.get("state"):
        dest += "&state=" + quote(form["state"], safe="")
    return RedirectResponse(dest, status_code=302)


async def token(request: Request):
    form = await _form(request)
    grant = form.get("grant_type")

    def err(code):
        return JSONResponse({"error": code}, status_code=400,
                            headers={"Cache-Control": "no-store"})

    if grant == "authorization_code":
        p = _verify(form.get("code", ""), "code")
        if not p:
            return err("invalid_grant")
        if p.get("ru") != form.get("redirect_uri"):
            return err("invalid_grant")
        if not _pkce_ok(form.get("code_verifier", ""), p.get("cc", "")):
            return err("invalid_grant")
        return _issue_tokens(p.get("scope", "whatsapp:read"),
                             form.get("resource") or p.get("aud", ""))

    if grant == "refresh_token":
        p = _verify(form.get("refresh_token", ""), "refresh")
        if not p:
            return err("invalid_grant")
        return _issue_tokens(p.get("scope", "whatsapp:read"),
                             form.get("resource") or p.get("aud", ""))

    return err("unsupported_grant_type")


# ---------------------------------------------------------------------------
# Auth gate for /mcp (OAuth access token OR static bearer); public OAuth paths
# pass straight through.
# ---------------------------------------------------------------------------
_PUBLIC_PREFIXES = ("/.well-known/",)
_PUBLIC_EXACT = {"/register", "/authorize", "/token"}


class AuthGate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            public = path in _PUBLIC_EXACT or any(path.startswith(p) for p in _PUBLIC_PREFIXES)
            if not public:
                headers = dict(scope.get("headers") or [])
                auth = headers.get(b"authorization", b"").decode("latin-1")
                provided = auth[7:] if auth.startswith("Bearer ") else ""
                ok = (bool(TOKEN) and hmac.compare_digest(provided, TOKEN)) or \
                     bool(_verify(provided, "access"))
                if not ok:
                    ch = 'Bearer resource_metadata="{}/.well-known/oauth-protected-resource/mcp"'.format(BASE)
                    await send({
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"www-authenticate", ch.encode("latin-1")),
                                    (b"x-robots-tag", b"noindex, nofollow")],
                    })
                    await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                    return
        await self.app(scope, receive, send)


mcp_app = main.mcp.streamable_http_app()
# prepend OAuth + discovery routes (matched before the /mcp handler)
mcp_app.router.routes[0:0] = [
    Route("/.well-known/oauth-authorization-server", meta_as),
    Route("/.well-known/oauth-authorization-server/mcp", meta_as),
    Route("/.well-known/oauth-protected-resource", meta_prm),
    Route("/.well-known/oauth-protected-resource/mcp", meta_prm),
    Route("/register", register, methods=["POST"]),
    Route("/authorize", authorize, methods=["GET", "POST"]),
    Route("/token", token, methods=["POST"]),
]

app = AuthGate(mcp_app)

if __name__ == "__main__":
    # access_log=False: keep tokens/paths out of logs.
    uvicorn.run(app, host=HOST, port=PORT, log_level="info", access_log=False)
