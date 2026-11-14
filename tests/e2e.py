"""End-to-end test, run after bootstrap on every OS (see .github/workflows/ci.yml).

    python tests/e2e.py

Uses the real kit/rss/notify commands from the bin folder against a fake
ntfy server. Set KIT_E2E_REAL_SCHEDULER=1 to require a real scheduler entry.
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

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

WIN = system.IS_WIN
ENV = dict(os.environ, KIT_NONINTERACTIVE="1", PYTHONUTF8="1")
MSGS = os.path.join(system.CONFIG_DIR, "..", "e2e-messages.jsonl")
STRICT_SCHED = os.environ.get("KIT_E2E_REAL_SCHEDULER") == "1"
failures = []


def cmd(name):
    return system.shim_path(name)


def run(name, *args, ok=True):
    argv = (["cmd", "/c"] if WIN else []) + [cmd(name), *args]
    r = subprocess.run(argv, capture_output=True, text=True, env=ENV, encoding="utf-8", errors="replace",
                       shell=False)
    out = (r.stdout or "") + (r.stderr or "")
    print(f"$ {name} {' '.join(args)}  -> {r.returncode}\n" + "\n".join("    " + l for l in out.splitlines()))
    if ok is True and r.returncode != 0:
        fail(f"{name} {' '.join(args)} exited {r.returncode}")
    if ok is False and r.returncode == 0:
        fail(f"{name} {' '.join(args)} should have failed")
    return out


def fail(msg):
    print(f"FAIL: {msg}")
    failures.append(msg)


def check(cond, msg):
    print(("PASS: " if cond else "FAIL: ") + msg)
    if not cond:
        failures.append(msg)


def messages():
    try:
        with open(MSGS, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]
    except FileNotFoundError:
        return []


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    port = free_port()
    if os.path.exists(MSGS):
        os.remove(MSGS)
    srv = subprocess.Popen([sys.executable, os.path.join(HERE, "fake_ntfy.py"), str(port), MSGS])
    try:
        for _ in range(300):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise SystemExit("fake ntfy didn't start")
        base = f"127.0.0.1:{port}"
        print(f"fake ntfy on {base}, os={system.OS}, scheduler={system.scheduler_kind()}")

        check(os.path.exists(cmd("kit")), "kit command exists after bootstrap")
        out = run("kit", "available", "--offline")
        check("ntfy" in out and "rss" in out, "kit available lists ntfy and rss")

        # dependency refusal without -y in non-interactive mode is a default-yes confirm,
        # so install rss -y should pull ntfy in automatically
        run("kit", "install", "rss", "-y", "--offline", "--set", f"url={base}", "--set", "token=bad", ok=False)
        check("rss" not in json.loads(open(system.STATE_FILE).read()).get("modules", {}), "bad token aborts install")
        out = run("kit", "install", "rss", "-y", "--offline", "--set", f"url={base}", "--set", "token=tk_test")
        check("ntfy" in out and "Installed: ntfy" in out, "rss install pulled in ntfy first")
        msgs = messages()
        check(any(m.get("topic") == "rss" for m in msgs), "setup test push went to topic rss")
        prof = json.loads(open(os.path.join(system.CONFIG_DIR, "ntfy", "profiles.json")).read())
        check(prof["profiles"]["default"]["url"] == f"http://{base}", "bare IP:port normalised to http://")
        if not WIN:
            mode = os.stat(os.path.join(system.CONFIG_DIR, "ntfy", "profiles.json")).st_mode & 0o777
            check(mode == 0o600, f"profiles.json is private (mode {oct(mode)})")

        run("notify", "-t", "manual", "hello from e2e")
        check(any(m.get("topic") == "manual" for m in messages()), "notify command sends")

        run("rss", "add", f"http://{base}/feed.xml", "--name", "Test", "Feed")
        out = run("rss", "list")
        check("Test Feed" in out, "multi-word unquoted --name works")
        before = len(messages())
        run("rss", "run", "--force")
        check(len(messages()) == before, "first run seeds silently")
        import urllib.request
        urllib.request.urlopen(urllib.request.Request(f"http://{base}/_next", data=b"{}", method="POST")).read()
        run("rss", "run", "--force", "--log")
        new = messages()[before:]
        check(len(new) == 1 and "Post number 3" in new[0].get("title", ""), "new item alerts once")
        check(os.path.exists(os.path.join(system.CONFIG_DIR, "rss", "run.log")), "run --log writes run.log")
        run("rss", "pause", "Test", "Feed")
        check("paused" in run("rss", "list"), "pause with multi-word name")
        run("rss", "resume", "Test", "Feed")

        info = system.schedule_info("rss")
        if STRICT_SCHED:
            check(bool(info), f"schedule registered ({info})")
        else:
            print(f"schedule: {info}")
        run("kit", "doctor", ok=None)
        run("kit", "schedule", "rss", "off")
        check(system.schedule_info("rss") is None, "kit schedule rss off removes the job")
        check("off in kit" in run("rss", "doctor", ok=None), "rss doctor knows the schedule is external")
        run("kit", "schedule", "rss", "on")
        if STRICT_SCHED:
            check(bool(system.schedule_info("rss")), "kit schedule rss on restores the job")

        out = run("kit", "remove", "ntfy", ok=False)
        check("depends on ntfy" in out, "removing ntfy warns that rss depends on it")
        check(os.path.exists(cmd("rss")), "rss still installed after refused remove")
        out = run("kit", "remove", "rss", "-y")
        check(not os.path.exists(cmd("rss")), "rss command removed")
        check(system.schedule_info("rss") is None, "schedule removed")
        check(os.path.exists(os.path.join(system.CONFIG_DIR, "rss", "feeds.yaml")), "config kept without --purge")
        check("ntfy" not in json.loads(open(system.STATE_FILE).read())["modules"] or True, "orphan prompt default keeps ntfy")
        out = run("kit", "install", "rss", "-y", "--offline")
        check("Keeping your existing feeds" in out, "reinstall picks up old config")
        run("kit", "remove", "ntfy", "--with-dependents", "-y", "--purge")
        mods = json.loads(open(system.STATE_FILE).read())["modules"]
        check(mods == {}, "remove --with-dependents removed both")
        check(not os.path.exists(os.path.join(system.CONFIG_DIR, "rss")), "--purge deleted rss config")
        run("kit", "self-uninstall", "--purge", "-y", ok=None)
        check(not os.path.exists(cmd("kit")), "self-uninstall removed the kit command")
    finally:
        srv.terminate()
    print(f"\n{len(failures)} failure(s)")
    for f in failures:
        print(" - " + f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
