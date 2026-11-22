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
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

import media
from tasks import TaskList
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
        sc = self.mm.get("self_chat") or []
        self.self_chats = set([sc] if isinstance(sc, str) else sc)
        self.hinted = set()
        self.read_only = set()     # client groups: read, never post
        self.group_names = {}      # group id -> name
        self.daylog = defaultdict(list)  # today's messages per listed chat, for chases and the evening report
        self.day = datetime.now().date()
        here = os.path.dirname(os.path.abspath(__file__))
        self.rules_file = os.path.join(here, "grok_rules.local.md")
        self.tasks = TaskList(os.path.join(here, "task_list.local.csv"), self.mm.get("task_groups", {}))

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
            outbound = str(m.get("direction", "inbound")).lower() not in ("in", "inbound", "incoming")
            if (own and wa._number(chat) == own) or chat in self.self_chats:
                sender = own or sender  # your "Message yourself" chat: your control channel
            elif outbound:
                return  # your own messages to other people
        # anyone in a listed group (or a listed person): coded replies ("D22 DONE", "D10 under process") update the task list for free
        key = chat if is_group else sender
        who = self.names.get(sender) or ("Anirag (owner)" if sender in self.owner else
                                         (f"{m['sender_name']} ({sender})" if m.get("sender_name") else sender))
        if text and key not in self.read_only and not text.strip().lower().startswith(("tasks", "add task", "import task")):
            plain = self.names.get(sender) or m.get("sender_name") or ("Anirag" if sender in self.owner else sender)
            found = self.tasks.update_from_text(text, plain, self.group_names.get(chat, ""))
            if found:
                print("Task list updated:", ", ".join(f"{c} {st}" for c, st in found))
                lines = [l for l in text.splitlines() if l.strip()]
                if is_group and all(re.match(r"\s*[*_]*[A-Z]{1,3}\d", l) for l in lines):
                    with self.lock:
                        self.daylog[key].append(f"{datetime.now():%H:%M} {who}: {text[:300]}")
                        self.history[key].append(f"{who}: {text}")
                    return  # only status updates: saved, no Grok call needed
        everyone = route.get("everyone", self.mm.get("group_everyone", False))
        if sender not in self.people and not (is_group and everyone):
            return
        self.stats["seen"] += 1
        key = chat if is_group else sender
        who = self.names.get(sender) or ("Anirag (owner)" if sender in self.owner else
                                         (f"{m['sender_name']} ({sender})" if m.get("sender_name") else sender))
        if not is_group and sender in self.owner and not has_media:
            answer = self.owner_command(text)
            if answer is not None:  # a command: handled here, Grok is not called
                if answer:
                    wa.send(self.config, key, answer)
                return
        if has_media:
            label = "[picture]" if kind == "image" else f"[file: {att.get('file_name') or 'document'}]"
            text = f"{label} {text}".strip()
        with self.lock:
            if datetime.now().date() != self.day:
                self.day, self.daylog = datetime.now().date(), defaultdict(list)
            if len(self.daylog[key]) < 300:
                self.daylog[key].append(f"{datetime.now():%H:%M} {who}: {text[:300]}")
            self.history[key].append(f"{who}: {text}")
            if key in self.read_only:
                return  # client group: kept for the evening report, never answered
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
        m = re.fullmatch(r"(send|cancel)\s+(all|[\d ,]+)", low)
        if m:
            rows = self.team.memory.outbox("pending")
            if m.group(2) != "all":
                ids = {int(x) for x in re.findall(r"\d+", m.group(2))}
                rows = [r for r in rows if r[0] in ids]
            if not rows:
                return "Nothing waiting."
            if m.group(1) == "cancel":
                for r in rows:
                    self.team.memory.set_outbox(r[0], "cancelled")
                return "Cancelled " + ", ".join(f"#{r[0]}" for r in rows)
            self._auto_send(rows, force=True)
            return ""
        if low in ("outbox", "pending"):
            return self._pending_text() if self.team.memory.outbox("pending") else "Nothing waiting."
        if low in ("groups", "my groups"):
            return "Internal groups Grok can post in:\n" + "\n".join(f"- {n}" for n in sorted(self.postable_groups().values()))
        if low == "staff" and self.team.staff:
            return "\n".join(f"{p['name']} ({p.get('role') or '-'})" for p in self.team.staff.people) or "No staff listed."
        if low in ("tasks", "task list", "open tasks"):
            return self.tasks.summary(150)
        if low.startswith(("tasks:", "import tasks", "add tasks", "add task")):
            added = self.tasks.import_lines(text.split(":", 1)[1] if ":" in text.splitlines()[0] else "\n".join(text.splitlines()[1:]))
            return f"Task list: added/updated {', '.join(added)}." if added else "No coded lines found. Use e.g. D5 | Deepak | task | 25 Nov"
        m = re.fullmatch(r"(done|close|drop|delete)\s+([a-z]{1,3}\d{1,3}[a-z]?)", low)
        if m:
            r = self.tasks.get(m.group(2))
            if not r:
                return f"No task {m.group(2).upper()}."
            if m.group(1) in ("drop", "delete"):
                self.tasks.rows.remove(r)
            else:
                r.update(status="DONE", updated=datetime.now().strftime("%d %b %H:%M"), by="Anirag")
            self.tasks.save()
            return f"{r['code']} {'removed' if m.group(1) in ('drop', 'delete') else 'marked DONE'}."
        if low in ("help", "commands"):
            return ("Commands: tasks | tasks: <paste coded list> | done D5 | drop D5 | remember: ... | facts | groups | "
                    "staff | outbox | send all | send 3 | cancel 3. "
                    "Anything else goes to Grok, e.g. 'post in HR group: ...' or 'tell Komal to ...'.")
        return None

    # ---------- Grok's instructions: MESSAGE <person>: ... / GROUP <group>: ... ----------
    def postable_groups(self):
        """Internal groups Grok may post in: listed groups that are not read-only (client) groups."""
        return {c: self.group_names.get(c, c) for c in getattr(self, "groups", set()) if c not in self.read_only}

    def find_group(self, name):
        """(group id, None) or (None, reason)."""
        want = name.strip().lower()
        everything = {c: self.group_names.get(c, c) for c in getattr(self, "groups", set())}
        hits = [c for c, n in everything.items() if wa.group_matches(want, {"chat": c, "name": n})]
        if not hits:  # also try word by word, e.g. "HR group" -> "Lucent HR Recruitment"
            words = [w for w in re.findall(r"[a-z0-9&]+", want) if w not in ("group", "the", "lucent", "in", "team")]
            hits = [c for c, n in everything.items() if words and all(w in n.lower() for w in words)]
        if not hits:
            return None, f"no group called '{name}'"
        exact = [c for c in hits if everything[c].lower() == want]
        hits = exact or hits
        if any(c in self.read_only for c in hits):
            return None, f"'{everything[hits[0]]}' is a client / read-only group, so nothing was posted there"
        if len(hits) > 1:
            return None, f"'{name}' fits several groups ({', '.join(everything[c] for c in hits[:4])}); be more specific"
        return hits[0], None

    def find_person(self, name):
        """(person, None) or (None, reason). Exact name first, then a unique partial match."""
        people = self.team.staff.people if self.team.staff else []
        want = name.strip().lower()
        hits = [p for p in people if p["name"].lower() == want] or \
               [p for p in people if want in p["name"].lower()] or \
               [p for p in people if want.split()[0] in p["name"].lower().split()]
        names = {p["name"] for p in hits}
        if not hits:
            return None, f"'{name}' is not in contacts.local.json"
        if len(names) > 1:
            return None, f"'{name}' fits several people ({', '.join(sorted(names)[:4])}); use the full name"
        return hits[0], None

    def deliver(self, reply):
        """Returns (text for the current chat, notes for the owner)."""
        return self._deliver(reply)

    def _deliver(self, reply):
        """Split Grok's reply: text for the current chat, plus MESSAGE / GROUP blocks sent elsewhere.
        A block runs until the next block or ACTION line. Returns the text for the current chat."""
        here, blocks, cur = [], [], None
        for line in reply.splitlines():
            m = re.match(r"\s*\**\s*(MESSAGE|GROUP)\s+([^:\n]{2,60}?)\s*\**\s*:\s*(.*)", line)
            if m:
                cur = [m.group(1).upper(), m.group(2).strip(), [m.group(3)]]
                blocks.append(cur)
            elif re.match(r"\s*ACTION:", line):
                cur = None
                here.append(line)
            elif re.match(r"\s*(TASK|UPDATE)\s+[A-Z]{1,3}\d", line):
                cur = None
                body = re.sub(r"^\s*(TASK|UPDATE)\s+", "", line)
                if line.strip().upper().startswith("TASK"):
                    self.tasks.import_lines(body)
                else:
                    self.tasks.update_from_text(body.replace("|", " "), "CEO")
            elif cur:
                cur[2].append(line)
            else:
                here.append(line)
        # tasks are saved, but nobody is messaged from an ACTION line (only explicit MESSAGE / GROUP)
        for owner, task, due in re.findall(r"ACTION:\s*([a-z]+)\s*\|\s*([^|\n]+)\|\s*([^\n]+)", reply):
            if owner in self.team.agents:
                self.team.memory.add_task(owner, task.strip(), due.strip())
        problems, new = [], []
        for kind, target, lines in blocks:
            body = "\n".join(lines).strip()
            if not body:
                continue
            if kind == "GROUP":
                chat, why = self.find_group(target)
                label = self.group_names.get(chat, target)
            else:
                person, why = self.find_person(target)
                chat, label = (person["phone"], person["name"]) if person else (None, target)
            if not chat:
                problems.append(why)
                continue
            new.append(self.team.memory.add_outbox(None, ("group " if kind == "GROUP" else "") + label, chat, body))
        rows = [r for r in self.team.memory.outbox("pending") if r[0] in new]
        notes = []
        if rows:
            if self.team.staff and self.team.staff.mode == "auto":
                notes.append(self._auto_send(rows, quiet=True))
            else:
                notes.append(self._pending_text())
        if problems:
            notes.append("⚠️ Not sent: " + "; ".join(problems) + ".")
        return self.team._clean("\n".join(here)), "\n\n".join(n for n in notes if n)

    def _pending_text(self):
        rows = self.team.memory.outbox("pending")
        return ("Waiting for your OK:\n" + "\n".join(f"#{r[0]} to {r[2]}: {r[4].splitlines()[0][:120]}" for r in rows) +
                "\nReply 'send all', 'send 3' or 'cancel 3'.")

    def _notify(self, text):
        """Owner-only notices (drafts, copies, limits, reports) go ONLY to your own self-chat."""
        if self.owner:
            wa.send(self.config, self.owner[0], text)

    def _auto_send(self, rows, force=False, quiet=False):
        """staff_messages "auto": Grok's messages go straight to staff (listed contacts only),
        within a daily cap, and you get a copy in your self-chat."""
        cap = int(self.mm.get("max_staff_messages_per_day", 60))
        if self.stats.get("staff_day") != datetime.now().date():
            self.stats.update(staff_day=datetime.now().date(), staff_sent=0)
        if not force and self.stats["staff_sent"] + len(rows) > cap:
            msg = f"Today's limit of {cap} staff messages is reached, so these wait for you:\n" + self._pending_text()
            return msg if quiet else self._notify(msg)
        done = []
        for r in rows:
            try:
                wa.send(self.config, r[3], r[4])
                self.team.memory.set_outbox(r[0], "sent")
                done.append(f"#{r[0]} to {r[2]}")
                self.daylog[r[3]].append(f"{datetime.now():%H:%M} CEO: {r[4][:300]}")
                self.history[r[3]].append(f"CEO: {r[4]}")
            except Exception as e:
                done.append(f"#{r[0]} to {r[2]} FAILED: {e}")
        result = "Sent: " + ", ".join(done)
        self.stats["staff_sent"] += len(rows)
        print(result)
        copy = "\n\n".join(f"✅ Sent to {r[2]}:\n{r[4]}" for r in rows if f"#{r[0]} to {r[2]} FAILED" not in result)
        fails = [d for d in done if "FAILED" in d]
        msg = (copy + ("\n\n⚠️ " + "; ".join(fails) if fails else "")).strip()
        return msg if quiet else self._notify(msg)

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
                    wait = 5 if p["reply_to"] in self.owner else self.batch_seconds  # your own chat: answer fast
                    if now - p["last"] >= wait or now - p["first"] >= 3 * wait:
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

    def _rules(self):
        """Your standing rules for Grok, from grok_rules.local.md (private, stays on your PC)."""
        if os.path.exists(self.rules_file):
            with open(self.rules_file, encoding="utf-8-sig") as f:
                return "\n\nOWNER'S STANDING RULES (always follow):\n" + f.read()[:12000]
        return ""

    def _briefing(self, bot="ceo"):
        if bot != "ceo" and bot in self.team.agents:
            # a department head: its own brief and data from lucent-kb, plus WhatsApp rules
            return (self.team._system(self.team.agents[bot]) +
                    "\nYou are answering on WhatsApp: reply short (max 6 lines), in the language used. "
                    "If no answer is needed, reply exactly NO_REPLY." + self._rules())
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
        send_rule = ("It is sent straight away and the owner gets a copy. Be polite and specific; never message "
                     "customers or outside parties." if self.team.staff and self.team.staff.mode == "auto" else
                     "The owner approves each message before it is sent, so never say it was already sent.")
        groups_list = "\n".join(f"- {n}" for n in sorted(self.postable_groups().values())) or "- none"
        people = "\n".join(f"- {p['name']}: {self.team.agents.get(p.get('role'), {}).get('title', p.get('role') or 'role not set')}"
                           + (f" (in {p['group']})" if p.get("group") else "")
                           for p in (self.team.staff.people if self.team.staff else [])[:150]) or "- none listed yet"
        return (
            f"You are the CEO of {self.team.company}, working on WhatsApp with the owner "
            f"({self.team.owner}) and plant staff. You learn the business, ask staff short questions, "
            "assign work and follow up so that people stay accountable.\n"
            "Rules: reply like a person on WhatsApp, short (max 6 lines), in the language they used "
            "(Hindi/Hinglish is fine). Never invent numbers. Only the owner's explicit approvals count. "
            "Ask the owner before money, approvals, hiring/firing, or messages to outside parties. "
            "If the message needs no answer from you, reply exactly NO_REPLY.\n"
            "To give someone a task, add a line: ACTION: <dept id> | <task> | <due>  "
            f"(dept ids: {', '.join(self.team.agents)}).\n"
            "To send something to ANOTHER chat, end your reply with blocks like these (each block runs until "
            "the next block):\n"
            "GROUP <group name>: <text>   -> posts in that internal WhatsApp group\n"
            "MESSAGE <person name>: <text>   -> sends to that one employee\n"
            "When the owner asks you to post in a group, use GROUP with that group, never MESSAGE to a person. "
            "Use only group names and people from the lists below; client groups are read only, never post there. "
            "Your normal reply (before the blocks) goes back to the chat you are answering, so tell the owner "
            f"briefly what you sent. {send_rule}\n\n"
            f"Internal groups you can post in:\n{groups_list}\n\n"
            f"Staff you can message:\n{people}\n\n"
            f"Company knowledge:\n{brief}\n\nWhat the owner told you to remember:\n{facts}\n\nOpen tasks:\n{tasks}\n\n"
            f"{self.tasks.summary()}\n"
            "To add a coded item: TASK <code> | <owner> | <task> | <due>. To change one: UPDATE <code> | DONE/WIP/HOLD/NEED | <note>. "
            "Staff replies with codes update the list automatically."
            + self._rules()
        )

    def ask_ceo(self, p):
        if not self._budget_ok():
            if not self.cap_warned:
                self.cap_warned = True
                self._notify("Middleman: today's Grok limit is reached, so I'm holding messages. "
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
        text, notes = self.deliver(reply)
        if notes and p["reply_to"] in self.owner:  # you asked: one tidy answer in your chat
            text = (text.strip() + "\n\n" + notes).strip()
        elif notes:
            self._notify(notes)
        if not text.strip():
            return
        if p["reply_to"] in self.read_only:
            print("Not posting in a read-only group.")
            return
        wa.send(self.config, p["reply_to"], text)
        self.history[p["reply_to"]].append(f"CEO: {text}")
        self.stats["replies"] += 1
        print(f"Replied to {p['reply_to']}. Today: {self.stats['seen']} messages seen, "
              f"{self.stats['dropped']} dropped free, {self.stats['ai_calls']} Grok calls.")

    # ---------- running ----------
    def run(self):
        wm = self.config["whatsmcp"]
        own = wa._own_phone(wm.get("account_id"))
        if own:  # the linked number is yours: your self-chat is the control channel
            self.owner = [own]  # only YOUR number is the owner; other allowed numbers are just people
            self.people.add(own)
        groups = set()
        wanted = self.config["whatsapp"].get("allowed_groups") or []
        is_phone = lambda c: str(c).isdigit() and len(str(c)) <= 15  # longer digit strings are group ids
        group_routes = [r for r in self.routes_cfg if not is_phone(r.get("chat", ""))]
        for r in self.routes_cfg:
            if is_phone(r.get("chat", "")):
                phone = str(r["chat"])
                self.route_of[phone] = r
                if r.get("bot") != "ignore":
                    self.people.add(phone)
        if wanted or group_routes:
            ro = self.config["whatsapp"].get("read_only_groups") or []
            for g in wa.list_groups(wm.get("account_id")):
                self.group_names[g["chat"]] = g.get("name") or g["chat"]
                if any(wa.group_matches(x, g) for x in ro):
                    self.read_only.add(g["chat"])
                r = next((r for r in group_routes if wa.group_matches(r["chat"], g)), None)
                if r:
                    self.route_of[g["chat"]] = r
                    if r.get("bot") == "ignore":
                        continue
                elif not any(wa.group_matches(w, g) for w in wanted):
                    continue
                groups.add(g["chat"])
                print(f"Group: {g.get('name') or g['chat']}  ->  "
                      f"{'READ ONLY' if g['chat'] in self.read_only else (r or {}).get('bot', 'ceo')}")
        for phone, r in self.route_of.items():
            if phone.isdigit():
                print(f"Person: {self.names.get(phone, phone)}  ->  {r.get('bot', 'ceo')}")
        self.groups = groups
        threading.Thread(target=self.flush_loop, daemon=True).start()
        threading.Thread(target=self.schedule_loop, daemon=True).start()
        if os.path.exists(self.rules_file):
            print("Standing rules loaded from grok_rules.local.md")
        if self.config.get("email", {}).get("enabled"):
            from email_reader import EmailReader
            threading.Thread(target=EmailReader(self.config, self).loop, daemon=True).start()
        print(f"Middleman ready. People: {len(self.people)}, groups: {len(groups)}, "
              f"Grok caps: {self.cap_hour}/hour, {self.cap_day}/day.")
        if wm.get("receive", "poll") == "webhook":
            self._webhook(groups, own, int(self.config["whatsapp"].get("port", 8000)))
        else:
            self._poll(groups, own, int(self.mm.get("poll_seconds", wm.get("poll_seconds", 30))))

    # ---------- scheduled work: daily posts, chases, evening report ----------
    def schedule_loop(self):
        """middleman.schedule: [{"time": "10:00", "to": ["DRA Daily Work"] or "owner", "task": "..."}]"""
        done = set()
        while True:
            now = datetime.now()
            for i, job in enumerate(self.mm.get("schedule", [])):
                try:
                    hh, mm_ = [int(x) for x in str(job["time"]).split(":")]
                except (KeyError, ValueError):
                    continue
                due = now.replace(hour=hh, minute=mm_, second=0, microsecond=0)
                tag = (i, now.date())
                if tag in done or not (due <= now < due + timedelta(minutes=10)):
                    continue
                done.add(tag)
                try:
                    self.run_job(job)
                except Exception as e:
                    print("Scheduled job failed:", e)
            time.sleep(30)

    def run_job(self, job):
        to = job.get("to", "owner")
        if to == "owner":
            logs = []
            for chat, lines in self.daylog.items():
                name = self.group_names.get(chat) or self.names.get(chat) or ("your self-chat" if chat in self.owner else chat)
                logs.append(f"--- {name}{' (client group, read only)' if chat in self.read_only else ''} ---\n"
                            + "\n".join(lines)[-1500:])
            sent = self.team.memory.outbox("sent")[-40:]
            body = (self.tasks.summary(150) + "\n\nToday's messages in your groups:\n" + ("\n".join(logs)[-12000:] or "(none)") +
                    "\n\nStaff messages sent (latest):\n" + ("\n".join(f"to {r[2]}: {r[4][:150]}" for r in sent) or "(none)"))
            target = self.owner[0] if self.owner else None
            if target:
                print(f"Scheduled: {job.get('task', '')[:50]} -> owner")
                self.ask_ceo({"reply_to": target, "group": False, "route": {}, "media": [],
                              "header": f"Scheduled task ({job['time']}): {job.get('task', '')}", "lines": [body]})
            return
        for chat in list(self.groups):
            g = {"chat": chat, "name": self.group_names.get(chat, "")}
            if chat in self.read_only or not any(wa.group_matches(x, g) for x in to):
                continue
            if job.get("do") in ("post_tasks", "chase"):  # built from the task list, no AI needed
                text = self.tasks.post_for(g["name"]) if job["do"] == "post_tasks" else self.tasks.chase_for(g["name"])
                print(f"Scheduled {job['do']} -> {g['name']}: {len(text.splitlines())} lines")
                if text:
                    wa.send(self.config, chat, text)
                    self.daylog[chat].append(f"{datetime.now():%H:%M} CEO: {text[:300]}")
                continue
            print(f"Scheduled: {job.get('task', '')[:50]} -> {g['name'] or chat}")
            log = "\n".join(self.daylog.get(chat, [])) or "(no messages yet today)"
            self.ask_ceo({"reply_to": chat, "group": True, "route": self.route_of.get(chat, {}), "media": [],
                          "header": f"Scheduled task ({job['time']}) for group {g['name']}: {job.get('task', '')}",
                          "lines": [f"Today's messages in this group:\n{log[-4000:]}"]})

    def _poll(self, groups, own, every):
        """ONE WhatsMCP call per check: the newest messages from all chats, filtered here for free."""
        here = os.path.dirname(os.path.abspath(__file__))
        cursor_file = os.path.join(here, "whatsmcp_stream_cursor.json")
        cursor = json.load(open(cursor_file)).get("cursor") if os.path.exists(cursor_file) else None
        if cursor is None:
            cursor = self._latest_cursor()
        print(f"Checking all chats with one call every {every} s (about {86400 // max(every, 1)} WhatsMCP calls a day). "
              "Only the people and groups in your settings are passed on. Ctrl+C to stop.")
        seen = deque(maxlen=2000)
        while True:
            try:
                while True:
                    page = wa._call("GET", f"/messages?after_cursor={cursor}&limit=100")
                    skipped = 0
                    for m in page.get("messages", []):
                        if not self._listed(m, groups, own):
                            skipped += 1  # not on your lists: dropped at once, never printed, stored or sent
                            continue
                        if os.environ.get("DEBUG_WAMCP"):
                            print("seen:", {k: m.get(k) for k in ("direction", "peer", "chat", "kind")},
                                  (m.get("text") or "")[:40])
                        if m.get("message_id") in seen:
                            continue
                        seen.append(m.get("message_id"))
                        self.on_message(m, groups, own)
                    if skipped and os.environ.get("DEBUG_WAMCP"):
                        print(f"skipped {skipped} message(s) from unlisted chats")
                    cursor = page.get("next_cursor") or cursor
                    if not page.get("has_more"):
                        break
                with open(cursor_file, "w") as f:
                    json.dump({"cursor": cursor}, f)
            except Exception as e:
                print("WhatsMCP check failed:", e)
            time.sleep(every)

    def _listed(self, m, groups, own):
        """True only for your listed groups and people, your self-chat, or a '#selfchat' test message."""
        chat = str(m.get("chat") or "")
        if "@g.us" in chat:
            return chat in groups
        num = wa._number(chat)
        if chat in self.self_chats or (own and num == own):
            return True
        outbound = str(m.get("direction", "inbound")).lower() not in ("in", "inbound", "incoming")
        if num in self.people and not outbound:  # a listed person writing to you
            return True
        if outbound and str(m.get("text") or "").strip().lower().startswith("#selfchat"):
            print(f'Your self-chat id is {chat}. Put "self_chat": "{chat}" in the middleman block of config.local.json.')
        return False

    def _latest_cursor(self):
        """First start: jump to the newest message so old chats are not answered."""
        def has_after(c):
            return bool(wa._call("GET", f"/messages?after_cursor={c}&limit=1").get("messages"))
        lo, hi = 0, 1024
        while has_after(hi):
            lo, hi = hi, hi * 2
        while hi - lo > 1:
            mid = (lo + hi) // 2
            lo, hi = (mid, hi) if has_after(mid) else (lo, mid)
        return hi if not has_after(hi) else lo

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
