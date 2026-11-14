"""ntfy client shared by every kit module.

    from kit import notify
    notify.send("rss", "Title", "Body", priority=4, profile="default")

Profiles (server URL + token) live in <config>/ntfy/profiles.json,
readable only by the current user. `notify` on the command line wraps this.
"""
import argparse
import ipaddress
import json
import os
import sys
import urllib.error
import urllib.request

from . import system

PROFILES = os.path.join(system.CONFIG_DIR, "ntfy", "profiles.json")
UA = "kit-notify/1.0"


class NotifyError(Exception):
    pass


def load_profiles():
    return system.read_json(PROFILES).get("profiles", {})


def save_profiles(profiles):
    system.write_json(PROFILES, {"profiles": profiles}, secret=True)


def get_profile(name="default"):
    profiles = load_profiles()
    if name not in profiles:
        have = ", ".join(profiles) or "none"
        raise NotifyError(f'ntfy profile "{name}" not set up (have: {have}). Run: kit configure ntfy')
    return profiles[name]


# ---------- URL handling ----------

def _looks_like_ip(host):
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def url_candidates(raw):
    """'1.2.3.4:2586' -> ['http://1.2.3.4:2586']; 'ntfy.example.com' ->
    ['https://ntfy.example.com', 'http://ntfy.example.com']."""
    s = raw.strip().rstrip("/")
    if not s:
        return []
    if s.startswith(("http://", "https://")):
        return [s]
    host = s.split("/", 1)[0]
    hostname, _, port = host.rpartition(":") if host.count(":") == 1 else (host, "", "")
    hostname = hostname or host
    if _looks_like_ip(hostname) or hostname == "localhost" or (port and port not in ("443",)):
        return [f"http://{s}", f"https://{s}"]
    return [f"https://{s}", f"http://{s}"]


def _req(url, token=None, data=None, method=None, timeout=8):
    h = {"User-Agent": UA}
    if token:
        h["Authorization"] = f"Bearer {token}"
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def health(url):
    """(True, None) if url is a live ntfy server."""
    try:
        _, body = _req(url + "/v1/health", timeout=6)
        if json.loads(body or b"{}").get("healthy"):
            return True, None
        return False, "answered, but it doesn't look like ntfy"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, str(getattr(e, "reason", e))


def resolve_url(raw):
    """Turn whatever the user typed into a working server URL.
    Returns (ok, url_or_error)."""
    cands = url_candidates(raw)
    if not cands:
        return False, "enter an IP:port or a URL"
    errs = []
    for c in cands:
        ok, err = health(c)
        if ok:
            return True, c
        errs.append(f"{c}: {err}")
    return False, "couldn't reach ntfy (" + "; ".join(errs) + ")"


def check_token(url, token):
    """(True, None) if the token is accepted (or no token and server allows anonymous)."""
    try:
        _req(url + "/v1/account", token=token or None)
        return True, None
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, "token rejected (401)" if token else "server needs a token"
        if e.code == 404:  # old ntfy without accounts API: can't check, assume fine
            return True, None
        return False, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, str(getattr(e, "reason", e))


# ---------- sending ----------

def send(topic, title=None, message="", priority=3, tags=None, click=None, actions=None,
         profile="default", markdown=False, dry=False):
    payload = {"topic": topic, "message": message or (title or "")}
    if title:
        payload["title"] = title
    if priority:
        payload["priority"] = int(priority)
    if tags:
        payload["tags"] = list(tags)
    if click:
        payload["click"] = click
    if actions:
        payload["actions"] = actions
    if markdown:
        payload["markdown"] = True
    if dry:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return payload
    p = get_profile(profile)
    try:
        _req(p["url"], token=p.get("token"), data=json.dumps(payload).encode(), method="POST", timeout=15)
    except urllib.error.HTTPError as e:
        raise NotifyError(f"ntfy said HTTP {e.code} ({profile} profile)") from None
    except Exception as e:  # noqa: BLE001
        raise NotifyError(f"couldn't reach ntfy ({profile} profile): {getattr(e, 'reason', e)}") from None
    return payload


# ---------- CLI ----------

HELP = """\
notify: send a push through ntfy (set up with `kit install ntfy`)

  notify -t TOPIC "message"         send a message
      --title "Title"                 bold first line
      --priority 1-5                  default 3
      --tags warning,tada             ntfy emoji tags
      --click https://...             link opened on tap
      -p PROFILE                      which server (default: default)
  notify profiles                   list servers (tokens hidden)
  notify rm-profile NAME            delete a server profile
  kit configure ntfy                add or change a profile
"""


def main(argv=None):
    system.fix_console()
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0
    if argv[0] == "profiles":
        profiles = load_profiles()
        if not profiles:
            print("No profiles yet. Run: kit configure ntfy")
        for n, p in profiles.items():
            tok = "token set" if p.get("token") else "no token"
            print(f"{n:<12} {p['url']}  ({tok})")
        return 0
    if argv[0] == "rm-profile":
        if len(argv) < 2:
            print("usage: notify rm-profile NAME")
            return 2
        profiles = load_profiles()
        if profiles.pop(argv[1], None) is None:
            print(f'No profile "{argv[1]}".')
            return 1
        save_profiles(profiles)
        print(f'Removed profile "{argv[1]}".')
        return 0
    ap = argparse.ArgumentParser(prog="notify", add_help=False)
    ap.add_argument("-t", "--topic", required=True)
    ap.add_argument("-p", "--profile", default="default")
    ap.add_argument("--title")
    ap.add_argument("--priority", type=int, default=3)
    ap.add_argument("--tags")
    ap.add_argument("--click")
    ap.add_argument("message", nargs="+")
    a = ap.parse_args(argv)
    try:
        send(a.topic, a.title, " ".join(a.message), a.priority,
             [t for t in (a.tags or "").split(",") if t], a.click, profile=a.profile)
    except NotifyError as e:
        print(f"notify: {e}")
        return 1
    print(f"Sent to {a.topic}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
