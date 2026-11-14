"""Who may use the bot, and what Claude may do on this computer.

  * allowed(user_id): the owner (bot.conf BOT_OWNER_ID) plus anyone added with
    `dbot allow` (BOT_ALLOWED_USERS, comma separated). Everyone else is ignored.
  * mode(): bot.conf MODE = full | safe
      full: Claude can read, edit and run commands (like Claude Code with
            --dangerously-skip-permissions)
      safe: read, search and web only; no edits, no shell commands except
            the bot's own Discord tools
  * claude_flags()/claude_env(): what every `claude -p` run gets. Hooks and
    permission rules are passed with --settings, so ~/.claude/settings.json
    is never edited.
"""
import json
import os
import pathlib
import sys

from botcore import paths
from botcore.config import conf

SECRET_ENV_KEYS = ("DISCORD_TOKEN",)

SAFE_TOOLS = ["Read", "Grep", "Glob", "WebSearch", "WebFetch", "Agent", "Task",
              "TodoWrite", "Bash(discord-live-card.py:*)", "Bash(discord-update:*)"]
SAFE_BLOCKED = ["Edit", "Write", "NotebookEdit"]


def _ids(raw):
    out = set()
    for part in (raw or "").replace(" ", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


def allowed(user_id):
    users = _ids(conf.get("BOT_OWNER_ID")) | _ids(conf.get("BOT_ALLOWED_USERS"))
    return bool(users) and int(user_id) in users


def mode():
    m = conf.get("MODE", "full").strip().lower()
    return m if m in ("full", "safe") else "full"


def _rule_path(p):
    # Claude Code permission rules: "//" + absolute path
    return "//" + str(pathlib.Path(p).resolve()).lstrip("/")


def secret_files():
    files = [paths.SECRETS_FILE]
    kit_conf = paths.HOME.parent                   # .../kit
    files.append(kit_conf / "ntfy" / "profiles.json")
    return files


def settings_json():
    py = sys.executable
    deny = []
    for f in secret_files():
        deny += [f"Read({_rule_path(f)})", f"Edit({_rule_path(f)})"]
    card = f'"{py}" "{paths.TOOLS / "discord-live-card.py"}" hook'
    hooks = {
        "PreToolUse": [
            {"hooks": [{"type": "command", "command": f'"{py}" "{paths.TOOLS / "steer-hook.py"}"', "timeout": 10}]},
            {"matcher": "Agent|Task", "hooks": [{"type": "command", "command": card, "timeout": 15}]},
        ],
        "PostToolUse": [{"matcher": "Agent|Task", "hooks": [{"type": "command", "command": card, "timeout": 15}]}],
        "SubagentStop": [{"hooks": [{"type": "command", "command": card, "timeout": 15}]}],
    }
    return json.dumps({"permissions": {"deny": deny}, "hooks": hooks})


def claude_flags():
    flags = ["--settings", settings_json()]
    if mode() == "safe":
        # one comma-joined value each: these options are variadic in the CLI
        return flags + ["--allowedTools", ",".join(SAFE_TOOLS),
                        "--disallowedTools", ",".join(SAFE_BLOCKED)]
    return flags + ["--dangerously-skip-permissions"]


def tool_shims():
    """Small launchers for the bot's Discord tools, run with the bot's Python
    (so a missing or prompting system python3 can't break them)."""
    d = paths.HOME / "bin"
    d.mkdir(parents=True, exist_ok=True)
    for name in ("discord-update", "discord-live-card.py", "request-restart"):
        target = paths.TOOLS / name
        shim = d / name
        text = f'#!/bin/sh\nexec "{sys.executable}" "{target}" "$@"\n'
        if not shim.exists() or shim.read_text() != text:
            shim.write_text(text)
            shim.chmod(0o755)
    return d


def claude_env():
    """Environment for Claude runs: no secrets, bot tools on PATH."""
    e = {k: v for k, v in os.environ.items() if k not in SECRET_ENV_KEYS}
    e["BOT_HOME"] = str(paths.HOME)
    e["PATH"] = str(tool_shims()) + os.pathsep + e.get("PATH", "")
    return e
