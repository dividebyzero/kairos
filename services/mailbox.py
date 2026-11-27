"""The Email workspace's mailbox layer: folders, paged search and reading, one shape for every provider.

Every message is named by an opaque reference that the UI, routes and agent
tools all share. IMAP references are `imap:<account>:<folder>:<UIDVALIDITY>:<UID>`
(folder base64url-encoded), so a renumbered mailbox can never point an old
reference at a different message. Gmail (M4) adds `gmail:<account>:<id>` and
its own provider behind `_provider()`; callers do not change.

Reading never changes the mailbox: every fetch is a PEEK, so opening a message
leaves it unread until a person or agent marks it (M3).
"""
import base64
import email
import email.utils
import imaplib
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

from core.secret_storage import decrypt
from services import mail_html

IMAP_TIMEOUT = 20
PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
STATUS_FOLDER_LIMIT = 80
READ_MAX_BYTES = 15 * 1024 * 1024
TEXT_MAX_CHARS = 500_000
HTML_MAX_CHARS = 2_000_000
INLINE_IMAGE_MAX_BYTES = 1_000_000
QUERY_MAX_CHARS = 200
_HEADER_FIELDS = "FROM TO CC SUBJECT DATE MESSAGE-ID"
# Folder roles from RFC 6154 special-use flags, in the order the rail shows them.
ROLES = ("inbox", "drafts", "sent", "archive", "all", "flagged", "junk", "trash")
_ROLE_FLAGS = {b"\\drafts": "drafts", b"\\sent": "sent", b"\\archive": "archive", b"\\all": "all",
               b"\\flagged": "flagged", b"\\junk": "junk", b"\\trash": "trash"}


class MailboxError(Exception):
    """The mail server could not answer."""


class StaleReference(MailboxError):
    """The folder was renumbered (UIDVALIDITY changed) since the reference was made."""


@dataclass(frozen=True)
class MessageRef:
    provider: str
    account_id: str
    folder: str
    uidvalidity: str
    uid: int

    def __str__(self) -> str:
        folder = base64.urlsafe_b64encode(self.folder.encode("utf-8")).decode().rstrip("=")
        return f"{self.provider}:{self.account_id}:{folder}:{self.uidvalidity}:{self.uid}"


_REF = re.compile(r"^imap:([A-Za-z0-9_-]{1,64}):([A-Za-z0-9_-]{0,1400}):(\d{1,20}):(\d{1,20})$")


def parse_ref(ref: str) -> MessageRef:
    match = _REF.match(ref or "")
    if not match or int(match.group(4)) < 1:
        raise ValueError("Invalid message reference")
    encoded = match.group(2)
    try:
        folder = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Invalid message reference")
    _check_folder(folder)
    return MessageRef("imap", match.group(1), folder, match.group(3), int(match.group(4)))


def _check_folder(folder: str) -> str:
    if not isinstance(folder, str) or not folder or len(folder) > 1000 or re.search(r"[\x00-\x1f\x7f]", folder):
        raise ValueError("Invalid folder")
    return folder


# -- public API: the same calls for every provider --------------------------------------------

def list_folders(account_id: str) -> list[dict]:
    return _provider(account_id).list_folders()


def list_messages(account_id: str, folder: str = "INBOX", page: int = 1, page_size: int = PAGE_SIZE,
                  query: str = "", unseen_only: bool = False, since: Optional[date] = None) -> dict:
    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    return _provider(account_id).list_messages(_check_folder(folder), max(1, int(page)), page_size,
                                               _clean_query(query), unseen_only, since)


def read_message(ref: str) -> dict:
    parsed = parse_ref(ref)
    return _provider(parsed.account_id).read_message(parsed)


def account_of(ref: str) -> str:
    """The account a reference belongs to, for the caller's ownership check before anything is read."""
    return parse_ref(ref).account_id


def _provider(account_id: str) -> "ImapProvider":
    from services.email_service import email_service
    account = email_service.get_account(account_id, decrypted=True)
    if account is None:
        raise KeyError("Email account not found")
    return ImapProvider(account)


def _clean_query(query: str) -> str:
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", query or "").split())[:QUERY_MAX_CHARS]


# -- IMAP ----------------------------------------------------------------------------------------

class ImapProvider:
    def __init__(self, account: dict):
        self.account = account

    def _connect(self):
        account = self.account
        imap = imaplib.IMAP4_SSL(account["imap_host"], account["imap_port"], timeout=IMAP_TIMEOUT)
        try:
            imap.login(account["email"], decrypt(account["password_encrypted"]))
        except Exception:
            imap.logout()
            raise
        return imap

    def _select(self, imap, folder: str) -> str:
        status, _ = imap.select(_quote(folder), readonly=True)
        if status != "OK":
            raise KeyError("Folder not found")
        from services.email_service import _uidvalidity
        return _uidvalidity(imap)

    def list_folders(self) -> list[dict]:
        with self._connect() as imap:
            status, data = imap.list()
            if status != "OK":
                raise MailboxError("Could not list folders")
            folders = [f for f in (_parse_list_line(item) for item in data or []) if f]
            for index, folder in enumerate(f for f in folders if f["selectable"]):
                if index >= STATUS_FOLDER_LIMIT:
                    break
                status, info = imap.status(_quote(folder["path"]), "(MESSAGES UNSEEN)")
                text = b" ".join(i for i in info or [] if isinstance(i, bytes)) if status == "OK" else b""
                total, unread = re.search(rb"MESSAGES (\d+)", text), re.search(rb"UNSEEN (\d+)", text)
                folder["total"] = int(total.group(1)) if total else None
                folder["unread"] = int(unread.group(1)) if unread else None
        order = {role: i for i, role in enumerate(ROLES)}
        folders.sort(key=lambda f: (order.get(f["role"], len(ROLES)), f["path"].lower()))
        return folders

    def list_messages(self, folder: str, page: int, page_size: int, query: str,
                      unseen_only: bool, since: Optional[date]) -> dict:
        with self._connect() as imap:
            validity = self._select(imap, folder)
            uids = self._search(imap, query, unseen_only, since)
            chosen = uids[(page - 1) * page_size: page * page_size]
            messages = self._envelopes(imap, folder, validity, chosen) if chosen else []
        return {"folder": folder, "page": page, "page_size": page_size, "total": len(uids), "query": query,
                "messages": messages}

    def _search(self, imap, query: str, unseen_only: bool, since: Optional[date]) -> list[int]:
        criteria: list[str] = []
        if unseen_only:
            criteria.append("UNSEEN")
        if since is not None:
            criteria += ["SINCE", since.strftime("%d-%b-%Y")]
        if query and query.isascii():
            for word in query.split():
                criteria += ["TEXT", _quote(word)]
            status, data = imap.uid("SEARCH", *criteria)
        elif query:
            # One UTF-8 literal: imaplib sends it after the command line.
            imap.literal = query.encode("utf-8")
            status, data = imap.uid("SEARCH", "CHARSET", "UTF-8", *criteria, "TEXT")
        else:
            status, data = imap.uid("SEARCH", *(criteria or ["ALL"]))
        if status != "OK":
            raise MailboxError("The mail server refused the search")
        return sorted((int(u) for u in (data[0] or b"").split() if u.isdigit()), reverse=True)

    def _envelopes(self, imap, folder: str, validity: str, uids: list[int]) -> list[dict]:
        status, data = imap.uid("FETCH", ",".join(map(str, uids)),
                                f"(UID FLAGS RFC822.SIZE INTERNALDATE BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS ({_HEADER_FIELDS})])")
        if status != "OK":
            raise MailboxError("Could not read the message list")
        found = {}
        for meta, header in _fetch_records(data):
            uid = re.search(rb"\bUID (\d+)", meta)
            if not uid:
                continue
            msg = email.message_from_bytes(header or b"")
            summary = _summary(msg, meta)
            summary["uid"] = int(uid.group(1))
            summary["ref"] = str(MessageRef("imap", self.account["id"], folder, validity, summary["uid"]))
            found[summary["uid"]] = summary
        return [found[uid] for uid in uids if uid in found]

    def read_message(self, ref: MessageRef) -> dict:
        with self._connect() as imap:
            validity = self._select(imap, ref.folder)
            if validity != ref.uidvalidity:
                raise StaleReference("This folder changed since the list was loaded. Reload it.")
            status, data = imap.uid("FETCH", str(ref.uid), f"(UID FLAGS RFC822.SIZE BODY.PEEK[]<0.{READ_MAX_BYTES}>)")
            records = _fetch_records(data) if status == "OK" else []
            if not records:
                raise KeyError("Email not found")
            meta, raw = records[0]
        msg = email.message_from_bytes(raw or b"")
        size = re.search(rb"RFC822\.SIZE (\d+)", meta)
        size = int(size.group(1)) if size else len(raw or b"")
        return {**_summary(msg, meta), **_content(msg), "uid": ref.uid, "ref": str(ref), "account_id": ref.account_id, "folder": ref.folder,
                "size": size, "truncated": size > READ_MAX_BYTES}


# -- parsing ------------------------------------------------------------------------------------

def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


_LIST_LINE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.*)$', re.S)


def _parse_list_line(item) -> Optional[dict]:
    line, literal = (item[0], item[1]) if isinstance(item, tuple) else (item, None)
    if not isinstance(line, bytes):
        return None
    match = _LIST_LINE.match(line.strip())
    if not match:
        return None
    flags = match.group("flags").lower().split()
    delim = match.group("delim")
    delim = "" if delim == b"NIL" else _unquote(delim).decode("utf-8", "replace")
    raw = literal if isinstance(literal, bytes) else _unquote(match.group("name").strip())
    path = raw.decode("utf-8", "replace")
    if not path or re.search(r"[\x00-\x1f\x7f]", path):
        return None
    role = "inbox" if path.upper() == "INBOX" else next((_ROLE_FLAGS[f] for f in flags if f in _ROLE_FLAGS), None)
    parts = path.split(delim) if delim else [path]
    return {"path": path, "name": "Inbox" if role == "inbox" else imap_utf7_decode(parts[-1]),
            "depth": len(parts) - 1, "delimiter": delim, "role": role,
            "selectable": b"\\noselect" not in flags and b"\\nonexistent" not in flags,
            "unread": None, "total": None}


def _unquote(value: bytes) -> bytes:
    if len(value) >= 2 and value[:1] == b'"' and value[-1:] == b'"':
        return re.sub(rb'\\(.)', rb"\1", value[1:-1])
    return value


def imap_utf7_decode(name: str) -> str:
    """IMAP's modified UTF-7 folder names (RFC 3501 5.1.3) as text."""
    out, i = [], 0
    while i < len(name):
        if name[i] != "&":
            out.append(name[i])
            i += 1
            continue
        end = name.find("-", i)
        if end == -1:
            out.append(name[i:])
            break
        chunk = name[i + 1:end]
        if not chunk:
            out.append("&")
        else:
            try:
                b64 = chunk.replace(",", "/")
                out.append(base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-16-be"))
            except (ValueError, UnicodeDecodeError):
                out.append(name[i:end + 1])
        i = end + 1
    return "".join(out)


def _fetch_records(data) -> list[tuple[bytes, bytes]]:
    """(metadata, literal) per message from imaplib's FETCH response, metadata after the literal included."""
    records: list[list[bytes]] = []
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2:
            records.append([item[0] or b"", item[1] if isinstance(item[1], bytes) else b""])
        elif isinstance(item, bytes) and records:
            records[-1][0] += b" " + item
    return [(meta, literal) for meta, literal in records]


def _decode(raw) -> str:
    from services.email_service import _decode_header
    try:
        return _decode_header(str(raw or ""))
    except (LookupError, ValueError):
        return str(raw or "")


def _addresses(msg, field: str) -> list[dict]:
    # Split first, then decode each name: a decoded name may itself contain a comma.
    values = [str(v) for v in msg.get_all(field, [])]
    return [{"name": _decode(name)[:300], "address": _decode(address)[:320]}
            for name, address in email.utils.getaddresses(values) if name or address][:100]


def _summary(msg, meta: bytes) -> dict:
    flags = re.search(rb"FLAGS \(([^)]*)\)", meta)
    flags = flags.group(1).lower().split() if flags else []
    when = None
    try:
        when = email.utils.parsedate_to_datetime(msg.get("Date", "")) if msg.get("Date") else None
    except (TypeError, ValueError, IndexError):
        when = None
    if when is None:
        internal = imaplib.Internaldate2tuple(meta) if b"INTERNALDATE" in meta else None
        when = datetime.fromtimestamp(time.mktime(internal), timezone.utc) if internal else None
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    sender = _addresses(msg, "From")
    size = re.search(rb"RFC822\.SIZE (\d+)", meta)
    return {
        "from": sender[0] if sender else {"name": "", "address": ""},
        "from_header": _decode(msg.get("From", ""))[:1000],
        "to": _addresses(msg, "To"), "cc": _addresses(msg, "Cc"),
        "subject": _decode(msg.get("Subject", ""))[:1000],
        "date": when.isoformat() if when else None, "ts": when.timestamp() if when else None,
        "date_header": str(msg.get("Date", ""))[:200],
        "message_id": str(msg.get("Message-ID", "")).strip()[:1000],
        "seen": b"\\seen" in flags, "flagged": b"\\flagged" in flags, "answered": b"\\answered" in flags,
        "has_attachments": b'"attachment"' in meta.lower(),
        "size": int(size.group(1)) if size else None,
    }


def _text(part) -> str:
    payload = part.get_payload(decode=True) or b""
    try:
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _content(msg) -> dict:
    """The reader's body: plain text, cleaned HTML and attachment metadata."""
    text = raw_html = None
    inline: dict[str, str] = {}
    files = []
    for index, part in enumerate(msg.walk()):
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disposition = part.get_content_disposition()
        filename = _decode(part.get_filename() or "")[:300]
        cid = str(part.get("Content-ID", "")).strip().strip("<>").lower()
        if disposition != "attachment" and not filename and ctype == "text/plain" and text is None:
            text = _text(part)[:TEXT_MAX_CHARS]
        elif disposition != "attachment" and not filename and ctype == "text/html" and raw_html is None:
            raw_html = _text(part)[:HTML_MAX_CHARS]
        else:
            payload = part.get_payload(decode=True) or b""
            if cid and ctype.startswith("image/") and len(payload) <= INLINE_IMAGE_MAX_BYTES:
                inline[cid] = f"data:{ctype};base64,{base64.b64encode(payload).decode()}"
            files.append({"part": index, "filename": filename, "content_type": ctype, "size": len(payload),
                          "content_id": cid, "inline": disposition == "inline"})
    clean, remote = mail_html.clean_html(raw_html, inline) if raw_html else (None, 0)
    used = {cid for cid in inline if raw_html and f"cid:{cid}" in raw_html.lower()}
    attachments = [{k: v for k, v in f.items() if k != "content_id"} for f in files
                   if not (f["content_id"] in used and not f["filename"])]
    return {"text": text, "html": clean, "remote_images": remote, "attachments": attachments,
            "reply_to": _addresses(msg, "Reply-To"),
            "in_reply_to": str(msg.get("In-Reply-To", "")).strip()[:1000],
            "references": " ".join(str(msg.get("References", "")).split())[:4000]}
