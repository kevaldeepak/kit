#!/usr/bin/env python3
"""Live agent-run card in Discord: one message, edited in place per step change.

The universal task progress card. Used for EVERY
substantive run, single-agent or multi-agent. Plain Discord markdown, no code
fence (phone-first). Finished steps show exact times; the running
step gets a live client-side counter (<t:..:R>) in the -# status line. Editing the message never resets the counter:
it counts from a fixed epoch.

Stateful interface (state per channel in BOT_HOME/run):
  discord-live-card.py start "TITLE" "PHASE:model:label" [...more steps]
        post card, step 1 running. Model tokens: fable|opus|sonnet|haiku.
  discord-live-card.py sub "text"          update running step's sub-label only
  discord-live-card.py next ["sublabel"]   complete running step(s), start next
  discord-live-card.py add "PHASE:model:label"   append a step (kickback/fix)
  discord-live-card.py note "text"         attach a └ note under running step
Parallel steps (index-based, 1-counted; any number may run at once — card
shows ⏳ ∥ on concurrent steps and "N running" in the subtext):
  discord-live-card.py run N               start step N (must be pending)
  discord-live-card.py done N ["sub"]      complete step N specifically
  discord-live-card.py model N tier        correct step N's model label (fable|opus|sonnet|haiku)
Lanes (one sub-row per parallel subagent under a step; the step's mark shows
"⏳ 3/5" and the step auto-completes when every lane has finished):
  discord-live-card.py lanes N "label" [...]     add running lanes A, B, C... to step N
  discord-live-card.py lane N ID done|fail|run   update one lane (ID = letter)
  discord-live-card.py hook                      Claude hook entry (stdin JSON):
        PreToolUse Agent adds a lane to the running step, PostToolUse binds
        the background agent id, SubagentStop ticks the lane off. Passed to
        each run with --settings by the bot; silent no-op when no card is
        active.
Steer line (called by steer-hook.py the instant the owner's mid-turn message is
delivered — before the model has even read it):
  discord-live-card.py steer "text"        └ ✉ steer: text under running step
  discord-live-card.py finish "result" [--tokens 29k]
        complete running step, append stats + short result INSIDE the card,
        flip subtext to ✅ done. Result = the answer (1-4 lines) — there is no
        separate explanation message.
  discord-live-card.py fail "reason"       mark ❌, stats, keep card visible
  discord-live-card.py delete              delete the card message

Raw interface (full content via stdin, auto-fenced unless it contains ```):
  discord-live-card.py post|edit < content.txt

All step timings are computed here (epoch deltas), no model involvement.
"""
import fcntl, json, os, pathlib, re, sys, time, urllib.error, urllib.request

API = "https://discord.com/api/v10"
BOT_HOME = pathlib.Path(os.environ.get("BOT_HOME") or
                        pathlib.Path.home() / ".config" / "kit" / "discord-bot")
RUN = BOT_HOME / "run"
MODELS = {"fable": "Fable", "opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku"}
AGENT_TOOLS = {"Agent", "Task"}
LANE_LABEL = 28     # lane labels are cut to this many chars
LANE_FOLD = 8       # more lanes than this → finished ones fold into one line


def _kv(path):
    conf = {}
    try:
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return conf


def env():
    live = os.environ.get("DISCORD_CHANNEL_ID", "")
    secrets = _kv(BOT_HOME / "secrets.env")
    conf = _kv(BOT_HOME / "bot.conf")
    token = secrets.get("DISCORD_TOKEN")
    channel = live or conf.get("BOT_CHANNEL_ID")
    if not token or not channel:
        sys.exit("DISCORD_TOKEN / channel id not available")
    return token, channel


def call(token, method, path, payload=None, _retry=True):
    req = urllib.request.Request(
        f"{API}{path}", method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                 "User-Agent": "DiscordBot (https://github.com/Rapptz/discord.py, 2.0)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        # parallel lanes can burst several edits at once: honour one 429 wait
        if e.code != 429 or not _retry:
            raise
        try:
            wait = float(json.loads(e.read() or b"{}").get("retry_after", 1))
        except ValueError:
            wait = 1.0
        time.sleep(min(max(wait, 0.2), 5))
        return call(token, method, path, payload, _retry=False)


def fmt_secs(s):
    return f"{s}s" if s < 60 else f"{s // 60}m{s % 60:02d}s"


def lane_id(n):
    return chr(65 + n) if n < 26 else str(n + 1)


def finish_lane(lane, status="done"):
    if lane["status"] != "running":
        return
    lane["status"] = status
    lane["secs"] = max(1, int(time.time()) - lane["start"])


def lanes_settled(step):
    """A running step whose lanes have all finished completes itself."""
    lanes = step.get("lanes") or []
    if step["status"] == "running" and lanes and all(l["status"] != "running" for l in lanes):
        step["status"] = "done"
        step["secs"] = max(1, int(time.time()) - step["start"])


def lane_count(step):
    lanes = step.get("lanes") or []
    return sum(1 for l in lanes if l["status"] != "running"), len(lanes)


def add_lane(step, label, **extra):
    lanes = step.setdefault("lanes", [])
    lane = {"id": lane_id(len(lanes)), "label": " ".join(str(label).split())[:LANE_LABEL].rstrip(),
            "status": "running", "start": int(time.time()), "secs": None, **extra}
    lanes.append(lane)
    return lane


def parse_step(spec):
    phase, model, label = spec.split(":", 2)
    return {"phase": phase.strip().upper(), "model": MODELS.get(model.strip().lower(), model.strip()),
            "label": label.strip(), "status": "pending", "secs": None, "notes": [],
            "lanes": []}


def md(text):
    """Escape Discord markdown in free text (labels, notes, titles)."""
    return re.sub(r"([*_~`|\\])", r"\\\1", str(text))


def render(state):
    """Phone-first card: plain Discord
    markdown, no code fence, so nothing pads out to a fixed column and lines
    wrap cleanly on a narrow screen. Lanes and notes ride on -# lines."""
    steps = state["steps"]
    lines = [f"### 🤖 {md(state['title'])}"]
    n_running = sum(1 for s in steps if s["status"] == "running")
    for i, s in enumerate(steps):
        nd, nl = lane_count(s)
        icon = {"pending": "▫️", "running": "⏳", "done": "✅", "fail": "❌"}[s["status"]]
        tail = ""
        if s["status"] == "done":
            tail = f" · {fmt_secs(s['secs'] or 0)}"
        elif s["status"] == "running":
            tail = (f" · {nd}/{nl}" if nl > 1 else "") + (" ∥" if n_running > 1 else "")
        lines.append(f"{icon} **{i + 1} · {md(s['label'])}** · {s['model']}{tail}")
        rows = []
        lanes = s.get("lanes") or []
        if len(lanes) == 1:
            lanes = []            # a lone agent = the step itself, no sub-row
        if len(lanes) > LANE_FOLD:
            fin = [l for l in lanes if l["status"] == "done"]
            if fin:
                rows.append(f"✅ {len(fin)} done")
                lanes = [l for l in lanes if l["status"] != "done"]
        for l in lanes:
            li = {"running": "⏳", "done": "✅", "fail": "❌"}[l["status"]]
            t = f" · {fmt_secs(l.get('secs') or 0)}" if l["status"] == "done" else ""
            rows.append(f"{li} {l['id']} · {md(l['label'])}{t}")
        for j, r in enumerate(rows):
            lines.append(f"-# {'└' if j == len(rows) - 1 else '├'} {r}")
        for n in s["notes"]:
            lines.append(f"-# └ {md(n)}")
    if state.get("summary"):
        lines += ["", state["summary"]]
    if state.get("result"):
        lines += ["", state["result"]]
    card = "\n".join(lines)
    running = [(i, s) for i, s in enumerate(steps) if s["status"] == "running"]
    if state.get("final"):
        card += f"\n-# {state['final']}"
    elif len(running) == 1:
        i, s = running[0]
        sub = f" · {md(s['sub'])}" if s.get("sub") else ""
        nd, nl = lane_count(s)
        if nl > 1:
            bar = ("▰" * nd + "▱" * (nl - nd) + " ") if nl <= 12 else ""
            sub += f" · {bar}{nd}/{nl} agents"
        card += f"\n-# ⏳ step {i + 1}{sub} · started <t:{s['start']}:R>"
    elif running:
        def tag(i, s):
            nd, nl = lane_count(s)
            return f"step {i + 1}" + (f" ({nd}/{nl})" if nl > 1 else "")
        ids = " + ".join(tag(i, s) for i, s in running)
        earliest = min(s["start"] for _, s in running)
        card += f"\n-# ⏳ {len(running)} running ∥ {ids} · earliest started <t:{earliest}:R>"
    if len(card) > 1990:          # Discord hard cap: keep the tail (status) visible
        tail = card[card.rfind("\n-# "):]
        card = card[:1990 - len(tail) - 30].rsplit("\n", 1)[0] + "\n-# … card trimmed" + tail
    return card


def complete_running(state):
    done = []
    for s in state["steps"]:
        if s["status"] == "running":
            for l in s.get("lanes") or []:
                finish_lane(l)
            s["status"] = "done"
            s["secs"] = max(1, int(time.time()) - s["start"])
            done.append(s)
    return done


def get_step(state, arg):
    i = int(arg)
    if not 1 <= i <= len(state["steps"]):
        sys.exit(f"step {i} out of range (1-{len(state['steps'])})")
    return state["steps"][i - 1]


def start_next(state):
    for s in state["steps"]:
        if s["status"] == "pending":
            s["status"] = "running"
            s["start"] = int(time.time())
            return s
    return None


def apply_hook(state, evt):
    """Mirror Claude's subagent lifecycle onto lanes. Returns True if changed."""
    name = evt.get("hook_event_name")
    steps = state["steps"]
    lanes = [l for s in steps for l in s.get("lanes") or []]
    if name == "PreToolUse":
        # only the main turn's agents get lanes (nested ones carry agent_id)
        if evt.get("tool_name") not in AGENT_TOOLS or evt.get("agent_id"):
            return False
        running = [s for s in steps if s["status"] == "running"]
        if not running:
            return False
        tgt = max(running, key=lambda s: s.get("start", 0))
        inp = evt.get("tool_input") or {}
        add_lane(tgt, inp.get("description") or inp.get("subagent_type") or "agent",
                 tuid=evt.get("tool_use_id"))
        return True
    if name == "PostToolUse":
        lane = next((l for l in lanes if l.get("tuid") == evt.get("tool_use_id")), None)
        if not lane:
            return False
        resp = evt.get("tool_response")
        if isinstance(resp, dict) and (resp.get("isAsync") or resp.get("status") == "async_launched"):
            lane["aid"] = resp.get("agentId")
            return False          # nothing visible changed
        finish_lane(lane)         # foreground agent: tool returned = agent done
    elif name == "SubagentStop":
        lane = next((l for l in lanes if l.get("aid") and l["aid"] == evt.get("agent_id")), None)
        if not lane or lane["status"] != "running":
            return False
        finish_lane(lane)
    else:
        return False
    for s in steps:
        lanes_settled(s)
    return True


def hook_main():
    """Claude hook entry: never blocks or fails the tool call."""
    try:
        evt = json.loads(sys.stdin.read() or "{}")
        key = os.environ.get("DISCORD_CARD_KEY") or os.environ.get("DISCORD_CHANNEL_ID")
        if not key:
            return
        statefile = (RUN / f"discord-live-card-{key}.json")
        if not statefile.exists():
            return
        with locked(statefile):
            if not statefile.exists():
                return
            state = json.loads(statefile.read_text())
            if not apply_hook(state, evt):
                statefile.write_text(json.dumps(state))
                return
            statefile.write_text(json.dumps(state))
            token, channel = env()
            call(token, "PATCH", f"/channels/{channel}/messages/{state['message_id']}",
                 {"content": render(state)})
    except Exception:
        pass


class locked:
    """flock on a sidecar file: hooks and the model's own card calls can land
    at the same instant, read-modify-write of the state must not interleave."""
    def __init__(self, statefile):
        self.path = str(statefile) + ".lock"

    def __enter__(self):
        self.fh = open(self.path, "w")
        fcntl.flock(self.fh, fcntl.LOCK_EX)

    def __exit__(self, *a):
        self.fh.close()


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "hook":
        hook_main()
        return
    tokens_flag = None
    args = []
    it = iter(sys.argv[2:])
    for a in it:
        if a == "--tokens":
            tokens_flag = next(it, None)
        else:
            args.append(a)
    token, channel = env()
    # Parallel tasks: several turns can run in the SAME thread at once. Each
    # subprocess gets DISCORD_CARD_KEY (its task-scoped track key) so sibling
    # cards keep separate state files; API posts still go to the real channel.
    card_key = os.environ.get("DISCORD_CARD_KEY") or channel
    statefile = (RUN / f"discord-live-card-{card_key}.json")

    # raw passthrough mode (legacy / free-form cards)
    if cmd in ("post", "edit"):
        raw = sys.stdin.read().rstrip()
        content = raw if "```" in raw else f"```\n{raw}\n```"
        if cmd == "post":
            msg = call(token, "POST", f"/channels/{channel}/messages", {"content": content})
            statefile.write_text(json.dumps({"message_id": msg["id"]}))
            print(msg["id"])
        else:
            mid = json.loads(statefile.read_text())["message_id"]
            call(token, "PATCH", f"/channels/{channel}/messages/{mid}", {"content": content})
            print("edited")
        return

    if cmd == "start":
        state = {"title": args[0], "t0": int(time.time()),
                 "steps": [parse_step(s) for s in args[1:]]}
        if not state["steps"]:
            sys.exit("start needs at least one PHASE:model:label step")
        start_next(state)
        # Take over the bot's status message if it offered one this turn (the
        # init file): the card and the "thinking" stub become ONE message. The
        # owned marker tells the bot to stop editing it and keep it at delivery.
        init = (RUN / f"discord-card-init-{card_key}.json")
        mid = None
        if init.exists() and time.time() - init.stat().st_mtime < 7200:
            try:
                mid = json.loads(init.read_text()).get("message_id")
            except (ValueError, OSError):
                mid = None
        if mid:
            try:
                call(token, "PATCH", f"/channels/{channel}/messages/{mid}",
                     {"content": render(state)})
                (RUN / f"discord-card-owned-{card_key}").write_text(str(mid))
            except Exception:
                mid = None  # stub gone/stale — fall back to a fresh message
        if not mid:
            msg = call(token, "POST", f"/channels/{channel}/messages",
                       {"content": render(state)})
            mid = msg["id"]
        init.unlink(missing_ok=True)
        state["message_id"] = str(mid)
        statefile.write_text(json.dumps(state))
        print(f"card started: msg {mid}")
        return

    if cmd == "delete":
        st = json.loads(statefile.read_text())
        call(token, "DELETE", f"/channels/{channel}/messages/{st['message_id']}")
        statefile.unlink(missing_ok=True)
        print("deleted")
        return

    if not statefile.exists():
        sys.exit(f"no active card for channel {channel} — run start first")
    with locked(statefile):
        if not statefile.exists():
            sys.exit(f"no active card for channel {channel} — run start first")
        update(cmd, args, tokens_flag, token, channel, statefile)


def update(cmd, args, tokens_flag, token, channel, statefile):
    state = json.loads(statefile.read_text())

    if cmd == "sub":
        for s in state["steps"]:
            if s["status"] == "running":
                s["sub"] = args[0]
                break
    elif cmd == "next":
        complete_running(state)
        nxt = start_next(state)
        if nxt and args:
            nxt["sub"] = args[0]
    elif cmd == "add":
        state["steps"].append(parse_step(args[0]))
        if not any(s["status"] == "running" for s in state["steps"]):
            start_next(state)
    elif cmd == "run":
        s = get_step(state, args[0])
        if s["status"] != "pending":
            sys.exit(f"step {args[0]} is {s['status']}, not pending")
        s["status"], s["start"] = "running", int(time.time())
    elif cmd == "done":
        s = get_step(state, args[0])
        if s["status"] == "done":
            print("done (already: lanes completed it)")
            return
        if s["status"] != "running":
            sys.exit(f"step {args[0]} is {s['status']}, not running")
        for l in s.get("lanes") or []:
            finish_lane(l)
        s["status"] = "done"
        s["secs"] = max(1, int(time.time()) - s["start"])
        if len(args) > 1:
            s["sub"] = args[1]
    elif cmd == "lanes":
        s = get_step(state, args[0])
        if not args[1:]:
            sys.exit("lanes N needs at least one label")
        if s["status"] == "pending":
            s["status"], s["start"] = "running", int(time.time())
        for lbl in args[1:]:
            add_lane(s, lbl)
    elif cmd == "lane":
        s = get_step(state, args[0])
        lid, what = args[1].upper(), args[2].lower()
        lane = next((l for l in s.get("lanes") or [] if l["id"] == lid), None)
        if not lane:
            sys.exit(f"step {args[0]} has no lane {lid}")
        if what in ("done", "fail"):
            finish_lane(lane, what)
            lanes_settled(s)
        elif what == "run":
            lane.update(status="running", start=int(time.time()), secs=None)
            if s["status"] == "done":
                s["status"] = "running"
        else:
            sys.exit("lane action must be done|fail|run")
    elif cmd == "model":
        s = get_step(state, args[0])
        s["model"] = MODELS.get(args[1].strip().lower(), args[1].strip())
    elif cmd == "steer":
        # target the running step (first if several), else the last step
        tgt = next((s for s in state["steps"] if s["status"] == "running"),
                   state["steps"][-1])
        tgt["notes"].append(f"✉ steer: {args[0][:70]}")
    elif cmd == "note":
        for s in state["steps"]:
            if s["status"] == "running":
                s["notes"].append(args[0])
                break
    elif cmd in ("finish", "fail"):
        ended = complete_running(state)
        if cmd == "fail":
            for s in ended:
                s["status"], s["secs"] = "fail", None
        total = int(time.time()) - state["t0"]
        n = sum(len(s.get("lanes") or []) or 1 for s in state["steps"])
        stats = f"{n} agent{'s' if n > 1 else ''} · {total}s"
        if tokens_flag:
            stats += f" · ~{tokens_flag} tokens"
        if cmd == "finish":
            state["summary"] = f"✅ done · {stats}"
            state["final"] = "✅ run complete"
        else:
            state["summary"] = f"❌ failed · {stats}"
            state["final"] = "❌ failed"
        if args:
            state["result"] = args[0]
    else:
        sys.exit(f"unknown command {cmd}")

    call(token, "PATCH", f"/channels/{channel}/messages/{state['message_id']}",
         {"content": render(state)})
    if cmd in ("finish", "fail"):
        statefile.unlink(missing_ok=True)
        print(f"{cmd}: card finalised")
    else:
        statefile.write_text(json.dumps(state))
        print(cmd)


if __name__ == "__main__":
    main()
