"""Tiny stand-in for an ntfy server + an RSS feed, for tests.

    python tests/fake_ntfy.py PORT MESSAGES_FILE

GET  /v1/health      -> {"healthy": true}
GET  /v1/account     -> 200 with "Bearer tk_test", else 401
POST /               -> appends the JSON body to MESSAGES_FILE
GET  /feed.xml       -> RSS feed; POST /_next adds one more item
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "tk_test"
ITEMS = [("First post", "one"), ("Second post", "two")]


def feed():
    items = "".join(
        f"<item><title>{t}</title><link>http://example.com/{g}</link><guid>{g}</guid>"
        f"<description>Body of {t}</description></item>" for t, g in ITEMS)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>Test Feed</title>{items}</channel></rss>'


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authed(self):
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def do_GET(self):
        if self.path == "/v1/health":
            return self.reply(200, '{"healthy":true}')
        if self.path == "/v1/account":
            return self.reply(200, "{}") if self.authed() else self.reply(401, '{"error":"unauthorized"}')
        if self.path == "/feed.xml":
            return self.reply(200, feed(), "application/rss+xml")
        self.reply(404, "{}")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/_next":
            n = len(ITEMS) + 1
            ITEMS.insert(0, (f"Post number {n}", f"id{n}"))
            return self.reply(200, "{}")
        if self.path == "/":
            if not self.authed():
                return self.reply(401, '{"error":"unauthorized"}')
            with open(sys.argv[2], "a", encoding="utf-8") as f:
                f.write(json.dumps(json.loads(body)) + "\n")
            return self.reply(200, '{"id":"x"}')
        self.reply(404, "{}")


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
