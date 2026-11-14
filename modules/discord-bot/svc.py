"""Run the bot as a background service that comes back after a reboot.

  Linux : systemd user service kit-discord-bot (falls back to a plain
          background process + an @reboot cron line where there is no
          systemd user session, e.g. some containers)
  macOS : launchd agent com.kit.discord-bot

Also: safe restart (wait for replies in progress, check it boots, roll back
to the last version that booted if it doesn't)."""
import os
import pathlib
import plistlib
import shutil
import signal
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from botcore import paths  # noqa: E402

PY = sys.executable
RUN_BOT = HERE / "run_bot.py"
UNIT = "kit-discord-bot.service"
LABEL = "com.kit.discord-bot"
LOG = paths.LOGS / "bot.log"
READY = paths.STATE / "bot-ready"
PIDFILE = paths.RUN / "bot.pid"
KIT_HOME = HERE.parent.parent                     # .../kit (modules/discord-bot)
LASTGOOD = KIT_HOME / "backup" / "discord-bot.last-good"
CRON_TAG = "# kit:discord-bot"


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def kind():
    forced = os.environ.get("DBOT_SERVICE")
    if forced in ("systemd", "launchd", "plain"):
        return forced
    if sys.platform == "darwin":
        return "launchd"
    if shutil.which("systemctl") and _run(["systemctl", "--user", "show-environment"]).returncode == 0:
        return "systemd"
    return "plain"


def claude_path():
    c = shutil.which("claude")
    if c:
        return c
    for p in (pathlib.Path.home() / ".local" / "bin" / "claude",
              pathlib.Path.home() / ".claude" / "local" / "claude"):
        if p.exists():
            return str(p)
    return None


def service_env():
    dirs = []
    c = claude_path()
    if c:
        dirs.append(os.path.dirname(c))
    dirs += [str(pathlib.Path.home() / ".local" / "bin"), "/opt/homebrew/bin",
             "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    seen, path = set(), []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            path.append(d)
    return {"BOT_HOME": str(paths.HOME), "PATH": os.pathsep.join(path),
            "HOME": str(pathlib.Path.home()), "LANG": os.environ.get("LANG") or "C.UTF-8",
            "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"}


# ---------- systemd ----------

def _unit_path():
    base = os.environ.get("XDG_CONFIG_HOME") or str(pathlib.Path.home() / ".config")
    return pathlib.Path(base) / "systemd" / "user" / UNIT


def _systemd_install():
    env = "".join(f'Environment="{k}={v}"\n' for k, v in service_env().items())
    text = (
        "[Unit]\nDescription=Claude on Discord (kit discord-bot)\n"
        "After=network-online.target\nWants=network-online.target\n\n"
        "[Service]\n"
        f'ExecStart="{PY}" "{RUN_BOT}"\n'
        f"{env}"
        f"WorkingDirectory={pathlib.Path.home()}\n"
        f"StandardOutput=append:{LOG}\nStandardError=append:{LOG}\n"
        "Restart=on-failure\nRestartSec=10\n\n"
        "[Install]\nWantedBy=default.target\n")
    p = _unit_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    _run(["systemctl", "--user", "daemon-reload"])
    _run(["systemctl", "--user", "enable", UNIT])
    lg = _run(["loginctl", "enable-linger", os.environ.get("USER") or ""])
    if lg.returncode != 0:
        return ("Couldn't turn on 'linger', so the bot only runs while you're logged in. "
                f"Fix: sudo loginctl enable-linger {os.environ.get('USER', '$USER')}")
    return None


# ---------- launchd ----------

def _plist_path():
    return pathlib.Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _domain():
    return f"gui/{os.getuid()}"


def _launchd_install():
    p = _plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"Label": LABEL, "ProgramArguments": [PY, str(RUN_BOT)],
            "EnvironmentVariables": service_env(), "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 10,
            "WorkingDirectory": str(pathlib.Path.home()),
            "StandardOutPath": str(LOG), "StandardErrorPath": str(LOG)}
    with open(p, "wb") as f:
        plistlib.dump(data, f)
    return None


def _launchd_loaded():
    return _run(["launchctl", "print", f"{_domain()}/{LABEL}"]).returncode == 0


# ---------- plain background process ----------

def _plain_pid():
    try:
        pid = int(PIDFILE.read_text().strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None
    try:
        cmd = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
        if "run_bot.py" not in cmd:
            return None
    except OSError:
        pass
    return pid


def _plain_start():
    if _plain_pid():
        return
    env = {**os.environ, **service_env()}
    with open(LOG, "a") as log:
        p = subprocess.Popen([PY, str(RUN_BOT)], stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                             env=env, cwd=str(pathlib.Path.home()), start_new_session=True)
    PIDFILE.write_text(str(p.pid))


def _plain_stop():
    pid = _plain_pid()
    if not pid:
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        time.sleep(0.2)
        if not _plain_pid():
            break
    else:
        os.kill(pid, signal.SIGKILL)
    PIDFILE.unlink(missing_ok=True)


def _cron_reboot(add):
    cur = _run(["crontab", "-l"])
    lines = [l for l in (cur.stdout.splitlines() if cur.returncode == 0 else []) if CRON_TAG not in l]
    if add:
        lines.append(f'@reboot "{PY}" "{HERE / "dbot.py"}" start {CRON_TAG}')
    if shutil.which("crontab"):
        _run(["crontab", "-"], input="\n".join(lines) + "\n")
        return True
    return False


# ---------- public ----------

def install():
    """Register the service (starts at boot/login). Returns a warning or None."""
    k = kind()
    if k == "systemd":
        return _systemd_install()
    if k == "launchd":
        return _launchd_install()
    if not _cron_reboot(True):
        return "No systemd user session or cron here, so the bot won't start by itself after a reboot (run: dbot start)."
    return None


def uninstall():
    stop()
    k = kind()
    if k == "systemd":
        _run(["systemctl", "--user", "disable", UNIT])
        _unit_path().unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"])
    elif k == "launchd":
        _plist_path().unlink(missing_ok=True)
    _cron_reboot(False)


def start():
    k = kind()
    if k == "systemd":
        if not _unit_path().exists():
            install()
        r = _run(["systemctl", "--user", "start", UNIT])
        return r.returncode == 0, r.stderr.strip()
    if k == "launchd":
        if not _plist_path().exists():
            install()
        if not _launchd_loaded():
            r = _run(["launchctl", "bootstrap", _domain(), str(_plist_path())])
            return r.returncode == 0, r.stderr.strip()
        r = _run(["launchctl", "kickstart", f"{_domain()}/{LABEL}"])
        return r.returncode == 0, r.stderr.strip()
    _plain_start()
    return True, ""


def stop():
    k = kind()
    if k == "systemd":
        _run(["systemctl", "--user", "stop", UNIT])
    elif k == "launchd":
        if _launchd_loaded():
            _run(["launchctl", "bootout", f"{_domain()}/{LABEL}"])
    else:
        _plain_stop()


def restart_raw():
    k = kind()
    if k == "systemd":
        r = _run(["systemctl", "--user", "restart", UNIT])
        return r.returncode == 0, r.stderr.strip()
    if k == "launchd" and _launchd_loaded():
        r = _run(["launchctl", "kickstart", "-k", f"{_domain()}/{LABEL}"])
        return r.returncode == 0, r.stderr.strip()
    stop()
    return start()


def pid():
    k = kind()
    if k == "systemd":
        r = _run(["systemctl", "--user", "show", "-p", "MainPID", "--value", UNIT])
        v = r.stdout.strip()
        return int(v) if v.isdigit() and v != "0" else None
    if k == "launchd":
        r = _run(["launchctl", "print", f"{_domain()}/{LABEL}"])
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith("pid = "):
                v = line[6:].strip()
                return int(v) if v.isdigit() else None
        return None
    return _plain_pid()


def running():
    return pid() is not None


def installed():
    k = kind()
    if k == "systemd":
        return _unit_path().exists()
    if k == "launchd":
        return _plist_path().exists()
    return True


def busy_turns():
    """Claude replies the bot is running right now (its child processes)."""
    p = pid()
    if not p:
        return 0
    r = _run(["pgrep", "-P", str(p), "-f", "output-format stream-json"])
    return len(r.stdout.split()) if r.returncode == 0 else 0


# ---------- precheck, snapshot, safe restart ----------

def precheck():
    files = [str(RUN_BOT)] + [str(f) for f in sorted((HERE / "botcore").glob("*.py"))]
    r = _run([PY, "-m", "py_compile", *files])
    if r.returncode != 0:
        return False, "compile failed:\n" + (r.stderr or r.stdout).strip()[-1200:]
    code = ("import sys, pkgutil, importlib; sys.path.insert(0, sys.argv[1]); import botcore\n"
            "for m in pkgutil.iter_modules(botcore.__path__):\n"
            "    importlib.import_module('botcore.' + m.name)")
    r = _run([PY, "-c", code, str(HERE)], env={**os.environ, **service_env()}, timeout=90)
    if r.returncode != 0:
        return False, "import failed:\n" + (r.stderr or r.stdout).strip()[-1200:]
    return True, "ok"


def snapshot():
    tmp = LASTGOOD.with_name(LASTGOOD.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(HERE, tmp, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.rmtree(LASTGOOD, ignore_errors=True)
    tmp.rename(LASTGOOD)


def restore():
    if not LASTGOOD.exists():
        return False
    for item in HERE.iterdir():
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        else:
            item.unlink(missing_ok=True)
    for item in LASTGOOD.iterdir():
        dst = HERE / item.name
        if item.is_dir():
            shutil.copytree(item, dst)
        else:
            shutil.copy2(item, dst)
    return True


def booted(t0, wait=120):
    end = time.time() + wait
    while time.time() < end:
        time.sleep(2)
        try:
            ok = READY.stat().st_mtime >= t0
        except OSError:
            ok = False
        if ok and running():
            time.sleep(10)                 # must stay up past on_ready
            return running()
    return False


def wait_for_turns(say=print, limit=50 * 60):
    n = busy_turns()
    if not n:
        return
    say(f"Waiting for {n} reply{'s' if n != 1 else ''} in progress to finish (dbot restart --now skips this)...")
    end = time.time() + limit
    while time.time() < end and busy_turns():
        time.sleep(5)


def _handover(msg):
    """Rewrite or create the post-restart note so the boot message says what happened."""
    import json
    from botcore.config import conf
    f = paths.STATE / "restart_handover.json"
    try:
        d = json.loads(f.read_text())
    except (OSError, ValueError):
        d = {"channel_id": conf.get("BOT_CHANNEL_ID")}
    if d.get("channel_id"):
        intended = d.get("content")
        d["content"] = msg + (f"\n-# intended: {intended[:300]}" if intended else "")
        d["ts"] = time.time()
        f.write_text(json.dumps(d))


def safe_restart(wait=True, say=print):
    ok, msg = precheck()
    if not ok:
        return False, f"Not restarted: the new code fails its check ({msg})"
    if wait:
        wait_for_turns(say)
    t0 = time.time()
    if running():
        restart_raw()
    else:
        start()
    if booted(t0):
        snapshot()
        return True, "Bot restarted and back online."
    if restore():
        _handover("⚠️ **Rolled back.** The new bot code failed to start, so the last good version is running again.")
        t1 = time.time()
        restart_raw()
        if booted(t1):
            return False, "The bot didn't come up, so it was rolled back to the last version that worked."
    return False, "The bot didn't come up. Check: dbot logs (and dbot doctor)."
