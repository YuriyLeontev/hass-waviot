"""Sensor platform for WAVIoT."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CHANNEL_NAMES, DOMAIN
from .coordinator import WaviotCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: WaviotCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = [
        WaviotEnergySensor(coordinator, channel) for channel in coordinator.channels
    ]
    entities += [
        WaviotBatterySensor(coordinator),
        WaviotTemperatureSensor(coordinator),
        WaviotLastSeenSensor(coordinator),
    ]
    async_add_entities(entities)


class WaviotBaseSensor(CoordinatorEntity[WaviotCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: WaviotCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.modem_id)},
            name=f"WAVIoT {coordinator.modem_id}",
            manufacturer="WAVIoT",
            model="NB-IoT electricity meter",
        )


class WaviotEnergySensor(WaviotBaseSensor):
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: WaviotCoordinator, channel: str) -> None:
        super().__init__(coordinator)
        self._channel = channel
        self._attr_unique_id = f"{coordinator.modem_id}_{channel}"
        self._attr_name = CHANNEL_NAMES.get(channel, channel)

    @property
    def native_value(self) -> float | None:
        ch = self.coordinator.data["channels"].get(self._channel) or {}
        return ch.get("value")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        ch = self.coordinator.data["channels"].get(self._channel) or {}
        ts: datetime | None = ch.get("ts")
        return {
            "channel": self._channel,
            "reading_time": ts.isoformat() if ts else None,
            "statistic_id": self.coordinator.statistic_id(self._channel),
        }


class WaviotBatterySensor(WaviotBaseSensor):
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "V"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Battery voltage"
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: WaviotCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.modem_id}_battery"

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.get("battery")


class WaviotTemperatureSensor(WaviotBaseSensor):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Temperature"

    def __init__(self, coordinator: WaviotCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.modem_id}_temperature"

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.get("temperature")


class WaviotLastSeenSensor(WaviotBaseSensor):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Last seen"

    def __init__(self, coordinator: WaviotCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.modem_id}_last_seen"

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.data.get("last_seen")
