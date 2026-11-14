"""Stand-in for the bits of the Discord REST API the discord-bot setup uses.

    python tests/fake_discord.py PORT

Token "aaa.bbb.ccc" is valid; anything else gets 401.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "aaa.bbb.ccc"
APP = "100000000000000001"
GUILD = "200000000000000002"
CHANNEL = "300000000000000003"
OWNER = "400000000000000004"

ROUTES = {
    "/users/@me": {"id": APP, "username": "testbot"},
    "/oauth2/applications/@me": {"id": APP, "flags": 1 << 19},
    "/users/@me/guilds": [{"id": GUILD, "name": "Test Server"}],
    f"/guilds/{GUILD}": {"id": GUILD, "name": "Test Server", "owner_id": OWNER},
    f"/guilds/{GUILD}/channels": [{"id": "300000000000000009", "type": 4, "name": "cat", "position": 0},
                                  {"id": CHANNEL, "type": 0, "name": "claude", "position": 1}],
    f"/channels/{CHANNEL}": {"id": CHANNEL, "name": "claude", "type": 0},
    f"/users/{OWNER}": {"id": OWNER, "username": "owner", "global_name": "Test Owner"},
}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.headers.get("Authorization") != f"Bot {TOKEN}":
            return self.send(401, {"message": "401: Unauthorized"})
        path = self.path.split("?")[0]
        if path.startswith("/api/v10"):
            path = path[len("/api/v10"):]
        if path in ROUTES:
            return self.send(200, ROUTES[path])
        self.send(404, {"message": "Unknown"})

    def send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Server(ThreadingHTTPServer):
    def server_bind(self):  # skip HTTPServer's reverse DNS (slow on macOS runners)
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]


if __name__ == "__main__":
    Server(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
