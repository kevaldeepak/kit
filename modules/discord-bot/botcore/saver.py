"""Quota savers for auto-routing (pins always win):

  * sticky big: a thread whose last turn needed the big model starts its next
    auto turn there directly, skipping a Sonnet pass that would only escalate.
  * saver mode: when this computer's plan usage crosses bot.conf SAVER_WEEKLY_AT
    (default 80%) or SAVER_SESSION_AT (default 90%), auto turns that would run
    on the big model run on Sonnet instead, and the footer says so.
  * heavy-thread hint: when one turn re-reads more than HEAVY_THREAD_TOKENS
    (default 1.5M) of history, the footer suggests moving to a fresh thread.
    Only when auto-compact can't help (it's off, or a thread is still heavy
    right after compacting); bot.py decides.
"""
import time

from botcore.config import conf, conf_int

STICKY_TTL = 45 * 60          # a quiet thread falls back to normal routing
_sticky = {}                  # channel key -> last time a turn ran on the big model
_usage_cache = {"ts": 0.0, "data": None}
_last_hint = {}               # channel key -> turns since last heavy-thread hint


def mark_tier(ckey, ran_big):
    if ran_big:
        _sticky[ckey] = time.time()
    else:
        _sticky.pop(ckey, None)


def sticky_big(ckey):
    t = _sticky.get(ckey)
    return bool(t and time.time() - t < STICKY_TTL)


def _usage():
    """Plan usage, cached 5 min. Never raises (no data = saver off)."""
    if time.time() - _usage_cache["ts"] < 300:
        return _usage_cache["data"]
    try:
        from botcore import usage
        data = usage.fetch()
    except Exception:
        data = None
    _usage_cache.update(ts=time.time(), data=data)
    return data


def saver_state():
    """(active, reason) from live plan usage."""
    if conf.get("SAVER", "on").lower() == "off":
        return False, ""
    data = _usage() or {}
    weekly_at = conf_int("SAVER_WEEKLY_AT", 80)
    session_at = conf_int("SAVER_SESSION_AT", 90)
    for lim in data.get("limits") or []:
        if (lim.get("scope") or {}).get("model"):
            continue                      # per-model caps (e.g. Fable) don't trigger saver
        pct = float(lim.get("percent") or 0)
        if lim.get("kind") == "weekly_all" and pct >= weekly_at:
            return True, f"weekly {pct:.0f}%"
        if lim.get("kind") == "session" and pct >= session_at:
            return True, f"5-hour {pct:.0f}%"
    return False, ""


def heavy_hint(ckey, cache_read):
    """Footer line suggesting a fresh thread, at most every 5 turns per thread."""
    limit = conf_int("HEAVY_THREAD_TOKENS", 1_500_000)
    n = _last_hint.get(ckey, 99) + 1
    _last_hint[ckey] = n
    if cache_read < limit or n < 5:
        return ""
    _last_hint[ckey] = 0
    return (f"-# 🧵 this thread re-read {cache_read / 1e6:.1f}M tokens of history this turn; "
            "say “move to a new thread” to continue fresh with a summary and save quota")
