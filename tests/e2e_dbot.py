"""discord-bot module test (Linux/macOS), run after bootstrap, before e2e.py.

    python tests/e2e_dbot.py

Fake Discord REST API for setup; the bot itself can't log in (fake token),
so the test checks setup, settings, access rules, the service wiring and
that a failed start is reported. Nothing talks to the real Discord.
"""
import json
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from kit import system  # noqa: E402
import e2e  # noqa: E402

run, check, cmd, failures = e2e.run, e2e.check, e2e.cmd, e2e.failures
BOT_HOME = os.path.join(system.CONFIG_DIR, "discord-bot")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def conf():
    out = {}
    with open(os.path.join(BOT_HOME, "bot.conf")) as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                out[k] = v
    return out


def main():
    port = free_port()
    srv = subprocess.Popen([sys.executable, os.path.join(HERE, "fake_discord.py"), str(port)])
    e2e.ENV.update(DBOT_DISCORD_API=f"http://127.0.0.1:{port}", DBOT_SERVICE="plain")
    try:
        for _ in range(300):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise SystemExit("fake discord didn't start")

        run("kit", "install", "discord-bot", "-y", "--offline", "--set", "token=wrong.token.here",
            "--set", "install_claude=no", ok=False)
        check(not os.path.exists(cmd("dbot")), "bad token aborts the install")

        out = run("kit", "install", "discord-bot", "-y", "--offline", "--set", "token=aaa.bbb.ccc",
                  "--set", "install_claude=no", "--set", "channel=300000000000000003",
                  "--set", "owner=400000000000000004", "--set", "mode=safe", "--set", "start=no")
        check(os.path.exists(cmd("dbot")), "dbot command installed")
        check('bot "testbot"' in out, "token checked against Discord")
        check("discord.com/oauth2/authorize?client_id=100000000000000001" in out, "invite link printed")
        sec = os.path.join(BOT_HOME, "secrets.env")
        check(os.path.exists(sec) and (os.stat(sec).st_mode & 0o077) == 0, "token file is owner-only")
        check("aaa.bbb.ccc" not in open(os.path.join(BOT_HOME, "bot.conf")).read(), "token not in settings file")
        c = conf()
        check(c.get("BOT_CHANNEL_ID") == "300000000000000003" and c.get("BOT_OWNER_ID") == "400000000000000004",
              "channel and owner saved")
        check(c.get("BOT_OWNER_NAME") == "Test Owner", "owner name looked up from Discord")
        check(c.get("MODE") == "safe", "mode saved")

        out = run("dbot", "config")
        check("aaa.bbb.ccc" not in out and "...b.ccc"[-4:] in out, "dbot config hides the token")
        run("dbot", "set", "mode", "full", "--no-restart")
        check(conf().get("MODE") == "full", "dbot set mode full")
        run("dbot", "set", "mode", "banana", "--no-restart", ok=False)
        check(conf().get("MODE") == "full", "invalid value refused")
        run("dbot", "set", "name", "Sam", "Smith", "--no-restart")
        check(conf().get("BOT_OWNER_NAME") == "Sam Smith", "multi-word value without quotes")
        run("dbot", "allow", "500000000000000005", "--no-restart")
        check(conf().get("BOT_ALLOWED_USERS") == "500000000000000005", "dbot allow")
        run("dbot", "deny", "500000000000000005", "--no-restart")
        check(not conf().get("BOT_ALLOWED_USERS"), "dbot deny")
        out = run("dbot", "precheck")
        check("precheck: ok" in out, "bot code passes its own precheck")

        # access rules, from the bot's own code
        py = system.python_exe()
        mod = os.path.join(system.MODULES_DIR, "discord-bot")
        probe = ("import sys, json; sys.path.insert(0, sys.argv[1]); from botcore import access;"
                 "print(json.dumps({'owner': access.allowed(400000000000000004), 'other': access.allowed(1),"
                 "'flags': access.claude_flags(), 'env_has_token': 'DISCORD_TOKEN' in access.claude_env()}))")
        r = subprocess.run([py, "-c", probe, mod], capture_output=True, text=True,
                           env={**e2e.ENV, "BOT_HOME": BOT_HOME, "DISCORD_TOKEN": "leak"})
        print(r.stdout, r.stderr)
        d = json.loads(r.stdout.strip().splitlines()[-1])
        check(d["owner"] and not d["other"], "only the owner is allowed")
        check("--dangerously-skip-permissions" in d["flags"], "full mode skips permission prompts")
        settings = json.loads(d["flags"][d["flags"].index("--settings") + 1])
        check(any("secrets.env" in r for r in settings["permissions"]["deny"]), "Claude is denied the token file")
        check(not d["env_has_token"], "token never passed to Claude's environment")
        run("dbot", "mode", "safe", "--no-restart")
        r = subprocess.run([py, "-c", probe, mod], capture_output=True, text=True,
                           env={**e2e.ENV, "BOT_HOME": BOT_HOME})
        d = json.loads(r.stdout.strip().splitlines()[-1])
        check("--dangerously-skip-permissions" not in d["flags"]
              and any("Bash(discord-update:*)" in f for f in d["flags"]),
              "safe mode: no skip-permissions, only the bot's own Bash tools")

        # starting with a fake token: must fail cleanly and say so
        out = run("dbot", "start", ok=False)
        check("didn't connect" in out or "Couldn't start" in out, "failed start is reported")
        time.sleep(2)
        log = open(os.path.join(BOT_HOME, "logs", "bot.log")).read() if os.path.exists(os.path.join(BOT_HOME, "logs", "bot.log")) else ""
        check("aaa.bbb.ccc" not in log, "token not written to the log")
        run("dbot", "stop")
        out = run("dbot", "status")
        check("stopped" in out, "dbot status after stop")
        run("dbot", "doctor", ok=None)

        run("kit", "remove", "discord-bot", "-y", "--purge")
        check(not os.path.exists(cmd("dbot")), "dbot removed")
        check(not os.path.exists(BOT_HOME), "--purge deleted settings and token")
    finally:
        srv.terminate()
        subprocess.run([cmd("dbot"), "stop"], capture_output=True) if os.path.exists(cmd("dbot")) else None
    print(f"\n{len(failures)} failure(s)")
    for f in failures:
        print(" - " + f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
