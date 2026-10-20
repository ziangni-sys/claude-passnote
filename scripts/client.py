#!/usr/bin/env python3
"""Passnote client shared by mcp_server.py and hook.py, plus a small command line. Standard library only.

Configuration (environment variables win over the local config file):
  PASSNOTE_URL, PASSNOTE_TOKEN  server address (https://host:port) and your token
  PASSNOTE_CA                   certificate to trust for https (default: ca.pem in PASSNOTE_HOME if it exists, else the
                                system's trusted certificates)
  PASSNOTE_HOME                 local directory, default ~/.config/claude-passnote; holds config.json ({"url", "token",
                                "ca", "name"}, written by `init`), ca.pem, the list of enabled sessions and the hooks'
                                per-session state. It is outside the plugin, so updating or uninstalling the plugin
                                keeps it.
Opt-in: passnote is active only in sessions that turned it on, so other conversations see no tools, get no hook output
and make no network calls:
  PASSNOTE_ENABLE=1             in the environment of that session (a shell or preset that sets it), or
  python3 client.py enable      adds the current session (--session, else CLAUDE_CODE_SESSION_ID) to
                                PASSNOTE_HOME/sessions; the /passnote:enable command does this
  PASSNOTE_ENABLE=0             forces it off.

usage: python3 client.py {init,hash,enable,disable,status,inbox,send,ack} ...
  init --name NAME --url URL [--ca CERT]
                                create your token locally (saved in config.json, never printed) and print the line for
                                the server's tokens.json (only the token's SHA-256, which is not secret); --ca copies the
                                server's self-signed certificate to PASSNOTE_HOME/ca.pem. Run again with a new URL or
                                certificate to switch servers: the token stays the same
  hash [--name NAME]            print that line again (default name: the one given to init)
  status                        this session, the configuration, and who you are and whom you can write to
"""
import argparse
import json
import os
import re
import shutil
import ssl
import sys
import urllib.error
import urllib.request

TYPES = ("info", "request", "interface_change", "claim", "done")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}\Z")     # what server.py users add accepts
NAME_RULE = "1 to 40 ASCII letters, digits, '.', '_' or '-', starting with a letter or digit"


class PassnoteError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code        # HTTP status, when the server answered with an error


def home():
    return os.environ.get("PASSNOTE_HOME") or os.path.join(os.path.expanduser("~"), ".config", "claude-passnote")


def config():
    """-> {"url", "token", "ca", "name"} (url and token may be empty when not configured, ca when the system store is
    used, name when init has not recorded it)."""
    c = {}
    try:
        with open(os.path.join(home(), "config.json"), encoding="utf-8") as f:
            c = json.load(f)
    except (OSError, ValueError):
        pass
    url = (os.environ.get("PASSNOTE_URL") or c.get("url") or "").rstrip("/")
    token = os.environ.get("PASSNOTE_TOKEN") or c.get("token") or ""
    ca = os.environ.get("PASSNOTE_CA") or c.get("ca") or ""
    if not ca and os.path.exists(os.path.join(home(), "ca.pem")):
        ca = os.path.join(home(), "ca.pem")
    return {"url": url, "token": token, "ca": ca, "name": c.get("name") or ""}


def session_id(given=None):
    # a command template whose ${...} was not substituted must not become a session id
    if given and "${" not in given:
        return given
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "")


def enabled_sessions():
    try:
        with open(os.path.join(home(), "sessions"), encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
    except OSError:
        return set()


def enabled(sid=None):
    flag = os.environ.get("PASSNOTE_ENABLE", "")
    if flag in ("0", "1"):
        return flag == "1"
    sid = session_id(sid)
    return bool(sid) and sid in enabled_sessions()


def configured():
    c = config()
    return bool(c["url"] and c["token"])


def call(method, path, payload=None, timeout=5.0):
    """One request to the server -> the decoded JSON. Raises PassnoteError (not configured, unreachable, rejected)."""
    c = config()
    if not (c["url"] and c["token"]):
        raise PassnoteError("passnote is not configured: run /passnote:init (or set PASSNOTE_URL and PASSNOTE_TOKEN)")
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(c["url"] + path, data=data, method=method,
                                 headers={"Authorization": "Bearer " + c["token"], "Content-Type": "application/json"})
    ctx = None
    if c["url"].startswith("https://"):
        if c["ca"] and not os.path.exists(c["ca"]):
            raise PassnoteError(f"certificate file not found: {c['ca']}")
        try:
            ctx = ssl.create_default_context(cafile=c["ca"] or None)
        except ssl.SSLError as e:
            raise PassnoteError(f"cannot load the certificate {c['ca']}: {e}") from None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error", "")
        except (ValueError, OSError):
            msg = ""
        raise PassnoteError(f"server said {e.code}: {msg}", e.code) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise PassnoteError(f"server unreachable: {e}") from None


def send(to, type, body, refs=(), needs_reply=False, reply_to=None, timeout=5.0):
    p = {"to": to, "type": type, "body": body, "refs": list(refs), "needs_reply": bool(needs_reply)}
    if reply_to is not None:
        p["reply_to"] = int(reply_to)
    return call("POST", "/send", p, timeout)


def inbox(include_acked=False, limit=50, timeout=5.0):
    return call("GET", f"/inbox?include_acked={int(bool(include_acked))}&limit={int(limit)}", timeout=timeout)["messages"]


def sent(limit=50, timeout=5.0):
    """Messages you sent, oldest first, each saying whether its recipient acked it. A server older than 0.3.0 answers
    404 (PassnoteError with code 404)."""
    return call("GET", f"/sent?limit={int(limit)}", timeout=timeout)["messages"]


def ack(ids, timeout=5.0):
    return call("POST", "/ack", {"ids": [int(i) for i in ids]}, timeout)["acked"]


def whoami(timeout=5.0):
    """-> {"user": your name on the server, "peers": the others' names, or None when the server is older than 0.2.0
    and cannot list them (it answers /whoami with 404; its /inbox still names the caller)}."""
    try:
        w = call("GET", "/whoami", timeout=timeout)
    except PassnoteError as e:
        if e.code != 404:
            raise
        inbox = call("GET", "/inbox?limit=1", timeout=timeout)
        w = {"user": inbox.get("user") if isinstance(inbox, dict) else None, "peers": None}
    if not isinstance(w, dict) or not isinstance(w.get("user"), str) or \
            not (w.get("peers") is None or isinstance(w["peers"], list)):
        raise PassnoteError(f"unexpected answer from the server: {w}")
    return {"user": w["user"], "peers": None if w["peers"] is None else [str(p) for p in w["peers"]]}


def describe(who):
    """whoami() -> one line for people."""
    if who["peers"] is None:
        return (f"you are {who['user']}; the others are unknown (this server is older than passnote 0.2.0 and does not "
                "list them)")
    if not who["peers"]:
        return f"you are {who['user']}; nobody else is registered on this server yet"
    return f"you are {who['user']}; you can send to {', '.join(who['peers'])}"


def token_line(name):
    """The line for the server's tokens.json, '"name": "<SHA-256 of your token>"' (the hash is not secret)."""
    import hashlib
    token = config()["token"]
    if not token:
        raise PassnoteError("no token yet: run /passnote:init first")
    return f'"{name}": "{hashlib.sha256(token.encode()).hexdigest()}"'


def _set_session(sid, on):
    os.makedirs(home(), exist_ok=True)
    s = enabled_sessions()
    s = (s | {sid}) if on else (s - {sid})
    tmp = os.path.join(home(), "sessions.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("".join(x + "\n" for x in sorted(s)))
    os.replace(tmp, os.path.join(home(), "sessions"))


def init(name, url, ca=""):
    """Create your token locally: saved in PASSNOTE_HOME/config.json (never printed), with your name; returns the
    tokens.json line with its SHA-256, which is all the server needs (the hash is not secret). An existing token is kept,
    so running it again with another URL or certificate switches servers. ca: the server's certificate, copied to
    PASSNOTE_HOME/ca.pem."""
    import hashlib
    import secrets
    if not NAME.match(name):
        raise PassnoteError(f"bad name {name!r}: {NAME_RULE}")
    os.makedirs(home(), exist_ok=True)
    path = os.path.join(home(), "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            c = json.load(f)
    except (OSError, ValueError):
        c = {}
    if url:
        c["url"] = url.rstrip("/")
    if ca:
        shutil.copyfile(ca, os.path.join(home(), "ca.pem"))
        c.pop("ca", None)                       # ca.pem in PASSNOTE_HOME is the default
    if not c.get("token"):
        c["token"] = secrets.token_urlsafe(32)
    c["name"] = name
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(c, f)
    os.replace(path + ".tmp", path)
    return path, f'"{name}": "{hashlib.sha256(c["token"].encode()).hexdigest()}"'


def main():
    p = argparse.ArgumentParser(description="passnote client (see README.md)")
    sub = p.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("init", help="create your token locally and print the line for the server's tokens.json")
    q.add_argument("--name", required=True, help="your mailbox name, as the others will address you")
    q.add_argument("--url", default="", help="server address, e.g. https://203.0.113.5:8765")
    q.add_argument("--ca", default="", help="the server's self-signed certificate (not needed for a CA-signed one)")
    q = sub.add_parser("hash", help="print your line for the server's tokens.json again (the token stays the same)")
    q.add_argument("--name", default="", help="your name on the server (default: the one given to init)")
    for name in ("enable", "disable", "status"):
        q = sub.add_parser(name)
        q.add_argument("--session", default="", help="session id (default: $CLAUDE_CODE_SESSION_ID)")
    q = sub.add_parser("inbox")
    q.add_argument("--all", action="store_true", help="include acked messages")
    q = sub.add_parser("send")
    q.add_argument("--to", required=True)
    q.add_argument("--type", required=True, choices=TYPES)
    q.add_argument("--body", required=True)
    q.add_argument("--refs", nargs="*", default=[])
    q.add_argument("--needs_reply", action="store_true")
    q.add_argument("--reply_to", type=int)
    q = sub.add_parser("ack")
    q.add_argument("ids", type=int, nargs="+")
    a = p.parse_args()
    out = sys.stdout.buffer
    try:
        if a.cmd == "init":
            path, line = init(a.name, a.url, a.ca)
            print(f"token saved in {path} (keep it there; PASSNOTE_TOKEN, if set, overrides it)\n"
                  "line for the server's tokens.json (not secret; send it to whoever runs the server, who adds it with "
                  f"`python3 server.py users add NAME HASH`):\n{line}")
        elif a.cmd == "hash":
            name = a.name or config()["name"]
            if not name:
                sys.exit("give your name on the server: hash --name NAME")
            if not NAME.match(name):
                sys.exit(f"bad name {name!r}: {NAME_RULE}")
            print(token_line(name))
        elif a.cmd in ("enable", "disable"):
            sid = session_id(a.session)
            if not sid:
                sys.exit("no session id: run this inside a Claude Code session or pass --session")
            _set_session(sid, a.cmd == "enable")
            print(f"passnote {a.cmd}d for session {sid} ({os.path.join(home(), 'sessions')}); the hooks take effect at "
                  f"once, the MCP tools {'appear' if a.cmd == 'enable' else 'go away'} within a few seconds, without a "
                  "restart (if they do not, select plugin:passnote:passnote in /mcp and choose Reconnect)")
        elif a.cmd == "status":
            c, sid = config(), session_id(a.session)
            print(f"session {sid or '-'}: enabled {enabled(sid)}, configured {configured()}\n"
                  f"url {c['url'] or '-'}, certificate {c['ca'] or 'system store'}, home {home()}")
            if configured():
                print("server:", call("GET", "/health"))
                try:
                    who = whoami()
                    print(describe(who))
                except PassnoteError as e:
                    if e.code != 401:
                        raise
                    who = None
                    print(f"{e}: the server does not know your token yet; send the line below to whoever runs it")
                name = who["user"] if who else c["name"]
                print("your line for the server's tokens.json:", token_line(name or "<your name>"))
                if who is None:
                    sys.exit(1)
        elif a.cmd == "inbox":
            out.write((json.dumps(inbox(a.all), ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
        elif a.cmd == "send":
            print(json.dumps(send(a.to, a.type, a.body, a.refs, a.needs_reply, a.reply_to)))
        else:
            print(json.dumps({"acked": ack(a.ids)}))
    except PassnoteError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
