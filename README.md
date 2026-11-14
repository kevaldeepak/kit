# kit

Small modules you can install on any device (Linux, macOS, Windows) with one command, then add, remove and update with `kit`.

No admin rights needed. Python is installed privately by [uv](https://github.com/astral-sh/uv), so the system Python is never touched.

## Install

Linux / macOS (Terminal):

```sh
curl -fsSL https://raw.githubusercontent.com/kevaldeepak/kit/main/bootstrap.sh | bash
```

Windows (PowerShell, not as admin):

```powershell
irm https://raw.githubusercontent.com/kevaldeepak/kit/main/bootstrap.ps1 | iex
```

It sets up kit, then offers to open the module picker. Re-running the command updates kit in place.

## Commands

```
kit available              every module in the repo
kit list                   what's installed here
kit info <module>          details, dependencies, config location
kit install [module ...]   install (no name: pick from a list)
kit configure <module>     change a module's settings
kit remove <module>        uninstall (keeps config; --purge deletes it)
kit update [module]        update kit and installed modules
kit doctor [module]        check everything is working
kit schedule <module> off  stop kit's timer (when n8n or your own cron runs it)
kit self-uninstall         remove kit itself (--purge: config too)
```

`-y` accepts defaults, `--set key=value` answers a prompt up front (for scripts):

```sh
kit install rss -y --set url=ntfy.example.com --set token=tk_... --set topic=rss
```

Dependencies are handled both ways: installing a module offers to install what it needs, and removing a module that others depend on warns first (cancel, remove them too, or `--force`).

## Modules

| Module | What it does | Needs |
|---|---|---|
| `ntfy` | Saves an ntfy server connection (URL + token) as a named profile and adds a `notify` command. Doesn't host ntfy. | |
| `rss` | Watches RSS/Atom feeds and pushes new items (and status-page incident updates) to an ntfy topic. Runs every 5 min. | ntfy |
| `discord-bot` | Your own Discord bot that runs Claude Code on this computer with your Claude account. Linux and macOS. | |

### ntfy

```
Server (IP:port or URL): 192.0.2.10:2586      -> http://192.0.2.10:2586
Server (IP:port or URL): ntfy.example.com     -> https://ntfy.example.com
```

The server is health-checked and the token verified before anything is saved. Add more servers with `kit configure ntfy` and a new profile name. Other modules pick a profile, so one can go to your home server and another somewhere else.

```
notify -t alerts "Backup finished"
notify -p work -t deploys --priority 4 --tags rocket "Shipped"
notify profiles
```

### rss

```
rss add https://example.com/feed.xml --name My Feed --include outage,degraded
rss list
rss pause My Feed
rss test My Feed
rss doctor
rss            (all commands)
```

Each feed can override the topic (`--topic`) or the ntfy profile (`--profile`).

If two devices watch the same feed and post to the same topic, you get each alert twice. Add a feed on one device only.

### discord-bot

Talk to Claude Code from Discord: each conversation gets its own thread, you can steer a reply while it runs, run tasks in parallel, get option buttons and files back, and watch a live progress card.

You need: a Claude account (Pro/Max), and a Discord bot you create yourself (free). `kit install discord-bot` walks through it:

1. installs Claude Code if it's missing and checks you're signed in (`claude`, then `/login`)
2. asks for your bot token (Discord Developer Portal > New Application > Bot > Reset Token; turn on **Message Content Intent** on the same page) and checks it
3. prints an invite link, then lets you pick the channel it listens in
4. asks for your Discord user id: **only you** can talk to it, everyone else is ignored
5. asks for the access mode: `full` (Claude can edit files and run commands, like Claude Code itself) or `safe` (read, search and web only)

It runs as a background service (systemd user service on Linux, launchd on macOS) and comes back after a reboot.

```
dbot status | start | stop
dbot restart [--now]       waits for replies in progress; rolls back if it doesn't come back up
dbot logs [-f]
dbot update | rollback
dbot config                settings (token hidden)
dbot set mode safe         or: channel, owner, name, workdir, model, helpers, ...
dbot allow USER_ID         let someone else use it (deny to remove)
dbot doctor
```

The token is stored in `~/.config/kit/discord-bot/secrets.env`, readable only by your user, never written to logs and never passed to Claude. Claude's own permission rules also block it from reading that file. Your Claude login stays in Claude Code's own store; kit never touches it.

In Discord: message the channel to start a thread, reply in the thread to continue, react ❌ on the progress message to stop, 🔄 on a reply to redo it. Slash commands: `/model`, `/helpers`, `/reset`, `/cancel`, `/brief`, `/usage`, `/claude on|off`, `/status`.

## Where things live

| | Linux / macOS | Windows |
|---|---|---|
| Code + Python | `~/.local/share/kit` | `%LOCALAPPDATA%\kit` |
| Config, secrets, feeds | `~/.config/kit` | `%APPDATA%\kit` |
| Commands | `~/.local/bin` | `%LOCALAPPDATA%\kit\bin` |
| Schedule | cron, or a systemd user timer (Linux); launchd agent (macOS) | Task Scheduler (`kit-<module>`) |

Secrets files are readable only by your user (chmod 600 / icacls).

## Writing a module

```
modules/<name>/
  module.json   name, version, description, requires, platforms, commands, schedule, pip
  module.py     optional hooks: configure, install, uninstall, upgrade, doctor
  <script>.py   commands, run with kit's Python (import kit.notify to send pushes)
```

```json
{
  "name": "rss",
  "version": "1.0.0",
  "description": "RSS/Atom feeds to ntfy push alerts",
  "requires": ["ntfy"],
  "platforms": ["linux", "macos", "windows"],
  "commands": {"rss": "rss.py"},
  "schedule": {"command": "rss", "args": ["run", "--log"], "every_min": 5}
}
```

`pip` lists Python packages installed into kit's private Python with the module.

Hooks get a `ctx` with `ask`, `confirm`, `load`/`save` (module config), `notify` and `system`. Push the folder and it appears in `kit available` on every device.

**Never hardcode anything specific to a setup** (IPs, hostnames, tokens, emails): ask for it in `configure`. The pre-commit hook and CI block private IPs, Tailscale names and token patterns. Enable the hook once per clone:

```sh
git config core.hooksPath .githooks
```

CI runs a full install, use and remove cycle on Ubuntu, macOS and Windows on every push.
