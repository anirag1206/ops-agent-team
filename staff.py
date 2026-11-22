"""Talking to plant staff on WhatsApp.

How it works:
  1. You list your staff in contacts.local.json (private, stays on your PC):
       {"people": [{"name": "Mehboob Khan", "role": "eng", "phone": "9198XXXXXXXX"}]}
     "role" is the agent id from team.json (ceo, cos, cfo, coo, qa, eng, scm, fd, ra, bd, dom, hr).
  2. When the CEO agent assigns an action item to a department that has a person listed,
     the team DRAFTS a WhatsApp message to that person.
  3. Nothing is sent until you reply "send 3", "send all" (or "cancel 3").
     Set "staff_messages": "auto" in config.local.json to send without asking.
  4. Staff answer with "done 3" or "update 3: <news>" (or "tasks" to see theirs).
     Their news is saved to that department's memory and forwarded to you.
     Any other message from staff is ignored, so normal chats are never answered.
"""
import json
import os
import re


class Staff:
    def __init__(self, path, memory, agents, owner_numbers, mode="approve"):
        self.memory = memory
        self.agents = agents
        self.owner_numbers = [n.lstrip("+") for n in owner_numbers]
        self.mode = mode
        self.people = []
        if os.path.exists(path):
            with open(path, encoding="utf-8-sig") as f:
                self.people = [p for p in json.load(f).get("people", []) if p.get("phone")]
        for p in self.people:
            p["phone"] = "".join(ch for ch in str(p["phone"]) if ch.isdigit())
        self.send_fn = None  # set by the WhatsApp bridge: send_fn(phone, text)

    # ---------- lookups ----------
    def by_phone(self, phone):
        return next((p for p in self.people if p["phone"] == phone), None)

    def by_role(self, role):
        return next((p for p in self.people if p.get("role") == role), None)

    def by_name(self, name):
        name = name.lower().strip()
        return next((p for p in self.people if name and name in p["name"].lower()), None)

    def title(self, role):
        return self.agents.get(role, {}).get("title", role)

    # ---------- drafting ----------
    def draft_for_task(self, task_id, role, task, due):
        person = self.by_role(role)
        if not person:
            return None
        text = (f"Task #{task_id} from the Lucent leadership team: {task}\n"
                f"Due: {due}\n"
                f"Reply 'done {task_id}' when finished, or 'update {task_id}: <your update>'.")
        return self.memory.add_outbox(task_id, person["name"], person["phone"], text)

    def draft_direct(self, name, text):
        person = self.by_name(name)
        if not person:
            return None
        return self.memory.add_outbox(None, person["name"], person["phone"], text)

    def pending_text(self):
        rows = self.memory.outbox("pending")
        if not rows:
            return ""
        lines = [f"#{r[0]} to {r[2]}: {r[4].splitlines()[0][:120]}" for r in rows]
        return ("Messages to staff waiting for your OK:\n" + "\n".join(lines) +
                "\nReply 'send all', 'send 3' or 'cancel 3'. 'outbox' shows them in full.")

    # ---------- sending ----------
    def send(self, ids=None):
        rows = [r for r in self.memory.outbox("pending") if ids is None or r[0] in ids]
        if not rows:
            return "Nothing to send."
        if not self.send_fn:
            return "WhatsApp is not running, so nothing was sent. Start start_whatsapp.bat and try again."
        done = []
        for r in rows:
            try:
                self.send_fn(r[3], r[4])
                self.memory.set_outbox(r[0], "sent")
                done.append(f"#{r[0]} to {r[2]}")
            except Exception as e:
                done.append(f"#{r[0]} to {r[2]} FAILED: {e}")
        return "Sent: " + ", ".join(done)

    def auto_send_if_allowed(self):
        if self.mode == "auto" and self.memory.outbox("pending"):
            return self.send()
        return ""

    # ---------- owner commands ----------
    def owner_command(self, low, text):
        """Returns a reply, or None if this is not a staff command."""
        if low == "outbox":
            rows = self.memory.outbox("pending")
            return "\n\n".join(f"#{r[0]} to {r[2]} ({r[3]}):\n{r[4]}" for r in rows) or "No messages waiting."
        if low == "send all":
            return self.send()
        m = re.fullmatch(r"(send|cancel)\s+([\d ,]+)", low)
        if m:
            ids = [int(x) for x in re.findall(r"\d+", m.group(2))]
            if m.group(1) == "send":
                return self.send(ids)
            for i in ids:
                self.memory.set_outbox(i, "cancelled")
            return "Cancelled " + ", ".join(f"#{i}" for i in ids)
        m = re.match(r"message\s+([^:]+):\s*(.+)", text.strip(), re.I | re.S)
        if m:
            oid = self.draft_direct(m.group(1), m.group(2).strip())
            if not oid:
                return f"I don't have '{m.group(1).strip()}' in contacts.local.json."
            return self.auto_send_if_allowed() or self.pending_text()
        if low == "staff":
            return "\n".join(f"{p['name']} - {self.title(p.get('role'))} - {p['phone']}" for p in self.people) \
                or "No staff listed yet. Add them to contacts.local.json."
        return None

    # ---------- messages from staff ----------
    def staff_message(self, phone, text):
        """Returns (reply_to_staff, note_to_owner) or (None, None) to ignore."""
        person = self.by_phone(phone)
        if not person:
            return None, None
        low = text.strip().lower()
        role = person.get("role")
        if low == "tasks":
            rows = self.memory.open_tasks(role)
            return ("\n".join(f"#{r[0]} {r[2]} (due {r[3]})" for r in rows) or "You have no open tasks."), None
        m = re.match(r"done\s+#?(\d+)", low)
        if m:
            tid = int(m.group(1))
            task = self.memory.task(tid)
            if not task or task[1] != role:
                return f"Task #{tid} is not one of yours.", None
            self.memory.close_task(tid)
            self.memory.log(role, f"task #{tid}", f"{person['name']} marked done: {task[2]}")
            return f"Thanks, task #{tid} is marked done.", f"{person['name']} ({self.title(role)}) finished task #{tid}: {task[2]}"
        m = re.match(r"update\s+#?(\d+)\s*:?\s*(.+)", text.strip(), re.I | re.S)
        if m:
            tid, news = int(m.group(1)), m.group(2).strip()
            self.memory.log(role, f"task #{tid}", f"Update from {person['name']}: {news}")
            return f"Thanks, update on task #{tid} noted.", f"Update from {person['name']} ({self.title(role)}) on task #{tid}: {news}"
        return None, None  # normal chat: ignore
