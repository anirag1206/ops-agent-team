"""The coded task list (D1, B1, M1 ...) the middleman posts, chases and updates. No AI involved.

Kept in task_list.local.csv (private, opens in Excel):
    code, group, owner, task, due, status, updated, by, note
status is OPEN, WIP, HOLD, NEED or DONE.

Staff update it just by replying in the group, any of these work:
    D22 | DONE | apostilled COPP sent         D10-Cilnillion10 dossier under process
    D25 need GMP from purchase                 D7 hold
"""
import csv
import os
import re
import threading
from datetime import datetime

FIELDS = ["code", "group", "owner", "task", "due", "status", "updated", "by", "note"]
CODE = r"([A-Z]{1,3}\d{1,3}[a-z]?)"


def status_of(text):
    t = text.lower()
    if re.search(r"\b(done|completed?|closed|finished|delivered|ho gaya|hogaya)\b", t):
        return "DONE"
    if re.search(r"\bhold\b", t):
        return "HOLD"
    if re.search(r"\b(need|needed|required|waiting|follow[- ]?up|expired|pending (from|with))\b", t):
        return "NEED"
    return "WIP"


class TaskList:
    def __init__(self, path, group_of_prefix=None):
        self.path = path
        self.prefix_group = group_of_prefix or {}
        self.lock = threading.Lock()
        self.rows = []
        if os.path.exists(path):
            with open(path, encoding="utf-8-sig", newline="") as f:
                self.rows = [{k: (r.get(k) or "").strip() for k in FIELDS} for r in csv.DictReader(f) if r.get("code")]

    def save(self):
        with open(self.path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(self.rows)

    def get(self, code):
        return next((r for r in self.rows if r["code"].upper() == code.upper()), None)

    def group_for(self, code):
        letters = re.match(r"[A-Z]+", code.upper()).group(0)
        return self.prefix_group.get(letters, "")

    def upsert(self, code, **fields):
        r = self.get(code)
        if not r:
            r = {k: "" for k in FIELDS}
            r.update(code=code, status="OPEN", group=self.group_for(code))
            self.rows.append(r)
        r.update({k: v for k, v in fields.items() if v is not None and k in FIELDS})
        return r

    # ---------- staff replies ----------
    def update_from_text(self, text, who, group_name=""):
        """Read 'D22 ... DONE' style lines. Returns [(code, status)] for the codes found."""
        found = []
        with self.lock:
            for line in re.split(r"\n|(?<=[.;])\s+(?=[A-Z]{1,3}\d)", text):
                m = re.match(r"\s*[*_]*" + CODE + r"\b\s*[-|:.)–]?\s*(.*)", line.strip())
                if not m:
                    continue
                code, rest = m.group(1), m.group(2).strip(" -|")
                if len(code) < 2:
                    continue
                st = status_of(rest)
                fields = dict(status=st, updated=datetime.now().strftime("%d %b %H:%M"), by=who, note=rest[:200])
                if not self.get(code):
                    fields.update(task=rest[:120], owner=who, group=self.group_for(code) or group_name)
                self.upsert(code, **fields)
                found.append((code, st))
            if found:
                self.save()
        return found

    # ---------- owner import: paste a list ----------
    def import_lines(self, text):
        """Lines like 'D5 – Farmamed – Apichem originals – Deepak – due today' or 'D5 | Deepak | task | 25 Nov'."""
        added = []
        with self.lock:
            for line in text.splitlines():
                m = re.match(r"\s*" + CODE + r"\s*[-|:–—]\s*(.+)", line.strip())
                if not m:
                    continue
                piped = "|" in m.group(2)
                parts = [p.strip() for p in re.split(r"\s*\|\s*" if piped else "\\s+[\u2013\u2014-]\\s+", m.group(2)) if p.strip()]
                due = owner = ""
                if len(parts) > 1 and re.search(r"\b(due|today|overdue|pending|\d{1,2} ?[a-z]{3}|tbd)\b", parts[-1], re.I):
                    due = re.sub(r"^due\s*", "", parts.pop(), flags=re.I)
                if len(parts) > 1:  # 'code | owner | task | due'  or  'code - task - owner - due'
                    owner = re.sub(r"^owner:\s*", "", parts.pop(0) if piped else parts.pop(), flags=re.I)
                self.upsert(m.group(1), task=" – ".join(parts), owner=owner, due=due, status="OPEN")
                added.append(m.group(1))
            if added:
                self.save()
        return added

    # ---------- views ----------
    def open_items(self, group_name=None):
        out = [r for r in self.rows if r["status"] != "DONE"]
        if group_name is not None:
            g = group_name.lower()
            out = [r for r in out if r["group"] and (r["group"].lower() in g or g in r["group"].lower())]
        return out

    def line(self, r):
        bits = [r["code"], r["owner"] or "owner TBD", r["task"], ("due " + r["due"]) if r["due"] else ""]
        s = " | ".join(b for b in bits if b)
        if r["status"] not in ("OPEN", ""):
            s += f"  [{r['status']}{' ' + r['updated'] if r['updated'] else ''}]"
        return s

    def post_for(self, group_name):
        items = self.open_items(group_name)
        if not items:
            return ""
        return (f"Open items {datetime.now():%d %b}:\n" + "\n".join(self.line(r) for r in items) +
                "\n\nReply: <code> | DONE / WIP / HOLD / NEED | detail\ne.g. D3 | DONE | CoA sent 20 Nov")

    def chase_for(self, group_name):
        today = datetime.now().strftime("%d %b")
        late = [r for r in self.open_items(group_name) if not r["updated"].startswith(today)]
        if not late:
            return ""
        return ("Reminder: no update yet today on\n" + "\n".join(f"{r['code']} ({r['owner'] or 'owner TBD'}) {r['task'][:60]}" for r in late) +
                "\nPlease reply with the code and status.")

    def summary(self, limit=80):
        today = datetime.now().strftime("%d %b")
        done_today = [r for r in self.rows if r["status"] == "DONE" and r["updated"].startswith(today)]
        lines = [self.line(r) + (f"  ({r['group']})" if r["group"] else "") for r in self.open_items()[:limit]]
        return ("Open coded items:\n" + ("\n".join(lines) or "(none)") +
                ("\nClosed today: " + ", ".join(r["code"] for r in done_today) if done_today else ""))
