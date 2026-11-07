"""Config flow for HA Terminal."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
)

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


def _schema(defaults: dict) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_SSH_ENABLED, default=defaults.get(CONF_SSH_ENABLED, DEFAULT_SSH_ENABLED)
            ): BooleanSelector(),
            vol.Required(
                CONF_SSH_PORT, default=defaults.get(CONF_SSH_PORT, DEFAULT_SSH_PORT)
            ): NumberSelector(
                NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
            ),
            vol.Required(
                CONF_SHELL, default=defaults.get(CONF_SHELL, DEFAULT_SHELL)
            ): TextSelector(),
            vol.Required(
                CONF_ALLOW_SFTP, default=defaults.get(CONF_ALLOW_SFTP, DEFAULT_ALLOW_SFTP)
            ): BooleanSelector(),
            vol.Required(
                CONF_ALLOW_PORT_FORWARDING,
                default=defaults.get(CONF_ALLOW_PORT_FORWARDING, DEFAULT_ALLOW_PORT_FORWARDING),
            ): BooleanSelector(),
        }
    )


class HATerminalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up HA Terminal."""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(title="HA Terminal", data=user_input)
        return self.async_show_form(step_id="user", data_schema=_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return HATerminalOptionsFlow(config_entry)


class HATerminalOptionsFlow(OptionsFlow):
    """Change HA Terminal options."""

    def __init__(self, config_entry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        defaults = {**self._config_entry.data, **self._config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema(defaults))
