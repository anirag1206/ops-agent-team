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

import media
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
        self.brains = {"default": self.brain}
        self.routes_cfg = config.get("routes", [])
        self.route_of = {}  # chat key (phone or group id) -> route dict
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
        kind = m.get("kind") or "text"
        att = m.get("attachment") or {}
        has_media = ((kind == "image" and self.mm.get("read_pictures", True)) or
                     (kind == "document" and self.mm.get("read_files", True))) and m.get("message_id")
        if (not text and not has_media) or text.startswith(wa.MARK.strip()):
            return
        is_group = "@g.us" in chat
        if is_group and chat not in groups:
            return
        route = self.route_of.get(chat if is_group else sender, {})
        if route.get("bot") == "ignore":
            return
        if not is_group:
            if own and own in (sender, wa._number(chat)):
                sender = own  # message to yourself
            elif str(m.get("direction", "inbound")).lower() not in ("in", "inbound", "incoming"):
                return  # your own messages to other people
        everyone = route.get("everyone", self.mm.get("group_everyone", False))
        if sender not in self.people and not (is_group and everyone):
            return
        self.stats["seen"] += 1
        key = chat if is_group else sender
        who = self.names.get(sender) or ("Anirag (owner)" if sender in self.owner else sender)
        if not is_group and sender in self.owner and not has_media:
            answer = self.owner_command(text)
            if answer:
                wa.send(self.config, key, answer)
                return
        if has_media:
            label = "[picture]" if kind == "image" else f"[file: {att.get('file_name') or 'document'}]"
            text = f"{label} {text}".strip()
        with self.lock:
            self.history[key].append(f"{who}: {text}")
            if not has_media and not worth_forwarding(text, kind):
                self.stats["dropped"] += 1
                return
            p = self.pending.setdefault(key, {"reply_to": key, "lines": [], "first": time.time(), "group": is_group,
                                              "route": route, "media": []})
            p["lines"].append(f"{who}: {text}")
            if has_media:
                p["media"].append({"id": m["message_id"], "kind": kind, "name": att.get("file_name", "")})
            p["last"] = time.time()

    def owner_command(self, text):
        """A few free commands from the owner that need no AI. Returns a reply or None."""
        low = text.strip().lower()
        if low.startswith("remember:"):
            self.team.memory.add_fact(text.split(":", 1)[1].strip())
            return "Noted. I'll include this every time I brief Grok."
        if low in ("facts", "what do you remember"):
            return "\n".join("- " + f for f in self.team.memory.facts()) or "Nothing saved yet. Send 'remember: ...'."
        if self.team.staff:
            return self.team.staff.owner_command(low, text)
        return None

    def _attachments(self, p):
        """Download WhatsApp pictures and files of one bundle: (pictures for Grok, text from files)."""
        images, notes = list(p.get("images", [])), []
        acc = self.config["whatsmcp"].get("account_id")
        for a in p.get("media", [])[:int(self.mm.get("max_files_per_message", 4))]:
            try:
                data, mime = media.fetch_whatsapp(acc, a["id"])
            except Exception as e:
                print("Attachment download failed:", e)
                continue
            pic = media.as_image(data, mime) if a["kind"] == "image" else None
            if pic:
                images.append(pic)
            elif data:
                text = media.file_text(data, mime, a["name"])
                notes.append(f"Text of file {a['name'] or ''}:\n{text}" if text
                             else f"(File {a['name'] or ''} could not be read on this PC.)")
        return images, "\n\n".join(notes)

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

    def _brain_for(self, name):
        """name: None/'default' = middleman brain, 'grok', 'claude', or 'online' (Groq/Gemini settings)."""
        name = name or "default"
        if name not in self.brains:
            cfg = dict(self.config)
            if name == "grok":
                cfg.update(brain="online", online_url="https://api.x.ai/v1", online_key_env="XAI_API_KEY",
                           online_model=self.mm.get("online_model", "grok-4-fast"))
            else:
                cfg["brain"] = name
            self.brains[name] = Brain(cfg)
        return self.brains[name]

    def _briefing(self, bot="ceo"):
        if bot != "ceo" and bot in self.team.agents:
            # a department head: its own brief and data from lucent-kb, plus WhatsApp rules
            return (self.team._system(self.team.agents[bot]) +
                    "\nYou are answering on WhatsApp: reply short (max 6 lines), in the language used. "
                    "If no answer is needed, reply exactly NO_REPLY.")
        ceo = self.team.agents["ceo"]
        kb = self.team.kb
        brief = ""
        if kb and kb.ok:
            brief = kb.company_snapshot() + "\n\n" + kb.read("decisions.md", 3000)
            if self.mm.get("full_ceo_notes"):
                brief += "\n\n" + kb.read(ceo.get("kb_brief", ""), 3000)
        tasks = "\n".join(f"#{t[0]} {self.team.agents.get(t[1], {}).get('title', t[1])}: {t[2]} (due {t[3]})"
                          for t in self.team.memory.open_tasks()[:15]) or "none"
        facts = "\n".join("- " + f for f in self.team.memory.facts()) or "- none yet"
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
            f"Company knowledge:\n{brief}\n\nWhat the owner told you to remember:\n{facts}\n\nOpen tasks:\n{tasks}"
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
        earlier = [h for h in self.history[p["reply_to"]] if h not in p["lines"]]
        recent = "\n".join(earlier or ["(none)"])
        images, files = self._attachments(p)
        header = p.get("header") or ("Group chat" if p["group"] else "Private chat")
        prompt = (f"{header}. Earlier messages:\n{recent}\n\n"
                  f"New message(s):\n" + "\n".join(p["lines"]) +
                  (f"\n\n{files}" if files else "") +
                  (f"\n\n({len(images)} picture(s) attached, look at them.)" if images else "") + "\n\nYour reply:")
        route = p.get("route") or {}
        bot = route.get("bot", "ceo")
        brain = self._brain_for(route.get("brain"))
        self.calls.append(time.time())
        self.stats["ai_calls"] += 1
        try:
            if bot == "team":  # whole leadership team discusses (several AI calls, costs more)
                reply = self.team.handle("\n".join(p["lines"]))
            else:
                reply = brain.think(self._briefing(bot), prompt, int(self.mm.get("max_reply_tokens", 400)), images)
        except Exception as e:
            print("CEO call failed:", e)
            return
        self.team.memory.log(bot if bot in self.team.agents else "ceo", "whatsapp", f"IN: {' | '.join(p['lines'])}\nOUT: {reply}")
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
        group_routes = [r for r in self.routes_cfg if not str(r.get("chat", "")).isdigit()]
        for r in self.routes_cfg:
            if str(r.get("chat", "")).isdigit():
                phone = str(r["chat"])
                self.route_of[phone] = r
                if r.get("bot") != "ignore":
                    self.people.add(phone)
        if wanted or group_routes:
            for g in wa.list_groups(wm.get("account_id")):
                name = str(g.get("name", "")).lower()
                r = next((r for r in group_routes if str(r["chat"]).lower() in name), None)
                if r:
                    self.route_of[g["chat"]] = r
                    if r.get("bot") == "ignore":
                        continue
                elif not any(w.lower() in name for w in wanted):
                    continue
                groups.add(g["chat"])
                print(f"Group: {g.get('name')}  ->  {(r or {}).get('bot', 'ceo')}")
        for phone, r in self.route_of.items():
            if phone.isdigit():
                print(f"Person: {self.names.get(phone, phone)}  ->  {r.get('bot', 'ceo')}")
        threading.Thread(target=self.flush_loop, daemon=True).start()
        if self.config.get("email", {}).get("enabled"):
            from email_reader import EmailReader
            threading.Thread(target=EmailReader(self.config, self).loop, daemon=True).start()
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
                                   "kind": data.get("kind", "text"), "message_id": data.get("message_id"),
                                   "direction": "inbound"}, groups, own)

            def log_message(self, *a):
                pass

        print(f"Middleman webhook on http://localhost:{port} (instant, no polling). Ctrl+C to stop.")
        ThreadingHTTPServer(("0.0.0.0", port), Hook).serve_forever()
