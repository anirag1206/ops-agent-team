"""WhatsApp through WhatsMCP (whatsmcp.com), the service you already use.

Two ways to receive messages, set "receive" in config.json -> whatsmcp:
  "poll"    -> this program asks WhatsMCP for new messages every few minutes.
               Works on the FREE plan and needs no tunnel. Free plan allows only
               100 API calls a day, so the default check is every 15 minutes.
               On the Pro plan (unlimited calls) set poll_seconds to 20.
  "webhook" -> WhatsMCP pushes each message instantly to this PC (paid plans,
               Starter and up). Needs a tunnel, same as in README Part 2.

The API key goes in the Windows environment variable WAMCP_KEY (never in a file).
Docs used: https://whatsmcp.com/docs/rest  (read 21 Nov 2026)
"""
import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

from whatsapp_bridge import _briefing_clock, handle_command

API = "https://api.whatsmcp.com/v1"
MARK = "\U0001F916 "  # robot emoji at the start of every team reply, so the team never answers itself


def _call(method, path, body=None):
    key = os.environ.get("WAMCP_KEY")
    if not key:
        raise RuntimeError("Set the WAMCP_KEY environment variable to your WhatsMCP API key.")
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-API-Key": key, "Content-Type": "application/json", "User-Agent": "ops-agent-team"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode() or "{}")


def send(config, to, text):
    acc = config["whatsmcp"].get("account_id")
    if not acc or not os.environ.get("WAMCP_KEY"):
        print(f"\n[WhatsMCP not set up yet, would send to {to}]:\n{text}\n")
        return
    for i in range(0, len(text), 3900):
        _call("POST", f"/accounts/{acc}/messages", {"to": to, "text": MARK + text[i:i + 3900]})


def serve(team, config):
    wm = config["whatsmcp"]
    allowed = {n.lstrip("+") for n in (config["whatsapp"].get("allowed_numbers") or [])}
    lock = threading.Lock()

    if team.staff:
        team.staff.send_fn = lambda phone, text: send(config, phone, text)

    def work(sender, text, reply_to=None):
        sender = sender.split("@")[0].lstrip("+")
        dest = reply_to or sender  # in a group, answer in the group
        if team.staff and sender not in allowed:
            to_staff, to_owner = team.staff.staff_message(sender, text)
            if to_staff:
                print(f"Staff message from {sender}: {text[:60]}")
                send(config, dest, to_staff)
                for owner in allowed:
                    if to_owner:
                        send(config, owner, to_owner)
            return
        if allowed and sender not in allowed:
            return  # only answer the owner's numbers, never strangers
        print(f"Command from {sender}{' in group' if reply_to else ''}: {text[:60]}")
        with lock:
            try:
                reply = handle_command(team, text)
            except Exception as e:
                reply = f"Sorry, something went wrong: {e}"
        send(config, dest, reply)

    def briefing_sender(_cfg, number, text):
        send(config, number, text)

    threading.Thread(target=_briefing_clock, args=(team, config, lock, briefing_sender), daemon=True).start()

    own = _own_phone(wm.get("account_id"))
    if own:
        print(f"Linked WhatsApp number: {own}.")
    if wm.get("receive", "poll") == "webhook":
        _webhook(wm, work, int(config["whatsapp"].get("port", 8000)))
    else:
        chats = list(allowed) + ([p["phone"] for p in team.staff.people] if team.staff else [])
        groups = set()
        wanted = config["whatsapp"].get("allowed_groups") or []
        if wanted:
            for g in list_groups(wm.get("account_id")):
                if any(group_matches(w, g) for w in wanted):
                    groups.add(g["chat"])
                    print(f"Group: {g.get('name')} (team answers messages starting with "
                          f"'{config['whatsapp'].get('group_trigger', 'team')}')")
            if not groups:
                print("None of allowed_groups matched a WhatsApp group name. Run: python run.py whatsmcp-groups")
        _poll(wm, work, own, chats + sorted(groups), groups, config["whatsapp"].get("group_trigger", "team"))


def _own_phone(account_id):
    try:
        for a in _call("GET", "/accounts").get("accounts", []):
            if a.get("account_id") == account_id:
                return str(a.get("phone", "")).lstrip("+")
    except Exception as e:
        print("Could not read the linked number:", e)
    return ""


def _number(value):
    return "".join(ch for ch in str(value or "").split("@")[0] if ch.isdigit())


def _pick(m, own, groups=(), trigger="team"):
    """Return (sender, reply_to, text) for a message the team should handle, else None."""
    text = (m.get("text") or "").strip()
    if not text or text.startswith(MARK.strip()):
        return None  # empty, or one of the team's own replies
    chat_raw = str(m.get("chat", ""))
    peer = _number(m.get("peer"))
    if "@g.us" in chat_raw or "@newsletter" in chat_raw:
        if chat_raw not in groups:
            return None  # never act on other groups or channels
        # in an allowed group, only messages that start with the trigger word, e.g. "team, what is pending?"
        mt = re.match(r"@?" + re.escape(trigger) + r"\b[\s,:.-]*(.*)", text, re.I | re.S)
        if not mt or not mt.group(1).strip():
            return None
        return (own if (not peer and own) else peer), chat_raw, mt.group(1).strip()
    chat = _number(chat_raw)
    if own and own in (peer, chat):
        return own, None, text  # "message yourself" chat
    if str(m.get("direction", "")).lower() in ("in", "inbound", "incoming"):
        return peer, None, text
    return None


# ---------------- free plan: polling ----------------
def _poll(wm, work, own="", chats=None, groups=(), trigger="team"):
    """Asks WhatsMCP ONLY for the chats of the listed numbers (allowed numbers + staff),
    so other conversations are never downloaded."""
    here = os.path.dirname(os.path.abspath(__file__))
    cursor_file = os.path.join(here, "whatsmcp_cursors.json")
    every = int(wm.get("poll_seconds", 900))
    chats = sorted({c for c in (chats or []) if c})
    if not chats:
        print("No allowed numbers set in config, so nothing will be read. Add allowed_numbers and restart.")
        return
    cursors = {}
    if os.path.exists(cursor_file):
        with open(cursor_file) as f:
            cursors = json.load(f)
    for chat in chats:
        if chat not in cursors:  # first time: skip old messages in this chat, only answer new ones
            c = 0
            while True:
                page = _call("GET", f"/messages?chat={quote(chat, safe='')}&after_cursor={c}&limit=200")
                c = page.get("next_cursor") or c
                if not page.get("has_more"):
                    break
            cursors[chat] = c
    with open(cursor_file, "w") as f:
        json.dump(cursors, f)
    seen = set()
    calls_per_day = len(chats) * 86400 // max(every, 1)
    print(f"Reading only these chats: {', '.join(chats)}")
    print(f"Checking WhatsMCP for new messages every {every} seconds (about {calls_per_day} checks a day; "
          "the free plan allows 100) - Ctrl+C to stop")
    while True:
        for chat in chats:
            try:
                while True:
                    page = _call("GET", f"/messages?chat={quote(chat, safe='')}&after_cursor={cursors[chat]}&limit=50")
                    for m in page.get("messages", []):
                        if os.environ.get("DEBUG_WAMCP"):
                            print("seen:", {k: m.get(k) for k in ("direction", "peer", "chat", "kind")},
                                  (m.get("text") or "")[:40])
                        if m.get("message_id") in seen:
                            continue
                        seen.add(m.get("message_id"))
                        hit = _pick(m, own, groups, trigger)
                        if hit:
                            sender, reply_to, text = hit
                            threading.Thread(target=work, args=(sender, text, reply_to), daemon=True).start()
                    cursors[chat] = page.get("next_cursor") or cursors[chat]
                    if not page.get("has_more"):
                        break
                with open(cursor_file, "w") as f:
                    json.dump(cursors, f)
            except Exception as e:
                print(f"WhatsMCP check for {chat} failed:", e)
        time.sleep(every)


# ---------------- paid plans: webhook ----------------
def _webhook(wm, work, port):
    secret = os.environ.get("WAMCP_WEBHOOK_SECRET", "")

    class Hook(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if secret and not _signature_ok(raw, self.headers.get("X-WAMCP-Signature", ""), secret):
                self.send_response(401)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            data = json.loads(raw or b"{}")
            if data.get("event") == "message.inbound" and data.get("body") and not data["body"].startswith(MARK.strip()):
                threading.Thread(target=work, args=(str(data.get("peer", "")), data["body"]), daemon=True).start()

        def log_message(self, *a):
            pass

    print(f"WhatsMCP webhook listening on http://localhost:{port}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("0.0.0.0", port), Hook).serve_forever()


def _signature_ok(raw, header, secret):
    # Header looks like "t=<unix>,v1=<hex>". GUESS: signed text is "<t>.<body>" (common pattern).
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        expected = hmac.new(secret.encode(), f"{parts['t']}.".encode() + raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, parts["v1"])
    except Exception:
        return False


# ---------------- building the staff list from a WhatsApp group ----------------
MCP_URL = "https://api.whatsmcp.com/mcp"
HERE = os.path.dirname(os.path.abspath(__file__))
NAMES_FILE = os.path.join(HERE, "group_names.local.json")  # your own labels: {"1203...@g.us": "QA Team"}


def _mcp(method, params, session=None, rid=1):
    """One call to WhatsMCP's MCP endpoint (the REST API has no group names)."""
    key = os.environ.get("WAMCP_KEY")
    body = {"jsonrpc": "2.0", "method": method, "params": params}
    if rid is not None:
        body["id"] = rid
    headers = {"X-API-Key": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream", "User-Agent": "ops-agent-team"}
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(MCP_URL, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        session = r.headers.get("Mcp-Session-Id") or session
        raw = r.read().decode("utf-8", "replace")
    if raw.lstrip().startswith("{"):
        return json.loads(raw), session
    for line in raw.splitlines():  # event-stream answer
        if line.startswith("data:") and '"id"' in line:
            return json.loads(line[5:]), session
    return {}, session


def _find_groups(obj, out):
    """Pick (jid, name) pairs out of whatever shape the answer has."""
    if isinstance(obj, str) and obj.strip()[:1] in "[{":
        try:
            obj = json.loads(obj)
        except ValueError:
            return
    if isinstance(obj, dict):
        jid = next((str(v) for v in obj.values() if isinstance(v, str) and v.endswith("@g.us")), None)
        name = next((obj[k] for k in ("name", "subject", "title", "group_name", "display_name")
                     if isinstance(obj.get(k), str) and obj.get(k)), None)
        if jid and name:
            out[jid] = name
        for v in obj.values():
            _find_groups(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _find_groups(v, out)


_MCP = {}


def _mcp_session():
    if "session" not in _MCP:
        init, session = _mcp("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                            "clientInfo": {"name": "ops-agent-team", "version": "1"}})
        try:
            _mcp("notifications/initialized", {}, session, rid=None)
        except Exception:
            pass
        _MCP["session"] = session
    return _MCP["session"]


def mcp_tool(name, arguments):
    """Call one WhatsMCP MCP tool and return its 'result'."""
    res, _ = _mcp("tools/call", {"name": name, "arguments": arguments}, _mcp_session(), 2)
    if res.get("error"):
        raise RuntimeError(res["error"].get("message") or res["error"])
    return res.get("result", {})


def _tool_args(name):
    """The argument names a WhatsMCP tool expects (from tools/list)."""
    if "tools" not in _MCP:
        res, _ = _mcp("tools/list", {}, _mcp_session(), 3)
        _MCP["tools"] = {t["name"]: t.get("inputSchema", {}).get("properties", {}) for t in res.get("result", {}).get("tools", [])}
    return list(_MCP["tools"].get(name, {}))


def group_members(account_id, jid):
    """{phone: name} of EVERY member of a group (MCP wa_list_group_members). Names come from your
    WhatsApp contacts where WhatsMCP knows them. Members hidden behind WhatsApp privacy ids are skipped."""
    args = {"account_id": account_id}
    key = next((a for a in _tool_args("wa_list_group_members") if a != "account_id"), "chat")
    args[key] = jid
    result = mcp_tool("wa_list_group_members", args)
    if os.environ.get("DEBUG_WAMCP"):
        print("members answer:", json.dumps(result)[:800])
    found = {}

    def walk(o):
        if isinstance(o, str) and o.strip()[:1] in "[{":
            try:
                o = json.loads(o)
            except ValueError:
                return
        if isinstance(o, dict):
            phone = ""
            for k in ("phone", "number", "jid", "id", "participant"):
                v = str(o.get(k) or "")
                if v and "@g.us" not in v and "@lid" not in v and 10 <= len(_number(v)) <= 15:
                    phone = _number(v)
                    break
            if phone:
                found[phone] = next((o[k] for k in ("name", "push_name", "display_name", "contact_name", "notify")
                                     if isinstance(o.get(k), str) and o.get(k)), "")
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(result)
    try:  # names from your saved contacts
        book = _call("GET", f"/accounts/{account_id}/contacts?limit=1000").get("contacts", [])
        names = {_number(c.get("phone") or c.get("jid") or c.get("chat")): c.get("name") or c.get("push_name") or ""
                 for c in book if isinstance(c, dict)}
        for ph in found:
            found[ph] = found[ph] or names.get(ph, "")
    except Exception:
        pass
    return found


def group_names(account_id):
    """{group id: name} from WhatsMCP (MCP wa_list_groups) plus your own labels file."""
    names = {}
    try:
        res = {"result": mcp_tool("wa_list_groups", {"account_id": account_id})}
        _find_groups(res.get("result", {}), names)
        if not names and os.environ.get("DEBUG_WAMCP"):
            print("MCP answer:", json.dumps(res)[:1500])
    except Exception as e:
        print("Could not read group names from WhatsMCP:", e)
    if os.path.exists(NAMES_FILE):
        with open(NAMES_FILE, encoding="utf-8-sig") as f:
            names.update(json.load(f))
    return names


def list_groups(account_id, with_names=True):
    chats = _call("GET", f"/accounts/{account_id}/chats?limit=200").get("chats", [])
    groups = [c for c in chats if "@g.us" in str(c.get("chat", ""))]
    if with_names and any(not g.get("name") for g in groups):
        names = group_names(account_id)
        for g in groups:
            g["name"] = g.get("name") or names.get(g["chat"], "")
    return groups


def group_matches(pattern, g):
    """A route or allowed_groups entry matches a group by part of its name, or by its id."""
    p = str(pattern).strip().lower()
    jid = str(g.get("chat", "")).lower()
    return bool(p) and (p == jid or p == jid.split("@")[0] or p in str(g.get("name", "")).lower())


def group_senders(account_id, name_part, own_phone=""):
    """Every member of the group (full member list from WhatsMCP), plus the names people use when they write."""
    from urllib.parse import quote
    groups = [g for g in list_groups(account_id) if group_matches(name_part, g)]
    if not groups:
        raise RuntimeError(f"No WhatsApp group with '{name_part}' in its name. Run: python run.py whatsmcp-groups")
    g = groups[0]
    people = {}
    try:
        people = {ph: n for ph, n in group_members(account_id, g["chat"]).items() if ph != own_phone}
    except Exception as e:
        print(f"  (full member list not available: {e}; using people who have written instead)")
    jid = quote(g["chat"], safe="")
    try:  # ask WhatsApp for older messages too, so more members show up
        _call("POST", f"/accounts/{account_id}/chats/{jid}/history", {"count": 500})
        time.sleep(5)
    except Exception:
        pass
    msgs = _call("GET", f"/accounts/{account_id}/chats/{jid}?limit=200").get("messages", [])
    for m in msgs:
        phone = _number(m.get("peer"))
        if phone and phone != own_phone and 10 <= len(phone) <= 15:
            people[phone] = people.get(phone) or m.get("sender_name") or ""
    return g.get("name", name_part), people


def add_group_to_contacts(path, account_id, name_part, role="", own_phone=""):
    gname, people = group_senders(account_id, name_part, own_phone)
    gname = gname or name_part
    data = {"people": []}
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        data["people"] = [p for p in data.get("people", []) if not str(p.get("phone", "")).startswith("910000000")]
    known = {"".join(ch for ch in str(p.get("phone", "")) if ch.isdigit()) for p in data["people"]}
    added = []
    for phone, name in people.items():
        if phone not in known:
            data["people"].append({"name": name or phone, "role": role, "phone": phone, "group": gname})
            added.append(f"{name or '?'} ({phone})")
        else:  # remember which groups a known person is in
            for p in data["people"]:
                if "".join(ch for ch in str(p.get("phone", "")) if ch.isdigit()) == phone:
                    gs = [x for x in str(p.get("group", "")).split(" | ") if x]
                    if gname not in gs:
                        p["group"] = " | ".join(gs + [gname])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return gname, added
