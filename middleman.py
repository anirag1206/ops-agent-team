"""The cheap middleman between WhatsApp and your expensive CEO bot (Grok).

    WhatsApp  <->  WhatsMCP  <->  MIDDLEMAN (this file, free, no AI)  <->  Grok (CEO)

What the middleman does, every few seconds, at no AI cost:
  1. Fetches new messages, but ONLY from the people and groups you allow.
  2. Drops what needs no CEO: "ok", "👍", "thanks", stickers, empty messages,
     the bot's own replies, and anything from people not on your lists.
  3. Waits a short moment (batch_seconds) so that 5 quick messages from one person
     become ONE question to Grok instead of five.
  4. Sends that one bundle to Grok with a SHORT briefing (not the whole history),
     gets the CEO's reply and posts it back on WhatsApp in the same chat or group.
  5. Stops calling Grok if the hourly or daily cap is reached, and tells you once.
Grok can also answer NO_REPLY when nothing needs saying; then nothing is posted.

Run it with:  python run.py middleman
Settings are under "middleman" in config.json (override them in config.local.json).
"""
import json
import os
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

import whatsmcp_bridge as wa
from brain import Brain

SMALL_TALK = {
    "ok", "okay", "okk", "k", "kk", "ji", "ok ji", "okay ji", "ok sir", "yes", "yes sir", "ha", "haan", "han",
    "hmm", "hm", "thanks", "thank you", "thanku", "thx", "ty", "noted", "done", "sure", "fine", "good",
    "good morning", "gm", "good night", "gn", "welcome", "👍", "🙏", "👌", "✅", "🙂", "😊",
}


def worth_forwarding(text, kind="text"):
    """Free, rule-based filter: True if the message may need the CEO."""
    t = (text or "").strip()
    if kind not in ("text", "", None, "image", "document") or not t:
        return False
    low = re.sub(r"[.!,\s]+$", "", t.lower())
    if low in SMALL_TALK:
        return False
    if len(low) <= 2:
        return False
    if not re.search(r"[a-z0-9ऀ-ॿ]", low):  # only emojis or symbols
        return False
    return True


class Middleman:
    def __init__(self, config, team):
        self.config = config
        self.mm = config.get("middleman", {})
        self.team = team
        brain_cfg = dict(config)
        brain_cfg["brain"] = self.mm.get("brain", "online")
        for k in ("online_url", "online_model", "online_key_env", "claude_model"):
            if k in self.mm:
                brain_cfg[k] = self.mm[k]
        self.brain = Brain(brain_cfg)
        self.batch_seconds = int(self.mm.get("batch_seconds", 45))
        self.cap_hour = int(self.mm.get("max_ai_calls_per_hour", 20))
        self.cap_day = int(self.mm.get("max_ai_calls_per_day", 150))
        self.owner = [n.lstrip("+") for n in config["whatsapp"].get("allowed_numbers") or []]
        staff = team.staff.people if team.staff else []
        self.names = {p["phone"]: p["name"] for p in staff}
        self.people = set(self.owner) | set(self.names)
        self.history = defaultdict(lambda: deque(maxlen=int(self.mm.get("history_messages", 10))))
        self.pending = {}
        self.calls = deque()
        self.stats = {"seen": 0, "dropped": 0, "ai_calls": 0, "replies": 0, "day": datetime.now().date()}
        self.cap_warned = False
        self.lock = threading.Lock()

    # ---------- 1-3: receive, filter, batch ----------
    def on_message(self, m, groups, own):
        text = (m.get("text") or m.get("body") or "").strip()
        chat = str(m.get("chat") or m.get("chat_jid") or "")
        sender = wa._number(m.get("peer"))
        if not text or text.startswith(wa.MARK.strip()):
            return
        is_group = "@g.us" in chat
        if is_group and chat not in groups:
            return
        if not is_group:
            if own and own in (sender, wa._number(chat)):
                sender = own  # message to yourself
            elif str(m.get("direction", "inbound")).lower() not in ("in", "inbound", "incoming"):
                return  # your own messages to other people
        if sender not in self.people and not (is_group and self.mm.get("group_everyone", False)):
            return
        self.stats["seen"] += 1
        key = chat if is_group else sender
        who = self.names.get(sender) or ("Anirag (owner)" if sender in self.owner else sender)
        with self.lock:
            self.history[key].append(f"{who}: {text}")
            if not worth_forwarding(text, m.get("kind", "text")):
                self.stats["dropped"] += 1
                return
            p = self.pending.setdefault(key, {"reply_to": key, "lines": [], "first": time.time(), "group": is_group})
            p["lines"].append(f"{who}: {text}")
            p["last"] = time.time()

    def flush_loop(self):
        while True:
            time.sleep(3)
            now = time.time()
            ready = []
            with self.lock:
                for key, p in list(self.pending.items()):
                    # wait until the person stops typing for batch_seconds (max 3x that overall)
                    if now - p["last"] >= self.batch_seconds or now - p["first"] >= 3 * self.batch_seconds:
                        ready.append(self.pending.pop(key))
            for p in ready:
                self.ask_ceo(p)

    # ---------- 4-5: ask the CEO, within budget ----------
    def _budget_ok(self):
        now = time.time()
        while self.calls and now - self.calls[0] > 3600:
            self.calls.popleft()
        if datetime.now().date() != self.stats["day"]:
            self.stats.update(day=datetime.now().date(), ai_calls=0, seen=0, dropped=0, replies=0)
            self.cap_warned = False
        return len(self.calls) < self.cap_hour and self.stats["ai_calls"] < self.cap_day

    def _briefing(self):
        ceo = self.team.agents["ceo"]
        kb = self.team.kb
        brief = ""
        if kb and kb.ok:
            brief = kb.company_snapshot() + "\n\n" + kb.read("decisions.md", 3000)
            if self.mm.get("full_ceo_notes"):
                brief += "\n\n" + kb.read(ceo.get("kb_brief", ""), 3000)
        tasks = "\n".join(f"#{t[0]} {self.team.agents.get(t[1], {}).get('title', t[1])}: {t[2]} (due {t[3]})"
                          for t in self.team.memory.open_tasks()[:15]) or "none"
        return (
            f"You are the CEO of {self.team.company}, working on WhatsApp with the owner "
            f"({self.team.owner}) and plant staff. You learn the business, ask staff short questions, "
            "assign work and follow up so that people stay accountable.\n"
            "Rules: reply like a person on WhatsApp, short (max 6 lines), in the language they used "
            "(Hindi/Hinglish is fine). Never invent numbers. Only the owner's explicit approvals count. "
            "Ask the owner before money, approvals, hiring/firing, or messages to outside parties. "
            "If the message needs no answer from you, reply exactly NO_REPLY.\n"
            "To give someone a task, add a line: ACTION: <dept id> | <task> | <due>  "
            f"(dept ids: {', '.join(self.team.agents)}).\n\n"
            f"Company knowledge:\n{brief}\n\nOpen tasks:\n{tasks}"
        )

    def ask_ceo(self, p):
        if not self._budget_ok():
            if not self.cap_warned:
                self.cap_warned = True
                for o in self.owner:
                    wa.send(self.config, o, "Middleman: today's Grok limit is reached, so I'm holding messages. "
                                            "Raise max_ai_calls_per_day in config.local.json if needed.")
            print("Budget cap reached; skipped:", p["lines"][-1][:60])
            return
        recent = "\n".join(list(self.history[p["reply_to"]])[:-len(p["lines"])] or ["(none)"])
        prompt = (f"{'Group chat' if p['group'] else 'Private chat'}. Earlier messages:\n{recent}\n\n"
                  f"New message(s):\n" + "\n".join(p["lines"]) + "\n\nYour reply:")
        self.calls.append(time.time())
        self.stats["ai_calls"] += 1
        try:
            reply = self.brain.think(self._briefing(), prompt, int(self.mm.get("max_reply_tokens", 400)))
        except Exception as e:
            print("CEO call failed:", e)
            return
        self.team.memory.log("ceo", "whatsapp", f"IN: {' | '.join(p['lines'])}\nOUT: {reply}")
        if reply.strip().upper().startswith("NO_REPLY"):
            print("CEO chose not to reply.")
            return
        self.team._save_actions(reply)
        text = self.team._clean(reply)
        if self.team.staff:
            extra = self.team.staff.auto_send_if_allowed() or ""
            if extra:
                print(extra)
        wa.send(self.config, p["reply_to"], text)
        self.history[p["reply_to"]].append(f"CEO: {text}")
        self.stats["replies"] += 1
        print(f"Replied to {p['reply_to']}. Today: {self.stats['seen']} messages seen, "
              f"{self.stats['dropped']} dropped free, {self.stats['ai_calls']} Grok calls.")

    # ---------- running ----------
    def run(self):
        wm = self.config["whatsmcp"]
        own = wa._own_phone(wm.get("account_id"))
        groups = set()
        wanted = self.config["whatsapp"].get("allowed_groups") or []
        if wanted:
            for g in wa.list_groups(wm.get("account_id")):
                if any(w.lower() in str(g.get("name", "")).lower() for w in wanted):
                    groups.add(g["chat"])
                    print("Group:", g.get("name"))
        threading.Thread(target=self.flush_loop, daemon=True).start()
        print(f"Middleman ready. People: {len(self.people)}, groups: {len(groups)}, "
              f"Grok caps: {self.cap_hour}/hour, {self.cap_day}/day.")
        if wm.get("receive", "poll") == "webhook":
            self._webhook(groups, own, int(self.config["whatsapp"].get("port", 8000)))
        else:
            self._poll(groups, own, int(self.mm.get("poll_seconds", wm.get("poll_seconds", 30))))

    def _poll(self, groups, own, every):
        chats = sorted(self.people | groups)
        here = os.path.dirname(os.path.abspath(__file__))
        cursor_file = os.path.join(here, "whatsmcp_cursors.json")
        cursors = json.load(open(cursor_file)) if os.path.exists(cursor_file) else {}
        for chat in chats:
            if chat not in cursors:  # skip old messages the first time
                c = 0
                while True:
                    page = wa._call("GET", f"/messages?chat={quote(chat, safe='')}&after_cursor={c}&limit=200")
                    c = page.get("next_cursor") or c
                    if not page.get("has_more"):
                        break
                cursors[chat] = c
        per_day = len(chats) * 86400 // max(every, 1)
        print(f"Checking {len(chats)} chats every {every} s (about {per_day} WhatsMCP calls a day). Ctrl+C to stop.")
        seen = set()
        while True:
            for chat in chats:
                try:
                    page = wa._call("GET", f"/messages?chat={quote(chat, safe='')}&after_cursor={cursors[chat]}&limit=50")
                    for m in page.get("messages", []):
                        if m.get("message_id") not in seen:
                            seen.add(m.get("message_id"))
                            self.on_message(m, groups, own)
                    cursors[chat] = page.get("next_cursor") or cursors[chat]
                except Exception as e:
                    print(f"Check of {chat} failed:", e)
            with open(cursor_file, "w") as f:
                json.dump(cursors, f)
            time.sleep(every)

    def _webhook(self, groups, own, port):
        mm = self

        class Hook(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(200)
                self.end_headers()
                data = json.loads(raw or b"{}")
                if data.get("event") == "message.inbound":
                    mm.on_message({"text": data.get("body"), "chat": data.get("chat_jid"), "peer": data.get("peer"),
                                   "kind": data.get("kind", "text"), "direction": "inbound"}, groups, own)

            def log_message(self, *a):
                pass

        print(f"Middleman webhook on http://localhost:{port} (instant, no polling). Ctrl+C to stop.")
        ThreadingHTTPServer(("0.0.0.0", port), Hook).serve_forever()
