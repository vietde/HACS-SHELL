"""HA Terminal — a native terminal + log viewer that lives inside Home
Assistant (no add-on container).

Runs entirely in HA Core: a full-screen web terminal (xterm.js over a
websocket to a PTY shell in the container), an SSH server on the
configured port, and a live tail of home-assistant.log. Lighter than the
Advanced SSH & Web Terminal add-on because there is no second container,
no ttyd/tmux stack — just the shell, in-process.
"""

from __future__ import annotations

import logging

from homeassistant.components import frontend
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ALLOW_PORT_FORWARDING,
    CONF_ALLOW_SFTP,
    CONF_SHELL,
    CONF_SSH_ENABLED,
    CONF_SSH_PORT,
    DEFAULT_ALLOW_PORT_FORWARDING,
    DEFAULT_ALLOW_SFTP,
    DEFAULT_SHELL,
    DEFAULT_SSH_ENABLED,
    DEFAULT_SSH_PORT,
    DOMAIN,
)
from .ssh_server import start_ssh_server
from .views import (
    FilesDownloadView,
    FilesListView,
    FilesUploadView,
    TerminalAssetView,
    TerminalFrontendView,
    TerminalWebSocketView,
)

_LOGGER = logging.getLogger(__name__)

PANEL_URL_PATH = "ha_terminal"


async def _is_admin_user(hass: HomeAssistant, username: str) -> bool:
    """True if the named HA user exists and is an admin."""
    for user in await hass.auth.async_get_users():
        if user.name and user.name.lower() == username.lower():
            return user.is_admin
    return False


async def _validate_ha_password(hass: HomeAssistant, username: str, password: str) -> bool:
    """Check an HA username/password; only admin users may use SSH."""
    for provider in hass.auth.auth_providers:
        validate = getattr(provider, "async_validate_login", None)
        if validate is None:
            continue
        try:
            await validate(username, password)
        except Exception:  # noqa: BLE001 - wrong password or provider issue
            continue
        # Login accepted: find the matching user and require admin.
        return await _is_admin_user(hass, username)
    return False


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up HA Terminal from a config entry."""
    options = {**entry.data, **entry.options}
    shell = options.get(CONF_SHELL) or DEFAULT_SHELL
    ssh_enabled = options.get(CONF_SSH_ENABLED, DEFAULT_SSH_ENABLED)
    ssh_port = options.get(CONF_SSH_PORT, DEFAULT_SSH_PORT)
    allow_sftp = options.get(CONF_ALLOW_SFTP, DEFAULT_ALLOW_SFTP)
    allow_port_forwarding = options.get(
        CONF_ALLOW_PORT_FORWARDING, DEFAULT_ALLOW_PORT_FORWARDING
    )

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["shell"] = shell

    hass.http.register_view(TerminalFrontendView())
    hass.http.register_view(TerminalAssetView())
    hass.http.register_view(TerminalWebSocketView(hass))
    hass.http.register_view(FilesListView(hass))
    hass.http.register_view(FilesDownloadView(hass))
    hass.http.register_view(FilesUploadView(hass))

    frontend.async_register_built_in_panel(
        hass,
        "iframe",
        "Terminal",
        "mdi:console-terminal",
        frontend_url_path=PANEL_URL_PATH,
        config={"url": "/api/ha_terminal/"},
        require_admin=True,
    )

    ssh_server = None
    if ssh_enabled:
        ssh_server = await start_ssh_server(
            hass,
            int(ssh_port),
            shell,
            lambda u, p: _validate_ha_password(hass, u, p),
            admin_check=lambda u: _is_admin_user(hass, u),
            allow_sftp=allow_sftp,
            allow_port_forwarding=allow_port_forwarding,
        )
    hass.data[DOMAIN]["ssh_server"] = ssh_server
    hass.data[DOMAIN]["entry_id"] = entry.entry_id

    entry.async_on_unload(entry.add_update_listener(_async_update_options))
    _LOGGER.info(
        "HA Terminal ready: web terminal at /api/ha_terminal/, SSH %s",
        f"on port {ssh_port}" if ssh_server else "disabled",
    )
    return True


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload HA Terminal."""
    data = hass.data.get(DOMAIN) or {}
    if (server := data.get("ssh_server")) is not None:
        server.close()
        await server.wait_closed()
    frontend.async_remove_panel(hass, PANEL_URL_PATH)
    hass.data.pop(DOMAIN, None)
    return True
