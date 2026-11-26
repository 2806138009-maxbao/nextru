"""Claude Code UserPromptSubmit hook: inject Nexus situational knowledge before the model sees the prompt.

Reads the hook JSON on stdin ({"prompt": ...}); prints hook JSON with additionalContext. Fails open:
any error means no injection and exit 0, so the hook can never block a prompt.

Config (environment, set in the hook command or settings):
    NEXUS_CONTEXT_URL   e.g. http://127.0.0.1:8791   (preferred: shared provider process)
    or NEXUS_DB + NEXUS_SCOPE                         (in-process, read-only)

.claude/settings.json (project) example:
  {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command",
     "command": "python -X utf8 integrations/claude_code/nexus_context_hook.py"}]}]}}
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def fetch(task: str) -> dict:
    url = os.environ.get("NEXUS_CONTEXT_URL")
    if url:
        req = urllib.request.Request(url.rstrip("/") + "/context", method="POST",
                                     data=json.dumps({"task": task, "client": "claude-code-hook"}).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    from nexus.context_provider import ContextProvider
    return ContextProvider(os.environ["NEXUS_DB"], os.environ["NEXUS_SCOPE"],
                           log_path=os.environ.get("NEXUS_CONTEXT_LOG")).context(task, client="claude-code-hook")


def main() -> int:
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
        prompt = json.loads(sys.stdin.read() or "{}").get("prompt", "")
        if not prompt.strip():
            return 0
        from nexus.context_provider import render
        text = render(fetch(prompt))
        if text:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                     "additionalContext": text}}, ensure_ascii=False))
    except Exception as exc:  # fail open
        print(f"nexus-context hook skipped: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
