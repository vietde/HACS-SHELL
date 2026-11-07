"""SSH server for HA Terminal (asyncssh, imported lazily).

Listens on the configured port and gives an SSH shell in the Home
Assistant container, plus:

- password auth against HA admin accounts;
- public-key auth against /config/.ssh/authorized_keys (admin users only);
- SFTP, chrooted to /config (when enabled);
- TCP port forwarding, ssh -L / -R (when enabled — off by default).

Two shell paths exist because of how asyncssh routes sessions: with SFTP
enabled, asyncssh sends every session channel through its stream session,
so the shell runs as a stream handler (`_shell_stream_handler`); with
SFTP disabled, the classic ``session_requested()`` path is used
(`_HATerminalSSHSession`). Both bridge the same PTY core in terminal.py.

If asyncssh cannot be imported (or the server cannot bind), the caller
logs a warning and carries on without SSH — the web terminal still works.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
from pathlib import Path

_LOGGER = logging.getLogger(__name__)


def _load_authorized_keys(path: Path):
    """Load /config/.ssh/authorized_keys, or None if missing/unusable."""
    import asyncssh

    try:
        return asyncssh.read_authorized_keys(str(path))
    except (FileNotFoundError, ValueError, OSError) as err:
        _LOGGER.debug("HA Terminal: no usable authorized_keys at %s: %s", path, err)
        return None


async def start_ssh_server(
    hass,
    port: int,
    shell: str,
    password_validator,
    admin_check=None,
    allow_sftp: bool = True,
    allow_port_forwarding: bool = False,
):
    """Start the SSH server. Returns the server, or None on failure."""
    try:
        import asyncssh
    except ImportError as err:
        _LOGGER.warning("HA Terminal: asyncssh is not available, SSH server disabled: %s", err)
        return None

    config_dir = Path(hass.config.path("")).resolve()
    key_dir = Path(hass.config.path(".ssh"))
    key_path = key_dir / "ha_terminal_host_key"
    authorized_keys_path = key_dir / "authorized_keys"
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

    async def _shell_stream_handler(stdin, stdout, stderr):
        """Shell for the stream-session path (used when SFTP is on).

        stdin/stdout are raw byte streams (encoding=None). Terminal
        resizes arrive as TerminalSizeChanged exceptions from stdin.read.
        """
        from asyncssh.misc import BreakReceived, SoftEOFReceived, TerminalSizeChanged

        from .terminal import TerminalSession, default_shell

        _LOGGER.debug("HA Terminal: stream shell handler started")
        use_shell = shell
        if not (os.path.isfile(use_shell) and os.access(use_shell, os.X_OK)):
            use_shell = default_shell()
        loop = asyncio.get_running_loop()
        exited = asyncio.Event()

        def _on_output(data: bytes) -> None:
            try:
                stdout.write(data)
                _LOGGER.debug("HA Terminal: stream stdout wrote %d bytes", len(data))
            except Exception as err:  # noqa: BLE001 - log, don't crash the pump
                _LOGGER.debug("HA Terminal: stream stdout write failed: %r", err)

        def _on_exit(code) -> None:
            exited.set()

        session = TerminalSession(
            loop, use_shell, "/config", _on_output, on_exit=_on_exit
        )
        try:
            session.start()
        except Exception as err:  # noqa: BLE001 - report to the client
            try:
                stdout.write(f"Could not start shell: {err}\r\n".encode())
            except Exception:  # noqa: BLE001 - channel gone
                pass
            return
        try:
            size = stdin.channel.get_terminal_size()
            session.resize(size[0], size[1])
        except Exception:  # noqa: BLE001 - no PTY, keep 80x24
            pass
        try:
            command = stdin.channel.get_command()
        except Exception:  # noqa: BLE001 - no command
            command = None
        if command:
            # One-shot command (ssh host "cmd"): run it, then exit.
            session.write_input(command.encode() + b"\nexit\n")

        async def _pump() -> None:
            try:
                while not exited.is_set():
                    try:
                        data = await stdin.read(4096)
                    except TerminalSizeChanged as exc:
                        session.resize(exc.width, exc.height)
                        continue
                    except BreakReceived:
                        session.write_input(b"\x03")  # Ctrl-C
                        continue
                    except SoftEOFReceived:
                        break
                    except Exception as err:  # noqa: BLE001 - log and stop pumping
                        _LOGGER.debug("HA Terminal: stream stdin read failed: %s", err)
                        break
                    if not data:
                        break  # client closed stdin
                    _LOGGER.debug("HA Terminal: stream stdin got %d bytes", len(data))
                    session.write_input(data)
            except asyncio.CancelledError:
                pass

        pump_task = asyncio.ensure_future(_pump())
        exit_task = asyncio.ensure_future(exited.wait())
        await asyncio.wait(
            {pump_task, exit_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in (pump_task, exit_task):
            if not task.done():
                task.cancel()
        await session.close()
        # Signal the client: exit status + EOF + close. Without this the
        # channel stays open after the shell exits and the client waits
        # forever (a stream handler returning does not close the channel).
        try:
            stdin.channel.exit(0)
        except Exception:  # noqa: BLE001 - channel already closed
            pass

    class _HATerminalSSHServer(asyncssh.SSHServer):
        """Password + public-key auth against HA admin accounts."""

        def password_auth_supported(self) -> bool:
            return True

        async def validate_password(self, username: str, password: str) -> bool:
            try:
                return bool(await password_validator(username, password))
            except Exception:  # noqa: BLE001 - any failure denies access
                return False

        def public_key_auth_supported(self) -> bool:
            # Only useful once /config/.ssh/authorized_keys exists.
            return True

        async def validate_public_key(self, username: str, key) -> bool:
            try:
                if admin_check is not None and not await admin_check(username):
                    return False
                authorized = _load_authorized_keys(authorized_keys_path)
                if authorized is None:
                    return False
                # Empty host/addr: keys without from= restrictions match;
                # keys with from= restrictions do not (safe failure mode).
                return authorized.validate(key, "", "") is not None
            except Exception:  # noqa: BLE001 - any failure denies access
                return False

        def connection_requested(self, dest_host: str, dest_port: int,
                                 orig_host: str, orig_port: int):
            """Local forwarding (ssh -L): client asks HA to connect out."""
            if allow_port_forwarding:
                _LOGGER.info(
                    "HA Terminal: forwarding allowed to %s:%s", dest_host, dest_port
                )
                return True
            _LOGGER.warning(
                "HA Terminal: forwarding to %s:%s refused (disabled)",
                dest_host, dest_port,
            )
            return False

        def server_requested(self, listen_host: str, listen_port: int):
            """Remote forwarding (ssh -R): client asks HA to listen."""
            if allow_port_forwarding:
                _LOGGER.info(
                    "HA Terminal: remote forwarding allowed on %s:%s",
                    listen_host, listen_port,
                )
                return True
            _LOGGER.warning(
                "HA Terminal: remote forwarding on %s:%s refused (disabled)",
                listen_host, listen_port,
            )
            return False

        def session_requested(self):
            # Used only when SFTP is off (with SFTP on, asyncssh routes
            # sessions through the stream session + session_factory).
            return _HATerminalSSHSession()

    class _HATerminalSSHSession(asyncssh.SSHServerSession):
        """One SSH session: a PTY shell bridged to the SSH channel.

        Used when SFTP is disabled. (When SFTP is enabled, the shell runs
        through _shell_stream_handler instead.)
        """

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

    create_kwargs: dict = {
        # line_editor=False: the PTY does its own line editing and echo.
        # asyncssh's built-in editor (default on) would echo input a second
        # time and buffer whole lines, breaking interactive programs.
        "line_editor": False,
        # Raw bytes on sessions: the terminal is a byte stream, and the
        # stream handler converts nothing.
        "encoding": None,
    }
    if allow_sftp:
        # SFTP subsystem, virtually chrooted to the HA config folder:
        # clients see /config as / and cannot walk above it. Setting a
        # factory also routes shell sessions through the stream session,
        # so the shell needs the stream handler too.
        create_kwargs["sftp_factory"] = functools.partial(
            asyncssh.SFTPServer, chroot=os.fsencode(config_dir)
        )
        create_kwargs["session_factory"] = _shell_stream_handler
    try:
        server = await asyncssh.create_server(
            _HATerminalSSHServer, "", port, server_host_keys=[host_key],
            **create_kwargs,
        )
    except Exception as err:  # noqa: BLE001 - e.g. port already in use
        _LOGGER.warning("HA Terminal: could not start SSH server on port %s: %s", port, err)
        return None
    _LOGGER.info(
        "HA Terminal: SSH server listening on port %s (SFTP %s, port forwarding %s)",
        port,
        "on" if allow_sftp else "off",
        "on" if allow_port_forwarding else "off",
    )
    return server
