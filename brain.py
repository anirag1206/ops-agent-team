"""The 'brain' every agent uses to think.

Three options, chosen in config.json:
  ollama -> free, runs on your own PC (default)
  claude -> paid per use, smarter, needs ANTHROPIC_API_KEY
  demo   -> no AI, canned answers, only for testing the wiring
"""
import json
import os
import urllib.request


class Brain:
    def __init__(self, config):
        self.kind = config.get("brain", "ollama")
        self.config = config

    def think(self, system, prompt, max_tokens=700):
        if self.kind == "ollama":
            return self._ollama(system, prompt)
        if self.kind == "claude":
            return self._claude(system, prompt, max_tokens)
        return self._demo(system, prompt)

    # ---- free, local -------------------------------------------------
    def _ollama(self, system, prompt):
        url = self.config.get("ollama_url", "http://localhost:11434") + "/api/chat"
        body = {
            "model": self.config.get("ollama_model", "qwen2.5:7b"),
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "options": {"temperature": 0.3, "num_ctx": int(self.config.get("ollama_context", 16384))},
        }
        try:
            data = _post_json(url, body, {}, timeout=600)
        except Exception as e:
            raise RuntimeError(
                "Could not reach Ollama. Is the Ollama app running, and did you run "
                f"'ollama pull {body['model']}'? Details: {e}"
            )
        return data["message"]["content"].strip()

    # ---- paid, hosted ------------------------------------------------
    def _claude(self, system, prompt, max_tokens):
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("Set the ANTHROPIC_API_KEY environment variable to use Claude.")
        body = {
            "model": self.config.get("claude_model", "claude-sonnet-5-5"),
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        data = _post_json("https://api.anthropic.com/v1/messages", body, headers, timeout=120)
        return "".join(b.get("text", "") for b in data["content"]).strip()

    # ---- offline test mode -------------------------------------------
    def _demo(self, system, prompt):
        role = system.split("You are the ", 1)[-1].split(" of", 1)[0] if "You are the " in system else "Agent"
        if "Reply ONLY with agent ids" in prompt:
            return ""  # let keyword routing decide
        if "final decision" in prompt.lower():
            return (
                f"[{role}] Decision: go ahead, in the order the heads suggested.\n"
                "ACTION: coo | Confirm production slot for the request | 2 days\n"
                "ACTION: cfo | Check cost and cash impact | 3 days"
            )
        return f"[{role}] (demo answer) I have reviewed this from my department's side. No blockers seen."


def _post_json(url, body, headers, timeout):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))
