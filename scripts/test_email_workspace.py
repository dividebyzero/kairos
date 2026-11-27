"""The Email workspace (M1): folders, paged search, the reader, references and HTML cleanup.

Runs against a fake IMAP server shaped like imaplib's real responses; never a real mailbox.

    .venv\\Scripts\\python.exe -B -X utf8 scripts\\test_email_workspace.py
"""
import os
import re
import sys
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.test_data_support import TestDataDirectory
fixture = TestDataDirectory(prefix="email-workspace-test-")
os.environ["JARVIS_DATA_DIR"] = fixture.name

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core.auth import auth_manager  # noqa: E402
from core.middleware import require_user  # noqa: E402
from routes import email_routes  # noqa: E402
from services import mail_html, mailbox  # noqa: E402
from services.email_service import email_service  # noqa: E402


def plain(uid, subject, body="Hello", sender="Ann <ann@example.test>", seen=True):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, "me@example.test", subject
    msg["Date"] = "Mon, 02 Nov 2026 10:00:00 +0100"
    msg["Message-ID"] = f"<m{uid}@example.test>"
    msg.set_content(body)
    return {"uid": uid, "raw": bytes(msg), "seen": seen}


def rich(uid):
    msg = EmailMessage()
    msg["From"] = '"Smith, Jo" <jo@example.test>'
    msg["To"] = "me@example.test, =?utf-8?q?J=C3=BCrgen?= <j@example.test>"
    msg["Cc"] = "team@example.test"
    msg["Subject"] = "=?utf-8?q?Gr=C3=BC=C3=9Fe_and_invoice?="
    msg["Date"] = "Tue, 03 Nov 2026 09:00:00 +0000"
    msg["Message-ID"] = "<rich@example.test>"
    msg["In-Reply-To"] = "<earlier@example.test>"
    msg.set_content("Plain version")
    msg.add_alternative(
        '<html><head><title>T</title><style>body{background:url(https://track.example/bg.png)}</style>'
        '<script>alert(1)</script></head><body onload="x()"><p onclick="steal()">Hi <b>there</b></p>'
        '<img src="https://track.example/pixel.gif" width="1"><img src="cid:logo@x">'
        '<a href="javascript:alert(1)">bad</a><a href="https://example.test/pay">pay</a>'
        '<form action="https://evil.example"><input name="pw"></form><svg><script>x</script></svg></body></html>',
        subtype="html")
    msg.get_payload()[1].add_related(b"\x89PNG fake", maintype="image", subtype="png", cid="<logo@x>")
    msg.add_attachment(b"%PDF-1.4 data", maintype="application", subtype="pdf", filename="invoice.pdf")
    return {"uid": uid, "raw": bytes(msg), "seen": False}


def unquote(name):
    return re.sub(r'\\(.)', r"\1", name[1:-1]) if name.startswith('"') else name


class FakeImap:
    """Enough of imaplib.IMAP4_SSL for the mailbox layer, with the server's quirks kept."""

    def __init__(self, folders, validity="7"):
        self.folders, self.validity, self.selected, self.literal, self.commands = folders, validity, None, None, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.logout()

    def login(self, user, password):
        self.commands.append(("LOGIN", user))

    def logout(self):
        self.commands.append(("LOGOUT",))

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Trash) "/" "Trash"',
                      b'(\\Noselect \\HasChildren) "/" "[Gmail]"', b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"',
                      b'(\\HasNoChildren \\Drafts) "/" "Entw&APw-rfe"', b'(\\HasNoChildren) "/" Archive',
                      (b'(\\HasNoChildren) "/" {11}', b'Odd "Name"')]

    def status(self, name, items):
        messages = self.folders.get(unquote(name), [])
        return "OK", [f'{name} (MESSAGES {len(messages)} UNSEEN {sum(not m["seen"] for m in messages)})'.encode()]

    def select(self, name, readonly=False):
        self.commands.append(("SELECT", name, readonly))
        path = unquote(name)
        if path not in self.folders:
            return "NO", [b"no such folder"]
        self.selected = path
        return "OK", [str(len(self.folders[path])).encode()]

    def response(self, code):
        return code, [self.validity.encode()]

    def uid(self, command, *args):
        self.commands.append(("UID", command, args, self.literal))
        messages = self.folders[self.selected]
        if command == "SEARCH":
            found = list(messages)
            words = [a.strip('"') for a, prev in zip(args, (None,) + args) if prev == "TEXT"]
            if self.literal is not None:
                words.append(self.literal.decode())
                self.literal = None
            for word in words:
                found = [m for m in found if word.lower().encode() in m["raw"].lower()
                         or word.lower() in m["raw"].decode("utf-8", "replace").lower()]
            if "UNSEEN" in args:
                found = [m for m in found if not m["seen"]]
            return "OK", [" ".join(str(m["uid"]) for m in found).encode()]
        wanted = {int(u) for u in args[0].split(",")}
        out = []
        for m in messages:
            if m["uid"] not in wanted:
                continue
            flags = "\\Seen" if m["seen"] else ""
            if "HEADER.FIELDS" in args[1]:
                header = m["raw"].split(b"\n\n", 1)[0] + b"\n\n"
                structure = '("application" "pdf" NIL NIL NIL "base64" 10 NIL ("attachment" ("filename" "a.pdf")))' \
                    if b"invoice.pdf" in m["raw"] else '("text" "plain" NIL NIL NIL "7bit" 5 1)'
                meta = (f'1 (UID {m["uid"]} FLAGS ({flags}) RFC822.SIZE {len(m["raw"])} '
                        f'INTERNALDATE "02-Nov-2026 10:00:00 +0000" BODY[HEADER.FIELDS (FROM)] {{{len(header)}}}').encode()
                out += [(meta, header), f' BODYSTRUCTURE {structure})'.encode()]
            else:
                meta = f'1 (UID {m["uid"]} FLAGS ({flags}) RFC822.SIZE {len(m["raw"])} BODY[]<0> {{{len(m["raw"])}}}'.encode()
                out += [(meta, m["raw"]), b")"]
        return "OK", out


class Base(unittest.TestCase):
    def setUp(self):
        self.archive = [plain(uid, f"Archived {uid}") for uid in range(1, 121)]
        self.inbox = [plain(1, "Lunch?", seen=False), plain(2, "Café Zürich plans"), rich(3)]
        self.server = FakeImap({"INBOX": self.inbox, "Archive": self.archive, "Trash": [], 'Odd "Name"': [],
                                "[Gmail]/Sent Mail": [plain(9, "Sent one")], "Entw&APw-rfe": []})
        item = patch.object(mailbox.imaplib, "IMAP4_SSL", lambda *a, **k: self.server)
        item.start()
        self.addCleanup(item.stop)
        self.account = email_service.create_account("me@example.test", "pw", "imap.example.test", 993,
                                                    "smtp.example.test", 465, owner="ann")
        self.addCleanup(email_service.delete_account, self.account["id"])


class MailboxTests(Base):
    def test_folders_come_ordered_named_and_counted(self):
        folders = mailbox.list_folders(self.account["id"])
        self.assertEqual([f["path"] for f in folders][:4], ["INBOX", "Entw&APw-rfe", "[Gmail]/Sent Mail", "Trash"])
        by_path = {f["path"]: f for f in folders}
        self.assertEqual(by_path["Entw&APw-rfe"]["name"], "Entwürfe")
        self.assertEqual((by_path["INBOX"]["unread"], by_path["INBOX"]["total"]), (2, 3))
        self.assertFalse(by_path["[Gmail]"]["selectable"])
        self.assertEqual((by_path["[Gmail]/Sent Mail"]["name"], by_path["[Gmail]/Sent Mail"]["depth"]), ("Sent Mail", 1))
        self.assertIn('Odd "Name"', by_path)

    def test_pages_go_newest_first_by_uid_with_stable_references(self):
        first = mailbox.list_messages(self.account["id"], "Archive", page=1, page_size=50)
        self.assertEqual((first["total"], len(first["messages"])), (120, 50))
        self.assertEqual([m["uid"] for m in first["messages"]][:2], [120, 119])
        last = mailbox.list_messages(self.account["id"], "Archive", page=3, page_size=50)
        self.assertEqual([m["uid"] for m in last["messages"]], list(range(20, 0, -1)))
        ref = mailbox.parse_ref(first["messages"][0]["ref"])
        self.assertEqual((ref.account_id, ref.folder, ref.uidvalidity, ref.uid), (self.account["id"], "Archive", "7", 120))
        self.assertTrue(all(cmd[2] for cmd in self.server.commands if cmd[0] == "SELECT"), "always read-only")

    def test_list_rows_carry_sender_flags_and_attachment_hint(self):
        rows = {m["uid"]: m for m in mailbox.list_messages(self.account["id"])["messages"]}
        self.assertEqual(rows[3]["from"], {"name": "Smith, Jo", "address": "jo@example.test"})
        self.assertEqual(rows[3]["subject"], "Grüße and invoice")
        self.assertEqual([a["name"] for a in rows[3]["to"]], ["", "Jürgen"])
        self.assertTrue(rows[3]["has_attachments"] and not rows[1]["has_attachments"])
        self.assertFalse(rows[1]["seen"])
        self.assertTrue(rows[2]["seen"])
        self.assertEqual(rows[3]["date"], "2026-11-03T09:00:00+00:00")

    def test_search_ands_words_and_sends_non_ascii_as_a_utf8_literal(self):
        hits = mailbox.list_messages(self.account["id"], "INBOX", query="archived 11")
        self.assertEqual(hits["total"], 0)
        hits = mailbox.list_messages(self.account["id"], "Archive", query="  Archived\r\n 11 ")
        self.assertEqual(hits["query"], "Archived 11")
        self.assertEqual(sorted(m["uid"] for m in hits["messages"]), [11, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119])
        mailbox.list_messages(self.account["id"], "INBOX", query="Zürich")
        search = [c for c in self.server.commands if c[0] == "UID" and c[1] == "SEARCH"][-1]
        self.assertEqual((search[2][:2], search[3]), (("CHARSET", "UTF-8"), "Zürich".encode()))

    def test_reading_peeks_cleans_html_and_lists_attachments(self):
        ref = next(m["ref"] for m in mailbox.list_messages(self.account["id"])["messages"] if m["uid"] == 3)
        message = mailbox.read_message(ref)
        fetch = [c for c in self.server.commands if c[0] == "UID" and c[1] == "FETCH"][-1]
        self.assertIn("BODY.PEEK[]", fetch[2][1], "reading never marks the message seen")
        self.assertEqual(message["text"].strip(), "Plain version")
        html = message["html"]
        for gone in ("<script", "onload", "onclick", "javascript:", "track.example", "<form", "<input", "<svg", "<title"):
            self.assertNotIn(gone, html)
        self.assertIn('<a href="https://example.test/pay" target="_blank" rel="noopener noreferrer">pay</a>', html)
        self.assertIn('src="data:image/png;base64,', html)
        self.assertEqual(message["remote_images"], 1)
        self.assertEqual([(a["filename"], a["content_type"]) for a in message["attachments"]], [("invoice.pdf", "application/pdf")])
        self.assertEqual(message["in_reply_to"], "<earlier@example.test>")
        self.assertEqual([c["address"] for c in message["cc"]], ["team@example.test"])

    def test_a_renumbered_folder_refuses_an_old_reference(self):
        ref = mailbox.list_messages(self.account["id"])["messages"][0]["ref"]
        self.server.validity = "8"
        with self.assertRaises(mailbox.StaleReference):
            mailbox.read_message(ref)

    def test_references_are_validated_and_folder_names_quoted(self):
        for bad in ("", "imap:a:b:c:d", "imap:acct:Zm9v:7:0", "gmail:x:y", "imap:a/b:Zm9v:7:1", "imap:a:" + "A" * 1401 + ":7:1"):
            with self.assertRaises(ValueError, msg=bad):
                mailbox.parse_ref(bad)
        ref = str(mailbox.MessageRef("imap", self.account["id"], 'Odd "Name"', "7", 5))
        self.assertEqual(mailbox.parse_ref(ref).folder, 'Odd "Name"')
        mailbox.list_messages(self.account["id"], 'Odd "Name"')
        self.assertIn(("SELECT", '"Odd \\"Name\\""', True), self.server.commands)
        with self.assertRaises(ValueError):
            mailbox.list_messages(self.account["id"], "INBOX\r\nA1 DELETE INBOX")

    def test_the_legacy_list_now_returns_real_uids_and_references(self):
        rows = email_service.list_messages(self.account["id"], limit=2)
        self.assertEqual([r["uid"] for r in rows], ["3", "2"])
        self.assertEqual(set(rows[0]), {"uid", "ref", "from", "subject", "date", "seen"})
        self.assertEqual(rows[0]["from"], "\"Smith, Jo\" <jo@example.test>")
        unread = email_service.list_messages(self.account["id"], limit=10, unseen_only=True)
        self.assertEqual([r["uid"] for r in unread], ["3", "1"])


class CleanHtmlTests(unittest.TestCase):
    def clean(self, raw, inline=None):
        return mail_html.clean_html(raw, inline)[0]

    def test_style_cannot_load_or_break_out(self):
        out = self.clean("<style>@import 'x.css'; a{background:url( 'https://t/x' )} b{background:url(data:image/png;base64,AA)}"
                         "</style><p style=\"background:url(http://t/y);color:red\">x</p>")
        self.assertNotIn("@import", out)
        self.assertNotIn("https://t", out)
        self.assertNotIn("http://t", out)
        self.assertIn("url(data:image/png;base64,AA)", out)
        self.assertIn("color:red", out)
        out, remote = mail_html.clean_html('<style>a{content:"</style><img src=x onerror=alert(1)>"}</style><p>ok</p>')
        self.assertEqual((out, remote), ('<style>a{content:"</style>"}<p>ok</p>', 1))

    def test_dropped_containers_take_their_content_and_text_is_escaped(self):
        out = self.clean("<div><iframe><p>inner</p></iframe><object><object>o</object></object>after &lt;b&gt;</div><!-- c -->")
        self.assertEqual(out, "<div>after &lt;b&gt;</div>")

    def test_only_safe_images_and_links_remain(self):
        out, remote = mail_html.clean_html('<img src="cid:A@B"><img src="data:image/gif;base64,R0lG"><img src="//t/x">'
                                           '<img><a href=" mailto:a@b">m</a><a href="data:text/html,x">d</a>',
                                           {"a@b": "data:image/png;base64,QQ=="})
        self.assertEqual(remote, 1)
        self.assertEqual(out.count("<img"), 2)
        self.assertIn('src="data:image/png;base64,QQ=="', out)
        self.assertIn('href=" mailto:a@b"', out)
        self.assertIn("<a>d</a>", out)

    def test_unclosed_tags_are_closed(self):
        self.assertEqual(self.clean("<table><tr><td>x"), "<table><tr><td>x</td></tr></table>")


def principal(request: Request) -> str:
    return request.headers.get("x-test-user", "ann")


class RouteTests(Base):
    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(email_routes.router)
        app.dependency_overrides[require_user] = principal
        self.web = TestClient(app)
        item = patch.object(auth_manager, "is_admin", lambda user: user == "admin")
        item.start()
        self.addCleanup(item.stop)

    def get(self, path, user="ann", **params):
        return self.web.get(path, params=params, headers={"x-test-user": user})

    def test_the_owner_browses_and_reads(self):
        base = f"/api/email/accounts/{self.account['id']}"
        self.assertEqual(self.get(f"{base}/folders").json()["folders"][0]["path"], "INBOX")
        page = self.get(f"{base}/mailbox", folder="Archive", page=2, page_size=10).json()
        self.assertEqual((page["total"], page["page"], page["messages"][0]["uid"]), (120, 2, 110))
        ref = page["messages"][0]["ref"]
        self.assertEqual(self.get("/api/email/message", ref=ref).json()["subject"], "Archived 110")
        self.assertEqual(self.get("/api/email/message", "admin", ref=ref).status_code, 200)

    def test_someone_else_cannot_list_or_read_by_reference(self):
        base = f"/api/email/accounts/{self.account['id']}"
        ref = self.get(f"{base}/mailbox").json()["messages"][0]["ref"]
        before = len(self.server.commands)
        self.assertEqual(self.get(f"{base}/folders", "bob").status_code, 404)
        self.assertEqual(self.get(f"{base}/mailbox", "bob").status_code, 404)
        self.assertEqual(self.get("/api/email/message", "bob", ref=ref).status_code, 404)
        self.assertEqual(len(self.server.commands), before, "the server was never contacted for bob")

    def test_errors_are_named(self):
        base = f"/api/email/accounts/{self.account['id']}"
        self.assertEqual(self.get("/api/email/message", ref="imap:nope").status_code, 400)
        self.assertEqual(self.get("/api/email/message", ref=str(mailbox.MessageRef("imap", "gone", "INBOX", "7", 1))).status_code, 404)
        self.assertEqual(self.get(f"{base}/mailbox", folder="Missing").status_code, 404)
        self.assertEqual(self.get(f"{base}/mailbox", page_size=500).status_code, 422)
        ref = self.get(f"{base}/mailbox").json()["messages"][0]["ref"]
        self.server.validity = "9"
        self.assertEqual(self.get("/api/email/message", ref=ref).status_code, 409)
        with patch.object(self.server, "list", side_effect=OSError("connection reset")):
            response = self.get(f"{base}/folders")
        self.assertEqual(response.status_code, 502)
        self.assertIn("connection reset", response.json()["detail"])


def tearDownModule():
    fixture.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
