"""discord-bot module hooks: guided setup, service install, safe upgrade.

Settings (--set key=value): token, channel, owner, name, mode (full|safe),
workdir, install_claude (yes/no), start (yes/no)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _lib():
    import setup_lib
    return setup_lib


def _ensure_deps(ctx):
    try:
        import discord  # noqa: F401,F811
        return
    except ImportError:
        pass
    pip = getattr(ctx.system, "pip_install", None)
    if pip is None:
        raise SystemExit("This kit is too old for discord-bot. Run `kit update` first, then install again.")
    pip(ctx.manifest.get("pip") or [])


def _claude(ctx, L):
    inst, logged, detail = L.claude_status()
    if not inst:
        ctx.say("Claude Code isn't installed. The bot runs Claude Code on this computer.")
        if ctx.confirm("Install it now (official installer from claude.ai)?", key="install_claude", default=True):
            if not L.install_claude():
                raise SystemExit("Claude Code install failed. Install it yourself, then run: kit install discord-bot")
            inst, logged, detail = L.claude_status()
        else:
            ctx.warn("Carrying on without it. The bot won't answer until Claude Code is installed: "
                     + L.INSTALL_CLAUDE)
            return
    if not logged:
        ctx.say("Claude Code needs you to sign in with your own Claude account.")
        ctx.say("  Open a terminal, run `claude`, and type /login (opens your browser).")
        if not ctx.yes and ctx.confirm("Done signing in? (No = carry on, the bot won't answer until you do)", default=True):
            inst, logged, detail = L.claude_status()
        ctx.say("  ok, Claude Code " + detail if logged else "  ! not signed in yet: run claude, then /login")
    else:
        ctx.say(f"  ok, Claude Code {detail}")


def configure(ctx):
    _ensure_deps(ctx)
    L = _lib()
    conf = L.load_conf()
    ctx.say("")
    ctx.say("Step 1 of 4: Claude Code")
    _claude(ctx, L)

    ctx.say("")
    ctx.say("Step 2 of 4: your Discord bot")
    ctx.say(f"  1. Open {L.PORTAL} and click New Application (any name).")
    ctx.say("  2. Bot page: turn ON 'Message Content Intent', then Reset Token and copy it.")
    cur_tok = L.load_token()
    info = {}

    def check(v):
        ok, res = L.check_token(v)
        if ok:
            info.update(res)
            return True, res["token"]
        return False, res

    token = ctx.ask("Bot token", key="token", secret=True, default=cur_tok or None, validate=check)
    L.save_token(token)
    ctx.say(f"  ok, bot \"{info.get('bot')}\" (token saved, only your user can read it)")
    if not info.get("content_intent"):
        ctx.warn("Message Content Intent is OFF, so the bot can't read messages. "
                 f"Turn it on: {L.PORTAL} > your app > Bot.")

    ctx.say("")
    ctx.say("Step 3 of 4: add the bot to your server")
    ctx.say(f"  Open this link and pick your server:\n  {L.invite_url(info.get('app_id'))}")
    channel = ctx.opts.get("channel") or (conf.get("BOT_CHANNEL_ID") if ctx.yes else None)
    guild_owner = None
    if not channel:
        if not ctx.yes:
            ctx.ask("Press Enter once the bot is in your server", allow_empty=True, default="")
        chans = []
        try:
            chans = L.text_channels(token)
        except L.DiscordError as e:
            ctx.warn(f"Couldn't list channels: {e}")
        if chans:
            from kit import ui
            labels = [f"{g} / #{c}" for g, _, c, _ in chans]
            cur = next((i for i, c in enumerate(chans) if c[1] == conf.get("BOT_CHANNEL_ID")), 0)
            ctx.say("  Which channel should the bot listen in? (it makes a thread per conversation)")
            i = ui.choose("Channel", labels, default=cur)
            channel, guild_owner = chans[i][1], chans[i][3]
        else:
            ctx.say("  No channels visible yet. Paste a channel id instead "
                    "(Discord: Settings > Advanced > Developer Mode, then right-click the channel > Copy Channel ID).")
            channel = ctx.ask("Channel id", key="channel", default=conf.get("BOT_CHANNEL_ID"), validate=L._digits)
    ok, channel = L._digits(channel)
    if not ok:
        raise SystemExit(f"channel: {channel}")

    ctx.say("")
    ctx.say("Step 4 of 4: who can use it")
    ctx.say("  Only the owner's messages are answered; everyone else is ignored.")
    ctx.say("  Your user id: Discord Settings > Advanced > Developer Mode, then right-click your name > Copy User ID.")
    owner = ctx.ask("Owner Discord user id", key="owner",
                    default=conf.get("BOT_OWNER_ID") or guild_owner, validate=L._digits)
    uname = L.user_name(token, owner)
    if uname:
        ctx.say(f"  ok, that's {uname}")
    name = ctx.ask("What should Claude call you", key="name",
                   default=conf.get("BOT_OWNER_NAME") or uname or "the user")
    ctx.say("")
    ctx.say("  Access mode:")
    ctx.say("    full: Claude can read and edit files and run commands on this computer (most useful)")
    ctx.say("    safe: read, search and look things up only; no edits, no commands")
    ctx.say("  Anyone who gets into your Discord account can use this, so keep 2FA on.")
    mode = ctx.ask("Mode", key="mode", default=conf.get("MODE") or "full", validate=L.VALIDATE["MODE"])
    workdir = ctx.ask("Folder Claude works in", key="workdir",
                      default=conf.get("WORKDIR") or os.path.expanduser("~"), validate=L.VALIDATE["WORKDIR"])
    conf.update({"BOT_CHANNEL_ID": channel, "BOT_OWNER_ID": owner, "BOT_OWNER_NAME": name,
                 "MODE": mode, "WORKDIR": workdir})
    L.save_conf(conf)
    ctx.say("Settings saved. Change any later with: dbot set <key> <value> (see: dbot config)")


def install(ctx):
    import svc
    warn = svc.install()
    if warn:
        ctx.warn(warn)
    if not ctx.confirm("Start the bot now?", key="start", default=True):
        ctx.say("Start it later with: dbot start")
        return
    import time
    t0 = time.time()
    ok, err = svc.start()
    if ok and svc.booted(t0, wait=90):
        svc.snapshot()
        ctx.say("Bot is online. Say hi in your channel; it replies in a thread.")
        ctx.say("Commands: dbot (help), dbot status, dbot logs, dbot config")
    else:
        ctx.warn("The bot didn't come online yet" + (f" ({err})" if err else "") + ". Check: dbot logs, dbot doctor")


def reconfigured(ctx):
    import svc
    if svc.running():
        ok, msg = svc.safe_restart(say=ctx.say)
        ctx.say(msg)


def upgrade(ctx, old):
    _ensure_deps(ctx)
    import subprocess
    import svc
    if not svc.running():
        return
    if os.environ.get("DISCORD_CHANNEL_ID"):
        # running inside a bot turn: restart after this reply, detached
        subprocess.run([os.path.join(HERE, "botcore", "bin", "request-restart"),
                        f"🔄 Updated discord-bot {old} -> {ctx.manifest.get('version')}, back online."])
        return
    ok, msg = svc.safe_restart(say=ctx.say)
    ctx.say(msg)


def uninstall(ctx, purge):
    import svc
    svc.uninstall()
    import shutil
    shutil.rmtree(svc.LASTGOOD, ignore_errors=True)


def doctor(ctx):
    import dbot
    return dbot.doctor_checks()
