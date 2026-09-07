"""DataUpdateCoordinator for WAVIoT."""
from __future__ import annotations

import asyncio
import json
import logging
from functools import partial
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
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
    BACKFILL_CHUNK_DAYS,
    BACKFILL_EMPTY_CHUNKS,
    BACKFILL_ERROR_CHUNKS,
    BACKFILL_MAX_DAYS,
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
from .prices import PriceSchedule, cost_series, parse_prices, price_at

_LOGGER = logging.getLogger(__name__)

PRICES_STORAGE_VERSION = 1

# Start of the window used to re-read the whole kWh series. Statistics are
# never older than this, and the recorder does not mind a wide range.
_STATS_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


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
        super().__init__(hass, _LOGGER, config_entry=entry, **coordinator_kwargs)
        self.entry = entry
        self.client = client
        self.modem_id = modem_id
        self.channels: list[str] = []
        self._channels_discovered = False
        self._stats_backfilled: set[str] = set()
        # Channels whose cost statistics must be rebuilt from scratch
        # because the tariff schedule changed, and the kWh rows written for
        # them in the current poll (see _async_rebuild_cost).
        self._cost_rebuild: set[str] = set()
        self._pending_energy: dict[str, list[StatisticData]] = {}
        self._prices_store: Store[dict[str, Any]] = Store(
            hass, PRICES_STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.prices"
        )

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
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
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

        await self._async_check_price_changes()

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
                # Only API trouble is tolerated here: a failing channel must
                # not kill the poll, but anything else has to surface instead
                # of being swallowed poll after poll.
                try:
                    await self._async_import_statistics(channel, values)
                except WaviotApiError as err:
                    _LOGGER.warning(
                        "Failed to import statistics for channel %s: %s",
                        channel,
                        err,
                    )

        if import_stats:
            # After the kWh rows of this poll are queued, so the rebuild
            # prices the freshest history available.
            for channel in list(self._cost_rebuild):
                try:
                    await self._async_rebuild_cost(channel)
                except Exception:  # noqa: BLE001 - the poll must survive
                    # The cost rows were dropped, so the regular import
                    # rebuilds them from the API on the next poll instead.
                    _LOGGER.exception(
                        "Failed to rebuild cost statistics for channel %s",
                        channel,
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

    def _parse_prices(self) -> dict[str, PriceSchedule]:
        """Tariff schedules from the prices option, bad entries skipped."""
        schedules, bad = parse_prices(self.entry.options.get(CONF_PRICES, ""))
        for entry in bad:
            _LOGGER.warning("Ignoring invalid price entry: %s", entry)
        return schedules

    async def _async_check_price_changes(self) -> None:
        """Rebuild cost statistics when the tariff schedule was edited.

        Cost rows carry a running sum, so an edited price only reaches the
        rows imported after the edit. Whenever the schedule changes we drop
        the cost series and rebuild it from the kWh statistics later in
        this poll; the kWh series itself is untouched.
        """
        schedules = self._parse_prices()
        signature = json.dumps(
            {
                channel: [
                    [start.isoformat() if start else None, price]
                    for start, price in entries
                ]
                for channel, entries in sorted(schedules.items())
            },
            sort_keys=True,
        )
        stored = await self._prices_store.async_load() or {}
        previous = stored.get("signature")
        if previous == signature:
            return

        await self._prices_store.async_save({"signature": signature})
        if previous is None:
            # First run with this store: nothing is known to be stale.
            return

        _LOGGER.info(
            "WAVIoT %s: tariffs changed, rebuilding cost statistics",
            self.modem_id,
        )
        cost_ids = [f"{self.statistic_id(ch)}_cost" for ch in self.channels]
        if cost_ids:
            # Queued on the recorder, so it lands before the rows the
            # rebuild writes later in this poll.
            get_instance(self.hass).async_clear_statistics(cost_ids)
        self._cost_rebuild.update(self.channels)

    async def _async_rebuild_cost(self, channel: str) -> None:
        """Recompute one cost series from the kWh statistics.

        The kWh series is the local record of the meter, hour by hour and
        for as long as the recorder has kept it, so it - not the API - is
        what the new tariffs are applied to: it covers everything ever
        imported, however old, and costs nothing in API calls.
        """
        self._cost_rebuild.discard(channel)
        schedule = self._parse_prices().get(channel)
        if not schedule:
            return  # prices removed: the cleared cost series stays gone

        base_id = self.statistic_id(channel)
        period = await get_instance(self.hass).async_add_executor_job(
            partial(
                statistics_during_period,
                self.hass,
                _STATS_EPOCH,
                None,
                statistic_ids={base_id},
                period="hour",
                units=None,
                types={"state", "sum"},
            )
        )
        # Rows just written in this poll are not necessarily visible to the
        # read above, so they are merged in; both are the same computation,
        # so the running kWh sums line up.
        hourly: dict[datetime, tuple[float | None, float]] = {
            _stat_row_start(row): (row.get("state"), row["sum"])
            for row in period.get(base_id) or []
            if row.get("sum") is not None
        }
        for row in self._pending_energy.pop(channel, []):
            if row.get("sum") is not None:
                hourly[row["start"]] = (row.get("state"), row["sum"])
        if not hourly:
            # Nothing imported yet; the regular path builds both series.
            return

        priced = cost_series(
            ((hour, state, total) for hour, (state, total) in sorted(hourly.items())),
            schedule,
        )
        if not priced:
            return

        statistics = [
            StatisticData(start=hour, state=state, sum=total)
            for hour, state, total in priced
        ]
        name = f"WAVIoT {self.modem_id} {CHANNEL_NAMES.get(channel, channel)} cost"
        meta: dict = dict(_MEAN_KWARGS)
        meta["has_sum"] = True
        meta["name"] = name
        meta["source"] = STATISTICS_SOURCE
        meta["statistic_id"] = f"{base_id}_cost"
        meta["unit_of_measurement"] = self.hass.config.currency or "RUB"
        if _UNIT_CLASS_SUPPORTED:
            meta["unit_class"] = None  # a currency has no unit converter
        async_add_external_statistics(self.hass, StatisticMetaData(**meta), statistics)
        _LOGGER.info(
            "WAVIoT %s: rebuilt %d cost rows for %s (from %s)",
            self.modem_id,
            len(statistics),
            channel,
            priced[0][0].date(),
        )

    async def _fetch_backfill(self, channel: str) -> dict[int, float]:
        """Fetch the history of one channel, as deep as the API has it.

        Queried in BACKFILL_CHUNK_DAYS windows, newest first: a single
        multi-year request is slow and trips server-side rate limits. The
        walk goes back until BACKFILL_EMPTY_CHUNKS windows in a row come
        back empty (the meter's history has ended) or BACKFILL_MAX_DAYS is
        reached, so meters that keep more than a year are not truncated to
        a guessed depth.

        Windows that fail are logged and skipped without counting as the
        end of history; whatever was collected is still usable, and an
        empty result makes the caller retry on the next poll.
        """
        now = int(datetime.now(tz=timezone.utc).timestamp())
        floor_ts = now - BACKFILL_MAX_DAYS * 86400
        collected: dict[int, float] = {}
        empty_run = 0
        error_run = 0

        window_to = now
        while window_to > floor_ts and empty_run < BACKFILL_EMPTY_CHUNKS:
            window_from = max(floor_ts, window_to - BACKFILL_CHUNK_DAYS * 86400)
            try:
                chunk = await self.client.channel_values(
                    self.modem_id, channel, ts_from=window_from, ts_to=window_to
                )
            except WaviotApiError as err:
                _LOGGER.warning(
                    "Backfill of channel %s (%s..%s) failed: %s",
                    channel,
                    datetime.fromtimestamp(window_from, tz=timezone.utc).date(),
                    datetime.fromtimestamp(window_to, tz=timezone.utc).date(),
                    err,
                )
                error_run += 1
                if error_run >= BACKFILL_ERROR_CHUNKS:
                    _LOGGER.warning(
                        "WAVIoT %s: backfill of channel %s stopped after %d "
                        "failed windows in a row",
                        self.modem_id,
                        channel,
                        error_run,
                    )
                    break
            else:
                error_run = 0
                if chunk:
                    collected.update(chunk)
                    empty_run = 0
                else:
                    empty_run += 1
            await asyncio.sleep(0.5)  # be gentle with the API
            window_to = window_from

        if collected:
            _LOGGER.info(
                "WAVIoT %s: backfill of channel %s collected %d readings "
                "back to %s",
                self.modem_id,
                channel,
                len(collected),
                datetime.fromtimestamp(min(collected), tz=timezone.utc).date(),
            )
        else:
            _LOGGER.info(
                "WAVIoT %s: backfill of channel %s found no readings",
                self.modem_id,
                channel,
            )
        return collected

    async def _async_import_statistics(
        self, channel: str, recent_values: dict[int, float]
    ) -> None:
        """Import energy (kWh) and, if a price is set, cost statistics."""
        base_id = self.statistic_id(channel)
        base_name = f"WAVIoT {self.modem_id} {CHANNEL_NAMES.get(channel, channel)}"
        schedule = self._parse_prices().get(channel)

        # (statistic_id, name, unit, unit_class, tariffs) - tariffs is None
        # for the kWh series, a schedule for the cost one.
        series: list[tuple[str, str, str, str | None, PriceSchedule | None]] = [
            (base_id, base_name, UnitOfEnergy.KILO_WATT_HOUR, "energy", None)
        ]
        if schedule:
            currency = self.hass.config.currency or "RUB"
            series.append(
                (f"{base_id}_cost", f"{base_name} cost", currency, None, schedule)
            )

        if channel in self._cost_rebuild:
            # The cost rows were just dropped and are about to be rebuilt
            # from the kWh series; adding to them now would race the clear.
            series = series[:1]

        # Fetch last imported row for every series
        last: dict[str, dict | None] = {}
        need_backfill = False
        for stat_id, _, _, _, tariffs in series:
            stats = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics, self.hass, 1, stat_id, True, {"state", "sum"}
            )
            row = stats.get(stat_id)
            last[stat_id] = row[0] if row else None
            if not row:
                need_backfill = True

        values = recent_values
        if need_backfill and channel not in self._stats_backfilled:
            history = await self._fetch_backfill(channel)
            if history:
                # Recent values win: they are the freshest read of the meter.
                values = {**history, **recent_values}
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

        for stat_id, name, unit, unit_class, tariffs in series:
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
                run_sum += diff * (
                    1.0 if tariffs is None else price_at(tariffs, hour)
                )
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
            if _UNIT_CLASS_SUPPORTED:
                # Always declare it, None included: cores from 2026.11 on
                # stop deriving it from the unit, and a currency has no
                # converter to derive it from anyway.
                base_meta["unit_class"] = unit_class

            if tariffs is None and channel in self._cost_rebuild:
                # The rebuild reads the kWh series back from the recorder,
                # which may not have committed these rows yet.
                self._pending_energy[channel] = list(statistics)

            metadata = StatisticMetaData(**base_meta)
            async_add_external_statistics(self.hass, metadata, statistics)
            _LOGGER.debug(
                "Imported %d statistic rows for %s", len(statistics), stat_id
            )


def _stat_row_start(row: Any) -> datetime:
    """Start of a statistics row, which cores report as ts or datetime."""
    start = row["start"]
    if isinstance(start, datetime):
        return start
    return datetime.fromtimestamp(start, tz=timezone.utc)


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
