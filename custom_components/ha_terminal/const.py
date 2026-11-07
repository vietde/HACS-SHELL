"""Constants for the HA Terminal integration."""

DOMAIN = "ha_terminal"

CONF_SSH_ENABLED = "ssh_enabled"
CONF_SSH_PORT = "ssh_port"
CONF_SHELL = "shell"

DEFAULT_SSH_ENABLED = True
DEFAULT_SSH_PORT = 2222
DEFAULT_SHELL = "/bin/sh"

# The terminal runs inside the Home Assistant container, so its working
# directory is the config folder (the same /config the user edits).
DEFAULT_CWD = "/config"

# WebSocket close code sent when the browser token is missing/invalid.
WS_CLOSE_UNAUTHORIZED = 4401
