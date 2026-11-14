#!/usr/bin/env python3
"""rss: RSS/Atom feeds -> ntfy push alerts. Run `rss` for help."""
import argparse, datetime, hashlib, html, json, os, re, shutil, subprocess, sys, tempfile, time
import urllib.request
import xml.etree.ElementTree as ET
import yaml

from kit import notify, system

DATA = os.path.join(system.CONFIG_DIR, "rss")
CFG = os.path.join(DATA, "feeds.yaml")
LASTGOOD = os.path.join(DATA, ".feeds.lastgood.yaml")
STATE = os.path.join(DATA, "state.json")
LOG = os.path.join(DATA, "run.log")
SETTINGS = os.path.join(DATA, "config.json")
FAIL_ALERT_AFTER = 3
LOG_MAX_LINES = 3000
UA = "kit-rss/1.0"
FEED_KEYS = {"name", "url", "topic", "profile", "interval_min", "mode", "click", "priorities", "tags", "filters", "paused"}
FILTER_KEYS = {"include", "exclude", "regex", "status"}

HELP = """\
rss: RSS/Atom feeds -> ntfy push alerts (checked every 5 min)

  rss list                     all feeds: status, mode, topic, last check
  rss add <url> [options]      add a feed (fetches it first to make sure it works)
      --name My Feed             name (default: the feed's own title)
      --include word,word        only alert if one of these words appears
      --exclude word,word        never alert if one of these words appears
      --regex 'pattern'          only alert if this regex matches
      --status Value,Value       only alert on these status values (status-page
                                 feeds, e.g. ServiceInterruption for M365)
      --topic name               ntfy topic (default: {topic})
      --profile name             ntfy server profile (default: {profile})
      --mode auto|new|track      auto (default) works it out for you
  rss rm <name>                remove a feed
  rss pause <name>             stop alerts for a feed (keeps it in the list)
  rss resume <name>            turn a paused feed back on
  rss test <name>              send the feed's latest item to your phone
  rss edit                     open feeds.yaml in an editor, checked when you save
  rss check                    check feeds.yaml for mistakes
  rss doctor                   check setup: ntfy, schedule, feeds
  rss log [-n 30]              recent runs and errors
  rss run [--dry] [--force]    check feeds now (--dry: print, don't send)

<name> can be any unique part of a feed's name, case-insensitive.
Default topic and profile: kit configure rss
Files: {data}
"""

YAML_TEMPLATE_HEAD = """\
# RSS -> ntfy feeds. Edit with `rss edit` (checked on save) or `rss add <url>`.
# Run `rss` for all commands.
#
# Smallest possible feed:
#   - name: Some Blog
#     url: https://example.com/feed.xml
#
# Optional per-feed settings:
#   topic: rss                     ntfy topic (default: kit configure rss)
#   profile: default               ntfy server profile
#   interval_min: 5                minutes between checks
#   mode: auto                     auto | new (only new posts) | track (new + edits + resolved)
#   paused: true                   skip this feed
#   click: https://...             link opened when you tap the alert
#   tags: [warning]                ntfy emoji tags
#   priorities: {new: 4, update: 3, resolved: 2}
#   filters:                       case-insensitive, title + text
#     include: [outage, exchange]  at least one must appear (whole words)
#     exclude: [resolved]          none may appear
#     regex: 'teams|outlook'       must match
#     status: [ServiceDegradation] only these <status> values
"""


# ---------- helpers ----------

def now():
    return time.time()


def ts():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ago(t):
    if not t:
        return "never"
    s = int(now() - t)
    if s < 90:
        return f"{s}s ago"
    if s < 5400:
        return f"{s // 60}m ago"
    if s < 172800:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def rd(path):
    return system.read_text(path)


def settings():
    """Module settings from `kit install rss` / `kit configure rss`."""
    s = system.read_json(SETTINGS)
    return {"topic": s.get("topic") or "rss", "profile": s.get("profile") or "default"}


def load_state():
    return system.read_json(STATE)


def save_state(state):
    system.write_text(STATE, json.dumps(state, indent=1))


# ---------- config ----------

def validate(text):
    """Return (cfg, errors). errors is a list of human-readable strings."""
    try:
        cfg = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        return None, [f"Not valid YAML{where}: {getattr(e, 'problem', e)}. Usually a wrong indent or a missing colon."]
    errs = []
    if not isinstance(cfg, dict):
        return None, ["The file should have a `feeds:` list at the top level."]
    feeds = cfg.get("feeds")
    if feeds is None:
        cfg["feeds"] = feeds = []
    if not isinstance(feeds, list):
        return None, ["`feeds:` must be a list (each feed starts with `  - name:`)."]
    names = set()
    for i, f in enumerate(feeds, 1):
        label = f"Feed #{i}"
        if not isinstance(f, dict):
            errs.append(f"{label}: should be a block starting with `- name:`")
            continue
        n = f.get("name")
        if n:
            label = f'Feed "{n}"'
        if not n:
            errs.append(f"{label}: missing `name:`")
        elif str(n).lower() in names:
            errs.append(f"{label}: the name is used twice")
        else:
            names.add(str(n).lower())
        u = f.get("url")
        if not u or not str(u).startswith(("http://", "https://")):
            errs.append(f"{label}: `url:` missing or not http(s)")
        unknown = set(f) - FEED_KEYS
        if unknown:
            errs.append(f"{label}: unknown setting(s) {', '.join(sorted(unknown))} (typo?)")
        if f.get("mode", "auto") not in ("auto", "new", "track"):
            errs.append(f"{label}: mode must be auto, new or track")
        if "interval_min" in f and not (isinstance(f["interval_min"], int) and f["interval_min"] >= 1):
            errs.append(f"{label}: interval_min must be a whole number of minutes")
        flt = f.get("filters") or {}
        if not isinstance(flt, dict):
            errs.append(f"{label}: filters must be a block (include/exclude/regex/status)")
        else:
            bad = set(flt) - FILTER_KEYS
            if bad:
                errs.append(f"{label}: unknown filter(s) {', '.join(sorted(bad))}")
            for k in ("include", "exclude", "status"):
                if k in flt and flt[k] is not None and not isinstance(flt[k], list):
                    errs.append(f"{label}: filters.{k} must be a list like [word, word]")
            if flt.get("regex"):
                try:
                    re.compile(flt["regex"])
                except re.error as e:
                    errs.append(f"{label}: regex is invalid ({e})")
    return cfg, errs


def read_cfg_strict():
    cfg, errs = validate(rd(CFG))
    if errs:
        print("feeds.yaml has problems:\n  - " + "\n  - ".join(errs))
        print("Fix with `rss edit`.")
        sys.exit(1)
    return cfg


def feed_settings(cfg, raw):
    d = cfg.get("defaults") or {}
    f = {**settings(), "interval_min": 5, "mode": "auto", **d, **raw}
    f["priorities"] = {"new": 4, "update": 3, "resolved": 2, **(d.get("priorities") or {}), **(raw.get("priorities") or {})}
    return f


def find_feed(cfg, query):
    q = query.lower()
    feeds = cfg.get("feeds") or []
    exact = [f for f in feeds if str(f["name"]).lower() == q]
    if exact:
        return exact[0]
    hits = [f for f in feeds if q in str(f["name"]).lower()]
    if not hits:
        sys.exit(f'No feed matches "{query}". See `rss list`.')
    if len(hits) > 1:
        sys.exit(f'"{query}" matches several feeds: ' + ", ".join(f["name"] for f in hits) + ". Be more specific.")
    return hits[0]


def write_cfg_text(text):
    cfg, errs = validate(text)
    if errs:
        sys.exit("Refusing to save, the result would be invalid:\n  - " + "\n  - ".join(errs))
    system.write_text(CFG, text)
    shutil.copy(CFG, LASTGOOD)


def block_span(lines, name):
    """Line range [start, end) of the feed block whose name is `name`."""
    pat = re.compile(r"^(\s*)-\s+name:\s*(.+?)\s*$")
    start = indent = None
    for i, l in enumerate(lines):
        m = pat.match(l)
        if start is None:
            if m and m.group(2).strip("'\"") == name:
                start, indent = i, len(m.group(1))
        else:
            if m and len(m.group(1)) == indent:
                return start, i
            if l.strip() and not l.startswith(" ") and not l.lstrip().startswith("#"):
                return start, i
    if start is None:
        sys.exit(f'Couldn\'t find the "{name}" block in feeds.yaml. Use `rss edit`.')
    end = len(lines)
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return start, end


# ---------- feed parsing ----------

def strip_html(s):
    s = re.sub(r"<br\s*/?>|</div>|</p>|</li>", "\n", s or "", flags=re.I)
    s = html.unescape(re.sub(r"<[^>]+>", "", s)).replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def local(tag):
    return tag.rsplit("}", 1)[-1]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=30).read()


def parse_feed(xml_bytes):
    root = ET.fromstring(xml_bytes)
    title = ""
    for el in root.iter():
        if local(el.tag) == "title":
            title = (el.text or "").strip()
            break
    items = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        d = {}
        for c in el:
            t = local(c.tag)
            if t == "link":
                if c.get("href") and c.get("rel", "alternate") == "alternate":
                    d.setdefault("link", c.get("href"))
                elif c.text:
                    d.setdefault("link", c.text.strip())
            elif t in ("summary", "content", "description"):
                d.setdefault("description", c.text or "")
            elif t == "id":
                d.setdefault("guid", c.text)
            elif t in ("updated", "published"):
                d.setdefault("pubDate", c.text)
            else:
                d.setdefault(t, (c.text or "").strip())
        d["text"] = strip_html(d.get("description", ""))
        d["title"] = html.unescape(d.get("title", ""))
        d["key"] = d.get("guid") or d.get("link") or d.get("title") or hashlib.sha1(d["text"].encode()).hexdigest()
        items.append(d)
    return title, items


def passes(item, f):
    if not f:
        return True
    blob = (item.get("title", "") + "\n" + item["text"]).lower()
    inc = [str(w).lower() for w in f.get("include") or []]
    exc = [str(w).lower() for w in f.get("exclude") or []]
    has = lambda w: re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", blob) is not None
    if inc and not any(has(w) for w in inc):
        return False
    if any(has(w) for w in exc):
        return False
    if f.get("regex") and not re.search(f["regex"], blob, re.I):
        return False
    st = [str(s).lower() for s in f.get("status") or []]
    if st and (item.get("status") or "").lower() not in st:
        return False
    return True


def section(text, label):
    m = re.search(rf"{label}:\s*(.+?)(?=\n[A-Z][A-Za-z ]{{2,30}}:|\Z)", text, re.S)
    return m.group(1).strip() if m else ""


def pretty_status(s):
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s or "").strip()


def is_resolved(status):
    s = (status or "").lower()
    return any(w in s for w in ("restored", "resolved", "operational", "falsepositive", "postincident"))


def trim(s, n=900):
    return s if len(s) <= n else s[: n - 1].rsplit(" ", 1)[0] + "…"


def fingerprint(item):
    return hashlib.sha1((item.get("status", "") + "|" + item.get("title", "") + "|" + item["text"]).encode()).hexdigest()


# ---------- ntfy ----------

def build(kind, feed, item):
    title = item.get("title") or feed["name"]
    st = pretty_status(item.get("status"))
    text = item.get("text", "")
    cur, nxt, impact = section(text, "Current status"), section(text, "Next update by"), section(text, "User impact")
    if kind == "new":
        head = f"🆕 {feed['name']}: {title}"
        body = impact or text or title
        if cur:
            body = f"{trim(body, 400)}\n\nStatus: {cur}"
    elif kind == "update":
        head = f"🔄 {feed['name']}: {title}"
        body = cur or text
    else:
        head = f"✅ {feed['name']}: {title} resolved"
        body = cur or "No longer listed in the feed."
    if st and kind != "resolved":
        head += f" ({st})"
    body = trim(body)
    if nxt and kind != "resolved":
        body += f"\n\nNext update by: {nxt}"
    return head, body


def send(feed, head, body, prio, click, dry=False):
    payload = {"topic": feed["topic"], "title": head, "message": body, "priority": int(prio),
               "tags": feed.get("tags") or []}
    if click:
        payload["click"] = click
        payload["actions"] = [{"action": "view", "label": "Open", "url": click}]
    if dry:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return
    notify.send(payload["topic"], payload["title"], payload["message"], payload["priority"],
                payload["tags"], payload.get("click"), payload.get("actions"),
                profile=feed.get("profile") or settings()["profile"])


def publish(feed, kind, item, dry=False, prefix=""):
    head, body = build(kind, feed, item)
    click = feed.get("click") or item.get("link") or feed["url"]
    send(feed, prefix + head, body, feed["priorities"].get(kind, 3), click, dry)


# ---------- run ----------

def process_feed(feed, st, dry, log):
    name = feed["name"]
    title, items = parse_feed(fetch(feed["url"]))
    items = [i for i in items if passes(i, feed.get("filters"))]
    first = not st.get("seeded")
    has_status = any(i.get("status") for i in items)
    mode = feed.get("mode", "auto")
    if mode == "auto":
        tracking = st.get("auto_track", False) or has_status
    else:
        tracking = mode == "track"
    seen = st.setdefault("items", {})
    keys = set()
    sent = 0
    for it in items:
        k = it["key"]
        keys.add(k)
        fp = fingerprint(it)
        prev = seen.get(k)
        kind = None
        if prev is None:
            if first:
                # Seed silently, except open incidents on status-style feeds.
                if tracking and it.get("status") and not is_resolved(it.get("status")):
                    kind = "new"
            else:
                kind = "resolved" if (tracking and is_resolved(it.get("status"))) else "new"
        elif prev["fp"] != fp:
            if mode == "auto" and not tracking:
                tracking = st["auto_track"] = True
                log(f"[{name}] items are edited in place, switched to tracking updates")
            if tracking:
                kind = "resolved" if (is_resolved(it.get("status")) and not is_resolved(prev.get("status"))) else "update"
        if kind:
            publish(feed, kind, it, dry)
            sent += 1
        seen[k] = {"fp": fp, "status": it.get("status", ""), "title": it.get("title", ""),
                   "link": it.get("link", ""), "ts": now()}
    for k in list(seen):
        if k in keys:
            continue
        if tracking and not first and not is_resolved(seen[k].get("status")):
            gone = {"title": seen[k]["title"], "status": "", "text": "", "link": seen[k].get("link")}
            publish(feed, "resolved", gone, dry)
            sent += 1
        if tracking or now() - seen[k]["ts"] > 30 * 86400:
            del seen[k]
    st["seeded"] = True
    st["title"] = title
    st["tracking"] = tracking
    return len(items), sent, first


def start_log():
    """Send output to run.log, trimmed to the last LOG_MAX_LINES lines."""
    try:
        lines = rd(LOG).splitlines(True)
        if len(lines) > LOG_MAX_LINES * 1.2:
            system.write_text(LOG, "".join(lines[-LOG_MAX_LINES:]))
    except FileNotFoundError:
        pass
    f = open(LOG, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = f


def cmd_run(args):
    if args.log:
        start_log()
    state = load_state()
    meta = state.setdefault("_meta", {})
    meta["last_run"] = now()
    text = rd(CFG) if os.path.exists(CFG) else ""
    cfg, errs = validate(text)
    if errs:
        h = hashlib.sha1(text.encode()).hexdigest()
        if meta.get("broken_hash") != h:
            meta["broken_hash"] = h
            send({**settings(), "tags": ["warning"]}, "⚠️ rss: feeds.yaml is broken",
                 "Still running on the last working version. Fix with `rss edit`.\n\n" + "\n".join(errs)[:700],
                 4, None, args.dry)
        print(f"{ts()} feeds.yaml invalid, using last good copy: {'; '.join(errs)}")
        if not os.path.exists(LASTGOOD):
            return
        cfg, _ = validate(rd(LASTGOOD))
    else:
        if meta.pop("broken_hash", None):
            print(f"{ts()} feeds.yaml fixed")
        if not args.dry:
            shutil.copy(CFG, LASTGOOD)
    for raw in cfg.get("feeds") or []:
        feed = feed_settings(cfg, raw)
        name = feed["name"]
        if args.only and args.only.lower() not in name.lower():
            continue
        if feed.get("paused"):
            continue
        st = state.setdefault(name, {})
        if not (args.force or args.dry or args.only) and now() - st.get("last", 0) < feed["interval_min"] * 60 - 30:
            continue
        try:
            n, sent, first = process_feed(feed, st, args.dry, lambda m: print(f"{ts()} {m}"))
            if st.get("fails", 0) >= FAIL_ALERT_AFTER:
                send(feed, f"✅ {name}: feed is back", "Loading again after earlier failures.", 2, None, args.dry)
            st["fails"] = 0
            st.pop("error", None)
            print(f"{ts()} [{name}] ok, {n} items, {sent} sent" + (" (first run, seeded)" if first else ""))
        except Exception as e:
            st["fails"] = st.get("fails", 0) + 1
            st["error"] = str(e)[:200]
            print(f"{ts()} [{name}] FAILED ({st['fails']}): {e}")
            if st["fails"] == FAIL_ALERT_AFTER:
                send(feed, f"⚠️ {name}: feed failing", f"Failed {FAIL_ALERT_AFTER} times in a row: {e}"[:500], 3, None, args.dry)
        st["last"] = now()
    # forget state for feeds that were removed
    names = {f["name"] for f in cfg.get("feeds") or []}
    for k in list(state):
        if k != "_meta" and k not in names:
            del state[k]
    if not args.dry:
        save_state(state)


# ---------- commands ----------

def cmd_list(args):
    cfg = read_cfg_strict()
    state = load_state()
    feeds = cfg.get("feeds") or []
    if not feeds:
        print("No feeds yet. Add one with: rss add <url>")
        return
    W = 44
    for i, raw in enumerate(feeds, 1):
        f = feed_settings(cfg, raw)
        st = state.get(f["name"], {})
        if f.get("paused"):
            status = "⏸ paused"
        elif st.get("fails"):
            status = f"❌ failing x{st['fails']}"
        elif st.get("seeded"):
            status = "✅ ok"
        else:
            status = "⏳ not checked yet"
        title = f"{i}. {f['name']}"
        print(f"┌ {title.ljust(max(W - len(status), len(title) + 2))}{status}")
        rows = [("url", re.sub(r"^https?://(www\.)?", "", f["url"]))]
        if st.get("fails") and st.get("error"):
            rows.append(("error", str(st["error"])[:70]))
        flt = f.get("filters") or {}
        parts = []
        for key, label in (("include", "only"), ("exclude", "skip"), ("status", "status"), ("regex", "match")):
            v = flt.get(key)
            if v:
                parts.append(f"{label}: {', '.join(map(str, v)) if isinstance(v, list) else v}")
        mode = f["mode"]
        tracking = mode == "track" or (mode == "auto" and st.get("tracking"))
        how = "follows updates to each incident" if tracking else "new items only"
        rows.append(("filter", " · ".join(parts)) if parts else ("alerts", f"everything, {how}"))
        if parts:
            rows.append(("alerts", how))
        when = "paused, last checked" if f.get("paused") else "checked"
        rows.append(("topic", f"{f['topic']} · every {f['interval_min']}m · {when} {ago(st.get('last'))}"))
        n = len(st.get("items", {}))
        rows.append(("seen", f"{n} item{'' if n == 1 else 's'}"))
        for j, (k, v) in enumerate(rows):
            print(f"{'└' if j == len(rows) - 1 else '│'}  {k.ljust(8)} {v}")
        print()


def cmd_add(args):
    read_cfg_strict()
    cfg = yaml.safe_load(rd(CFG))
    for f in cfg.get("feeds") or []:
        if f.get("url") == args.url:
            sys.exit(f'Already added as "{f["name"]}".')
    print("Fetching feed...")
    try:
        title, items = parse_feed(fetch(args.url))
    except Exception as e:
        sys.exit(f"Couldn't load that as an RSS/Atom feed: {e}")
    if isinstance(args.name, list):
        args.name = " ".join(args.name)
    name = (args.name or title or args.url).strip()
    if any(str(f["name"]).lower() == name.lower() for f in cfg.get("feeds") or []):
        sys.exit(f'A feed called "{name}" already exists. Pick another with --name "...".')
    def ylist(s):
        return "[" + ", ".join(json.dumps(w.strip()) for w in s.split(",") if w.strip()) + "]"
    lines = [f"  - name: {json.dumps(name)}", f"    url: {args.url}"]
    if args.topic:
        lines.append(f"    topic: {args.topic}")
    if args.profile:
        if args.profile not in notify.load_profiles():
            sys.exit(f'No ntfy profile "{args.profile}". See: notify profiles')
        lines.append(f"    profile: {args.profile}")
    if args.mode:
        lines.append(f"    mode: {args.mode}")
    flt = []
    if args.include:
        flt.append(f"      include: {ylist(args.include)}")
    if args.exclude:
        flt.append(f"      exclude: {ylist(args.exclude)}")
    if args.regex:
        flt.append(f"      regex: {json.dumps(args.regex)}")
    if args.status:
        flt.append(f"      status: {ylist(args.status)}")
    if flt:
        lines += ["    filters:"] + flt
    text = rd(CFG).rstrip("\n") + "\n\n" + "\n".join(lines) + "\n"
    write_cfg_text(text)
    matching = [i for i in items if passes(i, yaml.safe_load("\n".join(l[4:] for l in flt) or "{}") if flt else {})]
    print(f'Added "{name}" ({len(items)} items in the feed, {len(matching)} pass your filters).')
    print("Existing items are recorded quietly on the next check; you only get alerts for new ones.")
    print(f"Want to see what an alert looks like? rss test {name}")


def edit_block(query, fn):
    cfg = read_cfg_strict()
    feed = find_feed(cfg, query)
    lines = rd(CFG).split("\n")
    a, b = block_span(lines, str(feed["name"]))
    new = fn(lines[a:b])
    write_cfg_text("\n".join(lines[:a] + new + lines[b:]))
    return feed["name"]


def cmd_rm(args):
    cfg = read_cfg_strict()
    name = find_feed(cfg, args.name)["name"]
    if not args.yes:
        if input(f'Remove "{name}"? [y/N] ').strip().lower() not in ("y", "yes"):
            sys.exit("Cancelled.")
    edit_block(name, lambda blk: [])
    text = re.sub(r"\n{3,}", "\n\n", rd(CFG))
    write_cfg_text(text)
    print(f'Removed "{name}".')


def cmd_pause(args, on=True):
    def fn(blk):
        blk = [l for l in blk if not re.match(r"^\s+paused:", l)]
        if on:
            blk.insert(1, "    paused: true")
        return blk
    print(f'{"Paused" if on else "Resumed"} "{edit_block(args.name, fn)}".')


def cmd_test(args):
    cfg = read_cfg_strict()
    feed = feed_settings(cfg, find_feed(cfg, args.name))
    _, items = parse_feed(fetch(feed["url"]))
    items = [i for i in items if passes(i, feed.get("filters"))]
    if not items:
        sys.exit("Nothing in the feed passes your filters right now, so there's nothing to send.")
    publish(feed, "new", items[0], prefix="🧪 TEST ")
    print(f'Sent "{items[0].get("title")}" to topic {feed["topic"]}.')


def cmd_edit(args):
    editor = system.default_editor()
    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
    tmp.write(rd(CFG))
    tmp.close()
    try:
        while True:
            subprocess.call(editor + [tmp.name])
            text = rd(tmp.name)
            _, errs = validate(text)
            if not errs:
                if text == rd(CFG):
                    print("No changes.")
                else:
                    write_cfg_text(text)
                    print("Saved. Changes apply on the next check.")
                return
            print("That has problems:\n  - " + "\n  - ".join(errs))
            if input("Edit again to fix? [Y/n] ").strip().lower() in ("n", "no"):
                print("Discarded, feeds.yaml is unchanged.")
                return
    finally:
        os.unlink(tmp.name)


def cmd_check(args):
    cfg = read_cfg_strict()
    print(f"feeds.yaml is OK ({len(cfg.get('feeds') or [])} feeds).")


def doctor_checks():
    """[(ok, message)] used by `rss doctor` and `kit doctor`."""
    out = []
    st = settings()
    profiles = notify.load_profiles()
    prof = profiles.get(st["profile"])
    out.append((bool(prof), f"default topic {st['topic']}, profile {st['profile']}"
                + ("" if prof else " (profile missing: kit configure ntfy)")))
    if prof:
        ok, err = notify.health(prof["url"])
        out.append((ok, f"ntfy reachable at {prof['url']}" + ("" if ok else f" ({err})")))
    if os.path.exists(CFG):
        cfg, errs = validate(rd(CFG))
        n = len((cfg or {}).get("feeds") or [])
        out.append((not errs, f"feeds.yaml: {n} feed{'' if n == 1 else 's'}" + (f", {len(errs)} problem(s), run rss check" if errs else "")))
        for raw in (cfg or {}).get("feeds") or []:
            p = raw.get("profile")
            if p and p not in profiles:
                out.append((False, f'feed "{raw.get("name")}" uses missing profile "{p}"'))
    else:
        out.append((True, "feeds.yaml: not created yet (rss add <url>)"))
    kit_off = system.read_json(system.STATE_FILE).get("modules", {}).get("rss", {}).get("schedule") == "off"
    info = system.schedule_info("rss")
    if kit_off:
        out.append((True, "schedule: off in kit (something else runs `rss run --log`)"))
    else:
        out.append((bool(info), f"schedule: {info or 'missing (kit update rss --force)'}"))
    last = load_state().get("_meta", {}).get("last_run")
    if last:
        late = now() - last > 20 * 60
        out.append((not late, f"last run {ago(last)}" + (" (schedule may not be running)" if late else "")))
    failing = [k for k, v in load_state().items() if k != "_meta" and v.get("fails")]
    if failing:
        out.append((False, "failing feeds: " + ", ".join(failing)))
    return out


def cmd_doctor(args):
    bad = 0
    for ok, msg in doctor_checks():
        print(f"  {'ok ' if ok else 'BAD'}  {msg}")
        bad += 0 if ok else 1
    print("All good." if not bad else f"{bad} problem(s).")
    sys.exit(1 if bad else 0)


def cmd_log(args):
    if not os.path.exists(LOG):
        sys.exit("No log yet.")
    lines = rd(LOG).splitlines()
    print("\n".join(lines[-args.n:]))


def help_text():
    st = settings()
    return HELP.format(topic=st["topic"], profile=st["profile"], data=DATA)


def main():
    system.fix_console()
    os.makedirs(DATA, exist_ok=True)
    if len(sys.argv) == 1 or sys.argv[1] in ("help", "-h", "--help"):
        print(help_text())
        return
    if not os.path.exists(CFG):
        system.write_text(CFG, YAML_TEMPLATE_HEAD + "\nfeeds:\n")
    p = argparse.ArgumentParser(prog="rss", add_help=False)
    p.error = lambda msg: (print(f"rss: {msg}\n"), print(help_text()), sys.exit(2))
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("list")
    a = sub.add_parser("add")
    a.add_argument("url")
    a.add_argument("--name", nargs="+")
    for o in ("--include", "--exclude", "--regex", "--status", "--topic", "--profile"):
        a.add_argument(o)
    a.add_argument("--mode", choices=["auto", "new", "track"])
    r = sub.add_parser("rm")
    r.add_argument("name", nargs="+")
    r.add_argument("-y", "--yes", action="store_true")
    for c in ("pause", "resume", "test"):
        sub.add_parser(c).add_argument("name", nargs="+")
    sub.add_parser("edit")
    sub.add_parser("check")
    sub.add_parser("doctor")
    lg = sub.add_parser("log")
    lg.add_argument("-n", type=int, default=30)
    rn = sub.add_parser("run")
    rn.add_argument("--dry", action="store_true")
    rn.add_argument("--force", action="store_true")
    rn.add_argument("--only")
    rn.add_argument("--log", action="store_true", help="append output to run.log (used by the schedule)")
    for sp in sub.choices.values():
        sp.error = p.error
    args = p.parse_args()
    if isinstance(getattr(args, "name", None), list):
        args.name = " ".join(args.name)
    {"list": cmd_list, "add": cmd_add, "rm": cmd_rm, "pause": cmd_pause,
     "resume": lambda a: cmd_pause(a, on=False), "test": cmd_test, "edit": cmd_edit,
     "check": cmd_check, "log": cmd_log, "run": cmd_run, "doctor": cmd_doctor}[args.cmd](args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
