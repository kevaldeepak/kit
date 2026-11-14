"""Long-thread savers: keep "one thread = one conversation" without re-reading
an ever-growing history every turn.

  * auto-compact: when a turn's context passes bot.conf COMPACT_AT tokens
    (default 150k), the thread's session is compacted in the background right
    after the reply (Claude Code `/compact`, same session id). The next turn in
    that thread waits for it, then starts from a short summary.
  * project brief: each thread gets a small living brief file that Claude
    creates and maintains itself; compaction keeps it verbatim.
  * helper mode (botcore/helpers.py, /helpers): off / auto / turbo subagent use.
The appended system prompt is static per thread (path + rules, never the brief
contents), so it doesn't break prompt caching between turns.
"""
import asyncio, pathlib, time
from botcore import paths

from botcore.config import conf, conf_int

BRIEF_DIR = paths.STATE / "briefs"
BRIEF_DIR.mkdir(parents=True, exist_ok=True)

threads = set()          # channel ids known to be Discord threads (get a brief)
compacting = {}          # session key -> asyncio.Task of a running compaction
notes = {}               # session key -> footer note for the NEXT reply


def compact_at():
    return conf_int("COMPACT_AT", 150_000)


def auto_compact_on():
    return conf.get("AUTO_COMPACT", "on").lower() != "off"


def brief_path(thread_id):
    return BRIEF_DIR / f"{thread_id}.md"


BOT_TOOLS = (
    "You are answering through a Discord bot. Only the final text after your last tool "
    "call is posted, so do every tool call first and write the full answer last; never "
    "refer to text 'above'. Discord tools on PATH (they post into the current thread): "
    "`discord-update \"text\"` posts a progress message without ending the turn; "
    "`discord-live-card.py start \"TITLE\" \"PHASE:model:label\" ...` then `next`, `sub \"text\"`, "
    "`add \"PHASE:model:label\"`, `finish \"1-4 line result\"` or `fail \"reason\"` drives a live "
    "progress card for substantial multi-step work (skip it for quick answers; when the card "
    "holds the result, the final reply can be one short line). "
    "`request-restart \"message\"` restarts this bot safely and posts the message when it is "
    "back; use it only when asked to restart the bot, never a plain service restart. The bot's "
    "token and other secrets are off limits: never read, print or send them.")


def system_addendum(channel_key):
    """Extra system prompt for a turn. Static per thread for cache stability."""
    tid = (channel_key or "").split("~")[0]
    from botcore import helpers
    parts = [helpers.addendum(channel_key)]
    parts.append("Tables: Discord can't show markdown tables, but the bot converts any you "
                 "write (small ones to an aligned text block with coloured status words, wide "
                 "ones to an image). Use them only when a grid really helps, keep cells short, "
                 "and put status words like ok / fail / pending first in a cell.")
    parts.append(BOT_TOOLS)
    if tid in threads and conf.get("BRIEFS", "on").lower() != "off":
        p = brief_path(tid)
        parts.append(
            f"This Discord thread is one long-running conversation. Its project brief "
            f"lives at {p}. Rules: (1) once the thread involves real project work and no "
            f"brief exists, create it; (2) in the same turn, update it whenever a "
            f"decision, status, file path/URL or the user's preference changes; (3) keep "
            f"it under 40 lines with sections Goal / Decisions / Key paths & URLs / Done / "
            f"Next / Preferences; (4) never put secrets in it; (5) read it when you need "
            f"project facts instead of asking again. Don't mention the brief unless asked.")
    return "\n\n".join(parts)


COMPACT_FOCUS = (
    "Keep: the project brief file contents verbatim if one exists for this thread; "
    "every decision and its reason; file paths, URLs, hosts, ports, commands that "
    "worked; what is done and what is next; the user's stated preferences and "
    "corrections. Drop: raw tool output, file contents, logs, superseded attempts.")


async def compact(session_key, session_id, ctx_tokens, cwd, env):
    """Run /compact on a session in the background (same session id)."""
    t0 = time.monotonic()
    model = conf.get("COMPACT_MODEL", "claude-sonnet-5-5")
    from botcore import access
    cmd = ["claude", "-p", f"/compact {COMPACT_FOCUS}", "--resume", session_id,
           "--model", model, "--output-format", "json", *access.claude_flags()]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL, cwd=cwd, env=env)
        await asyncio.wait_for(proc.communicate(), timeout=600)
        ok = proc.returncode == 0
    except Exception as e:
        print(f"[compact] {session_key} failed: {e!r}", flush=True)
        ok = False
    secs = int(time.monotonic() - t0)
    print(f"[compact] {session_key} ctx={ctx_tokens} ok={ok} {secs}s", flush=True)
    if ok:
        notes[session_key] = (f"-# 🗜️ history compacted after the last reply "
                              f"(was {ctx_tokens / 1000:.0f}k tokens of context)")


def maybe_compact(session_key, session_id, ctx_tokens, cwd, env, spawn):
    if not auto_compact_on():
        return
    if not (session_key and session_id) or ctx_tokens < compact_at():
        return
    if session_key in compacting and not compacting[session_key].done():
        return
    task = spawn(compact(session_key, session_id, ctx_tokens, cwd, env))
    compacting[session_key] = task


async def wait_compaction(session_key):
    """A new turn on a session being compacted waits for it (never resume twice)."""
    task = compacting.get(session_key)
    if task and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=600)
        except Exception:
            pass
    compacting.pop(session_key, None)


def pop_note(session_key):
    return notes.pop(session_key, None)
