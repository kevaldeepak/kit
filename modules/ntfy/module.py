"""ntfy module hooks: store a server connection (profile) other modules send through.

Settings (--set key=value): profile, url, token
"""
import os


def configure(ctx):
    n = ctx.notify
    profiles = n.load_profiles()
    name = ctx.ask("Profile name", key="profile", default="default")
    cur = profiles.get(name, {})
    url = ctx.ask("Server (IP:port or URL)", key="url", default=cur.get("url"), validate=n.resolve_url)
    ctx.say(f"  ok, ntfy found at {url}")
    env_tok = os.environ.get("KIT_NTFY_TOKEN")

    def check(tok):
        ok, err = n.check_token(url, tok)
        return (True, tok) if ok else (False, err)

    token = ctx.ask("Token (blank if none)", key="token", secret=True, allow_empty=True,
                    default=env_tok if env_tok is not None else cur.get("token", ""), validate=check)
    ctx.say("  ok, token accepted" if token else "  ok, no token needed")
    profiles[name] = {"url": url, "token": token}
    n.save_profiles(profiles)
    ctx.say(f"Saved profile \"{name}\". Send a test: notify -t <topic> \"hello\"")


def uninstall(ctx, purge):
    if purge:
        p = ctx.notify.PROFILES
        if os.path.exists(p):
            os.remove(p)


def doctor(ctx):
    n = ctx.notify
    profiles = n.load_profiles()
    if not profiles:
        return [(False, "no profiles (kit configure ntfy)")]
    out = []
    for name, p in profiles.items():
        ok, err = n.health(p["url"])
        if ok:
            ok, err = n.check_token(p["url"], p.get("token"))
        out.append((ok, f"profile {name}: {p['url']}" + ("" if ok else f" ({err})")))
    return out
