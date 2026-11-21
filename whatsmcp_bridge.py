"""WhatsApp through WhatsMCP (whatsmcp.com), the service you already use.

Two ways to receive messages, set "receive" in config.json -> whatsmcp:
  "poll"    -> this program asks WhatsMCP for new messages every few minutes.
               Works on the FREE plan and needs no tunnel. Free plan allows only
               100 API calls a day, so the default check is every 15 minutes.
               On the Pro plan (unlimited calls) set poll_seconds to 20.
  "webhook" -> WhatsMCP pushes each message instantly to this PC (paid plans,
               Starter and up). Needs a tunnel, same as in README Part 2.

The API key goes in the Windows environment variable WAMCP_KEY (never in a file).
Docs used: https://whatsmcp.com/docs/rest  (read 21 Nov 2026)
"""
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from whatsapp_bridge import _briefing_clock, handle_command

API = "https://api.whatsmcp.com/v1"
MARK = "\U0001F916 "  # robot emoji at the start of every team reply, so the team never answers itself


def _call(method, path, body=None):
    key = os.environ.get("WAMCP_KEY")
    if not key:
        raise RuntimeError("Set the WAMCP_KEY environment variable to your WhatsMCP API key.")
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-API-Key": key, "Content-Type": "application/json", "User-Agent": "ops-agent-team"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode() or "{}")


def send(config, to, text):
    acc = config["whatsmcp"].get("account_id")
    if not acc or not os.environ.get("WAMCP_KEY"):
        print(f"\n[WhatsMCP not set up yet, would send to {to}]:\n{text}\n")
        return
    for i in range(0, len(text), 3900):
        _call("POST", f"/accounts/{acc}/messages", {"to": to, "text": MARK + text[i:i + 3900]})


def serve(team, config):
    wm = config["whatsmcp"]
    allowed = {n.lstrip("+") for n in (config["whatsapp"].get("allowed_numbers") or [])}
    lock = threading.Lock()

    if team.staff:
        team.staff.send_fn = lambda phone, text: send(config, phone, text)

    def work(sender, text):
        sender = sender.split("@")[0].lstrip("+")
        if team.staff and sender not in allowed:
            to_staff, to_owner = team.staff.staff_message(sender, text)
            if to_staff:
                print(f"Staff message from {sender}: {text[:60]}")
                send(config, sender, to_staff)
                for owner in allowed:
                    if to_owner:
                        send(config, owner, to_owner)
            return
        if allowed and sender not in allowed:
            return  # only answer the owner's numbers, never strangers
        print(f"Command from {sender}: {text[:60]}")
        with lock:
            try:
                reply = handle_command(team, text)
            except Exception as e:
                reply = f"Sorry, something went wrong: {e}"
        send(config, sender, reply)

    def briefing_sender(_cfg, number, text):
        send(config, number, text)

    threading.Thread(target=_briefing_clock, args=(team, config, lock, briefing_sender), daemon=True).start()

    own = _own_phone(wm.get("account_id"))
    if own:
        print(f"Linked WhatsApp number: {own}.")
    if wm.get("receive", "poll") == "webhook":
        _webhook(wm, work, int(config["whatsapp"].get("port", 8000)))
    else:
        chats = list(allowed) + ([p["phone"] for p in team.staff.people] if team.staff else [])
        _poll(wm, work, own, chats)


def _own_phone(account_id):
    try:
        for a in _call("GET", "/accounts").get("accounts", []):
            if a.get("account_id") == account_id:
                return str(a.get("phone", "")).lstrip("+")
    except Exception as e:
        print("Could not read the linked number:", e)
    return ""


def _number(value):
    return "".join(ch for ch in str(value or "").split("@")[0] if ch.isdigit())


def _pick(m, own):
    """Return the number to answer, or None if this message is not a command for the team."""
    text = m.get("text") or ""
    if not text or text.startswith(MARK.strip()):
        return None  # empty, or one of the team's own replies
    if "@g.us" in str(m.get("chat", "")) or "@g.us" in str(m.get("peer", "")) or "@newsletter" in str(m.get("chat", "")):
        return None  # never act on group or channel messages
    peer = _number(m.get("peer"))
    chat = _number(m.get("chat"))
    if own and own in (peer, chat):
        return own  # "message yourself" chat
    if str(m.get("direction", "")).lower() in ("in", "inbound", "incoming"):
        return peer
    return None


# ---------------- free plan: polling ----------------
def _poll(wm, work, own="", chats=None):
    """Asks WhatsMCP ONLY for the chats of the listed numbers (allowed numbers + staff),
    so other conversations are never downloaded."""
    here = os.path.dirname(os.path.abspath(__file__))
    cursor_file = os.path.join(here, "whatsmcp_cursors.json")
    every = int(wm.get("poll_seconds", 900))
    chats = sorted({c for c in (chats or []) if c})
    if not chats:
        print("No allowed numbers set in config, so nothing will be read. Add allowed_numbers and restart.")
        return
    cursors = {}
    if os.path.exists(cursor_file):
        with open(cursor_file) as f:
            cursors = json.load(f)
    for chat in chats:
        if chat not in cursors:  # first time: skip old messages in this chat, only answer new ones
            c = 0
            while True:
                page = _call("GET", f"/messages?chat={chat}&after_cursor={c}&limit=200")
                c = page.get("next_cursor") or c
                if not page.get("has_more"):
                    break
            cursors[chat] = c
    with open(cursor_file, "w") as f:
        json.dump(cursors, f)
    seen = set()
    calls_per_day = len(chats) * 86400 // max(every, 1)
    print(f"Reading only these chats: {', '.join(chats)}")
    print(f"Checking WhatsMCP for new messages every {every} seconds (about {calls_per_day} checks a day; "
          "the free plan allows 100) - Ctrl+C to stop")
    while True:
        for chat in chats:
            try:
                while True:
                    page = _call("GET", f"/messages?chat={chat}&after_cursor={cursors[chat]}&limit=50")
                    for m in page.get("messages", []):
                        if os.environ.get("DEBUG_WAMCP"):
                            print("seen:", {k: m.get(k) for k in ("direction", "peer", "chat", "kind")},
                                  (m.get("text") or "")[:40])
                        if m.get("message_id") in seen:
                            continue
                        seen.add(m.get("message_id"))
                        who = _pick(m, own)
                        if who:
                            threading.Thread(target=work, args=(who, m["text"]), daemon=True).start()
                    cursors[chat] = page.get("next_cursor") or cursors[chat]
                    if not page.get("has_more"):
                        break
                with open(cursor_file, "w") as f:
                    json.dump(cursors, f)
            except Exception as e:
                print(f"WhatsMCP check for {chat} failed:", e)
        time.sleep(every)


# ---------------- paid plans: webhook ----------------
def _webhook(wm, work, port):
    secret = os.environ.get("WAMCP_WEBHOOK_SECRET", "")

    class Hook(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if secret and not _signature_ok(raw, self.headers.get("X-WAMCP-Signature", ""), secret):
                self.send_response(401)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            data = json.loads(raw or b"{}")
            if data.get("event") == "message.inbound" and data.get("body") and not data["body"].startswith(MARK.strip()):
                threading.Thread(target=work, args=(str(data.get("peer", "")), data["body"]), daemon=True).start()

        def log_message(self, *a):
            pass

    print(f"WhatsMCP webhook listening on http://localhost:{port}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("0.0.0.0", port), Hook).serve_forever()


def _signature_ok(raw, header, secret):
    # Header looks like "t=<unix>,v1=<hex>". GUESS: signed text is "<t>.<body>" (common pattern).
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        expected = hmac.new(secret.encode(), f"{parts['t']}.".encode() + raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, parts["v1"])
    except Exception:
        return False


# ---------------- building the staff list from a WhatsApp group ----------------
def list_groups(account_id):
    chats = _call("GET", f"/accounts/{account_id}/chats?limit=200").get("chats", [])
    return [c for c in chats if "@g.us" in str(c.get("chat", ""))]


def group_senders(account_id, name_part, own_phone=""):
    """Everyone who has written in the group (WhatsMCP has no member list, only messages)."""
    from urllib.parse import quote
    groups = [g for g in list_groups(account_id) if name_part.lower() in str(g.get("name", "")).lower()]
    if not groups:
        raise RuntimeError(f"No WhatsApp group with '{name_part}' in its name. Run: python run.py whatsmcp-groups")
    g = groups[0]
    jid = quote(g["chat"], safe="")
    try:  # ask WhatsApp for older messages too, so more members show up
        _call("POST", f"/accounts/{account_id}/chats/{jid}/history", {"count": 500})
        time.sleep(5)
    except Exception:
        pass
    msgs = _call("GET", f"/accounts/{account_id}/chats/{jid}?limit=200").get("messages", [])
    people = {}
    for m in msgs:
        phone = _number(m.get("peer"))
        if phone and phone != own_phone and len(phone) >= 10:
            people.setdefault(phone, m.get("sender_name") or "")
    return g.get("name", name_part), people


def add_group_to_contacts(path, account_id, name_part, role="", own_phone=""):
    gname, people = group_senders(account_id, name_part, own_phone)
    data = {"people": []}
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        data["people"] = [p for p in data.get("people", []) if not str(p.get("phone", "")).startswith("910000000")]
    known = {"".join(ch for ch in str(p.get("phone", "")) if ch.isdigit()) for p in data["people"]}
    added = []
    for phone, name in people.items():
        if phone not in known:
            data["people"].append({"name": name or phone, "role": role, "phone": phone})
            added.append(f"{name or '?'} ({phone})")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return gname, added
