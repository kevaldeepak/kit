"""/usage: this computer's Claude plan usage as progress bars.

Reads the CLI's own OAuth login (~/.claude/.credentials.json) and asks the
account usage endpoint, so every bot shows the usage of the account logged in
on this computer. Nothing is sent anywhere except Anthropic's API."""
import datetime as dt, json, pathlib, urllib.request

CREDS = pathlib.Path.home() / ".claude" / ".credentials.json"
URL = "https://api.anthropic.com/api/oauth/usage"


def fetch():
    tok = (json.loads(CREDS.read_text()).get("claudeAiOauth") or {}).get("accessToken")
    if not tok:
        raise RuntimeError("no Claude login found on this computer")
    req = urllib.request.Request(URL, headers={
        "Authorization": f"Bearer {tok}", "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code/kit-discord-bot"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _bar(pct, width=20):
    pct = max(0.0, min(100.0, float(pct or 0)))
    filled = round(pct / 100 * width)
    return "▰" * filled + "▱" * (width - filled)


def _when(iso):
    if not iso:
        return "-"
    t = dt.datetime.fromisoformat(iso).astimezone()
    now = dt.datetime.now().astimezone()
    return t.strftime("%H:%M") if t.date() == now.date() else t.strftime("%a %H:%M")


def _dot(pct):
    return "🔴" if pct >= 90 else "🟠" if pct >= 70 else "🟢"


def render(data):
    now = dt.datetime.now().astimezone().strftime("%H:%M %Z")
    out = ["### 📊 Claude usage", f"-# this bot's login · checked {now}", ""]
    for lim in data.get("limits") or []:
        pct = float(lim.get("percent") or 0)
        scope = ((lim.get("scope") or {}).get("model") or {}).get("display_name")
        kind = lim.get("kind", "")
        title = ("5-hour session" if kind == "session" else
                 f"Weekly, {scope}" if scope else
                 "Weekly, all models" if kind.startswith("weekly") else kind.replace("_", " "))
        note = f"{100 - pct:.0f}% left · resets {_when(lim.get('resets_at'))}"
        if pct >= 100 and scope == "Fable":
            note = f"maxed, so Fable pins fall back to Opus 5.5 · resets {_when(lim.get('resets_at'))}"
        elif pct >= 100:
            note = f"maxed · resets {_when(lim.get('resets_at'))}"
        out += [f"**{title}**", f"{_bar(pct)} {pct:.0f}% used {_dot(pct)}", f"-# {note}", ""]
    extra = data.get("extra_usage") or {}
    out += ["**Extra usage credits**",
            "-# on: plan limits are covered by credits" if extra.get("is_enabled")
            else "-# off, so there's no pay-as-you-go cover if you hit a limit"]
    return "\n".join(out)
