"""The WAVIoT integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import WaviotApiClient
from .const import (
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_MODEM_ID,
    DEFAULT_BASE_URL,
    DOMAIN,
)
from .coordinator import PRICES_STORAGE_VERSION, WaviotCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]

type WaviotConfigEntry = ConfigEntry[WaviotCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: WaviotConfigEntry) -> bool:
    """Set up WAVIoT from a config entry."""
    session = async_get_clientsession(hass)
    client = WaviotApiClient(
        session,
        api_key=entry.data[CONF_API_KEY],
        base_url=entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL),
    )
    coordinator = WaviotCoordinator(hass, entry, client, entry.data[CONF_MODEM_ID])
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: WaviotConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: WaviotConfigEntry) -> None:
    """Drop the stored tariff signature along with the entry."""
    await Store(
        hass, PRICES_STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.prices"
    ).async_remove()
