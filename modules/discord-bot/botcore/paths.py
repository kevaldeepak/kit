"""Where the bot keeps its files. One folder (BOT_HOME) holds everything that
belongs to this install: settings, the token, state and logs. `dbot` sets
BOT_HOME for the service; the default matches kit's config folder."""
import os
import pathlib
import platform


def _default_home():
    if platform.system() == "Windows":
        base = os.environ.get("APPDATA") or str(pathlib.Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(pathlib.Path.home() / ".config")
    kit = os.environ.get("KIT_CONFIG") or os.path.join(base, "kit")
    return pathlib.Path(kit) / "discord-bot"


HOME = pathlib.Path(os.environ.get("BOT_HOME") or _default_home()).expanduser()
CONF_FILE = HOME / "bot.conf"          # settings, KEY=VALUE, no secrets
SECRETS_FILE = HOME / "secrets.env"    # DISCORD_TOKEN=..., owner-only (chmod 600)
STATE = HOME / "state"                 # sessions, thread index, inboxes
RUN = HOME / "run"                     # short-lived per-turn files (live card state)
FILES = HOME / "files"                 # Discord attachments saved for Claude
LOGS = HOME / "logs"
CODE = pathlib.Path(__file__).resolve().parent
TOOLS = CODE / "bin"                   # discord-update, discord-live-card.py, ...

for _d in (STATE, RUN, FILES, LOGS):
    _d.mkdir(parents=True, exist_ok=True)


def workdir():
    """Folder Claude works in (bot.conf WORKDIR, default: your home folder)."""
    from botcore.config import conf
    w = conf.get("WORKDIR", "").strip()
    return str(pathlib.Path(w).expanduser()) if w else str(pathlib.Path.home())


def load_secrets():
    env = {}
    try:
        for line in SECRETS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env
