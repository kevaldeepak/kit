"""Shared by dbot and the kit install hooks: settings file, secrets file,
Discord API checks, Claude CLI checks."""
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from botcore import paths  # noqa: E402

API = os.environ.get("DBOT_DISCORD_API") or "https://discord.com/api/v10"   # override = tests
UA = "DiscordBot (https://github.com/Rapptz/discord.py, 2.0)"
PORTAL = "https://discord.com/developers/applications"
# View Channels, Send Messages, Send in Threads, Create Public Threads, Embed Links,
# Attach Files, Read Message History, Add Reactions, Use Application Commands
PERMISSIONS = 1024 | 2048 | (1 << 38) | (1 << 35) | 16384 | 32768 | 65536 | 64 | (1 << 31)
MESSAGE_CONTENT_FLAGS = (1 << 18) | (1 << 19)   # GATEWAY_MESSAGE_CONTENT (+ _LIMITED)

# friendly name -> bot.conf key
KEYS = {
    "channel": "BOT_CHANNEL_ID",
    "owner": "BOT_OWNER_ID",
    "name": "BOT_OWNER_NAME",
    "allowed": "BOT_ALLOWED_USERS",
    "mode": "MODE",
    "workdir": "WORKDIR",
    "model": "BIG_MODEL",
    "helpers": "HELPERS",
    "compact": "AUTO_COMPACT",
    "compact_at": "COMPACT_AT",
    "briefs": "BRIEFS",
}
LABELS = {
    "BOT_CHANNEL_ID": "channel the bot listens in",
    "BOT_OWNER_ID": "owner (Discord user id)",
    "BOT_OWNER_NAME": "what Claude calls you",
    "BOT_ALLOWED_USERS": "other allowed users",
    "MODE": "access: full or safe",
    "WORKDIR": "folder Claude works in",
    "BIG_MODEL": "main model: opus or sonnet",
    "HELPERS": "subagents: off, auto, turbo",
    "AUTO_COMPACT": "auto-compact long threads: on/off",
    "COMPACT_AT": "compact at this many tokens",
    "BRIEFS": "per-thread project briefs: on/off",
}
DEFAULTS = {"MODE": "full", "BIG_MODEL": "opus", "HELPERS": "auto", "AUTO_COMPACT": "on",
            "BRIEFS": "on", "COMPACT_AT": "150000"}


def _digits(v):
    v = str(v).strip().strip("<@!#>")
    return (True, v) if v.isdigit() and 15 <= len(v) <= 21 else (False, "needs a Discord id (17-20 digits)")


def _choice(*opts):
    def f(v):
        v = str(v).strip().lower()
        return (True, v) if v in opts else (False, "one of: " + ", ".join(opts))
    return f


def _ids(v):
    parts = [p for p in str(v).replace(" ", ",").split(",") if p]
    for p in parts:
        if not _digits(p)[0]:
            return False, f"{p} isn't a Discord user id"
    return True, ",".join(parts)


def _folder(v):
    p = pathlib.Path(str(v)).expanduser()
    return (True, str(p)) if p.is_dir() else (False, f"{p} isn't a folder")


def _num(v):
    return (True, str(v)) if str(v).isdigit() else (False, "a number")


VALIDATE = {
    "BOT_CHANNEL_ID": _digits, "BOT_OWNER_ID": _digits, "BOT_ALLOWED_USERS": _ids,
    "MODE": _choice("full", "safe"), "WORKDIR": _folder,
    "BIG_MODEL": _choice("opus", "sonnet", "opus4.8"),
    "HELPERS": _choice("off", "auto", "turbo"), "AUTO_COMPACT": _choice("on", "off"),
    "BRIEFS": _choice("on", "off"), "COMPACT_AT": _num,
    "BOT_OWNER_NAME": lambda v: (True, str(v).strip()) if str(v).strip() else (False, "can't be empty"),
}


# ---------- files ----------

def read_kv(path):
    out = {}
    try:
        for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def _write_kv(path, data, header, secret=False):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    body = header + "".join(f"{k}={v}\n" for k, v in data.items() if v not in (None, ""))
    if secret:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
    else:
        tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)
    if secret:
        os.chmod(path, 0o600)


def load_conf():
    return {**DEFAULTS, **read_kv(paths.CONF_FILE)}


def save_conf(conf):
    order = list(LABELS) + [k for k in conf if k not in LABELS]
    _write_kv(paths.CONF_FILE, {k: conf.get(k) for k in order},
              "# discord-bot settings (no secrets here). Change with: dbot set <key> <value>\n")


def load_token():
    return read_kv(paths.SECRETS_FILE).get("DISCORD_TOKEN", "")


def save_token(token):
    _write_kv(paths.SECRETS_FILE, {"DISCORD_TOKEN": token},
              "# Discord bot token. Owner-only file; never share it.\n", secret=True)


def mask(token):
    return f"set (...{token[-4:]})" if token else "not set"


# ---------- Discord ----------

class DiscordError(Exception):
    pass


def api(token, path, method="GET"):
    req = urllib.request.Request(API + path, method=method,
                                 headers={"Authorization": f"Bot {token}", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise DiscordError("Discord rejected the token") from None
        if e.code == 403:
            raise DiscordError("the bot can't see that (missing permission or not in the server)") from None
        if e.code == 404:
            raise DiscordError("not found") from None
        raise DiscordError(f"Discord said HTTP {e.code}") from None
    except Exception as e:  # noqa: BLE001
        raise DiscordError(f"couldn't reach Discord: {getattr(e, 'reason', e)}") from None


def check_token(token):
    """(ok, info_or_error). info = {"bot": name, "app_id": id, "content_intent": bool}"""
    token = (token or "").strip()
    if token.lower().startswith("bot "):
        token = token[4:].strip()
    if token.count(".") < 2:
        return False, "that doesn't look like a bot token (Bot page, Reset Token, copy)"
    try:
        me = api(token, "/users/@me")
        app = api(token, "/oauth2/applications/@me")
    except DiscordError as e:
        return False, str(e)
    flags = int(app.get("flags") or 0)
    return True, {"token": token, "bot": me.get("username", "?"), "app_id": app.get("id") or me.get("id"),
                  "content_intent": bool(flags & MESSAGE_CONTENT_FLAGS)}


def invite_url(app_id):
    return (f"https://discord.com/oauth2/authorize?client_id={app_id}"
            f"&scope=bot%20applications.commands&permissions={PERMISSIONS}")


def text_channels(token):
    """[(guild_name, channel_id, channel_name, guild_owner_id)] the bot can see."""
    out = []
    for g in api(token, "/users/@me/guilds") or []:
        try:
            guild = api(token, f"/guilds/{g['id']}")
            chans = api(token, f"/guilds/{g['id']}/channels")
        except DiscordError:
            continue
        for c in sorted(chans or [], key=lambda c: c.get("position", 0)):
            if c.get("type") == 0:
                out.append((g.get("name", "?"), c["id"], c.get("name", "?"), guild.get("owner_id")))
    return out


def user_name(token, uid):
    try:
        u = api(token, f"/users/{uid}")
        return u.get("global_name") or u.get("username")
    except DiscordError:
        return None


# ---------- Claude CLI ----------

def claude_bin():
    import svc
    return svc.claude_path()


def claude_status():
    """(installed, logged_in, detail)"""
    c = claude_bin()
    if not c:
        return False, False, "Claude Code isn't installed"
    try:
        r = subprocess.run([c, "auth", "status", "--json"], capture_output=True, text=True, timeout=30)
        d = json.loads(r.stdout or "{}")
        if d.get("loggedIn"):
            return True, True, f"logged in ({d.get('authMethod', '?')})"
        return True, False, "not logged in"
    except Exception as e:  # noqa: BLE001
        return True, False, f"couldn't check login ({e})"


INSTALL_CLAUDE = "curl -fsSL https://claude.ai/install.sh | bash"


def install_claude():
    r = subprocess.run(["bash", "-c", INSTALL_CLAUDE])
    return r.returncode == 0
