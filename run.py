"""Start your AI leadership team.

  python run.py chat        -> talk to the team in this window
  python run.py ask "..."   -> one question, then exit
  python run.py briefing    -> morning briefing now
  python run.py whatsapp    -> start the WhatsApp connection + daily briefing
  python run.py middleman   -> cheap filter: watch WhatsApp, call the CEO AI (Grok) only when needed
  python run.py email-test  -> check the mailbox login and show the newest emails' subjects
  python run.py team        -> show the org chart
  python run.py update-kb   -> pull the latest knowledge base from GitHub
  python run.py whatsmcp-accounts -> list your WhatsMCP numbers and their account_id
  python run.py whatsmcp-plan     -> show your WhatsMCP plan and limits
  python run.py whatsmcp-groups   -> list your WhatsApp groups
  python run.py staff-from-group "Group name" [role] -> add everyone who wrote in that group to contacts.local.json

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
    with open(os.path.join(HERE, "config.json"), encoding="utf-8-sig") as f:
        config = json.load(f)
    # Your own settings go in config.local.json (never overwritten by updates)
    local = os.path.join(HERE, "config.local.json")
    if os.path.exists(local):
        with open(local, encoding="utf-8-sig") as f:
            mine = json.load(f)
        for k, v in mine.items():
            if isinstance(v, dict) and isinstance(config.get(k), dict):
                config[k].update(v)
            else:
                config[k] = v
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

    contacts = os.path.join(HERE, "contacts.local.json")
    if os.path.exists(contacts):
        from staff import Staff
        team.staff = Staff(contacts, team.memory, team.agents,
                           config["whatsapp"].get("allowed_numbers") or [], config.get("staff_messages", "approve"))
        if config.get("whatsapp_provider", "whatsmcp") == "whatsmcp" and os.environ.get("WAMCP_KEY"):
            import whatsmcp_bridge
            team.staff.send_fn = lambda phone, text: whatsmcp_bridge.send(config, phone, text)

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
    elif mode == "middleman":
        from middleman import Middleman
        Middleman(config, team).run()
    elif mode == "email-test":
        from email_reader import EmailReader
        r = EmailReader(config)
        box = r._login()
        ok, data = box.uid("search", None, "ALL")
        uids = data[0].split()
        print(f"Logged in to {config['email'].get('user')}: {len(uids)} emails in {config['email'].get('folder', 'INBOX')}.")
        for u in uids[-5:]:
            ok, parts = box.uid("fetch", u, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
            print(" ", parts[0][1].decode("utf-8", "replace").strip().replace("\r\n", " | "))
        box.logout()
        print("Email works. Set \"enabled\": true in the email block of config.local.json and start the middleman.")
    elif mode == "whatsmcp-plan":
        import whatsmcp_bridge
        plan = whatsmcp_bridge._call("GET", f"/accounts/{config['whatsmcp']['account_id']}/plan")
        for k in ("display_name", "plan", "messages_24h", "messages_1h", "webhooks_enabled", "history_days"):
            print(f"{k}: {plan.get(k)}")
    elif mode == "whatsmcp-groups":
        import whatsmcp_bridge
        for g in whatsmcp_bridge.list_groups(config["whatsmcp"]["account_id"]):
            print(g.get("name") or g.get("chat"))
    elif mode == "staff-from-group":
        # python run.py staff-from-group "Group name" [role]
        import whatsmcp_bridge
        own = whatsmcp_bridge._own_phone(config["whatsmcp"]["account_id"])
        gname, added = whatsmcp_bridge.add_group_to_contacts(
            os.path.join(HERE, "contacts.local.json"), config["whatsmcp"]["account_id"],
            args[1], args[2] if len(args) > 2 else "", own)
        print(f"Group '{gname}': added {len(added)} people to contacts.local.json")
        print("\n".join(added) or "(nobody new; only people who have written in the group can be found)")
        print("Open contacts.local.json in Notepad, check names, and fill in each person's role.")
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
                try:
                    out(handle_command(team, text))
                except Exception as e:
                    print(f"Sorry, that failed: {e}")
                print()


if __name__ == "__main__":
    main()
