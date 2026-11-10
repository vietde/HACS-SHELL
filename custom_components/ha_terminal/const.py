"""Constants for the HA Terminal integration."""

DOMAIN = "ha_terminal"

CONF_SSH_ENABLED = "ssh_enabled"
CONF_SSH_PORT = "ssh_port"
CONF_SHELL = "shell"
CONF_ALLOW_SFTP = "allow_sftp"
CONF_ALLOW_PORT_FORWARDING = "allow_port_forwarding"

DEFAULT_SSH_ENABLED = True
DEFAULT_SSH_PORT = 2222
DEFAULT_SHELL = "/bin/sh"
DEFAULT_ALLOW_SFTP = True
DEFAULT_ALLOW_PORT_FORWARDING = False

# The terminal runs inside the Home Assistant container, so its working
# directory is the config folder (the same /config the user edits).
DEFAULT_CWD = "/config"

# WebSocket close code sent when the browser token is missing/invalid.
WS_CLOSE_UNAUTHORIZED = 4401

# Integration version (mirrors manifest.json). Used to bust the browser
# cache on the panel module URL after an update.
VERSION = "0.1.8"
