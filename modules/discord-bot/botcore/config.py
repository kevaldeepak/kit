"""Settings from BOT_HOME/bot.conf (KEY=VALUE lines, shell-compatible).
Written by `dbot` / `kit configure discord-bot`: channel, owner, mode, model...
Secrets stay in BOT_HOME/secrets.env, not here."""
import pathlib
from botcore import paths

CONF_FILE = paths.CONF_FILE


def load_conf(path=CONF_FILE):
    conf = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return conf


conf = load_conf()


def conf_int(key, default=0):
    try:
        return int(conf.get(key) or default)
    except ValueError:
        return default
