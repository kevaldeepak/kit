"""Plain text prompts. Every prompt can be answered up front with
--set key=value, and --yes accepts defaults, so nothing ever blocks a
script or CI run."""
import getpass
import os
import sys


def interactive():
    if os.environ.get("KIT_NONINTERACTIVE"):
        return False
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


class Abort(SystemExit):
    pass


def say(msg=""):
    print(msg, flush=True)


def warn(msg):
    print(f"! {msg}", flush=True)


def _need(label, key):
    hint = f" (pass --set {key}=...)" if key else ""
    raise Abort(f"{label}: no answer and not running in a terminal{hint}")


def ask(label, default=None, secret=False, validate=None, allow_empty=False, preset=None, key=None, assume=False):
    """Ask for a value. `preset` (from --set) skips the prompt. `validate`
    returns (ok, value_or_error). `assume` (from --yes) takes the default."""
    def check(v):
        if validate:
            return validate(v)
        return True, v

    if preset is not None:
        ok, v = check(preset)
        if not ok:
            raise Abort(f"{label}: {v}")
        return v
    if assume or not interactive():
        if default is not None and (default != "" or allow_empty):
            ok, v = check(default)
            if ok:
                return v
            raise Abort(f"{label}: {v}")
        if allow_empty:
            return ""
        _need(label, key)
    shown = ""
    if default not in (None, ""):
        shown = " [hidden]" if secret else f" [{default}]"
    while True:
        try:
            raw = getpass.getpass(f"{label}{shown}: ") if secret else input(f"{label}{shown}: ")
        except EOFError:
            raise Abort("\nCancelled.")
        raw = raw.strip()
        if not raw:
            if default is not None and (default != "" or allow_empty):
                raw = default
            elif allow_empty:
                return ""
            else:
                print("  (required)")
                continue
        ok, v = check(raw)
        if ok:
            return v
        print(f"  {v}")


def truthy(v):
    return str(v).strip().lower() in ("y", "yes", "true", "1", "on")


def confirm(label, default=True, preset=None, assume=False):
    if preset is not None:
        return truthy(preset)
    if assume or not interactive():
        return default
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            raw = input(f"{label} {hint} ").strip().lower()
        except EOFError:
            raise Abort("\nCancelled.")
        if not raw:
            return default
        if raw in ("y", "yes", "n", "no"):
            return raw.startswith("y")


def choose(label, options, default=0):
    """options: list of strings. Returns index. Non-interactive: default."""
    if not interactive():
        return default
    for i, o in enumerate(options, 1):
        print(f"  [{i}] {o}")
    while True:
        try:
            raw = input(f"{label} [{default + 1}]: ").strip()
        except EOFError:
            raise Abort("\nCancelled.")
        if not raw:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
