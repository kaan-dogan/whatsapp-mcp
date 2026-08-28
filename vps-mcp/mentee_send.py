"""Allowlisted WhatsApp send tool for the Global Mentorluk mentee groups.

The read-only server strips `send_message`/`send_file`/`send_audio_message`/
`download_media` from the tool surface. This module adds back exactly one
narrow write capability: sending text to a group JID that appears verbatim in
an allowlist file on disk.

Why a separate tool instead of un-stripping `send_message`: the upstream tool
takes `recipient: str` and documents it as "phone number OR JID", so exposing
it hands the model every chat and every phone number on the account. The tool
below cannot express a recipient outside the allowlist.

Wire it up in serve_http.py, directly after the WRITE_TOOLS pop loop:

    import mentee_send  # registers send_to_mentee_group + list_mentee_groups

Fails closed: a missing, unreadable, or malformed allowlist denies every send.
"""

import json
import os
import re
import time

import main
from whatsapp import send_message as _wa_send
from whatsapp import send_file as _wa_send_file

ALLOWLIST_PATH = os.environ.get(
    "MENTEE_ALLOWLIST_PATH", "/root/whatsapp/mcp/allowlist.json"
)
AUDIT_PATH = os.environ.get(
    "MENTEE_AUDIT_PATH", "/root/whatsapp/mcp/send-audit.log"
)
MAX_LEN = int(os.environ.get("MENTEE_MAX_LEN", "2000"))
VALID_ACCOUNTS = ("main", "second")

# Files are only ever sent from the staging directory the outbox service writes
# to. A handle is a UUID and nothing else, so a path outside OUTBOX_ROOT cannot
# be expressed -- the tool never accepts a filesystem path from the caller.
OUTBOX_ROOT = os.environ.get("OUTBOX_ROOT", "/root/whatsapp/outbox")
OUTBOX_MAX_BYTES = int(os.environ.get("OUTBOX_MAX_BYTES", str(20 * 1024 * 1024)))
_HANDLE_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _load_allowlist() -> dict:
    """Read the allowlist fresh on every call so edits need no restart.

    Returns {jid: {"name": str, "account": str}}. Any failure returns {},
    which denies every send.
    """
    try:
        with open(ALLOWLIST_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception as exc:
        print(f"[mentee_send] allowlist unreadable ({exc}) - denying all sends", flush=True)
        return {}

    if not isinstance(raw, dict):
        print("[mentee_send] allowlist is not an object - denying all sends", flush=True)
        return {}

    entries = {}
    for jid, value in raw.items():
        if not isinstance(jid, str) or not jid.endswith("@g.us"):
            continue
        if isinstance(value, str):
            entries[jid] = {"name": value, "account": "main"}
        elif isinstance(value, dict):
            account = value.get("account", "main")
            if account not in VALID_ACCOUNTS:
                continue
            entries[jid] = {"name": str(value.get("name", jid)), "account": account}
    return entries


def _audit(jid, name, allowed, reason, message, account, dry_run, extra=None):
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "jid": jid,
        "name": name,
        "account": account,
        "allowed": allowed,
        "dry_run": dry_run,
        "reason": reason,
        "preview": (message or "")[:80],
    }
    if extra:
        record.update(extra)
    try:
        with open(AUDIT_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        # Never block a send because the audit file is unwritable, but make the
        # failure loud in the service log.
        print(f"[mentee_send] AUDIT WRITE FAILED ({exc}): {record}", flush=True)


@main.mcp.tool()
def list_mentee_groups() -> list:
    """List the WhatsApp groups this server is permitted to send messages to.

    Returns:
        A list of {jid, name, account} objects. Anything absent from this list
        cannot be messaged.
    """
    return [
        {"jid": jid, "name": entry["name"], "account": entry["account"]}
        for jid, entry in sorted(_load_allowlist().items(), key=lambda kv: kv[1]["name"])
    ]


@main.mcp.tool()
def send_to_mentee_group(chat_jid: str, message: str, dry_run: bool = False) -> dict:
    """Send a text message to one allowlisted Global Mentorluk mentee group.

    Only group JIDs present in the server's allowlist can be reached; phone
    numbers and direct chats are rejected outright. Call list_mentee_groups()
    to see the permitted JIDs - never guess a JID from a group name.

    Args:
        chat_jid: Group JID, e.g. "120363408193283158@g.us". Must match an
                  allowlist entry exactly.
        message: The message text.
        dry_run: When true, run every validation and report the outcome
                 without sending anything.

    Returns:
        {success, message, jid, name, dry_run}
    """
    allowlist = _load_allowlist()

    def deny(reason, name=None):
        _audit(chat_jid, name, False, reason, message, None, dry_run)
        return {
            "success": False,
            "message": reason,
            "jid": chat_jid,
            "name": name,
            "dry_run": dry_run,
        }

    if not isinstance(chat_jid, str) or not chat_jid:
        return deny("chat_jid must be a non-empty string")
    if not chat_jid.endswith("@g.us"):
        return deny("Only group JIDs (@g.us) may be messaged; direct chats are blocked")
    if chat_jid not in allowlist:
        return deny("JID is not on the allowlist; call list_mentee_groups() for permitted groups")
    if not isinstance(message, str) or not message.strip():
        return deny("message must be a non-empty string", allowlist[chat_jid]["name"])
    if len(message) > MAX_LEN:
        return deny(f"message exceeds {MAX_LEN} characters", allowlist[chat_jid]["name"])

    entry = allowlist[chat_jid]

    if dry_run:
        _audit(chat_jid, entry["name"], True, "dry run - not sent", message, entry["account"], True)
        return {
            "success": True,
            "message": f"Dry run: would send to {entry['name']} from account '{entry['account']}'",
            "jid": chat_jid,
            "name": entry["name"],
            "dry_run": True,
        }

    ok, status = _wa_send(chat_jid, message, account=entry["account"])
    _audit(chat_jid, entry["name"], ok, status, message, entry["account"], False)
    return {
        "success": ok,
        "message": status,
        "jid": chat_jid,
        "name": entry["name"],
        "dry_run": False,
    }

def _resolve_staged(handle):
    """Turn an outbox handle into (path, filename, size). Returns (None, reason)
    on any doubt -- the checks the outbox service already ran are repeated here
    because this is the step that actually puts a file in front of a student."""
    if not isinstance(handle, str) or not _HANDLE_RE.match(handle):
        return None, "handle must be a UUID returned by the outbox upload"

    directory = os.path.join(OUTBOX_ROOT, handle)
    if not os.path.isdir(directory):
        return None, "handle not found or already expired (staged files live 24h)"

    names = [n for n in os.listdir(directory) if not n.startswith(".")]
    if len(names) != 1:
        return None, f"staging dir holds {len(names)} files, expected exactly 1"

    path = os.path.join(directory, names[0])
    # realpath before the prefix test, so a symlink planted in the staging dir
    # cannot point the send at /root/.ssh/id_rsa.
    real = os.path.realpath(path)
    if not real.startswith(os.path.realpath(OUTBOX_ROOT) + os.sep):
        return None, "resolved path escapes the outbox root"
    if not os.path.isfile(real):
        return None, "staged entry is not a regular file"

    size = os.path.getsize(real)
    if size == 0:
        return None, "staged file is empty"
    if size > OUTBOX_MAX_BYTES:
        return None, f"staged file is {size} bytes, over the {OUTBOX_MAX_BYTES} limit"
    try:
        with open(real, "rb") as fh:
            if fh.read(5) != b"%PDF-":
                return None, "staged file is not a PDF"
    except OSError as exc:
        return None, f"staged file unreadable: {exc}"

    return (real, names[0], size), None


@main.mcp.tool()
def send_file_to_mentee_group(
    chat_jid: str, handle: str, caption: str = "", dry_run: bool = False
) -> dict:
    """Send a staged PDF to one allowlisted Global Mentorluk mentee group.

    The file must already have been uploaded to the outbox service, which
    returns the `handle` used here; this tool cannot read an arbitrary path.
    Only group JIDs on the allowlist can be reached - call list_mentee_groups()
    to see them, never guess a JID from a group name.

    The student sees the staged filename as the document title, so upload the
    file under the name they should read (e.g. "Ada - Danismanlik Raporu.pdf").

    Args:
        chat_jid: Group JID, e.g. "120363408193283158@g.us".
        handle: UUID returned by the outbox upload.
        caption: Optional text delivered with the document, as one message.
        dry_run: Run every validation and report what would happen, sending
                 nothing.

    Returns:
        {success, message, jid, name, filename, bytes, dry_run}
    """
    allowlist = _load_allowlist()

    def deny(reason, name=None, extra=None):
        _audit(chat_jid, name, False, reason, caption, None, dry_run,
               dict(extra or {}, kind="file", handle=handle))
        out = {"success": False, "message": reason, "jid": chat_jid,
               "name": name, "dry_run": dry_run}
        out.update(extra or {})
        return out

    if not isinstance(chat_jid, str) or not chat_jid:
        return deny("chat_jid must be a non-empty string")
    if not chat_jid.endswith("@g.us"):
        return deny("Only group JIDs (@g.us) may be messaged; direct chats are blocked")
    if chat_jid not in allowlist:
        return deny("JID is not on the allowlist; call list_mentee_groups() for permitted groups")

    entry = allowlist[chat_jid]

    if not isinstance(caption, str):
        return deny("caption must be a string", entry["name"])
    if len(caption) > MAX_LEN:
        return deny(f"caption exceeds {MAX_LEN} characters", entry["name"])

    staged, reason = _resolve_staged(handle)
    if staged is None:
        return deny(reason, entry["name"])
    path, filename, size = staged
    facts = {"filename": filename, "bytes": size}

    if dry_run:
        _audit(chat_jid, entry["name"], True, "dry run - not sent", caption,
               entry["account"], True, dict(facts, kind="file", handle=handle))
        return dict(
            {"success": True,
             "message": f"Dry run: would send {filename} ({size} bytes) to "
                        f"{entry['name']} from account '{entry['account']}'",
             "jid": chat_jid, "name": entry["name"], "dry_run": True},
            **facts,
        )

    ok, status = _wa_send_file(chat_jid, path, account=entry["account"], message=caption)
    _audit(chat_jid, entry["name"], ok, status, caption, entry["account"], False,
           dict(facts, kind="file", handle=handle))
    return dict(
        {"success": ok, "message": status, "jid": chat_jid,
         "name": entry["name"], "dry_run": False},
        **facts,
    )


print(
    f"[mentee_send] registered; allowlist={ALLOWLIST_PATH} "
    f"({len(_load_allowlist())} groups)",
    flush=True,
)
