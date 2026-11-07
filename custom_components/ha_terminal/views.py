"""HTTP views: terminal frontend, terminal websocket, file manager.

The terminal runs in the browser (xterm.js) and talks to Home Assistant
over a websocket. Browsers cannot set an Authorization header on a
websocket, so the token travels as a ``?token=`` query parameter and is
validated here against HA's own auth — admin users only.

The file manager (list / download / upload under /config) uses HA's
normal authenticated API views: the frontend sends the Bearer token, so
``requires_auth=True`` applies.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
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


def _config_base(hass: HomeAssistant) -> Path:
    """The file manager is confined to the HA config folder."""
    return Path(hass.config.path("")).resolve()


def _resolve_within_base(base: Path, rel: str) -> Path | None:
    """Resolve a user-supplied relative path inside base.

    Returns None for absolute paths, ``..`` escapes, and anything that
    resolves outside base (including through symlinks).
    """
    if not rel or rel == ".":
        return base
    candidate = (base / rel).resolve()
    if not candidate.is_relative_to(base):
        return None
    return candidate


def _rel_posix(base: Path, path: Path) -> str:
    rel = path.relative_to(base).as_posix()
    return "" if rel == "." else rel


class FilesListView(HomeAssistantView):
    """List a directory under /config: ``GET ?path=<relative dir>``."""

    url = "/api/ha_terminal/files/list"
    name = "ha_terminal:files_list"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.StreamResponse:
        base = _config_base(self.hass)
        target = _resolve_within_base(base, request.query.get("path", ""))
        if target is None or not target.is_dir():
            return self.json_message("directory not found", status_code=404)
        dirs: list[dict] = []
        files: list[dict] = []
        try:
            entries = sorted(target.iterdir(), key=lambda p: p.name.lower())
        except OSError as err:
            return self.json_message(f"cannot list directory: {err}", status_code=500)
        for entry in entries:
            try:
                stat = entry.stat()
            except OSError:
                continue
            if entry.is_dir():
                dirs.append({"name": entry.name})
            elif entry.is_file():
                files.append(
                    {
                        "name": entry.name,
                        "size": stat.st_size,
                        "modified": int(stat.st_mtime),
                    }
                )
        return self.json(
            {
                "path": "" if target == base else _rel_posix(base, target),
                "dirs": dirs,
                "files": files,
            }
        )


class FilesDownloadView(HomeAssistantView):
    """Download one file under /config: ``GET ?path=<relative file>``."""

    url = "/api/ha_terminal/files/download"
    name = "ha_terminal:files_download"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.StreamResponse:
        base = _config_base(self.hass)
        target = _resolve_within_base(base, request.query.get("path", ""))
        if target is None or not target.is_file():
            return self.json_message("file not found", status_code=404)
        return web.FileResponse(
            target,
            headers={"Content-Disposition": f'attachment; filename="{target.name}"'},
        )


class FilesUploadView(HomeAssistantView):
    """Upload files into a directory under /config.

    ``POST ?path=<relative dir>`` with a multipart form (field "file",
    one or more). Existing files are overwritten.
    """

    url = "/api/ha_terminal/files/upload"
    name = "ha_terminal:files_upload"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request: web.Request) -> web.StreamResponse:
        base = _config_base(self.hass)
        target_dir = _resolve_within_base(base, request.query.get("path", ""))
        if target_dir is None or not target_dir.is_dir():
            return self.json_message("directory not found", status_code=404)
        try:
            form = await request.post()
        except Exception as err:  # noqa: BLE001 - malformed multipart body
            return self.json_message(f"cannot read upload: {err}", status_code=400)
        uploaded: list[str] = []
        for field in form.values():
            if not hasattr(field, "file") or not getattr(field, "filename", None):
                continue
            filename = Path(field.filename).name
            if not filename or filename in (".", ".."):
                continue
            dest = (target_dir / filename).resolve()
            if not dest.is_relative_to(base):
                continue
            try:
                with dest.open("wb") as out:
                    while True:
                        chunk = field.file.read(65536)
                        if not chunk:
                            break
                        out.write(chunk)
            except OSError as err:
                return self.json_message(f"cannot save {filename}: {err}", status_code=500)
            uploaded.append(filename)
        if not uploaded:
            return self.json_message("no files uploaded", status_code=400)
        return self.json({"uploaded": uploaded, "path": _rel_posix(base, target_dir)})
