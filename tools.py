"""Tools the agents can use: your knowledge base (with its Graphify map) and GitHub.

An agent uses a tool by writing ONE line in its answer, for example:
  TOOL github_issues: anirag1206/lucent-qa
  TOOL github_commits: anirag1206/lucent-ebmr
  TOOL github_file: anirag1206/lucent-kb README.md
  TOOL graphify: yield target
  TOOL kb_read: data/stock_top40.csv

graphify / kb_read use your local lucent-kb folder (free, no key). If the open source
graphify tool is installed they use 'graphify query', otherwise a plain text search.
GitHub needs the Windows environment variable GITHUB_TOKEN (read-only is enough).
If a key is missing the tool just says so; nothing breaks.
"""
import base64
import json
import os
import urllib.request

TOOL_HELP = (
    "Tools you may use (one line each, only when useful):\n"
    "TOOL graphify: <question>             -> search the company knowledge base and its Graphify map\n"
    "TOOL kb_read: <file path>             -> read a knowledge base file, e.g. data/order_book_open.csv\n"
    "TOOL github_issues: <owner/repo>      -> open issues of a GitHub repository\n"
    "TOOL github_commits: <owner/repo>     -> latest changes in a repository\n"
    "TOOL github_file: <owner/repo> <path> -> read a file from a repository"
)


def run_tool(name, arg, kb=None):
    try:
        if name == "graphify":
            return kb.search(arg.strip()) if kb else "No knowledge base configured."
        if name == "kb_read":
            return (kb.read(arg.strip(), 3000) if kb else "") or "File not found in the knowledge base."
        if name == "github_issues":
            items = _gh(f"/repos/{arg.strip()}/issues?state=open&per_page=10")
            return "\n".join(f"#{i['number']} {i['title']}" for i in items if "pull_request" not in i) or "No open issues."
        if name == "github_commits":
            items = _gh(f"/repos/{arg.strip()}/commits?per_page=8")
            return "\n".join(f"{c['commit']['author']['date'][:10]} {c['commit']['message'].splitlines()[0]}" for c in items)
        if name == "github_file":
            repo, path = arg.strip().split(" ", 1)
            data = _gh(f"/repos/{repo}/contents/{path.strip()}")
            return base64.b64decode(data["content"]).decode("utf-8", "replace")[:3000]
        return f"Unknown tool '{name}'."
    except Exception as e:
        return f"Tool {name} failed: {e}"


# ---------------- GitHub (REST API) ----------------
def _gh(path):
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is not set")
    req = urllib.request.Request(
        "https://api.github.com" + path,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "User-Agent": "ops-agent-team"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))
