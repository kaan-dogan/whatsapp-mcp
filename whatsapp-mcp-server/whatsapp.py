import sqlite3
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, List, Tuple
import os.path
import requests
import json
import mimetypes
import audio

# --- Multi-account support -------------------------------------------------
# A single MCP server can talk to several WhatsApp accounts, each backed by its
# own bridge (separate SQLite store + REST port). Accounts are configured via
# the WHATSAPP_ACCOUNTS env var (JSON), e.g.:
#   {"main":   {"db": "/abs/whatsapp-bridge/store/messages.db", "api": "http://localhost:8080/api"},
#    "second": {"db": "/abs/account2/store/messages.db",        "api": "http://localhost:8081/api"}}
# If WHATSAPP_ACCOUNTS is unset, we fall back to a single "main" account built
# from the legacy WHATSAPP_MESSAGES_DB / WHATSAPP_API_BASE_URL env vars (or the
# repo-relative defaults), so single-account setups keep working unchanged.

_DEFAULT_DB = os.environ.get(
    'WHATSAPP_MESSAGES_DB',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge', 'store', 'messages.db'),
)
_DEFAULT_API = os.environ.get('WHATSAPP_API_BASE_URL', "http://localhost:8080/api")


def _load_accounts() -> dict:
    raw = os.environ.get('WHATSAPP_ACCOUNTS')
    if raw:
        try:
            data = json.loads(raw)
            accounts = {}
            for name, cfg in data.items():
                db = cfg.get('db', _DEFAULT_DB)
                api = cfg.get('api', _DEFAULT_API)
                accounts[name] = (db, api)
            if accounts:
                return accounts
        except Exception as e:
            print(f"Failed to parse WHATSAPP_ACCOUNTS, falling back to single account: {e}")
    return {"main": (_DEFAULT_DB, _DEFAULT_API)}


ACCOUNTS = _load_accounts()
DEFAULT_ACCOUNT = "main" if "main" in ACCOUNTS else next(iter(ACCOUNTS))


def _resolve(account: Optional[str]) -> Tuple[str, str]:
    """Resolve an account name to its (db_path, api_base_url)."""
    if not account:
        account = DEFAULT_ACCOUNT
    if account not in ACCOUNTS:
        raise ValueError(
            f"Unknown WhatsApp account '{account}'. Available accounts: {list(ACCOUNTS.keys())}"
        )
    return ACCOUNTS[account]


def list_accounts() -> List[str]:
    """Return the configured account names."""
    return list(ACCOUNTS.keys())


# --- @lid identity resolution ---------------------------------------------
# Around 2026-06-22 WhatsApp migrated this account to opaque LID (`@lid`) JIDs.
# After that, DMs are stored keyed by a LID instead of the phone number, and the
# old phone-number-named chats froze. The bridge's whatsmeow store (sibling
# whatsapp.db) still holds `whatsmeow_lid_map` (lid<->pn) and `whatsmeow_contacts`
# (names), so we can map a LID back to the friendly name the user saved. We build
# {lid -> name} once per account (cached), preferring the user's saved chat name,
# then contact full/first/push name.
import time as _time
import unicodedata as _ud

_LID_CACHE = {}          # messages_db path -> (checked_at, db_stamp, index)
_LID_RECHECK = 2.0       # seconds between disk-change checks

# Turkish dotless/dotted i survive NFKD unchanged, so fold them explicitly.
_FOLD_MAP = str.maketrans({"\u0131": "i", "\u0130": "i", "I": "i"})


def _norm(value) -> str:
    """Fold a string for accent- and case-insensitive matching.

    SQLite's own LOWER()/LIKE fold ASCII only, so "Bet\u00fcl" never matched
    "betul" and "\u0130STANBUL" never matched "istanbul". Every name and content
    comparison goes through this one function, so search behaves identically in
    every tool.
    """
    if value is None:
        return ""
    text = str(value).translate(_FOLD_MAP).casefold()
    text = _ud.normalize("NFKD", text)
    return "".join(ch for ch in text if not _ud.combining(ch))


def _db_stamp(*paths) -> tuple:
    """Cheap change detector: (size, mtime) for each file, None when missing."""
    out = []
    for path in paths:
        try:
            st = os.stat(path)
            out.append((st.st_size, st.st_mtime_ns))
        except OSError:
            out.append(None)
    return tuple(out)


def _load_lid_index(db_path: str) -> dict:
    """Build and cache {"lid_name": {lid: name}, "pn_lids": {pn: [lid, ...]}}.

    Rebuilt whenever either database changes on disk (checked at most every
    _LID_RECHECK seconds), so a newly saved contact or a fresh LID mapping shows
    up without restarting the server.
    """
    now = _time.time()
    cached = _LID_CACHE.get(db_path)
    if cached and now - cached[0] < _LID_RECHECK:
        return cached[2]

    wa_db = os.path.join(os.path.dirname(os.path.abspath(db_path)), "whatsapp.db")
    stamp = _db_stamp(db_path, wa_db)
    if cached and cached[1] == stamp:
        _LID_CACHE[db_path] = (now, stamp, cached[2])
        return cached[2]

    lid_name: dict = {}
    pn_lids: dict = {}
    lid_pushname: dict = {}
    try:
        pn_name: dict = {}
        mc = sqlite3.connect(db_path)
        try:
            for jid, nm in mc.execute(
                "SELECT jid, name FROM chats "
                "WHERE jid LIKE '%@s.whatsapp.net' AND name IS NOT NULL AND name <> ''"
            ):
                if nm and not str(nm).isdigit():
                    pn_name.setdefault(jid.split("@")[0], nm)
        finally:
            mc.close()

        if os.path.exists(wa_db):
            wc = sqlite3.connect(wa_db)
            try:
                for tj, full, first, push in wc.execute(
                    "SELECT their_jid, full_name, first_name, push_name FROM whatsmeow_contacts"
                ):
                    ident = str(tj or "").split("@")[0]
                    nm = full or first or push
                    if not ident or not nm or str(nm).isdigit():
                        continue
                    if str(tj).endswith("@lid"):
                        # A LID-keyed contact row usually carries only the push
                        # name -- what the other person calls themselves. That
                        # must never outrank the name the user saved, so hold it
                        # aside and apply it below only where nothing else won.
                        lid_pushname.setdefault(ident, nm)
                    else:
                        pn_name.setdefault(ident, nm)

                for lid, pn in wc.execute("SELECT lid, pn FROM whatsmeow_lid_map"):
                    lid_b = str(lid).split("@")[0]
                    pn_b = str(pn).split("@")[0]
                    if not lid_b or not pn_b:
                        continue
                    peers = pn_lids.setdefault(pn_b, [])
                    if lid_b not in peers:
                        peers.append(lid_b)
                    nm = pn_name.get(pn_b)
                    if nm:
                        lid_name.setdefault(lid_b, nm)

                # Gap filler only: LIDs the map could not tie back to a saved
                # name still get the push name rather than a bare number.
                for lid_b, nm in lid_pushname.items():
                    lid_name.setdefault(lid_b, nm)
            finally:
                wc.close()
    except sqlite3.Error as e:
        print(f"lid index load error: {e}")

    index = {"lid_name": lid_name, "pn_lids": pn_lids}
    _LID_CACHE[db_path] = (now, stamp, index)
    return index


def _load_lid_names(db_path: str) -> dict:
    """{bare_lid: name}, kept for callers that only need the name map."""
    return _load_lid_index(db_path)["lid_name"]


def resolve_lid_name(identifier: Optional[str], db_path: str, fallback=None):
    """Map a LID identifier (bare `<lid>` or `<lid>@lid`) to the saved contact
    name, e.g. 161800091918435 -> "Babam Turkiye". Returns fallback if unknown."""
    if not identifier:
        return fallback
    bare = str(identifier).split("@")[0]
    return _load_lid_index(db_path)["lid_name"].get(bare, fallback)


def display_name(jid: Optional[str], raw_name, db_path: str):
    """The single name a caller ever sees for a chat.

    Filtering and sorting run through this same function (exposed to SQL as
    wa_name()), so a chat can never be visible in a listing yet invisible to a
    search for the very name that listing showed.
    """
    resolved = resolve_lid_name(jid, db_path, fallback=raw_name)
    if resolved is None or str(resolved).strip() == "":
        return raw_name
    return resolved


def jid_aliases(identifier: Optional[str], db_path: str) -> List[str]:
    """Every bare identifier that refers to the same person: the phone-number
    form plus every LID it has been migrated to, in both directions."""
    bare = str(identifier or "").split("@")[0]
    if not bare:
        return []
    index = _load_lid_index(db_path)
    out = [bare]
    for lid in index["pn_lids"].get(bare, []):
        if lid not in out:
            out.append(lid)
    for pn, lids in index["pn_lids"].items():
        if bare in lids and pn not in out:
            out.append(pn)
    return out


def _connect(db_path: str) -> sqlite3.Connection:
    """Open the messages DB with the shared name helpers registered.

    wa_name(jid, name)   -> the display name, byte-identical to what tools return
    wa_match(hay, needle) -> 1 when needle occurs in hay, accent/case-folded

    Every read goes through here, so a query can never filter on a different
    notion of "name" than the one it returns.
    """
    conn = sqlite3.connect(db_path)
    conn.create_function("wa_name", 2, lambda jid, nm: display_name(jid, nm, db_path))
    conn.create_function("wa_match", 2, lambda hay, needle: 1 if _norm(needle) in _norm(hay) else 0)
    return conn

# ---------------------------------------------------------------------------

@dataclass
class Message:
    timestamp: datetime
    sender: str
    content: str
    is_from_me: bool
    chat_jid: str
    id: str
    chat_name: Optional[str] = None
    media_type: Optional[str] = None
    filename: Optional[str] = None
    file_length: Optional[int] = None

@dataclass
class Chat:
    jid: str
    name: Optional[str]
    last_message_time: Optional[datetime]
    last_message: Optional[str] = None
    last_sender: Optional[str] = None
    last_is_from_me: Optional[bool] = None

    @property
    def is_group(self) -> bool:
        """Determine if chat is a group based on JID pattern."""
        return self.jid.endswith("@g.us")

@dataclass
class Contact:
    phone_number: str
    name: Optional[str]
    jid: str

@dataclass
class MessageContext:
    message: Message
    before: List[Message]
    after: List[Message]

def get_sender_name(sender_jid: str, db_path: str) -> str:
    # @lid senders: the chats.name lookup would return an opaque push number, so
    # resolve via the LID map first (e.g. 161800091918435 -> "Babam Turkiye").
    _lid = resolve_lid_name(sender_jid, db_path)
    if _lid:
        return _lid
    try:
        conn = _connect(db_path)
        cursor = conn.cursor()

        # First try matching by exact JID
        cursor.execute("""
            SELECT name
            FROM chats
            WHERE jid = ?
            LIMIT 1
        """, (sender_jid,))

        result = cursor.fetchone()

        # If no result, try looking for the number within JIDs
        if not result:
            # Extract the phone number part if it's a JID
            if '@' in sender_jid:
                phone_part = sender_jid.split('@')[0]
            else:
                phone_part = sender_jid

            cursor.execute("""
                SELECT name
                FROM chats
                WHERE jid LIKE ?
                LIMIT 1
            """, (f"%{phone_part}%",))

            result = cursor.fetchone()

        if result and result[0]:
            return result[0]
        else:
            return sender_jid

    except sqlite3.Error as e:
        print(f"Database error while getting sender name: {e}")
        return sender_jid
    finally:
        if 'conn' in locals():
            conn.close()

# The bridge stores no mimetype, so it is derived from the filename it kept.
# Voice notes are always Opus-in-Ogg; the bridge names every audio file *.ogg.
_MEDIA_DEFAULT_MIME = {
    "image": "image/jpeg",
    "video": "video/mp4",
    "audio": "audio/ogg",
    "document": "application/octet-stream",
}


def guess_mimetype(media_type: Optional[str], filename: Optional[str]) -> str:
    if media_type == "audio":
        return "audio/ogg"
    guessed = mimetypes.guess_type(filename or "")[0]
    return guessed or _MEDIA_DEFAULT_MIME.get(media_type or "", "application/octet-stream")


def human_size(n: Optional[int]) -> str:
    if not n:
        return "?"
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def media_meta_tag(media_type: Optional[str], filename: Optional[str], file_length: Optional[int]) -> str:
    """`[filename: X | mimetype: Y | size: Z]` -- enough to say "they sent this" without downloading."""
    return (f"[filename: {filename or '?'} | mimetype: {guess_mimetype(media_type, filename)}"
            f" | size: {human_size(file_length)}{f' ({file_length} bytes)' if file_length else ''}] ")


def format_message(message: Message, show_chat_info: bool = True, db_path: Optional[str] = None) -> None:
    """Print a single message with consistent formatting."""
    output = ""

    chat_disp = message.chat_name
    if db_path and getattr(message, "chat_jid", None):
        chat_disp = resolve_lid_name(message.chat_jid, db_path, fallback=message.chat_name)
    if show_chat_info and chat_disp:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] Chat: {chat_disp} "
    else:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] "

    content_prefix = ""
    if hasattr(message, 'media_type') and message.media_type:
        # Keep the first bracket byte-identical: routines parse it for the IDs.
        content_prefix = f"[{message.media_type} - Message ID: {message.id} - Chat JID: {message.chat_jid}] "
        content_prefix += media_meta_tag(message.media_type, message.filename, message.file_length)
        if message.content:
            content_prefix += "caption: "

    try:
        sender_name = get_sender_name(message.sender, db_path) if not message.is_from_me else "Me"
        output += f"From: {sender_name}: {content_prefix}{message.content}\n"
    except Exception as e:
        print(f"Error formatting message: {e}")
    return output

def format_messages_list(messages: List[Message], show_chat_info: bool = True, db_path: Optional[str] = None) -> None:
    output = ""
    if not messages:
        output += "No messages to display."
        return output

    for message in messages:
        output += format_message(message, show_chat_info, db_path)
    return output

def list_messages(
    after: Optional[str] = None,
    before: Optional[str] = None,
    sender_phone_number: Optional[str] = None,
    chat_jid: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_context: bool = True,
    context_before: int = 1,
    context_after: int = 1,
    account: Optional[str] = None
) -> List[Message]:
    """Get messages matching the specified criteria with optional context."""
    db_path, _ = _resolve(account)
    try:
        conn = _connect(db_path)
        cursor = conn.cursor()

        # Build base query
        query_parts = ["SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type, messages.filename, messages.file_length FROM messages"]
        query_parts.append("JOIN chats ON messages.chat_jid = chats.jid")
        where_clauses = []
        params = []

        # Add filters
        if after:
            try:
                after = datetime.fromisoformat(after)
            except ValueError:
                raise ValueError(f"Invalid date format for 'after': {after}. Please use ISO-8601 format.")

            where_clauses.append("messages.timestamp > ?")
            params.append(after)

        if before:
            try:
                before = datetime.fromisoformat(before)
            except ValueError:
                raise ValueError(f"Invalid date format for 'before': {before}. Please use ISO-8601 format.")

            where_clauses.append("messages.timestamp < ?")
            params.append(before)

        if sender_phone_number:
            where_clauses.append("messages.sender = ?")
            params.append(sender_phone_number)

        if chat_jid:
            where_clauses.append("messages.chat_jid = ?")
            params.append(chat_jid)

        if query:
            where_clauses.append("wa_match(messages.content, ?) = 1")
            params.append(query)

        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))

        # Add pagination
        offset = page * limit
        query_parts.append("ORDER BY messages.timestamp DESC")
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])

        cursor.execute(" ".join(query_parts), tuple(params))
        messages = cursor.fetchall()

        result = []
        for msg in messages:
            message = Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7],
                filename=msg[8],
                file_length=msg[9]
            )
            result.append(message)

        if include_context and result:
            # Add context for each message
            messages_with_context = []
            for msg in result:
                context = get_message_context(msg.id, context_before, context_after, account)
                messages_with_context.extend(context.before)
                messages_with_context.append(context.message)
                messages_with_context.extend(context.after)

            return format_messages_list(messages_with_context, show_chat_info=True, db_path=db_path)

        # Format and display messages without context
        return format_messages_list(result, show_chat_info=True, db_path=db_path)

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5,
    account: Optional[str] = None
) -> MessageContext:
    """Get context around a specific message."""
    db_path, _ = _resolve(account)
    try:
        conn = _connect(db_path)
        cursor = conn.cursor()

        # Get the target message first
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.chat_jid, messages.media_type, messages.filename, messages.file_length
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.id = ?
        """, (message_id,))
        msg_data = cursor.fetchone()

        if not msg_data:
            raise ValueError(f"Message with ID {message_id} not found")

        target_message = Message(
            timestamp=datetime.fromisoformat(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[8],
            filename=msg_data[9],
            file_length=msg_data[10]
        )

        # Get messages before
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type, messages.filename, messages.file_length
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp < ?
            ORDER BY messages.timestamp DESC
            LIMIT ?
        """, (msg_data[7], msg_data[0], before))

        before_messages = []
        for msg in cursor.fetchall():
            before_messages.append(Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7],
                filename=msg[8],
                file_length=msg[9]
            ))

        # Get messages after
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type, messages.filename, messages.file_length
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp > ?
            ORDER BY messages.timestamp ASC
            LIMIT ?
        """, (msg_data[7], msg_data[0], after))

        after_messages = []
        for msg in cursor.fetchall():
            after_messages.append(Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7],
                filename=msg[8],
                file_length=msg[9]
            ))

        return MessageContext(
            message=target_message,
            before=before_messages,
            after=after_messages
        )

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        raise
    finally:
        if 'conn' in locals():
            conn.close()


def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active",
    account: Optional[str] = None
) -> List[Chat]:
    """Get chats matching the specified criteria."""
    db_path, _ = _resolve(account)
    try:
        conn = _connect(db_path)
        cursor = conn.cursor()

        # Build base query. The last-message columns only exist when the
        # messages table is actually joined -- selecting them unconditionally
        # made include_last_message=False fail with "no such column" and return
        # an empty list instead of the chats it was asked for.
        if include_last_message:
            query_parts = ["""
                SELECT
                    chats.jid,
                    chats.name,
                    chats.last_message_time,
                    messages.content as last_message,
                    messages.sender as last_sender,
                    messages.is_from_me as last_is_from_me
                FROM chats
                LEFT JOIN messages ON chats.jid = messages.chat_jid
                AND chats.last_message_time = messages.timestamp
            """]
        else:
            query_parts = ["""
                SELECT
                    chats.jid,
                    chats.name,
                    chats.last_message_time,
                    NULL as last_message,
                    NULL as last_sender,
                    NULL as last_is_from_me
                FROM chats
            """]

        where_clauses = []
        params = []

        if query:
            where_clauses.append(
                "(wa_match(wa_name(chats.jid, chats.name), ?) = 1 OR chats.jid LIKE ?)"
            )
            params.extend([query, f"%{query}%"])

        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))

        # Add sorting
        order_by = (
            "chats.last_message_time DESC" if sort_by == "last_active"
            else "wa_name(chats.jid, chats.name)"
        )
        query_parts.append(f"ORDER BY {order_by}")

        # Add pagination
        offset = (page ) * limit
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])

        cursor.execute(" ".join(query_parts), tuple(params))
        chats = cursor.fetchall()

        result = []
        for chat_data in chats:
            chat = Chat(
                jid=chat_data[0],
                name=display_name(chat_data[0], chat_data[1], db_path),
                last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
                last_message=chat_data[3],
                last_sender=chat_data[4],
                last_is_from_me=chat_data[5]
            )
            result.append(chat)

        return result

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def search_contacts(query: str, account: Optional[str] = None) -> List[Contact]:
    """Search contacts by name or phone number."""
    db_path, _ = _resolve(account)
    try:
        conn = _connect(db_path)
        cursor = conn.cursor()

        # Names are matched accent- and case-folded via wa_match; the raw
        # pattern is only used for the phone-number/JID half of the search.
        search_pattern = '%' + query + '%'

        cursor.execute("""
            SELECT DISTINCT
                jid,
                name,
                last_message_time
            FROM chats
            WHERE
                (wa_match(wa_name(jid, name), ?) = 1 OR jid LIKE ?)
                AND jid NOT LIKE '%@g.us'
            ORDER BY last_message_time DESC
            LIMIT 50
        """, (query, search_pattern))

        contacts = cursor.fetchall()

        result = []
        for contact_data in contacts:
            contact = Contact(
                phone_number=contact_data[0].split('@')[0],
                name=display_name(contact_data[0], contact_data[1], db_path),
                jid=contact_data[0]
            )
            result.append(contact)

        return result

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_contact_chats(jid: str, limit: int = 20, page: int = 0, account: Optional[str] = None) -> List[Chat]:
    """Get all chats involving the contact.

    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    db_path, _ = _resolve(account)
    try:
        conn = _connect(db_path)
        cursor = conn.cursor()

        # Match every JID this contact owns (phone-number form and LIDs), so
        # the history does not split in half at a LID migration.
        aliases = jid_aliases(jid, db_path) or [str(jid).split("@")[0]]
        alias_clause = " OR ".join(
            ["m.sender LIKE ? OR c.jid LIKE ?"] * len(aliases)
        )
        alias_params = []
        for alias in aliases:
            alias_params.extend([f"{alias}@%", f"{alias}@%"])

        cursor.execute(f"""
            SELECT DISTINCT
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
            JOIN messages m ON c.jid = m.chat_jid
            WHERE m.sender = ? OR c.jid = ? OR {alias_clause}
            ORDER BY c.last_message_time DESC
            LIMIT ? OFFSET ?
        """, tuple([jid, jid] + alias_params + [limit, page * limit]))

        chats = cursor.fetchall()

        result = []
        for chat_data in chats:
            chat = Chat(
                jid=chat_data[0],
                name=display_name(chat_data[0], chat_data[1], db_path),
                last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
                last_message=chat_data[3],
                last_sender=chat_data[4],
                last_is_from_me=chat_data[5]
            )
            result.append(chat)

        return result

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_last_interaction(jid: str, account: Optional[str] = None) -> str:
    """Get most recent message involving the contact."""
    db_path, _ = _resolve(account)
    try:
        conn = _connect(db_path)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                m.timestamp,
                m.sender,
                c.name,
                m.content,
                m.is_from_me,
                c.jid,
                m.id,
                m.media_type,
                m.filename,
                m.file_length
            FROM messages m
            JOIN chats c ON m.chat_jid = c.jid
            WHERE m.sender = ? OR c.jid = ?
            ORDER BY m.timestamp DESC
            LIMIT 1
        """, (jid, jid))

        msg_data = cursor.fetchone()

        if not msg_data:
            return None

        message = Message(
            timestamp=datetime.fromisoformat(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[7],
            filename=msg_data[8],
            file_length=msg_data[9]
        )

        return format_message(message, db_path=db_path)

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_chat(chat_jid: str, include_last_message: bool = True, account: Optional[str] = None) -> Optional[Chat]:
    """Get chat metadata by JID."""
    db_path, _ = _resolve(account)
    try:
        conn = _connect(db_path)
        cursor = conn.cursor()

        query = """
            SELECT
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
        """

        if include_last_message:
            query += """
                LEFT JOIN messages m ON c.jid = m.chat_jid
                AND c.last_message_time = m.timestamp
            """

        query += " WHERE c.jid = ?"

        cursor.execute(query, (chat_jid,))
        chat_data = cursor.fetchone()

        if not chat_data:
            return None

        return Chat(
            jid=chat_data[0],
            name=display_name(chat_data[0], chat_data[1], db_path),
            last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
            last_message=chat_data[3],
            last_sender=chat_data[4],
            last_is_from_me=chat_data[5]
        )

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_direct_chat_by_contact(sender_phone_number: str, account: Optional[str] = None) -> Optional[Chat]:
    """Get chat metadata by sender phone number."""
    db_path, _ = _resolve(account)
    try:
        conn = _connect(db_path)
        cursor = conn.cursor()

        # One person can own several JIDs at once: the original phone-number
        # form plus every LID WhatsApp migrated them to. Match all of them and
        # return the most recently active chat, so a lookup by phone number
        # never lands on a chat that froze at the migration.
        aliases = jid_aliases(sender_phone_number, db_path)
        if aliases:
            clauses = " OR ".join(["c.jid LIKE ?"] * len(aliases))
            params = [f"{alias}@%" for alias in aliases]
        else:
            clauses = "c.jid LIKE ?"
            params = [f"%{sender_phone_number}%"]

        cursor.execute(f"""
            SELECT
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
            LEFT JOIN messages m ON c.jid = m.chat_jid
                AND c.last_message_time = m.timestamp
            WHERE ({clauses}) AND c.jid NOT LIKE '%@g.us'
            ORDER BY c.last_message_time DESC
            LIMIT 1
        """, tuple(params))

        chat_data = cursor.fetchone()

        if not chat_data:
            return None

        return Chat(
            jid=chat_data[0],
            name=display_name(chat_data[0], chat_data[1], db_path),
            last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
            last_message=chat_data[3],
            last_sender=chat_data[4],
            last_is_from_me=chat_data[5]
        )

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()

def send_message(recipient: str, message: str, account: Optional[str] = None) -> Tuple[bool, str]:
    _, api_base_url = _resolve(account)
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"

        url = f"{api_base_url}/send"
        payload = {
            "recipient": recipient,
            "message": message,
        }

        response = requests.post(url, json=payload)

        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"

    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_file(recipient: str, media_path: str, account: Optional[str] = None,
              message: str = "") -> Tuple[bool, str]:
    """Send a file. `message` rides along as the media caption -- the bridge puts
    it on the DocumentMessage, so the file and its explanation arrive as one
    message instead of two."""
    _, api_base_url = _resolve(account)
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"

        if not media_path:
            return False, "Media path must be provided"

        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"

        url = f"{api_base_url}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path,
            "message": message,
        }

        response = requests.post(url, json=payload)

        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"

    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_audio_message(recipient: str, media_path: str, account: Optional[str] = None) -> Tuple[bool, str]:
    _, api_base_url = _resolve(account)
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"

        if not media_path:
            return False, "Media path must be provided"

        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"

        if not media_path.endswith(".ogg"):
            try:
                media_path = audio.convert_to_opus_ogg_temp(media_path)
            except Exception as e:
                return False, f"Error converting file to opus ogg. You likely need to install ffmpeg: {str(e)}"

        url = f"{api_base_url}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }

        response = requests.post(url, json=payload)

        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"

    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def download_media(message_id: str, chat_jid: str, account: Optional[str] = None) -> Optional[str]:
    """Download media from a message and return the local file path.

    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message

    Returns:
        The local file path if download was successful, None otherwise
    """
    _, api_base_url = _resolve(account)
    try:
        url = f"{api_base_url}/download"
        payload = {
            "message_id": message_id,
            "chat_jid": chat_jid
        }

        response = requests.post(url, json=payload)

        if response.status_code == 200:
            result = response.json()
            if result.get("success", False):
                path = result.get("path")
                print(f"Media downloaded successfully: {path}")
                return path
            else:
                print(f"Download failed: {result.get('message', 'Unknown error')}")
                return None
        else:
            print(f"Error: HTTP {response.status_code} - {response.text}")
            return None

    except requests.RequestException as e:
        print(f"Request error: {str(e)}")
        return None
    except json.JSONDecodeError:
        print(f"Error parsing response: {response.text}")
        return None
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        return None
