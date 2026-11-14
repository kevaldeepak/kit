"""File-backed bot state (sessions, model pins, thread index,
engagement, steer inbox, parallel notes) plus the in-memory runtime
registries the handlers share. Mutated in place, never rebound."""
import json, pathlib, time
from botcore import paths

SESSIONS_FILE = paths.STATE / "discord-sessions.json"
SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)

def load_sessions():
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text())
        except Exception:
            return {}
    return {}

def save_sessions(s):
    SESSIONS_FILE.write_text(json.dumps(s))

sessions = load_sessions()

# Per-thread (or main-channel default) model pin set via /model. Maps a
# channel/thread id -> "small" (Sonnet) or "big" (Fable). A pin on the main
# channel is inherited by every new thread spawned from it; a pin inside a
# thread applies to that thread only. Empty/absent -> auto-routing.
OVERRIDES_FILE = paths.STATE / "model-overrides.json"


def load_overrides():
    if OVERRIDES_FILE.exists():
        try:
            return json.loads(OVERRIDES_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_overrides():
    OVERRIDES_FILE.write_text(json.dumps(model_overrides))


model_overrides = load_overrides()

# Local thread index: thread_id(str) -> {"name","summary","ts"}. Lets the
# thread finder work off a cheap local lookup instead of
# re-paginating Discord + with zero model calls. Updated whenever a thread is
# created, renamed or handed over; backfilled from Discord once at startup.
THREAD_INDEX_FILE = paths.STATE / "thread-index.json"


def load_thread_index():
    if THREAD_INDEX_FILE.exists():
        try:
            return json.loads(THREAD_INDEX_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_thread_index():
    try:
        THREAD_INDEX_FILE.write_text(json.dumps(thread_index))
    except OSError as e:
        print(f"thread index save failed: {e}", flush=True)


def index_thread(thread_id, name, summary=None):
    """Upsert a thread into the local index. A None summary keeps any existing one."""
    tid = str(thread_id)
    entry = thread_index.get(tid, {})
    entry["name"] = name or entry.get("name", "")
    if summary:
        entry["summary"] = summary[:500]
    entry.setdefault("summary", "")
    entry["ts"] = time.time()
    thread_index[tid] = entry
    save_thread_index()


thread_index = load_thread_index()
running_procs = {}      # channel_id (str) -> asyncio.subprocess.Process
last_prompts = {}       # bot reply message_id -> (prompt_text, channel_id, reply_to_msg_id)


# Per-channel timestamp of the last handle_thread_find response (for follow-up detection).
recent_thread_searches: dict[int, float] = {}

# Mid-turn steering: while a turn runs in a channel, new messages append to the
# channel's inbox file; the PreToolUse steer hook (bin/steer-hook.py) surfaces
# them inside the running claude turn at its next tool call.
INBOX_DIR = paths.STATE / "inbox"
INBOX_DIR.mkdir(parents=True, exist_ok=True)
busy_channels = {}       # channel_key -> in-flight status message
killed_channels = set()  # channels stopped via ❌ reaction or /cancel


def inbox_path(key):
    return INBOX_DIR / f"{key}.jsonl"


def drain_inbox(key):
    """Return never-delivered (pending) message contents and remove the inbox.
    Items marked delivered were already surfaced in-turn by the steer hook."""
    p = inbox_path(key)
    if not p.exists():
        return []
    pending = []
    for line in p.read_text().splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if item.get("status") == "pending" and item.get("content"):
            pending.append(item["content"])
    p.unlink(missing_ok=True)
    pathlib.Path(str(p) + ".lock").unlink(missing_ok=True)
    return pending


# ──── PARALLEL TASKS ─────────────────────────────────────────────
# Several turns may run in the SAME thread at once. The primary turn keeps the
# thread's canonical session lineage (and busy_channels slot); siblings fork
# the session at spawn and run under a task-scoped track key "chid~tN" — that
# key namespaces their inbox, subprocess, kill flag and card files. A Haiku
# router decides whether a mid-turn message steers a running task or is an
# independent task worth a sibling.
thread_tasks = {}    # channel_key -> {task_id: {"track_key","status_msg","label","ts"}}
task_counters = {}   # channel_key -> last issued task_id (resets when thread idles)
PARALLEL_CAP = 3     # max concurrent tasks per thread (primary + siblings)
NOTES_DIR = paths.STATE / "parallel-notes"
NOTES_DIR.mkdir(parents=True, exist_ok=True)


def _next_task_id(ckey):
    task_counters[ckey] = task_counters.get(ckey, 0) + 1
    return task_counters[ckey]


def append_parallel_note(ckey, label, summary):
    """Record a finished sibling's outcome so the thread's canonical session
    hears about it on its next turn (siblings' forked sessions are discarded)."""
    with open(NOTES_DIR / f"{ckey}.jsonl", "a") as f:
        f.write(json.dumps({"ts": time.time(), "label": label[:150],
                            "summary": summary[:600]}) + "\n")


def pop_parallel_notes(ckey):
    p = NOTES_DIR / f"{ckey}.jsonl"
    if not p.exists():
        return []
    notes = []
    for line in p.read_text().splitlines():
        try:
            notes.append(json.loads(line))
        except ValueError:
            continue
    p.unlink(missing_ok=True)
    return notes




ENGAGEMENT_FILE = paths.STATE / "thread-engagement.json"


def _load_engagement():
    try:
        return json.loads(ENGAGEMENT_FILE.read_text())
    except Exception:
        return {}


engagement = _load_engagement()


def _save_engagement():
    ENGAGEMENT_FILE.write_text(json.dumps(engagement))


# Last time a human message reached Claude (or a run started).
ACTIVITY_FILE = paths.STATE / "last-activity"


def mark_activity():
    try:
        ACTIVITY_FILE.write_text(str(time.time()))
    except OSError:
        pass
