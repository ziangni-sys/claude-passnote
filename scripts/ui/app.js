"use strict";
/* passnote ui: the page scripts/ui.py serves. Names, bodies and refs come from other people: they go into the page only
   as text (text nodes, textContent), never as HTML. */

const TYPES = {
  info: "For reading. No reply expected.",
  request: "Asks them to do something.",
  interface_change: "Something others use changed: put the commit SHA in refs.",
  claim: "You are taking a task, so nobody else starts it.",
  done: "You finished something.",
};
const LABEL = { info: "info", request: "request", interface_change: "interface change", claim: "claim", done: "done" };
const POLL_MS = 15000;
const MAX_BODY = 1000, MAX_REFS = 10, MAX_REF = 200;

const $ = (sel) => document.querySelector(sel);

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === undefined || kid === null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

/* The key ui.py printed travels in the address fragment. Keep it for reloads of this tab and take it out of the
   address bar; if the browser keeps no session storage, leave it in the address. */
const KEY = (() => {
  const m = location.hash.match(/key=([A-Za-z0-9_-]+)/);
  try {
    if (m) {
      sessionStorage.setItem("passnote-key", m[1]);
      history.replaceState(null, "", location.pathname);
      return m[1];
    }
    return sessionStorage.getItem("passnote-key") || "";
  } catch (e) {
    return m ? m[1] : "";
  }
})();

const st = {
  view: "inbox",          // inbox | sent
  filter: "unread",       // inbox: unread | all
  status: null,           // the last /api/state answer
  inbox: [],              // the last messages we got; kept while the server cannot be reached
  sent: null,             // null: this server cannot list them
  presence: null,         // Map name -> {online, prompt: ISO time on this computer's clock or null}; null: not known
  loaded: false,
  offline: "",            // ui.py itself does not answer
  locked: false,
  checked: 0,
  sig: "",
};

async function api(path, body) {
  const opts = { method: "GET", headers: { "X-Passnote-Key": KEY }, cache: "no-store" };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let r;
  try {
    r = await fetch(path, opts);
  } catch (e) {
    throw Object.assign(new Error("the interface stopped: start /passnote:ui again"), { offline: true });
  }
  let j = {};
  try { j = await r.json(); } catch (e) { /* not JSON */ }
  if (r.status === 403) throw Object.assign(new Error(j.error || "forbidden"), { locked: true });
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

/* refresh: one request at a time; a call while one is on its way runs once more after it */
let inflight = null, again = false;
function refresh() {
  if (inflight) { again = true; return inflight; }
  inflight = (async () => {
    try {
      const s = await api("/api/state");
      st.status = s;
      st.offline = "";
      if (s.user) st.presence = presence(s.presence);
      if (s.inbox) {
        st.inbox = s.inbox;
        st.sent = s.sent;
        st.loaded = true;
      }
    } catch (e) {
      if (e.locked) st.locked = true;
      else st.offline = e.message;
    }
    st.checked = Date.now();
    inflight = null;
    render();
    if (again) { again = false; refresh(); }
  })();
  return inflight;
}

const unread = () => st.inbox.filter((m) => !m.acked);
const byId = () => new Map([...st.inbox, ...(st.sent || [])].map((m) => [m.id, m]));

/* the server says how many seconds ago each of the others last sent a prompt; turn that into a time on this computer's
   clock once per answer, so that it ticks on with the other times. null: a server older than 0.4.0 */
function presence(p) {
  if (!p || typeof p !== "object") return null;
  const now = Date.now(), out = new Map();
  for (const [name, v] of Object.entries(p)) {
    if (!v || typeof v !== "object") continue;
    const s = v.last_prompt;
    out.set(name, { online: v.online === true,
      prompt: Number.isFinite(s) && s >= 0 ? new Date(now - s * 1000).toISOString() : null });
  }
  return out;
}
const isOnline = (name) => !!(st.presence && st.presence.get(name) && st.presence.get(name).online);

/* time */
function ago(iso) {
  const t = Date.parse(iso);
  if (isNaN(t)) return iso || "";
  const s = (Date.now() - t) / 1000;
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  const d = new Date(t);
  const hm = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  if (s < 6 * 86400) return `${d.toLocaleDateString(undefined, { weekday: "short" })} ${hm}`;
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}
function timeEl(iso) {
  const t = Date.parse(iso);
  return h("time", { datetime: iso, title: isNaN(t) ? iso : new Date(t).toLocaleString() }, ago(iso));
}
function tickTimes() {
  document.querySelectorAll("time[datetime]").forEach((el) => { el.textContent = ago(el.getAttribute("datetime")); });
  const conn = $("#conn");
  if (st.checked) conn.title = `checked ${ago(new Date(st.checked).toISOString())}`;
}

function avatar(name) {
  let n = 0;
  for (const c of String(name)) n = (n * 31 + c.codePointAt(0)) >>> 0;
  return h("span", { class: `avatar av-${n % 8}`, "aria-hidden": "true" }, [...String(name)][0] || "?");
}
const badge = (type) => h("span", { class: `badge t-${TYPES[type] ? type : "info"}` }, LABEL[type] || type);

/* render */
function render() {
  $("#locked").hidden = !st.locked;
  if (st.locked) {
    document.title = "passnote";
    return;
  }
  renderTop();
  renderBanner();
  renderSide();
  renderList();
  tickTimes();
  const n = unread().length, user = st.status && st.status.user;
  document.title = `${n ? `(${n}) ` : ""}passnote${user ? ` · ${user}` : ""}`;
}

function renderTop() {
  const s = st.status || {};
  const name = s.user || s.name;
  $("#who").replaceChildren(
    name ? h("strong", {}, name) : h("span", { class: "muted" }, st.status ? "not set up" : ""),
    s.url ? h("span", { class: "url", title: `certificate: ${s.certificate || "the system's"}` }, s.url) : null);
  let cls = "ok", text = "connected";
  if (st.offline) [cls, text] = ["bad", "interface stopped"];
  else if (!st.status) [cls, text] = ["wait", "connecting…"];
  else if (!s.configured) [cls, text] = ["warn", "not set up"];
  else if (s.code === 401) [cls, text] = ["warn", "not registered"];
  else if (s.code) [cls, text] = ["bad", `server error ${s.code}`];
  else if (s.error) [cls, text] = ["bad", "server unreachable"];
  const conn = $("#conn");
  conn.className = `conn ${cls}`;
  conn.replaceChildren(h("span", { class: "dot" }), text);
}

function renderBanner() {
  const b = $("#banner"), s = st.status || {};
  let kind = "", kids = [];
  if (st.offline) {
    kind = "bad";
    kids = [h("p", {}, h("strong", {}, "The page lost its connection to this computer's passnote interface. "),
      "If it was stopped, run ", h("code", {}, "/passnote:ui"), " again: it opens a new address.")];
  } else if (st.status && !s.configured) {
    kids = [h("p", {}, h("strong", {}, "Passnote is not set up on this computer yet. "), "In Claude Code, run ",
      h("code", {}, "/passnote:init <your-name> <server-url> [server-cert.pem]"), ", then reload this page.")];
  } else if (s.code === 401) {
    kids = [
      h("p", {}, h("strong", {}, "The server does not know this computer's token yet. "),
        "Send this line to whoever runs the server. They add it with ",
        h("code", {}, "python3 server.py users add NAME HASH"), "; no restart is needed. The line is not secret."),
      s.line ? h("div", { class: "line" }, h("code", {}, s.line),
        h("button", { type: "button", onclick: () => copy(s.line) }, "Copy")) : null];
  } else if (s.error) {
    kind = "bad";
    kids = [h("p", {}, h("strong", {}, "Cannot ask the server. "), s.error,
      st.loaded ? " Showing what was loaded before." : "")];
  }
  b.hidden = !kids.length;
  b.className = `banner ${kind}`;
  b.replaceChildren(...kids);
}

function renderSide() {
  document.querySelectorAll(".box").forEach((el) => el.classList.toggle("active", el.dataset.view === st.view));
  const n = unread().length;
  $("#count-inbox").textContent = n ? String(n) : "";
  const waiting = st.sent ? st.sent.filter((m) => !m.acked).length : 0;
  $("#count-sent").textContent = waiting ? String(waiting) : "";
  const s = st.status || {};
  const people = $("#people");
  if (!s.user) {
    people.replaceChildren(h("li", { class: "note" }, st.status ? "Nobody to show until the server answers." : ""));
    return;
  }
  if (s.peers === null) {
    people.replaceChildren(h("li", { class: "note" },
      "This server is older than passnote 0.2.0 and does not list the others. You can still write to them by name."));
    return;
  }
  if (!s.peers.length) {
    people.replaceChildren(h("li", { class: "note" }, "Nobody else is registered on this server yet."));
    return;
  }
  const from = {};
  for (const m of unread()) from[m.from] = (from[m.from] || 0) + 1;
  people.replaceChildren(...s.peers.map((p) => {
    const seen = st.presence ? st.presence.get(p) : undefined;
    return h("li", {},
      h("button", { type: "button", class: "person", title: `Write to ${p}`, onclick: () => openCompose({ to: p }) },
        h("span", { class: "av-wrap" }, avatar(p), seen ? dot(seen.online) : null),
        h("span", { class: "name-col" }, h("span", { class: "name" }, p),
          seen ? h("span", { class: "seen" }, seen.prompt ? ["last prompt ", timeEl(seen.prompt)] : "no prompt seen") : null),
        from[p] ? h("span", { class: "count", title: "unread from them" }, from[p]) : h("span", { class: "write" }, "write")));
  }));
}

function dot(online) {
  const text = online ? "online: a Claude Code session of theirs with passnote on is open"
    : "offline: no Claude Code session of theirs with passnote on was heard from in the last few minutes";
  return h("span", { class: `presence ${online ? "on" : "off"}`, role: "img", "aria-label": online ? "online" : "offline",
    title: text });
}

function renderList() {
  const inbox = st.view === "inbox";
  const all = inbox ? st.inbox : st.sent || [];
  const shown = (inbox && st.filter === "unread" ? all.filter((m) => !m.acked) : all).slice().reverse();
  const sig = JSON.stringify([st.view, st.filter, st.loaded, st.sent === null, shown, inbox ? st.sent : st.inbox]);
  renderToolbar();
  if (sig === st.sig) return;     // nothing changed: keep the list, and any text selected in it
  st.sig = sig;
  const list = $("#messages");
  if (!st.loaded) {
    list.replaceChildren(st.status && st.status.error ? empty("No messages loaded", "They show up here once the server answers.") : "");
    return;
  }
  if (!inbox && st.sent === null) {
    list.replaceChildren(empty("This server cannot list what you sent",
      "It runs passnote older than 0.3.0. Messages work; to see them here, whoever runs the server copies the new server.py over and restarts it once."));
    return;
  }
  if (!shown.length) {
    list.replaceChildren(inbox
      ? (st.filter === "unread" ? empty("Nothing unread", "New messages show up here by themselves.")
        : empty("No messages yet", "Messages addressed to you show up here."))
      : empty("Nothing sent yet", "What you and your agent send shows up here, with whether it was acked."));
    return;
  }
  const ids = byId();
  list.replaceChildren(...shown.map((m) => card(m, !inbox, ids)));
}

function empty(title, text) {
  return h("li", { class: "empty" }, h("strong", {}, title), text);
}

function renderToolbar() {
  const tb = $("#toolbar");
  if (st.view === "sent") {
    tb.replaceChildren(h("h1", {}, "Sent"),
      h("span", { class: "sub" }, "what you sent, and whether each recipient has acked it"));
    return;
  }
  const n = unread().length;
  const seg = h("div", { class: "seg", role: "group", "aria-label": "Show" },
    h("button", { type: "button", class: st.filter === "unread" ? "on" : "", "aria-pressed": st.filter === "unread",
      onclick: () => { st.filter = "unread"; render(); } }, "Unread", h("span", { class: "n" }, n)),
    h("button", { type: "button", class: st.filter === "all" ? "on" : "", "aria-pressed": st.filter === "all",
      onclick: () => { st.filter = "all"; render(); } }, "All", h("span", { class: "n" }, st.inbox.length)));
  const kids = [h("h1", {}, "Inbox"), seg, h("span", { class: "spacer" })];
  if (n) kids.push(ackAllButton(n));
  tb.replaceChildren(...kids);
}

/* "Ack all" asks once more on the button itself: acking cannot be undone */
let ackAllArmed = 0;
function ackAllButton(n) {
  const armed = Date.now() - ackAllArmed < 4000;
  return h("button", { type: "button", class: armed ? "primary" : "", onclick: (ev) => {
    if (Date.now() - ackAllArmed < 4000) {
      ackAllArmed = 0;
      ack(unread().map((m) => m.id), ev.currentTarget);
    } else {
      ackAllArmed = Date.now();
      renderToolbar();
      setTimeout(renderToolbar, 4100);
    }
  } }, armed ? `Ack all ${n}? Click again` : `Ack all ${n}`);
}

function card(m, mine, ids) {
  const other = mine ? m.to : m.from;
  const parent = m.reply_to ? ids.get(m.reply_to) : null;
  const answers = [...ids.values()].filter((x) => x.reply_to === m.id);
  const head = h("div", { class: "msg-head" },
    avatar(other),
    h("span", { class: "msg-who" }, mine ? h("span", { class: "to" }, "to") : null, h("strong", {}, other)),
    badge(m.type),
    m.needs_reply ? h("span", { class: "pill reply" }, "needs reply") : null,
    h("span", { class: "spacer" }),
    h("span", { class: "id" }, `#${m.id}`),
    timeEl(m.created_at));
  let quote = null;
  if (m.reply_to) {
    const who = parent ? `${parent.from === (st.status || {}).user ? "you" : parent.from}: ` : "";
    const text = `↩ reply to #${m.reply_to}${parent ? ` · ${who}${parent.body.replace(/\s+/g, " ").slice(0, 120)}` : ""}`;
    quote = parent ? h("button", { type: "button", class: "quote", title: "Show that message", onclick: () => jump(parent.id) }, text)
      : h("div", { class: "quote" }, text);
  }
  const foot = h("div", { class: "msg-foot" });
  if (mine) {
    foot.append(m.acked
      ? h("span", { class: "state" }, h("span", { class: "tick" }, "✓"), `acked by ${m.to}`, " · ", timeEl(m.acked_at))
      : h("span", { class: "state" }, `not acked by ${m.to} yet`));
  } else {
    foot.append(m.acked ? h("span", { class: "state" }, h("span", { class: "tick" }, "✓"), "acked · ", timeEl(m.acked_at))
      : h("span", { class: "state unread-state" }, "unread"));
  }
  for (const a of answers) {
    const by = a.from === (st.status || {}).user ? "you" : a.from;
    foot.append(h("button", { type: "button", class: "link", onclick: () => jump(a.id) }, `${by} replied (#${a.id})`));
  }
  foot.append(h("span", { class: "spacer" }));
  if (!mine) {
    foot.append(h("button", { type: "button", class: m.needs_reply && !answers.length ? "primary" : "",
      onclick: () => openCompose({ to: m.from, reply: m }) }, "Reply"));
    if (!m.acked) foot.append(h("button", { type: "button", onclick: (ev) => ack([m.id], ev.currentTarget) }, "Ack"));
  }
  return h("li", { class: `msg t-${TYPES[m.type] ? m.type : "info"}${!mine && !m.acked ? " unread" : ""}`, id: `m${m.id}` },
    head, quote,
    h("p", { class: "body" }, m.body),
    m.refs && m.refs.length ? h("ul", { class: "refs" }, m.refs.map((r) => h("li", {},
      h("button", { type: "button", class: "ref", title: `Copy ${r}`, onclick: () => copy(r) }, r)))) : null,
    foot);
}

/* show a message: switch to the list that has it, scroll to it, flash it */
function jump(id) {
  const inInbox = st.inbox.find((m) => m.id === id);
  if (inInbox) {
    st.view = "inbox";
    if (inInbox.acked) st.filter = "all";
  } else {
    st.view = "sent";
  }
  render();
  const el = document.getElementById(`m${id}`);
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  el.classList.add("flash");
  setTimeout(() => el.classList.remove("flash"), 900);
}

/* actions */
async function ack(ids, btn) {
  if (!ids.length) return;
  if (btn) btn.disabled = true;
  try {
    const r = await api("/api/ack", { ids });
    const now = new Date().toISOString().replace(/\.\d+Z$/, "Z");
    for (const m of st.inbox) if (ids.includes(m.id) && !m.acked) Object.assign(m, { acked: true, acked_at: now });
    toast(r.acked === 1 ? "Acked" : `Acked ${r.acked} messages`);
  } catch (e) {
    if (btn) btn.disabled = false;
    toast(e.message, true);
  }
  render();
  refresh();
}

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Copied");
  } catch (e) {
    toast("Could not copy: select the text instead", true);
  }
}

let toastTimer = 0;
function toast(text, bad) {
  const t = $("#toast");
  t.textContent = text;
  t.className = `toast${bad ? " bad" : ""}`;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, bad ? 5000 : 2200);
}

/* compose */
const dlg = $("#compose");
const f = {
  to: $("#f-to"), toText: $("#f-to-text"), body: $("#f-body"), refs: $("#f-refs"), needsReply: $("#f-needs-reply"),
  count: $("#f-count"), error: $("#f-error"), send: $("#f-send"), hint: $("#f-type-hint"),
};
let replyTo = null;

function typeValue() {
  const el = document.querySelector("input[name=type]:checked");
  return el ? el.value : "info";
}
function setType(t) {
  const el = document.querySelector(`input[name=type][value="${t}"]`);
  if (el) el.checked = true;
  f.hint.textContent = TYPES[typeValue()];
}

function openCompose(opts = {}) {
  const s = st.status || {};
  const peers = Array.isArray(s.peers) ? s.peers : null;
  const useSelect = peers !== null && peers.length > 0;
  f.to.hidden = !useSelect;
  f.toText.hidden = useSelect;
  if (useSelect) {
    const keep = opts.to || f.to.value;
    f.to.replaceChildren(...peers.map((p) => h("option", { value: p }, isOnline(p) ? `${p} (online)` : p)));
    if (keep && !peers.includes(keep)) f.to.append(h("option", { value: keep }, keep));
    if (keep) f.to.value = keep;
  } else if (opts.to) {
    f.toText.value = opts.to;
  }
  if (opts.reply) {
    replyTo = opts.reply.id;
    $("#replying-text").textContent = `Replying to #${opts.reply.id} from ${opts.reply.from}: ${opts.reply.body.replace(/\s+/g, " ").slice(0, 80)}`;
    $("#replying").hidden = false;
    $("#compose-title").textContent = `Reply to ${opts.reply.from}`;
    setType("info");
  } else if (opts.to || !replyTo) {
    clearReply();
  }
  f.hint.textContent = TYPES[typeValue()];
  f.error.hidden = true;
  updateCount();
  if (!dlg.open) dlg.showModal();
  (useSelect || f.toText.value ? f.body : f.toText).focus();
}

function clearReply() {
  replyTo = null;
  $("#replying").hidden = true;
  $("#compose-title").textContent = "New message";
}

function updateCount() {
  const n = f.body.value.trim().length;
  f.count.textContent = `${n} / ${MAX_BODY}`;
  f.count.classList.toggle("over", n > MAX_BODY);
}

function showError(text) {
  f.error.textContent = text;
  f.error.hidden = false;
}

async function submit(ev) {
  ev.preventDefault();
  if (f.send.disabled) return;      // already on its way
  const to = (f.to.hidden ? f.toText.value : f.to.value).trim();
  const body = f.body.value.trim();
  const refs = f.refs.value.split(/[\s,]+/).filter(Boolean);
  if (!to) return showError("Who is it for?");
  if (!body) return showError("Write a message first.");
  if (body.length > MAX_BODY) return showError(`At most ${MAX_BODY} characters: put the details into a file and list it under refs.`);
  if (refs.length > MAX_REFS) return showError(`At most ${MAX_REFS} refs.`);
  if (refs.some((r) => r.length > MAX_REF)) return showError(`Each ref at most ${MAX_REF} characters.`);
  const payload = { to, type: typeValue(), body, refs, needs_reply: f.needsReply.checked };
  if (replyTo !== null) payload.reply_to = replyTo;
  f.send.disabled = true;
  f.error.hidden = true;
  try {
    const r = await api("/api/send", payload);
    toast(`Sent #${r.id} to ${to}`);
    f.body.value = "";
    f.refs.value = "";
    f.needsReply.checked = false;
    setType("info");
    clearReply();
    dlg.close();
    refresh();
  } catch (e) {
    showError(e.message);
  } finally {
    f.send.disabled = false;
  }
}

/* wiring */
document.querySelectorAll(".box").forEach((el) => el.addEventListener("click", () => {
  st.view = el.dataset.view;
  render();
}));
$("#refresh").addEventListener("click", () => refresh());
$("#compose-open").addEventListener("click", () => openCompose());
$("#replying-clear").addEventListener("click", clearReply);
dlg.querySelectorAll("[data-close]").forEach((el) => el.addEventListener("click", () => dlg.close()));
// a click on the backdrop closes it; a text selection that merely ends there does not
let downOnBackdrop = false;
dlg.addEventListener("mousedown", (ev) => { downOnBackdrop = ev.target === dlg; });
dlg.addEventListener("click", (ev) => { if (ev.target === dlg && downOnBackdrop) dlg.close(); });
$("#compose-form").addEventListener("submit", submit);
$("#compose-form").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) submit(ev);
});
f.body.addEventListener("input", updateCount);
document.querySelectorAll("input[name=type]").forEach((el) => el.addEventListener("change", () => {
  f.hint.textContent = TYPES[typeValue()];
}));
document.addEventListener("keydown", (ev) => {
  if (ev.ctrlKey || ev.metaKey || ev.altKey || dlg.open || st.locked) return;
  if (ev.target.closest("input, textarea, select")) return;
  if (ev.key === "n") { ev.preventDefault(); openCompose(); }
  if (ev.key === "r") { ev.preventDefault(); refresh(); }
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
setInterval(tickTimes, 30000);

if (!KEY) {
  st.locked = true;
  render();
} else {
  refresh();
}
