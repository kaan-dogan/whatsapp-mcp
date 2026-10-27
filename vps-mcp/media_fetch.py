"""Read-only media download for the WhatsApp MCP: `download_media` + a one-shot URL.

The tool never puts file bytes in the MCP response (connectors truncate large
base64 payloads). It fetches and decrypts the attachment here, parks it on disk,
and returns a short-lived, single-use URL under /mcp/media/<token> that the
caller fetches with plain curl.

Why not the bridge's /api/download: it rebuilds the CDN path from the stored URL
but strips the query string (oh/oe signature), so every download returns 403 --
even for a file received yesterday. It also caches by sender-chosen filename
(collisions, path traversal). Instead we GET the stored mmg URL ourselves and
decrypt with the stored media key -- the same scheme whatsmeow uses -- and check
the plaintext SHA256 the bridge recorded, so a wrong or corrupted file cannot be
served.

Limits of this path, measured 2026-10-27: WhatsApp's CDN keeps media for roughly
a month. Older attachments answer 403 (URL signature expired) or 410 (deleted);
those come back as `media_gone`. Recovering them would need a re-upload request
to the phone (whatsmeow media retry), which lives in the bridge.

Tokens are deliberately STATEFUL (in-process dict), unlike the stateless OAuth
tokens in serve_http.py: single-use cannot be enforced by a signature alone. A
restart drops pending tokens; at a 10-minute TTL that is harmless.

Wire it up in serve_http.py next to `import mentee_send`, and prepend
`Route(MEDIA_ROUTE, serve_media)` + add MEDIA_PREFIX to the public paths.
"""

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone

import anyio
import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse

import main
from whatsapp import _resolve, guess_mimetype

MAX_BYTES = int(os.environ.get("MEDIA_MAX_BYTES", str(25 * 1024 * 1024)))
TTL = int(os.environ.get("MEDIA_URL_TTL", "600"))  # 10 min
SPOOL = os.environ.get("MEDIA_SPOOL_DIR", "/root/whatsapp/mcp/media-tmp")
BASE = os.environ.get("PUBLIC_BASE", "https://ingest.globalmentorluk.com").rstrip("/")
MEDIA_PREFIX = "/mcp/media/"
MEDIA_ROUTE = MEDIA_PREFIX + "{token}"
VALID_ACCOUNTS = ("main", "second")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "whisper-large-v3-turbo")
# The Groq key stays in ONE place -- meeting-ingest's .env, which already uses it --
# so a rotation there covers this tool too. GROQ_API_KEY in our env overrides.
GROQ_ENV_FILE = os.environ.get("GROQ_ENV_FILE", "/root/meeting-ingest/.env")


def _groq_key() -> str:
    key = os.environ.get("GROQ_API_KEY", "")
    if key:
        return key
    try:
        with open(GROQ_ENV_FILE) as fh:
            for line in fh:
                if line.startswith("GROQ_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""

_HKDF_INFO = {
    "image": b"WhatsApp Image Keys",
    "video": b"WhatsApp Video Keys",
    "audio": b"WhatsApp Audio Keys",
    "document": b"WhatsApp Document Keys",
}
_JID_RE = re.compile(r"^[0-9A-Za-z.:_-]+@[a-z.]+$")
_ID_RE = re.compile(r"^[0-9A-Za-z_-]{6,64}$")

_lock = threading.Lock()
_pending = {}  # token -> (path, filename, mimetype, expires_at)


class MediaError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _session_logged_in(db_path: str) -> bool:
    """A logout deletes whatsmeow_device; its row count is the session state."""
    wa_db = os.path.join(os.path.dirname(db_path), "whatsapp.db")
    try:
        conn = sqlite3.connect(f"file:{wa_db}?mode=ro", uri=True, timeout=5)
        try:
            return conn.execute("SELECT count(*) FROM whatsmeow_device").fetchone()[0] > 0
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def _load_row(db_path: str, message_id: str, chat_jid: str):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        return conn.execute(
            "SELECT media_type, filename, url, media_key, file_sha256, file_enc_sha256, file_length "
            "FROM messages WHERE id = ? AND chat_jid = ?",
            (message_id, chat_jid),
        ).fetchone()
    finally:
        conn.close()


def _decrypt(enc: bytes, media_key: bytes, media_type: str, sha: bytes, enc_sha: bytes) -> bytes:
    if enc_sha and hashlib.sha256(enc).digest() != enc_sha:
        raise MediaError("integrity_error", "Şifreli dosyanın SHA256'sı kayıtla uyuşmuyor.")
    keys = HKDF(hashes.SHA256(), 112, None, _HKDF_INFO[media_type]).derive(media_key)
    iv, cipher_key, mac_key = keys[:16], keys[16:48], keys[48:80]
    body, tag = enc[:-10], enc[-10:]
    if not hmac.compare_digest(hmac.new(mac_key, iv + body, hashlib.sha256).digest()[:10], tag):
        raise MediaError("integrity_error", "Dosyanın MAC doğrulaması başarısız.")
    dec = Cipher(algorithms.AES(cipher_key), modes.CBC(iv)).decryptor()
    plain = dec.update(body) + dec.finalize()
    plain = plain[: -plain[-1]]
    if sha and hashlib.sha256(plain).digest() != sha:
        raise MediaError("integrity_error", "Çözülen dosyanın SHA256'sı kayıtla uyuşmuyor.")
    return plain


def _fetch(url: str, logged_in: bool) -> bytes:
    try:
        resp = requests.get(url, timeout=60, stream=True, headers={"User-Agent": "WhatsApp/2.24"})
    except requests.RequestException as exc:
        raise MediaError("cdn_error", f"WhatsApp medya sunucusuna ulaşılamadı: {exc}")
    with resp:
        if resp.status_code in (403, 404, 410):
            if not logged_in:
                raise MediaError(
                    "qr_required",
                    f"Medya sunucuda yok (HTTP {resp.status_code}) ve WhatsApp oturumu düşmüş: "
                    "QR ile yeniden bağlanmak gerekiyor.",
                )
            raise MediaError(
                "media_gone",
                f"Medya WhatsApp sunucusunda artık yok (HTTP {resp.status_code}). "
                "WhatsApp eski ekleri ~1 ay sonra siler.",
            )
        if resp.status_code != 200:
            raise MediaError("cdn_error", f"WhatsApp medya sunucusu HTTP {resp.status_code} döndü.")
        buf = bytearray()
        for chunk in resp.iter_content(256 * 1024):
            buf += chunk
            if len(buf) > MAX_BYTES + 1024:
                raise MediaError("too_large", "Dosya 25 MB sınırını aşıyor.")
        return bytes(buf)


def _transcribe(data: bytes, filename: str) -> str:
    key = _groq_key()
    if not key:
        raise RuntimeError("GROQ_API_KEY bulunamadı")
    resp = requests.post(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {key}"},
        files={"file": (filename or "voice.ogg", data, "audio/ogg")},
        data={"model": GROQ_MODEL, "response_format": "json"},
        timeout=120,
    )
    resp.raise_for_status()
    return (resp.json().get("text") or "").strip()


def _purge_expired() -> None:
    now = time.time()
    with _lock:
        dead = [t for t, v in _pending.items() if v[3] < now]
        for t in dead:
            _unlink(_pending.pop(t)[0])
    # Files orphaned by a restart (the dict is gone, the files are not).
    try:
        for name in os.listdir(SPOOL):
            path = os.path.join(SPOOL, name)
            if os.path.getmtime(path) < now - TTL - 60:
                _unlink(path)
    except OSError:
        pass


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _download(message_id: str, chat_jid: str, account: str, transcribe: bool) -> dict:
    if account not in VALID_ACCOUNTS:
        raise MediaError("bad_request", f"account 'main' ya da 'second' olmalı, '{account}' geldi.")
    if not _ID_RE.match(message_id or "") or not _JID_RE.match(chat_jid or ""):
        raise MediaError("bad_request", "message_id ya da chat_jid biçimi geçersiz.")
    db_path, _ = _resolve(account)

    row = _load_row(db_path, message_id, chat_jid)
    if not row:
        raise MediaError("not_found", "Bu sohbette bu ID ile mesaj yok.")
    media_type, filename, url, media_key, sha, enc_sha, length = row
    if not media_type:
        raise MediaError("not_media", "Mesajda medya yok.")
    if media_type not in _HKDF_INFO:
        raise MediaError("not_media", f"Desteklenmeyen medya türü: {media_type}")
    if length and length > MAX_BYTES:
        raise MediaError("too_large", f"Dosya {length} bayt; sınır 25 MB.")
    if not url or not media_key:
        raise MediaError("incomplete_media_info", "Bridge bu mesajın indirme bilgisini kaydetmemiş.")

    plain = _decrypt(_fetch(url, _session_logged_in(db_path)), media_key, media_type, sha, enc_sha)
    if len(plain) > MAX_BYTES:
        raise MediaError("too_large", "Dosya 25 MB sınırını aşıyor.")

    mimetype = guess_mimetype(media_type, filename)
    safe_name = re.sub(r'[\x00-\x1f/\\"]', "_", filename or f"{media_type}_{message_id}") or "file"

    _purge_expired()
    os.makedirs(SPOOL, mode=0o700, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path = os.path.join(SPOOL, token)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(plain)
    expires = time.time() + TTL
    with _lock:
        _pending[token] = (path, safe_name, mimetype, expires)

    out = {
        "ok": True,
        "filename": safe_name,
        "mimetype": mimetype,
        "size_bytes": len(plain),
        "url": BASE + MEDIA_PREFIX + token,
        "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat(timespec="seconds"),
        "single_use": True,
    }
    if transcribe and media_type == "audio":
        try:
            out["transcript"] = _transcribe(plain, safe_name)
        except Exception as exc:  # the file itself is still downloadable
            out["transcript_error"] = str(exc)[:300]
    return out


@main.mcp.tool()
async def download_media(message_id: str, chat_jid: str, account: str = "main",
                         transcribe: bool = True) -> dict:
    """Download a WhatsApp attachment (document, image, video, voice note) — read-only.

    Returns metadata plus a short-lived (10 min), single-use HTTPS URL; fetch it with
    `curl -fsSL -o <file> <url>`. The file bytes are never inlined in this response.
    Voice notes (audio/ogg) also come back with a `transcript` field unless
    transcribe=False.

    On failure returns {"ok": false, "error": <code>, "detail": <text>}:
      media_gone   — WhatsApp deleted the file from its server (older than ~1 month)
      qr_required  — the WhatsApp session dropped; re-link by QR
      too_large    — over 25 MB
      not_found / not_media / incomplete_media_info / integrity_error / cdn_error / bad_request

    Args:
        message_id: ID shown in list_messages as "Message ID: …"
        chat_jid: chat shown in list_messages as "Chat JID: …"
        account: "main" or "second" (default "main")
        transcribe: transcribe voice notes (default True)
    """
    try:
        return await anyio.to_thread.run_sync(_download, message_id, chat_jid, account, transcribe)
    except MediaError as exc:
        return {"ok": False, "error": exc.code, "detail": exc.detail}


async def serve_media(request: Request):
    token = request.path_params.get("token", "")
    with _lock:
        entry = _pending.pop(token, None)
    if not entry or entry[3] < time.time():
        if entry:
            _unlink(entry[0])
        return JSONResponse({"error": "expired_or_used"}, status_code=410,
                            headers={"X-Robots-Tag": "noindex, nofollow"})
    path, filename, mimetype, _ = entry
    return FileResponse(
        path, media_type=mimetype, filename=filename,
        headers={"X-Content-Type-Options": "nosniff", "X-Robots-Tag": "noindex, nofollow",
                 "Cache-Control": "no-store"},
        background=BackgroundTask(_unlink, path),
    )
