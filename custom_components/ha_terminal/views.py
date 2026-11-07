"""HTTP views: terminal frontend, terminal + log websockets, log download.

The terminal runs in the browser (xterm.js) and talks to Home Assistant
over a websocket. Browsers cannot set an Authorization header on a
websocket, so the token travels as a ``?token=`` query parameter and is
validated here against HA's own auth — admin users only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import (
    DEFAULT_CWD,
    WS_CLOSE_UNAUTHORIZED,
)
from .terminal import TerminalSession, default_shell

_LOGGER = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).parent / "frontend"

_ASSET_TYPES = {
    ".js": "application/javascript",
    ".css": "text/css",
    ".html": "text/html",
    ".map": "application/json",
}

_LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
_LOG_LINE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?\s+"
    r"(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b"
)


def _validate_ws_user(hass: HomeAssistant, request: web.Request):
    """Return the admin user for a valid ``?token=``, else None."""
    token = request.query.get("token")
    if not token:
        return None
    try:
        refresh_token = hass.auth.async_validate_access_token(token)
    except Exception:  # noqa: BLE001 - any auth failure denies access
        return None
    if refresh_token is None:
        return None
    user = refresh_token.user
    if user is None or not user.is_admin:
        return None
    return user


class TerminalFrontendView(HomeAssistantView):
    """Serve the terminal page. The page itself holds no secrets; the
    websocket behind it validates the HA token, so this needs no auth
    (same as HA's own login page)."""

    url = "/api/ha_terminal/"
    name = "ha_terminal:frontend"
    requires_auth = False

    async def get(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(FRONTEND_DIR / "index.html")


class TerminalAssetView(HomeAssistantView):
    """Serve the frontend's static files (xterm.js, app.js, styles)."""

    url = "/api/ha_terminal/assets/{filename}"
    name = "ha_terminal:assets"
    requires_auth = False

    async def get(self, request: web.Request, filename: str) -> web.StreamResponse:
        path = FRONTEND_DIR / filename
        if (
            path.suffix not in _ASSET_TYPES
            or path.parent != FRONTEND_DIR
            or not path.is_file()
        ):
            return web.Response(status=404, text="not found")
        return web.FileResponse(path, headers={"Content-Type": _ASSET_TYPES[path.suffix]})


class TerminalWebSocketView(HomeAssistantView):
    """WebSocket bridge between the browser terminal and a PTY shell."""

    url = "/api/ha_terminal/ws"
    name = "ha_terminal:terminal_ws"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        user = _validate_ws_user(self.hass, request)
        if user is None:
            await ws.close(code=WS_CLOSE_UNAUTHORIZED, message=b"unauthorized")
            return ws

        shell = self._shell()
        loop = asyncio.get_running_loop()
        session: TerminalSession | None = None

        def _on_output(data: bytes) -> None:
            if not ws.closed:
                asyncio.ensure_future(_safe_send(data))

        async def _safe_send(data: bytes) -> None:
            try:
                await ws.send_bytes(data)
            except Exception:  # noqa: BLE001 - peer gone
                pass

        def _on_exit(code) -> None:
            if not ws.closed:
                asyncio.ensure_future(_safe_send(b"\r\n[terminal closed]\r\n"))

        try:
            session = TerminalSession(
                loop, shell, DEFAULT_CWD, _on_output, on_exit=_on_exit
            )
            session.start()
        except Exception as err:  # noqa: BLE001 - report to the browser
            _LOGGER.warning("HA Terminal: could not start shell %s: %s", shell, err)
            await ws.send_str(json.dumps({"type": "error", "message": f"Could not start shell: {err}"}))
            await ws.close()
            return ws

        _LOGGER.debug("HA Terminal: websocket opened for %s", user.name)
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.BINARY:
                    session.write_input(msg.data)
                elif msg.type == web.WSMsgType.TEXT:
                    try:
                        payload = json.loads(msg.data)
                    except (ValueError, TypeError):
                        continue
                    if payload.get("type") == "resize":
                        try:
                            session.resize(int(payload["cols"]), int(payload["rows"]))
                        except (KeyError, TypeError, ValueError):
                            pass
                    elif payload.get("type") == "ping":
                        await ws.send_str(json.dumps({"type": "pong"}))
                elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                    break
        finally:
            if session is not None:
                await session.close()
            _LOGGER.debug("HA Terminal: websocket closed for %s", user.name)
        return ws

    def _shell(self) -> str:
        data = self.hass.data.get("ha_terminal") or {}
        shell = data.get("shell") or default_shell()
        if not (os.path.isfile(shell) and os.access(shell, os.X_OK)):
            return default_shell()
        return shell


def _log_level_of(line: str, current: int) -> int:
    """Level of a log line; continuation lines inherit the previous one."""
    match = _LOG_LINE_RE.match(line)
    if match:
        return _LOG_LEVELS[match.group(1)]
    return current


class LogsWebSocketView(HomeAssistantView):
    """Live tail of home-assistant.log over a websocket."""

    url = "/api/ha_terminal/logs/ws"
    name = "ha_terminal:logs_ws"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    @property
    def _log_path(self) -> Path:
        return Path(self.hass.config.path("home-assistant.log"))

    async def get(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        user = _validate_ws_user(self.hass, request)
        if user is None:
            await ws.close(code=WS_CLOSE_UNAUTHORIZED, message=b"unauthorized")
            return ws

        path = self._log_path
        if not path.is_file():
            await ws.send_str(json.dumps({"type": "error", "message": "home-assistant.log not found"}))
            await ws.close()
            return ws

        min_level = _LOG_LEVELS["DEBUG"]
        offset = 0
        level_state = _LOG_LEVELS["INFO"]

        async def _send(lines: list[str]) -> None:
            nonlocal level_state
            kept: list[str] = []
            for line in lines:
                level_state = _log_level_of(line, level_state)
                if level_state >= min_level:
                    kept.append(line)
            if kept:
                await ws.send_str(json.dumps({"type": "lines", "lines": kept}))

        # History: last 300 lines.
        try:
            size = path.stat().st_size
            start = max(0, size - 262144)
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(start)
                if start:
                    handle.readline()  # drop the partial first line
                history = handle.read().splitlines()[-300:]
            offset = size
            await _send(history)
        except OSError as err:
            await ws.send_str(json.dumps({"type": "error", "message": f"Could not read log: {err}"}))
            await ws.close()
            return ws

        async def _watcher() -> None:
            nonlocal offset
            while not ws.closed:
                await asyncio.sleep(0.5)
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if size < offset:
                    offset = 0  # truncated / rotated
                if size == offset:
                    continue
                try:
                    with path.open("r", encoding="utf-8", errors="replace") as handle:
                        handle.seek(offset)
                        chunk = handle.read()
                        offset = handle.tell()
                except OSError:
                    continue
                if chunk:
                    await _send(chunk.splitlines())

        watcher = asyncio.ensure_future(_watcher())
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        payload = json.loads(msg.data)
                    except (ValueError, TypeError):
                        continue
                    if payload.get("type") == "set_level":
                        level = str(payload.get("level", "DEBUG")).upper()
                        if level in _LOG_LEVELS:
                            min_level = _LOG_LEVELS[level]
                            # Re-send the tail under the new filter.
                            try:
                                size = path.stat().st_size
                                start = max(0, size - 262144)
                                with path.open("r", encoding="utf-8", errors="replace") as handle:
                                    handle.seek(start)
                                    if start:
                                        handle.readline()
                                    tail = handle.read().splitlines()[-300:]
                                level_state = _LOG_LEVELS["INFO"]
                                await _send(tail)
                            except OSError:
                                pass
                elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                    break
        finally:
            watcher.cancel()
        return ws


class LogsDownloadView(HomeAssistantView):
    """Download the full log file. Authenticated like any HA API call."""

    url = "/api/ha_terminal/logs/download"
    name = "ha_terminal:logs_download"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.StreamResponse:
        path = Path(self.hass.config.path("home-assistant.log"))
        if not path.is_file():
            return self.json_message("home-assistant.log not found", status_code=404)
        return web.FileResponse(
            path,
            headers={"Content-Disposition": 'attachment; filename="home-assistant.log"'},
        )
