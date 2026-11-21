# AI Leadership Team for Lucent Biotech (open source, runs free on your PC)

A team of AI agents that works like your leadership team. You send a message on WhatsApp (through **WhatsMCP**, which you already use) or type it in a window on your PC. The **Chief of Staff** sends it to the right heads. Each head reads its own department brief and data from your **lucent-kb** knowledge base, can look things up in the **Graphify** map and on **GitHub**, and can ask other heads questions. Managers combine their team's reports, and the **CEO** replies with a plan and action items. Every morning the team sends you a briefing and follows up on open tasks.

```
CEO
  - Chief of Staff
  - CFO
  - COO (Plant Head, Roorkee Unit-II)
    - QA/QC Head            (reporting line is my guess)
    - Engineering Head      (my guess)
    - Supply Chain & Purchase Head (my guess)
    - F&D Head              (my guess)
  - DRA Head (Regulatory Affairs)
  - Business Development Head
  - Domestic Sales Head (Trade Generics)
  - HR Head
```

These are the same 12 departments as `lucent-kb/departments/`. The team follows the rules already written in your knowledge base: only your explicit approvals count as decided, and the agents bring you only decisions on money, approvals, people, and messages to staff or outside parties. They reply only to the numbers you allow and never message staff or customers on their own.

**Cost:** nothing per message with the default "brain", Ollama, a free program that runs AI models on your own PC. You can switch to Claude for sharper answers, which costs per use.

---

## Part 1: Get it running on Windows

**Easiest way (one click):** download [SETUP.bat](https://github.com/anirag1206/ops-agent-team/raw/main/SETUP.bat) and [setup_windows.ps1](https://github.com/anirag1206/ops-agent-team/raw/main/setup_windows.ps1) into the same folder (for example Downloads), then double-click **SETUP.bat**. It installs Python, Git and Ollama, downloads the free AI model (it picks a smaller one if your PC has under 16 GB memory), downloads the team and your knowledge base, and puts a **Leadership Team** icon on your desktop. Click Yes when Windows asks, and sign in to GitHub when asked. It takes 15 to 40 minutes, mostly the model download. You can run it again any time to update.

**Or do it by hand** (about 20 minutes):


1. **Install Python** from https://www.python.org/downloads/ . On the first screen tick **"Add python.exe to PATH"**.
2. **Install Git** from https://git-scm.com/download/win (default options).
3. **Install Ollama** from https://ollama.com/download and open it once.
4. Open **Command Prompt** and run, one line at a time:
   ```
   ollama pull qwen2.5:7b
   cd C:\
   git clone https://github.com/anirag1206/lucent-kb
   pip install graphifyy
   ```
   The model download is about 4.7 GB. On a PC with less than 16 GB RAM, use `qwen2.5:3b` instead and change `ollama_model` in `config.json` to match. Git will ask you to sign in to GitHub, because lucent-kb is private.
5. Unzip this `ops-agent-team` folder to `C:\ops-agent-team`, so it sits next to `C:\lucent-kb`. That's where `config.json` expects to find the knowledge base (`"knowledge_base": "../lucent-kb"`).
6. Double click **`start_chat.bat`** and type a question, for example:
   *"A customer sent an RFQ for 20 lakh tablets. Can we take it, and do we have the RM?"*

To watch the agents talk to each other, run `python run.py chat --show`.

| Type (in the chat window or on WhatsApp) | What happens |
|---|---|
| any question | the team discusses it and the CEO replies with a plan |
| `team` | shows the org chart |
| `tasks` | open action items |
| `done 3` | marks action item 3 as done |
| `remember: <fact>` | every agent will know this from now on |
| `briefing` | the morning briefing now |

To get the newest snapshots after your other bots refresh lucent-kb, run `python run.py update-kb`.

## Part 2: Connect WhatsApp through WhatsMCP

1. In your WhatsMCP dashboard create an **API key** (it starts with `wamcp_live_`). Save it as a Windows environment variable: Start > "Edit environment variables for your account" > New, with the name `WAMCP_KEY`.
2. Run `python run.py whatsmcp-accounts` and copy the `account_id` of your number into `config.json` under `whatsmcp`.
3. Put your own mobile number in `config.json` under `whatsapp` > `allowed_numbers`, for example `["919812345678"]`. **Only these numbers can command the team.**
4. Double click **`start_whatsapp.bat`** and send "hi" from your phone.

**Which WhatsMCP plan:**
- **Free plan** works and needs no extra setup, but it allows only 100 API calls and 30 messages a day. The team checks for new messages every 15 minutes (`poll_seconds: 900`), so replies are slow.
- **Pro ($25 a month)** has unlimited API calls. Set `poll_seconds` to `20` for near-instant replies.
- **Starter and above** can also push messages instantly (`"receive": "webhook"`). This needs a tunnel such as `cloudflared tunnel --url http://localhost:8000`, and you register that address with WhatsMCP.

**Ban risk:** a WhatsMCP **Personal** number is linked like WhatsApp Web, and WhatsMCP itself warns this is outside WhatsApp's terms, so WhatsApp may restrict that number. Use a separate number, or a WhatsMCP **Business** number (official Meta API), for the team. To use Meta's API directly instead, set `"whatsapp_provider": "meta"` (setup notes are in `whatsapp_bridge.py`).

The PC must stay on for the team to answer. The morning briefing is sent at `daily_briefing_time` (default 09:00).

## Part 2b: Let the team message plant staff

1. Quickest: `python run.py whatsmcp-groups` lists your WhatsApp groups, and `python run.py staff-from-group "Plant" coo` adds everyone who has written in that group to `contacts.local.json` (WhatsMCP shares no member list, so silent members are missed). Or copy `contacts.example.json` to `contacts.local.json` and list your people by hand: name, `role` (the agent id from `team.json`, e.g. `eng`, `qa`, `coo`) and WhatsApp number (`91` + number). This file stays on your PC and never goes to GitHub.
2. When the CEO agent assigns an action item to a department that has a person listed, the team **drafts** a WhatsApp message to that person and shows it to you. **Nothing is sent until you reply** `send all`, `send 3` or `cancel 3`. Type `outbox` to read drafts in full.
3. You can also write to someone through the team: `message Mehboob: please share today's press 4 status`. This is drafted the same way.
4. Staff reply with `done 3`, `update 3: <news>` or `tasks`. Updates are saved to that department's memory and forwarded to you. Every other message from staff is ignored, so normal chats never get an automatic answer.

To send task messages without approving each one, put `"staff_messages": "auto"` in `config.local.json`.

**Use a separate WhatsApp number for the team.** If WhatsMCP is linked to your personal number, staff see the messages coming from you personally, and sending to many people from a personal number raises the chance that WhatsApp restricts it. A second SIM linked in WhatsMCP as "Lucent Leadership Team" avoids both, and you then command the team from your own phone like any other chat.

## Part 2c: Only certain people, and replies in a group

- The team reads **only** the chats of numbers in `allowed_numbers` (plus listed staff). It never downloads your other conversations.
- To let it work in a group, add part of the group name to `allowed_groups`, e.g. `["Production"]`. It then reads only that group, and answers only when an allowed person starts a message with **team**, e.g. *"team, what is pending for packing today?"*. The answer goes to the group. Everything else in the group is ignored.
- `python run.py whatsmcp-groups` shows the exact group names. (Listing groups reads only their names, not their messages.)

## Part 3: Knowledge base, Graphify and GitHub

- **lucent-kb**: every agent automatically reads the company snapshot, `decisions.md`, its own `departments/*.md` brief and its `data/*.md` snapshots. The CEO, Chief of Staff and HR also read `people.md`. Which files each agent reads is listed in `team.json` (`kb_brief`, `kb_data`).
- **Graphify**: when an agent writes `TOOL graphify: <question>`, the team runs the open source `graphify query` on your `graphify-out` map (no key, free) and adds a text search across the knowledge base. Agents can also open any knowledge base file with `TOOL kb_read: data/order_book_open.csv`.
- **GitHub** (optional): create a read-only fine-grained token at https://github.com/settings/tokens and save it as the environment variable `GITHUB_TOKEN`. Agents can then read issues, recent changes and files in your repositories, such as `lucent-ebmr` or `lucent-qa`.

## Part 4: Switch to Claude (optional, paid, smarter)

Set the environment variable `ANTHROPIC_API_KEY`, then change `"brain": "ollama"` to `"brain": "claude"` in `config.json`. Each agent reads about 3,000 to 4,000 tokens of knowledge base, and one discussion makes 4 to 10 AI calls, so it costs roughly a few rupees per question.

## Customise

- **Your own settings** (which brain, model, WhatsMCP account, allowed numbers) go in `config.local.json` next to `config.json`. Only write the lines you change, for example `{"brain": "claude", "whatsapp": {"allowed_numbers": ["919812345678"]}}`. Updates never overwrite this file.

- **Change a role or reporting line:** edit `team.json`. Each block has an `id`, a `title`, who it `reports_to`, its `focus`, the knowledge base files it reads, and `keywords` the Chief of Staff uses for routing. `handoffs` lists who sends what to whom (copied from `kb_model.py`).
- **Memory:** the team's own memory (facts you teach it, action items, discussion log) is in `team_memory.db`. Delete it to start fresh. It never writes to lucent-kb.

## Honest limits of this first version

- Free local models are slower and less sharp than Claude. On a normal office PC, expect 1 to 4 minutes per discussion, because every agent reads its slice of the knowledge base. A PC with an NVIDIA graphics card (8 GB or more) is much faster.
- The agents only know what lucent-kb, GitHub and you tell them. Snapshots are only as fresh as your last `refresh.py` run.
- Treat their answers as advice. Regulatory, QA release and financial decisions still need a person to sign off.
- **Tested here:** the whole flow in test mode, with your real lucent-kb (routing, reporting up, agents asking each other, Graphify queries, memory, tasks, and the WhatsApp webhook). **Not yet tested:** a real Ollama model, a live WhatsMCP number, and GitHub with a real token. Two details of WhatsMCP are my best reading of their docs and are marked GUESS in `whatsmcp_bridge.py`: how incoming messages are labelled, and the webhook signature format.

## Files

| File | What it is |
|---|---|
| `team.json` | roles, org chart, knowledge base files per role, handoffs |
| `config.json` | settings: brain, knowledge base folder, WhatsMCP, briefing time |
| `team.py` | how agents route, discuss and report up |
| `kb.py` | reads lucent-kb and runs Graphify queries |
| `brain.py` | connects to Ollama or Claude |
| `memory.py` | the team's own memory, facts and action items |
| `tools.py` | Graphify, knowledge base and GitHub lookups |
| `whatsmcp_bridge.py` | WhatsApp through WhatsMCP (default) |
| `staff.py` | drafts, approvals and replies for messages to plant staff |
| `contacts.example.json` | template for your private staff list |
| `whatsapp_bridge.py` | commands, morning briefing, and Meta's direct API (alternative) |
| `run.py` | starts everything |

This code contains no company data. Keep lucent-kb private, as its README says.
