"""Everything that differs per OS lives here: paths, file permissions,
command shims, PATH setup and the job scheduler."""
import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import sys

OS = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}.get(platform.system(), platform.system().lower())
IS_WIN = OS == "windows"
HOME = os.path.expanduser("~")


def _env_path(name, default):
    v = os.environ.get(name)
    return os.path.abspath(os.path.expanduser(v)) if v else default


if IS_WIN:
    _local = os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local")
    _roam = os.environ.get("APPDATA") or os.path.join(HOME, "AppData", "Roaming")
    KIT_HOME = _env_path("KIT_HOME", os.path.join(_local, "kit"))
    CONFIG_DIR = _env_path("KIT_CONFIG", os.path.join(_roam, "kit"))
    BIN_DIR = _env_path("KIT_BIN", os.path.join(KIT_HOME, "bin"))
else:
    _data = os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share")
    _conf = os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")
    KIT_HOME = _env_path("KIT_HOME", os.path.join(_data, "kit"))
    CONFIG_DIR = _env_path("KIT_CONFIG", os.path.join(_conf, "kit"))
    BIN_DIR = _env_path("KIT_BIN", os.path.join(HOME, ".local", "bin"))

CORE_DIR = os.path.join(KIT_HOME, "core")        # holds the kit/ package
MODULES_DIR = os.path.join(KIT_HOME, "modules")  # installed module copies
CACHE_DIR = os.path.join(KIT_HOME, "cache")      # latest repo snapshot
VENV_DIR = os.path.join(KIT_HOME, "venv")
STATE_FILE = os.path.join(CONFIG_DIR, "installed.json")


def venv_python(windowed=False):
    if IS_WIN:
        exe = "pythonw.exe" if windowed else "python.exe"
        return os.path.join(VENV_DIR, "Scripts", exe)
    return os.path.join(VENV_DIR, "bin", "python")


def python_exe():
    """The interpreter kit should hand to shims: the venv one if it exists."""
    p = venv_python()
    return p if os.path.exists(p) else sys.executable


# ---------- files ----------

def read_text(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        if default is not None:
            return default
        raise


def write_text(path, text, secret=False):
    """Atomic write. secret=True locks the file to the current user."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    if secret and not IS_WIN:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    os.replace(tmp, path)
    if secret:
        lock_down(path)


def lock_down(path):
    if IS_WIN:
        user = os.environ.get("USERNAME", "")
        if user:
            subprocess.run(["icacls", path, "/inheritance:r", "/grant:r", f"{user}:F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        os.chmod(path, 0o600)


def read_json(path, default=None):
    try:
        return json.loads(read_text(path))
    except FileNotFoundError:
        return {} if default is None else default


def write_json(path, data, secret=False):
    write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", secret=secret)


def rmtree(path):
    if os.path.isdir(path):
        def onerr(func, p, _):
            os.chmod(p, 0o700)
            func(p)
        shutil.rmtree(path, onerror=onerr)


def copy_tree(src, dst):
    """Replace dst with a copy of src (via a temp dir, so a failed copy
    leaves the old one in place)."""
    tmp = dst + ".new"
    rmtree(tmp)
    shutil.copytree(src, tmp, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))
    old = dst + ".old"
    rmtree(old)
    if os.path.exists(dst):
        os.replace(dst, old)
    os.replace(tmp, dst)
    rmtree(old)


# ---------- python packages for modules ----------

def find_uv():
    exe = "uv.exe" if IS_WIN else "uv"
    for c in (shutil.which("uv"), os.path.join(HOME, ".local", "bin", exe),
              os.path.join(HOME, ".cargo", "bin", exe)):
        if c and os.path.exists(c):
            return c
    return None


def pip_install(pkgs):
    """Install a module's Python packages into kit's private venv.
    Uses uv (what bootstrap installed), else the venv's own pip."""
    pkgs = list(pkgs or [])
    if not pkgs:
        return
    py = python_exe()
    uv = find_uv()
    if uv:
        cmd = [uv, "pip", "install", "--quiet", "--python", py, *pkgs]
    else:
        subprocess.run([py, "-m", "ensurepip", "--upgrade"], capture_output=True)
        cmd = [py, "-m", "pip", "install", "--quiet", *pkgs]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("installing " + ", ".join(pkgs) + " failed: " + (r.stderr or r.stdout).strip()[-400:])


# ---------- shims (commands on PATH) ----------

def shim_path(name):
    return os.path.join(BIN_DIR, name + (".cmd" if IS_WIN else ""))


def write_shim(name, args):
    """Create a command `name` that runs `python <args...>` from the venv."""
    os.makedirs(BIN_DIR, exist_ok=True)
    py = python_exe()
    path = shim_path(name)
    if IS_WIN:
        argstr = " ".join(f'"{a}"' for a in [py] + list(args))
        text = ("@echo off\r\nsetlocal\r\n"
                f'set "KIT_HOME={KIT_HOME}"\r\nset "KIT_CONFIG={CONFIG_DIR}"\r\n'
                "set PYTHONUTF8=1\r\n"
                f"{argstr} %*\r\n")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    else:
        q = shlex.quote
        text = ("#!/bin/sh\n# created by kit\n"
                f"KIT_HOME={q(KIT_HOME)}; KIT_CONFIG={q(CONFIG_DIR)}; export KIT_HOME KIT_CONFIG\n"
                f"exec {' '.join(q(a) for a in [py] + list(args))} \"$@\"\n")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(path, 0o755)
    return path


def remove_shim(name):
    p = shim_path(name)
    if os.path.exists(p):
        os.remove(p)


def bin_on_path():
    parts = [os.path.normcase(os.path.abspath(p)) for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    return os.path.normcase(os.path.abspath(BIN_DIR)) in parts


PATH_MARK = "# added by kit"


def ensure_path():
    """Put BIN_DIR on the user's PATH for future shells. Returns a hint
    string if the current shell needs refreshing, else None.
    KIT_NO_PATH=1 skips this (tests)."""
    if os.environ.get("KIT_NO_PATH"):
        return None if bin_on_path() else f"(KIT_NO_PATH set) add {BIN_DIR} to PATH yourself"
    if IS_WIN:
        return _ensure_path_windows()
    if bin_on_path():
        return None
    rel = BIN_DIR.replace(HOME, "$HOME", 1) if BIN_DIR.startswith(HOME) else BIN_DIR
    line = f'export PATH="{rel}:$PATH"  {PATH_MARK}\n'
    rcs = [".profile", ".bashrc"] + ([".zshrc"] if OS == "macos" or os.path.exists(os.path.join(HOME, ".zshrc")) else [])
    for rc in rcs:
        p = os.path.join(HOME, rc)
        cur = read_text(p, "")
        if rel in cur or BIN_DIR in cur:
            continue
        with open(p, "a", encoding="utf-8") as f:
            f.write(("\n" if cur and not cur.endswith("\n") else "") + line)
    return f'Open a new terminal, or run: export PATH="{BIN_DIR}:$PATH"'


def _ensure_path_windows():
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
        try:
            cur, typ = winreg.QueryValueEx(k, "Path")
        except FileNotFoundError:
            cur, typ = "", winreg.REG_EXPAND_SZ
        parts = [p for p in cur.split(";") if p]
        if any(os.path.normcase(p.rstrip("\\")) == os.path.normcase(BIN_DIR) for p in parts):
            return None if bin_on_path() else "Open a new terminal to use kit."
        winreg.SetValueEx(k, "Path", 0, typ, ";".join(parts + [BIN_DIR]))
    try:
        import ctypes
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 2, 5000, None)
    except Exception:
        pass
    return "Open a new terminal to use kit."


def remove_path_entry():
    if os.environ.get("KIT_NO_PATH"):
        return
    if IS_WIN:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
            try:
                cur, typ = winreg.QueryValueEx(k, "Path")
            except FileNotFoundError:
                return
            parts = [p for p in cur.split(";") if p and os.path.normcase(p.rstrip("\\")) != os.path.normcase(BIN_DIR)]
            winreg.SetValueEx(k, "Path", 0, typ, ";".join(parts))
        return
    for rc in (".profile", ".bashrc", ".zshrc"):
        p = os.path.join(HOME, rc)
        cur = read_text(p, "")
        if PATH_MARK in cur:
            write_text(p, "".join(l for l in cur.splitlines(True) if PATH_MARK not in l))


# ---------- scheduler ----------
# KIT_SCHEDULER=none|file makes this a no-op that records jobs in a json
# file instead (used by tests so they don't touch the real crontab).

def _fake_sched():
    return os.environ.get("KIT_SCHEDULER") in ("none", "file")


def _fake_file():
    return os.path.join(CONFIG_DIR, "schedule.test.json")


def scheduler_kind():
    if _fake_sched():
        return "test"
    if IS_WIN:
        return "Task Scheduler"
    if OS == "macos":
        return "launchd"
    if shutil.which("crontab"):
        return "cron"
    if shutil.which("systemctl"):
        return "systemd timer"
    return None


def schedule_argv(script, args):
    """argv a scheduled job should run for a module script."""
    if IS_WIN:
        return [venv_python(windowed=True), script] + list(args)
    return [python_exe(), script] + list(args)


def _job_env():
    return {"KIT_HOME": KIT_HOME, "KIT_CONFIG": CONFIG_DIR}


def schedule_add(name, argv, every_min):
    """Run argv every `every_min` minutes. Returns (ok, message)."""
    kind = scheduler_kind()
    try:
        if kind == "test":
            d = read_json(_fake_file())
            d[name] = {"argv": argv, "every_min": every_min}
            write_json(_fake_file(), d)
        elif kind == "cron":
            _cron_set(name, argv, every_min)
        elif kind == "systemd timer":
            _systemd_set(name, argv, every_min)
        elif kind == "launchd":
            return _launchd_set(name, argv, every_min)
        elif kind == "Task Scheduler":
            _schtasks_set(name, argv, every_min)
        else:
            return False, "no scheduler found (install cron)"
    except Exception as e:  # noqa: BLE001 - report, don't crash an install
        return False, f"{kind}: {e}"
    return True, f"every {every_min} min ({kind})"


def schedule_remove(name):
    kind = scheduler_kind()
    try:
        if kind == "test":
            d = read_json(_fake_file())
            d.pop(name, None)
            write_json(_fake_file(), d)
        elif kind == "cron":
            _cron_set(name, None, None)
        elif kind == "systemd timer":
            _systemd_remove(name)
        elif kind == "launchd":
            _launchd_remove(name)
        elif kind == "Task Scheduler":
            subprocess.run(["schtasks", "/Delete", "/F", "/TN", f"kit-{name}"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def schedule_info(name):
    """Short description of the job if it exists, else None."""
    kind = scheduler_kind()
    try:
        if kind == "test":
            j = read_json(_fake_file()).get(name)
            return f"every {j['every_min']} min (test)" if j else None
        if kind == "cron":
            for l in _crontab_lines():
                if l.rstrip().endswith(f"# kit:{name}"):
                    return "cron: " + l.split(" ", 5)[0].replace("*/", "every ") + " min"
            return None
        if kind == "systemd timer":
            r = subprocess.run(["systemctl", "--user", "is-active", f"kit-{name}.timer"], capture_output=True, text=True)
            return "systemd timer (active)" if r.stdout.strip() == "active" else None
        if kind == "launchd":
            p = _plist_path(name)
            if not os.path.exists(p):
                return None
            r = subprocess.run(["launchctl", "list", _label(name)], capture_output=True, text=True)
            return "launchd (loaded)" if r.returncode == 0 else "launchd (plist present, loads at next login)"
        if kind == "Task Scheduler":
            r = subprocess.run(["schtasks", "/Query", "/TN", f"kit-{name}"], capture_output=True, text=True)
            return "Task Scheduler" if r.returncode == 0 else None
    except Exception:
        return None
    return None


def _crontab_lines():
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return r.stdout.splitlines() if r.returncode == 0 else []


def _cron_set(name, argv, every_min):
    lines = [l for l in _crontab_lines() if not l.rstrip().endswith(f"# kit:{name}")]
    if argv:
        env = " ".join(f"{k}={shlex.quote(v)}" for k, v in _job_env().items())
        cmd = " ".join(shlex.quote(a) for a in argv)
        lines.append(f"*/{every_min} * * * * {env} {cmd} >/dev/null 2>&1 # kit:{name}")
    text = "\n".join(lines) + "\n" if lines else ""
    r = subprocess.run(["crontab", "-"], input=text, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "crontab failed")


def _systemd_dir():
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"), "systemd", "user")


def _systemd_set(name, argv, every_min):
    d = _systemd_dir()
    os.makedirs(d, exist_ok=True)
    env = "".join(f'Environment="{k}={v}"\n' for k, v in _job_env().items())
    write_text(os.path.join(d, f"kit-{name}.service"),
               f"[Unit]\nDescription=kit {name}\n\n[Service]\nType=oneshot\n{env}"
               f"ExecStart={' '.join(shlex.quote(a) for a in argv)}\n")
    write_text(os.path.join(d, f"kit-{name}.timer"),
               f"[Unit]\nDescription=kit {name} every {every_min} min\n\n[Timer]\nOnBootSec=1min\n"
               f"OnUnitActiveSec={every_min}min\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, capture_output=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", f"kit-{name}.timer"], check=True, capture_output=True)


def _systemd_remove(name):
    subprocess.run(["systemctl", "--user", "disable", "--now", f"kit-{name}.timer"], capture_output=True)
    for ext in ("service", "timer"):
        p = os.path.join(_systemd_dir(), f"kit-{name}.{ext}")
        if os.path.exists(p):
            os.remove(p)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)


def _label(name):
    return f"com.kit.{name}"


def _plist_path(name):
    return os.path.join(HOME, "Library", "LaunchAgents", _label(name) + ".plist")


def _launchd_set(name, argv, every_min):
    p = _plist_path(name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        plistlib.dump({"Label": _label(name), "ProgramArguments": argv, "StartInterval": every_min * 60,
                       "RunAtLoad": True, "EnvironmentVariables": _job_env(),
                       "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null"}, f)
    uid = str(os.getuid())
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{_label(name)}"], capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", p], capture_output=True, text=True)
    if r.returncode != 0:
        r = subprocess.run(["launchctl", "load", "-w", p], capture_output=True, text=True)
    if r.returncode != 0:
        return False, "launchd plist written, but it couldn't be loaded now (no desktop session?). It loads at next login."
    return True, f"every {every_min} min (launchd)"


def _launchd_remove(name):
    uid = str(os.getuid())
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{_label(name)}"], capture_output=True)
    p = _plist_path(name)
    if os.path.exists(p):
        subprocess.run(["launchctl", "unload", "-w", p], capture_output=True)
        os.remove(p)


def _schtasks_set(name, argv, every_min):
    tr = subprocess.list2cmdline(argv)
    if len(tr) > 261:
        raise RuntimeError("command too long for Task Scheduler")
    r = subprocess.run(["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", str(every_min),
                        "/TN", f"kit-{name}", "/TR", tr], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip())


# ---------- misc ----------

def default_editor():
    e = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if e:
        return shlex.split(e, posix=not IS_WIN)
    if IS_WIN:
        return ["notepad"]
    for c in ("nano", "vim", "vi"):
        if shutil.which(c):
            return [c]
    return ["vi"]


def fix_console():
    """Emoji-safe stdout/stderr on every platform."""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:3]) or (0,)
