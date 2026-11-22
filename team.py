"""The leadership team: how agents route work, talk to each other and report up.

Flow for every message you send:
  1. Chief of Staff reads it and picks which heads should work on it.
  2. Each chosen head answers from their department's view, using shared memory.
     A head can ask another head a question with a line "ASK <id>: question".
  3. Heads with a team (e.g. COO over QA and Supply Chain) combine their team's
     reports into their own.
  4. The CEO reads all reports and gives the final decision with action items.
  5. The Chief of Staff saves the action items so they are followed up.
"""
import json
import re

from tools import TOOL_HELP, run_tool


class Team:
    def __init__(self, team_file, brain, memory, kb=None):
        with open(team_file, encoding="utf-8-sig") as f:
            data = json.load(f)
        self.company = data.get("company", "our company")
        self.owner = data.get("owner", "the owner")
        self.handoffs = data.get("handoffs", [])
        self.kb = kb
        self.staff = None  # set in run.py when contacts.local.json exists
        self.agents = {a["id"]: a for a in data["agents"]}
        self.brain = brain
        self.memory = memory
        self.trace = []  # who said what, for showing the conversation

    # ---------- org chart helpers ----------
    def reports_of(self, agent_id):
        return [a for a in self.agents.values() if a.get("reports_to") == agent_id]

    def org_chart(self, root="ceo", depth=0):
        a = self.agents[root]
        line = "  " * depth + ("- " if depth else "") + a["title"] + (" (reporting line is a guess)" if a.get("guess_reporting") else "")
        lines = [line]
        for child in self.reports_of(root):
            lines += self.org_chart(child["id"], depth + 1).split("\n")
        return "\n".join(lines)

    def _system(self, agent):
        boss = self.agents.get(agent.get("reports_to") or "", {}).get("title", "the board")
        team = ", ".join(r["title"] for r in self.reports_of(agent["id"])) or "no direct reports"
        facts = "\n".join("- " + f for f in self.memory.facts()) or "- (none yet)"
        tasks = "\n".join(f"- #{t[0]} {t[2]} (due {t[3]})" for t in self.memory.open_tasks(agent["id"])) or "- none"
        past = "\n".join(f"- {r[0]} on '{r[1]}': {r[2][:200]}" for r in self.memory.recent(agent["id"], 4)) or "- none"
        others = ", ".join(f"{a['id']}={a['title']}" for a in self.agents.values() if a["id"] != agent["id"])
        flows = "; ".join(f"you send {w} to {t}" if f == agent["id"] else f"{f} sends you {w}"
                          for f, t, w in self.handoffs if agent["id"] in (f, t)) or "none listed"
        kb = self.kb.context_for(agent) if self.kb else ""
        return (
            f"You are the {agent['title']} of {self.company}. You are an AI agent on the leadership team.\n"
            f"Your focus: {agent['focus']}\n"
            f"You report to: {boss}. Your team: {team}.\n"
            f"Work handoffs: {flows}.\n"
            f"Company knowledge base (trust this, it is the shared memory of all company bots):\n{kb}\n\n"
            f"Extra facts the owner told this team:\n{facts}\n"
            f"Your open action items:\n{tasks}\n"
            f"Your recent notes:\n{past}\n"
            f"Other heads you can ask (by id): {others}\n"
            "Rules: be short and practical (max 8 lines). Use Indian context (INR, CDSCO, GST) when relevant. "
            "Never invent numbers; quote figures only from the knowledge base or tool results, and say which file. "
            "If data is missing, say exactly what data you need. "
            f"Only {self.owner}'s explicit approvals count as decided. Bring {self.owner} only decisions on money, approvals, "
            "people, and messages to staff or outside parties. Never claim you messaged anyone. "
            "If you truly need another head's input, add ONE line: ASK <id>: <question>.\n"
            + TOOL_HELP
        )

    def _say(self, agent_id, topic, prompt, max_tokens=700):
        agent = self.agents[agent_id]
        text = self.brain.think(self._system(agent), prompt, max_tokens)
        self.memory.log(agent_id, topic, text)
        self.trace.append((agent["title"], text))
        return text

    # ---------- step 1: routing ----------
    def route(self, message):
        low = message.lower()
        picked = {a["id"] for a in self.agents.values()
                  if a["id"] not in ("ceo", "cos") and any(re.search(r"\b" + re.escape(k) + r"\b", low) for k in a["keywords"])}
        ids = ", ".join(a for a in self.agents if a not in ("ceo", "cos"))
        answer = self.brain.think(
            self._system(self.agents["cos"]),
            f"Message from the owner:\n\"{message}\"\n\nWhich heads must work on this? "
            f"Choose from: {ids}. Reply ONLY with agent ids separated by commas.",
            60,
        )
        picked |= {i for i in re.findall(r"[a-z]+", answer.lower()) if i in self.agents and i not in ("ceo", "cos")}
        if not picked:
            picked = {"coo", "cfo"}
        self.trace.append(("Chief of Staff", "Sending this to: " + ", ".join(self.agents[i]["title"] for i in sorted(picked))))
        return picked

    # ---------- step 2-3: heads work and report up ----------
    def _work(self, agent_id, message, picked, done):
        if agent_id in done:
            return done[agent_id]
        team_notes = ""
        for child in self.reports_of(agent_id):
            if child["id"] in picked:
                team_notes += f"\nReport from your {child['title']}:\n{self._work(child['id'], message, picked, done)}\n"
        prompt = f"The owner asked:\n\"{message}\"\n{team_notes}\nGive your department's view and recommendation."
        text = self._say(agent_id, message, prompt)

        # agents talking to each other: one round of questions
        for target, question in re.findall(r"ASK\s+([a-z]+)\s*:\s*(.+)", text):
            if target in self.agents and target != agent_id:
                reply = self._say(target, message,
                                  f"The {self.agents[agent_id]['title']} asks you: {question}\n(Context: owner asked \"{message}\")")
                text += f"\nAnswer from {self.agents[target]['title']}: {reply}"
        # tools: GitHub / Graphify, one round, then the agent rethinks with the results
        tool_calls = re.findall(r"TOOL\s+([a-z_]+)\s*:\s*(.+)", text)
        if tool_calls:
            results = "\n".join(f"[{n}: {a.strip()}]\n{run_tool(n, a, self.kb)}" for n, a in tool_calls[:3])
            self.trace.append(("Tools", results[:1500]))
            text = self._say(agent_id, message,
                             f"{prompt}\n\nYour earlier draft:\n{text}\n\nTool results:\n{results}\n\n"
                             "Now give your final view using these results. Do not call tools again.")
        done[agent_id] = text
        return text

    def handle(self, message):
        """Main entry: a message from the owner -> team discussion -> CEO decision."""
        self.trace = []
        picked = self.route(message)
        # a head's boss should also join if the head was picked
        for i in list(picked):
            boss = self.agents[i].get("reports_to")
            if boss and boss not in ("ceo", "cos"):
                picked.add(boss)
        done = {}
        top_level = [i for i in picked if self.agents[i].get("reports_to") in ("ceo", None)]
        reports = ""
        for i in sorted(top_level):
            reports += f"\n--- {self.agents[i]['title']} ---\n{self._work(i, message, picked, done)}\n"

        decision = self._say(
            "ceo", message,
            f"The owner asked:\n\"{message}\"\n\nReports from your heads:{reports}\n"
            "Give your final decision and plan in under 10 lines. Then list action items, each on its own line as:\n"
            "ACTION: <agent id> | <task> | <due>",
            900,
        )
        self._save_actions(decision)
        return self._clean(decision)

    # ---------- proactive daily briefing ----------
    def daily_briefing(self):
        self.trace = []
        updates = ""
        for a in self.agents.values():
            if a["id"] == "ceo":
                continue
            if not self.memory.open_tasks(a["id"]) and a["id"] != "cos":
                continue
            updates += f"\n--- {a['title']} ---\n" + self._say(
                a["id"], "daily briefing",
                "Write your morning update for the CEO: status of your open action items, "
                "risks you see today, and anything you need. Max 5 lines.") + "\n"
        if not updates:
            updates = "\n(No open action items. Quiet day.)"
        summary = self._say(
            "ceo", "daily briefing",
            f"Morning updates from your heads:{updates}\nWrite a short morning briefing for the owner "
            "(max 8 lines): what is on track, what is at risk, top 3 priorities today.",
        )
        return "Good morning. Today's leadership briefing:\n\n" + self._clean(summary)

    # ---------- helpers ----------
    def _save_actions(self, text):
        for owner, task, due in re.findall(r"ACTION:\s*([a-z]+)\s*\|\s*([^|\n]+)\|\s*([^\n]+)", text):
            if owner in self.agents:
                tid = self.memory.add_task(owner, task.strip(), due.strip())
                if self.staff:
                    self.staff.draft_for_task(tid, owner, task.strip(), due.strip())

    def _clean(self, text):
        lines, actions = [], []
        for line in text.splitlines():
            m = re.match(r"ACTION:\s*([a-z]+)\s*\|\s*([^|]+)\|\s*(.+)", line.strip())
            if m and m.group(1) in self.agents:
                actions.append(f"- {self.agents[m.group(1)]['title']}: {m.group(2).strip()} (due {m.group(3).strip()})")
            else:
                lines.append(line)
        out = "\n".join(lines).strip()
        if actions:
            out += "\n\nAction items:\n" + "\n".join(actions)
        return out
