"""Config flow for the WAVIoT integration."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import WaviotApiClient, WaviotApiError, WaviotAuthError
from .const import (
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_CHANNELS,
    CONF_IMPORT_STATISTICS,
    CONF_MODEM_ID,
    CONF_PRICES,
    DEFAULT_BASE_URL,
    DOMAIN,
)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): str,
        vol.Required(CONF_MODEM_ID): str,
        vol.Optional(CONF_BASE_URL, default=DEFAULT_BASE_URL): str,
    }
)


class WaviotConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for WAVIoT."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            modem_id = user_input[CONF_MODEM_ID].strip().upper()
            user_input[CONF_MODEM_ID] = modem_id

            await self.async_set_unique_id(modem_id)
            self._abort_if_unique_id_configured()

            session = async_get_clientsession(self.hass)
            client = WaviotApiClient(
                session,
                api_key=user_input[CONF_API_KEY].strip(),
                base_url=user_input.get(CONF_BASE_URL, DEFAULT_BASE_URL),
            )
            try:
                await client.modem_info(modem_id)
            except WaviotAuthError:
                errors["base"] = "invalid_auth"
            except WaviotApiError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=f"WAVIoT {modem_id}", data=user_input
                )

        return self.async_show_form(
            step_id="user", data_schema=USER_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "WaviotOptionsFlow":
        return WaviotOptionsFlow()


class WaviotOptionsFlow(OptionsFlow):
    """Options: manual channel list and statistics import toggle."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_CHANNELS,
                    description={
                        "suggested_value": options.get(CONF_CHANNELS, "")
                    },
                ): str,
                vol.Optional(
                    CONF_PRICES,
                    description={
                        "suggested_value": options.get(CONF_PRICES, "")
                    },
                ): str,
                vol.Optional(
                    CONF_IMPORT_STATISTICS,
                    default=options.get(CONF_IMPORT_STATISTICS, True),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
