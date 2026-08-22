#!/usr/bin/env python3
"""Search-integrity self-check for the WhatsApp MCP server.

The failure this guards against is silent: WhatsApp migrated the accounts to
opaque `@lid` JIDs in June 2026, the real names moved into the whatsmeow LID
map, and every name-based query kept filtering on the raw `chats.name` column.
Listings still showed names, so nothing looked broken -- searches just returned
nothing, and any workflow that starts from a name ("what did I write to X?")
came back empty all the way down.

So the invariant worth testing is not "the database has rows", it is:

    anything a listing shows by name must be findable by that same name.

Run with no arguments to check every configured account:

    python3 selfcheck.py

Exits 0 when every account passes, 1 otherwise, and prints one line per check.
Safe to run against production: it only reads.
"""
import io
import os
import sys
import traceback

# Load ./.env exactly the way serve_http.py does (KEY=VALUE, first '=' wins),
# before importing whatsapp -- it reads WHATSAPP_ACCOUNTS at import time.
# Parsing the file here rather than sourcing it in the shell is deliberate:
# WHATSAPP_ACCOUNTS is unquoted JSON, and `. ./.env` mangles the braces into
# something that no longer parses, which makes this check fail for a reason
# that has nothing to do with the thing it is testing.
_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_ENV):
    with io.open(_ENV, encoding="utf-8") as _fh:
        for _line in _fh:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

import whatsapp as wa


class Failure(Exception):
    pass


def check_folding():
    """Turkish and accented text must fold before it is compared."""
    cases = [
        ("Betül Danış", "betul"),
        ("Betül Danış", "DANIS"),
        ("İSTANBUL", "istanbul"),
        ("Çınar", "cinar"),
        ("Şeyma", "seyma"),
        ("Gökçe", "GOKCE"),
    ]
    norm = getattr(wa, "_norm", None)
    if norm is None:
        raise Failure("whatsapp.py has no _norm() -- running a build without folded matching")
    for haystack, needle in cases:
        if norm(needle) not in norm(haystack):
            raise Failure(f"{needle!r} should fold into {haystack!r}")
    return f"{len(cases)} folding cases"


def check_name_roundtrip(account, sample=8):
    """Every chat a listing names must be findable by that name."""
    chats = wa.list_chats(limit=40, include_last_message=False, account=account)
    if not chats:
        raise Failure("list_chats returned nothing at all")

    named = [c for c in chats if c.name and not str(c.name).replace(" ", "").isdigit()]
    if not named:
        raise Failure(f"{len(chats)} chats, none with a resolved name -- LID map is empty")

    checked = 0
    for chat in named[:sample]:
        needle = str(chat.name).split()[0]
        hits = wa.list_chats(limit=50, query=needle, include_last_message=False, account=account)
        if not any(h.jid == chat.jid for h in hits):
            raise Failure(
                f"list_chats shows {chat.jid} as {chat.name!r} but "
                f"query={needle!r} does not return it"
            )
        checked += 1
    return f"{checked}/{len(named)} named chats round-trip"


def check_contact_search(account, sample=5):
    """search_contacts must find the DMs that list_chats just showed."""
    chats = wa.list_chats(limit=40, include_last_message=False, account=account)
    dms = [
        c for c in chats
        if not c.is_group and c.name and not str(c.name).replace(" ", "").isdigit()
    ]
    if not dms:
        raise Failure("no named direct chats to search for")

    checked = 0
    for chat in dms[:sample]:
        needle = str(chat.name).split()[0]
        hits = wa.search_contacts(needle, account=account)
        if not any(h.jid == chat.jid for h in hits):
            raise Failure(
                f"search_contacts({needle!r}) misses {chat.jid} "
                f"which list_chats names {chat.name!r}"
            )
        if any(h.name and str(h.name).isdigit() for h in hits):
            raise Failure(f"search_contacts({needle!r}) returned an unresolved numeric name")
        checked += 1
    return f"{checked}/{len(dms)} contacts findable by name"


def check_liveness(account):
    """A phone-number lookup must land on the live chat, not a pre-migration one."""
    chats = [
        c for c in wa.list_chats(limit=40, include_last_message=False, account=account)
        if not c.is_group and c.last_message_time
    ]
    if not chats:
        raise Failure("no direct chats with a timestamp")

    newest = chats[0]
    if not hasattr(wa, "jid_aliases"):
        raise Failure("whatsapp.py has no jid_aliases() -- running a build without alias lookup")
    aliases = wa.jid_aliases(newest.jid, db_path=wa._resolve(account)[0])
    for alias in aliases:
        found = wa.get_direct_chat_by_contact(alias, account=account)
        if not found:
            raise Failure(f"get_direct_chat_by_contact({alias!r}) returned nothing")
        if found.last_message_time and found.last_message_time < newest.last_message_time:
            raise Failure(
                f"get_direct_chat_by_contact({alias!r}) returned a stale chat "
                f"({found.jid} @ {found.last_message_time}) while {newest.jid} "
                f"is active at {newest.last_message_time}"
            )
    return f"{len(aliases)} alias(es) resolve to the live chat"


def check_name_precedence(account, sample=25):
    """A name the user saved must outrank the name the contact chose.

    whatsmeow stores both: full_name is what you saved in your address book,
    push_name is what the other person calls themselves, and a LID-keyed
    contact row usually carries only the latter. Letting the push name win
    silently relabels chats -- "Onur" became "Yigit Tan" -- which is worse than
    the empty results this whole file exists to catch, because it looks right.
    """
    import sqlite3

    db_path = wa._resolve(account)[0]
    index = wa._load_lid_index(db_path)
    conn = sqlite3.connect(db_path)
    try:
        saved = {
            jid.split("@")[0]: name
            for jid, name in conn.execute(
                "SELECT jid, name FROM chats "
                "WHERE jid LIKE '%@s.whatsapp.net' AND name IS NOT NULL AND name <> ''"
            )
            if name and not str(name).isdigit()
        }
    finally:
        conn.close()

    checked = 0
    for pn, lids in index["pn_lids"].items():
        if checked >= sample:
            break
        want = saved.get(pn)
        if not want:
            continue
        for lid in lids:
            got = wa.display_name(f"{lid}@lid", None, db_path)
            if got != want:
                raise Failure(
                    f"{lid}@lid resolves to {got!r} but {pn} is saved as {want!r} "
                    f"-- a push name is outranking the saved name"
                )
        checked += 1

    if not checked:
        return "no LID maps with a saved name to compare"
    return f"{checked} LID(s) keep the saved name"


def run(account):
    checks = [
        ("name round-trip", lambda: check_name_roundtrip(account)),
        ("contact search", lambda: check_contact_search(account)),
        ("name precedence", lambda: check_name_precedence(account)),
        ("live chat lookup", lambda: check_liveness(account)),
    ]
    ok = True
    for label, fn in checks:
        try:
            print(f"  PASS  {label}: {fn()}")
        except Failure as e:
            ok = False
            print(f"  FAIL  {label}: {e}")
        except Exception:
            ok = False
            print(f"  ERROR {label}:\n{traceback.format_exc()}")
    return ok


def main():
    accounts = sys.argv[1:] or wa.list_accounts()
    print("text folding")
    try:
        print(f"  PASS  unicode: {check_folding()}")
        all_ok = True
    except Exception as e:
        print(f"  FAIL  unicode: {e}")
        all_ok = False

    for account in accounts:
        print(f"\naccount: {account}  ({wa._resolve(account)[0]})")
        all_ok = run(account) and all_ok

    print("\n" + ("ALL CHECKS PASSED" if all_ok else "SELF-CHECK FAILED"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
