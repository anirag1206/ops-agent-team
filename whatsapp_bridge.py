"""WhatsApp connection option 2: Meta's OFFICIAL WhatsApp Business Cloud API (Meta).

Why official: unofficial "WhatsApp Web" libraries can get your number banned.
The official API is free for replies within 24 hours of a customer/owner message,
and business-started messages (like the morning briefing) use a small per-message fee
or a free monthly allowance, depending on Meta's current pricing.

How it works:
  Meta sends each incoming WhatsApp message to this small web server (a "webhook").
  The team works on it and the answer is sent back with the Cloud API.
Your PC must be reachable from the internet for Meta to call it. The easy free way
is a tunnel such as Cloudflare Tunnel or ngrok (see README).
"""
import json
import os
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

GRAPH = "https://graph.facebook.com/v21.0"


def send_whatsapp(config, to, text):
    token = os.environ.get("WHATSAPP_TOKEN")
    phone_id = config["whatsapp"].get("phone_number_id")
    if not token or not phone_id:
        print(f"\n[WhatsApp not set up yet, would send to {to}]:\n{text}\n")
        return
    for i in range(0, len(text), 3900):  # WhatsApp limit is 4096 characters
        body = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text[i:i + 3900]}}
        req = urllib.request.Request(
            f"{GRAPH}/{phone_id}/messages", data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=30).read()


def handle_command(team, text):
    """Simple commands first, everything else goes to the leadership team."""
    t = text.strip()
    low = t.lower()
    if low in ("help", "hi", "hello", "menu"):
        return ("Your AI leadership team is ready.\n"
                "- Just type any question or problem and the team will plan it.\n"
                "- team : see the org chart\n"
                "- tasks : see open action items\n"
                "- done 3 : mark action item 3 as done\n"
                "- remember: <fact> : teach the team something permanent\n"
                "- briefing : get the morning briefing now")
    if low == "team":
        return team.org_chart()
    if low == "tasks":
        rows = team.memory.open_tasks()
        return "\n".join(f"#{r[0]} {team.agents.get(r[1], {}).get('title', r[1])}: {r[2]} (due {r[3]})"
                         for r in rows) or "No open action items."
    if low.startswith("done "):
        ok = team.memory.close_task(int(low.split()[1])) if low.split()[1].isdigit() else False
        return "Marked done." if ok else "I could not find that action item number."
    if low.startswith("remember"):
        fact = t.split(":", 1)[-1].strip() if ":" in t else t[8:].strip()
        team.memory.add_fact(fact)
        return f"Saved. The whole team will remember: {fact}"
    if low == "briefing":
        return team.daily_briefing()
    return team.handle(t)


def serve(team, config):
    wa = config["whatsapp"]
    allowed = set(wa.get("allowed_numbers") or [])
    lock = threading.Lock()  # one discussion at a time, so a small PC is not overloaded

    def work(sender, text):
        with lock:
            try:
                reply = handle_command(team, text)
            except Exception as e:
                reply = f"Sorry, something went wrong: {e}"
        send_whatsapp(config, sender, reply)

    class Hook(BaseHTTPRequestHandler):
        def do_GET(self):  # Meta's one-time verification
            q = parse_qs(urlparse(self.path).query)
            if q.get("hub.verify_token", [""])[0] == wa.get("verify_token"):
                self._reply(200, q.get("hub.challenge", [""])[0])
            else:
                self._reply(403, "wrong verify token")

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            self._reply(200, "ok")  # answer Meta at once, think afterwards
            for entry in data.get("entry", []):
                for change in entry.get("changes", []):
                    for msg in change.get("value", {}).get("messages", []):
                        sender = msg.get("from")
                        if allowed and sender not in allowed:
                            print(f"Ignored message from unknown number {sender}")
                            continue
                        if msg.get("type") == "text":
                            threading.Thread(target=work, args=(sender, msg["text"]["body"]), daemon=True).start()

        def _reply(self, code, text):
            self.send_response(code)
            self.end_headers()
            self.wfile.write(text.encode())

        def log_message(self, *a):
            pass

    threading.Thread(target=_briefing_clock, args=(team, config, lock), daemon=True).start()
    port = int(wa.get("port", 8000))
    print(f"WhatsApp webhook listening on http://localhost:{port}  (press Ctrl+C to stop)")
    ThreadingHTTPServer(("0.0.0.0", port), Hook).serve_forever()


def _briefing_clock(team, config, lock, sender=None):
    """Proactive part: sends the morning briefing every day at the set time."""
    sender = sender or send_whatsapp
    at = config.get("daily_briefing_time", "09:00")
    last = None
    while True:
        now = datetime.now()
        if now.strftime("%H:%M") == at and last != now.date():
            last = now.date()
            with lock:
                text = team.daily_briefing()
            for number in config["whatsapp"].get("allowed_numbers") or []:
                sender(config, number, text)
        time.sleep(30)
