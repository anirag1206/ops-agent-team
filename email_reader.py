"""Email -> Grok -> your WhatsApp. Part of the middleman.

Every few minutes the middleman looks at your mailbox (read only, nothing is marked read,
moved or deleted), skips newsletters and automatic mails for free, and passes the new
emails, with the text of their attachments and any pictures, to Grok in ONE call.
Grok's reply comes to you on WhatsApp. Grok decides which department it belongs to and
what to do; you can answer it on WhatsApp like any other message.
Nothing is ever sent by email.

Setup (Gmail / Google Workspace):
  1. Turn on 2-Step Verification for the mailbox, then create an App password
     at https://myaccount.google.com/apppasswords (Workspace admins may need to allow it).
  2. In Command Prompt:  setx EMAIL_APP_PASSWORD "the 16 letters"
  3. In config.local.json:  "email": {"enabled": true, "user": "you@yourcompany.com"}
Test with:  python run.py email-test
"""
import email
import email.policy
import imaplib
import json
import os
import re
import time
from email.utils import parseaddr

import media

AUTOMATIC = re.compile(r"no-?reply|do-?not-?reply|mailer-daemon|notification|newsletter|bounce", re.I)


class EmailReader:
    def __init__(self, config, middleman=None):
        self.cfg = config.get("email", {})
        self.mm = middleman
        here = os.path.dirname(os.path.abspath(__file__))
        self.state_file = os.path.join(here, "email_state.json")

    # ---------- mailbox ----------
    def _login(self):
        pw = os.environ.get(self.cfg.get("password_env", "EMAIL_APP_PASSWORD"))
        if not pw or not self.cfg.get("user"):
            raise RuntimeError("Set email.user in config.local.json and the EMAIL_APP_PASSWORD environment variable.")
        box = imaplib.IMAP4_SSL(self.cfg.get("imap_host", "imap.gmail.com"), int(self.cfg.get("imap_port", 993)))
        box.login(self.cfg["user"], pw)
        box.select(self.cfg.get("folder", "INBOX"), readonly=True)
        return box

    def _last_uid(self):
        if os.path.exists(self.state_file):
            return json.load(open(self.state_file)).get("last_uid")
        return None

    def new_emails(self):
        """New emails since the last check. The first time, old mail is skipped."""
        box = self._login()
        try:
            last = self._last_uid()
            if last is None:
                ok, data = box.uid("search", None, "ALL")
                uids = [int(u) for u in data[0].split()]
                self._save(max(uids) if uids else 0)
                print(f"Email: connected. {len(uids)} old emails skipped; only new ones from now on.")
                return []
            ok, data = box.uid("search", None, f"UID {last + 1}:*")
            uids = sorted(int(u) for u in data[0].split() if int(u) > last)
            mails = []
            for uid in uids[:int(self.cfg.get("max_per_check", 10))]:
                ok, parts = box.uid("fetch", str(uid), "(BODY.PEEK[])")
                raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
                if raw:
                    mails.append(email.message_from_bytes(raw, policy=email.policy.default))
            if uids:
                self._save(uids[:int(self.cfg.get("max_per_check", 10))][-1])
            return mails
        finally:
            box.logout()

    def _save(self, uid):
        with open(self.state_file, "w") as f:
            json.dump({"last_uid": uid}, f)

    # ---------- free filter ----------
    def worth_forwarding(self, msg):
        sender = parseaddr(msg.get("From", ""))[1].lower()
        if any(s.lower() in sender for s in self.cfg.get("always_read", [])):
            return True
        if any(s.lower() in sender for s in self.cfg.get("skip_senders", [])):
            return False
        if self.cfg.get("skip_automatic", True) and (
                msg.get("List-Unsubscribe") or str(msg.get("Precedence", "")).lower() in ("bulk", "list")
                or AUTOMATIC.search(sender)):
            return False
        return True

    # ---------- turn one email into text + pictures for Grok ----------
    def describe(self, msg, n):
        name, addr = parseaddr(msg.get("From", ""))
        body = msg.get_body(preferencelist=("plain", "html"))
        text = body.get_content() if body else ""
        if body and body.get_content_type() == "text/html":
            text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
            text = re.sub(r"<br\s*/?>|</p>", "\n", text, flags=re.I)
            text = re.sub(r"<[^>]+>", " ", text)
        # cut the quoted earlier mails
        text = re.split(r"\n\s*On .{5,200}wrote:|\n-{2,}\s*Original Message|\n>", text, maxsplit=1)[0]
        text = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()
        limit = int(self.cfg.get("max_chars_per_email", 2000))
        lines = [f"EMAIL {n}", f"From: {name} <{addr}>", f"To: {msg.get('To', '')}",
                 f"Date: {msg.get('Date', '')}", f"Subject: {msg.get('Subject', '')}",
                 text[:limit] + (" ...(cut)" if len(text) > limit else "")]
        images = []
        for part in msg.iter_attachments():
            fname = part.get_filename() or "file"
            data = part.get_payload(decode=True) or b""
            pic = media.as_image(data, part.get_content_type()) if self.cfg.get("read_pictures", True) else None
            if pic and len(images) < 3:
                images.append(pic)
                lines.append(f"[picture attached: {fname}]")
                continue
            ftext = media.file_text(data, part.get_content_type(), fname, 1500)
            lines.append(f"[attachment {fname}]" + (f"\n{ftext}" if ftext else " (not readable on this PC)"))
        return "\n".join(lines), images, f"{name or addr}: {msg.get('Subject', '')}"

    # ---------- the loop ----------
    def check_once(self):
        mails = self.new_emails()
        keep = [m for m in mails if self.worth_forwarding(m)]
        if mails:
            print(f"Email: {len(mails)} new, {len(mails) - len(keep)} skipped free (newsletters, automatic).")
        if not keep:
            return None
        blocks, images, short = [], [], []
        for n, m in enumerate(keep, 1):
            text, pics, title = self.describe(m, n)
            blocks.append(text)
            images += pics
            short.append(title)
        return {"blocks": blocks, "images": images[:4], "short": short}

    def loop(self):
        every = max(60, int(self.cfg.get("check_minutes", 5)) * 60)
        print(f"Email: checking {self.cfg.get('user')} every {every // 60} min.")
        while True:
            try:
                found = self.check_once()
                if found and self.mm:
                    owner = self.mm.owner[0] if self.mm.owner else None
                    if owner:
                        with self.mm.lock:
                            for s in found["short"]:
                                self.mm.history[owner].append(f"(email arrived) {s}")
                        self.mm.ask_ceo({
                            "reply_to": owner, "group": False, "route": self.cfg.get("route", {}),
                            "header": "New emails in the company mailbox (read them, tell the owner on WhatsApp "
                                      "what matters, which department it is for and what to do; NO_REPLY if "
                                      "nothing matters)",
                            "lines": found["blocks"], "images": found["images"], "media": []})
            except Exception as e:
                print("Email check failed:", e)
            time.sleep(every)
