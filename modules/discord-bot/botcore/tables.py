"""Tables for Discord, which doesn't render markdown pipe tables.

Every markdown table in a reply (outside code fences) is converted:
  * small (fits ~46 chars, up to 5 columns): aligned code block; if any cell is
    a status word (ok/live/fail/...) it becomes an `ansi` block with coloured
    status cells
  * wider or bigger: rendered to a PNG (headless Chromium via playwright) and
    attached; if rendering isn't available it falls back to the code block
"""
import html, os, re, subprocess, sys, tempfile, time, unicodedata

MAX_TEXT_WIDTH = 46
MAX_TEXT_COLS = 5
MAX_TEXT_ROWS = 15

_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_GREEN = re.compile(r"^(✅|🟢|●\s*)?\s*(ok|live|done|pass(ed)?|up|yes|healthy|active|success|running)\b", re.I)
_YELLOW = re.compile(r"^(🟡|⚠️|●\s*)?\s*(warn(ing)?|behind|pending|waiting|queued|partial|slow|stale|idle|skipped)\b", re.I)
_RED = re.compile(r"^(❌|🔴|●\s*)?\s*(fail(ed)?|error|down|no|dead|broken|blocked|critical)\b", re.I)
_COL = {"g": ("\x1b[32m", "#57f287"), "y": ("\x1b[33m", "#fee75c"), "r": ("\x1b[31m", "#ed4245")}


def _w(s):
    """Display width in a monospace block (wide chars and emoji count 2)."""
    n = 0
    for ch in s:
        if unicodedata.combining(ch) or ch in "️‍":
            continue
        n += 2 if unicodedata.east_asian_width(ch) in "WF" else 1
    return n


def _cells(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    out = []
    for c in line.split("|"):
        c = c.strip()
        c = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), c)
        c = re.sub(r"`([^`]*)`", r"\1", c)
        out.append(c)
    return out


def _status(cell):
    for k, rx in (("g", _GREEN), ("y", _YELLOW), ("r", _RED)):
        if rx.match(cell):
            return k
    return None


def as_text(header, rows):
    n = len(header)
    rows = [r + [""] * (n - len(r)) for r in rows]
    widths = [max(_w(x) for x in [header[i]] + [r[i] for r in rows]) for i in range(n)]
    colour = any(_status(c) for r in rows for c in r)

    def fmt(cells, bold=False):
        parts = []
        for i, c in enumerate(cells):
            pad = " " * (widths[i] - _w(c)) if i < n - 1 else ""
            k = None if bold else _status(c)
            if colour and k:
                parts.append(f"{_COL[k][0]}{c}\x1b[0m{pad}")
            else:
                parts.append(c + pad)
        line = "  ".join(parts).rstrip()
        return f"\x1b[1;37m{line}\x1b[0m" if (bold and colour) else line

    body = "\n".join([fmt(header, bold=True)] + [fmt(r) for r in rows])
    return f"```{'ansi' if colour else ''}\n{body}\n```"


def _html(header, rows, title):
    td = "padding:10px 16px;white-space:nowrap"
    head = "".join(f'<th style="{td}">{html.escape(h)}</th>' for h in header)
    trs = []
    for i, r in enumerate(rows):
        r = r + [""] * (len(header) - len(r))
        tds = []
        for j, c in enumerate(r):
            k = _status(c)
            st = td + (f";color:{_COL[k][1]}" if k else "") + (";font-weight:600" if j == 0 else "")
            tds.append(f'<td style="{st}">{html.escape(c)}</td>')
        bg = ' style="background:#2e3035"' if i % 2 else ""
        trs.append(f"<tr{bg}>{''.join(tds)}</tr>")
    cap = (f'<div style="color:#f2f3f5;font-weight:700;font-size:17px;margin:0 0 10px 4px">'
           f'{html.escape(title)}</div>') if title else ""
    return ('<html><body style="margin:0;background:#313338;font-family:-apple-system,'
            'Segoe UI,Helvetica,Arial,sans-serif"><div id="c" style="display:inline-block;'
            f'padding:16px">{cap}<table style="border-collapse:collapse;color:#dbdee1;'
            'font-size:15px;background:#2b2d31;border-radius:10px;overflow:hidden">'
            '<tr style="background:#1e1f22;color:#b5bac1;text-align:left;font-size:12px;'
            f'letter-spacing:.06em;text-transform:uppercase">{head}</tr>{"".join(trs)}'
            '</table></div></body></html>')


_RENDER = """
import sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(device_scale_factor=2)
    pg.goto("file://" + sys.argv[1]); pg.locator("#c").screenshot(path=sys.argv[2]); b.close()
"""


def as_image(header, rows, title=""):
    """PNG path, or None if this computer can't render."""
    d = tempfile.mkdtemp(prefix="discord-table-")
    src, out = os.path.join(d, "t.html"), os.path.join(d, f"table-{int(time.time())}.png")
    with open(src, "w") as f:
        f.write(_html(header, rows, title))
    for py in dict.fromkeys([sys.executable, "/usr/bin/python3"]):
        try:
            r = subprocess.run([py, "-c", _RENDER, src, out], capture_output=True, timeout=60)
            if r.returncode == 0 and os.path.exists(out):
                return out
        except Exception:
            pass
    return None


def _convert(header, rows, title=""):
    """(replacement text, image path or None)"""
    text = as_text(header, rows)
    width = max(_w(l) for l in re.sub(r"\x1b\[[0-9;]*m", "", text).splitlines())
    if width <= MAX_TEXT_WIDTH and len(header) <= MAX_TEXT_COLS and len(rows) <= MAX_TEXT_ROWS:
        return text, None
    img = as_image(header, rows, title)
    if img:
        return (f"📊 **{title}**" if title else ""), img
    return text, None


def process(reply):
    """Convert every markdown table in reply. Returns (reply, [png paths])."""
    if "|" not in reply:
        return reply, []
    lines = reply.split("\n")
    out, files, i, fence = [], [], 0, False
    while i < len(lines):
        ln = lines[i]
        if ln.lstrip().startswith("```"):
            fence = not fence
        if (not fence and "|" in ln and i + 1 < len(lines) and _SEP.match(lines[i + 1])
                and lines[i + 1].count("-") >= 3):
            header = _cells(ln)
            j, rows = i + 2, []
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                rows.append(_cells(lines[j]))
                j += 1
            # a bold/heading line right above becomes the image title
            title = ""
            if out and re.match(r"^\s*(#{1,3}\s+|\*\*).+", out[-1]):
                title = re.sub(r"^#{1,3}\s+|\*\*", "", out[-1]).strip()
            rep, img = _convert(header, rows, title)
            if img:
                files.append(img)
                if title:
                    out.pop()
            if rep:
                out.append(rep)
            i = j
            continue
        out.append(ln)
        i += 1
    return "\n".join(out), files
