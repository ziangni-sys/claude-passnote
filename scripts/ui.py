#!/usr/bin/env python3
"""Passnote in the browser: a web page for the person behind the agent, to read, send and ack messages, see what they
sent and whether it was acked, and whom they can write to. Standard library only. Started by /passnote:ui:

  python3 ui.py [--port N] [--no-browser]

It serves the files in ui/ on 127.0.0.1 only, never on another interface, and answers the page's API by calling the
passnote server with this computer's config.json (client.py). The token stays in this process: no answer contains it.
  GET  /api/state           -> {"configured", "url", "name", "certificate", "home", "error", "code", "line",
                                "user", "peers", "presence", "inbox": [...], "sent": [...] or null}
                               error and code say why the server could not be asked (code: its HTTP status, if it
                               answered; line: your tokens.json line when it does not know your token); then user,
                               peers, presence, inbox and sent are missing. presence: whether each of the others is
                               around, {name: {"online", "last_prompt"}} as the server's /whoami gives it, null for a
                               server older than 0.4.0; inbox: the latest messages to you and all unread ones, oldest
                               first; sent: the latest you sent, null for a server older than 0.3.0
  POST /api/send {to, type, body, refs?, needs_reply?, reply_to?}  -> {"id", "created_at"}
  POST /api/ack  {ids}      -> {"acked": n}
  A failed call answers {"error"}: 400 for a bad request from the page, 502 when the passnote server refused it or
  could not be reached.
Each start makes a random key. The address it prints and opens carries the key in its fragment (#key=...), which the
browser sends nowhere; the page sends it back in the X-Passnote-Key header of each API call. Without that key, or with
a Host header other than this address, the answer is 403, so other websites open in the same browser can neither read
nor send messages (cross-site requests, DNS rebinding). The page puts everything it gets into the document as text,
never as HTML, and the Content-Security-Policy allows no inline or foreign scripts.
It is the human's own view: it needs no /passnote:enable, and it calls the server only while the page is open. Its
calls are marked as the web page's (client.KIND "ui"), so an open page does not make you online for the others: only a
Claude Code session with passnote on does.
"""
import argparse
import hmac
import json
import os
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import client  # noqa: E402

client.KIND = "ui"
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
FILES = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
         "/style.css": ("style.css", "text/css; charset=utf-8"), "/icon.svg": ("icon.svg", "image/svg+xml")}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
LIMIT = 200                 # messages per list: the most the server gives at once
TIMEOUT = 5.0               # seconds, per call to the server
MAX_REQUEST = 64 * 1024     # bytes


def state():
    c = client.config()
    out = {"configured": client.configured(), "url": c["url"], "name": c["name"], "certificate": c["ca"],
           "home": client.home(), "error": None, "code": None}
    if not out["configured"]:
        out["error"] = "passnote is not set up on this computer: run /passnote:init <your-name> <server-url> [cert.pem]"
        return out
    try:
        who = client.whoami(TIMEOUT)
        recent = client.inbox(True, LIMIT, TIMEOUT)
        unread = client.inbox(False, LIMIT, TIMEOUT)     # all of them, also those older than the latest LIMIT
        try:
            sent = client.sent(LIMIT, TIMEOUT)
        except client.PassnoteError as e:
            if e.code != 404:
                raise
            sent = None
    except client.PassnoteError as e:
        out.update(error=str(e), code=e.code)
        if e.code == 401:
            out["line"] = client.token_line(c["name"] or "<your name>")
        return out
    inbox = {m["id"]: m for m in recent + unread}
    out.update(user=who["user"], peers=who["peers"], presence=who["presence"], inbox=[inbox[i] for i in sorted(inbox)],
               sent=sent)
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "passnote-ui/1"
    key = b""                   # set in main()
    hosts = ()                  # the Host headers of this address

    def log_message(self, fmt, *args):     # the page asks every few seconds: keep the terminal quiet
        pass

    def reply(self, code, obj=None, data=None, ctype="application/json; charset=utf-8"):
        if data is None:
            data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def allowed(self, api):
        if self.headers.get("Host", "") not in self.hosts:
            return False
        return not api or hmac.compare_digest(self.headers.get("X-Passnote-Key", "").encode("utf-8", "replace"),
                                              self.key)

    def forbidden(self):
        self.reply(403, {"error": "open the address that /passnote:ui printed (it ends with #key=...)"})

    def call(self, f):
        try:
            return self.reply(200, f())
        except client.PassnoteError as e:
            return self.reply(502, {"error": str(e)})
        except (KeyError, TypeError, ValueError) as e:
            return self.reply(400, {"error": f"bad request: {e}"})

    def do_GET(self):
        path = urlparse(self.path).path
        if not self.allowed(path.startswith("/api/")):
            return self.forbidden()
        if path == "/api/state":
            return self.call(state)
        if path in FILES:
            name, ctype = FILES[path]
            with open(os.path.join(PAGE, name), "rb") as f:
                return self.reply(200, data=f.read(), ctype=ctype)
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self.allowed(True):
            return self.forbidden()
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_REQUEST:
                raise ValueError("request too large")
            p = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(p, dict):
                raise ValueError("expected a JSON object")
        except ValueError as e:
            return self.reply(400, {"error": f"bad request: {e}"})
        if path == "/api/send":
            return self.call(lambda: client.send(p["to"], p["type"], p["body"], p.get("refs") or [],
                                                 p.get("needs_reply", False), p.get("reply_to"), TIMEOUT))
        if path == "/api/ack":
            return self.call(lambda: {"acked": client.ack(p["ids"], TIMEOUT)})
        self.reply(404, {"error": "not found"})


def main():
    p = argparse.ArgumentParser(description="passnote in the browser (see README.md)")
    p.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: a free one)")
    p.add_argument("--no-browser", action="store_true", help="only print the address, do not open it")
    a = p.parse_args()
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except OSError as e:
        sys.exit(f"cannot listen on 127.0.0.1:{a.port}: {e}")
    port = httpd.server_address[1]
    key = secrets.token_urlsafe(24)
    Handler.key, Handler.hosts = key.encode(), (f"127.0.0.1:{port}", f"localhost:{port}")
    url = f"http://127.0.0.1:{port}/#key={key}"
    print(f"passnote ui: {url}", flush=True)
    print("(only this computer can open it; it runs until stopped, e.g. with Ctrl+C)", flush=True)
    if not a.no_browser:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
