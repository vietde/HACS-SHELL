"""SSH server for HA Terminal (asyncssh, imported lazily).

Listens on the configured port and gives an SSH shell in the Home
Assistant container. Authentication is by Home Assistant username +
password (admin users only); the password check is injected by
``__init__`` so this module stays testable without HA.

If asyncssh cannot be imported (or the server cannot bind), the caller
logs a warning and carries on without SSH — the web terminal still works.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

_LOGGER = logging.getLogger(__name__)


async def start_ssh_server(hass, port: int, shell: str, password_validator):
    """Start the SSH server. Returns the server, or None on failure."""
    try:
        import asyncssh
    except ImportError as err:
        _LOGGER.warning("HA Terminal: asyncssh is not available, SSH server disabled: %s", err)
        return None

    key_dir = Path(hass.config.path(".ssh"))
    key_path = key_dir / "ha_terminal_host_key"
    try:
        key_dir.mkdir(parents=True, exist_ok=True)
        if key_path.is_file():
            host_key = asyncssh.read_private_key(str(key_path))
        else:
            host_key = asyncssh.generate_private_key("ssh-ed25519")
            host_key.write_private_key(str(key_path))
            os.chmod(key_path, 0o600)
            _LOGGER.info("HA Terminal: generated SSH host key at %s", key_path)
    except Exception as err:  # noqa: BLE001 - without a host key there is no server
        _LOGGER.warning("HA Terminal: could not load host key, SSH server disabled: %s", err)
        return None

    class _HATerminalSSHServer(asyncssh.SSHServer):
        """Password auth against HA admin accounts."""

        def password_auth_supported(self) -> bool:
            return True

        async def validate_password(self, username: str, password: str) -> bool:
            try:
                return bool(await password_validator(username, password))
            except Exception:  # noqa: BLE001 - any failure denies access
                return False

        def session_requested(self):
            return _HATerminalSSHSession()

    class _HATerminalSSHSession(asyncssh.SSHServerSession):
        """One SSH session: a PTY shell bridged to the SSH channel."""

        def __init__(self) -> None:
            self._terminal = None
            self._chan = None
            self._cols = 80
            self._rows = 24

        def connection_made(self, chan) -> None:
            self._chan = chan

        def shell_requested(self) -> bool:
            return True

        def pty_requested(self, term, width, height, pixwidth, pixheight, modes) -> bool:
            self._cols = width or 80
            self._rows = height or 24
            return True

        def session_started(self) -> None:
            from .terminal import TerminalSession, default_shell

            _LOGGER.debug("HA Terminal: SSH session started")
            use_shell = shell
            if not (os.path.isfile(use_shell) and os.access(use_shell, os.X_OK)):
                use_shell = default_shell()
            loop = asyncio.get_running_loop()
            self._terminal = TerminalSession(
                loop,
                use_shell,
                "/config",
                on_output=self._on_output,
                on_exit=self._on_terminal_exit,
            )
            self._terminal.start()
            self._terminal.resize(self._cols, self._rows)

        def _channel_encoding(self):
            """The channel decodes/encodes text when the client negotiated
            an encoding (asyncssh delivers str to data_received and expects
            str from write in that case); None means raw bytes. Note
            get_encoding() returns a (encoding, errors) tuple."""
            if self._chan is None:
                return None
            try:
                encoding, _errors = self._chan.get_encoding()
                return encoding
            except Exception:  # noqa: BLE001 - no encoding API
                return None

        def _on_output(self, data: bytes) -> None:
            if self._chan is not None:
                try:
                    encoding = self._channel_encoding()
                    if encoding:
                        self._chan.write(data.decode(encoding, errors="replace"))
                    else:
                        self._chan.write(data)
                except Exception:  # noqa: BLE001 - channel gone
                    pass

        def _on_terminal_exit(self, code) -> None:
            _LOGGER.debug("HA Terminal: SSH shell exited (code=%s)", code)
            if self._chan is not None:
                try:
                    self._chan.exit(0)
                except Exception:  # noqa: BLE001 - channel gone
                    pass

        def data_received(self, data, datatype) -> None:
            # asyncssh delivers str when the channel has an encoding,
            # bytes otherwise; the PTY always wants bytes.
            if self._terminal is not None:
                if isinstance(data, str):
                    data = data.encode()
                self._terminal.write_input(data)

        def terminal_size_changed(self, width, height, pixwidth, pixheight) -> None:
            if self._terminal is not None:
                self._terminal.resize(width, height)

        def break_received(self, msec: int) -> None:
            # Ctrl-C
            if self._terminal is not None:
                self._terminal.write_input(b"\x03")

        def eof_received(self) -> bool:
            if self._terminal is not None:
                asyncio.ensure_future(self._terminal.close())
            return True

        def connection_lost(self, exc) -> None:
            if self._terminal is not None:
                asyncio.ensure_future(self._terminal.close())
                self._terminal = None

    try:
        # line_editor=False: the PTY does its own line editing and echo.
        # asyncssh's built-in editor (default on) would echo input a second
        # time and buffer whole lines, breaking interactive programs.
        server = await asyncssh.create_server(
            _HATerminalSSHServer, "", port, server_host_keys=[host_key],
            line_editor=False,
        )
    except Exception as err:  # noqa: BLE001 - e.g. port already in use
        _LOGGER.warning("HA Terminal: could not start SSH server on port %s: %s", port, err)
        return None
    _LOGGER.info("HA Terminal: SSH server listening on port %s", port)
    return server
