"""Start your AI leadership team.

  python run.py chat        -> talk to the team in this window
  python run.py ask "..."   -> one question, then exit
  python run.py briefing    -> morning briefing now
  python run.py whatsapp    -> start the WhatsApp connection + daily briefing
  python run.py team        -> show the org chart
  python run.py update-kb   -> pull the latest knowledge base from GitHub
  python run.py whatsmcp-accounts -> list your WhatsMCP numbers and their account_id

Add --show to see the full discussion between agents (who said what).
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from brain import Brain  # noqa: E402
from kb import KnowledgeBase  # noqa: E402
from memory import Memory  # noqa: E402
from team import Team  # noqa: E402
from whatsapp_bridge import handle_command, serve  # noqa: E402


def main():
    args = [a for a in sys.argv[1:] if a != "--show"]
    show = "--show" in sys.argv
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        config = json.load(f)
    if os.environ.get("BRAIN"):
        config["brain"] = os.environ["BRAIN"]
    kb_path = config.get("knowledge_base", "")
    if kb_path and not os.path.isabs(kb_path):
        kb_path = os.path.join(HERE, kb_path)
    kb = KnowledgeBase(kb_path)
    if not kb.ok:
        print(f"Note: knowledge base not found at {kb_path}. Agents will work without company data.")
    team = Team(os.path.join(HERE, "team.json"), Brain(config),
                Memory(os.path.join(HERE, "team_memory.db")), kb)

    mode = args[0] if args else "chat"

    def out(text):
        if show:
            print("\n========== Team discussion ==========")
            for who, said in team.trace:
                print(f"\n[{who}]\n{said}")
            print("\n========== Final answer ==========")
        print(text)

    if mode == "team":
        print(team.org_chart())
    elif mode == "ask":
        out(handle_command(team, " ".join(args[1:])))
    elif mode == "briefing":
        out(team.daily_briefing())
    elif mode == "whatsapp":
        if config.get("whatsapp_provider", "whatsmcp") == "whatsmcp":
            import whatsmcp_bridge
            whatsmcp_bridge.serve(team, config)
        else:
            serve(team, config)
    elif mode == "update-kb":
        print(kb.update())
    elif mode == "whatsmcp-accounts":
        import whatsmcp_bridge
        print(json.dumps(whatsmcp_bridge._call("GET", "/accounts"), indent=2))
    else:
        print(f"AI leadership team ({config['brain']} brain). Type 'help' for commands, 'exit' to quit.\n")
        while True:
            try:
                text = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if text.lower() in ("exit", "quit"):
                break
            if text:
                print("\n(The team is discussing, this can take a minute on a normal PC...)\n")
                out(handle_command(team, text))
                print()


if __name__ == "__main__":
    main()
