# HA Terminal

A terminal and log viewer that lives **inside** Home Assistant — a HACS
integration, not an add-on.

The Advanced SSH & Web Terminal add-on runs a whole second container
(Alpine + ttyd + tmux + zsh + OpenSSH) just to give you a shell. HA
Terminal does the same job in-process in Home Assistant Core: a PTY shell
bridged to the browser over a websocket, an SSH server on a port you pick,
and a live tail of `home-assistant.log`. No extra container, no extra
memory-hungry stack — just the shell.

## Features

- **Full-screen web terminal** — a Terminal item in the sidebar opens a
  full-screen [xterm.js](https://xtermjs.org/) terminal (bundled, works
  offline). Resize-aware, reconnect button, Ctrl-C/Ctrl-D work like a
  real terminal.
- **SSH server** — connects with any SSH client on the port you choose
  (default **2222**, so it never clashes with the add-on's 22). Log in
  with your Home Assistant username and password; **admin users only**.
- **Log viewer** — live tail of `home-assistant.log` with level filter
  (Debug → Critical), text search, follow mode, and a one-click download
  of the full log file.
- **Native and light** — runs in HA Core's own process. A shell session
  is one PTY plus a websocket; idle cost is essentially zero.

## Install

1. In HACS, add this repo as a custom repository (category: Integration):
   `https://github.com/vietde/hacs-shell`
2. Install **HA Terminal**, restart Home Assistant.
3. Settings → Devices & Services → Add Integration → **HA Terminal**.
   Pick the SSH port (default 2222), the shell (`/bin/sh` or `/bin/bash`),
   and whether the SSH server is on.
4. A **Terminal** item appears in the sidebar (admins only).

## Usage

- **Web terminal:** sidebar → Terminal. If it says "No HA session", open
  it from inside Home Assistant while logged in.
- **SSH:** `ssh -p 2222 <your-ha-username>@<ha-ip>`, password is your Home
  Assistant password. Admin accounts only.
- **Logs:** Terminal → Logs tab. Change the level to cut the noise, or
  Download log for the whole file.

## What the shell can (and cannot) do

The shell runs **inside the Home Assistant container**, not on the HAOS
host. That is the whole point (lightweight), and the one real limit:

- ✅ `/config` (your whole configuration), Home Assistant's Python
  environment, `pip`, network tools in the container, `ha` core logs.
- ❌ No host OS shell, no Docker control, no `ha` CLI host commands —
  those need the host, which only an add-on with protection mode off (or
  the HAOS console) can give you. If you need the host, keep the add-on
  for that job; HA Terminal covers everything inside Home Assistant.

## Security

- The web terminal and the log viewer require a valid Home Assistant
  access token **and** an admin user. Opening the page without logging
  into HA gets you nothing.
- The SSH server authenticates against Home Assistant accounts (admin
  only). No separate password to manage, no anonymous access. The host
  key is generated on first start and stored at
  `/config/.ssh/ha_terminal_host_key`.
- Do not expose the SSH port outside your LAN unless you know what you
  are doing.

## Files

- `custom_components/ha_terminal/` — the integration
  (`__init__.py`, `config_flow.py`, `terminal.py` PTY sessions,
  `views.py` HTTP + websockets, `ssh_server.py` asyncssh server,
  `frontend/` xterm.js UI)
- `hacs.json` — HACS repository metadata

## Status

v0.1.0 — first release. The web terminal, SSH server and log viewer are
all in. Not yet in: public-key SSH auth, SFTP, multiple saved terminal
tabs, tmux-style persistent sessions (reconnect starts a fresh shell).
