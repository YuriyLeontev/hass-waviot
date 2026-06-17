"""DataUpdateCoordinator for WAVIoT."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

try:  # HA 2025.x+: mean_type replaces has_mean
    from homeassistant.components.recorder.models import StatisticMeanType

    _MEAN_KWARGS: dict = {"mean_type": StatisticMeanType.NONE}
except ImportError:  # older cores
    _MEAN_KWARGS = {"has_mean": False}


def _statistic_meta_keys() -> set[str]:
    """All valid keys of the StatisticMetaData TypedDict on this core.

    __required_keys__/__optional_keys__ are reliable across Python versions,
    unlike __annotations__ which may omit inherited keys on 3.14+.
    """
    keys: set[str] = set()
    for attr in ("__required_keys__", "__optional_keys__", "__annotations__"):
        keys |= set(getattr(StatisticMetaData, attr, ()) or ())
    return keys


_UNIT_CLASS_SUPPORTED = "unit_class" in _statistic_meta_keys()

from .api import WaviotApiClient, WaviotApiError, WaviotAuthError
from .const import (
    BACKFILL_DAYS,
    CHANNEL_NAMES,
    CONF_PRICES,
    ENERGY_CHANNEL_PREFIX,
    CONF_CHANNELS,
    CONF_IMPORT_STATISTICS,
    DEFAULT_ENERGY_CHANNELS,
    DEFAULT_UPDATE_INTERVAL_MIN,
    DOMAIN,
    FETCH_WINDOW_DAYS,
    STATISTICS_SOURCE,
)

_LOGGER = logging.getLogger(__name__)


class WaviotCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches modem info + channel readings and feeds HA statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: WaviotApiClient,
        modem_id: str,
    ) -> None:
        coordinator_kwargs: dict[str, Any] = {
            "name": f"WAVIoT {modem_id}",
            "update_interval": timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MIN),
        }
        try:
            super().__init__(hass, _LOGGER, config_entry=entry, **coordinator_kwargs)
        except TypeError:
            super().__init__(hass, _LOGGER, **coordinator_kwargs)
        self.entry = entry
        self.client = client
        self.modem_id = modem_id
        self.channels: list[str] = []
        self._channels_discovered = False
        self._stats_backfilled: set[str] = set()

    # ------------------------------------------------------------------ #
    # Channel discovery
    # ------------------------------------------------------------------ #

    async def _discover_channels(self) -> list[str]:
        """Figure out which energy channels this meter actually has."""
        configured = self.entry.options.get(
            CONF_CHANNELS, self.entry.data.get(CONF_CHANNELS)
        )
        if configured:
            channels = [c.strip() for c in str(configured).split(",") if c.strip()]
            _LOGGER.debug("Using configured channels: %s", channels)
            return channels

        candidates: list[str] = list(DEFAULT_ENERGY_CHANNELS)
        # Channels that get_values reports as having data (last_value /
        # values present). Used to avoid probing dead channels.
        active_hint: set[str] = set()
        hints_available = False

        # Learn channel ids and activity from api.data/get_values
        try:
            now = int(datetime.now(tz=timezone.utc).timestamp())
            payload = await self.client.get_values(
                self.modem_id, ts_from=now - 30 * 86400, ts_to=now
            )
            for reg in self._iter_registrators(payload):
                channel = reg.get("channel") or reg.get("channel_id")
                if not isinstance(channel, str):
                    continue
                # Only active imported energy; FOBOS exposes ~50 channels
                # (voltages, cos-fi, ...) and probing all of them is slow
                # and trips server-side rate limits.
                if not channel.startswith(ENERGY_CHANNEL_PREFIX):
                    continue
                hints_available = True
                if channel not in candidates:
                    candidates.append(channel)
                if reg.get("last_value") is not None or reg.get("values"):
                    active_hint.add(channel)
        except WaviotApiError as err:
            _LOGGER.debug("get_values discovery failed (non-fatal): %s", err)

        if hints_available:
            candidates = [c for c in candidates if c in active_hint]

        # Probe remaining candidates: keep only channels that return data
        found: list[str] = []
        now = int(datetime.now(tz=timezone.utc).timestamp())
        for channel in candidates:
            try:
                values = await self.client.channel_values(
                    self.modem_id, channel, ts_from=now - 90 * 86400, ts_to=now
                )
            except WaviotApiError as err:
                _LOGGER.debug("Probe of channel %s failed: %s", channel, err)
                continue
            finally:
                await asyncio.sleep(0.5)  # be gentle with the API
            if values:
                found.append(channel)
        _LOGGER.info("WAVIoT %s: discovered channels %s", self.modem_id, found)
        return found

    @staticmethod
    def _iter_registrators(payload: dict) -> list[dict]:
        regs = payload.get("registrators")
        out: list[dict] = []
        if isinstance(regs, dict):
            for value in regs.values():
                if isinstance(value, dict):
                    out.append(value)
        elif isinstance(regs, list):
            out.extend(v for v in regs if isinstance(v, dict))
        return out

    # ------------------------------------------------------------------ #
    # Update
    # ------------------------------------------------------------------ #

    async def _async_update_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"channels": {}}

        try:
            modem = await self.client.modem_info(self.modem_id)
        except WaviotAuthError as err:
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except WaviotApiError as err:
            raise UpdateFailed(str(err)) from err

        data["battery"] = _to_float(modem.get("battery"))
        data["temperature"] = _to_float(modem.get("temperature"))
        station_dt = _to_dt(modem.get("last_station_time")) or _to_dt(
            modem.get("last_info_message")
        )
        _LOGGER.debug(
            "WAVIoT %s poll: last_station_time=%s battery=%s temp=%s",
            self.modem_id,
            modem.get("last_station_time"),
            modem.get("battery"),
            modem.get("temperature"),
        )
        reading_times: list[datetime | None] = []

        if not self._channels_discovered:
            self.channels = await self._discover_channels()
            self._channels_discovered = True

        now = int(datetime.now(tz=timezone.utc).timestamp())
        window_from = now - FETCH_WINDOW_DAYS * 86400

        import_stats = self.entry.options.get(CONF_IMPORT_STATISTICS, True)

        for channel in self.channels:
            try:
                values = await self.client.channel_values(
                    self.modem_id, channel, ts_from=window_from, ts_to=now
                )
            except WaviotApiError as err:
                _LOGGER.warning("Failed to fetch channel %s: %s", channel, err)
                await asyncio.sleep(0.5)
                continue
            await asyncio.sleep(0.5)  # be gentle with the API

            if values:
                last_ts, last_val = max(values.items())
                reading_dt = _to_dt(last_ts)
                data["channels"][channel] = {
                    "value": last_val,
                    "ts": reading_dt,
                }
                reading_times.append(reading_dt)
            else:
                data["channels"][channel] = {"value": None, "ts": None}

            if import_stats:
                try:
                    await self._async_import_statistics(channel, values)
                except Exception:  # noqa: BLE001 - stats must never kill updates
                    _LOGGER.exception(
                        "Failed to import statistics for channel %s", channel
                    )

        reads = [d for d in reading_times if d is not None]
        last_reading = max(reads) if reads else None
        data["last_reading"] = last_reading
        data["last_seen"] = last_reading or station_dt
        return data

    # ------------------------------------------------------------------ #
    # Long-term statistics import (external statistics, like Tibber)
    # ------------------------------------------------------------------ #

    def statistic_id(self, channel: str) -> str:
        return f"{STATISTICS_SOURCE}:{self.modem_id.lower()}_{channel}"

    def _parse_prices(self) -> dict[str, float]:
        """Parse the prices option: "t1=6.97,t2=3.86" or full channel names.

        Decimal comma is accepted ("6,97") when pairs are separated by ";".
        """
        raw = str(self.entry.options.get(CONF_PRICES, "") or "").strip()
        if not raw:
            return {}
        sep = ";" if ";" in raw else ","
        prices: dict[str, float] = {}
        for pair in raw.split(sep):
            if "=" not in pair:
                continue
            key, value = pair.split("=", 1)
            key = key.strip().lower()
            if key in ("t1", "t2", "t3", "t4", "tsum"):
                key = f"{ENERGY_CHANNEL_PREFIX}_{key}"
            try:
                prices[key] = float(value.strip().replace(",", "."))
            except ValueError:
                _LOGGER.warning("Ignoring invalid price entry: %s", pair)
        return prices

    async def _async_import_statistics(
        self, channel: str, recent_values: dict[int, float]
    ) -> None:
        """Import energy (kWh) and, if a price is set, cost statistics."""
        base_id = self.statistic_id(channel)
        base_name = f"WAVIoT {self.modem_id} {CHANNEL_NAMES.get(channel, channel)}"
        price = self._parse_prices().get(channel)

        # (statistic_id, name, unit, unit_class, factor)
        series: list[tuple[str, str, str, str | None, float]] = [
            (base_id, base_name, UnitOfEnergy.KILO_WATT_HOUR, "energy", 1.0)
        ]
        if price is not None:
            currency = self.hass.config.currency or "RUB"
            series.append(
                (f"{base_id}_cost", f"{base_name} cost", currency, None, price)
            )

        # Fetch last imported row for every series
        last: dict[str, dict | None] = {}
        need_backfill = False
        for stat_id, _, _, _, _ in series:
            stats = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics, self.hass, 1, stat_id, True, {"state", "sum"}
            )
            row = stats.get(stat_id)
            last[stat_id] = row[0] if row else None
            if not row:
                need_backfill = True

        values = recent_values
        if need_backfill and channel not in self._stats_backfilled:
            values = await self._fetch_backfill(channel)
            self._stats_backfilled.add(channel)

        # Collapse readings to one per hour (last reading inside the hour).
        hourly: dict[datetime, float] = {}
        for ts, val in sorted(values.items()):
            hour = datetime.fromtimestamp(ts, tz=timezone.utc).replace(
                minute=0, second=0, microsecond=0
            )
            hourly[hour] = val
        if not hourly:
            return

        for stat_id, name, unit, unit_class, factor in series:
            row = last[stat_id]
            if row is not None:
                run_sum = row.get("sum") or 0.0
                # state always stores the raw meter reading, for both series
                last_state: float | None = row.get("state")
                last_start: datetime | None = datetime.fromtimestamp(
                    row["start"], tz=timezone.utc
                )
            else:
                run_sum = 0.0
                last_state = None
                last_start = None

            statistics: list[StatisticData] = []
            for hour, state in sorted(hourly.items()):
                if last_start is not None and hour <= last_start:
                    continue
                if last_state is None:
                    diff = 0.0
                else:
                    diff = state - last_state
                    if diff < 0:  # meter reset/replacement
                        diff = 0.0
                run_sum += diff * factor
                last_state = state
                last_start = hour
                statistics.append(
                    StatisticData(start=hour, state=state, sum=run_sum)
                )

            if not statistics:
                continue

            base_meta: dict = dict(_MEAN_KWARGS)
            base_meta["has_sum"] = True
            base_meta["name"] = name
            base_meta["source"] = STATISTICS_SOURCE
            base_meta["statistic_id"] = stat_id
            base_meta["unit_of_measurement"] = unit
            if unit_class is not None and _UNIT_CLASS_SUPPORTED:
                base_meta["unit_class"] = unit_class

            metadata = StatisticMetaData(**base_meta)
            async_add_external_statistics(self.hass, metadata, statistics)
            _LOGGER.debug(
                "Imported %d statistic rows for %s", len(statistics), stat_id
            )


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_dt(value: Any) -> datetime | None:
    try:
        ts = int(value)
        if ts > 10**12:
            ts //= 1000
        if ts <= 0:
            return None
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (TypeError, ValueError):
        return None
