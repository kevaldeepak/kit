import os, json, asyncio, pathlib, re, time, sys, contextlib
from datetime import datetime, timezone
import discord
from discord import app_commands
from botcore.config import conf, conf_int
from botcore import paths, access

# Name the prompts use for the human who owns this bot (bot.conf BOT_OWNER_NAME).
OWNER = conf.get("BOT_OWNER_NAME") or "the user"


# ──── CONFIG ──────────────────────────────────────────────────────

# The Discord token lives in BOT_HOME/secrets.env (owner-only file). It is
# read here once and never handed to Claude's environment.
env = paths.load_secrets()

# ──── BOTCORE: split-out modules (models, routing, state, pollers) ────
from botcore.models import (MODELS, DEFAULT_BIG, resolve_model, FAST_MODEL,
    HAIKU_MODEL, HELPER_MCP_FLAGS, BIG_LABEL, usage_footer)
from botcore.routing import (default_tier)
from botcore.text import (split_chunks)
from botcore.stores import (save_sessions, sessions, save_overrides, model_overrides,
    save_thread_index, index_thread, thread_index, running_procs, last_prompts,
    recent_thread_searches, busy_channels,
    killed_channels, inbox_path, drain_inbox, thread_tasks, task_counters,
    PARALLEL_CAP, _next_task_id, append_parallel_note, pop_parallel_notes, engagement,
    _save_engagement)
from botcore import pollers, saver, context, helpers, tables
from botcore.stores import mark_activity

LOGS  = paths.LOGS
CWD   = paths.workdir()

# Settings come from BOT_HOME/bot.conf (botcore.config), written by `dbot`.
ALLOWED_CHANNEL_ID = conf_int("BOT_CHANNEL_ID")
if not ALLOWED_CHANNEL_ID:
    sys.exit(f"BOT_CHANNEL_ID missing from {paths.CONF_FILE} (run: dbot set channel <id>)")
CLAUDE_TIMEOUT = 12 * 3600  # max runtime per prompt; big overnight projects need hours
EDIT_THROTTLE = 1.5
DISCORD_SAFE_LIMIT = 1900
PING_LIMIT = 140       # end-ping caps here so the whole thing fits one phone notification
RENDER_MENTION = 8     # approx width of "@name " as rendered in a push notification
LONG_REPLY_FILE_THRESHOLD = 3500
THREAD_HISTORY_LIMIT = 30
BLOCKED_EXTS = {".exe", ".dll", ".so", ".dylib", ".msi", ".dmg", ".deb", ".rpm"}

# Incoming attachments persist here (not /tmp) so continued threads can still
# open them after a reboot; files older than 30 days are swept at startup.
FILES_DIR = paths.FILES
ATTACHMENT_MAX_BYTES = 50 * 1024 * 1024

# Pasted/unnamed uploads arrive without a filename extension — recover it from
# the content type so the saved file is readable as what it actually is.
CONTENT_TYPE_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "image/bmp": ".bmp", "image/svg+xml": ".svg",
    "application/pdf": ".pdf", "text/plain": ".txt", "text/csv": ".csv",
    "application/json": ".json", "text/html": ".html", "text/markdown": ".md",
    "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/wav": ".wav",
    "video/mp4": ".mp4", "video/quicktime": ".mov",
}

ULTRA_SUFFIX = ("This is ultra mode. Fan the work out across multiple subagents spawned "
                "with the Agent tool, each on a focused piece, running in parallel where "
                "possible, then synthesize their results into a single answer. Prefer "
                "directly-spawned subagents over the Workflow tool so their progress is "
                "visible.")

# ──── FAST LANE: trivial turns ride a smaller model on the SAME session ─────
# Memory lives in the session transcript, not the model — Sonnet resuming the
# shared session is still "one brain", just cheaper/faster horsepower.
FAST_PREFIX = (
    "[FAST-LANE TURN] You are running as a smaller, faster model for this one turn "
    "of an ongoing conversation. Answer briefly. Read-only tool use is fine (status "
    "checks, reading files/state, quick lookups). If this turn requires MODIFYING "
    "anything (files, configs, services, repos, deployments) or turns out to need "
    "deep reasoning or multi-step work, reply with exactly <<<ESCALATE>>> and "
    "nothing else — the turn will be transparently re-run on the big model.\n\n")

# Standing capability note appended to every turn: how to deliver a real file as a
# Discord attachment. Write the file first, then emit the marker; the bot uploads it.
FILE_HINT = (
    f"[file delivery] To send a file to {OWNER} as a real Discord attachment, write it "
    "to disk (any absolute path) then include a marker `<<<FILE:/abs/path>>>` "
    "anywhere in your reply. The bot strips the marker and uploads the file (≤24MB, "
    f"up to 10 per message). Use this whenever {OWNER} asks for a file, document, "
    "export, or download.")

# Standing capability note: clickable option buttons under a question. Rides
# every turn (a question with fixed options can come up in any turn), so it is
# kept to two sentences to avoid the dilution problem FILE_HINT had.
BUTTONS_HINT = (
    f"[option buttons] When your reply asks {OWNER} a question with 2-10 fixed "
    "choices, end it with `<<<BUTTONS: option one | option two | ...>>>` — the "
    "bot strips the marker and renders the options as clickable buttons (a click "
    "is their answer; typing a custom reply always works too). Only use it for "
    "genuinely fixed choices, and keep labels under ~60 characters.")

# Only inject FILE_HINT when the turn is actually file-shaped: a trailing
# instruction on EVERY prompt dilutes task-specific formatting. Keyword-gated.
FILE_ASK_RE = re.compile(
    r"\b(file|files|attach|attachment|upload|download|export|handover|"
    r"document|spreadsheet|pdf|csv|zip|tarball|screenshot)\b"
    r"|\.(md|txt|csv|json|log|ya?ml|pdf|zip|py|sh|png|jpe?g)\b",
    re.I)

def wants_file(text):
    return bool(text and FILE_ASK_RE.search(text))

async def route_mid_turn(active, content):
    """Classify a message that arrived while task(s) run in its thread.
    Returns ("steer", task_id) or ("new", None). Any failure → steer to the
    oldest active task (pre-upgrade behaviour)."""
    fallback = ("steer", min(active))
    roster = "\n".join(
        f"Task {tid}: {rec['label']}" for tid, rec in sorted(active.items()))
    rp = (
        f"You route a Discord message from {OWNER} that arrived while Claude task(s) "
        "are already running in the same thread.\n\nRunning tasks:\n" + roster +
        "\n\nNew message:\n" + content[:1500] + "\n\nDecide:\n"
        '- "steer": the message adjusts, corrects, questions, stops, or otherwise '
        "redirects ONE of the running tasks (e.g. \"actually make it black\", "
        "\"for task 2 use X\", \"stop\", \"also add a button to that page\"), "
        "depends on a running task's outcome, or is a quick ack/reply to it.\n"
        '- "new": a task that can run in parallel right now. Being about the same '
        "project or app as a running task does NOT make it a steer — only a real "
        "conflict does (it would edit the same files/config/service a running "
        "task is editing, or needs that task's result first). Research, "
        "brainstorming, advice, look-ups, status checks and other read-only asks "
        "are ALWAYS safe as new parallel tasks, whatever their topic.\n"
        "If torn: steer for tweaks to work in flight, new for anything with its "
        "own separate deliverable.\n"
        'Reply ONLY with minified JSON: {"action":"steer","task":<task number>} '
        'or {"action":"new"}')
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", "--model", HAIKU_MODEL, "--tools", "", *HELPER_MCP_FLAGS,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(rp.encode()), timeout=25)
        m = re.search(r"\{.*\}", out.decode(errors="replace"), re.S)
        if not m:
            return fallback
        verdict = json.loads(m.group(0))
        if verdict.get("action") == "new":
            return ("new", None)
        try:
            tid = int(verdict.get("task"))
        except (TypeError, ValueError):
            tid = None
        return ("steer", tid if tid in active else min(active))
    except Exception as e:
        print(f"[router] fell back to steer: {e}", flush=True)
        try:
            if proc:
                proc.kill()
        except Exception:
            pass
        return fallback

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
client = discord.Client(intents=intents)


class OwnerOnlyTree(app_commands.CommandTree):
    """Slash commands answer the owner (and `dbot allow` users) only."""
    async def interaction_check(self, interaction):
        if access.allowed(interaction.user.id):
            return True
        with contextlib.suppress(discord.HTTPException):
            await interaction.response.send_message(
                "This bot only takes commands from its owner.", ephemeral=True)
        return False


tree = OwnerOnlyTree(client)


# ──── ATTACHMENTS ────────────────────────────────────────────────

def _convert_office(path):
    """Office formats aren't natively readable by the agent — extract a plain
    sidecar next to the original (docx/pptx/odt → .txt from the XML inside the
    zip, xlsx → .csv via openpyxl). Returns the sidecar path or None."""
    import zipfile, csv, html as _html
    ext = path.suffix.lower()
    try:
        if ext in (".docx", ".pptx", ".odt"):
            with zipfile.ZipFile(path) as z:
                if ext == ".docx":
                    names = [n for n in ("word/document.xml",) if n in z.namelist()]
                elif ext == ".odt":
                    names = [n for n in ("content.xml",) if n in z.namelist()]
                else:
                    names = sorted(n for n in z.namelist()
                                   if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
                parts = []
                for n in names:
                    xml = z.read(n).decode("utf-8", "ignore")
                    xml = re.sub(r"</(?:w|a|text):p>", "\n", xml)
                    parts.append(_html.unescape(re.sub(r"<[^>]+>", "", xml)))
            if not any(p.strip() for p in parts):
                return None
            out = pathlib.Path(str(path) + ".txt")
            out.write_text("\n\n".join(parts), encoding="utf-8")
            return out
        if ext == ".xlsx":
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            out = pathlib.Path(str(path) + ".csv")
            with open(out, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                for ws in wb.worksheets:
                    w.writerow([f"# sheet: {ws.title}"])
                    for row in ws.iter_rows(values_only=True):
                        w.writerow(["" if v is None else v for v in row])
            return out
    except Exception as e:
        print(f"attachment convert failed for {path.name}: {e}", flush=True)
    return None


async def download_attachments(message):
    """Save attachments to FILES_DIR; return (paths, notes). Office files get a
    readable sidecar added to paths; blocked/oversized files are skipped with a
    note that rides the prompt so the skip is never silent."""
    paths, notes = [], []
    for i, att in enumerate(message.attachments):
        ext = (pathlib.Path(att.filename).suffix or "").lower()
        if not ext and att.content_type:
            ext = CONTENT_TYPE_EXT.get(att.content_type.split(";")[0].strip(), "")
        if ext in BLOCKED_EXTS:
            notes.append(f"[attachment {att.filename} skipped: blocked file type]")
            continue
        if att.size and att.size > ATTACHMENT_MAX_BYTES:
            notes.append(f"[attachment {att.filename} skipped: "
                         f"{att.size // (1024 * 1024)}MB is over the 50MB cap]")
            continue
        path = FILES_DIR / f"discord-{message.id}-{i}{ext or '.bin'}"
        try:
            await att.save(path)
        except Exception as e:
            print(f"attachment save failed: {e}", flush=True)
            continue
        paths.append(str(path))
        sidecar = await asyncio.to_thread(_convert_office, path)
        if sidecar:
            paths.append(str(sidecar))
    return paths, notes


def saved_attachment_paths(msg_id):
    """Where an earlier message's attachments landed on disk — lets a cold
    session reopen them."""
    hits = []
    hits += sorted(str(p) for p in FILES_DIR.glob(f"discord-{msg_id}-*"))
    return hits


# ──── REPLY CONTEXT + REACTIONS ──────────────────────────────────

def format_reactions(message):
    if not message.reactions:
        return ""
    return ", ".join(f"{r.emoji} {r.count}" for r in message.reactions)


# In-flight markers the bot puts on the user's message while a turn is working.
# They must be cleared when the turn ends, otherwise a message looks "in progress"
# forever. resolve_reactions() swaps whichever is present for a terminal marker.
WORKING_REACTIONS = ("⚡", "🐇", "🏗️", "👀")
DONE_OK = "✅"
DONE_ERROR = "⚠️"
DONE_STOPPED = "🛑"


async def resolve_reactions(message, terminal):
    """Close the emoji lifecycle on `message`: drop the bot's working markers and
    leave a single terminal one (✅ ok / ⚠️ error / 🛑 stopped). No-op on None."""
    if message is None:
        return
    for emoji in WORKING_REACTIONS:
        try:
            await message.remove_reaction(emoji, client.user)
        except discord.HTTPException:
            pass
    if terminal:
        try:
            await message.add_reaction(terminal)
        except discord.HTTPException:
            pass


async def reply_context_block(message):
    """If this message is a reply, fetch the parent and format a context block."""
    ref = message.reference
    if not ref or not ref.message_id:
        return ""
    try:
        parent = await message.channel.fetch_message(ref.message_id)
    except Exception:
        return ""
    author = parent.author.display_name
    minutes = max(0, int((datetime.now(timezone.utc) - parent.created_at).total_seconds() / 60))
    age = f"{minutes}m ago" if minutes < 90 else f"{minutes/60:.1f}h ago"
    body = (parent.content or "").strip()
    if len(body) > 800:
        body = body[:800] + "…"
    header = f"[in reply to @{author} · {age}"
    rx = format_reactions(parent)
    if rx:
        header += f", reactions: {rx}"
    header += "]"
    quoted = "\n".join("> " + l for l in body.splitlines()) if body else "> _(no text)_"
    return f"{header}\n{quoted}\n\n"


def current_reactions_block(message):
    rx = format_reactions(message)
    return f"[reactions on this message: {rx}]\n\n" if rx else ""


# Lead text of the message the "move to a new thread" feature drops into a fresh
# thread. thread_history_block keeps messages with this prefix uncapped so the
# cold session inherits the whole handover summary.
HANDOVER_MARKER = "📦 **Context handover"


async def thread_history_block(message, limit=THREAD_HISTORY_LIMIT):
    """Fetch recent messages in this thread (oldest→newest, excluding the
    current one) so a cold session has the backlog it never saw. Streaming
    placeholders are deleted in finalize(), so remaining bot messages are
    real replies worth keeping as context."""
    channel = message.channel
    if not isinstance(channel, discord.Thread):
        return ""
    msgs = []
    try:
        async for m in channel.history(limit=limit, before=message):
            msgs.append(m)
    except Exception as e:
        print(f"thread history fetch failed: {e}", flush=True)
        return ""
    if not msgs:
        return ""
    msgs.reverse()
    lines = ["[thread history, oldest first — earlier context for the message below]"]
    for m in msgs:
        author = "Claude (bot)" if m.author == client.user else f"@{m.author.display_name}"
        body = (m.content or "").strip()
        if not body:
            body = f"_({len(m.attachments)} attachment(s))_" if m.attachments else "_(no text)_"
        # A context handover is the whole point of a continued thread — keep it
        # intact so the cold session inherits the full summary, not a 400-char stub.
        cap = 1600 if body.startswith(HANDOVER_MARKER) else 400
        if len(body) > cap:
            body = body[:cap] + "…"
        line = f"{author}: {body.replace(chr(10), ' ')}"
        # a cold session can't see Discord files — point it at the saved copies
        if m.attachments:
            saved = saved_attachment_paths(m.id)
            if saved:
                line += f" [attachments on disk: {', '.join(saved)}]"
        lines.append(line)
    return "\n".join(lines) + "\n\n"


# ──── PROMPT ASSEMBLY ────────────────────────────────────────────

async def build_prompt(message, body, attachment_paths, ultracode=False,
                       include_thread_history=False, attachment_notes=None):
    who = f"[discord user: {message.author.display_name} (@{message.author.name})] [discord channel: {message.channel.id}]"
    hist = await thread_history_block(message) if include_thread_history else ""
    ctx = await reply_context_block(message)
    rx = current_reactions_block(message)
    refs = " ".join(f"@{p}" for p in attachment_paths) if attachment_paths else ""
    if attachment_notes:
        refs = (refs + "\n" if refs else "") + "\n".join(attachment_notes)
    pieces = [who, ""]
    if hist:
        pieces.append(hist)
    if ctx or rx:
        pieces.append(ctx + rx)
    pieces.append(body or "")
    if refs:
        pieces.extend(["", refs])
    if ultracode:
        pieces.extend(["", ULTRA_SUFFIX])
    if wants_file(body):
        pieces.extend(["", FILE_HINT])
    pieces.extend(["", BUTTONS_HINT])
    return "\n".join(pieces).strip()


# ──── STREAMING CLAUDE ───────────────────────────────────────────

def describe_tool(name, inp):
    if name == "Read":
        p = (inp.get("file_path") or "").split("/")[-1]
        return f"Reading {p}"
    if name == "Edit":
        p = (inp.get("file_path") or "").split("/")[-1]
        return f"Editing {p}"
    if name == "Write":
        p = (inp.get("file_path") or "").split("/")[-1]
        return f"Writing {p}"
    if name == "Bash":
        return "Running " + (inp.get("description") or (inp.get("command", "")[:60]))
    if name == "Grep":
        return f'Grep "{(inp.get("pattern", "") or "")[:40]}"'
    if name == "Glob":
        return f"Glob {inp.get('pattern','')}"
    if name == "Workflow":
        return f"Workflow: {inp.get('name','custom')}"
    if name == "Agent":
        d = (inp.get("description") or "")[:50]
        return f"Subagent: {d}"
    if name == "WebFetch":
        return f"Fetch {(inp.get('url') or '')[:60]}"
    if name == "WebSearch":
        return f'Search "{(inp.get("query") or "")[:40]}"'
    return name


AGENT_TOOLS = {"Agent", "Task"}


def _agent_label(inp):
    return (inp.get("description") or inp.get("subagent_type") or "subagent")[:60]


# ──── live agent-card integration (bin/discord-live-card.py) ─────
# The turn's status message doubles as the agent-card canvas. run_pipeline
# writes an init file carrying the status message id + a title; the card
# tool's `start` PATCHes that same message into the full card and drops an
# "owned" marker. While owned, the bot must never edit the message again and
# must not delete it at delivery (the finished card stays in the channel).

def card_init_write(channel_key, message_id):
    (paths.RUN / f"discord-card-init-{channel_key}.json").write_text(
        json.dumps({"message_id": str(message_id)}))


def card_owned(channel_key, message_id=None):
    try:
        owner = (paths.RUN / f"discord-card-owned-{channel_key}").read_text().strip()
    except OSError:
        return False
    return True if message_id is None else owner == str(message_id)


def card_turn_reset(channel_key):
    """New turn → any leftover card files are stale (a finished card deletes
    its own state; only killed/crashed runs leave debris)."""
    for p in (f"discord-card-owned-{channel_key}",
              f"discord-live-card-{channel_key}.json",
              f"discord-card-init-{channel_key}.json"):
        (paths.RUN / p).unlink(missing_ok=True)


def precard_stub(activity="🤔 thinking…"):
    """Status message content BEFORE the card tool takes over: a minimal card
    shell with the live thinking/tool/escalation activity INSIDE the box. No
    title line: the raw user message copy-pasted up there reads poorly; a real
    title arrives when the card tool's `start` takes the message over."""
    act = re.sub(r"[`\n]", " ", str(activity)).strip()[:200] or "🤔 thinking…"
    return f"```\n🤖 TASK\n\n━━ STARTING ━━━━━━━━━━━━\n{act}\n```"


async def retire_status(status_msg, key=None):
    """Delete the turn's status message UNLESS the card took it over — a
    finished card is the run's record and stays. `key` is the turn's track key
    (a sibling task's "chid~tN"); default = the channel id (primary turns)."""
    key = key or str(status_msg.channel.id)
    if card_owned(key, status_msg.id):
        (paths.RUN / f"discord-card-owned-{key}").unlink(missing_ok=True)
        return
    with contextlib.suppress(discord.HTTPException):
        await status_msg.delete()


LOST_TEXT_MIN = 400


def _with_lost_text(result, blocks):
    """The CLI's `result` is only the LAST text block. Substantial text written
    before a later tool call (a plan, then a brief/memory Write) was silently
    dropped. Prepend those blocks; short "let me check" narration stays dropped."""
    if not result:
        return result
    keep = []
    for b in blocks[:-1]:
        b = re.sub(r"<<<BUTTONS:.*?>>>", "", b, flags=re.S).strip()
        if len(b) >= LOST_TEXT_MIN and b not in result and b not in keep:
            keep.append(b)
    return "\n\n".join(keep + [result]) if keep else result


async def run_streaming(status_msg, prompt, session_id, channel_key, model="big"):
    """Run `claude -p --output-format stream-json`, editing status_msg with
    progress as events arrive. Returns (reply, new_session_id, err)."""

    # agents: tool_use_id -> {"desc": str, "done": bool}, insertion-ordered.
    state = {"tools": [], "text": "", "result": None, "session_id": session_id,
             "err": None, "agents": {}, "usage": {}, "cost": 0.0, "model_id": None,
             "is_error": False, "api_status": None, "fallback_note": None,
             "ctx": 0, "blocks": []}
    dirty = asyncio.Event()
    done = asyncio.Event()

    card_file = ((paths.RUN / f"discord-live-card-{channel_key}.json")
                 if channel_key else None)

    def render():
        # Card running as a SEPARATE message (it posted its own) → pointer stub.
        # When the card owns THIS message, editor_loop skips edits entirely.
        if card_file is not None and card_file.exists():
            return "📋 _progress in the live card above_ · ❌ here stops the run"
        # Pre-card: minimal card shell, latest activity in the -# line.
        agents = list(state["agents"].values())
        if agents:
            nd = sum(1 for a in agents if a["done"])
            running = next((a["desc"] for a in reversed(agents) if not a["done"]), None)
            act = f"{nd}/{len(agents)} subagents done" + (f" · {running}" if running else "")
        elif state["text"]:
            act = "…" + re.sub(r"[`\n]", " ", state["text"].strip())[-150:]
        elif state["tools"]:
            act = state["tools"][-1]
        else:
            act = "thinking…"
        return precard_stub(f"🤔 {act[:180]}")

    async def editor_loop():
        while not done.is_set():
            try:
                await asyncio.wait_for(dirty.wait(), timeout=EDIT_THROTTLE)
            except asyncio.TimeoutError:
                continue
            dirty.clear()
            # Card took this message over — it's the card tool's canvas now.
            if channel_key and card_owned(channel_key, status_msg.id):
                continue
            try:
                await status_msg.edit(content=render())
            except discord.HTTPException:
                pass
            await asyncio.sleep(EDIT_THROTTLE)

    editor_task = asyncio.create_task(editor_loop())

    # Resolve tier token / pin → registry entry. If the primary is unavailable or
    # overloaded the CLI drops to its registered fallback instead of failing.
    _entry = resolve_model(model)
    _base_cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose",
                 *access.claude_flags(),
                 "--append-system-prompt", context.system_addendum(channel_key)]
    _env = {**access.claude_env(),
            "DISCORD_INBOX": str(inbox_path(channel_key)) if channel_key else "",
            # The channel/thread this turn is running in. For a thread it's the
            # thread's own id, so tool posts (discord-update, the live card) land
            # back in the same thread instead of the main channel. A sibling
            # task's key is "chid~tN" — strip the task suffix for posting, keep
            # the full key for the card tool's per-task state files.
            "DISCORD_CHANNEL_ID": (channel_key or "").split("~")[0],
            "DISCORD_CARD_KEY": channel_key or ""}
    proc = None

    async def reader():
        while True:
            try:
                line = await proc.stdout.readline()
            except (asyncio.LimitOverrunError, ValueError) as e:
                # A single line still exceeded our buffer — drain the rest
                # of that line by reading until the next newline shows up.
                print(f"line overran buffer: {e}", flush=True)
                try:
                    await proc.stdout.readuntil(b"\n")
                except Exception:
                    pass
                continue
            if not line:
                break
            try:
                evt = json.loads(line.decode(errors="replace"))
            except json.JSONDecodeError:
                continue
            t = evt.get("type")
            if t == "system":
                sid = evt.get("session_id")
                if sid:
                    state["session_id"] = sid
            elif t == "assistant":
                # The real model serving the turn (Fable, or the Opus
                # fallback when Fable is unavailable). Last one wins.
                _m = evt.get("message", {}).get("model")
                if _m:
                    state["model_id"] = _m
                # Context size of this step = everything the model read for it.
                _mu = evt.get("message", {}).get("usage") or {}
                _ctx = sum(int(_mu.get(k, 0) or 0) for k in (
                    "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                if _ctx > state["ctx"]:
                    state["ctx"] = _ctx
                for blk in evt.get("message", {}).get("content", []):
                    bt = blk.get("type")
                    if bt == "text":
                        state["text"] += blk.get("text", "")
                        state["blocks"].append(blk.get("text", ""))
                    elif bt == "tool_use":
                        name = blk.get("name", "")
                        inp = blk.get("input", {})
                        if name in AGENT_TOOLS:
                            state["agents"][blk.get("id")] = {
                                "desc": _agent_label(inp), "done": False,
                            }
                        else:
                            state["tools"].append(describe_tool(name, inp))
            elif t == "user":
                # tool_result blocks close out the subagent that produced them
                for blk in evt.get("message", {}).get("content", []):
                    if isinstance(blk, dict) and blk.get("type") == "tool_result":
                        a = state["agents"].get(blk.get("tool_use_id"))
                        if a:
                            a["done"] = True
            elif t == "result":
                state["result"] = (evt.get("result") or "").strip()
                sid = evt.get("session_id")
                if sid:
                    state["session_id"] = sid
                if isinstance(evt.get("usage"), dict):
                    state["usage"] = evt["usage"]
                if evt.get("total_cost_usd") is not None:
                    state["cost"] = evt["total_cost_usd"]
                state["is_error"] = bool(evt.get("is_error"))
                state["api_status"] = evt.get("api_error_status")
            dirty.set()

    async def _launch(model_id, fallback_id):
        """Run ONE claude subprocess to completion, streaming into `state`.
        Shared by the primary run and the limit/credits retry. Returns stderr."""
        nonlocal proc
        cmd = _base_cmd + ["--model", model_id]
        if fallback_id:
            cmd += ["--fallback-model", fallback_id]
        if session_id:
            cmd += ["--resume", session_id]
        # Bump the StreamReader buffer; a single stream-json event can easily
        # exceed the 64 KB default (e.g. a Read tool_result on a large file).
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=CWD,
            limit=64 * 1024 * 1024,  # 64 MB per line
            env=_env,
        )
        if channel_key:
            running_procs[channel_key] = proc
        proc.stdin.write(prompt.encode())
        try:
            await proc.stdin.drain()
        except Exception:
            pass
        proc.stdin.close()
        try:
            await asyncio.wait_for(reader(), timeout=CLAUDE_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            state["err"] = f"timeout after {CLAUDE_TIMEOUT}s"
        await proc.wait()
        stderr_text = (await proc.stderr.read()).decode(errors="replace")
        if proc.returncode != 0 and not state["result"]:
            state["err"] = stderr_text.strip() or f"claude exited {proc.returncode}"
        return stderr_text

    def _rejected(stderr_text):
        """Billing/limit rejection of the PRIMARY model (Fable limit, usage
        credits, 429). Arrives via stderr OR as an is_error result event; a
        normal answer that merely mentions limits never has is_error set."""
        txt = f"{stderr_text}\n{state['result'] or ''}\n{state['err'] or ''}".lower()
        if "usage credits" in stderr_text.lower():
            return True
        if state["is_error"] or (state["err"] and not state["result"]):
            return (state["api_status"] == 429 or "usage credits" in txt
                    or re.search(r"reached your .{0,40}limit|usage limit", txt) is not None)
        return False

    try:
        stderr_text = await _launch(_entry["id"], _entry["fallback"])
        # Retry on the primary's registered fallback (models without one just
        # surface the error). The footer notes why the model changed.
        if _entry["fallback"] and _rejected(stderr_text):
            print(f"{_entry['id']} limit/credits rejection, retrying with "
                  f"{_entry['fallback']}", flush=True)
            state.update({"tools": [], "text": "", "result": None, "err": None,
                          "agents": {}, "usage": {}, "cost": 0.0, "model_id": None,
                          "session_id": session_id, "is_error": False, "api_status": None,
                          "blocks": []})
            await _launch(_entry["fallback"], None)
            state["fallback_note"] = f"{_entry['label']} limit"
    finally:
        done.set()
        dirty.set()
        try:
            await editor_task
        except Exception:
            pass
        if channel_key and running_procs.get(channel_key) is proc:
            running_procs.pop(channel_key, None)

    # Stash the actual model id on the usage dict so callers can label the
    # footer correctly without growing this return tuple. The footer's token
    # math only reads the numeric *_tokens keys, so an extra string key is inert.
    _u_out = state["usage"] if isinstance(state["usage"], dict) else {}
    if state["model_id"]:
        _u_out["model_id"] = state["model_id"]
    if state["ctx"]:
        _u_out["ctx_tokens"] = state["ctx"]
    if state["fallback_note"]:
        _u_out["fallback_note"] = state["fallback_note"]
    if state["agents"]:
        _u_out["helpers"] = len(state["agents"])
    return (_with_lost_text(state["result"] or "", state["blocks"]), state["session_id"],
            state["err"], state["tools"], _u_out, state["cost"])


# ──── REPLY FINALIZATION ─────────────────────────────────────────

def build_summary(reply, tools):
    """One-line summary for the long-reply notification ping. Either a brief
    trail of what tools ran, or (if no tools) a preview of the reply itself."""
    if tools:
        seen = []
        for t in tools:
            if t not in seen:
                seen.append(t)
            if len(seen) >= 3:
                break
        rest = max(0, len(tools) - len(seen))
        line = " · ".join(seen)
        if rest:
            line += f" · +{rest} more"
        return f"✓ {line}\n_Full reply in thread ↓_"
    first_para = (reply or "").strip().split("\n\n")[0]
    if len(first_para) > 200:
        first_para = first_para[:200].rstrip() + "…"
    return f"{first_para}\n\n_Full reply in thread ↓_"


def make_thread_name(hint):
    """Discord thread names cap at 100 chars; trim and clean newlines.
    Fallback title when the summarizer call fails."""
    s = (hint or "").strip().split("\n")[0]
    s = s or "Full answer"
    return s[:90]


TITLE_MODEL = MODELS["haiku"]["id"]  # cheap/fast one-shot: turn the ask into a title

TITLE_PROMPT = """Write a VERY short Discord thread title — a topic label, not a sentence —
summarizing what the user is ASKING FOR (the topic, not your answer).
Rules: 2-4 words, max 40 chars. Title Case. No quotes, no punctuation, no emoji,
no "Re:"/"Thread:" prefixes, no trailing period. Reply with ONLY the title.

Examples:
"can you look at my repos and tell me which ones are out of date" -> Repo Freshness Check
"why does my python script crash when the csv has empty rows" -> CSV Empty Row Crash
"what's the best way to back up postgres" -> Postgres Backup

User message:
"""


async def summarize_thread_title(hint):
    """Fast model call → a short title summarizing the user's request. Any
    failure/timeout falls back to the trimmed first line of the prompt."""
    fallback = make_thread_name(hint)
    text = (hint or "").strip()
    if not text:
        return fallback
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", "--model", TITLE_MODEL, "--tools", "", *HELPER_MCP_FLAGS,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(
            proc.communicate((TITLE_PROMPT + text[:2000]).encode()), timeout=20)
        title = out.decode().strip().split("\n")[0].strip().strip('"').strip("'")
        title = re.sub(r"\s+", " ", title).strip()
        # The CLI prints API errors to stdout with exit code 0-or-1 depending on
        # failure mode; never let an error sentence become the thread name.
        if proc.returncode != 0 or re.search(
                r"(?i)error|too long|api |rate limit|overloaded", title):
            print(f"thread title rejected (rc={proc.returncode}): {title[:80]}",
                  flush=True)
            return fallback
        return title[:90] if title else fallback
    except Exception as e:
        print(f"thread title summary failed, using fallback: {e}", flush=True)
        try:
            if proc:
                proc.kill()
        except Exception:
            pass
        return fallback


async def rename_thread_to_summary(thread, hint):
    """Background task: replace a thread's placeholder name with a short summary
    of the user's request. Created threads start with the trimmed-prompt fallback
    name (instant, no latency on the turn); this swaps in the nicer title once the
    fast model call returns. Best-effort — a failed edit just leaves the fallback."""
    try:
        title = await summarize_thread_title(hint)
        if title and title != thread.name:
            await thread.edit(name=title)
        index_thread(thread.id, title or thread.name, hint)
    except Exception as e:
        print(f"thread rename failed, keeping fallback name: {e}", flush=True)


# ──── THREAD FINDER ──────────────────────────────────────────────
# Conversational "find my thread about X" / "list my threads". Triggered when a
# find/search/list/where verb appears in the same clause as the word "thread(s)".

THREAD_FIND_RE = re.compile(
    r"\b(find|search|locate|look\s+for|which|where(?:'s|s| is| are)?|list|show|"
    r"got any|what)\b[^.?!]*\bthreads?\b", re.I)

# Follow-up patterns: "what about X?", "and X?", "how about X?", "try X"
# Only matched when recent_thread_searches shows a search in the last 90s.
THREAD_FOLLOWUP_RE = re.compile(
    r"^(?:(?:what|how)\s+about|and|try|now\s+(?:search|find)(?:\s+for)?)\s+(.+?)[\?!\.]*\s*$",
    re.I)

# Words to drop when pulling the search topic out of the phrasing.
THREAD_STOPWORDS = {
    "the", "a", "an", "my", "me", "i", "was", "were", "is", "are", "am", "be",
    "about", "on", "for", "regarding", "that", "which", "where", "thread",
    "threads", "find", "search", "locate", "look", "show", "list", "what",
    "with", "to", "do", "of", "in", "and", "or", "please", "can", "could", "you",
    "claude", "we", "our", "us", "related", "discussing", "discuss", "any",
    "earlier", "previous", "old", "archived", "had", "have", "got", "where's",
    "wheres", "there", "from", "back", "when", "some",
    # common filler / function words — too generic to signal a duplicate topic
    "but", "not", "want", "wanted", "like", "way", "next", "just", "this",
    "that's", "thats", "your", "yours", "into", "only", "lets", "let", "dont",
    "cant", "wont", "its", "make", "made", "new", "thing", "things", "stuff",
    "need", "needs", "idea", "ideas", "also", "really", "very", "much", "more",
}


def _thread_query(body):
    """Pull the topic tokens out of a 'find my thread about X' message.
    Returns [] when the user just wants the whole list."""
    s = (body or "").lower()
    m = re.search(
        r"\b(?:about|on|for|regarding|to do with|related to|discussing|"
        r"reviewing|review of|covering|where (?:i|we) (?:was|were))\b\s+(.+)", s)
    tail = m.group(1) if m else s
    toks = re.findall(r"[a-z0-9]+", tail)
    return [t for t in toks if t not in THREAD_STOPWORDS and len(t) > 1]


def _thread_score(name, qtoks):
    """Relevance of a thread name to the query tokens (0 = no match)."""
    n = (name or "").lower()
    ntoks = set(re.findall(r"[a-z0-9]+", n))
    score = 0
    for q in qtoks:
        if q in ntoks:
            score += 3                                   # whole-word hit
        elif any(q in nt or nt in q for nt in ntoks):
            score += 2                                   # stem/substring of a word
        elif q in n:
            score += 1                                   # loose substring
    return score


async def collect_threads(parent):
    """Every thread under the main channel — cached-active, fetched-active and
    archived — deduped by id. Best-effort: a failed source is just skipped."""
    seen = {}
    for t in getattr(parent, "threads", []):
        seen[t.id] = t
    try:
        for t in await parent.guild.active_threads():
            if t.parent_id == parent.id:
                seen[t.id] = t
    except (discord.HTTPException, AttributeError) as e:
        print(f"active_threads fetch failed: {e}", flush=True)
    try:
        async for t in parent.archived_threads(limit=300):
            seen.setdefault(t.id, t)
    except (discord.HTTPException, AttributeError) as e:
        print(f"archived_threads fetch failed: {e}", flush=True)
    return list(seen.values())


def _thread_created_key(t):
    """Sort key for newest-first listing; tolerates a missing timestamp."""
    return t.created_at or datetime.min.replace(tzinfo=timezone.utc)


async def handle_thread_find(message, override_topic=None):
    """Answer a 'find/list my threads' message with clickable thread links.
    With a topic → ranked matches across active + archived; without → newest
    first. Read-only and session-free, so it's safe to run even mid-turn.
    override_topic: pre-extracted topic string (used for follow-up messages)."""
    parent = client.get_channel(ALLOWED_CHANNEL_ID)
    if parent is None:
        try:
            parent = await client.fetch_channel(ALLOWED_CHANNEL_ID)
        except discord.HTTPException:
            parent = None
    if parent is None:
        await message.channel.send("Couldn't reach the main channel to search its threads.")
        return

    qtoks = _thread_query(override_topic if override_topic is not None else (message.content or ""))
    threads = [t for t in await collect_threads(parent) if t.id != message.channel.id]
    if not threads:
        await message.channel.send("No threads under this channel yet.")
        return

    def _line(t):
        tag = " · 🗄️ archived" if getattr(t, "archived", False) else ""
        return f"- <#{t.id}> — {t.name}{tag}"

    if qtoks:
        scored = sorted(((_thread_score(t.name, qtoks), t) for t in threads),
                        key=lambda x: (x[0], _thread_created_key(x[1])), reverse=True)
        hits = [t for s, t in scored if s > 0][:8]
        if not hits:
            await message.channel.send(
                f"No thread names matched **{' '.join(qtoks)}** "
                f"({len(threads)} threads total — say *list threads* to see them all).")
            return
        out = [f"🔎 Threads matching **{' '.join(qtoks)}**:"] + [_line(t) for t in hits]
    else:
        threads.sort(key=_thread_created_key, reverse=True)
        shown = threads[:15]
        out = [f"🧵 Your threads (newest first, {len(shown)} of {len(threads)}):"]
        out += [_line(t) for t in shown]

    for chunk in split_chunks("\n".join(out)):
        await message.channel.send(chunk)

    recent_thread_searches[message.channel.id] = time.time()


# ──── MOVE TO A NEW THREAD (with summarised handover) ────────────
# "let's move to a new thread", "context is getting too long" → summarise this
# thread, open a fresh one in the main channel, and seed it with the handover so
# the new (cold) session keeps the context without carrying the token weight.

MOVE_THREAD_RE = re.compile(
    r"\b(?:move|switch|shift|start|spin\s*up|open|begin|continue|carry\s+on|jump|go)"
    r"\b[^.?!]{0,40}\b(?:new|fresh|another|clean)\s+thread\b"
    r"|\b(?:new|fresh|clean)\s+thread\b[^.?!]{0,20}\b(?:please|pls|now|here)\b"
    r"|\blet'?s\b[^.?!]{0,30}\b(?:new|fresh)\s+thread\b"
    r"|\bcontext\b[^.?!]{0,40}\b(?:too\s+|getting\s+|building\s+)?(?:long|high|big|large|heavy|full|deep)\b",
    re.I)

HANDOVER_SUMMARY_PROMPT = """You are writing a CONTEXT HANDOVER so a fresh chat thread can continue this
conversation seamlessly. Summarize the transcript below into a tight brief.
Cover: the goal/topic, key decisions, what's done, what's still open or in
progress, and any exact identifiers that matter (files, paths, hosts, repos,
IDs). Use short bullets under a few bold headers. Be concrete
and preserve exact identifiers. Invent nothing that isn't in the transcript.
Keep it under 1200 characters. Reply with ONLY the brief.

Transcript:
"""


async def _thread_transcript(channel, limit=120):
    """Oldest-first transcript of a thread for summarising. Keeps more text per
    message than thread_history_block since this feeds a summary, not a prompt."""
    msgs = []
    try:
        async for m in channel.history(limit=limit):
            msgs.append(m)
    except Exception as e:
        print(f"transcript fetch failed: {e}", flush=True)
        return ""
    msgs.reverse()
    lines = []
    for m in msgs:
        body = (m.content or "").strip()
        if not body:
            if not m.attachments:
                continue
            body = f"[{len(m.attachments)} attachment(s)]"
        author = "Claude" if m.author == client.user else m.author.display_name
        lines.append(f"{author}: {body}")
    return "\n".join(lines)


async def summarize_handover(transcript):
    """Sonnet call → a compact context brief. None on empty input or failure."""
    if not (transcript or "").strip():
        return None
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", "--model", FAST_MODEL, "--tools", "", *HELPER_MCP_FLAGS,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(
            proc.communicate((HANDOVER_SUMMARY_PROMPT + transcript[:24000]).encode()),
            timeout=90)
        return out.decode().strip() or None
    except Exception as e:
        print(f"handover summary failed: {e}", flush=True)
        try:
            if proc:
                proc.kill()
        except Exception:
            pass
        return None


async def handle_move_thread(message):
    """Summarise the current thread, open a fresh thread in the main channel, seed
    it with the handover, and cross-link both ways. The new thread's id is a new
    session key, so it starts cold — the seeded handover (read back via
    thread_history_block) is what carries the context across."""
    old = message.channel
    if not isinstance(old, discord.Thread):
        await old.send("You're not in a thread yet — just message the main channel and I'll start one.")
        return
    parent = client.get_channel(old.parent_id)
    if parent is None:
        try:
            parent = await client.fetch_channel(old.parent_id)
        except discord.HTTPException:
            parent = None
    if parent is None:
        await old.send("Couldn't reach the main channel to open a new thread.")
        return

    notice = await old.send("📦 Summarising this thread and spinning up a fresh one…")
    transcript = await _thread_transcript(old)
    summary = await summarize_handover(transcript)

    # Name the continuation from a SHORT summary of the conversation, not the old
    # thread's name — that name may predate the auto-rename fix and be a long
    # truncated prompt. This keeps continuation threads short and relevant too.
    title = await summarize_thread_title(summary or transcript[:2000] or old.name)
    title = re.sub(r"\s*\(cont\.?(?:\s*\d+)?\)\s*$", "", title or "").strip()
    base = title or "Conversation"
    new_name = f"{base} (cont.)"[:100]
    try:
        new = await parent.create_thread(
            name=new_name, type=discord.ChannelType.public_thread,
            auto_archive_duration=1440)
    except (discord.HTTPException, AttributeError) as e:
        await notice.edit(content=f"Couldn't create the new thread: {e}")
        return

    if summary:
        head = f"{HANDOVER_MARKER}** — continued from <#{old.id}>\n\n{summary}"
    else:
        head = (f"{HANDOVER_MARKER}** — continued from <#{old.id}>\n\n"
                "_(Summary unavailable — the previous thread has the full history.)_")
    for chunk in split_chunks(head):
        await new.send(chunk)
    try:
        await new.send("-# Fresh session — the handover above is your context. Carry on here.")
    except discord.HTTPException:
        pass

    index_thread(new.id, new_name, summary or transcript[:500])
    try:
        await notice.edit(
            content=f"📦 Moved to <#{new.id}> — context summarised there. Continue in the new thread ↪️")
    except discord.HTTPException:
        await old.send(f"📦 Moved to <#{new.id}>.")


async def _backfill_thread_index():
    """Seed the index from existing Discord threads (names only) so the finder
    works against threads created before the index existed. One-time per boot."""
    await client.wait_until_ready()
    parent = client.get_channel(ALLOWED_CHANNEL_ID)
    if parent is None:
        return
    try:
        threads = await collect_threads(parent)
    except Exception as e:
        print(f"thread index backfill failed: {e}", flush=True)
        return
    added = 0
    for t in threads:
        if str(t.id) not in thread_index:
            thread_index[str(t.id)] = {"name": t.name, "summary": "", "ts": 0}
            added += 1
    if added:
        save_thread_index()
    print(f"thread index backfill: +{added} ({len(thread_index)} total)", flush=True)


def _clean_for_ping(text):
    """First meaningful line of the reply, markdown/fences stripped — the
    headline that goes in the notification."""
    for raw in (text or "").split("\n"):
        line = raw.strip()
        if not line or line.startswith("-#") or line.startswith("```"):
            continue
        line = re.sub(r"[*_`>#~\[\]()]", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            return line
    return (text or "").strip()


# ──── ONE-TAP ASK REACTIONS ──────────────────────────────────────
# When a reply asks the owner to "react 👍 / react with 🗑️ or ✅", pre-seed those
# emojis on the delivered message so they tap instead of opening the picker, and
# remember them so the tap itself starts the follow-up turn (no typed message
# needed). In-memory like last_prompts — a restart just clears pending asks.

ask_reactions = {}   # reply message_id -> {emojis, session_key, mention, excerpt}

_ASK_LINE_RE = re.compile(r"\breact", re.I)
_EMOJI_SCAN_RE = re.compile(
    "<a?:\\w+:\\d+>|["                      # custom Discord emoji, or unicode:
    "\U0001F1E6-\U0001F1FF"                 # regional indicators
    "\U0001F300-\U0001F5FF\U0001F600-\U0001F64F\U0001F680-\U0001F6FF"
    "\U0001F900-\U0001F9FF\U0001FA70-\U0001FAFF"
    "⌀-⏿☀-➿⬀-⯿⁉‼❤"
    "]\ufe0f?")                             # optional variation selector
# Emojis with a global meaning on reply messages — never harvest as an ask.
_RESERVED_REACTS = {"❌", "\U0001F504", "\U0001F440", "\U0001F9F5",
                    "\U0001F3D7"}           # ❌ 🔄 👀 🧵 🏗


def harvest_ask_reactions(text):
    """Emojis the reply asks the owner to react with — pulled from any line that
    contains a 'react' verb, deduped in order, capped at 5."""
    out = []
    for line in (text or "").split("\n"):
        if not _ASK_LINE_RE.search(line):
            continue
        for m in _EMOJI_SCAN_RE.finditer(line):
            e = m.group(0)
            if e.replace("\ufe0f", "") in _RESERVED_REACTS:
                continue
            if e not in out:
                out.append(e)
    return out[:5]


# ──── OPTION BUTTONS ─────────────────────────────────────────────
# A reply can end with <<<BUTTONS: opt | opt | ...>>>: the options render as
# clickable buttons under the delivered message and a click IS the owner's answer
# (it starts the follow-up turn on the same session, like a reaction-ask tap).
# Typing instead always works — any new message in the channel retires the live
# buttons. On timeout they grey out in place. In-memory like ask_reactions: a
# bot restart leaves the buttons visible but dead, and typing still works.

option_views = {}      # channel_id(str) -> (message, view) with buttons still live
BUTTONS_TIMEOUT = 15 * 60


class OptionView(discord.ui.View):
    def __init__(self, options, session_key, mention, excerpt, reg_keys=None):
        super().__init__(timeout=BUTTONS_TIMEOUT)
        self.message = None
        # Registry keys this view lives under (the originating channel, plus the
        # thread the buttons message landed in when those differ).
        self.reg_keys = reg_keys or []
        self.session_key, self.mention, self.excerpt = session_key, mention, excerpt
        for i, label in enumerate(options):
            self.add_item(self._make_button(i, label))

    def _make_button(self, i, label):
        btn = discord.ui.Button(label=label[:80],
                                style=discord.ButtonStyle.secondary, row=i // 5)

        async def _cb(interaction, _label=label, _btn=btn):
            for c in self.children:
                c.disabled = True
            _btn.style = discord.ButtonStyle.success   # the picked one stays green
            self.stop()
            for k in (self.reg_keys or [str(interaction.channel_id)]):
                option_views.pop(k, None)
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.edit_message(view=self)
            note = (f"[{OWNER} clicked the '{_label}' button on your question — treat "
                    f"that as their answer and proceed now.]\n"
                    f"[that message began: {self.excerpt}]")
            # busy/steer tracking is keyed by the ORIGINATING channel, which may
            # differ from where the buttons message lives (long replies put the
            # buttons at the end of the answer thread).
            akey = (self.reg_keys or [str(interaction.channel_id)])[0]
            if busy_channels.get(akey):
                # Mid-turn click: deliver through the steer inbox like a typed message.
                with open(inbox_path(akey), "a") as f:
                    f.write(json.dumps({"ts": time.time(),
                                        "author": interaction.user.display_name,
                                        "content": note, "status": "pending"}) + "\n")
                return
            _spawn(run_pipeline(interaction.channel, note, self.session_key,
                                mention=self.mention,
                                thread_hint=f"button answer: {_label[:60]}"))

        btn.callback = _cb
        return btn

    async def interaction_check(self, interaction):
        return access.allowed(interaction.user.id)

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        if self.message:
            for k in (self.reg_keys or [str(self.message.channel.id)]):
                option_views.pop(k, None)
            with contextlib.suppress(discord.HTTPException):
                await self.message.edit(view=self)


async def retire_option_view(channel_id):
    """The owner answered by typing (or a newer question superseded this one) —
    grey out the channel's live buttons without firing anything."""
    entry = option_views.pop(str(channel_id), None)
    if not entry:
        return
    msg, view = entry
    for k in getattr(view, "reg_keys", []):
        option_views.pop(k, None)
    view.stop()
    for c in view.children:
        c.disabled = True
    with contextlib.suppress(discord.HTTPException):
        await msg.edit(view=view)


def build_main_gist(reply, mention, is_error=False):
    """The single short channel message for a long reply: it @mentions (so it
    pings), carries a one-line gist (the reply's lead/TL;DR line), and points to
    the thread that holds the full answer. This is the only message left in the
    channel for the turn, so its notification preview stays short and clean."""
    m = (mention + " ") if mention else ""
    mlen = RENDER_MENTION if mention else 0
    icon = "⚠️ " if is_error else "✅ "
    tail = " — **Full answer in thread ↓**"
    budget = PING_LIMIT - mlen - len(icon) - len(tail)
    gist = _clean_for_ping(reply)
    if len(gist) > budget:
        gist = gist[:budget - 1].rstrip() + "…"
    return m + icon + gist + tail


async def finalize(status_msg, channel, reply, reply_to=None, mention=None, route=None,
                   tools=None, thread_hint=None, files=None, track_key=None):
    """Deliver the turn as a single channel message:

    • Reply fits one message → the channel gets ONE fresh @mention message that
      both pings and carries the whole answer. No board, no thread.
    • Reply too long → the channel gets ONE short @mention message of the form
      "@owner ✅ <one-line gist> — Full answer in thread ↓"; the complete answer,
      chunked, lives in a thread hanging off that message. The gist is the
      reply's lead/TL;DR line, so the notification preview stays short and clean.

    Either way the channel ends up with exactly one bot message and it's the one
    that notifies — no repeated headline. Returns (primary_msg, tail_dest):
    primary_msg is the message users react to (🔄 redo / ❌); tail_dest is the
    channel/thread where the LAST content chunk landed when the reply was
    chunked (so option buttons can follow it), or None for a single-message
    reply (buttons attach to the message itself — it already IS the end)."""
    reply = (reply or "(no output)").strip()
    tools = tools or []
    is_error = reply.startswith("Error:")
    m = (mention + " ") if mention else ""

    # Build discord.File objects for any attachments (max 10, skip missing/oversized).
    # File handles are consumed on send, so these are attached to the PRIMARY message
    # only (the one that pings) and built exactly once.
    file_objs = []
    for p in (files or [])[:10]:
        try:
            if os.path.exists(p) and os.path.getsize(p) <= 24 * 1024 * 1024:
                file_objs.append(discord.File(p))
            else:
                print(f"[file] skipped (missing or >24MB): {p}", flush=True)
        except OSError as e:
            print(f"[file] open failed {p}: {e}", flush=True)

    async def send_fresh(content, with_files=False):
        kw = {"files": file_objs} if (with_files and file_objs) else {}
        try:
            if reply_to:
                return await reply_to.reply(content=content, **kw)
            return await channel.send(content=content, **kw)
        except discord.HTTPException as e:
            # A rejected attachment (413 over the server's 10MB upload cap etc.)
            # must never take the reply text down with it.
            # Resend text-only, then flag the failed files.
            if not kw.get("files"):
                raise
            print(f"[file] attach failed ({e}); resending text-only", flush=True)
            msg = (await reply_to.reply(content=content) if reply_to
                   else await channel.send(content=content))
            with contextlib.suppress(discord.HTTPException):
                await channel.send(
                    f"⚠️ Attachment upload failed ({e.status}) — likely over this "
                    "server's 10MB/file cap. Still on disk:\n"
                    + "\n".join(f"`{p}`" for p in (files or [])[:10]))
            return msg

    # CASE 1 — the whole answer fits in ONE Discord message: one fresh message
    # both pings and carries the full answer. No board, no thread. Only answers
    # too long for a single message fall through to the thread path below.
    if (RENDER_MENTION if mention else 0) + len(reply) <= DISCORD_SAFE_LIMIT:
        await retire_status(status_msg, track_key)
        return await send_fresh(m + reply, with_files=True), None

    # Longer answer: the full reply, chunked, goes into a thread; the channel
    # keeps only a short pinging message that points at it. Drop the silent
    # "Thinking" board and post fresh so the new message actually notifies.
    header = f"-# {route}\n" if route else ""
    chunks = split_chunks(reply, limit=DISCORD_SAFE_LIMIT - len(header))
    chunks[0] = header + chunks[0]
    await retire_status(status_msg, track_key)

    # CASE 2a — already inside a thread: there's no separate channel to keep
    # short, so the full answer goes right here. The @mention on the first chunk
    # is the single notification; nothing is repeated.
    if isinstance(channel, discord.Thread):
        first = await send_fresh(m + chunks[0], with_files=True)
        for c in chunks[1:]:
            await channel.send(c)
        return first, channel

    # CASE 2b — top-level channel: one short pinging message, full answer in a
    # thread hanging off it.
    main_msg = await send_fresh(build_main_gist(reply, mention, is_error), with_files=True)
    tail_dest = channel
    try:
        thread = await main_msg.create_thread(
            name=make_thread_name(thread_hint), auto_archive_duration=1440)
        _spawn(rename_thread_to_summary(thread, thread_hint))
        for c in chunks:
            await thread.send(c)
        tail_dest = thread
    except (discord.HTTPException, AttributeError) as e:
        print(f"thread creation failed, posting chunks inline: {e}", flush=True)
        for c in chunks:
            await channel.send(c)
    return main_msg, tail_dest


# ──── PIPELINE ───────────────────────────────────────────────────

async def run_pipeline(channel, prompt, session_key, reply_to=None,
                       persist_session=True, remember_for_redo=True, mention=None,
                       thread_hint=None, speed=None, route=None, meta=None,
                       model_pin=None, task_id=None, fork_session=False,
                       resume_sid=None):
    """Send placeholder → stream → finalize. Returns the primary reply message.

    fork_session=True runs this turn as a parallel SIBLING task: it resumes the
    thread's session (forking it) but never saves its session id back, runs
    under a task-scoped track key ("chid~tN") so its inbox/proc/kill-flag/card
    never clash with the primary turn, and on finish leaves a parallel-note the
    canonical session picks up next turn. resume_sid overrides the session
    lookup (a sibling's own follow-up turns keep its forked context)."""
    _turn_t0 = time.monotonic()
    mark_activity()
    _escalated = False
    _outcome = DONE_OK  # terminal reaction to leave on reply_to when the turn ends
    ckey = str(channel.id)
    tid = task_id or _next_task_id(ckey)
    track_key = f"{ckey}~t{tid}" if fork_session else ckey
    card_turn_reset(track_key)
    if isinstance(channel, discord.Thread):
        context.threads.add(ckey)          # gets a project brief (botcore/context.py)
    if reply_to:
        status = await reply_to.reply(precard_stub())
    else:
        status = await channel.send(precard_stub())
    card_init_write(track_key, status.id)
    if not fork_session:
        busy_channels[track_key] = status
        # Canonical turns hear about siblings that finished since the last
        # turn (their forked sessions are discarded — this is the hand-off).
        _pnotes = pop_parallel_notes(ckey)
        if _pnotes:
            prompt = ("[FYI — parallel task(s) that ran alongside earlier turns "
                      "in this thread have finished. Results, for context only "
                      "(do NOT redo them):\n"
                      + "\n".join(f"- {n['label']}: {n['summary']}" for n in _pnotes)
                      + "]\n\n" + prompt)
    thread_tasks.setdefault(ckey, {})[tid] = {
        "track_key": track_key, "status_msg": status,
        "label": (thread_hint or prompt).strip()[:150], "ts": time.time()}
    defer = None
    try:
        # Never resume a session that's being compacted in the background.
        await context.wait_compaction(session_key)
        session_id = resume_sid or (
            sessions.get(session_key) if (session_key and persist_session) else None)
        # model_pin (an explicit /model choice: tier token or registry key) wins over
        # the speed-derived default. The auto fast tiers (Sonnet OR Haiku, picked
        # by the heuristic) get FAST_PREFIX so they can self-escalate to the big
        # model; a deliberate pin runs the chosen model as-is.
        model = model_pin or (speed if speed in ("fast", "haiku") else "big")
        _auto_fast = (model in ("fast", "haiku")) and not model_pin
        _saver_note = None
        if not model_pin:
            # Quota savers (botcore/saver.py). Order matters: sticky-big first,
            # then saver mode may still step a big turn down to Sonnet.
            if model == "fast" and saver.sticky_big(track_key):
                model, _auto_fast = "big", False
                if route:
                    route += " · 📌 sticky big (last turn needed it)"
            if model in ("big", "fast"):
                # In saver mode auto turns run on Sonnet with no escalation hatch
                # (an escalation would just spend the Opus quota we're saving).
                _sv_on, _sv_why = await asyncio.to_thread(saver.saver_state)
                if _sv_on:
                    model, _auto_fast = "sonnet", False
                    _saver_note = f"saver mode, {_sv_why}"
                    if route:
                        route += f" · 💡 saver ({_sv_why})"
            # Escalating from Sonnet to a "big" that IS Sonnet (BIG_MODEL=sonnet)
            # would just re-run the same model: skip the fast-lane round trip.
            if _auto_fast and model == "fast" and resolve_model("fast")["id"] == resolve_model("big")["id"]:
                _auto_fast = False
        sent_prompt = (FAST_PREFIX + prompt) if _auto_fast else prompt

        # Accumulate token spend across every run_streaming call this turn (the
        # first pass plus any session-expiry retry or fast-lane escalation), so
        # the footer reflects the turn's TRUE cost — escalation included.
        _usage_tot = {}
        _cost_tot = 0.0
        _actual_id = None  # real model id from the LAST run (primary vs its fallback)
        _fb_note = None    # "Fable 5.1 limit" when a rejection forced the fallback
        _ctx_max = 0       # largest single-step context this turn (auto-compact trigger)

        def _acc(u, c):
            nonlocal _cost_tot, _actual_id, _fb_note, _ctx_max
            if isinstance(u, dict):
                if u.get("fallback_note"):
                    _fb_note = u["fallback_note"]
                _ctx_max = max(_ctx_max, int(u.get("ctx_tokens", 0) or 0))
                for k, v in u.items():
                    if k == "ctx_tokens":
                        continue
                    if isinstance(v, (int, float)):
                        _usage_tot[k] = _usage_tot.get(k, 0) + v
                if u.get("model_id"):
                    _actual_id = u["model_id"]
            _cost_tot += (c or 0.0)

        reply, new_sid, err, tools, _u, _c = await run_streaming(status, sent_prompt, session_id, track_key, model=model)
        _acc(_u, _c)

        if track_key in killed_channels:
            # ❌ / /cancel — do NOT retry the prompt, discard queued steers.
            _outcome = DONE_STOPPED
            killed_channels.discard(track_key)
            if meta:
                log_route(**meta, model=model, escalated=_escalated,
                          turn_s=round(time.monotonic() - _turn_t0, 1), outcome="cancelled")
            drain_inbox(track_key)
            await retire_status(status, track_key)
            return None

        # Empty reply + we had a session → it probably expired. The big model and
        # any deliberate pin restart fresh on the SAME model. The auto fast/haiku
        # tiers skip this and let the escalation hatch below handle empties.
        if not reply and session_id and not _auto_fast:
            if not card_owned(track_key, status.id):
                with contextlib.suppress(discord.HTTPException):
                    await status.edit(content=precard_stub(
                        "🤔 session expired, starting fresh…"))
            sessions.pop(session_key, None)
            save_sessions(sessions)
            reply, new_sid, err, tools, _u, _c = await run_streaming(status, prompt, None, track_key, model=model)
            _acc(_u, _c)

        # Fast-lane escape hatch: an auto fast/haiku turn bailed (<<<ESCALATE>>>),
        # came back empty, or hit a too-long prompt — transparently re-run the SAME
        # turn on the big model against the original session. Triggers for the AUTO
        # tiers only; a deliberate /model pin is respected as-is (marker still
        # stripped below so it can never leak to Discord).
        _fast_broke = (not reply or "<<<ESCALATE>>>" in (reply or "")
                       or "prompt is too long" in (reply or "").lower()
                       or "prompt is too long" in (err or "").lower())
        if _auto_fast and _fast_broke:
            if not card_owned(track_key, status.id):
                with contextlib.suppress(discord.HTTPException):
                    await status.edit(content=precard_stub(
                        "🔼 escalating to the big model…"))
            reply, new_sid, err, tools, _u, _c = await run_streaming(status, prompt, session_id, track_key)
            _acc(_u, _c)
            if route:
                route += f" → 🔼 escalated to {MODELS[DEFAULT_BIG]['label']}"
            _escalated = True
            if not reply and session_id:
                sessions.pop(session_key, None)
                save_sessions(sessions)
                reply, new_sid, err, tools, _u, _c = await run_streaming(status, prompt, None, track_key)
                _acc(_u, _c)

        # Safety net: the <<<ESCALATE>>> hand-off marker must NEVER reach Discord,
        # even if escalation was skipped (deliberate pin) or the re-run still
        # echoed it. Strip every occurrence before the reply is finalized.
        _marker_only = False
        if reply and "<<<ESCALATE>>>" in reply:
            _stripped = reply.replace("<<<ESCALATE>>>", "").strip()
            _marker_only = not _stripped  # the marker WAS the whole reply
            reply = _stripped

        # Recovery: the model echoed *only* the marker (common once a thread's
        # context is saturated with escalation talk — even the big model mimics
        # it) and the escalation block didn't already handle it. Re-run once on
        # the big model in a FRESH session so it answers instead of erroring.
        if _marker_only and not _escalated:
            if not card_owned(track_key, status.id):
                with contextlib.suppress(discord.HTTPException):
                    await status.edit(content=precard_stub(
                        "🔼 retrying on the big model…"))
            sessions.pop(session_key, None)
            save_sessions(sessions)
            reply, new_sid, err, tools, _u, _c = await run_streaming(status, prompt, None, track_key)
            _acc(_u, _c)
            _escalated = True
            if reply and "<<<ESCALATE>>>" in reply:
                reply = reply.replace("<<<ESCALATE>>>", "").strip()

        if persist_session and session_key and new_sid and not fork_session:
            sessions[session_key] = new_sid
            save_sessions(sessions)

        if not reply:
            reply = f"Error: {err or 'no output'}"
        if reply.startswith("Error:"):
            _outcome = DONE_ERROR

        # Steer hook may have told the model to defer a separate mid-turn task.
        m = re.search(r"<<<DEFER:(.*?)>>>", reply, re.S)
        if m:
            defer = m.group(1).strip()
            reply = reply.replace(m.group(0), "").strip() or "(done)"

        # <<<FILE:/abs/path>>> markers: upload the referenced file(s) as real Discord
        # attachments. The markers are stripped so they never render in the message.
        _files = []
        for _fm in re.finditer(r"<<<FILE:(.*?)>>>", reply):
            _p = _fm.group(1).strip()
            if _p and os.path.isfile(_p):
                _files.append(_p)
            else:
                print(f"[file] marker path missing/invalid: {_p!r}", flush=True)
        if re.search(r"<<<FILE:.*?>>>", reply):
            reply = re.sub(r"<<<FILE:.*?>>>", "", reply).strip()
        # Markdown tables don't render on Discord: small ones become aligned
        # (colour) code blocks, wide ones a PNG attachment (botcore/tables.py).
        try:
            reply, _tbl_files = await asyncio.to_thread(tables.process, reply)
            _files += _tbl_files
        except Exception as e:
            print(f"[tables] {e!r}", flush=True)
        if _files and not reply:
            reply = "📎 " + ", ".join(os.path.basename(p) for p in _files)

        # <<<BUTTONS: a | b | c>>> markers: render the options as clickable
        # buttons under the delivered message (stripped from the text).
        _buttons = None
        _bm = re.search(r"<<<BUTTONS:(.*?)>>>", reply, re.S)
        if _bm:
            _buttons = [o.strip() for o in _bm.group(1).split("|") if o.strip()][:10]
            reply = reply.replace(_bm.group(0), "").strip() or "Pick one:"
            if len(_buttons) < 2:
                _buttons = None   # a one-option "choice" is just a statement

        # Sibling hand-off: record the outcome (pre-footer) so the thread's
        # canonical session hears about it on its next turn.
        if fork_session and not reply.startswith("Error:"):
            append_parallel_note(ckey, thread_hint or prompt, reply)

        _turn_s = round(time.monotonic() - _turn_t0, 1)
        if meta:
            log_route(**meta, model=("big" if _escalated else model),
                      escalated=_escalated, turn_s=_turn_s,
                      outcome="error" if reply.startswith("Error:") else "ok")
        log_activity(thread_hint or prompt, channel.id,
                     "error" if reply.startswith("Error:") else "ok", turn_s=_turn_s)

        # Per-reply usage footer (small text): which model answered + the turn's
        # token spend. Also mirrored to the journal for `journalctl` visibility.
        _eff_model = "big" if _escalated else model
        saver.mark_tier(track_key, _eff_model == "big")
        _footer = usage_footer(_eff_model, _usage_tot, _cost_tot,
                               escalated=_escalated, actual_id=_actual_id,
                               note=_fb_note or _saver_note)
        # Heavy-thread hint only when auto-compact can't fix it: compaction is off
        # on this bot, or the first turn after a compaction is still heavy.
        _cnote = context.pop_note(session_key) if session_key else None
        _hint = ""
        if (not context.auto_compact_on()
                or (_cnote and _ctx_max >= context.compact_at() * 0.6)):
            _hint = saver.heavy_hint(track_key, int(_usage_tot.get("cache_read_input_tokens", 0) or 0))
        if _footer and _hint:
            _footer += "\n" + _hint
        if _footer and _cnote:
            _footer += "\n" + _cnote
        _hmode = helpers.mode(track_key)
        _nhelp = int(_usage_tot.get("helpers", 0) or 0)
        _hline = helpers.footer(_hmode, _nhelp, _turn_s)
        if _footer and _hline:
            _footer += "\n" + _hline
        if not reply.startswith("Error:"):
            helpers.record(track_key, _hmode, _turn_s, _nhelp)
        print(f"[usage] ch={channel.id} requested={_eff_model} actual={_actual_id} "
              f"escalated={_escalated} turn_s={_turn_s} "
              f"usage={_usage_tot} cost=${_cost_tot:.4f}", flush=True)
        if _footer and not reply.startswith("Error:"):
            reply = f"{reply}\n\n{_footer}"

        # Long thread? Compact its session in the background now that the reply
        # is out; the next turn here waits for it (context.wait_compaction).
        if persist_session and not fork_session and session_key and new_sid:
            context.maybe_compact(session_key, new_sid, _ctx_max, CWD, access.claude_env(), _spawn)

        final_msg, _tail_dest = await finalize(status, channel, reply, reply_to=reply_to,
                                    mention=mention, tools=tools, thread_hint=thread_hint,
                                    files=_files, track_key=track_key)

        if remember_for_redo and final_msg:
            last_prompts[final_msg.id] = (
                prompt, channel.id, reply_to.id if reply_to else None, mention,
            )

        # One-tap asks: if the reply asks the owner to react with specific emoji(s),
        # pre-seed them on the delivered message and register the ask so the tap
        # itself triggers the follow-up turn.
        if final_msg and not reply.startswith("Error:"):
            _asks = harvest_ask_reactions(reply)
            if _asks:
                for _emo in _asks:
                    with contextlib.suppress(discord.HTTPException):
                        await final_msg.add_reaction(_emo)
                ask_reactions[final_msg.id] = {
                    "emojis": _asks, "session_key": session_key,
                    "mention": mention, "excerpt": _clean_for_ping(reply)[:300]}

        # Attach clickable option buttons (one live set per channel — a newer
        # question supersedes the old one's buttons). A single-message reply
        # gets the buttons on the message itself; a chunked reply gets them as
        # a separate trailing message AFTER the last chunk, so they always sit
        # at the end of the answer instead of stranded mid-content.
        if final_msg and _buttons and not reply.startswith("Error:"):
            await retire_option_view(channel.id)
            _keys = [str(channel.id)]
            if _tail_dest is not None and str(_tail_dest.id) not in _keys:
                _keys.append(str(_tail_dest.id))
            _view = OptionView(_buttons, session_key, mention,
                               _clean_for_ping(reply)[:300], reg_keys=_keys)
            try:
                if _tail_dest is not None:
                    btn_msg = await _tail_dest.send("-# pick one ⬇️", view=_view)
                else:
                    btn_msg = final_msg
                    await final_msg.edit(view=_view)
                _view.message = btn_msg
                for _k in _keys:
                    option_views[_k] = (btn_msg, _view)
            except discord.HTTPException as e:
                print(f"[buttons] attach failed: {e}", flush=True)
    finally:
        if not fork_session:
            busy_channels.pop(track_key, None)
        _d = thread_tasks.get(ckey)
        if _d:
            _d.pop(tid, None)
        if not _d:
            thread_tasks.pop(ckey, None)
            task_counters.pop(ckey, None)  # thread idle → task numbering resets
        # Close the emoji lifecycle on the message that started this turn.
        await resolve_reactions(reply_to, _outcome)

    # Follow-up turns AFTER this task's slot is free:
    # 1) a task the model explicitly deferred mid-turn, 2) messages that arrived
    # too late for the hook to deliver (no tool call left in the turn).
    # A sibling's follow-ups stay sibling-scoped: fork flag kept, and its own
    # forked session id (never persisted) is resumed explicitly so its context
    # carries over.
    _fw = dict(fork_session=fork_session,
               resume_sid=(new_sid if fork_session else None)) if fork_session else {}
    if defer:
        await run_pipeline(channel, "[Deferred task from your previous turn — do it now]\n"
                           + defer, session_key, mention=mention, thread_hint=defer, **_fw)
    leftover = drain_inbox(track_key)
    if leftover:
        combo = "\n".join(f"- {c}" for c in leftover)
        await run_pipeline(channel,
                           f"[These messages from {OWNER} arrived while you were busy and were "
                           "NOT yet handled — handle them now in one coherent reply]\n" + combo,
                           session_key, mention=mention, thread_hint=leftover[0], **_fw)
    return final_msg


# ──── SLASH COMMANDS ─────────────────────────────────────────────

@tree.command(name="status", description="Bot status: running tasks, routing today")
async def status_cmd(interaction: discord.Interaction):
    running = sum(len(v) for v in thread_tasks.values())
    msg = (f"**Running tasks**: {running}\n"
           f"**Routing today**: {routing_summary()}\n"
           f"**Auto-routing**: {'on' if dispatcher_enabled() else 'OFF'}\n"
           f"**Access**: {access.mode()} mode")
    await interaction.response.send_message(msg)


async def _slash_prompt(interaction, prompt, ultracode=False):
    await interaction.response.send_message(
        "🤖 _Spawning workflow…_" if ultracode else "🤔 _Thinking…_"
    )
    status = await interaction.original_response()
    who = f"[discord user: {interaction.user.display_name} (@{interaction.user.name})]"
    pieces = [who, "", prompt]
    if ultracode:
        pieces.extend(["", ULTRA_SUFFIX])
    if wants_file(prompt):
        pieces.extend(["", FILE_HINT])
    pieces.extend(["", BUTTONS_HINT])
    full = "\n".join(pieces)
    reply, _, err, tools, _u, _c = await run_streaming(status, full, None, str(interaction.channel.id))
    if not reply:
        reply = f"Error: {err or 'no output'}"
    if reply and "<<<ESCALATE>>>" in reply:
        reply = reply.replace("<<<ESCALATE>>>", "").strip()
    _files = []
    for _fm in re.finditer(r"<<<FILE:(.*?)>>>", reply):
        _p = _fm.group(1).strip()
        if _p and os.path.isfile(_p):
            _files.append(_p)
    if re.search(r"<<<FILE:.*?>>>", reply):
        reply = re.sub(r"<<<FILE:.*?>>>", "", reply).strip()
    if _files and not reply:
        reply = "📎 " + ", ".join(os.path.basename(p) for p in _files)
    _footer = usage_footer("big", _u, _c, actual_id=(_u or {}).get("model_id"),
                           note=(_u or {}).get("fallback_note"))
    if _footer and not reply.startswith("Error:"):
        reply = f"{reply}\n\n{_footer}"
    mention = interaction.user.mention
    msg, _ = await finalize(status, interaction.channel, reply, mention=mention,
                          tools=tools, thread_hint=prompt, files=_files)
    if msg:
        last_prompts[msg.id] = (full, interaction.channel.id, None, mention)


@tree.command(name="ask", description="Ask Claude one-off (no conversation memory)")
@app_commands.describe(prompt="What do you want Claude to do?")
async def ask_cmd(interaction: discord.Interaction, prompt: str):
    await _slash_prompt(interaction, prompt, ultracode=False)


@tree.command(name="ultra", description="Multi-agent / workflow mode — slower, more thorough")
@app_commands.describe(prompt="What do you want Claude to do?")
async def ultra_cmd(interaction: discord.Interaction, prompt: str):
    await _slash_prompt(interaction, prompt, ultracode=True)


@tree.command(name="reset", description="Start a fresh Claude conversation in this channel")
async def reset_cmd(interaction: discord.Interaction):
    key = str(interaction.channel.id)
    if key in sessions:
        del sessions[key]
        save_sessions(sessions)
        await interaction.response.send_message(
            "Conversation reset. Next message starts a new session."
        )
    else:
        await interaction.response.send_message("No active session in this channel.")


@tree.command(name="cancel", description="Kill the running Claude run(s) in this channel")
async def cancel_cmd(interaction: discord.Interaction):
    key = str(interaction.channel.id)
    # Primary run + any parallel sibling tasks (track keys "key~tN").
    n = 0
    for k in [k for k in running_procs if k == key or k.startswith(key + "~")]:
        killed_channels.add(k)  # suppress the auto-retry of the cancelled prompt
        try:
            running_procs[k].kill()
        except Exception:
            pass
        running_procs.pop(k, None)
        n += 1
    if not n:
        await interaction.response.send_message("Nothing running in this channel.")
        return
    await interaction.response.send_message(
        f"✋ Cancelled {n} run(s)." if n > 1 else "✋ Cancelled.")


@tree.command(name="cancel-all", description="Kill ALL running Claude runs everywhere")
async def cancel_all_cmd(interaction: discord.Interaction):
    n = 0
    for k, proc in list(running_procs.items()):
        killed_channels.add(k)
        try:
            proc.kill()
        except Exception:
            pass
        running_procs.pop(k, None)
        n += 1
    await interaction.response.send_message(
        f"✋ Cancelled {n} running run(s)." if n else "Nothing running anywhere.")


# Pin values -> (display label, run_streaming model token). "haiku" is a
# testing-phase option reachable only here, never from the auto-router.
@tree.command(name="brief", description="Show this thread's project brief (Claude keeps it up to date)")
async def brief_cmd(interaction: discord.Interaction):
    p = context.brief_path(interaction.channel.id)
    if not p.exists():
        await interaction.response.send_message(
            "No brief for this thread yet. Claude creates one once there's real project "
            "work here; say \"add to the brief: …\" to add something yourself.", ephemeral=True)
        return
    body = p.read_text()[:1800]
    await interaction.response.send_message(f"### 📌 Project brief\n{body}")


@tree.command(name="helpers", description="Helpers (parallel subagents) in this thread: off, auto or turbo")
@app_commands.describe(mode="empty = show mode + reply times here")
@app_commands.choices(mode=[
    app_commands.Choice(name="turbo · split tasks into parallel helpers, speed first", value="turbo"),
    app_commands.Choice(name="auto · helpers only for big lookups (default)", value="auto"),
    app_commands.Choice(name="off · one agent, lowest token use", value="off"),
])
async def helpers_cmd(interaction: discord.Interaction, mode: str = ""):
    key = str(interaction.channel.id)
    if mode:
        helpers.set_mode(key, mode)
    msg = helpers.summary(key)
    if mode:
        msg += "\n-# applies from the next message in this thread"
    await interaction.response.send_message(msg[:1900])


@tree.command(name="usage", description="Claude plan usage for this bot's login, as progress bars")
async def usage_cmd(interaction: discord.Interaction):
    from botcore import usage as _usage
    await interaction.response.defer(thinking=True)
    try:
        data = await asyncio.to_thread(_usage.fetch)
        msg = _usage.render(data)
    except Exception as e:
        msg = f"⚠️ Couldn't read usage: {e}"
    await interaction.followup.send(msg[:1900])


@tree.command(name="claude",
              description="Mute/unmute Claude here (muted = only answers when @-tagged)")
@app_commands.describe(state="on = normal · off = stay quiet unless @-tagged · empty = show state")
@app_commands.choices(state=[
    app_commands.Choice(name="on  — answer normally", value="on"),
    app_commands.Choice(name="off — only when @-tagged", value="off"),
])
async def claude_cmd(interaction: discord.Interaction, state: str = ""):
    key = str(interaction.channel.id)
    if state in ("on", "off"):
        engagement[key] = "active" if state == "on" else "muted"
        _save_engagement()
    cur = await _engagement_state(interaction.channel)
    where = "this thread" if isinstance(interaction.channel, discord.Thread) else "this channel"
    msg = (f"🔇 Claude is **muted** in {where}: only answers messages that @-tag it."
           if cur == "muted" else f"🔊 Claude is **active** in {where}.")
    await interaction.response.send_message(msg)


# /model pin values = registry keys. Run token per pin: Sonnet/Haiku keep the
# auto-tier tokens; "opus" = the "big" tier (DEFAULT_BIG); the rest run as-is.
PIN_LABELS = {"opus": "Opus 5.5", "opus4.8": "Opus 4.8", "fable5.2": "Fable 5.2", "fable": "Fable 5.1",
              "fable5": "Fable 5", "sonnet": "Sonnet 5.5 (cheap)", "haiku": "Haiku 5.5 (fastest)"}
PIN_MODELS = {"opus": "big", "opus4.8": "opus4.8", "fable5.2": "fable5.2", "fable": "fable",
              "fable5": "fable5", "sonnet": "fast", "haiku": "haiku"}


def _normalize_size(size):
    """Free-text / stored pin → registry key. NB "fable" = the real Fable 5.1."""
    s = re.sub(r"\s+", " ", (size or "").strip().lower())
    if s in ("opus 4.8", "opus4.8", "opus-4-8", "4.8"):
        return "opus4.8"
    if s in ("fable 5.2", "fable5.2", "fable-5-2", "5.2"):
        return "fable5.2"
    if s in ("fable 5", "fable5", "fable-5"):
        return "fable5"
    if s in ("fable", "fable 5.1", "fable5.1", "fable-5-1", "5.1"):
        return "fable"
    if s in ("opus", "opus 5.5", "opus5.5", "5.5"):
        return "opus"
    if s in ("sonnet", "fast", "cheap"):
        return "sonnet"
    if s in ("haiku", "fastest"):
        return "haiku"
    return None


@tree.command(
    name="model",
    description="Pin a model (auto/opus/opus 4.8/fable 5.2/fable/sonnet/haiku). Add text to force just one message.")
@app_commands.describe(
    size="Choose a model, or 'auto' to clear the pin and return to auto-routing",
    message="optional: run ONLY this message on that model, leaving the thread's pin unchanged")
@app_commands.choices(size=[
    app_commands.Choice(name="auto     — back to auto-routing (clear pin)", value="auto"),
    app_commands.Choice(name="opus     — Opus 5.5 (default big model)",     value="opus"),
    app_commands.Choice(name="opus 4.8 — Opus 4.8",                         value="opus4.8"),
    app_commands.Choice(name="fable 5.2 — Fable 5.2 (newest, manual only)", value="fable5.2"),
    app_commands.Choice(name="fable    — Fable 5.1 (manual only)",          value="fable"),
    app_commands.Choice(name="fable 5  — Fable 5",                          value="fable5"),
    app_commands.Choice(name="sonnet   — Sonnet 5.5 (cheap, fast)",         value="sonnet"),
    app_commands.Choice(name="haiku    — Haiku 5.5 (fastest)",              value="haiku"),
])
async def model_cmd(interaction: discord.Interaction, size: str = "", message: str = ""):
    key = str(interaction.channel.id)
    size_raw = (size or "").strip().lower()
    message = (message or "").strip()

    # ── Per-message override: force ONE turn onto the chosen model without
    # touching the thread's persistent pin. In a thread it continues that
    # thread's session; in the main channel it runs as a clean one-off (so it
    # never revives the shared never-ending session).
    if message:
        val = _normalize_size(size_raw)
        if val is None:
            await interaction.response.send_message(
                "Start with a model for a per-message run: `/model opus <your message>` "
                "(opus / opus 4.8 / fable / fable 5 / sonnet / haiku).", ephemeral=True)
            return
        await interaction.response.send_message(
            f"🎯 Forcing **{PIN_LABELS[val]}** for this one message…", ephemeral=True)
        who = f"[discord user: {interaction.user.display_name} (@{interaction.user.name})]"
        full = f"{who}\n\n{message}"
        in_thread = isinstance(interaction.channel, discord.Thread)
        await run_pipeline(interaction.channel, full, key, reply_to=None,
                           persist_session=in_thread, mention=interaction.user.mention,
                           thread_hint=message, model_pin=PIN_MODELS[val])
        return

    # ── No message → set or clear the thread's (or channel-default) pin.
    if size_raw in ("", "auto", "clear", "default", "none"):
        if model_overrides.pop(key, None) is not None:
            save_overrides()
        await interaction.response.send_message("🎛️ Model pin cleared — back to auto-routing.")
        return
    val = _normalize_size(size_raw)
    if val is None:
        await interaction.response.send_message(
            "Use `/model opus`, `opus 4.8`, `fable`, `fable 5`, `sonnet` or `haiku`; "
            "`auto` clears the pin.")
        return
    model_overrides[key] = val
    save_overrides()
    where = ("this thread" if isinstance(interaction.channel, discord.Thread)
             else "new threads from this channel")
    await interaction.response.send_message(f"📌 Pinned **{PIN_LABELS[val]}** for {where}.")


# ──── DISPATCHER STATE + ROUTING LOG ─────────────────────────────

DISPATCHER_FLAG = paths.STATE / "dispatcher.flag"
ROUTING_LOG = LOGS / "routing.jsonl"


def dispatcher_enabled():
    try:
        return DISPATCHER_FLAG.read_text().strip() != "off"
    except FileNotFoundError:
        return True


def log_route(**kw):
    """One line per message: lane, how decided, model, timings, outcome."""
    kw["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        LOGS.mkdir(exist_ok=True)
        with open(ROUTING_LOG, "a") as f:
            f.write(json.dumps(kw) + "\n")
    except Exception as e:
        print(f"routing log failed: {e}", flush=True)


ACTIVITY_LOG = LOGS / "activity.jsonl"


def log_activity(prompt, channel_id, outcome, turn_s=None):
    """One line per task: the prompt text + outcome (logs/activity.jsonl)."""
    try:
        LOGS.mkdir(exist_ok=True)
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "text": (prompt or "").strip().replace("\n", " ")[:200],
               "channel": str(channel_id), "outcome": outcome, "turn_s": turn_s}
        with open(ACTIVITY_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        print(f"activity log failed: {e}", flush=True)


def routing_summary(day=None):
    """Aggregate today's routing log into a one-liner."""
    day = day or datetime.now(timezone.utc).date().isoformat()
    lanes = {"fast": 0, "direct": 0}
    esc = fails = n = 0
    times = []
    try:
        for line in open(ROUTING_LOG):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not r.get("ts", "").startswith(day):
                continue
            n += 1
            lanes[r.get("lane", "direct")] = lanes.get(r.get("lane", "direct"), 0) + 1
            esc += 1 if r.get("escalated") else 0
            fails += 1 if r.get("outcome") not in (None, "ok") else 0
            if r.get("turn_s"):
                times.append(r["turn_s"])
    except FileNotFoundError:
        pass
    if not n:
        return "no routed messages today"
    med = sorted(times)[len(times) // 2] if times else 0
    return (f"routed {n}: ⚡{lanes['direct']} 🐇{lanes['fast']} · "
            f"{esc} escalation{'s' if esc != 1 else ''} · {fails} failure{'s' if fails != 1 else ''} · "
            f"median turn {med:.0f}s")


# ──── ON_MESSAGE ─────────────────────────────────────────────────

# ──── ENGAGEMENT GATE: stay out of human-to-human chat ─────────────
# thread/channel id (str) → "muted" | "active". Threads are auto-classified ONCE
# from their root message (root @-tagged another human but not Claude → muted);
# /claude on|off writes the same file and wins. In a muted thread/channel Claude
# answers ONLY messages that explicitly @-tag it, then goes quiet again.
def _tag_info(message):
    """(claude_tagged, other_human_tagged) from EXPLICIT @-tags in the text.
    raw_mentions parses the content only, so a reply's auto-ping (replying to
    Claude's or someone's message) never counts as a tag."""
    raw = set(message.raw_mentions)
    claude = client.user.id in raw
    others = any(m.id in raw and m.id != client.user.id and not m.bot
                 for m in message.mentions)
    return claude, others


async def _replies_to_other_human(message):
    """True when the message is a Discord reply to ANOTHER human's message (not
    Claude's, not the author's own, not a bot/webhook post): it's addressed to
    them, so Claude stays out unless explicitly @-tagged."""
    ref = message.reference
    if ref is None or not ref.message_id:
        return False
    target = ref.resolved if isinstance(ref.resolved, discord.Message) else None
    if target is None:
        with contextlib.suppress(Exception):
            target = ref.cached_message or await message.channel.fetch_message(ref.message_id)
    if target is None:
        return False
    a = target.author
    return a.id not in (client.user.id, message.author.id) and not a.bot


async def _engagement_state(channel):
    """'muted' or 'active'. Threads with no stored state are classified once
    from their root message; the main channel defaults to active."""
    key = str(channel.id)
    if key in engagement:
        return engagement[key]
    if not isinstance(channel, discord.Thread):
        return "active"
    state = "active"
    root = None
    with contextlib.suppress(Exception):
        root = channel.starter_message or await channel.parent.fetch_message(channel.id)
    if root is not None:
        claude, others = _tag_info(root)
        if others and not claude:
            state = "muted"
    engagement[key] = state
    _save_engagement()
    return state


def _in_allowed(channel):
    """True for the main allowed channel and any thread under it."""
    if channel.id == ALLOWED_CHANNEL_ID:
        return True
    return isinstance(channel, discord.Thread) and channel.parent_id == ALLOWED_CHANNEL_ID


@client.event
async def on_message(message):
    if message.author == client.user:
        return
    # Ignore other bots/apps/webhooks (e.g. reminder apps) so
    # the bot doesn't reply to automated posts.
    if message.author.bot or message.webhook_id is not None:
        return
    if not _in_allowed(message.channel):
        return
    # Only the owner (and anyone `dbot allow` added) can drive Claude.
    if not access.allowed(message.author.id):
        return

    # Engagement gate: BEFORE attachments, the steer inbox and any status
    # message, so a skip is fully silent (no 👀, no thinking-box flicker).
    _claude_tagged, _others_tagged = _tag_info(message)
    if not _claude_tagged:
        if _others_tagged or await _replies_to_other_human(message):
            return
        if await _engagement_state(message.channel) == "muted":
            return
    mark_activity()   # last-activity clock (shown by dbot status)
    for _hook in message_hooks:   # local handlers (bot_local.py)
        try:
            if await _hook(message, (message.content or "").strip()):
                return
        except Exception as _he:
            print(f"local message hook error: {_he!r}", flush=True)

    attachments, att_notes = await download_attachments(message)
    body = (message.content or "").strip()
    if not body and not attachments and not att_notes:
        return

    # Typing supersedes any live option buttons in this channel — grey them out
    # (the typed message is the answer; don't let a later click double-fire).
    if str(message.channel.id) in option_views:
        _spawn(retire_option_view(message.channel.id))

    # Inline "ultra" trigger: prefix with `>>` or `!ultra ` opts into workflow mode.
    ultracode = False
    if body.startswith(">> "):
        body = body[3:]
        ultracode = True
    elif body.lower().startswith("!ultra "):
        body = body[7:]
        ultracode = True

    # Lane override: !fast forces the small-model fast lane (same session).
    # Kill-switch for auto-routing: "!dispatcher off" = every message goes
    # straight to the big model. Persistent across restarts.
    if body.lower().strip() in ("!dispatcher off", "!dispatcher on"):
        val = body.lower().strip().split()[-1]
        DISPATCHER_FLAG.write_text(val)
        await message.channel.send(
            "🔀 Auto-routing **ON** (🐤/🐇/⚡ picked per message)." if val == "on" else
            f"⏸️ Auto-routing **OFF**: every message goes straight to {BIG_LABEL}. "
            "`!dispatcher on` to re-enable. The `!fast` prefix still works.")
        return

    lane, speed, route = None, None, None
    _t_triage = 0.0
    _how = "default"
    if body.lower().startswith("!fast "):
        body, lane, speed = body[6:], "direct", "fast"
        route = "🐇 fast lane (forced) · sonnet"
        _how = "forced"

    # Conversational thread finder: "find my thread about github repos",
    # "list my threads". Read-only and session-free, so it answers right away —
    # even mid-turn — and never spins up its own thread or pipeline run.
    if (not ultracode and not attachments and len(body) <= 200
            and THREAD_FIND_RE.search(body)):
        await handle_thread_find(message)
        return

    # Follow-up to a recent thread search: "what about X?", "and X?", "try X"
    # Catches the pattern where the user refines their search without repeating
    # "find thread" — avoids accidentally spawning a new thread+pipeline run.
    _last_search = recent_thread_searches.get(message.channel.id, 0)
    if (not ultracode and not attachments and len(body) <= 200
            and time.time() - _last_search < 90):
        _fu = THREAD_FOLLOWUP_RE.match(body.strip())
        if _fu:
            await handle_thread_find(message, override_topic=_fu.group(1))
            return

    # Conversational "move to a new thread": only from inside a thread. Summarises
    # the current thread and seeds a fresh one so context resets without loss.
    if (not ultracode and not attachments and len(body) <= 200
            and isinstance(message.channel, discord.Thread)
            and MOVE_THREAD_RE.search(body)):
        await handle_move_thread(message)
        return

    key = str(message.channel.id)

    # Task(s) already running here → route the message: a steer/correction goes
    # to the matching task's inbox (steer hook delivers it mid-turn, 👀 =
    # absorbed); an independent new task spawns a parallel SIBLING turn (🆕)
    # that forks the session and runs alongside. Router failure falls back to
    # the pre-upgrade behaviour (steer the oldest running task).
    _active = dict(thread_tasks.get(key) or {})
    if busy_channels.get(key) or _active:
        content = body
        for p in attachments:
            content += f"\n[attachment saved at {p}]"
        for n in att_notes:
            content += f"\n{n}"
        action, rtid = ("steer", None)
        if _active:
            action, rtid = await route_mid_turn(_active, content)
        if action == "new" and _active and len(_active) < PARALLEL_CAP:
            ntid = _next_task_id(key)
            try:
                await message.add_reaction("🆕")
            except discord.HTTPException:
                pass
            prefix = (f"[Parallel task — {OWNER} sent this while other work runs in "
                      "this thread. Another Claude instance is handling the other "
                      "task(s); do ONLY what this message asks, and don't touch "
                      "the other tasks' work.]\n")
            _spawn(run_pipeline(message.channel, prefix + content, key,
                                reply_to=message, mention=message.author.mention,
                                thread_hint=body, fork_session=True, task_id=ntid))
            return
        # steer (or over-cap "new" → absorbed by the busiest-fit task)
        _tgt = _active.get(rtid) or (_active.get(min(_active)) if _active else None)
        tkey = _tgt["track_key"] if _tgt else key
        with open(inbox_path(tkey), "a") as f:
            f.write(json.dumps({"ts": time.time(),
                                "author": message.author.display_name,
                                "content": content, "status": "pending"}) + "\n")
        try:
            await message.add_reaction("👀")
        except discord.HTTPException:
            pass
        return

    # On any thread message, pull the backlog. A session alone isn't enough: it
    # may have been created mid-thread and never saw the earlier messages, so
    # relying on session memory leaves me blind to context that's right there.
    in_thread = isinstance(message.channel, discord.Thread)

    lane = "direct"

    # Route-DOWN default: a turn starts on the
    # cheapest model that plausibly fits — Haiku for trivial acks, the big model
    # for clearly heavy work (BIG_WORK_RE), Sonnet for everything else — and the
    # escalation hatch upgrades mid-turn if needed. Skipped when a lane already
    # forced the model (!fast) or this is an ultra run; a /model pin overrides it
    # below.
    if speed is None and not ultracode and dispatcher_enabled():
        speed = default_tier(body, bool(attachments))
        _how = "route-down" if _how == "default" else _how
        if route is None:
            if speed == "haiku":
                route = "🐤 haiku (route-down default)"
            elif speed == "fast":
                route = "🐇 sonnet (route-down default)"
            else:
                route = f"⚡ big work (route-down) · {BIG_LABEL}"

    # Thread = session: a fresh prompt in the MAIN channel spins up its own
    # thread and the turn runs there, so each conversation gets its own
    # short-lived session instead of every main-channel turn sharing one
    # ever-growing one (the #1 token-cost driver). Replies inside a thread just
    # continue that thread's session (key is already the thread id below).
    run_channel, run_key, run_reply_to = message.channel, key, message
    if not in_thread:
        try:
            thread = await message.create_thread(
                name=make_thread_name(body or "New conversation"),
                auto_archive_duration=1440)
            run_channel, run_key, run_reply_to = thread, str(thread.id), None
            # Swap the trimmed-prompt name for a short summary in the background so
            # the turn isn't blocked on the title model call.
            _spawn(rename_thread_to_summary(thread, body))
        except (discord.HTTPException, AttributeError) as e:
            print(f"thread=session create failed, running in channel: {e}", flush=True)

    # A /model pin wins over auto-routing. In a thread it's that thread's pin; a
    # fresh main-channel turn inherits the main channel's default pin. Haiku is
    # reachable here only via an explicit pin, never the auto-router.
    override = model_overrides.get(run_key) or (
        None if in_thread else model_overrides.get(str(message.channel.id)))
    override = _normalize_size(override) if override else None
    model_pin = PIN_MODELS.get(override)
    if model_pin:
        speed = model_pin if model_pin in ("fast", "haiku") else None

    if route is None:
        route = (f"🛠️ ultra · {BIG_LABEL}" if ultracode else
                 f"⚡ direct (default) · {BIG_LABEL}" if dispatcher_enabled() else
                 f"⚡ direct (dispatcher off) · {BIG_LABEL}")
    if override:
        route += f" · 📌 {override}"

    # Reaction marker on the user's main-channel message: 🧵 if we spawned a
    # thread (not a working marker, so resolve_reactions never strips it), else
    # the lane emoji for the in-channel fallback path.
    if run_reply_to is None:
        try:
            await message.add_reaction("🧵")
        except discord.HTTPException:
            pass
    elif lane == "direct":
        try:
            await message.add_reaction(
                {"haiku": "🐤", "fast": "🐇"}.get(speed, "⚡"))
        except discord.HTTPException:
            pass

    full = await build_prompt(message, body, attachments, ultracode=ultracode,
                              include_thread_history=in_thread and not sessions.get(run_key),
                              attachment_notes=att_notes)
    await run_pipeline(run_channel, full, run_key, reply_to=run_reply_to,
                       mention=message.author.mention, thread_hint=body,
                       speed=speed, route=route, model_pin=model_pin,
                       meta={"lane": "fast" if speed in ("fast", "haiku") else "direct",
                             "how": _how, "triage_s": _t_triage})


# ──── 🔄 REACTION = REDO ─────────────────────────────────────────

@client.event
async def on_reaction_add(reaction, user):
    if user == client.user:
        return
    if not _in_allowed(reaction.message.channel):
        return
    if not access.allowed(user.id):
        return
    # ❌ on the "thinking…" status message = emergency brake: hard-kill the turn.
    if str(reaction.emoji) == "❌":
        key = str(reaction.message.channel.id)
        # Which task's status/card message was ❌'d? Primary and parallel
        # siblings each have their own — kill ONLY that task.
        _tasks = thread_tasks.get(key) or {}
        hit_key, hit_tid = None, None
        for _tid, _rec in sorted(_tasks.items()):
            _sm = _rec.get("status_msg")
            if _sm is not None and reaction.message.id == _sm.id:
                hit_key, hit_tid = _rec["track_key"], _tid
                break
        if hit_key is None:
            st = busy_channels.get(key)
            if st and reaction.message.id == st.id:
                hit_key = key
        if hit_key is not None:
            killed_channels.add(hit_key)
            proc = running_procs.get(hit_key)
            if proc and proc.returncode is None:
                proc.kill()
            _which = (f" task {hit_tid}" if hit_tid and len(_tasks) > 1 else "")
            try:
                await reaction.message.channel.send(
                    f"⛔ Stopped{_which}. Anything that already executed stays "
                    "executed; nothing further will run. (If the next reply "
                    "errors, send any message — I recover automatically.)")
            except discord.HTTPException:
                pass
        return
    # A tap on a pre-seeded "react X to …" ask: the reaction IS the owner's answer —
    # run a follow-up turn in that conversation so they never have to type it.
    _ask = ask_reactions.get(reaction.message.id)
    if _ask and (str(reaction.emoji).replace("\ufe0f", "")
                 in {e.replace("\ufe0f", "") for e in _ask["emojis"]}):
        ask_reactions.pop(reaction.message.id, None)
        akey = str(reaction.message.channel.id)
        note = (f"[{OWNER} reacted {reaction.emoji} to your message that asked for a "
                f"reaction — treat that as their answer and proceed now.]\n"
                f"[that message began: {_ask['excerpt']}]")
        if busy_channels.get(akey):
            with open(inbox_path(akey), "a") as f:
                f.write(json.dumps({"ts": time.time(), "author": user.display_name,
                                    "content": note, "status": "pending"}) + "\n")
            with contextlib.suppress(discord.HTTPException):
                await reaction.message.add_reaction("👀")
            return
        _spawn(run_pipeline(reaction.message.channel, note, _ask["session_key"],
                            mention=_ask.get("mention"),
                            thread_hint=f"reaction follow-up: {_ask['excerpt'][:60]}"))
        return

    if str(reaction.emoji) != "🔄":
        return
    entry = last_prompts.get(reaction.message.id)
    if not entry:
        return
    # Older entries (pre-mention) are 3-tuples; new ones are 4-tuples.
    if len(entry) == 4:
        prompt_text, _channel_id, reply_to_id, mention = entry
    else:
        prompt_text, _channel_id, reply_to_id = entry
        mention = None
    channel = reaction.message.channel
    reply_to = None
    if reply_to_id:
        try:
            reply_to = await channel.fetch_message(reply_to_id)
        except Exception:
            reply_to = None
    key = str(channel.id)
    # Fresh session for the redo
    sessions.pop(key, None)
    save_sessions(sessions)
    await run_pipeline(channel, prompt_text, key, reply_to=reply_to, mention=mention)


_loops_started = False
_bg_tasks = set()   # hold refs so background tasks aren't garbage-collected


def _spawn(coro):
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t


@client.event
async def on_ready():
    global _loops_started
    if not _loops_started:
        _loops_started = True
        _spawn(pollers.update_outbox_poller(client))       # mid-task progress updates
        _spawn(pollers.post_restart_handover(client))      # announce "I'm back" after a requested restart
        _spawn(_backfill_thread_index())      # seed thread index (finder) from existing threads
    # Boot marker for `dbot restart`: no fresh marker within 2 min of a
    # requested restart → it restores the last-good code and restarts again.
    with contextlib.suppress(OSError):
        (paths.STATE / "bot-ready").write_text(str(time.time()))
    print(f"Logged in as {client.user}", flush=True)
    try:
        await tree.sync()
    except Exception as e:
        print(f"tree.sync failed (non-fatal): {e}", flush=True)


# ──── BOX-LOCAL EXTENSIONS ───────────────────────────────────────
# Optional BOT_HOME/bot_local.py (yours, never overwritten by updates):
#   def setup(bot): ...   # `bot` is this module: bot.tree / bot.client to add
#                         # commands; bot.message_hooks.append(async fn) where
#                         # fn(message, body) -> True means "handled, stop".
message_hooks = []


def _load_local():
    if str(paths.HOME) not in sys.path:
        sys.path.insert(0, str(paths.HOME))
    try:
        import bot_local
    except ModuleNotFoundError:
        return
    try:
        bot_local.setup(sys.modules[__name__])
        print("bot_local loaded", flush=True)
    except Exception as e:
        print(f"bot_local setup failed (ignored): {e!r}", flush=True)


def _sweep_files(days=30):
    cut = time.time() - days * 86400
    for f in FILES_DIR.glob("discord-*"):
        with contextlib.suppress(OSError):
            if f.stat().st_mtime < cut:
                f.unlink()


def main():
    token = env.get("DISCORD_TOKEN", "")
    if not token:
        sys.exit(f"No DISCORD_TOKEN in {paths.SECRETS_FILE} (run: kit configure discord-bot)")
    _sweep_files()
    _load_local()
    client.run(token)
