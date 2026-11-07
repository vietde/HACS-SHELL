"""PTY-backed shell sessions.

Pure standard library — no Home Assistant imports — so this module is
unit-testable outside HA. A session owns one shell process attached to a
pseudo-terminal: input bytes go to the PTY master, output bytes arrive via
the ``on_output`` callback, and the process is terminated when the session
closes (or exits on its own, which fires ``on_exit``).
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import pty
import signal
import struct
import subprocess
import termios

_LOGGER = logging.getLogger(__name__)


def default_shell() -> str:
    """Pick a shell that exists in the container."""
    for candidate in (os.environ.get("SHELL"), "/bin/bash", "/bin/sh"):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return "/bin/sh"


class TerminalSession:
    """One interactive shell on a PTY."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        shell: str,
        cwd: str,
        on_output,
        on_exit=None,
    ) -> None:
        self._loop = loop
        self._shell = shell
        self._cwd = cwd
        self._on_output = on_output
        self._on_exit = on_exit
        self._master_fd: int | None = None
        self._proc: subprocess.Popen | None = None
        self._closed = False
        self._exit_reported = False

    @property
    def closed(self) -> bool:
        return self._closed

    def start(self) -> None:
        """Spawn the shell. Must be called from the event loop thread."""
        master_fd, slave_fd = pty.openpty()

        # /config always exists in the HA container; fall back gracefully
        # anywhere else (e.g. a dev machine) instead of failing to spawn.
        cwd = self._cwd
        if not os.path.isdir(cwd):
            cwd = os.path.expanduser("~")
        if not os.path.isdir(cwd):
            cwd = "/"

        def _preexec() -> None:
            os.setsid()
            # Make the slave the controlling terminal of the new session,
            # so job control and signals behave like a real terminal.
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        env["SHELL"] = self._shell
        self._proc = subprocess.Popen(
            [self._shell],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            preexec_fn=_preexec,
            cwd=cwd,
            env=env,
        )
        os.close(slave_fd)
        os.set_blocking(master_fd, False)
        self._master_fd = master_fd
        self._loop.add_reader(master_fd, self._on_readable)
        _LOGGER.debug("Terminal session started: pid=%s shell=%s", self._proc.pid, self._shell)

    def _on_readable(self) -> None:
        if self._master_fd is None:
            return
        try:
            data = os.read(self._master_fd, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if not data:
            # EOF on the master: the shell exited.
            self._loop.call_soon(self._report_exit)
            return
        try:
            self._on_output(data)
        except Exception:  # noqa: BLE001 - a dead transport must not kill the reader
            _LOGGER.debug("Terminal output callback failed", exc_info=True)

    def _report_exit(self) -> None:
        if self._exit_reported:
            return
        self._exit_reported = True
        code = self._proc.poll() if self._proc else None
        _LOGGER.debug("Terminal session exited: code=%s", code)
        if self._on_exit is not None:
            try:
                self._on_exit(code)
            except Exception:  # noqa: BLE001 - never raise out of the reader
                _LOGGER.debug("Terminal exit callback failed", exc_info=True)

    def write_input(self, data: bytes) -> None:
        """Send keystrokes to the shell."""
        if self._closed or self._master_fd is None or not data:
            return
        try:
            os.write(self._master_fd, data)
        except (BlockingIOError, InterruptedError):
            # PTY buffer full; the bytes are dropped rather than blocking HA.
            pass
        except OSError:
            pass

    def resize(self, cols: int, rows: int) -> None:
        """Tell the shell the terminal size changed."""
        if self._closed or self._master_fd is None:
            return
        if cols < 1 or rows < 1:
            return
        try:
            fcntl.ioctl(
                self._master_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0),
            )
        except OSError:
            pass

    async def close(self) -> None:
        """Terminate the shell and release the PTY."""
        if self._closed:
            return
        self._closed = True
        if self._master_fd is not None:
            try:
                self._loop.remove_reader(self._master_fd)
            except Exception:  # noqa: BLE001 - already removed
                pass
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGHUP)
            except (OSError, ValueError):
                pass
            try:
                await asyncio.wait_for(
                    self._loop.run_in_executor(None, proc.wait), timeout=2.0
                )
            except (asyncio.TimeoutError, OSError, ValueError):
                try:
                    proc.kill()
                except (OSError, ValueError):
                    pass
        _LOGGER.debug("Terminal session closed")
