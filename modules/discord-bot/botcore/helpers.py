"""Helpers (subagents) per thread, set with /helpers. Speed comes from running
independent pieces of a task in parallel on fast models, while the main model
plans, checks and joins the results.

  * off   : one agent, no subagents (lowest token use)
  * auto  : subagents only for broad lookups / clearly separate sub-tasks
  * turbo : split every substantial task into parallel helpers straight away
            (Haiku reads/searches, Sonnet edits; Haiku only while saver mode is on)

Box default: bot.conf HELPERS=auto. Reply times are recorded per thread and
mode so /helpers can show whether turbo is really faster there.
"""
import json, os, pathlib, time
from botcore import paths

from botcore.config import conf

CFG = paths.STATE
MODE_FILE = CFG / "helpers-mode.json"
STATS_FILE = CFG / "helpers-stats.json"
MODES = ("off", "auto", "turbo")
KEEP = 20                       # recent reply times kept per thread + mode


def _load(p):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def _save(p, data):
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, p)


def _tid(key):
    return str(key or "").split("~")[0]


def default_mode():
    m = conf.get("HELPERS", "auto").lower()
    return m if m in MODES else "auto"


def mode(key):
    return _load(MODE_FILE).get(_tid(key)) or default_mode()


def set_mode(key, m):
    data = _load(MODE_FILE)
    if m == default_mode():
        data.pop(_tid(key), None)
    else:
        data[_tid(key)] = m
    _save(MODE_FILE, data)


def _saver_on():
    """Saver mode from the cached usage only (never blocks a turn on a fetch)."""
    try:
        from botcore import saver
        data = saver._usage_cache.get("data")
        if not data or conf.get("SAVER", "on").lower() == "off":
            return False
        return saver.saver_state()[0]
    except Exception:
        return False


def addendum(key):
    """System-prompt text for this thread's helper mode."""
    m = mode(key)
    if m == "off":
        return ("Helpers are OFF in this thread: do the work yourself and don't start "
                "subagents (Agent tool) unless the user explicitly asks for them.")
    economy = ("Context economy: for broad exploration (reading many files, scanning a "
               "codebase, long logs or search results) use a subagent (Agent tool) and keep "
               "only its short summary, so bulky raw output never enters this conversation.")
    if m == "auto":
        return economy
    models = ("model haiku for every helper (quota saver is on)" if _saver_on() else
              "model haiku for reading, searching and summarising; model sonnet for "
              "mechanical edits, scripts and checks")
    return (economy + "\n\nHelpers are in TURBO mode in this thread: speed comes first. "
            "For any substantial task, plan briefly, split the work into independent "
            "pieces and launch them as parallel subagents in ONE response (several Agent "
            "tool calls in the same message, so they run at the same time). Use "
            f"{models}. Give each helper a self-contained brief (exact paths, hosts, what "
            "to return) and ask for a short result. Keep judgement, integration and the "
            "final check yourself. Do small tasks and strictly step-by-step chains "
            "directly instead of splitting them. Also batch independent tool calls "
            "(reads, greps, commands) into one response.")


def record(key, m, secs, n_helpers):
    data = _load(STATS_FILE)
    rows = data.setdefault(_tid(key), {}).setdefault(m, [])
    rows.append([round(secs, 1), int(n_helpers), int(time.time())])
    del rows[:-KEEP]
    try:
        _save(STATS_FILE, data)
    except OSError:
        pass


def footer(m, n_helpers, secs):
    """'-# ⚡ turbo · 3 helpers · 41s' when helpers ran or turbo is on, else ''."""
    if m == "off" or (m == "auto" and not n_helpers):
        return ""
    icon = "⚡" if m == "turbo" else "🤝"
    parts = [f"{icon} {m}"]
    if n_helpers:
        parts.append(f"{n_helpers} helper{'s' if n_helpers != 1 else ''}")
    parts.append(f"{secs:.0f}s")
    return "-# " + " · ".join(parts)


def summary(key):
    """Card for /helpers: current mode + average reply time per mode here."""
    m = mode(key)
    stats = _load(STATS_FILE).get(_tid(key), {})
    lines = [f"### ⚡ Helpers: **{m}**" + (" (default)" if m == default_mode() else ""),
             {"off": "-# one agent, no helpers, lowest token use",
              "auto": "-# helpers only for broad lookups and clearly separate sub-tasks",
              "turbo": "-# every substantial task split into parallel helpers, speed first"}[m]]
    rows = [(k, stats[k]) for k in MODES if stats.get(k)]
    if rows:
        lines.append("**Reply times in this thread**")
        for k, r in rows:
            avg = sum(x[0] for x in r) / len(r)
            hl = sum(x[1] for x in r) / len(r)
            lines.append(f"{k}: avg **{avg:.0f}s** over {len(r)} replies"
                         + (f" · {hl:.1f} helpers/reply" if hl else ""))
    else:
        lines.append("-# no reply times recorded here yet")
    return "\n".join(lines)
