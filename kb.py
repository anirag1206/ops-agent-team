"""Reads your Lucent knowledge base (the lucent-kb repository) so every agent
starts from the same facts as your existing bots: department briefs, decisions,
people, daily data snapshots and the Graphify map.

Set "knowledge_base" in config.json to the folder where lucent-kb is cloned.
Nothing here writes to the knowledge base.
"""
import os
import re
import shutil
import subprocess


class KnowledgeBase:
    def __init__(self, path):
        self.root = os.path.abspath(path) if path else ""
        self.ok = bool(self.root) and os.path.exists(os.path.join(self.root, "README.md"))

    def read(self, rel, limit=4000):
        """Read one file inside the knowledge base (never outside it)."""
        if not self.ok:
            return ""
        full = os.path.abspath(os.path.join(self.root, rel))
        if not full.startswith(self.root + os.sep) or not os.path.isfile(full):
            return ""
        with open(full, encoding="utf-8", errors="replace") as f:
            text = f.read()
        return text if len(text) <= limit else text[:limit] + "\n...(cut)"

    def company_snapshot(self):
        readme = self.read("README.md", 20000)
        m = re.search(r"## Company snapshot.*", readme, re.S)
        return m.group(0)[:1500] if m else ""

    def context_for(self, agent):
        """The reading pack for one agent: its brief, its data, decisions, people."""
        if not self.ok:
            return ""
        parts = [self.company_snapshot(), self.read("decisions.md", 6000)]
        if agent.get("kb_brief"):
            parts.append(self.read(agent["kb_brief"]))
        for rel in agent.get("kb_data", []):
            parts.append(f"[{rel}]\n" + self.read(rel, 2500))
        if agent["id"] in ("ceo", "cos", "hr"):
            parts.append(self.read("people.md", 5000))
        return "\n\n".join(p for p in parts if p)

    def search(self, question, max_lines=25):
        """Graphify query if the graphify tool is installed, else a plain text search."""
        if not self.ok:
            return "Knowledge base folder not found. Set knowledge_base in config.json."
        graph = ""
        exe = shutil.which("graphify")
        if exe:  # the open source Graphify map: which files and topics connect to the question
            try:
                r = subprocess.run([exe, "query", question, "--budget", "800"], cwd=self.root,
                                   capture_output=True, text=True, timeout=120)
                if r.returncode == 0:
                    graph = "Graphify map:\n" + r.stdout[:2000] + "\n\n"
            except Exception:
                pass
        words = [w for w in re.findall(r"[a-z0-9]{3,}", question.lower())]
        hits = []
        for folder, _, files in os.walk(self.root):
            if ".git" in folder or "source" in folder.split(os.sep):
                continue
            for name in files:
                if not name.endswith((".md", ".csv")):
                    continue
                path = os.path.join(folder, name)
                rel = os.path.relpath(path, self.root)
                with open(path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        score = sum(w in line.lower() for w in words)
                        if score:
                            hits.append((score, f"{rel}: {line.strip()[:220]}"))
        hits.sort(key=lambda h: -h[0])
        lines = "\n".join(h[1] for h in hits[:max_lines])
        return (graph + "Matching lines:\n" + lines) if (graph or lines) else "Nothing found in the knowledge base."

    def update(self):
        """git pull the latest snapshots from GitHub."""
        if not self.ok:
            return "Knowledge base folder not found."
        r = subprocess.run(["git", "pull", "--ff-only"], cwd=self.root, capture_output=True, text=True)
        return (r.stdout + r.stderr).strip() or "Up to date."
