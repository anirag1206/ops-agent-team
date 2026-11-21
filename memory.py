"""Shared memory for the whole team, kept in one file: team_memory.db (SQLite).

Three things are remembered:
  facts  -> things you tell the team to remember ("remember: our MOQ is 50,000 tabs")
  log    -> everything each agent said, so they recall past discussions
  tasks  -> action items the CEO assigned, with owner, due date and status
"""
import sqlite3
from datetime import datetime


class Memory:
    def __init__(self, path="team_memory.db"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS facts (id INTEGER PRIMARY KEY, at TEXT, text TEXT);
            CREATE TABLE IF NOT EXISTS log   (id INTEGER PRIMARY KEY, at TEXT, agent TEXT, topic TEXT, text TEXT);
            CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, at TEXT, owner TEXT, task TEXT,
                                              due TEXT, status TEXT DEFAULT 'open');
            """
        )

    @staticmethod
    def _now():
        return datetime.now().strftime("%Y-%m-%d %H:%M")

    # facts
    def add_fact(self, text):
        self.db.execute("INSERT INTO facts(at, text) VALUES (?, ?)", (self._now(), text))
        self.db.commit()

    def facts(self, limit=30):
        rows = self.db.execute("SELECT text FROM facts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [r[0] for r in rows]

    # log
    def log(self, agent, topic, text):
        self.db.execute(
            "INSERT INTO log(at, agent, topic, text) VALUES (?, ?, ?, ?)",
            (self._now(), agent, topic[:200], text),
        )
        self.db.commit()

    def recent(self, agent=None, limit=6):
        if agent:
            rows = self.db.execute(
                "SELECT at, topic, text FROM log WHERE agent=? ORDER BY id DESC LIMIT ?", (agent, limit)
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT at, topic, text FROM log WHERE agent='ceo' ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return list(reversed(rows))

    # tasks
    def add_task(self, owner, task, due):
        self.db.execute(
            "INSERT INTO tasks(at, owner, task, due) VALUES (?, ?, ?, ?)", (self._now(), owner, task, due)
        )
        self.db.commit()

    def open_tasks(self, owner=None):
        q = "SELECT id, owner, task, due, at FROM tasks WHERE status='open'"
        args = ()
        if owner:
            q += " AND owner=?"
            args = (owner,)
        return self.db.execute(q + " ORDER BY id", args).fetchall()

    def close_task(self, task_id):
        cur = self.db.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
        self.db.commit()
        return cur.rowcount > 0
