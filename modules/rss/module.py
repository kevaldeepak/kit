"""rss module hooks.

Settings (--set key=value): topic, profile, starter (yes/no), test_push (yes/no)
"""
import os
import sys

STARTER = """
  - name: Microsoft 365
    url: https://status.cloud.microsoft/api/feed/mac
"""


def _rss():
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import rss  # noqa: E402
    return rss


def configure(ctx):
    cur = ctx.load()
    profiles = list(ctx.notify.load_profiles())
    topic = ctx.ask("Topic", key="topic", default=cur.get("topic") or "rss",
                    validate=lambda v: (True, v) if v.replace("-", "").replace("_", "").isalnum() and len(v) <= 64
                    else (False, "letters, numbers, - and _ only"))

    def has_profile(v):
        return (True, v) if v in profiles else (False, f"no such profile (have: {', '.join(profiles) or 'none'})")

    default_prof = cur.get("profile") or ("default" if "default" in profiles else (profiles[0] if profiles else "default"))
    profile = ctx.ask("ntfy profile", key="profile", default=default_prof, validate=has_profile)
    ctx.save({"topic": topic, "profile": profile})
    rss = _rss()
    if not os.path.exists(rss.CFG):
        text = rss.YAML_TEMPLATE_HEAD + "\nfeeds:\n"
        if ctx.confirm("Add the Microsoft 365 status feed as a starter?", key="starter", default=False):
            text += STARTER
        ctx.system.write_text(rss.CFG, text)
    else:
        ctx.say(f"Keeping your existing feeds ({rss.CFG}).")


def install(ctx):
    st = ctx.load()
    if ctx.opts.get("test_push", "yes").lower() in ("no", "n", "false", "0"):
        ctx.say("Add a feed: rss add <url>    All commands: rss")
        return
    try:
        ctx.notify.send(st["topic"], "✅ rss is set up", f"Alerts from {ctx.hostname} will arrive on this topic.",
                        priority=2, tags=["white_check_mark"], profile=st["profile"])
        ctx.say(f"Test push sent to topic {st['topic']}, check your phone.")
    except ctx.notify.NotifyError as e:
        ctx.warn(f"Test push failed: {e}")
    ctx.say("Add a feed: rss add <url>    All commands: rss")


def reconfigured(ctx):
    install(ctx)


def doctor(ctx):
    return _rss().doctor_checks()
