# HA Terminal

A terminal and file manager that lives **inside** Home Assistant — a HACS
integration, not an add-on.

The Advanced SSH & Web Terminal add-on runs a whole second container
(Alpine + ttyd + tmux + zsh + OpenSSH) just to give you a shell. HA
Terminal does the same job in-process in Home Assistant Core: a PTY shell
bridged to the browser over a websocket, an SSH server on a port you pick,
and a file manager for `/config`. No extra container, no extra
memory-hungry stack — just the shell.

## Features

- **Full-screen web terminal** — a Terminal item in the sidebar opens a
  full-screen [xterm.js](https://xtermjs.org/) terminal (bundled, works
  offline). Resize-aware, reconnect button, Ctrl-C/Ctrl-D work like a
  real terminal. The sidebar panel is a custom element that hands the
  page your access token, so it works in the HA mobile app too (the app
  keeps tokens out of localStorage, where a plain iframe panel would
  find nothing).
- **SSH server** — connects with any SSH client on the port you choose
  (default **2222**, so it never clashes with the add-on's 22). Log in
  with your Home Assistant username and password, or with a public key
  (see below); **admin users only**.
- **SFTP** — file transfer over the same SSH port (`sftp`, FileZilla,
  WinSCP…), chrooted to `/config`: clients see `/config` as `/` and
  cannot walk above it.
- **Port forwarding** — `ssh -L` / `ssh -R` tunnels through Home
  Assistant to other devices on your network. **Off by default** — turn
  it on in the integration options (Settings → Devices & Services →
  HA Terminal → Configure).
- **Public-key auth** — passwordless login with a key in
  `/config/.ssh/authorized_keys` (standard OpenSSH format, one key per
  line). Only works for HA admin users; the key is matched against the
  HA username you log in as.
- **File manager** — browse `/config` right from the Terminal page:
  navigate folders, click a file to download it, and upload files
  straight into the folder you're in (existing files are overwritten).
  Everything stays inside `/config`; `..` escapes are rejected.
- **SSH keys box** — at the bottom of the Files tab: paste a public key
  and it is validated and added to `/config/.ssh/authorized_keys`
  (duplicates refused, private keys refused). The box also lists the
  keys already installed, with a Remove button for each. Admin users
  only.
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
- **Public key:** generate a key (`ssh-keygen -t ed25519`), then paste
  the `.pub` line into the **SSH keys** box at the bottom of the Files
  tab — or put it in `/config/.ssh/authorized_keys` yourself (via the
  Files tab upload or SFTP). After that, `ssh -p 2222
  <your-ha-username>@<ha-ip>` logs in without a password. Keys with
  `from=` restrictions are not honoured (they are rejected — safe
  failure).
- **SFTP:** `sftp -P 2222 <your-ha-username>@<ha-ip>` — you land in
  `/config`. Or point FileZilla/WinSCP at the same host/port with SFTP
  protocol. Downloads and uploads stay inside `/config`.
- **Port forwarding** (must be enabled in the integration options first):
  `ssh -p 2222 -L 8080:<other-device-ip>:80 <user>@<ha-ip>` reaches
  another device on your LAN through HA; `ssh -p 2222 -R 8080:localhost:8123
  <user>@<ha-ip>` exposes a port on HA to a remote host.
- **Files:** Terminal → Files tab. Browse `/config`, click a file to
  download it, Upload to put files into the current folder.

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

- The web terminal requires a valid Home Assistant access token **and**
  an admin user. Opening the page without logging into HA gets you
  nothing. The file manager and the SSH keys box are admin only too
  (the API enforces it, not just the sidebar panel).
- The SSH server authenticates against Home Assistant accounts (admin
  only), by password or by a key in `/config/.ssh/authorized_keys`.
  No separate password to manage, no anonymous access. The host
  key is generated on first start and stored at
  `/config/.ssh/ha_terminal_host_key`.
- SFTP is chrooted to `/config`. Note: a symlink *inside* `/config` that
  points outside it could still be followed by SFTP — don't create such
  links.
- Port forwarding is off by default because a tunnel lets an SSH client
  reach other devices on your LAN through HA. Only enable it if you use
  tunnels.
- Do not expose the SSH port outside your LAN unless you know what you
  are doing.

## Files

- `custom_components/ha_terminal/` — the integration
  (`__init__.py`, `config_flow.py`, `terminal.py` PTY sessions,
  `views.py` HTTP + websockets, `keys.py` authorized-keys logic,
  `ssh_server.py` asyncssh server, `frontend/` xterm.js UI + panel.js
  custom sidebar element)
- `hacs.json` — HACS repository metadata

## Status

v0.1.5 — the 0.1.4 custom panel showed only a black screen on the mobile
app (the desktop web version was fine). The likeliest cause is the panel
element collapsing to near-zero height on mobile, so the page adds a
viewport-height fallback when its height comes out under 50px, and the
terminal page now reports where it got to (loaded, token received,
websocket open/closed) to a small endpoint that logs it — so a blank
screen can be read off the Home Assistant log instead of guessed at.
