"""Config flow for the WAVIoT integration."""
from __future__ import annotations

from collections.abc import Mapping
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
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

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
from .prices import parse_prices

API_KEY_SELECTOR = TextSelector(
    TextSelectorConfig(
        type=TextSelectorType.PASSWORD, autocomplete="current-password"
    )
)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): API_KEY_SELECTOR,
        vol.Required(CONF_MODEM_ID): str,
        vol.Optional(CONF_BASE_URL, default=DEFAULT_BASE_URL): str,
    }
)

REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_API_KEY): API_KEY_SELECTOR})


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

            user_input[CONF_API_KEY] = user_input[CONF_API_KEY].strip()
            error = await self._async_validate(
                user_input[CONF_API_KEY],
                modem_id,
                user_input.get(CONF_BASE_URL, DEFAULT_BASE_URL),
            )
            if error:
                errors["base"] = error
            else:
                return self.async_create_entry(
                    title=f"WAVIoT {modem_id}", data=user_input
                )

        return self.async_show_form(
            step_id="user", data_schema=USER_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication after the API key was rotated."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new API key and validate it against the same modem."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            error = await self._async_validate(
                api_key,
                entry.data[CONF_MODEM_ID],
                entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL),
            )
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_KEY: api_key}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"modem_id": entry.data[CONF_MODEM_ID]},
        )

    async def _async_validate(
        self, api_key: str, modem_id: str, base_url: str
    ) -> str | None:
        """Return an error key, or None when the key works for this modem."""
        client = WaviotApiClient(
            async_get_clientsession(self.hass),
            api_key=api_key,
            base_url=base_url,
        )
        try:
            await client.modem_info(modem_id)
        except WaviotAuthError:
            return "invalid_auth"
        except WaviotApiError:
            return "cannot_connect"
        return None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "WaviotOptionsFlow":
        return WaviotOptionsFlow()


class WaviotOptionsFlow(OptionsFlow):
    """Options: manual channel list and statistics import toggle."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}

        if user_input is not None:
            _, bad = parse_prices(user_input.get(CONF_PRICES, ""))
            if bad:
                errors[CONF_PRICES] = "invalid_prices"
                placeholders["entries"] = ", ".join(bad)
            else:
                return self.async_create_entry(data=user_input)

        options = {**self.config_entry.options, **(user_input or {})}
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
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )
