#!/usr/bin/env python3
"""Passnote server: a small HTTP(S) + SQLite message service for Claude Code agents on different computers.
Standard library only. Deployment and client setup: README.md.

usage:
  python3 server.py serve --db passnote.db --tokens tokens.json --bind 0.0.0.0:8765 [--cert cert.pem --key key.pem]
  python3 server.py users list                 who is registered, as tokens.json lines
  python3 server.py users add NAME HASH        register NAME with the hash /passnote:init printed for them (--replace
                                               changes the hash of a name that is already there, e.g. a new token)
  python3 server.py users remove NAME
  python3 server.py stats [--json]             per user: unread, received and sent messages, last activity (read-only)
  python3 server.py gen-token NAME             new token for NAME (give it to that person) and the line for tokens.json;
                                               usually each person runs /passnote:init instead and sends only that line
users and stats use the tokens.json and passnote.db next to this script unless --tokens / --db name others.

tokens.json maps each name to the SHA-256 of that person's token ({"alice": "<hex>", "bob": "<hex>"}); the server never
stores tokens. The sender of a message is the owner of the token, so nobody can send in someone else's name. The running
server reads tokens.json again whenever the file changes (users add / remove, or an edit by hand): no restart needed.

API (JSON; every call except /health needs "Authorization: Bearer <token>"):
  GET  /health                                         -> {"ok": true}
  GET  /whoami                                         -> {"user", "peers", "presence"}  the caller's name and
                                                                               everybody else's (since 0.2.0; older
                                                                               servers answer 404), and whether each of
                                                                               the others is around (since 0.4.0):
                                                                               {peer: {"online", "last_prompt"}}, see
                                                                               Presence below
  POST /send   {to, type, body, refs?, needs_reply?, reply_to?}
                                                       -> {"id", "created_at"}
  GET  /inbox?include_acked=0&limit=50                 -> {"user", "messages": [...]}  addressed to the caller,
                                                                               oldest first
  GET  /sent?limit=50                                  -> {"user", "messages": [...]}  sent by the caller, oldest
                                                                               first, each saying whether its recipient
                                                                               acked it (since 0.3.0; older servers
                                                                               answer 404)
  POST /ack    {"ids": [...]}                          -> {"acked": n}         only messages addressed to the caller
  POST /bye                                            -> {"ok": true}         the calling session closes: it stops
                                                                               counting as online at once (since 0.4.0)
A message: {id, from, to, type, body, refs, needs_reply, reply_to, created_at, acked, acked_at}; type is one of
info / request / interface_change / claim / done; body at most MAX_BODY characters (details go into a file the
message refers to).
Presence (since 0.4.0), kept in memory only (a restart forgets it) and computed on the server's clock, so clients'
clocks do not matter. Every authenticated request records it, unless it carries "X-Passnote-Presence: off":
  online        a request from that person's Claude side came within ONLINE_SECONDS (about 2.5 of the MCP server's
                heartbeats), from any of their sessions. "X-Passnote-Client: ui" marks the requests of the web page,
                which do not count; any other value, or none (older clients), is the Claude side.
                "X-Passnote-Session" names the session (a short SHA-256 prefix of its id; "-" when missing), so that
                /bye ends only that one and other sessions of the same person keep them online.
  last_prompt   whole seconds since a request with "X-Passnote-Event: prompt" (the UserPromptSubmit hook's) came from
                that person, or null if none did.
Entries older than FORGET_SECONDS are dropped.
"""
import argparse
import hashlib
import hmac
import json
import os
import pathlib
import re
import secrets
import sqlite3
import ssl
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TYPES = ("info", "request", "interface_change", "claim", "done")
MAX_BODY = 1000             # characters
MAX_REFS, MAX_REF = 10, 200
MAX_REQUEST = 16 * 1024     # bytes
SEND_PER_HOUR = 120         # per sender
ONLINE_SECONDS = 150        # presence: about 2.5 heartbeats (mcp_server.py sends one a minute)
FORGET_SECONDS = 86400      # presence older than a day is dropped
MAX_SESSIONS = 20           # presence: the most recent sessions kept per user
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}\Z")     # names users add accepts (client.py init: the same rule)
HASH = re.compile(r"[0-9a-f]{64}\Z")
SESSION = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")              # X-Passnote-Session values kept as they are, else "-"
HERE = os.path.dirname(os.path.abspath(__file__))

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sender TEXT NOT NULL,
  recipient TEXT NOT NULL,
  type TEXT NOT NULL,
  body TEXT NOT NULL,
  refs TEXT NOT NULL DEFAULT '[]',
  needs_reply INTEGER NOT NULL DEFAULT 0,
  reply_to INTEGER,
  created_at TEXT NOT NULL,
  acked_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_inbox ON messages (recipient, acked_at, id);
CREATE INDEX IF NOT EXISTS ix_sent ON messages (sender, id);
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def log(msg):
    sys.stderr.write(f"{now()} {msg}\n")


def load_users(path):
    """tokens.json -> {name: sha256 hex}. Raises OSError (unreadable) or ValueError (not such a table)."""
    with open(path, encoding="utf-8") as f:
        users = json.load(f)
    if not isinstance(users, dict) or not all(isinstance(k, str) and isinstance(v, str) and len(v) == 64
                                              for k, v in users.items()):
        raise ValueError("expected {name: sha256 hex of the token}")
    return users


class Users:
    """tokens.json, read again whenever the file changes, so adding or removing someone needs no restart. A file that
    cannot be read or parsed is reported in the log and the previous table stays in use."""

    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        self.sig = self.stat()
        self.table = load_users(path)

    def stat(self):
        try:
            st = os.stat(self.path)
            return st.st_mtime_ns, st.st_size, st.st_ino
        except OSError:
            return None

    def current(self):
        sig = self.stat()
        if sig != self.sig:
            with self.lock:
                if sig != self.sig:
                    self.sig = sig
                    try:
                        self.table = load_users(self.path)
                        log(f"tokens.json reloaded: {sorted(self.table)}")
                    except (OSError, ValueError) as e:
                        log(f"tokens.json not reloaded, still using {sorted(self.table)}: {e}")
        return self.table


class Store:
    """One SQLite connection behind a lock (traffic is a few requests a minute)."""

    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    @staticmethod
    def public(r):
        return {"id": r["id"], "from": r["sender"], "to": r["recipient"], "type": r["type"], "body": r["body"],
                "refs": json.loads(r["refs"]), "needs_reply": bool(r["needs_reply"]), "reply_to": r["reply_to"],
                "created_at": r["created_at"], "acked": r["acked_at"] is not None, "acked_at": r["acked_at"]}

    def send(self, sender, to, typ, body, refs, needs_reply, reply_to):
        with self.lock, self.db:
            if reply_to is not None:
                r = self.db.execute("SELECT sender, recipient FROM messages WHERE id = ?", (reply_to,)).fetchone()
                if r is None or sender not in (r["sender"], r["recipient"]):
                    raise ValueError("reply_to: no such message in your conversations")
            ts = now()
            cur = self.db.execute(
                "INSERT INTO messages (sender, recipient, type, body, refs, needs_reply, reply_to, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (sender, to, typ, body, json.dumps(refs), int(needs_reply), reply_to, ts))
            return {"id": cur.lastrowid, "created_at": ts}

    def inbox(self, user, include_acked, limit):
        q = "SELECT * FROM messages WHERE recipient = ?" + ("" if include_acked else " AND acked_at IS NULL")
        with self.lock:
            rows = self.db.execute(q + " ORDER BY id DESC LIMIT ?", (user, limit)).fetchall()
        return [self.public(r) for r in reversed(rows)]

    def sent(self, user, limit):
        with self.lock:
            rows = self.db.execute("SELECT * FROM messages WHERE sender = ? ORDER BY id DESC LIMIT ?",
                                   (user, limit)).fetchall()
        return [self.public(r) for r in reversed(rows)]

    def ack(self, user, ids):
        with self.lock, self.db:
            cur = self.db.execute(
                f"UPDATE messages SET acked_at = ? WHERE recipient = ? AND acked_at IS NULL AND id IN "
                f"({','.join('?' * len(ids))})", (now(), user, *ids))
            return cur.rowcount


class Presence:
    """Who is around (module docstring: Presence), in memory only. Times are time.monotonic(), so a change of the
    server's clock does not shift them."""

    def __init__(self):
        self.lock = threading.Lock()
        self.seen = {}          # user -> {session: time of its last request from the Claude side}
        self.prompt = {}        # user -> time of their last prompt

    def record(self, user, session, claude, prompt):
        t = time.monotonic()
        with self.lock:
            if claude:
                s = self.seen.setdefault(user, {})
                s[session] = t
                if len(s) > MAX_SESSIONS:
                    del s[min(s, key=s.get)]
            if prompt:
                self.prompt[user] = t

    def bye(self, user, session):
        with self.lock:
            s = self.seen.get(user, {})
            s.pop(session, None)
            if not s:
                self.seen.pop(user, None)

    def of(self, names):
        """-> {name: {"online": bool, "last_prompt": whole seconds since their last prompt, or None}}"""
        t = time.monotonic()
        with self.lock:
            for user in list(self.seen):
                self.seen[user] = {k: v for k, v in self.seen[user].items() if t - v < FORGET_SECONDS}
                if not self.seen[user]:
                    del self.seen[user]
            self.prompt = {k: v for k, v in self.prompt.items() if t - v < FORGET_SECONDS}
            return {n: {"online": any(t - v < ONLINE_SECONDS for v in self.seen.get(n, {}).values()),
                        "last_prompt": int(t - self.prompt[n]) if n in self.prompt else None} for n in names}


class Handler(BaseHTTPRequestHandler):
    server_version = "passnote/1"
    timeout = 10                # per connection (also bounds the TLS handshake, which runs in the handler's thread)
    store = users = presence = None     # set in serve(): Store, Users, Presence
    sent = {}                   # sender -> send timestamps of the last hour

    def log_message(self, fmt, *args):     # method, path and status only: never tokens or bodies
        log(f"{self.address_string()} {fmt % args}")

    def reply(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def user(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        h = token_hash(auth[7:].strip())
        for name, want in self.users.current().items():
            if hmac.compare_digest(h, want):
                return name
        return None

    def session(self):
        s = self.headers.get("X-Passnote-Session", "")
        return s if SESSION.match(s) else "-"

    def note(self, user):
        """Record this request of user's for presence, unless it asks not to be."""
        h = self.headers
        if h.get("X-Passnote-Presence", "").strip().lower() != "off":
            self.presence.record(user, self.session(), h.get("X-Passnote-Client", "").strip().lower() != "ui",
                                 h.get("X-Passnote-Event", "").strip().lower() == "prompt")

    def payload(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_REQUEST:
            raise ValueError("request too large")
        obj = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(obj, dict):
            raise ValueError("expected a JSON object")
        return obj

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            return self.reply(200, {"ok": True})
        user = self.user()
        if user is None:
            return self.reply(401, {"error": "unauthorized"})
        self.note(user)
        if u.path == "/whoami":
            peers = sorted(n for n in self.users.current() if n != user)
            return self.reply(200, {"user": user, "peers": peers, "presence": self.presence.of(peers)})
        if u.path not in ("/inbox", "/sent"):
            return self.reply(404, {"error": "not found"})
        q = parse_qs(u.query)
        try:
            limit = max(1, min(200, int(q.get("limit", ["50"])[0])))
        except ValueError:
            return self.reply(400, {"error": "limit must be an integer"})
        if u.path == "/sent":
            return self.reply(200, {"user": user, "messages": self.store.sent(user, limit)})
        include_acked = q.get("include_acked", ["0"])[0] in ("1", "true")
        self.reply(200, {"user": user, "messages": self.store.inbox(user, include_acked, limit)})

    def do_POST(self):
        user = self.user()
        if user is None:
            return self.reply(401, {"error": "unauthorized"})
        if self.path == "/bye":
            self.presence.bye(user, self.session())
            return self.reply(200, {"ok": True})
        self.note(user)
        try:
            p = self.payload()
            if self.path == "/send":
                return self.reply(200, self.send_message(user, p))
            if self.path == "/ack":
                ids = p.get("ids")
                if not isinstance(ids, list) or not ids or len(ids) > 200 or \
                        not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
                    raise ValueError("ids: a non-empty list of message ids")
                return self.reply(200, {"acked": self.store.ack(user, ids)})
            return self.reply(404, {"error": "not found"})
        except (ValueError, json.JSONDecodeError) as e:
            return self.reply(400, {"error": str(e)})

    def send_message(self, user, p):
        to, typ, body = p.get("to"), p.get("type"), p.get("body")
        refs, needs_reply, reply_to = p.get("refs", []), p.get("needs_reply", False), p.get("reply_to")
        users = self.users.current()
        if to not in users:
            raise ValueError(f"to: one of {sorted(users)}")
        if typ not in TYPES:
            raise ValueError(f"type: one of {list(TYPES)}")
        if not isinstance(body, str) or not body.strip() or len(body) > MAX_BODY:
            raise ValueError(f"body: non-empty text of at most {MAX_BODY} characters (put details into a file and "
                             "reference it in refs)")
        if not isinstance(refs, list) or len(refs) > MAX_REFS or \
                not all(isinstance(r, str) and 0 < len(r) <= MAX_REF for r in refs):
            raise ValueError(f"refs: a list of at most {MAX_REFS} strings (commit SHAs, file paths)")
        if not isinstance(needs_reply, bool):
            raise ValueError("needs_reply: true or false")
        if reply_to is not None and (not isinstance(reply_to, int) or isinstance(reply_to, bool)):
            raise ValueError("reply_to: a message id")
        t = time.time()
        recent = [s for s in self.sent.get(user, []) if t - s < 3600]
        if len(recent) >= SEND_PER_HOUR:
            raise ValueError("rate limit: too many messages in the last hour")
        self.sent[user] = recent + [t]
        return self.store.send(user, to, typ, body.strip(), refs, needs_reply, reply_to)


def serve(a):
    try:
        users = Users(a.tokens)
    except (OSError, ValueError) as e:
        sys.exit(f"{a.tokens}: {e}")
    Handler.store, Handler.users, Handler.presence = Store(a.db), users, Presence()
    host, port = a.bind.rsplit(":", 1)
    httpd = ThreadingHTTPServer((host, int(port)), Handler)
    scheme = "http"
    if a.cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(a.cert, a.key)
        # handshake on first read, in the connection's own thread: a client that connects and stays silent must not
        # block accept() for everybody else
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
        scheme = "https"
    log(f"passnote on {scheme}://{a.bind} for {sorted(users.table)}"
        + ("" if users.table else " (nobody yet: add people with `server.py users add`)"))
    httpd.serve_forever()


def save_users(path, table):
    """Write tokens.json atomically (a running server never sees half a file), keeping the mode and owner of the old
    file (0600 for a new one), so a server running as another user can still read it."""
    tmp = path + ".tmp"
    try:
        os.remove(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(table.items())), f, ensure_ascii=False, indent=1)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    try:
        st = os.stat(path)
        os.chmod(tmp, st.st_mode & 0o777)
        if hasattr(os, "chown"):
            os.chown(tmp, st.st_uid, st.st_gid)
    except OSError:
        pass
    os.replace(tmp, path)


def users_cmd(a):
    path = a.tokens
    try:
        table = load_users(path) if a.action != "add" or os.path.exists(path) else {}
    except (OSError, ValueError) as e:
        sys.exit(f"{path}: {e}")
    if a.action == "list":
        print(f"{path}: {len(table)} user(s)")
        for name in sorted(table):
            print(f'"{name}": "{table[name]}"')
        return
    name = a.name
    if a.action == "remove":
        if name not in table:
            sys.exit(f"{name} is not in {path} (there: {', '.join(sorted(table)) or 'nobody'})")
        del table[name]
        save_users(path, table)
        print(f"removed {name} from {path}; a running server stops accepting their token at once (messages to and "
              "from them stay in the database)")
        return
    h = a.hash.strip().lower()
    if not NAME.match(name):
        sys.exit(f"bad name {name!r}: 1 to 40 ASCII letters, digits, '.', '_' or '-', starting with a letter or digit")
    if not HASH.match(h):
        sys.exit("bad hash: give the 64 hex digits from the line /passnote:init printed (the SHA-256 of the token, "
                 "never the token itself)")
    other = next((n for n, v in table.items() if v.lower() == h and n != name), None)
    if other is not None:
        sys.exit(f"this hash already belongs to {other}: everybody needs their own token")
    if table.get(name) == h:
        print(f"{name} is already registered with this hash in {path}; nothing to do")
        return
    if name in table and not a.replace:
        sys.exit(f"{name} is already registered with another hash; add --replace to change it (after a new token)")
    verb = "replaced the hash of" if name in table else "added"
    table[name] = h
    save_users(path, table)
    print(f"{verb} {name} in {path}; a running server accepts the token at once")


def stats(a):
    """Per user, from the database opened read-only: unread, received and sent messages, and when they last sent or
    acked a message (reading the inbox leaves no trace in the database)."""
    if not os.path.isfile(a.db):
        sys.exit(f"no database at {a.db}")
    # a running server keeps passnote.db-wal open: read alongside it; without one nothing writes the file, and
    # immutable=1 reads it without creating -wal / -shm files (which, made by root, would lock the server out)
    mode = "mode=ro" if os.path.exists(a.db + "-wal") else "immutable=1"
    db = sqlite3.connect(pathlib.Path(a.db).resolve().as_uri() + "?" + mode, uri=True)
    try:
        rows = db.execute(
            "SELECT name, SUM(unread), SUM(received), SUM(sent), MAX(last) FROM ("
            " SELECT recipient AS name, acked_at IS NULL AS unread, 1 AS received, 0 AS sent, acked_at AS last"
            " FROM messages UNION ALL"
            " SELECT sender, 0, 0, 1, created_at FROM messages) GROUP BY name").fetchall()
    finally:
        db.close()
    try:
        registered = set(load_users(a.tokens))
    except (OSError, ValueError):
        registered = None
    out = {r[0]: {"unread": r[1], "received": r[2], "sent": r[3], "last_activity": r[4]} for r in rows}
    for name in registered or ():
        out.setdefault(name, {"unread": 0, "received": 0, "sent": 0, "last_activity": None})
    for name, v in out.items():
        v["registered"] = None if registered is None else name in registered
    if a.json:
        print(json.dumps(dict(sorted(out.items())), ensure_ascii=False, indent=1))
        return
    w = max([4] + [len(n) for n in out])
    print(f"{'user':<{w}}  unread  received  sent  last activity (sent or acked)")
    for name, v in sorted(out.items()):
        note = "  (not in tokens.json)" if v["registered"] is False else ""
        print(f"{name:<{w}}  {v['unread']:>6}  {v['received']:>8}  {v['sent']:>4}  {v['last_activity'] or '-'}{note}")
    if registered is None:
        print(f"({a.tokens} not readable: only users with messages are listed)")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--db", required=True)
    s.add_argument("--tokens", required=True)
    s.add_argument("--bind", default="127.0.0.1:8765")
    s.add_argument("--cert", default="")
    s.add_argument("--key", default="")
    tokens = os.path.join(HERE, "tokens.json")
    u = sub.add_parser("users", help="list, add or remove users in tokens.json (a running server picks changes up)")
    u.add_argument("--tokens", default=tokens, help="default: tokens.json next to server.py")
    us = u.add_subparsers(dest="action", required=True)
    us.add_parser("list")
    ua = us.add_parser("add")
    ua.add_argument("name")
    ua.add_argument("hash", help="SHA-256 of their token, from the line /passnote:init printed")
    ua.add_argument("--replace", action="store_true", help="change the hash of a name that is already registered")
    us.add_parser("remove").add_argument("name")
    st = sub.add_parser("stats", help="per user: unread, received and sent messages, last activity (read-only)")
    st.add_argument("--db", default=os.path.join(HERE, "passnote.db"), help="default: passnote.db next to server.py")
    st.add_argument("--tokens", default=tokens, help="default: tokens.json next to server.py")
    st.add_argument("--json", action="store_true")
    g = sub.add_parser("gen-token")
    g.add_argument("name")
    a = p.parse_args()
    if a.cmd == "gen-token":
        tok = secrets.token_urlsafe(32)
        print(f"token for {a.name} (give it to them; it is not stored anywhere): {tok}")
        print(f'tokens.json entry: "{a.name}": "{token_hash(tok)}"')
    elif a.cmd == "users":
        users_cmd(a)
    elif a.cmd == "stats":
        stats(a)
    else:
        serve(a)


if __name__ == "__main__":
    main()
