"""dbot: control the Claude Discord bot (kit module discord-bot)."""
import argparse
import os
import pathlib
import shutil
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import setup_lib as L  # noqa: E402
import svc  # noqa: E402
from botcore import paths  # noqa: E402

HELP = """\
dbot: Claude on Discord

  dbot status                 running? mode, channel, owner
  dbot start | stop
  dbot restart [--now]        safe restart: waits for replies in progress,
                              rolls back if the bot doesn't come back up
  dbot logs [-f] [-n 50]      bot log (-f follows it)
  dbot update                 get the latest version, then safe restart
  dbot rollback               go back to the last version that started fine

  dbot config                 show settings (token hidden)
  dbot set KEY VALUE          change one, e.g. dbot set mode safe
  dbot edit                   edit the settings file in a text editor
  dbot allow USER_ID          let another Discord user talk to the bot
  dbot deny USER_ID           remove them again
  dbot mode full|safe         what Claude may do on this computer
  dbot doctor                 check everything (token, channel, Claude login)

  kit configure discord-bot   run the full setup again (token, channel, owner)

Settings keys: """ + ", ".join(L.KEYS) + "\n"


def say(msg=""):
    print(msg, flush=True)


def _apply(restart, quiet=False):
    if not restart:
        say("Saved. Takes effect after: dbot restart")
        return 0
    if not svc.running():
        say("Saved. The bot isn't running (dbot start).")
        return 0
    if os.environ.get("DISCORD_CHANNEL_ID"):
        # called from inside a bot turn: restart detached after this reply
        subprocess.run([str(paths.TOOLS / "request-restart"), "⚙️ Settings changed, back online."])
        return 0
    ok, msg = svc.safe_restart(say=say)
    say(msg)
    return 0 if ok else 1


# ---------- commands ----------

def cmd_status(a):
    conf = L.load_conf()
    pid = svc.pid()
    state = f"running (pid {pid})" if pid else "stopped"
    busy = svc.busy_turns() if pid else 0
    say(f"Bot: {state}" + (f", {busy} reply in progress" if busy else ""))
    say(f"Service: {svc.kind()}")
    say(f"Mode: {conf.get('MODE')}  Model: {conf.get('BIG_MODEL')}")
    say(f"Channel: {conf.get('BOT_CHANNEL_ID') or 'not set'}  Owner: {conf.get('BOT_OWNER_ID') or 'not set'}")
    try:
        age = time.time() - (paths.STATE / "bot-ready").stat().st_mtime
        say(f"Last connected: {int(age // 60)} min ago")
    except OSError:
        pass
    return 0


def cmd_start(a):
    if svc.running():
        say("Already running.")
        return 0
    t0 = time.time()
    ok, err = svc.start()
    if not ok:
        say(f"Couldn't start: {err}")
        return 1
    say("Starting...")
    if svc.booted(t0, wait=90):
        say("Bot is online.")
        return 0
    say("It didn't connect yet. Check: dbot logs")
    return 1


def cmd_stop(a):
    svc.stop()
    say("Stopped.")
    return 0


def cmd_restart(a):
    if a.quiet:
        ok, msg = svc.safe_restart(wait=not a.now, say=lambda m: None)
        print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)
        return 0 if ok else 1
    ok, msg = svc.safe_restart(wait=not a.now, say=say)
    say(msg)
    return 0 if ok else 1


def cmd_logs(a):
    if not svc.LOG.exists():
        say("No log yet.")
        return 0
    if a.follow and shutil.which("tail"):
        os.execvp("tail", ["tail", "-n", str(a.n), "-f", str(svc.LOG)])
    lines = svc.LOG.read_text(errors="replace").splitlines()[-a.n:]
    say("\n".join(lines))
    return 0


def cmd_update(a):
    # kit downloads the new version; the module's upgrade hook then does the
    # safe restart (and rolls back if the new version doesn't start)
    kit = shutil.which("kit") or str(pathlib.Path.home() / ".local" / "bin" / "kit")
    return subprocess.run([kit, "update", "discord-bot", "-y"]).returncode


def cmd_rollback(a):
    if not svc.LASTGOOD.exists():
        say("No saved version to roll back to yet (one is saved after each good start).")
        return 1
    svc.restore()
    t0 = time.time()
    svc.restart_raw() if svc.running() else svc.start()
    ok = svc.booted(t0)
    say("Rolled back and online." if ok else "Rolled back, but it didn't connect. Check: dbot logs")
    return 0 if ok else 1


def cmd_config(a):
    conf = L.load_conf()
    width = max(len(k) for k in L.KEYS)
    for short, key in L.KEYS.items():
        say(f"  {short:<{width}}  {conf.get(key, '') or '-':<22} {L.LABELS[key]}")
    say(f"  {'token':<{width}}  {L.mask(L.load_token()):<22} change with: kit configure discord-bot")
    say("")
    say(f"Settings: {paths.CONF_FILE}")
    say(f"Token:    {paths.SECRETS_FILE} (only your user can read it)")
    say(f"Logs:     {svc.LOG}")
    return 0


def _set_many(pairs, restart):
    conf = L.load_conf()
    for short, value in pairs:
        key = L.KEYS.get(short.lower(), short.upper())
        if key not in L.LABELS:
            say(f"Unknown setting {short!r}. Keys: {', '.join(L.KEYS)}")
            return 2
        if key == "MODE" and value == "full" and conf.get("MODE") != "full":
            say("Full access: Claude can edit files and run commands on this computer when the owner asks.")
        ok, v = L.VALIDATE.get(key, lambda x: (True, x))(value)
        if not ok:
            say(f"{short}: {v}")
            return 2
        conf[key] = v
        say(f"{short} = {v}")
    L.save_conf(conf)
    return _apply(restart)


def cmd_set(a):
    if len(a.args) < 2:
        say("usage: dbot set KEY VALUE   (keys: " + ", ".join(L.KEYS) + ")")
        return 2
    return _set_many([(a.args[0], " ".join(a.args[1:]))], not a.no_restart)


def cmd_mode(a):
    return _set_many([("mode", a.mode)], not a.no_restart)


def cmd_allow(a, remove=False):
    ok, uid = L._digits(a.user)
    if not ok:
        say(uid)
        return 2
    conf = L.load_conf()
    cur = [u for u in (conf.get("BOT_ALLOWED_USERS") or "").split(",") if u]
    if remove:
        if uid not in cur:
            say(f"{uid} isn't on the list.")
            return 0
        cur.remove(uid)
    elif uid not in cur:
        cur.append(uid)
    conf["BOT_ALLOWED_USERS"] = ",".join(cur)
    L.save_conf(conf)
    say(("Removed " if remove else "Allowed ") + uid + ". They can now " + ("no longer " if remove else "") + "talk to Claude through the bot.")
    if not remove:
        say("Careful: in full mode they can make Claude change things on this computer.")
    return _apply(not a.no_restart)


def cmd_edit(a):
    try:
        from kit import system as ksys
        editor = ksys.default_editor()
    except Exception:  # noqa: BLE001
        editor = [os.environ.get("EDITOR") or "nano"]
    backup = paths.CONF_FILE.read_text() if paths.CONF_FILE.exists() else ""
    if not paths.CONF_FILE.exists():
        L.save_conf(L.load_conf())
    subprocess.call(editor + [str(paths.CONF_FILE)])
    conf = L.read_kv(paths.CONF_FILE)
    bad = []
    for key, v in conf.items():
        if key in L.VALIDATE and v:
            ok, msg = L.VALIDATE[key](v)
            if not ok:
                bad.append(f"{key}: {msg}")
    for key in ("BOT_CHANNEL_ID", "BOT_OWNER_ID"):
        if not conf.get(key):
            bad.append(f"{key} is required")
    if bad:
        paths.CONF_FILE.write_text(backup)
        say("Not saved, the old settings are back:\n  " + "\n  ".join(bad))
        return 1
    return _apply(True)


def doctor_checks():
    out = []
    inst, logged, detail = L.claude_status()
    out.append((inst and logged, f"Claude Code: {detail}" + ("" if logged else " (run: claude, then /login)")))
    token = L.load_token()
    conf = L.load_conf()
    if not token:
        out.append((False, "Discord token: not set (kit configure discord-bot)"))
    else:
        ok, info = L.check_token(token)
        if not ok:
            out.append((False, f"Discord token: {info}"))
        else:
            out.append((True, f"Discord bot: {info['bot']}"))
            out.append((info["content_intent"], "Message Content intent: " + (
                "on" if info["content_intent"] else f"OFF (turn it on: {L.PORTAL} > your app > Bot)")))
            ch = conf.get("BOT_CHANNEL_ID")
            if ch:
                try:
                    c = L.api(token, f"/channels/{ch}")
                    out.append((True, f"Channel: #{c.get('name', ch)}"))
                except L.DiscordError as e:
                    out.append((False, f"Channel {ch}: {e}"))
    out.append((bool(conf.get("BOT_CHANNEL_ID")), "Channel set" if conf.get("BOT_CHANNEL_ID") else "No channel (dbot set channel ID)"))
    out.append((bool(conf.get("BOT_OWNER_ID")), "Owner set" if conf.get("BOT_OWNER_ID") else "No owner: nobody can use the bot (dbot set owner ID)"))
    out.append((svc.running(), f"Service ({svc.kind()}): " + ("running" if svc.running() else "stopped (dbot start)")))
    out.append((True, f"Mode: {conf.get('MODE')}"))
    return out


def cmd_doctor(a):
    bad = 0
    for ok, msg in doctor_checks():
        say(("  ok   " if ok else "  FAIL ") + msg)
        bad += not ok
    return 1 if bad else 0


def cmd_precheck(a):
    ok, msg = svc.precheck()
    say("precheck: " + msg)
    return 0 if ok else 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "help"):
        say(HELP)
        return 0
    ap = argparse.ArgumentParser(prog="dbot", add_help=False)
    sub = ap.add_subparsers(dest="cmd")
    for n in ("status", "start", "stop", "update", "rollback", "config", "edit", "doctor", "precheck"):
        sub.add_parser(n)
    p = sub.add_parser("restart")
    p.add_argument("--now", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p = sub.add_parser("logs")
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("-n", type=int, default=50)
    p = sub.add_parser("set")
    p.add_argument("args", nargs="*")
    p.add_argument("--no-restart", action="store_true")
    p = sub.add_parser("mode")
    p.add_argument("mode", choices=["full", "safe"])
    p.add_argument("--no-restart", action="store_true")
    for n in ("allow", "deny"):
        p = sub.add_parser(n)
        p.add_argument("user")
        p.add_argument("--no-restart", action="store_true")
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        say(HELP)
        return 2
    fn = {"status": cmd_status, "start": cmd_start, "stop": cmd_stop, "restart": cmd_restart,
          "logs": cmd_logs, "update": cmd_update, "rollback": cmd_rollback, "config": cmd_config,
          "set": cmd_set, "mode": cmd_mode, "allow": cmd_allow,
          "deny": lambda a: cmd_allow(a, remove=True), "edit": cmd_edit, "doctor": cmd_doctor,
          "precheck": cmd_precheck}[a.cmd]
    try:
        return fn(a)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
