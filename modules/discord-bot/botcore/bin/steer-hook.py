#!/usr/bin/env python3
"""PreToolUse hook: deliver mid-turn Discord messages (steers/stops) into the
running claude turn. The bot appends pending messages to $DISCORD_INBOX (JSONL);
this marks them delivered, shows them on the live card, and blocks ONE tool
call (exit 2) so the model reads them and decides. Pure stdlib (no jq), so it
works anywhere the bot runs."""
import fcntl, json, os, pathlib, subprocess, sys

BOT_HOME = pathlib.Path(os.environ.get("BOT_HOME") or
                        pathlib.Path.home() / ".config" / "kit" / "discord-bot")


def owner_name():
    conf = BOT_HOME / "bot.conf"
    try:
        for line in conf.read_text().splitlines():
            if line.strip().startswith("BOT_OWNER_NAME="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or "the user"
    except OSError:
        pass
    return "the user"


def main():
    inbox = os.environ.get("DISCORD_INBOX", "")
    if not inbox:
        return 0
    p = pathlib.Path(inbox)
    if not p.exists() or p.stat().st_size == 0:
        return 0
    with open(str(p) + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        items, pending = [], []
        for line in p.read_text().splitlines():
            try:
                it = json.loads(line)
            except ValueError:
                continue
            if it.get("status") == "pending":
                pending.append(it)
                it = {**it, "status": "delivered"}
            items.append(it)
        if not pending:
            return 0
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text("".join(json.dumps(i) + "\n" for i in items))
        os.replace(tmp, p)
    card = pathlib.Path(__file__).resolve().parent / "discord-live-card.py"
    for it in pending:   # surface each steer on the live card instantly
        txt = " ".join(str(it.get("content", "")).split())[:70]
        if txt and card.exists():
            subprocess.run([sys.executable, str(card), "steer", txt], capture_output=True, timeout=15)
    who = owner_name()
    lines = [f"MID-TURN MESSAGE(S) FROM {who.upper()} — sent while you were working:"]
    lines += [f"  • {it.get('content', '')}" for it in pending]
    lines.append(f"""The blocked tool call did NOT run. Decide what this means:
1. Stop-like ("stop", "hold off", "disregard that") → abandon the current work NOW and reply, stating clearly what already executed vs what was prevented.
2. A correction/steer related to the current work → incorporate it and continue (re-issue the tool call adapted as needed).
3. A separate new task → finish the current work first, and end your reply with a line exactly of the form <<<DEFER: restated task>>> — the bot auto-runs it immediately after this turn; do NOT promise to handle it later yourself.
The live card (if one is active) already shows "✉ steer: …" under the running step — this hook added it automatically. Upgrade it: if you take the steer on as work, add it as a real step (discord-live-card.py add "PHASE:model:label"); if you defer it, add a note "⏭ queued next" (discord-live-card.py note ...).
In every case, end your reply with a short footer: "↪️ incorporated: …" and/or "⏭️ queued next: …" so {who} knows how their message was handled.""")
    print("\n".join(lines), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
