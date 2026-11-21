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

    def work(sender, text):
        sender = sender.split("@")[0].lstrip("+")
        if allowed and sender not in allowed:
            return  # only answer the owner's numbers, never strangers
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
        print(f"Linked WhatsApp number: {own}. You can also command the team by messaging yourself.")
    if wm.get("receive", "poll") == "webhook":
        _webhook(wm, work, int(config["whatsapp"].get("port", 8000)))
    else:
        _poll(wm, work, own)


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
    peer = _number(m.get("peer"))
    chat = _number(m.get("chat"))
    if own and own in (peer, chat):
        return own  # "message yourself" chat
    if str(m.get("direction", "")).lower() in ("in", "inbound", "incoming"):
        return peer
    return None


# ---------------- free plan: polling ----------------
def _poll(wm, work, own=""):
    here = os.path.dirname(os.path.abspath(__file__))
    cursor_file = os.path.join(here, "whatsmcp_cursor.txt")
    every = int(wm.get("poll_seconds", 900))
    if os.path.exists(cursor_file):
        cursor = int(open(cursor_file).read().strip() or 0)
    else:  # first start: skip old messages, only answer new ones
        cursor = 0
        while True:
            page = _call("GET", f"/messages?after_cursor={cursor}&limit=200")
            cursor = page.get("next_cursor") or cursor
            if not page.get("has_more"):
                break
        open(cursor_file, "w").write(str(cursor))
    seen = set()
    print(f"Checking WhatsMCP for new messages every {every} seconds (Ctrl+C to stop)")
    while True:
        try:
            page = _call("GET", f"/messages?after_cursor={cursor}&limit=50")
            for m in page.get("messages", []):
                if os.environ.get("DEBUG_WAMCP"):
                    print("seen:", {k: m.get(k) for k in ("direction", "peer", "chat", "kind")}, (m.get("text") or "")[:40])
                if m.get("message_id") in seen:
                    continue
                seen.add(m.get("message_id"))
                who = _pick(m, own)
                if who:
                    print(f"Message from {who}: {m['text'][:60]}")
                    threading.Thread(target=work, args=(who, m["text"]), daemon=True).start()
            cursor = page.get("next_cursor") or cursor
            open(cursor_file, "w").write(str(cursor))
            if page.get("has_more"):
                continue
        except Exception as e:
            print("WhatsMCP check failed:", e)
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
