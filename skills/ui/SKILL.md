---
name: ui
description: Open passnote in the browser - a page to read, send and ack messages, see what you sent and whether it was acked, and whom you can write to
disable-model-invocation: true
---

Start the passnote web interface on this computer. It keeps running until it is stopped, so run it **in the
background** (the Bash tool's `run_in_background`). If `python3` is not found, use `python`, or `py` on Windows:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ui.py"
```

Wait for its first line of output, `passnote ui: http://127.0.0.1:<port>/#key=...`, then tell the user in a few lines:

- The browser should have opened that address; if it did not, they open it themselves. Only this computer can open it,
  and only with the `#key=...` part, which changes at every start.
- It shows their inbox (unread or all), what they sent and whether it was acked, and whom they can write to. They can
  reply, ack and write new messages there, as the same name this computer's agent uses.
- It needs no `/passnote:enable`: it is the user's own view, and it asks the server only while the page is open.
- To stop it, stop that background command.
- If the page says passnote is not set up, run `/passnote:init` first. If it says the server does not know the token,
  the page shows the line to send to whoever runs the server.
